"""Step 6: paper monitoring. MFE/MAE, exits, and restart-safe recomputation.

`walk_position` is pure and deterministic: given the entry geometry and the
chronological list of closed bars strictly after the entry bar, it derives
MFE/MAE and the first exit condition (if any) with no reference to wall-clock
state. Every monitor tick recomputes fully from stored candles rather than
mutating counters incrementally, which is what makes a duplicate tick, a
restart mid-tick, or an out-of-order re-run all idempotent by construction:
the same inputs always produce the same output row.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from agent.outcomes import (
    classify_outcome,
    compute_funding,
    compute_realized,
    entry_slippage_cost,
    exit_fill_price,
    exit_slippage_cost,
    fetch_hourly_funding,
)
from agent.paper import EXIT_KIND_BY_REASON, STATUS_CLOSED
from agent.timeutil import require_utc, utc_now

EXIT_REASONS = ("stop", "target", "time_stop", "halt_flatten")


@dataclass(frozen=True)
class WalkResult:
    mfe_px: float
    mae_px: float
    mfe_r: float
    mae_r: float
    bars_held: int
    exit_reason: str | None
    exit_bar: Mapping[str, Any] | None


def walk_position(*, direction: str, entry: float, stop: float, target: float,
                  bars: Sequence[Mapping[str, Any]], hold_bars_limit: int, halted: bool) -> WalkResult:
    """Deterministic single-pass simulation over closed bars after entry.

    `bars` must be in chronological order and must contain only bars whose
    close_time has already passed (no forming/current bar) — the caller is
    responsible for that lookahead boundary, exactly like pipeline.evaluate.
    Same-candle target+stop is resolved STOP-first, never inventing an
    intrabar path.
    """
    risk = abs(entry - stop)
    if risk <= 0:
        raise ValueError("entry and stop must differ")
    if hold_bars_limit <= 0:
        raise ValueError("hold_bars_limit must be positive")

    mfe_px = entry
    mae_px = entry
    bars_held = 0
    exit_reason: str | None = None
    exit_bar: Mapping[str, Any] | None = None

    for bar in bars:
        bars_held += 1
        h = float(bar["h"]); l = float(bar["l"])
        if direction == "long":
            mfe_px = max(mfe_px, h)
            mae_px = min(mae_px, l)
            stop_hit = l <= stop
            target_hit = h >= target
        elif direction == "short":
            mfe_px = min(mfe_px, l)
            mae_px = max(mae_px, h)
            stop_hit = h >= stop
            target_hit = l <= target
        else:
            raise ValueError("direction must be long or short")

        if stop_hit:
            exit_reason, exit_bar = "stop", bar
            break
        if target_hit:
            exit_reason, exit_bar = "target", bar
            break
        if bars_held >= hold_bars_limit:
            exit_reason, exit_bar = "time_stop", bar
            break

    if exit_reason is None and halted and bars_held >= 1:
        exit_reason, exit_bar = "halt_flatten", bars[bars_held - 1]

    if direction == "long":
        mfe_r = (mfe_px - entry) / risk
        mae_r = (entry - mae_px) / risk
    else:
        mfe_r = (entry - mfe_px) / risk
        mae_r = (mae_px - entry) / risk

    return WalkResult(mfe_px=mfe_px, mae_px=mae_px, mfe_r=mfe_r, mae_r=mae_r,
                      bars_held=bars_held, exit_reason=exit_reason, exit_bar=exit_bar)


def _bars_since_entry(conn, *, asset: str, timeframe: str, opened_bar_open_time, asof) -> list[dict[str, Any]]:
    """Closed bars strictly after the entry bar, up to and including `asof`. No forming bar."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT open_time, close_time, h, l, c FROM candles
               WHERE asset=%s AND timeframe=%s AND open_time > %s AND close_time <= %s
               ORDER BY open_time ASC""",
            (asset, timeframe, opened_bar_open_time, asof),
        )
        rows = cur.fetchall()
    return [{"open_time": r[0], "close_time": r[1], "h": float(r[2]), "l": float(r[3]), "c": float(r[4])} for r in rows]


def _is_halted(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM system_state WHERE key='mode'")
        row = cur.fetchone()
    if not row:
        return False
    value = row[0]
    if isinstance(value, str):
        value = json.loads(value)
    mode = value.get("mode") if isinstance(value, dict) else value
    return mode == "halted"


def _entry_reference_price(conn, *, asset: str, timeframe: str, bar_open_time) -> float | None:
    with conn.cursor() as cur:
        cur.execute("SELECT c FROM candles WHERE asset=%s AND timeframe=%s AND open_time=%s", (asset, timeframe, bar_open_time))
        row = cur.fetchone()
    return float(row[0]) if row else None


def _close_position(conn, *, position, walk: WalkResult, hold_bars_default: int) -> None:
    position_id = position["id"]
    asset = position["asset"]
    timeframe = position["tf"]
    direction = position["direction"]
    entry_fill = float(position["entry"])
    stop = float(position["stop"])
    targets = position["targets"]
    target = float(targets[0] if isinstance(targets, list) else json.loads(targets)[0])
    size = float(position["size"])
    risk_cash = float(position["risk_cash"])
    slip_bps = position["slip_bps"]
    opened_at = require_utc(position["opened_at"])

    exit_reason = walk.exit_reason
    exit_bar = walk.exit_bar
    bar_close = float(exit_bar["c"])
    exit_fill = exit_fill_price(direction=direction, exit_reason=exit_reason, stop=stop, target=target, bar_close=bar_close, slip_bps=slip_bps)

    entry_reference = _entry_reference_price(conn, asset=asset, timeframe=timeframe, bar_open_time=position["idea_bar_open_time"])
    entry_slip = entry_slippage_cost(direction=direction, entry_fill=entry_fill, reference_price=entry_reference if entry_reference is not None else entry_fill, size=size)
    exit_slip = exit_slippage_cost(direction=direction, exit_reason=exit_reason, exit_fill=exit_fill, ideal_price=stop if exit_reason == "stop" else bar_close, size=size)

    closed_at = require_utc(exit_bar["close_time"])
    fees_usd = float(position["fees_usd_estimate"])
    notional = float(position["notional"])
    funding_usd = 0.0
    funding_missing = False
    if closed_at > opened_at:
        hourly_rates = fetch_hourly_funding(conn, asset, opened_at, closed_at)
        funding_usd, funding_missing = compute_funding(direction=direction, notional=notional, opened_at=opened_at, closed_at=closed_at, hourly_rates=hourly_rates)

    realized = compute_realized(direction=direction, entry_fill=entry_fill, exit_fill=exit_fill, size=size,
                                 risk_cash=risk_cash, fees_usd=fees_usd, funding_usd=funding_usd,
                                 entry_slip_usd=entry_slip, exit_slip_usd=exit_slip)
    outcome_class = classify_outcome(exit_reason)
    fill_kind = EXIT_KIND_BY_REASON[exit_reason]

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE paper_positions SET
                       status=%s, closed_at=%s, exit_px=%s, exit_reason=%s, realized_r=%s,
                       pnl_usd=%s, fees_usd=%s, funding_usd=%s, slip_usd=%s,
                       mfe_r=%s, mae_r=%s, mfe_px=%s, mae_px=%s, bars_held=%s,
                       outcome_class=%s, funding_missing=%s
                   WHERE id=%s AND status='OPEN'""",
                (
                    STATUS_CLOSED, closed_at, exit_fill, exit_reason, realized.realized_r,
                    realized.pnl_usd, fees_usd, funding_usd, realized.slip_usd,
                    walk.mfe_r, walk.mae_r, walk.mfe_px, walk.mae_px, walk.bars_held,
                    outcome_class, funding_missing, position_id,
                ),
            )
            cur.execute(
                """INSERT INTO paper_fills(id, position_id, ts, side, px, sz, kind, note)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (position_id, kind) DO NOTHING""",
                (
                    str(uuid.uuid4()), position_id, closed_at,
                    "sell" if direction == "long" else "buy",
                    exit_fill, size, fill_kind, f"exit_reason={exit_reason}",
                ),
            )


