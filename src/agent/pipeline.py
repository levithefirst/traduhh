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


def _persist_idea(conn, *, idea_id, target, detection, geometry, costs, gates, features, regime, ctx, book, decision, reasons, strategy_version_id, llm_review=None, prompt_version_id=None, confidence=None, code_decision_before_llm=None, code_would_take=False, llm_involved=False):
    payload = {"setup_id": detection.setup_id, "direction": detection.direction, "evidence": detection.evidence}
    packet_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    if confidence is None:
        confidence = float(regime.get("confidence", 0) or 0)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO ideas(id,created_at,asset,timeframe,direction,setup_id,strategy_version_id,prompt_version_id,bar_open_time,decision,decision_reason,gates,geometry,costs,features,regime,ctx,book,news,calendar,hist_cell,llm_review,packet_hash,data_quality,confidence,code_decision_before_llm,code_would_take,llm_involved)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,'[]'::jsonb,'[]'::jsonb,'{}'::jsonb,%s::jsonb,%s,%s::jsonb,%s,%s,%s,%s)
            ON CONFLICT (asset,timeframe,setup_id,bar_open_time,strategy_version_id) DO UPDATE SET decision=EXCLUDED.decision,decision_reason=EXCLUDED.decision_reason,gates=EXCLUDED.gates,geometry=EXCLUDED.geometry,costs=EXCLUDED.costs,features=EXCLUDED.features,regime=EXCLUDED.regime,ctx=EXCLUDED.ctx,book=EXCLUDED.book,packet_hash=EXCLUDED.packet_hash,data_quality=EXCLUDED.data_quality,confidence=EXCLUDED.confidence,llm_review=EXCLUDED.llm_review,prompt_version_id=EXCLUDED.prompt_version_id,code_decision_before_llm=EXCLUDED.code_decision_before_llm,code_would_take=EXCLUDED.code_would_take,llm_involved=EXCLUDED.llm_involved""",
            (str(idea_id),utc_now(),detection.asset,detection.timeframe,detection.direction,detection.setup_id,strategy_version_id,prompt_version_id,target,decision,reasons,
             json.dumps({"hard":gates.hard,"soft":gates.soft},separators=(",",":")),json.dumps(geometry.to_dict(),separators=(",",":")),json.dumps(costs.to_dict(),separators=(",",":")),json.dumps(features,separators=(",",":"),default=str),json.dumps(regime,separators=(",",":"),default=str),json.dumps(ctx,separators=(",",":"),default=str),json.dumps(book,separators=(",",":"),default=str),
             json.dumps(llm_review,separators=(",",":"),default=str) if llm_review is not None else None,
             packet_hash,json.dumps({"lookahead_protected":True},separators=(",",":")),float(confidence),
             code_decision_before_llm,bool(code_would_take),bool(llm_involved)))
    return packet_hash


MIN_FINAL_CONFIDENCE = 0.35
FUNDING_WAIT_MINUTES = 5


def _hist_cell(conn, *, asset: str, timeframe: str, setup_id: str, regime_primary) -> dict[str, Any]:
    """Historical cell support for this candidate (spec 10 step 4/14).

    Reports only what closed paper positions actually show. A thin cell is
    labelled unproven; it is never presented as an edge.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT count(*), avg(p.realized_r), stddev_samp(p.realized_r),
                      avg(CASE WHEN p.realized_r > 0 THEN 1.0 ELSE 0.0 END)
               FROM paper_positions p JOIN ideas i ON i.id = p.idea_id
               WHERE p.status='CLOSED' AND i.asset=%s AND i.timeframe=%s AND i.setup_id=%s""",
            (asset, timeframe, setup_id),
        )
        row = cur.fetchone()
    n = int(row[0] or 0)
    note = "unproven" if n < 30 else ("tentative" if n < 80 else "ok")
    return {
        "n": n,
        "mean_r": float(row[1]) if row[1] is not None else None,
        "std_r": float(row[2]) if row[2] is not None else None,
        "win_rate": float(row[3]) if row[3] is not None else None,
        "note": note,
        "regime_primary": regime_primary,
    }


def minutes_to_funding_hour(now: datetime) -> float:
    """Minutes until the next funding hour boundary (spec 10 step 17)."""
    current = require_utc(now)
    return (60 - current.minute - current.second / 60.0) % 60


def review_candidate(conn, *, settings, idea_id, detection, geometry, costs, gates, features,
                     regime, ctx, book, hist_cell, portfolio, llm_client, asof: datetime):
    """Spec 10 steps 15-18. Called ONLY when every hard gate has passed.

    Returns (final_decision, reasons, llm_review_json, prompt_version_id, confidence).
    The LLM can only hold the decision back; it can never create or widen one.
    """
    from agent.llm.client import persist_review
    from agent.llm.packet import build_packet, packet_price_allowlist
    from agent.llm.schema import final_confidence, resolve_after_llm

    if not gates.passed:
        raise AssertionError("review_candidate called with failing hard gates")

    prompt_version = llm_client.prompt_version_id
    packet = build_packet(
        idea_id=str(idea_id), ts_utc=require_utc(asof).isoformat(), asset=detection.asset,
        timeframe=detection.timeframe, strategy_version_id=STRATEGY_VERSION_ID,
        prompt_version_id=prompt_version, mode="paper",
        data_quality={"ok": True, "flags": []}, regime=regime,
        setup={"id": detection.setup_id, "direction": detection.direction,
               "levels": {"entry": geometry.entry, "stop": geometry.stop, "targets": list(geometry.targets)}},
        features=features,
        book=book or {},
        derivatives={"funding": features.get("funding"), "funding_z_168": features.get("funding_z_168"),
                     "oi": features.get("oi"), "oi_chg_24h": features.get("oi_chg_24h"),
                     "basis_bps": features.get("basis_bps")},
        costs=costs.to_dict(), portfolio=portfolio, news=[], calendar=[],
        hist_cell=hist_cell, gates_passed=sorted(name for name, ok in gates.hard.items() if ok),
    )

    result = llm_client.review(packet)
    try:
        persist_review(conn, idea_id=str(idea_id), result=result)
    except Exception:  # journaling the review must not change the decision
        pass

    outcome = resolve_after_llm(decision=result.decision, error=result.error,
                                allowlist=packet_price_allowlist(packet))
    reasons = list(outcome.reasons)
    decision = outcome.decision

    confidence = final_confidence(regime_confidence=float(regime.get("confidence") or 0.0),
                                  hist_n=int(hist_cell.get("n", 0) or 0), agreement=outcome.agreement)
    if decision == "TRADE_PAPER" and confidence < MIN_FINAL_CONFIDENCE:
        decision = "NO_TRADE"
        reasons.append("confidence_below_floor")
    if decision == "TRADE_PAPER" and detection.timeframe == "15m" and minutes_to_funding_hour(asof) < FUNDING_WAIT_MINUTES:
        decision = "WAIT"
        reasons.append("funding_hour_imminent")

    review_json = result.decision.to_dict() if result.decision else {"error": result.error}
    review_json["resolved_decision"] = decision
    return decision, reasons, review_json, prompt_version, confidence


def evaluate(conn, *, settings, asset: str, timeframe: str, bar_open_time: datetime | None = None, llm_client=None) -> list[dict[str, Any]]:
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
        code_decision=c["decision"]
        code_would_take=code_decision=="GATED_PASS"
        decision=code_decision
        reasons=c["reasons"]
        llm_review=None; prompt_version_id=None; confidence=None; llm_involved=False
        # Spec 11.1: the LLM is consulted only when every hard gate passed and the
        # candidate is still alive. A failed gate never reaches this branch.
        if code_would_take and llm_client is not None:
            hist_cell=_hist_cell(conn,asset=asset,timeframe=timeframe,setup_id=detection.setup_id,regime_primary=effective_regime.get("label"))
            portfolio={"equity":float(settings.paper_equity_usd),"open_positions":[],"day_pnl_pct":0.0,"cluster":None}
            decision,reasons,llm_review,prompt_version_id,confidence=review_candidate(
                conn,settings=settings,idea_id=idea_id,detection=detection,geometry=geometry,costs=costs,
                gates=gates,features=features,regime=effective_regime,ctx=ctx,book=book,hist_cell=hist_cell,
                portfolio=portfolio,llm_client=llm_client,asof=asof)
            reasons=list(c["reasons"])+list(reasons)
            llm_involved=True
        elif code_would_take:
            # No reviewer configured: the candidate stays gated, never auto-promoted.
            decision="GATED_PASS"
        packet_hash=_persist_idea(conn,idea_id=idea_id,target=target,detection=detection,geometry=geometry,costs=costs,gates=gates,features=features,regime=effective_regime,ctx=ctx,book=book,decision=decision,reasons=reasons,strategy_version_id=strategy_version_id,llm_review=llm_review,prompt_version_id=prompt_version_id,confidence=confidence,code_decision_before_llm=code_decision,code_would_take=code_would_take,llm_involved=llm_involved)
        results.append({"idea_id":str(idea_id),"setup_id":detection.setup_id,"direction":detection.direction,"decision":decision,"code_decision_before_llm":code_decision,"planned_r_after_costs":costs.planned_r_after_costs,"packet_hash":packet_hash})
    # No scan_events table is created because §5 does not define its schema. A cell
    # with no detector is intentionally not fabricated into an idea.
    return results


def scan_closed_bar(conn, *, settings, asset: str, timeframe: str, bar_open_time: datetime, llm_client=None) -> list[dict[str, Any]]:
    return evaluate(conn, settings=settings, asset=asset, timeframe=timeframe, bar_open_time=bar_open_time, llm_client=llm_client)
