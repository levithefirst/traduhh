from __future__ import annotations

from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(database_url: str) -> psycopg.Connection:
    return psycopg.connect(database_url, connect_timeout=5)


def run_migrations(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute("SELECT version FROM schema_migrations ORDER BY version")
        applied = {row[0] for row in cur.fetchall()}

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    applied_now: list[str] = []
    for path in migration_files:
        version = path.name
        if version in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute("INSERT INTO schema_migrations(version) VALUES (%s)", (version,))
        applied_now.append(version)
    return applied_now


def startup_check(database_url: str) -> list[str]:
    with connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            if cur.fetchone() != (1,):
                raise RuntimeError("database health check failed")
        return run_migrations(conn)
