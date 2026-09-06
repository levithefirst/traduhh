"""Step 6: realized R, fees, funding, outcome classification, and paper equity.

All arithmetic here is pure and DB-free; the two `fetch_*` / `run_*` functions
are thin DB orchestration that feed the pure functions real rows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping

from agent.timeutil import require_utc, utc_now

OUTCOME_CLASS_BY_EXIT_REASON = {
    "target": "target_hit",
    "stop": "stop_hit",
    "time_stop": "time_stop",
    "halt_flatten": "halt_flatten",
}


def classify_outcome(exit_reason: str) -> str:
    try:
        return OUTCOME_CLASS_BY_EXIT_REASON[exit_reason]
    except KeyError as exc:
        raise ValueError(f"unknown exit_reason: {exit_reason}") from exc


def exit_fill_price(*, direction: str, exit_reason: str, stop: float, target: float, bar_close: float, slip_bps: float) -> float:
    """Terminal fill for each of the four Step 6 exit kinds.

    stop:                adverse slippage (paper.stop_fill_price)
    target:               the Step 5 target level itself, no extra slippage
    time_stop/halt_flatten: flatten at the bar close, no extra slippage
    """
    from agent.paper import stop_fill_price

    if exit_reason == "stop":
        return stop_fill_price(direction=direction, stop=stop, slip_bps=slip_bps)
    if exit_reason == "target":
        return float(target)
    if exit_reason in ("time_stop", "halt_flatten"):
        return float(bar_close)
    raise ValueError(f"unknown exit_reason: {exit_reason}")


@dataclass(frozen=True)
class RealizedOutcome:
    pnl_usd: float
    realized_r: float
    slip_usd: float


def compute_realized(*, direction: str, entry_fill: float, exit_fill: float, size: float, risk_cash: float,
                     fees_usd: float, funding_usd: float, entry_slip_usd: float, exit_slip_usd: float) -> RealizedOutcome:
    """Realized R from the actual modeled paper fills, never from the idealized signal entry."""
    if risk_cash <= 0:
        raise ValueError("risk_cash must be positive")
    sign = 1.0 if direction == "long" else -1.0
    raw_pnl = (exit_fill - entry_fill) * size * sign
    net_pnl = raw_pnl - fees_usd - funding_usd
    realized_r = net_pnl / risk_cash
    return RealizedOutcome(pnl_usd=net_pnl, realized_r=realized_r, slip_usd=entry_slip_usd + exit_slip_usd)


def entry_slippage_cost(*, direction: str, entry_fill: float, reference_price: float, size: float) -> float:
    """Dollar cost of entry slippage vs. the unslipped reference price (close, or bid/ask)."""
    sign = 1.0 if direction == "long" else -1.0
    return max(0.0, (entry_fill - reference_price) * size * sign)


def exit_slippage_cost(*, direction: str, exit_reason: str, exit_fill: float, ideal_price: float, size: float) -> float:
    """Dollar cost of adverse stop slippage. Target/time/halt exits carry no modeled exit slippage."""
    if exit_reason != "stop":
        return 0.0
    sign = 1.0 if direction == "long" else -1.0
    return max(0.0, (ideal_price - exit_fill) * size * sign)


def hour_buckets(start: datetime, end: datetime) -> list[datetime]:
    """Distinct UTC hour buckets touched by the open interval [start, end).

    A position closed exactly on an hour boundary has zero exposure to that
    boundary hour, so the end bucket is exclusive; any partial hour still in
    progress at `end` counts as one full bucket of exposure.
    """
    start = require_utc(start)
    end = require_utc(end)
    if end <= start:
        return []
    cursor = start.replace(minute=0, second=0, microsecond=0)
    buckets = []
    while cursor < end:
        buckets.append(cursor)
        cursor += timedelta(hours=1)
    return buckets


def compute_funding(*, direction: str, notional: float, opened_at: datetime, closed_at: datetime,
                    hourly_rates: Mapping[datetime, float]) -> tuple[float, bool]:
    """Signed funding cost over the holding period from real hourly asset_ctx funding rates.

    Positive Hyperliquid funding means longs pay shorts. funding_usd here is a
    cost convention: positive = paid out (reduces PnL), negative = received.
    Missing hours are never invented; any missing expected bucket sets the
    `missing` flag and that bucket simply contributes nothing to the sum.
    """
    buckets = hour_buckets(opened_at, closed_at)
    if not buckets:
        return 0.0, False
    total_rate = 0.0
    missing = False
    for bucket in buckets:
        rate = hourly_rates.get(bucket)
        if rate is None:
            missing = True
            continue
        total_rate += float(rate)
    sign = 1.0 if direction == "long" else -1.0
    funding_usd = sign * total_rate * notional
    return funding_usd, missing


def fetch_hourly_funding(conn, asset: str, start: datetime, end: datetime) -> dict[datetime, float]:
    """Real asset_ctx funding, bucketed to the hour. Never fabricates a rate for a missing hour."""
    start = require_utc(start)
    end = require_utc(end)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT date_trunc('hour', ts) AS bucket, avg(funding)
               FROM asset_ctx
               WHERE asset=%s AND ts >= %s AND ts < %s AND funding IS NOT NULL
               GROUP BY bucket""",
            (asset, start, end + timedelta(hours=1)),
        )
        rows = cur.fetchall()
    return {row[0].astimezone(timezone.utc): float(row[1]) for row in rows}


