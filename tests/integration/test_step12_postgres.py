import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

T = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
ASSET = "BTC"
TIMEFRAME = "1h"

SETTINGS = SimpleNamespace(min_r_after_costs=1.2, paper_equity_usd=10000.0, risk_fraction=0.005,
                          taker_fee_bps=4.5, slippage_bps_floor=2.0, hold_bars_default=12,
                          max_concurrent_paper=3)

# The exact trend_pullback fixture shape from tests/unit/test_step5.py, so this
# integration test exercises the same scenario already proven (in isolation)
# to make the detector fire.
FEATURES = {"atr_14": 2, "ema_20": 100, "grammar": "HH_HL", "last_swing_low_px": 97,
           "last_swing_low_t": (T - timedelta(hours=2)).isoformat(), "last_swing_high_px": 103,
           "last_swing_high_t": (T - timedelta(hours=2)).isoformat()}


def _trend_bars():
    bars = []
    for i in range(5):
        t = T + timedelta(hours=i)
        bars.append({"open_time": t, "close_time": t + timedelta(hours=1), "o": 100, "h": 101, "l": 99, "c": 100, "v": 100})
    bars[2].update(l=100, h=102)
    bars[4].update(h=103, l=100, c=102)
    return bars


@pytest.fixture()
def pg_conn():
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    import psycopg

    from agent.db import run_migrations

    try:
        conn = psycopg.connect(url, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    run_migrations(conn)
    try:
        yield conn
    finally:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM ideas WHERE asset=%s AND timeframe=%s AND bar_open_time>=%s", (ASSET, TIMEFRAME, T))
                cur.execute("DELETE FROM feature_snapshots WHERE asset=%s AND timeframe=%s AND open_time>=%s", (ASSET, TIMEFRAME, T))
                cur.execute("DELETE FROM regime_snapshots WHERE asset=%s AND timeframe=%s AND open_time>=%s", (ASSET, TIMEFRAME, T))
                cur.execute("DELETE FROM asset_ctx WHERE asset=%s AND ts>=%s", (ASSET, T))
                cur.execute("DELETE FROM candles WHERE asset=%s AND timeframe=%s AND open_time>=%s", (ASSET, TIMEFRAME, T))
        conn.close()


def _seed(conn, *, include_future_candle: bool):
    target = T + timedelta(hours=4)
    with conn.transaction():
        with conn.cursor() as cur:
            for bar in _trend_bars():
                cur.execute(
                    """INSERT INTO candles(venue,asset,timeframe,open_time,close_time,o,h,l,c,v,n_trades,source,ingested_at)
                       VALUES ('hyperliquid',%s,%s,%s,%s,%s,%s,%s,%s,%s,10,'test',%s)
                       ON CONFLICT (venue,asset,timeframe,open_time) DO NOTHING""",
                    (ASSET, TIMEFRAME, bar["open_time"], bar["close_time"], bar["o"], bar["h"], bar["l"], bar["c"], bar["v"], T),
                )
            if include_future_candle:
                future_t = target + timedelta(hours=1)
                cur.execute(
                    """INSERT INTO candles(venue,asset,timeframe,open_time,close_time,o,h,l,c,v,n_trades,source,ingested_at)
                       VALUES ('hyperliquid',%s,%s,%s,%s,1,999999,1,999999,1,10,'test',%s)
                       ON CONFLICT (venue,asset,timeframe,open_time) DO NOTHING""",
                    (ASSET, TIMEFRAME, future_t, future_t + timedelta(hours=1), T),
                )
            cur.execute(
                """INSERT INTO feature_snapshots(asset,timeframe,open_time,features,computed_at)
                   VALUES (%s,%s,%s,%s::jsonb,%s)
                   ON CONFLICT (asset,timeframe,open_time) DO UPDATE SET features=EXCLUDED.features""",
                (ASSET, TIMEFRAME, target, json.dumps(FEATURES), T),
            )
            cur.execute(
                """INSERT INTO regime_snapshots(asset,timeframe,open_time,label,secondary,confidence,features_used)
                   VALUES (%s,%s,%s,'TREND_UP',%s,0.7,'{}'::jsonb)
                   ON CONFLICT (asset,timeframe,open_time) DO UPDATE SET label=EXCLUDED.label""",
                (ASSET, TIMEFRAME, target, []),
            )
            cur.execute(
                """INSERT INTO asset_ctx(venue,asset,ts,mid,mark,oracle,funding,raw)
                   VALUES ('hyperliquid',%s,%s,100,100,100,0,'{}'::jsonb)
                   ON CONFLICT (venue,asset,ts) DO NOTHING""",
                (ASSET, target + timedelta(minutes=59)),
            )
    return target


@pytest.mark.integration
def test_load_scan_inputs_excludes_a_future_candle(pg_conn):
    """Part 16.1: 'Lookahead tests: detector that peeks at bar t+1 must fail
    CI.' The candle window pipeline._load_scan_inputs hands to every detector
    must never include a bar beyond the one being scanned, even when a later
    candle already exists in the table (e.g. ingested by a subsequent poll).
    """
    from agent.pipeline import _load_scan_inputs

    target = _seed(pg_conn, include_future_candle=True)
    candles, *_ = _load_scan_inputs(pg_conn, ASSET, TIMEFRAME, target)

    assert candles, "expected the seeded closed bars to be returned"
    assert candles[-1]["open_time"] == target
    assert all(row["open_time"] <= target for row in candles)
    assert all(float(row["h"]) < 1000 for row in candles)  # the future bar's 999999 high never appears


@pytest.mark.integration
def test_pipeline_evaluate_twice_on_the_same_bar_produces_one_idea(pg_conn):
    """Part 16.1: 'Idempotency: pipeline twice on same bar produces one idea
    (unique on asset,tf,setup,bar_open_time).' A restart or an overlapping
    scheduler tick that re-runs the same closed-bar scan must never create a
    sibling idea row.
    """
    from agent.pipeline import evaluate

    target = _seed(pg_conn, include_future_candle=False)

    first = evaluate(pg_conn, settings=SETTINGS, asset=ASSET, timeframe=TIMEFRAME, bar_open_time=target)
    assert first, "expected the trend_pullback fixture to produce at least one idea"
    idea_id = first[0]["idea_id"]

    second = evaluate(pg_conn, settings=SETTINGS, asset=ASSET, timeframe=TIMEFRAME, bar_open_time=target)
    assert second and second[0]["idea_id"] == idea_id  # same deterministic id, not a new one

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM ideas WHERE asset=%s AND timeframe=%s AND setup_id='trend_pullback' AND bar_open_time=%s",
            (ASSET, TIMEFRAME, target),
        )
        assert cur.fetchone()[0] == 1


@pytest.mark.integration
def test_three_evaluate_calls_still_produce_exactly_one_idea(pg_conn):
    """A stronger form of the same guarantee: idempotency holds under repeated
    re-runs, not just a single retry."""
    from agent.pipeline import evaluate

    target = _seed(pg_conn, include_future_candle=False)
    for _ in range(3):
        evaluate(pg_conn, settings=SETTINGS, asset=ASSET, timeframe=TIMEFRAME, bar_open_time=target)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ideas WHERE asset=%s AND timeframe=%s AND bar_open_time=%s", (ASSET, TIMEFRAME, target))
        assert cur.fetchone()[0] == 1
