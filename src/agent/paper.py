"""Step 6: paper position lifecycle and hypothetical entry fills.

PENDING_ENTRY -> OPEN -> CLOSED per spec Part 13.1. The MVP has no intra-bar
limit simulation: a TRADE_PAPER idea receives an immediate hypothetical fill
at model price, so this module only ever creates positions already OPEN.

Fill-price math is pure and DB-free so it can be unit tested directly; the
DB-touching orchestration (create_paper_position) is a thin wrapper that reads
the book/candle inputs and persists the result idempotently.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping

from agent.timeutil import require_utc, utc_now

TF_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400}

ENTRY_KIND = "PAPER_ENTRY"
STOP_KIND = "PAPER_STOP"
TARGET_KIND = "PAPER_TARGET"
TIME_STOP_KIND = "PAPER_TIME_STOP"
HALT_KIND = "PAPER_HALT_FLATTEN"

EXIT_KIND_BY_REASON = {
    "stop": STOP_KIND,
    "target": TARGET_KIND,
    "time_stop": TIME_STOP_KIND,
    "halt_flatten": HALT_KIND,
}

STATUS_OPEN = "OPEN"
STATUS_CLOSED = "CLOSED"


def entry_fill_price(*, direction: str, close: float, bid1: float | None, ask1: float | None, slip_bps: float) -> float:
    """Frozen Step 6 entry fill model (spec 9.2 / 13.1).

    long:  min(ask1, close) * (1 + slip_bps/1e4); ask1 missing -> close * (1 + slip_bps/1e4)
    short: max(bid1, close) * (1 - slip_bps/1e4); bid1 missing -> close * (1 - slip_bps/1e4)
    """
    if direction == "long":
        base = min(ask1, close) if ask1 is not None else close
        return base * (1.0 + slip_bps / 10000.0)
    if direction == "short":
        base = max(bid1, close) if bid1 is not None else close
        return base * (1.0 - slip_bps / 10000.0)
    raise ValueError("direction must be long or short")


def stop_fill_price(*, direction: str, stop: float, slip_bps: float) -> float:
    """Adverse stop slippage (spec 9.2 / 13.1). STOP wins ties; no invented intrabar order."""
    if direction == "long":
        return stop * (1.0 - 0.5 * slip_bps / 10000.0)
    if direction == "short":
        return stop * (1.0 + 0.5 * slip_bps / 10000.0)
    raise ValueError("direction must be long or short")


@dataclass(frozen=True)
class PositionFill:
    idea_id: str
    asset: str
    direction: str
    timeframe: str
    entry_fill: float
    stop: float
    target: float
    size: float
    notional: float
    risk_cash: float
    slip_bps: float
    opened_at: Any


def build_entry(idea: Mapping[str, Any], *, close: float, bid1: float | None, ask1: float | None) -> PositionFill:
    """Pure computation of the immediate hypothetical entry for a TRADE_PAPER idea."""
    if idea["decision"] != "TRADE_PAPER":
        raise ValueError("only a TRADE_PAPER idea may open a paper position")
    geometry = idea["geometry"]
    costs = idea["costs"]
    direction = idea["direction"]
    slip_bps = float(costs["slip_bps"])
    entry_fill = entry_fill_price(direction=direction, close=close, bid1=bid1, ask1=ask1, slip_bps=slip_bps)
    return PositionFill(
        idea_id=str(idea["id"]),
        asset=idea["asset"],
        direction=direction,
        timeframe=idea["timeframe"],
        entry_fill=entry_fill,
        stop=float(geometry["stop"]),
        target=float(geometry["targets"][0]),
        size=float(geometry["size"]),
        notional=float(geometry["notional"]),
        risk_cash=float(geometry["risk_cash"]),
        slip_bps=slip_bps,
        opened_at=idea.get("opened_at") or utc_now(),
    )


def _book_asof(conn, asset: str, asof) -> tuple[float | None, float | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT bid1, ask1 FROM book_snapshots WHERE asset=%s AND ts<=%s ORDER BY ts DESC LIMIT 1",
            (asset, asof),
        )
        row = cur.fetchone()
    if not row:
        return None, None
    bid1 = float(row[0]) if row[0] is not None else None
    ask1 = float(row[1]) if row[1] is not None else None
    return bid1, ask1


def _candle_close(conn, asset: str, timeframe: str, open_time) -> float | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c FROM candles WHERE asset=%s AND timeframe=%s AND open_time=%s",
            (asset, timeframe, open_time),
        )
        row = cur.fetchone()
    return float(row[0]) if row else None


def create_paper_position(conn, idea: Mapping[str, Any]) -> str | None:
    """Idempotently open the single paper position for a TRADE_PAPER idea.

    Returns the position id (new or pre-existing). A duplicate call for the
    same idea_id never creates a second position or a second PAPER_ENTRY fill;
    this is what makes restart safe (spec 15.1 restart recovery).
    """
    idea_id = str(idea["id"])
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM paper_positions WHERE idea_id=%s", (idea_id,))
        existing = cur.fetchone()
    if existing:
        return str(existing[0])

    asset = idea["asset"]
    timeframe = idea["timeframe"]
    bar_open_time = require_utc(idea["bar_open_time"])
    asof = bar_open_time + timedelta(seconds=TF_SECONDS[timeframe])
    close = _candle_close(conn, asset, timeframe, bar_open_time)
    if close is None:
        close = float(idea["geometry"]["entry"])
    bid1, ask1 = _book_asof(conn, asset, asof)

    fill = build_entry(idea, close=close, bid1=bid1, ask1=ask1)
    position_id = str(uuid.uuid4())
    opened_at = utc_now()

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO paper_positions(
                       id, idea_id, asset, direction, tf, status,
                       entry, stop, targets, size, notional, risk_cash,
                       opened_at, closed_at, exit_px, exit_reason, realized_r,
                       pnl_usd, fees_usd, funding_usd, slip_usd,
                       mfe_r, mae_r, mfe_px, mae_px, bars_held, outcome_class,
                       counterfactuals, funding_missing
                   ) VALUES (
                       %s,%s,%s,%s,%s,%s,
                       %s,%s,%s::jsonb,%s,%s,%s,
                       %s,NULL,NULL,NULL,NULL,
                       NULL,NULL,NULL,NULL,
                       NULL,NULL,%s,%s,0,NULL,
                       '{}'::jsonb,false
                   )
                   ON CONFLICT (idea_id) DO NOTHING
                   RETURNING id""",
                (
                    position_id, idea_id, asset, fill.direction, timeframe, STATUS_OPEN,
                    fill.entry_fill, fill.stop, json.dumps([fill.target]), fill.size, fill.notional, fill.risk_cash,
                    opened_at,
                    fill.entry_fill, fill.entry_fill,
                ),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute("SELECT id FROM paper_positions WHERE idea_id=%s", (idea_id,))
                return str(cur.fetchone()[0])

            cur.execute(
                """INSERT INTO paper_fills(id, position_id, ts, side, px, sz, kind, note)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (position_id, kind) DO NOTHING""",
                (
                    str(uuid.uuid4()), position_id, opened_at,
                    "buy" if fill.direction == "long" else "sell",
                    fill.entry_fill, fill.size, ENTRY_KIND, "immediate model-price paper fill",
                ),
            )
    return position_id


def open_positions_for_new_ideas(conn) -> list[str]:
    """Scan for TRADE_PAPER ideas with no paper position yet and open them.

    This is the only bridge between the decision pipeline and Step 6: it does
    not alter how a decision becomes TRADE_PAPER (that is the unimplemented
    Step 8 LLM veto layer), it only reacts to ideas already carrying that
    decision.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT i.id, i.asset, i.timeframe, i.direction, i.bar_open_time,
                      i.decision, i.geometry, i.costs
               FROM ideas i
               WHERE i.decision = 'TRADE_PAPER'
                 AND NOT EXISTS (SELECT 1 FROM paper_positions p WHERE p.idea_id = i.id)"""
        )
        rows = cur.fetchall()
    opened: list[str] = []
    for row in rows:
        idea = {
            "id": row[0], "asset": row[1], "timeframe": row[2], "direction": row[3],
            "bar_open_time": row[4], "decision": row[5],
            "geometry": row[6] if isinstance(row[6], dict) else json.loads(row[6]),
            "costs": row[7] if isinstance(row[7], dict) else json.loads(row[7]),
        }
        position_id = create_paper_position(conn, idea)
        if position_id:
            opened.append(position_id)
    return opened
