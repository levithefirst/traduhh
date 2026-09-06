from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.config import FROZEN_ASSETS, FROZEN_TIMEFRAMES
from agent.costs import calculate_costs
from agent.geometry import build_geometry
from agent.gates import evaluate_gates
from agent.setups import detect_breakout_retest, detect_sweep_reclaim, detect_trend_pullback
from agent.timeutil import require_utc, utc_now
from agent.versioning import STRATEGY_VERSION_ID, ensure_strategy_version

IDEA_NAMESPACE = uuid.UUID("2a52f5c5-6b2f-5e36-9e65-2bcb3d1f6f0f")


def deterministic_idea_id(*, asset: str, timeframe: str, setup_id: str, direction: str, bar_open_time: datetime, strategy_version_id: str, evidence: dict[str, Any]) -> uuid.UUID:
    payload = {"asset": asset, "timeframe": timeframe, "setup_id": setup_id, "direction": direction,
               "bar_open_time": require_utc(bar_open_time).isoformat(), "strategy_version_id": strategy_version_id,
               "evidence": evidence}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return uuid.uuid5(IDEA_NAMESPACE, digest)


def _rowdicts(rows, columns):
    return [dict(zip(columns, row)) for row in rows]


def _load_scan_inputs(conn, asset: str, timeframe: str, target: datetime):
    target = require_utc(target)
    with conn.cursor() as cur:
        cur.execute("SELECT open_time,close_time,o,h,l,c,v,n_trades FROM candles WHERE asset=%s AND timeframe=%s AND open_time<=%s AND close_time<=%s ORDER BY open_time DESC LIMIT 400", (asset,timeframe,target,target+timedelta(seconds={"15m":900,"1h":3600,"4h":14400}[timeframe])))
        candles = list(reversed(cur.fetchall()))
        cur.execute("SELECT features FROM feature_snapshots WHERE asset=%s AND timeframe=%s AND open_time=%s", (asset,timeframe,target))
        feature_row = cur.fetchone()
        cur.execute("SELECT label,secondary,confidence,features_used FROM regime_snapshots WHERE asset=%s AND timeframe=%s AND open_time=%s", (asset,timeframe,target))
        regime_row = cur.fetchone()
        cur.execute("SELECT ts,mid,mark,oracle,funding,premium,oi,day_ntl_vlm FROM asset_ctx WHERE asset=%s AND ts<=%s ORDER BY ts DESC LIMIT 1", (asset,target+timedelta(seconds={"15m":900,"1h":3600,"4h":14400}[timeframe])))
        ctx_row = cur.fetchone()
        cur.execute("SELECT ts,spread_bps,imbalance_5,notional_to_10bps,bid1,ask1 FROM book_snapshots WHERE asset=%s AND ts<=%s ORDER BY ts DESC LIMIT 1", (asset,target+timedelta(seconds={"15m":900,"1h":3600,"4h":14400}[timeframe])))
        book_row = cur.fetchone()
        cur.execute("SELECT value FROM system_state WHERE key='integrity'")
        integrity_row = cur.fetchone()
        cur.execute("SELECT value FROM system_state WHERE key='mode'")
        mode_row = cur.fetchone()
        htf_regime = None
        if timeframe in {"15m", "1h"}:
            cur.execute("SELECT label,secondary,confidence,features_used FROM regime_snapshots WHERE asset=%s AND timeframe='4h' AND open_time<=%s ORDER BY open_time DESC LIMIT 1", (asset,target))
            hrow = cur.fetchone()
            if hrow:
                htf_regime = {"label": hrow[0], "secondary": hrow[1] or [], "confidence": float(hrow[2]), "features_used": hrow[3]}
    candles = _rowdicts(candles,("open_time","close_time","o","h","l","c","v","n_trades"))
    features = feature_row[0] if feature_row else None
    if isinstance(features, str): features = json.loads(features)
    regime = None
    if regime_row:
        regime = {"label": regime_row[0], "secondary": regime_row[1] or [], "confidence": float(regime_row[2]), "features_used": regime_row[3]}
    ctx = _rowdicts([ctx_row],("ts","mid","mark","oracle","funding","premium","oi","day_ntl_vlm"))[0] if ctx_row else None
    book = _rowdicts([book_row],("ts","spread_bps","imbalance_5","notional_to_10bps","bid1","ask1"))[0] if book_row else None
    integrity = integrity_row[0] if integrity_row else {"ok": True, "flags": []}
    if isinstance(integrity, str): integrity=json.loads(integrity)
    return candles, features or {}, regime or {}, ctx, book, integrity, htf_regime, (mode_row[0] if mode_row else None)


