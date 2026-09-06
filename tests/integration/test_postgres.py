import os

import pytest


@pytest.mark.integration
def test_postgres_migrations_and_constraints():
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")

    try:
        import psycopg
        from agent.db import run_migrations
        with psycopg.connect(url, connect_timeout=2) as conn:
            applied = run_migrations(conn)
            assert "0001_init.sql" in applied or applied == []

            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
                assert cur.fetchone()[0] >= 18

                cur.execute("SELECT 1 FROM pg_constraint WHERE conname = 'candles_venue_asset_timeframe_open_time_key'")
                assert cur.fetchone() is not None

                cur.execute("SELECT 1 FROM pg_constraint WHERE conname = 'ideas_asset_timeframe_setup_id_bar_open_time_strategy_version_id_key'")
                assert cur.fetchone() is not None
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc}")
