"""Circuit breakers: mode, halt/resume, and paper loss limits (spec 15.3).

Mode lives in `system_state['mode']`. Halting stops new TRADE_PAPER ideas;
open paper positions still flatten through the Step 6 monitor, per spec 1.4.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from agent.models import audit_event
from agent.timeutil import require_utc, utc_now

MODE_PAPER = "paper"
MODE_HALTED = "halted"
VALID_MODES = (MODE_PAPER, MODE_HALTED)

DAILY_LOSS_LIMIT = -0.02
WEEKLY_LOSS_LIMIT = -0.05


def _state(conn, key: str):
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM system_state WHERE key=%s", (key,))
        row = cur.fetchone()
    if not row or row[0] is None:
        return None
    value = row[0]
    return json.loads(value) if isinstance(value, str) else value


def _write_state(conn, key: str, value: dict) -> None:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO system_state(key,value,updated_at) VALUES (%s,%s::jsonb,%s)
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at""",
                (key, json.dumps(value, separators=(",", ":")), utc_now()),
            )


def get_mode(conn) -> str:
    value = _state(conn, "mode")
    if isinstance(value, dict):
        mode = value.get("mode")
    else:
        mode = value
    return mode if mode in VALID_MODES else MODE_PAPER


def is_halted(conn) -> bool:
    return get_mode(conn) == MODE_HALTED


def halt_reasons(conn) -> list[str]:
    value = _state(conn, "mode")
    if isinstance(value, dict):
        reasons = value.get("reasons")
        if isinstance(reasons, list):
            return [str(r) for r in reasons]
    return []


def integrity_ok(conn) -> bool:
    value = _state(conn, "integrity")
    if not isinstance(value, dict):
        return True
    return bool(value.get("ok", True)) and not bool(value.get("halt", False))


def halt(conn, *, reason: str, actor: str) -> dict:
    """Set halted. New TRADE_PAPER ideas stop; the paper monitor keeps running."""
    reasons = halt_reasons(conn)
    if reason and reason not in reasons:
        reasons.append(reason)
    value = {"mode": MODE_HALTED, "reasons": reasons, "updated_at": utc_now().isoformat()}
    _write_state(conn, "mode", value)
    audit_event(conn, actor=actor, action="halt", payload={"reason": reason, "reasons": reasons})
    return value


def resume(conn, *, actor: str) -> tuple[bool, str]:
    """Clear halt only when integrity is clean (spec 12.2: /resume requires integrity.ok)."""
    if not integrity_ok(conn):
        audit_event(conn, actor=actor, action="resume_refused", payload={"reason": "integrity_not_ok"})
        return False, "integrity_not_ok"
    value = {"mode": MODE_PAPER, "reasons": [], "updated_at": utc_now().isoformat()}
    _write_state(conn, "mode", value)
    audit_event(conn, actor=actor, action="resume", payload={})
    return True, "resumed"


@dataclass(frozen=True)
class LossBreakers:
    day_pnl_pct: float
    week_pnl_pct: float
    daily_tripped: bool
    weekly_tripped: bool

    @property
    def tripped(self) -> bool:
        return self.daily_tripped or self.weekly_tripped

    @property
    def reasons(self) -> list[str]:
        out = []
        if self.daily_tripped:
            out.append("daily_loss")
        if self.weekly_tripped:
            out.append("weekly_loss")
        return out


def evaluate_loss_breakers(*, day_pnl: float, week_pnl: float, equity: float) -> LossBreakers:
    """Pure breaker math: daily <= -2%, weekly <= -5% of equity (spec 15.3)."""
    if equity <= 0:
        raise ValueError("equity must be positive")
    day_pct = day_pnl / equity
    week_pct = week_pnl / equity
    return LossBreakers(
        day_pnl_pct=day_pct,
        week_pnl_pct=week_pct,
        daily_tripped=day_pct <= DAILY_LOSS_LIMIT,
        weekly_tripped=week_pct <= WEEKLY_LOSS_LIMIT,
    )


def realized_pnl_since(conn, since: datetime) -> float:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT coalesce(sum(pnl_usd),0) FROM paper_positions WHERE status='CLOSED' AND closed_at >= %s",
            (require_utc(since),),
        )
        return float(cur.fetchone()[0])


def loss_breakers(conn, *, equity: float, now: datetime | None = None) -> LossBreakers:
    current = require_utc(now or utc_now())
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())
    return evaluate_loss_breakers(
        day_pnl=realized_pnl_since(conn, day_start),
        week_pnl=realized_pnl_since(conn, week_start),
        equity=equity,
    )
