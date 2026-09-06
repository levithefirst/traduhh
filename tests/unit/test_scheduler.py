from datetime import datetime, timezone

import pytest

from agent.__main__ import run_scheduled_job, scheduled_for


def test_scheduler_occurrence_keys_are_utc_and_deterministic():
    now = datetime(2026, 9, 5, 10, 16, 12, tzinfo=timezone.utc)
    assert scheduled_for("ctx_poll", now).isoformat() == "2026-09-05T10:16:00+00:00"
    assert scheduled_for("integrity", now).isoformat() == "2026-09-05T10:16:00+00:00"
    assert scheduled_for("candle_15m", now).isoformat() == "2026-09-05T10:15:08+00:00"
    assert scheduled_for("candle_1h", now).isoformat() == "2026-09-05T10:00:08+00:00"
    assert scheduled_for("candle_4h", now).isoformat() == "2026-09-05T08:00:08+00:00"


def test_scheduler_rejects_unsupported_job_key():
    with pytest.raises(ValueError):
        scheduled_for("features", datetime.now(timezone.utc))


def test_run_scheduled_job_skips_existing_occurrence(monkeypatch):
    class Claim:
        acquired = False
        job_id = "existing"
        reason = "existing_RUNNING"

    calls = []
    monkeypatch.setattr("agent.__main__.claim_job", lambda *args, **kwargs: Claim())
    monkeypatch.setattr("agent.__main__.audit_event", lambda *args, **kwargs: calls.append((args, kwargs)))
    callback = lambda: (_ for _ in ()).throw(AssertionError("must not run"))

    assert run_scheduled_job(object(), "integrity", callback, scheduled_at=datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)) is False
    assert calls


def test_run_scheduled_job_marks_failure_and_reraises(monkeypatch):
    class Claim:
        acquired = True
        job_id = "job-1"
        reason = "claimed"

    finished = []
    monkeypatch.setattr("agent.__main__.claim_job", lambda *args, **kwargs: Claim())
    monkeypatch.setattr("agent.__main__.finish_job", lambda *args, **kwargs: finished.append(kwargs))
    monkeypatch.setattr("agent.__main__.audit_event", lambda *args, **kwargs: None)

    def fail():
        raise RuntimeError("hl unavailable")

    with pytest.raises(RuntimeError, match="hl unavailable"):
        run_scheduled_job(object(), "book_poll", fail, scheduled_at=datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc))
    assert finished[0]["status"] == "FAILED"
    assert finished[0]["error"] == "hl unavailable"


def test_run_scheduled_job_success_is_persisted(monkeypatch):
    class Claim:
        acquired = True
        job_id = "job-2"
        reason = "claimed"

    finished = []
    monkeypatch.setattr("agent.__main__.claim_job", lambda *args, **kwargs: Claim())
    monkeypatch.setattr("agent.__main__.finish_job", lambda *args, **kwargs: finished.append(kwargs))
    monkeypatch.setattr("agent.__main__.record_ctx_health", lambda *args, **kwargs: {"hl_down": False})

    assert run_scheduled_job(object(), "ctx_poll", lambda: {"contexts": 3}, scheduled_at=datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)) is True
    assert finished[0]["status"] == "SUCCESS"
    assert finished[0]["stats"] == {"contexts": 3}


class FakeCursor:
    def __init__(self):
        self.calls = []
        self.rows = {}
        self._fetchone = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if sql.startswith("INSERT INTO job_runs"):
            key = (params[1], params[2])
            if key in self.rows:
                self._fetchone = None
                self.rowcount = 0
            else:
                self.rows[key] = {"id": params[0], "status": "RUNNING"}
                self._fetchone = (params[0],)
                self.rowcount = 1
        elif sql.startswith("SELECT id,status FROM job_runs"):
            key = (params[0], params[1])
            row = self.rows.get(key)
            self._fetchone = (row["id"], row["status"]) if row else None
        elif "UPDATE job_runs SET id=%s" in sql:
            key = (params[2], params[3])
            self.rows[key] = {"id": params[0], "status": "RUNNING"}
            self.rowcount = 1
        elif sql.startswith("UPDATE job_runs SET finished_at"):
            job_id = params[-1]
            for row in self.rows.values():
                if row["id"] == job_id:
                    row["status"] = params[1]
                    break
            self.rowcount = 1
        elif sql.startswith("UPDATE job_runs SET status='FAILED'"):
            count = 0
            for row in self.rows.values():
                if row["status"] == "RUNNING":
                    row["status"] = "FAILED"
                    count += 1
            self.rowcount = count

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeConn:
    def __init__(self):
        self.cur = FakeCursor()

    def cursor(self):
        return self.cur

    def transaction(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_job_claim_is_idempotent_and_failed_runs_can_recover():
    from agent.models import claim_job, finish_job, recover_running_jobs

    conn = FakeConn()
    when = datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)
    first = claim_job(conn, "integrity", when)
    second = claim_job(conn, "integrity", when)
    assert first.acquired is True
    assert second.acquired is False
    assert second.reason == "existing_running"

    finish_job(conn, first.job_id, status="FAILED", error="timeout")
    retry = claim_job(conn, "integrity", when)
    assert retry.acquired is True
    assert retry.reason == "retry_failed"

    assert recover_running_jobs(conn) == 1
    assert conn.cur.rows[("integrity", when)]["status"] == "FAILED"
