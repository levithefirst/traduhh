"""Paper-trade alert dispatch (spec 12.3). Notification only.

Nothing in this module places, signs, or simulates an order — it reports what
the deterministic pipeline and the Step 6 paper monitor already decided and
recorded.

De-duplication is durable: the dedupe key is derived from the event itself
(idea or position id plus alert kind) and claimed in `alerts_sent` before the
message goes out, so a worker restart mid-dispatch cannot re-send. Only chats
in TELEGRAM_ALLOWED_CHAT_IDS ever receive anything.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Mapping, Sequence

from agent.telegram import formatters
from agent.timeutil import utc_now

LOGGER = logging.getLogger(__name__)

KIND_TRADE_PAPER = "trade_paper"
KIND_FILL = "paper_fill"
KIND_CLOSE = "paper_close"

# Only these decisions are ever announced. NO_TRADE, WAIT, gate failures and
# LLM vetoes are journaled but never alerted (WAIT only under /verbose).
ALERTABLE_DECISIONS = ("TRADE_PAPER",)


def dedupe_key(kind: str, entity_id: str, chat_id: int) -> str:
    return f"{kind}:{entity_id}:{chat_id}"


def should_alert_decision(decision: str) -> bool:
    return decision in ALERTABLE_DECISIONS


def claim_alert(conn, *, key: str, kind: str, chat_id: int,
                idea_id: str | None = None, position_id: str | None = None) -> bool:
    """Reserve the right to send exactly once. False means already sent."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO alerts_sent(dedupe_key, kind, idea_id, position_id, chat_id, sent_at, delivered)
                   VALUES (%s,%s,%s,%s,%s,%s,false)
                   ON CONFLICT (dedupe_key) DO NOTHING
                   RETURNING dedupe_key""",
                (key, kind, idea_id, position_id, chat_id, utc_now()),
            )
            return cur.fetchone() is not None


def mark_delivered(conn, *, key: str) -> None:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("UPDATE alerts_sent SET delivered=true WHERE dedupe_key=%s", (key,))


class AlertDispatcher:
    """Sends one message per (event, authorized chat), at most once, ever."""

    def __init__(self, settings, *, sender: Callable[[int, str], bool]):
        self.settings = settings
        self._sender = sender
        self._recipients: tuple[int, ...] = tuple(settings.telegram_allowed_chat_ids)

    @property
    def recipients(self) -> tuple[int, ...]:
        return self._recipients

    def _broadcast(self, conn, *, kind: str, entity_id: str, text: str,
                   idea_id: str | None = None, position_id: str | None = None) -> int:
        sent = 0
        for chat_id in self._recipients:
            key = dedupe_key(kind, entity_id, chat_id)
            if not claim_alert(conn, key=key, kind=kind, chat_id=chat_id,
                               idea_id=idea_id, position_id=position_id):
                continue
            if self._sender(chat_id, text):
                mark_delivered(conn, key=key)
                sent += 1
            else:
                LOGGER.warning("alert_send_failed", extra={"event": "alert_send_failed", "kind": kind})
        return sent

    def alert_trade_paper(self, conn, idea: Mapping[str, Any]) -> int:
        if not should_alert_decision(str(idea.get("decision"))):
            return 0
        return self._broadcast(conn, kind=KIND_TRADE_PAPER, entity_id=str(idea["id"]),
                               text=formatters.format_trade_paper_alert(idea), idea_id=str(idea["id"]))

    def alert_fill(self, conn, position: Mapping[str, Any]) -> int:
        return self._broadcast(conn, kind=KIND_FILL, entity_id=str(position["id"]),
                               text=formatters.format_fill_alert(position),
                               idea_id=str(position.get("idea_id")) if position.get("idea_id") else None,
                               position_id=str(position["id"]))

    def alert_close(self, conn, position: Mapping[str, Any]) -> int:
        return self._broadcast(conn, kind=KIND_CLOSE, entity_id=str(position["id"]),
                               text=formatters.format_close_alert(position),
                               idea_id=str(position.get("idea_id")) if position.get("idea_id") else None,
                               position_id=str(position["id"]))


def _as_json(value):
    if value is None:
        return None
    return json.loads(value) if isinstance(value, str) else value


def fetch_alertable_ideas(conn, idea_ids: Sequence[str]) -> list[dict[str, Any]]:
    """Load the TRADE_PAPER ideas named by the scan, with everything the alert needs."""
    if not idea_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, asset, timeframe, setup_id, direction, decision, geometry, costs,
                      regime, features, llm_review, strategy_version_id, prompt_version_id
               FROM ideas WHERE id = ANY(%s) AND decision = 'TRADE_PAPER'""",
            (list(idea_ids),),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({
            "id": str(r[0]), "asset": r[1], "timeframe": r[2], "setup_id": r[3], "direction": r[4],
            "decision": r[5], "geometry": _as_json(r[6]) or {}, "costs": _as_json(r[7]) or {},
            "regime": _as_json(r[8]) or {}, "features": _as_json(r[9]) or {},
            "llm_review": _as_json(r[10]) or {}, "strategy_version_id": r[11], "prompt_version_id": r[12],
            "hist_cell": {"n": 0, "note": "unproven"},
        })
    return out


def fetch_position(conn, position_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, idea_id, asset, tf, direction, entry, stop, size, risk_cash, status,
                      exit_px, exit_reason, realized_r, pnl_usd, fees_usd, funding_usd,
                      mfe_r, mae_r, bars_held, outcome_class, funding_missing
               FROM paper_positions WHERE id = %s""",
            (position_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    keys = ("id", "idea_id", "asset", "tf", "direction", "entry", "stop", "size", "risk_cash", "status",
            "exit_px", "exit_reason", "realized_r", "pnl_usd", "fees_usd", "funding_usd",
            "mfe_r", "mae_r", "bars_held", "outcome_class", "funding_missing")
    record = dict(zip(keys, row))
    record["id"] = str(record["id"])
    record["idea_id"] = str(record["idea_id"]) if record["idea_id"] else None
    for key in ("entry", "stop", "size", "risk_cash", "exit_px", "realized_r", "pnl_usd",
                "fees_usd", "funding_usd", "mfe_r", "mae_r"):
        if record.get(key) is not None:
            record[key] = float(record[key])
    return record


def dispatch_position_alerts(conn, dispatcher: AlertDispatcher) -> dict[str, int]:
    """Announce fills for OPEN positions and outcomes for CLOSED ones.

    Both paths are keyed on the position id, so re-running after a restart
    re-claims nothing and sends nothing twice.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id, status FROM paper_positions ORDER BY opened_at ASC")
        rows = cur.fetchall()
    fills, closes = 0, 0
    for position_id, status in rows:
        position = fetch_position(conn, str(position_id))
        if position is None:
            continue
        fills += dispatcher.alert_fill(conn, position)
        if status == "CLOSED":
            closes += dispatcher.alert_close(conn, position)
    return {"fills": fills, "closes": closes}
