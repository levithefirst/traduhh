from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable
import json

from agent.config import FROZEN_ASSETS, FROZEN_TIMEFRAMES, load_settings
from agent.ingest.book import fetch_book, upsert_book
from agent.ingest.candles import fetch_recent_candles, upsert_candles
from agent.ingest.context import fetch_contexts, upsert_contexts
from agent.features import FeatureWarmupError, compute_and_persist_latest
from agent.pipeline import scan_closed_bar
from agent.integrity import inspect_latest_market_data
from agent.models import audit_event, claim_job, finish_job, record_ctx_health, recover_running_jobs
from agent.monitor import run_equity_snap_job, run_monitor_open_job
from agent.timeutil import require_utc, utc_now
from agent.logging_setup import configure_logging

LOGGER = logging.getLogger(__name__)


def _slot(now: datetime, seconds: int) -> datetime:
    current = require_utc(now)
    epoch = int(current.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=timezone.utc)


def scheduled_for(job_name: str, now: datetime | None = None) -> datetime:
    """Return the deterministic scheduler key for a job occurrence."""
    current = require_utc(now or utc_now())
    if job_name in {"ctx_poll", "book_poll"}:
        return _slot(current, 15)
    if job_name == "integrity":
        return _slot(current, 30)
    if job_name == "candle_15m":
        base = current.replace(minute=(current.minute // 15) * 15, second=0, microsecond=0)
        return base + timedelta(seconds=8)
    if job_name == "candle_1h":
        return current.replace(minute=0, second=8, microsecond=0)
    if job_name == "candle_4h":
        base_hour = (current.hour // 4) * 4
        return current.replace(hour=base_hour, minute=0, second=8, microsecond=0)
    if job_name == "monitor_open":
        return _slot(current, 15)
    if job_name == "equity_snap":
        return current.replace(second=0, microsecond=0)
    raise ValueError(f"unsupported scheduler job: {job_name}")


def run_scheduled_job(
    conn,
    job_name: str,
    callback: Callable[[], dict | None],
    *,
    scheduled_at: datetime | None = None,
) -> bool:
    """Run one durable job occurrence with overlap protection and failure persistence."""
    scheduled = require_utc(scheduled_at or scheduled_for(job_name))
    claim = claim_job(conn, job_name, scheduled)
    if not claim.acquired:
        audit_event(conn, actor="scheduler", action="job_skipped", payload={"job": job_name, "scheduled_for": scheduled.isoformat(), "reason": claim.reason})
        LOGGER.info(
            "job_skipped",
            extra={"event": "job_skipped", "job": job_name, "scheduled_for": scheduled.isoformat(), "reason": claim.reason},
        )
        return False

    started = utc_now()
    try:
        stats = callback() or {}
    except Exception as exc:
        if job_name == "ctx_poll":
            try:
                health = record_ctx_health(conn, success=False, error=str(exc))
                if health["hl_down"]:
                    audit_event(conn, actor="integrity", action="hl_down", payload={"consecutive_failures": health["consecutive_failures"], "error": str(exc)})
            except Exception:
                LOGGER.exception("ctx_failure_state_persist_failed", extra={"event": "ctx_failure_state_persist_failed", "job": job_name})
        try:
            finish_job(conn, claim.job_id, status="FAILED", error=str(exc), stats={"duration_ms": int((utc_now() - started).total_seconds() * 1000)})
        except Exception:
            LOGGER.exception("job_failure_persist_failed", extra={"event": "job_failure_persist_failed", "job": job_name})
        LOGGER.exception("job_failed", extra={"event": "job_failed", "job": job_name, "scheduled_for": scheduled.isoformat()})
        raise
    if job_name == "ctx_poll":
        record_ctx_health(conn, success=True)
    finish_job(conn, claim.job_id, status="SUCCESS", stats=stats)
    LOGGER.info(
        "job_succeeded",
        extra={
            "event": "job_succeeded",
            "job": job_name,
            "scheduled_for": scheduled.isoformat(),
            "duration_ms": int((utc_now() - started).total_seconds() * 1000),
        },
    )
    return True


def _market_callbacks(settings, client):
    def ctx_poll() -> dict:
        from agent.db import connect
        with connect(settings.database_url) as conn:
            count = upsert_contexts(conn, fetch_contexts(client))
            return {"contexts": count}

    def book_poll() -> dict:
        from agent.db import connect
        with connect(settings.database_url) as conn:
            count = 0
            for asset in FROZEN_ASSETS:
                count += upsert_book(conn, fetch_book(client, asset))
            return {"books": count}

    def candle_job(timeframe: str) -> Callable[[], dict]:
        def callback() -> dict:
            from agent.db import connect
            with connect(settings.database_url) as conn:
                count = 0
                feature_count = 0
                regime_count = 0
                idea_count = 0
                for asset in FROZEN_ASSETS:
                    count += upsert_candles(conn, fetch_recent_candles(client, asset, timeframe, bars=5))
                    try:
                        snapshot, regime = compute_and_persist_latest(conn, asset=asset, timeframe=timeframe)
                        feature_count += 1
                        regime_count += 1 if regime is not None else 0
                        idea_count += len(scan_closed_bar(conn, settings=settings, asset=asset, timeframe=timeframe, bar_open_time=snapshot.open_time))
                    except FeatureWarmupError:
                        LOGGER.info(
                            "feature_warmup_insufficient",
                            extra={"event": "feature_warmup_insufficient", "asset": asset, "timeframe": timeframe},
                        )
                return {"timeframe": timeframe, "candles": count, "features": feature_count, "regimes": regime_count, "ideas": idea_count}

        return callback

    def integrity_job() -> dict:
        from agent.db import connect
        with connect(settings.database_url) as conn:
            result = inspect_latest_market_data(conn)
            LOGGER.info(
                "integrity_result",
                extra={"event": "integrity_result", "job": "integrity", "flags": result.flags, "halt": result.halt},
            )
            if result.halt:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            """INSERT INTO system_state(key,value,updated_at) VALUES ('integrity',%s::jsonb,%s)
                            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at""",
                            (json.dumps({"ok": False, "halt": True, "flags": list(result.flags)}, separators=(",", ":")), utc_now()),
                        )
            else:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            """INSERT INTO system_state(key,value,updated_at) VALUES ('integrity',%s::jsonb,%s)
                            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at""",
                            (json.dumps({"ok": True, "halt": False, "flags": []}, separators=(",", ":")), utc_now()),
                        )
            return {"ok": result.ok, "halt": result.halt, "flags": list(result.flags)}

    def monitor_open_job() -> dict:
        from agent.db import connect
        with connect(settings.database_url) as conn:
            return run_monitor_open_job(conn, settings=settings)

    def equity_snap_job() -> dict:
        from agent.db import connect
        with connect(settings.database_url) as conn:
            return run_equity_snap_job(conn, settings=settings)

    return {
        "ctx_poll": ctx_poll,
        "book_poll": book_poll,
        "candle_15m": candle_job("15m"),
        "candle_1h": candle_job("1h"),
        "candle_4h": candle_job("4h"),
        "integrity": integrity_job,
        "monitor_open": monitor_open_job,
        "equity_snap": equity_snap_job,
    }


def build_scheduler(settings, client):
    """Build the frozen single-process APScheduler job skeleton for Step 3."""
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = BackgroundScheduler(timezone=timezone.utc, job_defaults={"coalesce": True, "max_instances": 1})
    callbacks = _market_callbacks(settings, client)

    def add(job_name: str, trigger) -> None:
        callback = callbacks[job_name]
        scheduler.add_job(
            lambda name=job_name, cb=callback: _run_job_from_scheduler(settings, name, cb),
            trigger=trigger,
            id=job_name,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    add("ctx_poll", IntervalTrigger(seconds=15, timezone=timezone.utc))
    add("book_poll", IntervalTrigger(seconds=15, timezone=timezone.utc))
    add("candle_15m", CronTrigger(minute="*/15", second=8, timezone=timezone.utc))
    add("candle_1h", CronTrigger(minute=0, second=8, timezone=timezone.utc))
    add("candle_4h", CronTrigger(hour="0,4,8,12,16,20", minute=0, second=8, timezone=timezone.utc))
    add("integrity", IntervalTrigger(seconds=30, timezone=timezone.utc))
    add("monitor_open", IntervalTrigger(seconds=15, timezone=timezone.utc))
    add("equity_snap", IntervalTrigger(seconds=60, timezone=timezone.utc))
    return scheduler


def _run_job_from_scheduler(settings, job_name: str, callback: Callable[[], dict | None]) -> None:
    from agent.db import connect
    with connect(settings.database_url) as conn:
        run_scheduled_job(conn, job_name, callback)


def start_telegram_listener(settings) -> tuple[object, Callable[[], None]]:
    """Telegram long-poll listener on a thread inside this same worker (spec 1.1)."""
    import threading

    from agent.db import connect
    from agent.telegram.bot import Dispatcher, TelegramTransport, poll_once

    transport = TelegramTransport(settings.telegram_bot_token)
    dispatcher = Dispatcher(settings, conn_factory=lambda: connect(settings.database_url))
    stop = threading.Event()

    def loop() -> None:
        offset: int | None = None
        while not stop.is_set():
            try:
                offset = poll_once(transport, dispatcher, offset)
            except Exception:
                LOGGER.exception("telegram_listener_error", extra={"event": "telegram_listener_error"})
                stop.wait(5)

    thread = threading.Thread(target=loop, name="telegram-listener", daemon=True)
    thread.start()

    def shutdown() -> None:
        stop.set()
        transport.close()

    return thread, shutdown


def main() -> int:
    from agent.db import connect, startup_check
    settings = load_settings()
    configure_logging(settings.log_level)
    applied = startup_check(settings.database_url)
    with connect(settings.database_url) as conn:
        recovered = recover_running_jobs(conn)
    LOGGER.info("startup_ok", extra={"event": "startup_ok", "migrations_applied": applied, "recovered_jobs": recovered})

    # Market-data/integrity (Step 3) and paper monitoring (Step 6) share this one
    # worker/scheduler. Telegram and LLM jobs are not registered yet.
    from agent.hl_client import HyperliquidClient

    client = HyperliquidClient(settings.hl_info_url)
    scheduler = build_scheduler(settings, client)
    scheduler.start()
    _, stop_telegram = start_telegram_listener(settings)
    try:
        # APScheduler owns the process in the final worker; keep the skeleton alive.
        import time
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0
    finally:
        stop_telegram()
        scheduler.shutdown(wait=False)
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
