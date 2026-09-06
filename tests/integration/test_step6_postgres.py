import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

T = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
OPENED_AT = T
LATER = T + timedelta(hours=3)  # "now" once the monitor tick observes the close


@pytest.mark.integration
def test_step6_paper_lifecycle_and_idempotency(monkeypatch):
    """DB-backed Step 6 acceptance: creation, duplicate prevention, restart
    idempotency, a full target-exit close, funding-missing flagging, and
    equity snapshotting — against a real PostgreSQL instance.

    Wall-clock time is pinned rather than left to real utc_now(): the fixture
    candles live on a fixed 2026 timeline, and letting `opened_at` (stamped
    at real "now") float relative to that fixed `closed_at` makes the elapsed
    duration, and therefore the funding-missing assertion, depend on what day
    this test happens to run. Pinning `agent.paper.utc_now` to the moment the
    entry bar closes and `agent.monitor.utc_now`/`agent.outcomes.utc_now` to a
    later fixed instant reproduces real chronology (open, then later observe
    the close) deterministically, regardless of the actual calendar date.
    """
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")

    import psycopg
    from agent.db import run_migrations
    from agent.monitor import run_monitor_tick
    from agent.outcomes import run_equity_snapshot
    from agent.paper import create_paper_position, open_positions_for_new_ideas

    monkeypatch.setattr("agent.paper.utc_now", lambda: OPENED_AT)
    monkeypatch.setattr("agent.monitor.utc_now", lambda: LATER)
    monkeypatch.setattr("agent.outcomes.utc_now", lambda: LATER)

    try:
        conn = psycopg.connect(url, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc}")

    idea_id = str(uuid.uuid4())
    settings = SimpleNamespace(paper_equity_usd=10000.0, hold_bars_default=12)
    geometry = {"entry": 100.0, "stop": 98.0, "targets": [103.0], "size": 0.25, "notional": 25.0, "risk_cash": 50.0}
    costs = {"slip_bps": 2.0, "fee_round_trip": 0.05, "impact_bps": 0.0, "slip_cost_rt": 0.01,
             "funding_est": 0.0, "cost_r": 0.001, "planned_r_after_costs": 1.4}

    # Note: this function does NOT wrap the body in `with conn:` — psycopg3's
    # Connection.__exit__ closes the connection on a clean exit (not merely
    # commit), which would leave nothing open for the `finally` cleanup below.
    try:
        run_migrations(conn)

        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_constraint WHERE conname = 'paper_positions_idea_id_key'")
            assert cur.fetchone() is not None
            cur.execute("SELECT 1 FROM pg_constraint WHERE conname = 'paper_fills_position_id_kind_key'")
            assert cur.fetchone() is not None

        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO candles(venue,asset,timeframe,open_time,close_time,o,h,l,c,v,n_trades,source,ingested_at)
                       VALUES ('hyperliquid','BTC','1h',%s,%s,100,100.5,99.5,100,10,5,'test',%s)
                       ON CONFLICT (venue,asset,timeframe,open_time) DO NOTHING""",
                    (T, T + timedelta(hours=1), T),
                )
                cur.execute(
                    """INSERT INTO candles(venue,asset,timeframe,open_time,close_time,o,h,l,c,v,n_trades,source,ingested_at)
                       VALUES ('hyperliquid','BTC','1h',%s,%s,100,104,99.8,103.5,10,5,'test',%s)
                       ON CONFLICT (venue,asset,timeframe,open_time) DO NOTHING""",
                    (T + timedelta(hours=1), T + timedelta(hours=2), T + timedelta(hours=1)),
                )
                cur.execute(
                    """INSERT INTO ideas(id,created_at,asset,timeframe,direction,setup_id,strategy_version_id,
                                          prompt_version_id,bar_open_time,decision,decision_reason,gates,geometry,
                                          costs,features,regime,ctx,book,news,calendar,hist_cell,llm_review,
                                          packet_hash,data_quality,confidence)
                       VALUES (%s,%s,'BTC','1h','long','trend_pullback','sv_test_step6',NULL,%s,'TRADE_PAPER',
                               %s,'{}'::jsonb,%s::jsonb,%s::jsonb,'{}'::jsonb,'{}'::jsonb,'{}'::jsonb,'{}'::jsonb,
                               '[]'::jsonb,'[]'::jsonb,'{}'::jsonb,NULL,'test-hash','{}'::jsonb,0.7)""",
                    (idea_id, T, T, [], json.dumps(geometry), json.dumps(costs)),
                )

        opened = open_positions_for_new_ideas(conn)
        assert opened == [_position_id_for(conn, idea_id)]

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM paper_positions WHERE idea_id=%s", (idea_id,))
            assert cur.fetchone()[0] == 1
            cur.execute(
                "SELECT count(*) FROM paper_fills WHERE position_id=%s AND kind='PAPER_ENTRY'",
                (_position_id_for(conn, idea_id),),
            )
            assert cur.fetchone()[0] == 1

        # Restart-safety: re-running the open scan and calling create_paper_position
        # directly again must never create a second position or a second entry fill.
        again = open_positions_for_new_ideas(conn)
        assert again == []
        position_id = create_paper_position(conn, {"id": idea_id, "asset": "BTC", "timeframe": "1h",
                                                    "direction": "long", "bar_open_time": T,
                                                    "decision": "TRADE_PAPER", "geometry": geometry, "costs": costs})
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM paper_positions WHERE idea_id=%s", (idea_id,))
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT count(*) FROM paper_fills WHERE position_id=%s", (position_id,))
            assert cur.fetchone()[0] == 1

        # Full lifecycle: bar 2's high (104) reaches the 103 target.
        stats = run_monitor_tick(conn, settings=settings)
        assert stats["closed"] == 1

        with conn.cursor() as cur:
            cur.execute(
                """SELECT status, exit_reason, realized_r, pnl_usd, mfe_r, mae_r, fees_usd, funding_missing
                   FROM paper_positions WHERE id=%s""",
                (position_id,),
            )
            status, exit_reason, realized_r, pnl_usd, mfe_r, mae_r, fees_usd, funding_missing = cur.fetchone()
            assert status == "CLOSED"
            assert exit_reason == "target"
            assert realized_r is not None
            assert mfe_r is not None and mae_r is not None
            assert float(fees_usd) == pytest.approx(0.05)
            assert funding_missing is True  # no asset_ctx funding rows were inserted
            cur.execute("SELECT count(*) FROM paper_fills WHERE position_id=%s", (position_id,))
            assert cur.fetchone()[0] == 2  # entry + target

        # Idempotency: a duplicate monitor tick after close must not reprocess it.
        stats_again = run_monitor_tick(conn, settings=settings)
        assert stats_again["closed"] == 0
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM paper_fills WHERE position_id=%s", (position_id,))
            assert cur.fetchone()[0] == 2

        snapshot = run_equity_snapshot(conn, settings=settings)
        assert snapshot.equity == pytest.approx(10000.0 + float(pnl_usd), rel=1e-6)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM paper_equity")
            assert cur.fetchone()[0] >= 1
    finally:
        if not conn.closed:
            try:
                conn.rollback()
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM paper_fills WHERE position_id IN (SELECT id FROM paper_positions WHERE idea_id=%s)", (idea_id,))
                    cur.execute("DELETE FROM paper_positions WHERE idea_id=%s", (idea_id,))
                    cur.execute("DELETE FROM ideas WHERE id=%s", (idea_id,))
                    cur.execute("DELETE FROM candles WHERE asset='BTC' AND timeframe='1h' AND open_time IN (%s,%s)", (T, T + timedelta(hours=1)))
                conn.commit()
            finally:
                conn.close()


def _position_id_for(conn, idea_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM paper_positions WHERE idea_id=%s", (idea_id,))
        row = cur.fetchone()
    return str(row[0]) if row else None
