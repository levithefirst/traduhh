"""Raw-SQL database helpers for the Step 1 foundation.

The specification permits either SQLAlchemy or psycopg/raw SQL. The project uses
psycopg + raw SQL consistently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    import psycopg


def table_exists(conn: "psycopg.Connection", table_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass(%s) IS NOT NULL",
            (f"public.{table_name}",),
        )
        return bool(cur.fetchone()[0])


def count_rows(conn: "psycopg.Connection", table_name: str) -> int:
    if not table_exists(conn, table_name):
        raise ValueError(f"unknown table: {table_name}")
    with conn.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM "{table_name}"')
        return int(cur.fetchone()[0])


@dataclass(frozen=True)
class JobClaim:
    acquired: bool
    job_id: str | None
    reason: str


def claim_job(conn: "psycopg.Connection", job_name: str, scheduled_for) -> JobClaim:
    """Atomically claim a durable job key; overlapping execution is skipped."""
    import uuid

    now = __import__("agent.timeutil", fromlist=["utc_now"]).utc_now()
    job_id = str(uuid.uuid4())
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO job_runs(id,job_name,scheduled_for,started_at,status,stats)
                   VALUES (%s,%s,%s,%s,'RUNNING','{}'::jsonb)
                   ON CONFLICT (job_name,scheduled_for) DO NOTHING
                   RETURNING id""",
                (job_id, job_name, scheduled_for, now),
            )
            row = cur.fetchone()
            if row:
                return JobClaim(True, str(row[0]), "claimed")

            cur.execute(
                "SELECT id,status FROM job_runs WHERE job_name=%s AND scheduled_for=%s FOR UPDATE",
                (job_name, scheduled_for),
            )
            existing = cur.fetchone()
            if not existing:
                raise RuntimeError("job claim disappeared during transaction")
            existing_id, status = str(existing[0]), existing[1]
            if status == "FAILED":
                cur.execute(
                    """UPDATE job_runs SET id=%s,started_at=%s,finished_at=NULL,status='RUNNING',error=NULL,stats='{}'::jsonb
                       WHERE job_name=%s AND scheduled_for=%s AND status='FAILED'""",
                    (job_id, now, job_name, scheduled_for),
                )
                return JobClaim(True, job_id, "retry_failed")
            return JobClaim(False, existing_id, f"existing_{status.lower()}")


def finish_job(conn: "psycopg.Connection", job_id: str, *, status: str, error: str | None = None, stats: dict | None = None) -> None:
    if status not in {"SUCCESS", "FAILED"}:
        raise ValueError("invalid terminal job status")
    now = __import__("agent.timeutil", fromlist=["utc_now"]).utc_now()
    import json
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE job_runs SET finished_at=%s,status=%s,error=%s,stats=%s::jsonb WHERE id=%s AND status='RUNNING'",
                (now, status, error, json.dumps(stats or {}, separators=(",", ":")), job_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError("job completion did not update a running job")


def recover_running_jobs(conn: "psycopg.Connection") -> int:
    """Restart recovery: no RUNNING row survives a worker restart."""
    now = __import__("agent.timeutil", fromlist=["utc_now"]).utc_now()
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE job_runs SET status='FAILED',finished_at=%s,error='worker_restart_recovery'
                   WHERE status='RUNNING'""",
                (now,),
            )
            return cur.rowcount


def audit_event(conn: "psycopg.Connection", *, actor: str, action: str, payload: dict) -> None:
    import json
    from agent.timeutil import utc_now

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_log(ts,actor,action,payload) VALUES (%s,%s,%s,%s::jsonb)",
                (utc_now(), actor, action, json.dumps(payload, separators=(",", ":"))),
            )


def record_ctx_health(conn: "psycopg.Connection", *, success: bool, error: str | None = None) -> dict[str, object]:
    import json
    from agent.timeutil import utc_now

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM system_state WHERE key='integrity.hl_down' FOR UPDATE")
            row = cur.fetchone()
            state = row[0] if row else {}
            if isinstance(state, str):
                state = json.loads(state)
            failures = int(state.get("consecutive_failures", 0)) if isinstance(state, dict) else 0
            failures = 0 if success else failures + 1
            value = {
                "consecutive_failures": failures,
                "hl_down": failures >= 3,
                "last_error": None if success else error,
                "updated_at": utc_now().isoformat(),
            }
            cur.execute(
                """INSERT INTO system_state(key,value,updated_at) VALUES ('integrity.hl_down',%s::jsonb,%s)
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at""",
                (json.dumps(value, separators=(",", ":")), utc_now()),
            )
            return value
