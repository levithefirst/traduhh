from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterable, Sequence

from agent.config import FROZEN_ASSETS, FROZEN_TIMEFRAMES
from agent.timeutil import require_utc, utc_now


@dataclass(frozen=True)
class Gap:
    asset: str
    timeframe: str
    previous_open: datetime
    next_expected_open: datetime
    observed_next_open: datetime
    missing_bars: int


@dataclass(frozen=True)
class IntegrityResult:
    ok: bool
    flags: tuple[str, ...] = ()
    details: dict[str, object] = field(default_factory=dict)
    halt: bool = False


_INTERVALS = {"15m": timedelta(minutes=15), "1h": timedelta(hours=1), "4h": timedelta(hours=4)}


def _interval(timeframe: str) -> timedelta:
    if timeframe not in FROZEN_TIMEFRAMES:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    return _INTERVALS[timeframe]


def expected_open_after(open_time: datetime, timeframe: str) -> datetime:
    return require_utc(open_time) + _interval(timeframe)


def detect_candle_gaps(
    candles: Sequence[tuple[datetime, datetime] | object],
    asset: str,
    timeframe: str,
    *,
    now: datetime | None = None,
) -> tuple[Gap, ...]:
    """Detect missing closed bars only after the spec's two-interval delay."""
    if asset not in FROZEN_ASSETS:
        raise ValueError(f"unsupported asset: {asset}")
    interval = _interval(timeframe)
    current = require_utc(now or utc_now())

    opens: list[datetime] = []
    for item in candles:
        value = item[0] if isinstance(item, tuple) else getattr(item, "open_time")
        opens.append(require_utc(value))
    opens = sorted(set(opens))

    gaps: list[Gap] = []
    for previous, observed in zip(opens, opens[1:]):
        delta = observed - previous
        if delta <= interval:
            continue
        expected = previous + interval
        if current - expected < 2 * interval:
            continue
        missing = int(delta.total_seconds() // interval.total_seconds()) - 1
        if missing > 0:
            gaps.append(Gap(asset, timeframe, previous, expected, observed, missing))
    return tuple(gaps)


def detect_stale(age: timedelta | None, *, max_age: timedelta = timedelta(seconds=60)) -> bool:
    return age is None or age > max_age


def _safe_log_return(previous: Decimal, current: Decimal) -> float | None:
    if previous <= 0 or current <= 0:
        return None
    return abs(math.log(float(current / previous)))


def inspect_latest_market_data(conn, *, now: datetime | None = None) -> IntegrityResult:
    """Run Step 3 integrity checks against the durable market-data stores."""
    current = require_utc(now or utc_now())
    flags: list[str] = []
    details: dict[str, object] = {"gaps": [], "stale": [], "jumps": [], "clock": {}}

    hard_flags: list[str] = []

    with conn.cursor() as cur:
        for asset in FROZEN_ASSETS:
            cur.execute("SELECT ts, mark, mid FROM asset_ctx WHERE asset=%s ORDER BY ts DESC LIMIT 1", (asset,))
            ctx = cur.fetchone()
            if not ctx:
                flags.append(f"missing_ctx:{asset}"); hard_flags.append(f"missing_ctx:{asset}")
            else:
                age = current - require_utc(ctx[0])
                if detect_stale(age):
                    flags.append(f"stale_ctx:{asset}"); hard_flags.append(f"stale_ctx:{asset}")
                    details["stale"].append({"asset": asset, "kind": "ctx", "age_s": age.total_seconds()})
                if ctx[1] is None:
                    flags.append(f"missing_mark:{asset}"); hard_flags.append(f"missing_mark:{asset}")
                if ctx[1] is not None and ctx[2] is not None and ctx[2] != 0:
                    mark_mid = abs((ctx[1] - ctx[2]) / ctx[2])
                    if mark_mid > Decimal("0.01"):
                        flags.append(f"mark_mid_divergence:{asset}")

            cur.execute("SELECT ts, bid1, ask1 FROM book_snapshots WHERE asset=%s ORDER BY ts DESC LIMIT 1", (asset,))
            book = cur.fetchone()
            if not book:
                flags.append(f"missing_book:{asset}"); hard_flags.append(f"missing_book:{asset}")
            else:
                age = current - require_utc(book[0])
                if detect_stale(age):
                    flags.append(f"stale_book:{asset}"); hard_flags.append(f"stale_book:{asset}")
                    details["stale"].append({"asset": asset, "kind": "book", "age_s": age.total_seconds()})
                if book[1] is None or book[2] is None:
                    flags.append(f"missing_book_top:{asset}"); hard_flags.append(f"missing_book_top:{asset}")

            cur.execute(
                """
                SELECT open_time, o, h, l, c
                FROM candles
                WHERE asset=%s AND timeframe='15m'
                ORDER BY open_time DESC
                LIMIT 500
                """,
                (asset,),
            )
            rows = cur.fetchall()
            if not rows:
                flags.append(f"missing_candles:{asset}:15m"); hard_flags.append(flags[-1])
            else:
                latest_open = require_utc(rows[0][0])
                if current - latest_open > 2 * _interval("15m"):
                    flags.append(f"stale_candles:{asset}:15m"); hard_flags.append(flags[-1])
                for row in rows:
                    if int(require_utc(row[0]).timestamp()) % int(_interval("15m").total_seconds()) != 0:
                        flags.append(f"invalid_candle_boundary:{asset}:15m"); hard_flags.append(flags[-1]); break
                    if any(value is None for value in row[1:]) or row[1] <= 0 or row[4] <= 0 or row[3] <= 0 or row[2] < max(row[1], row[4]) or row[3] > min(row[1], row[4]) or row[2] < row[3]:
                        flags.append(f"malformed_candle:{asset}:15m"); hard_flags.append(flags[-1]); break
            for gap in detect_candle_gaps(rows, asset, "15m", now=current):
                flags.append(f"candle_gap:{asset}:15m:{gap.missing_bars}"); hard_flags.append(flags[-1])
                details["gaps"].append(gap)

            for timeframe in ("1h", "4h"):
                cur.execute(
                    "SELECT open_time, c FROM candles WHERE asset=%s AND timeframe=%s ORDER BY open_time DESC LIMIT 500",
                    (asset, timeframe),
                )
                candle_rows = cur.fetchall()
                if any(int(require_utc(row[0]).timestamp()) % int(_interval(timeframe).total_seconds()) != 0 for row in candle_rows):
                    flags.append(f"invalid_candle_boundary:{asset}:{timeframe}"); hard_flags.append(flags[-1])
                if any(any(value is None for value in row[1:]) or row[1] <= 0 or row[4] <= 0 or row[3] <= 0 or row[2] < max(row[1], row[4]) or row[3] > min(row[1], row[4]) or row[2] < row[3] for row in candle_rows):
                    flags.append(f"malformed_candle:{asset}:{timeframe}"); hard_flags.append(flags[-1])
                if not candle_rows:
                    flags.append(f"missing_candles:{asset}:{timeframe}"); hard_flags.append(flags[-1])
                else:
                    latest_open = require_utc(candle_rows[0][0])
                    if current - latest_open > 2 * _interval(timeframe):
                        flags.append(f"stale_candles:{asset}:{timeframe}"); hard_flags.append(flags[-1])
                for gap in detect_candle_gaps(candle_rows, asset, timeframe, now=current):
                    flags.append(f"candle_gap:{asset}:{timeframe}:{gap.missing_bars}"); hard_flags.append(flags[-1])
                    details["gaps"].append(gap)

            if asset in {"BTC", "ETH"}:
                cur.execute(
                    "SELECT c FROM candles WHERE asset=%s AND timeframe='15m' ORDER BY open_time DESC LIMIT 2",
                    (asset,),
                )
                closes = cur.fetchall()
                if len(closes) == 2 and closes[0][0] is not None and closes[1][0] is not None:
                    jump = _safe_log_return(Decimal(str(closes[1][0])), Decimal(str(closes[0][0])))
                    if jump is not None and jump > 0.15:
                        flags.append(f"implausible_jump:{asset}:15m"); hard_flags.append(flags[-1])
                        details["jumps"].append({"asset": asset, "log_return_abs": jump})

        cur.execute("SELECT job_name, scheduled_for, started_at FROM job_runs WHERE status='RUNNING'")
        details["running_jobs"] = [
            {"job_name": row[0], "scheduled_for": require_utc(row[1]), "started_at": require_utc(row[2])}
            for row in cur.fetchall()
        ]

        # The L2 timestamp is a venue clock. Context snapshots use local ingestion time
        # because metaAndAssetCtxs does not expose a snapshot timestamp.
        cur.execute("SELECT max(ts) FROM book_snapshots")
        venue_ts = cur.fetchone()[0]
        if venue_ts is not None:
            skew = abs((current - require_utc(venue_ts)).total_seconds())
            details["clock"] = {"skew_s": skew, "source": "latest_book_ts"}
            if skew > 5:
                flags.append("clock_skew_warning")
            if skew > 30:
                flags.append("clock_skew_halt"); hard_flags.append("clock_skew_halt")

    # Duplicate candle rows cannot exist under the frozen unique constraint. A non-zero
    # result here is therefore a schema-integrity failure rather than a repair opportunity.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT venue, asset, timeframe, open_time, COUNT(*)
                FROM candles GROUP BY venue, asset, timeframe, open_time HAVING COUNT(*) > 1
            ) duplicates
            """
        )
        if int(cur.fetchone()[0]) > 0:
            flags.append("duplicate_candles"); hard_flags.append("duplicate_candles")

    return IntegrityResult(ok=not hard_flags, flags=tuple(flags), details=details, halt=bool(hard_flags))
