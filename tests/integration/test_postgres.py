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

                # Postgres auto-names a multi-column UNIQUE constraint by
                # concatenating every column plus a "_key" suffix, then
                # truncates to its 63-byte identifier limit. That truncated
                # name is what actually exists in the catalog (verified
                # against a live instance), not the untruncated concatenation
                # spec 20.2 writes out for readability, so the columns
                # themselves are checked here instead of a guessed name.
                cur.execute(
                    """SELECT array_agg(a.attname ORDER BY a.attnum)
                       FROM pg_constraint c
                       JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
                       WHERE c.conrelid = 'ideas'::regclass AND c.contype = 'u'
                       GROUP BY c.oid"""
                )
                row = cur.fetchone()
                assert row is not None
                assert set(row[0]) == {"asset", "timeframe", "setup_id", "bar_open_time", "strategy_version_id"}
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc}")
