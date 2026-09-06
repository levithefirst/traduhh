"""Telegram operator surface (spec 12).

Access is allowlist-only: an update from a chat id outside
TELEGRAM_ALLOWED_CHAT_IDS produces no reply at all. Commands are rate
limited to 10/min/chat. No command can change setup parameters, risk
fraction, or the universe — those require a code change and a new
strategy_version (spec 12.2 SYSTEM RULE), so no such handler exists.

The bot token is never logged and never appears in a reply.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from datetime import timedelta
from typing import Any, Callable, Mapping

import httpx

from agent import circuit
from agent.telegram import formatters
from agent.timeutil import require_utc, utc_now
from agent.versioning import STRATEGY_VERSION_ID

LOGGER = logging.getLogger(__name__)

RATE_LIMIT_PER_MIN = 10
VERBOSE_STATE_KEY = "telegram.verbose"
TELEGRAM_API_BASE = "https://api.telegram.org"


def parse_command(text: str | None) -> tuple[str, list[str]] | None:
    """Return (command, args) for a slash command, else None."""
    if not text:
        return None
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split()
    command = parts[0].lower()
    if "@" in command:  # /status@mybot in groups
        command = command.split("@", 1)[0]
    return command, parts[1:]


class RateLimiter:
    """Sliding-window limiter, 10 commands per minute per chat (spec 12.1)."""

    def __init__(self, *, limit: int = RATE_LIMIT_PER_MIN, window_s: int = 60, clock: Callable[[], Any] = utc_now):
        self._limit = limit
        self._window = timedelta(seconds=window_s)
        self._clock = clock
        self._hits: dict[int, deque] = defaultdict(deque)

    def allow(self, chat_id: int) -> bool:
        now = require_utc(self._clock())
        hits = self._hits[chat_id]
        while hits and now - hits[0] >= self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return False
        hits.append(now)
        return True


def get_verbose(conn) -> bool:
    value = circuit._state(conn, VERBOSE_STATE_KEY)
    if isinstance(value, dict):
        return bool(value.get("enabled", False))
    return bool(value) if value is not None else False


def set_verbose(conn, enabled: bool) -> None:
    circuit._write_state(conn, VERBOSE_STATE_KEY, {"enabled": bool(enabled), "updated_at": utc_now().isoformat()})


# ---------------------------------------------------------------- data access

def fetch_status(conn, settings) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT equity FROM paper_equity ORDER BY ts DESC LIMIT 1")
        row = cur.fetchone()
        equity = float(row[0]) if row else float(settings.paper_equity_usd)
        cur.execute("SELECT count(*) FROM paper_positions WHERE status='OPEN'")
        open_positions = int(cur.fetchone()[0])
        cur.execute("SELECT max(created_at) FROM ideas")
        last_scan = cur.fetchone()[0]
    now = utc_now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_pnl = circuit.realized_pnl_since(conn, day_start)
    return {
        "mode": circuit.get_mode(conn),
        "equity": equity,
        "day_pnl_usd": day_pnl,
        "day_pnl_pct": day_pnl / equity if equity else 0.0,
        "open_positions": open_positions,
        "last_scan_ts": last_scan.isoformat() if last_scan else None,
        "integrity_ok": circuit.integrity_ok(conn),
        "halt_reasons": circuit.halt_reasons(conn),
    }


def fetch_health(conn) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT max(finished_at) FROM job_runs WHERE job_name='ctx_poll' AND status='SUCCESS'")
        hl_last = cur.fetchone()[0]
        cur.execute("SELECT max(created_at) FROM llm_reviews WHERE valid IS TRUE")
        llm_last = cur.fetchone()[0]
    hl_state = circuit._state(conn, "integrity.hl_down") or {}
    integrity = circuit._state(conn, "integrity") or {}
    return {
        "db_ok": True,
        "hl_last_success": hl_last.isoformat() if hl_last else None,
        "hl_down": bool(hl_state.get("hl_down", False)) if isinstance(hl_state, dict) else False,
        "llm_last_success": llm_last.isoformat() if llm_last else None,
        "flags": list(integrity.get("flags") or []) if isinstance(integrity, dict) else [],
    }


def fetch_regime(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT ON (asset, timeframe) asset, timeframe, label, secondary, confidence, open_time
               FROM regime_snapshots ORDER BY asset, timeframe, open_time DESC"""
        )
        rows = cur.fetchall()
    return [
        {"asset": r[0], "timeframe": r[1], "label": r[2], "secondary": list(r[3] or []),
         "confidence": float(r[4]) if r[4] is not None else None,
         "open_time": r[5].isoformat() if r[5] else None}
        for r in rows
    ]


