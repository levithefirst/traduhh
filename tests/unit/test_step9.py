from types import SimpleNamespace

import pytest

from agent.telegram import formatters
from agent.telegram.alerts import AlertDispatcher, dedupe_key, should_alert_decision

SETTINGS = SimpleNamespace(telegram_allowed_chat_ids=(111, 222), telegram_bot_token="secret-token")


class FakeAlertsTable:
    """Stands in for the alerts_sent table, honouring the PRIMARY KEY."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.claims = 0

    # -- connection/cursor protocol used by agent.telegram.alerts --
    def cursor(self):
        return self

    def transaction(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def execute(self, sql, params=()):
        self._last = None
        if "INSERT INTO alerts_sent" in sql:
            self.claims += 1
            key, kind, idea_id, position_id, chat_id, sent_at = params
            if key in self.rows:
                self._last = None
                return
            self.rows[key] = {"kind": kind, "idea_id": idea_id, "position_id": position_id,
                              "chat_id": chat_id, "sent_at": sent_at, "delivered": False}
            self._last = (key,)
        elif "UPDATE alerts_sent" in sql:
            key = params[0]
            if key in self.rows:
                self.rows[key]["delivered"] = True
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return self._last


class Recorder:
    def __init__(self, ok=True):
        self.sent = []
        self._ok = ok

    def __call__(self, chat_id, text):
        self.sent.append((chat_id, text))
        return self._ok


def idea(decision="TRADE_PAPER", idea_id="idea-1"):
    return {
        "id": idea_id, "asset": "BTC", "timeframe": "1h", "setup_id": "trend_pullback",
        "direction": "long", "decision": decision,
        "geometry": {"entry": 65000.0, "stop": 64000.0, "targets": [66800.0], "risk_cash": 50.0},
        "costs": {"planned_r_after_costs": 1.41, "fee_round_trip": 4.5, "slip_cost_rt": 4.0,
                  "funding_est": 2.0},
        "regime": {"label": "TREND_UP", "secondary": ["LOW_VOL"], "confidence": 0.7},
        "features": {"adx_14": 22.0, "atr_pct_100": 0.9},
        "llm_review": {"recommendation": "TAKE", "agree_with_code": True, "confidence": 0.46,
                       "thesis": "Pullback held the packet entry."},
        "hist_cell": {"n": 14, "mean_r": None, "note": "unproven"},
        "strategy_version_id": "sv_1", "prompt_version_id": "pv_1",
    }


def position(status="OPEN", position_id="pos-1"):
    return {
        "id": position_id, "idea_id": "idea-1", "asset": "BTC", "tf": "1h", "direction": "long",
        "entry": 65013.0, "stop": 64000.0, "size": 0.05, "risk_cash": 50.0, "status": status,
        "exit_px": 66800.0, "exit_reason": "target", "realized_r": 1.42, "pnl_usd": 71.0,
        "fees_usd": 4.5, "funding_usd": 0.3, "mfe_r": 1.8, "mae_r": 0.3, "bars_held": 7,
        "outcome_class": "target_hit", "funding_missing": False,
    }


def dispatcher(ok=True):
    recorder = Recorder(ok)
    return AlertDispatcher(SETTINGS, sender=recorder), recorder, FakeAlertsTable()


# ---------------- what is alertable ----------------

def test_trade_paper_is_alertable():
    assert should_alert_decision("TRADE_PAPER")


@pytest.mark.parametrize("decision", ["NO_TRADE", "WAIT", "GATED_PASS", "DETECTED", ""])
def test_non_trade_paper_decisions_are_never_alertable(decision):
    assert not should_alert_decision(decision)


def test_no_trade_idea_sends_nothing():
    d, recorder, conn = dispatcher()
    assert d.alert_trade_paper(conn, idea(decision="NO_TRADE")) == 0
    assert recorder.sent == []


def test_wait_idea_sends_nothing():
    d, recorder, conn = dispatcher()
    assert d.alert_trade_paper(conn, idea(decision="WAIT")) == 0
    assert recorder.sent == []


def test_llm_vetoed_idea_sends_nothing():
    # An LLM veto is persisted as NO_TRADE by the step 8 resolver.
    d, recorder, conn = dispatcher()
    vetoed = idea(decision="NO_TRADE")
    vetoed["llm_review"] = {"recommendation": "NO_TRADE", "agree_with_code": False}
    assert d.alert_trade_paper(conn, vetoed) == 0
    assert recorder.sent == []


def test_failed_hard_gate_idea_sends_nothing():
    d, recorder, conn = dispatcher()
    gated = idea(decision="NO_TRADE")
    gated["gates"] = {"hard": {"min_r": False}}
    assert d.alert_trade_paper(conn, gated) == 0
    assert recorder.sent == []


# ---------------- delivery and recipients ----------------

def test_trade_paper_alert_goes_to_every_authorized_chat():
    d, recorder, conn = dispatcher()
    assert d.alert_trade_paper(conn, idea()) == 2
    assert sorted(chat for chat, _ in recorder.sent) == [111, 222]


def test_alert_never_goes_to_an_unauthorized_chat():
    d, recorder, conn = dispatcher()
    d.alert_trade_paper(conn, idea())
    assert {chat for chat, _ in recorder.sent} <= set(SETTINGS.telegram_allowed_chat_ids)
    assert 999 not in {chat for chat, _ in recorder.sent}


def test_recipients_come_only_from_the_allowlist():
    d, _, _ = dispatcher()
    assert d.recipients == (111, 222)


def test_bot_token_never_appears_in_an_alert_body():
    d, recorder, conn = dispatcher()
    d.alert_trade_paper(conn, idea())
    assert all(SETTINGS.telegram_bot_token not in text for _, text in recorder.sent)


# ---------------- idempotency ----------------

def test_duplicate_dispatch_sends_once():
    d, recorder, conn = dispatcher()
    assert d.alert_trade_paper(conn, idea()) == 2
    assert d.alert_trade_paper(conn, idea()) == 0
    assert len(recorder.sent) == 2


def test_restart_with_shared_table_does_not_resend():
    conn = FakeAlertsTable()
    before_recorder = Recorder()
    AlertDispatcher(SETTINGS, sender=before_recorder).alert_trade_paper(conn, idea())
    assert len(before_recorder.sent) == 2

    # A fresh dispatcher, as after a worker restart, against the same durable table.
    after_recorder = Recorder()
    after = AlertDispatcher(SETTINGS, sender=after_recorder)
    assert after.alert_trade_paper(conn, idea()) == 0
    assert after_recorder.sent == []


def test_claim_is_recorded_before_send_so_failure_does_not_resend():
    d, recorder, conn = dispatcher(ok=False)
    assert d.alert_trade_paper(conn, idea()) == 0  # sender reported failure
    assert len(conn.rows) == 2                      # but the claim persisted
    assert all(not row["delivered"] for row in conn.rows.values())
    d2 = AlertDispatcher(SETTINGS, sender=Recorder())
    assert d2.alert_trade_paper(conn, idea()) == 0  # never retried into a duplicate


def test_successful_send_marks_delivered():
    d, _, conn = dispatcher()
    d.alert_trade_paper(conn, idea())
    assert all(row["delivered"] for row in conn.rows.values())


def test_distinct_ideas_alert_separately():
    d, recorder, conn = dispatcher()
    d.alert_trade_paper(conn, idea(idea_id="idea-1"))
    d.alert_trade_paper(conn, idea(idea_id="idea-2"))
    assert len(recorder.sent) == 4


def test_dedupe_key_is_per_kind_entity_and_chat():
    assert dedupe_key("trade_paper", "i1", 111) != dedupe_key("trade_paper", "i1", 222)
    assert dedupe_key("trade_paper", "i1", 111) != dedupe_key("paper_close", "i1", 111)
    assert dedupe_key("trade_paper", "i1", 111) == dedupe_key("trade_paper", "i1", 111)


def test_fill_and_close_are_separate_events_for_one_position():
    d, recorder, conn = dispatcher()
    assert d.alert_fill(conn, position()) == 2
    assert d.alert_close(conn, position(status="CLOSED")) == 2
    assert d.alert_fill(conn, position()) == 0
    assert d.alert_close(conn, position(status="CLOSED")) == 0
    assert len(recorder.sent) == 4


# ---------------- message bodies ----------------

def test_trade_paper_alert_has_the_frozen_shape():
    text = formatters.format_trade_paper_alert(idea())
    assert text.startswith("TITLE: PAPER | BTC | 1h | trend_pullback | LONG")
    for label in ("DECISION: TRADE_PAPER", "ENTRY / STOP / T1:", "REGIME:", "COST:",
                  "GATES: all hard passed", "HIST:", "LLM:", "THESIS:", "ID:"):
        assert label in text


def test_trade_paper_alert_reports_costs_in_r():
    text = formatters.format_trade_paper_alert(idea())
    assert "fee 0.09R" in text and "slip 0.08R" in text and "funding 0.04R" in text


def test_trade_paper_alert_labels_thin_history_unproven():
    assert "n=14 meanR=n/a (unproven)" in formatters.format_trade_paper_alert(idea())


def test_fill_alert_states_it_is_hypothetical():
    text = formatters.format_fill_alert(position())
    assert "PAPER FILL" in text and "No exchange order was placed." in text
    assert "65013.0000" in text


def test_close_alert_reports_outcome_fields():
    text = formatters.format_close_alert(position(status="CLOSED"))
    assert "PAPER CLOSE" in text and "target" in text
    assert "REALIZED R: 1.42" in text and "MFE 1.80R" in text and "CLASS: target_hit" in text


def test_close_alert_flags_incomplete_funding():
    record = position(status="CLOSED")
    record["funding_missing"] = True
    assert "(incomplete funding data)" in formatters.format_close_alert(record)


def test_alerts_contain_no_order_or_execution_language():
    bodies = [
        formatters.format_trade_paper_alert(idea()),
        formatters.format_fill_alert(position()),
        formatters.format_close_alert(position(status="CLOSED")),
    ]
    for text in bodies:
        lowered = text.lower()
        assert "order placed" not in lowered
        assert "testnet" not in lowered
        assert "wallet" not in lowered