def _update_open_marks(conn, *, position_id: str, walk: WalkResult) -> None:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE paper_positions SET mfe_r=%s, mae_r=%s, mfe_px=%s, mae_px=%s, bars_held=%s WHERE id=%s AND status='OPEN'",
                (walk.mfe_r, walk.mae_r, walk.mfe_px, walk.mae_px, walk.bars_held, position_id),
            )


def run_monitor_tick(conn, *, settings) -> dict[str, int]:
    """One monitor_open occurrence: recompute MFE/MAE/exit for every OPEN position.

    Idempotent by recomputation: re-running this against the same candle
    history and the same still-open position always yields the same result,
    and a position already CLOSED is simply excluded by the WHERE clause.
    """
    now = utc_now()
    halted = _is_halted(conn)
    hold_bars_limit = 2 * int(settings.hold_bars_default)

    with conn.cursor() as cur:
        cur.execute(
            """SELECT p.id, p.idea_id, p.asset, p.direction, p.tf, p.entry, p.stop, p.targets,
                      p.size, p.notional, p.risk_cash, p.opened_at, i.bar_open_time, i.costs
               FROM paper_positions p
               JOIN ideas i ON i.id = p.idea_id
               WHERE p.status = 'OPEN'"""
        )
        rows = cur.fetchall()

    updated, closed = 0, 0
    for row in rows:
        (position_id, idea_id, asset, direction, tf, entry, stop, targets,
         size, notional, risk_cash, opened_at, idea_bar_open_time, costs) = row
        costs = costs if isinstance(costs, dict) else json.loads(costs)
        targets = targets if isinstance(targets, list) else json.loads(targets)
        target = float(targets[0])

        bars = _bars_since_entry(conn, asset=asset, timeframe=tf, opened_bar_open_time=idea_bar_open_time, asof=now)
        walk = walk_position(direction=direction, entry=float(entry), stop=float(stop), target=target,
                             bars=bars, hold_bars_limit=hold_bars_limit, halted=halted)

        if walk.exit_reason is None:
            if bars:
                _update_open_marks(conn, position_id=str(position_id), walk=walk)
                updated += 1
            continue

        position = {
            "id": str(position_id), "asset": asset, "direction": direction, "tf": tf,
            "entry": entry, "stop": stop, "targets": targets, "size": size, "notional": notional,
            "risk_cash": risk_cash, "opened_at": opened_at, "idea_bar_open_time": idea_bar_open_time,
            "slip_bps": float(costs["slip_bps"]), "fees_usd_estimate": float(costs["fee_round_trip"]),
        }
        _close_position(conn, position=position, walk=walk, hold_bars_default=int(settings.hold_bars_default))
        closed += 1

    return {"updated": updated, "closed": closed}


def run_monitor_open_job(conn, *, settings) -> dict[str, int]:
    """monitor_open (spec 6.2, 15s): open any new TRADE_PAPER ideas, then update all OPEN positions."""
    from agent.paper import open_positions_for_new_ideas

    opened = open_positions_for_new_ideas(conn)
    stats = run_monitor_tick(conn, settings=settings)
    stats["opened"] = len(opened)
    return stats


def run_equity_snap_job(conn, *, settings) -> dict[str, float]:
    """equity_snap (spec 6.2, 1 min): durable paper mark-to-market snapshot."""
    from agent.outcomes import run_equity_snapshot

    snapshot = run_equity_snapshot(conn, settings=settings)
    return {
        "equity": snapshot.equity,
        "open_risk": snapshot.open_risk,
        "drawdown_from_peak": snapshot.drawdown_from_peak,
    }