def fetch_ideas(conn, limit: int = 5) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, created_at, asset, timeframe, setup_id, direction, decision
               FROM ideas ORDER BY created_at DESC LIMIT %s""",
            (limit,),
        )
        rows = cur.fetchall()
    return [
        {"id": str(r[0]), "created_at": r[1].isoformat() if r[1] else None, "asset": r[2],
         "timeframe": r[3], "setup_id": r[4], "direction": r[5], "decision": r[6]}
        for r in rows
    ]


def _as_json(value):
    if value is None:
        return None
    return json.loads(value) if isinstance(value, str) else value


def fetch_idea(conn, idea_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT i.id, i.asset, i.timeframe, i.setup_id, i.direction, i.decision, i.decision_reason,
                      i.geometry, i.costs, i.gates, i.llm_review, i.confidence,
                      i.strategy_version_id, i.prompt_version_id,
                      p.exit_reason, p.realized_r, p.mfe_r, p.mae_r
               FROM ideas i LEFT JOIN paper_positions p ON p.idea_id = i.id
               WHERE i.id = %s""",
            (idea_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    idea = {
        "id": str(row[0]), "asset": row[1], "timeframe": row[2], "setup_id": row[3], "direction": row[4],
        "decision": row[5], "decision_reason": list(row[6] or []),
        "geometry": _as_json(row[7]) or {}, "costs": _as_json(row[8]) or {}, "gates": _as_json(row[9]) or {},
        "llm_review": _as_json(row[10]) or {},
        "confidence": float(row[11]) if row[11] is not None else None,
        "strategy_version_id": row[12], "prompt_version_id": row[13],
    }
    if row[14] is not None:
        idea["outcome"] = {
            "exit_reason": row[14],
            "realized_r": float(row[15]) if row[15] is not None else None,
            "mfe_r": float(row[16]) if row[16] is not None else None,
            "mae_r": float(row[17]) if row[17] is not None else None,
        }
    return idea


def fetch_positions(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, asset, tf, direction, entry, stop, mfe_r, mae_r, bars_held
               FROM paper_positions WHERE status='OPEN' ORDER BY opened_at ASC"""
        )
        rows = cur.fetchall()
    return [
        {"id": str(r[0]), "asset": r[1], "tf": r[2], "direction": r[3],
         "entry": float(r[4]) if r[4] is not None else None,
         "stop": float(r[5]) if r[5] is not None else None,
         "mfe_r": float(r[6]) if r[6] is not None else None,
         "mae_r": float(r[7]) if r[7] is not None else None,
         "bars_held": r[8] or 0}
        for r in rows
    ]


def fetch_journal(conn, limit: int = 10) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT i.created_at, i.asset, i.timeframe, i.setup_id, i.decision, p.realized_r, p.outcome_class
               FROM ideas i LEFT JOIN paper_positions p ON p.idea_id = i.id
               ORDER BY i.created_at DESC LIMIT %s""",
            (limit,),
        )
        rows = cur.fetchall()
    return [
        {"created_at": r[0].isoformat() if r[0] else None, "asset": r[1], "timeframe": r[2],
         "setup_id": r[3], "decision": r[4],
         "realized_r": float(r[5]) if r[5] is not None else None, "outcome_class": r[6]}
        for r in rows
    ]


def fetch_version(conn) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, code_git_sha FROM strategy_version ORDER BY created_at DESC LIMIT 1")
        sv = cur.fetchone()
        cur.execute("SELECT id FROM prompt_version ORDER BY created_at DESC LIMIT 1")
        pv = cur.fetchone()
    return {
        "strategy_version_id": sv[0] if sv else STRATEGY_VERSION_ID,
        "code_git_sha": sv[1] if sv else None,
        "prompt_version_id": pv[0] if pv else None,
        "mode": circuit.get_mode(conn),
    }


# ---------------------------------------------------------------- dispatcher

class Dispatcher:
    """Routes an authorized command to a handler and returns the reply text."""

    def __init__(self, settings, *, conn_factory: Callable[[], Any], rate_limiter: RateLimiter | None = None):
        self.settings = settings
        self._conn_factory = conn_factory
        self._allowed = set(settings.telegram_allowed_chat_ids)
        self._limiter = rate_limiter or RateLimiter()

    def is_authorized(self, chat_id: int | None) -> bool:
        return chat_id is not None and chat_id in self._allowed

    def handle_update(self, update: Mapping[str, Any]) -> str | None:
        """None means: send nothing at all (stranger, or not a command)."""
        message = update.get("message") or update.get("edited_message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if not self.is_authorized(chat_id):
            LOGGER.info("telegram_update_ignored", extra={"event": "telegram_update_ignored"})
            return None
        parsed = parse_command(message.get("text"))
        if parsed is None:
            return None
        command, args = parsed
        if not self._limiter.allow(chat_id):
            LOGGER.info("telegram_rate_limited", extra={"event": "telegram_rate_limited"})
            return "Rate limit reached (10 commands/minute). Try again shortly."
        try:
            return self.dispatch(command, args)
        except Exception as exc:  # operator surface must not crash the worker
            LOGGER.exception("telegram_command_failed", extra={"event": "telegram_command_failed"})
            return f"Command failed: {type(exc).__name__}"

    def dispatch(self, command: str, args: list[str]) -> str:
        handler = HANDLERS.get(command)
        if handler is None:
            return "Unknown command. Send /help for the command list."
        with self._conn_factory() as conn:
            return handler(conn, self.settings, args)


def _limit_arg(args: list[str], default: int, maximum: int = 50) -> int:
    if not args:
        return default
    try:
        value = int(args[0])
    except ValueError:
        return default
    return max(1, min(maximum, value))


def _cmd_help(conn, settings, args):
    return formatters.format_help(circuit.get_mode(conn))


def _cmd_status(conn, settings, args):
    return formatters.format_status(fetch_status(conn, settings))


def _cmd_health(conn, settings, args):
    return formatters.format_health(fetch_health(conn))


def _cmd_regime(conn, settings, args):
    return formatters.format_regime(fetch_regime(conn))


def _cmd_ideas(conn, settings, args):
    return formatters.format_ideas(fetch_ideas(conn, _limit_arg(args, 5)))


def _cmd_idea(conn, settings, args):
    if not args:
        return "Usage: /idea <uuid>"
    return formatters.format_idea(fetch_idea(conn, args[0]))


def _cmd_positions(conn, settings, args):
    return formatters.format_positions(fetch_positions(conn))


def _cmd_journal(conn, settings, args):
    return formatters.format_journal(fetch_journal(conn, _limit_arg(args, 10)))


def _cmd_version(conn, settings, args):
    return formatters.format_version(fetch_version(conn))


def _cmd_mode(conn, settings, args):
    if args:
        return "Mode cannot be changed by command. Use /halt and /resume; testnet is not implemented."
    return formatters.format_mode(circuit.get_mode(conn))


def _cmd_halt(conn, settings, args):
    reason = " ".join(args) if args else "manual_halt"
    circuit.halt(conn, reason=reason, actor="telegram")
    return f"HALTED. Reason: {reason}\nNo new TRADE_PAPER ideas will be opened. Open positions still flatten."


def _cmd_resume(conn, settings, args):
    ok, detail = circuit.resume(conn, actor="telegram")
    if ok:
        return "RESUMED. Mode is paper."
    return "Resume refused: integrity is not ok. Fix integrity first."


def _cmd_verbose(conn, settings, args):
    if not args or args[0].lower() not in {"on", "off"}:
        return f"Usage: /verbose on|off (currently {'on' if get_verbose(conn) else 'off'})"
    enabled = args[0].lower() == "on"
    set_verbose(conn, enabled)
    return f"Verbose WAIT alerts {'enabled' if enabled else 'disabled'}."


def _cmd_stats(conn, settings, args):
    from agent.stats import format_stats_command

    return format_stats_command(conn, setup_id=args[0] if args else None)


HANDLERS: dict[str, Callable[[Any, Any, list[str]], str]] = {
    "/help": _cmd_help,
    "/start": _cmd_help,
    "/status": _cmd_status,
    "/health": _cmd_health,
    "/regime": _cmd_regime,
    "/ideas": _cmd_ideas,
    "/idea": _cmd_idea,
    "/positions": _cmd_positions,
    "/journal": _cmd_journal,
    "/version": _cmd_version,
    "/mode": _cmd_mode,
    "/halt": _cmd_halt,
    "/resume": _cmd_resume,
    "/verbose": _cmd_verbose,
    "/stats": _cmd_stats,
}


# ---------------------------------------------------------------- transport

class TelegramTransport:
    """Minimal Bot API client (sendMessage + getUpdates long poll).

    Raw HTTPS is used rather than a framework wrapper, per the spec's rule to
    pick the simpler option. The token lives only in the URL passed to httpx
    and is never logged.
    """

    def __init__(self, token: str, *, base_url: str = TELEGRAM_API_BASE, client: httpx.Client | None = None):
        self._token = token
        self._base = f"{base_url}/bot{token}"
        self._owned = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(35.0, connect=8.0))

    def close(self) -> None:
        if self._owned:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def send_message(self, chat_id: int, text: str) -> bool:
        try:
            response = self._client.post(f"{self._base}/sendMessage", json={"chat_id": chat_id, "text": text})
            response.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            # str(exc) can embed the request URL, which contains the token.
            LOGGER.warning("telegram_send_failed", extra={"event": "telegram_send_failed", "error": type(exc).__name__})
            return False

    def get_updates(self, offset: int | None = None, timeout_s: int = 25) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout_s}
        if offset is not None:
            payload["offset"] = offset
        try:
            response = self._client.post(f"{self._base}/getUpdates", json=payload)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            LOGGER.warning("telegram_poll_failed", extra={"event": "telegram_poll_failed", "error": type(exc).__name__})
            return []
        return list(body.get("result") or []) if isinstance(body, Mapping) else []


def poll_once(transport: TelegramTransport, dispatcher: Dispatcher, offset: int | None = None) -> int | None:
    """One long-poll cycle. Returns the next update offset."""
    updates = transport.get_updates(offset)
    next_offset = offset
    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            next_offset = update_id + 1
        reply = dispatcher.handle_update(update)
        if reply is None:
            continue
        chat_id = ((update.get("message") or update.get("edited_message") or {}).get("chat") or {}).get("id")
        if chat_id is not None:
            transport.send_message(chat_id, reply)
    return next_offset