@dataclass(frozen=True)
class EquitySnapshot:
    equity: float
    open_risk: float
    drawdown_from_peak: float
    peak_equity: float


def compute_equity_snapshot(*, starting_equity: float, closed_pnl_total: float, open_mtm_total: float,
                            open_risk_total: float, prior_peak_equity: float) -> EquitySnapshot:
    equity = starting_equity + closed_pnl_total + open_mtm_total
    peak = max(prior_peak_equity, equity)
    drawdown = 0.0 if peak <= 0 else max(0.0, (peak - equity) / peak)
    return EquitySnapshot(equity=equity, open_risk=open_risk_total, drawdown_from_peak=drawdown, peak_equity=peak)


def _open_position_mtm(conn, *, asset: str, timeframe: str, direction: str, entry: float, size: float) -> float:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c FROM candles WHERE asset=%s AND timeframe=%s ORDER BY open_time DESC LIMIT 1",
            (asset, timeframe),
        )
        row = cur.fetchone()
    if not row:
        return 0.0
    last_price = float(row[0])
    sign = 1.0 if direction == "long" else -1.0
    return (last_price - entry) * size * sign


def run_equity_snapshot(conn, *, settings) -> EquitySnapshot:
    """Durable paper-equity snapshot (spec 5.7 paper_equity, 6.2 equity_snap job)."""
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(sum(pnl_usd),0) FROM paper_positions WHERE status='CLOSED'")
        closed_pnl_total = float(cur.fetchone()[0])
        cur.execute(
            "SELECT asset, tf, direction, entry, size, risk_cash FROM paper_positions WHERE status='OPEN'"
        )
        open_rows = cur.fetchall()
        cur.execute("SELECT value FROM system_state WHERE key='paper.peak_equity'")
        peak_row = cur.fetchone()

    prior_peak = float(settings.paper_equity_usd)
    if peak_row and peak_row[0] is not None:
        value = peak_row[0]
        if isinstance(value, str):
            value = json.loads(value)
        prior_peak = float(value.get("peak", prior_peak))

    open_mtm_total = 0.0
    open_risk_total = 0.0
    for asset, tf, direction, entry, size, risk_cash in open_rows:
        open_risk_total += float(risk_cash)
        open_mtm_total += _open_position_mtm(conn, asset=asset, timeframe=tf, direction=direction, entry=float(entry), size=float(size))

    snapshot = compute_equity_snapshot(
        starting_equity=float(settings.paper_equity_usd),
        closed_pnl_total=closed_pnl_total,
        open_mtm_total=open_mtm_total,
        open_risk_total=open_risk_total,
        prior_peak_equity=prior_peak,
    )

    now = utc_now()
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO paper_equity(ts, equity, open_risk, drawdown_from_peak) VALUES (%s,%s,%s,%s)",
                (now, snapshot.equity, snapshot.open_risk, snapshot.drawdown_from_peak),
            )
            cur.execute(
                """INSERT INTO system_state(key,value,updated_at) VALUES ('paper.peak_equity',%s::jsonb,%s)
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at""",
                (json.dumps({"peak": snapshot.peak_equity}, separators=(",", ":")), now),
            )
    return snapshot