def _detectors(candles, *, asset, timeframe, features, regime, target):
    kwargs = dict(rows=candles, asset=asset, timeframe=timeframe, features=features, regime=regime, bar_open_time=target)
    return [d for d in (detect_trend_pullback(**kwargs), detect_breakout_retest(**kwargs), detect_sweep_reclaim(**kwargs)) if d is not None]


def _freshness(now: datetime, row: dict[str, Any] | None) -> float | None:
    if not row or not isinstance(row.get("ts"), datetime): return None
    return max(0.0, (require_utc(now) - require_utc(row["ts"])).total_seconds())


def _persist_idea(conn, *, idea_id, target, detection, geometry, costs, gates, features, regime, ctx, book, decision, reasons, strategy_version_id):
    payload = {"setup_id": detection.setup_id, "direction": detection.direction, "evidence": detection.evidence}
    packet_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO ideas(id,created_at,asset,timeframe,direction,setup_id,strategy_version_id,prompt_version_id,bar_open_time,decision,decision_reason,gates,geometry,costs,features,regime,ctx,book,news,calendar,hist_cell,llm_review,packet_hash,data_quality,confidence)
            VALUES (%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,'[]'::jsonb,'[]'::jsonb,'{}'::jsonb,NULL,%s,%s::jsonb,%s)
            ON CONFLICT (asset,timeframe,setup_id,bar_open_time,strategy_version_id) DO UPDATE SET decision=EXCLUDED.decision,decision_reason=EXCLUDED.decision_reason,gates=EXCLUDED.gates,geometry=EXCLUDED.geometry,costs=EXCLUDED.costs,features=EXCLUDED.features,regime=EXCLUDED.regime,ctx=EXCLUDED.ctx,book=EXCLUDED.book,packet_hash=EXCLUDED.packet_hash,data_quality=EXCLUDED.data_quality,confidence=EXCLUDED.confidence""",
            (str(idea_id),utc_now(),detection.asset,detection.timeframe,detection.direction,detection.setup_id,strategy_version_id,target,decision,reasons,
             json.dumps({"hard":gates.hard,"soft":gates.soft},separators=(",",":")),json.dumps(geometry.to_dict(),separators=(",",":")),json.dumps(costs.to_dict(),separators=(",",":")),json.dumps(features,separators=(",",":"),default=str),json.dumps(regime,separators=(",",":"),default=str),json.dumps(ctx,separators=(",",":"),default=str),json.dumps(book,separators=(",",":"),default=str),packet_hash,json.dumps({"lookahead_protected":True},separators=(",",":")),float(regime.get("confidence",0) or 0)))
    return packet_hash


def evaluate(conn, *, settings, asset: str, timeframe: str, bar_open_time: datetime | None = None) -> list[dict[str, Any]]:
    if asset not in FROZEN_ASSETS or timeframe not in FROZEN_TIMEFRAMES:
        raise ValueError("unsupported asset/timeframe")
    target = require_utc(bar_open_time or utc_now())
    candles, features, regime, ctx, book, integrity, htf_regime, mode_state = _load_scan_inputs(conn, asset, timeframe, target)
    if not candles or candles[-1]["open_time"] != target:
        return []
    strategy_version_id = ensure_strategy_version(conn, settings)
    detections = _detectors(candles, asset=asset, timeframe=timeframe, features=features, regime=regime, target=target)
    candidates=[]
    asof = target + timedelta(seconds={"15m":900,"1h":3600,"4h":14400}[timeframe])
    ctx_age=_freshness(asof,ctx); book_age=_freshness(asof,book); mark_present=bool(ctx and ctx.get("mark") is not None)
    effective_regime=dict(regime)
    if timeframe in {"15m", "1h"}:
        effective_regime["higher_tf"] = htf_regime
    halted = bool((mode_state.get("mode") if isinstance(mode_state, dict) else mode_state) == "halted")
    for detection in detections:
        geometry=build_geometry(detection,features=features,settings=settings)
        costs=calculate_costs(notional=geometry.notional,size=geometry.size,notional_to_10bps=features.get("notional_to_10bps"),funding=features.get("funding"),timeframe=timeframe,risk_cash=geometry.risk_cash,raw_r=geometry.raw_r,taker_fee_bps=float(settings.taker_fee_bps),slippage_bps_floor=float(settings.slippage_bps_floor),hold_bars=int(settings.hold_bars_default))
        gates=evaluate_gates(detection=detection,geometry=geometry,costs=costs,features=features,regime=effective_regime,integrity_ok=bool(integrity.get("ok",True)),warmup_ok=True,ctx_age_s=ctx_age,book_age_s=book_age,mark_present=mark_present,current_equity=float(settings.paper_equity_usd),halted=halted,day_ntl_vlm=(ctx or {}).get("day_ntl_vlm"),notional_to_10bps=features.get("notional_to_10bps"),min_r_after_costs=float(settings.min_r_after_costs),max_concurrent=int(settings.max_concurrent_paper))
        candidates.append({"detection":detection,"geometry":geometry,"costs":costs,"gates":gates,"decision":"NO_TRADE" if not gates.passed else "GATED_PASS","reasons":list(gates.reasons)})
    if len(candidates) > 1:
        dirs={c["detection"].direction for c in candidates}
        if len(dirs) > 1:
            for c in candidates:
                c["decision"]="NO_TRADE"; c["reasons"].append("opposite_setup_conflict")
        else:
            winner=max(candidates,key=lambda c:c["costs"].planned_r_after_costs)
            for c in candidates:
                if c is not winner:
                    c["decision"]="NO_TRADE"; c["reasons"].append("dominated")
    results=[]
    for c in candidates:
        detection=c["detection"]; geometry=c["geometry"]; costs=c["costs"]; gates=c["gates"]
        if len(gates.soft)>=3: c["reasons"].append("soft_gate_stack")
        idea_id=deterministic_idea_id(asset=asset,timeframe=timeframe,setup_id=detection.setup_id,direction=detection.direction,bar_open_time=target,strategy_version_id=strategy_version_id,evidence=detection.evidence)
        packet_hash=_persist_idea(conn,idea_id=idea_id,target=target,detection=detection,geometry=geometry,costs=costs,gates=gates,features=features,regime=effective_regime,ctx=ctx,book=book,decision=c["decision"],reasons=c["reasons"],strategy_version_id=strategy_version_id)
        results.append({"idea_id":str(idea_id),"setup_id":detection.setup_id,"direction":detection.direction,"decision":c["decision"],"planned_r_after_costs":costs.planned_r_after_costs,"packet_hash":packet_hash})
    # No scan_events table is created because §5 does not define its schema. A cell
    # with no detector is intentionally not fabricated into an idea.
    return results


def scan_closed_bar(conn, *, settings, asset: str, timeframe: str, bar_open_time: datetime) -> list[dict[str, Any]]:
    return evaluate(conn, settings=settings, asset=asset, timeframe=timeframe, bar_open_time=bar_open_time)
