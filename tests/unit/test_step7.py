from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent.circuit import evaluate_loss_breakers
from agent.telegram import formatters
from agent.telegram.bot import Dispatcher, RateLimiter, parse_command

T = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
SETTINGS = SimpleNamespace(telegram_allowed_chat_ids=(111, 222), paper_equity_usd=10000.0,
                           telegram_bot_token="secret-token-value")


class ExplodingConn:
    """Any DB use in these tests is a bug; the covered commands are DB-free."""

    def __enter__(self):
        raise AssertionError("handler unexpectedly opened a database connection")

    def __exit__(self, *_a):
        return False


def update(chat_id, text, update_id=1):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


def dispatcher(conn_factory=ExplodingConn, limiter=None):
    return Dispatcher(SETTINGS, conn_factory=conn_factory, rate_limiter=limiter)


# ---------------- access control ----------------

def test_unauthorized_chat_gets_no_reply_at_all():
    assert dispatcher().handle_update(update(999, "/status")) is None


def test_unauthorized_chat_cannot_halt():
    called = []
    d = dispatcher(conn_factory=lambda: called.append("used") or ExplodingConn())
    assert d.handle_update(update(999, "/halt")) is None
    assert called == []


def test_authorized_chat_is_recognized():
    d = dispatcher()
    assert d.is_authorized(111) and d.is_authorized(222)
    assert not d.is_authorized(333)
    assert not d.is_authorized(None)


def test_missing_chat_id_is_ignored():
    assert dispatcher().handle_update({"message": {"text": "/status"}}) is None


def test_non_command_text_gets_no_reply():
    assert dispatcher().handle_update(update(111, "hello there")) is None


# ---------------- command parsing ----------------

def test_parse_command_basic():
    assert parse_command("/ideas 5") == ("/ideas", ["5"])


def test_parse_command_strips_bot_suffix():
    assert parse_command("/status@traduhh_bot") == ("/status", [])


def test_parse_command_rejects_plain_text():
    assert parse_command("just talking") is None
    assert parse_command("") is None
    assert parse_command(None) is None


# ---------------- rate limiting ----------------

def test_rate_limiter_allows_ten_then_blocks():
    clock = [T]
    limiter = RateLimiter(clock=lambda: clock[0])
    assert all(limiter.allow(111) for _ in range(10))
    assert limiter.allow(111) is False


def test_rate_limiter_window_slides():
    clock = [T]
    limiter = RateLimiter(clock=lambda: clock[0])
    for _ in range(10):
        limiter.allow(111)
    assert limiter.allow(111) is False
    clock[0] = T + timedelta(seconds=61)
    assert limiter.allow(111) is True


def test_rate_limiter_is_per_chat():
    clock = [T]
    limiter = RateLimiter(clock=lambda: clock[0])
    for _ in range(10):
        limiter.allow(111)
    assert limiter.allow(111) is False
    assert limiter.allow(222) is True


def test_dispatcher_rate_limit_reply_mentions_limit():
    clock = [T]
    d = dispatcher(limiter=RateLimiter(clock=lambda: clock[0]))
    replies = [d.handle_update(update(111, "/help")) for _ in range(11)]
    assert "Rate limit" in replies[-1]


def test_rate_limited_command_never_opens_a_connection():
    clock = [T]
    opens = []

    class CountingConn:
        def __enter__(self):
            opens.append(1)
            return self

        def __exit__(self, *_a):
            return False

    d = Dispatcher(SETTINGS, conn_factory=CountingConn, rate_limiter=RateLimiter(limit=1, clock=lambda: clock[0]))
    d.handle_update(update(111, "/status"))
    assert opens == [1]
    assert "Rate limit" in d.handle_update(update(111, "/status"))
    assert opens == [1]  # the blocked command never reached the handler


# ---------------- routing ----------------

def test_unknown_command_is_reported_without_touching_the_database():
    # ExplodingConn proves the handler lookup happens before any connection.
    assert "Unknown command" in dispatcher().handle_update(update(111, "/launchrocket"))


def test_handler_exception_is_contained():
    def boom():
        raise RuntimeError("db exploded")

    d = dispatcher(conn_factory=boom)
    reply = d.handle_update(update(111, "/status"))
    assert reply.startswith("Command failed")


def test_no_command_can_change_strategy_parameters():
    from agent.telegram.bot import HANDLERS

    forbidden = ("risk", "param", "universe", "asset", "setup", "force", "timeframe")
    assert not [name for name in HANDLERS if any(word in name for word in forbidden)]


def test_bot_token_never_appears_in_any_reply():
    d = dispatcher()
    reply = d.handle_update(update(111, "/launchrocket"))
    assert SETTINGS.telegram_bot_token not in (reply or "")


# ---------------- formatters (pure) ----------------

def test_format_help_lists_frozen_commands_and_mode():
    text = formatters.format_help("paper")
    for command in ("/status", "/health", "/regime", "/ideas", "/idea", "/positions",
                    "/stats", "/journal", "/version", "/halt", "/resume", "/mode", "/verbose"):
        assert command in text
    assert "MODE: paper" in text


def test_format_status_renders_all_fields():
    text = formatters.format_status({
        "mode": "paper", "equity": 10250.0, "day_pnl_usd": 250.0, "day_pnl_pct": 0.025,
        "open_positions": 2, "last_scan_ts": "2026-09-05T12:00:00+00:00",
        "integrity_ok": True, "halt_reasons": [],
    })
    assert "MODE: paper" in text and "10250.00" in text and "OPEN PAPER: 2" in text
    assert "INTEGRITY: ok" in text and "HALT REASONS: none" in text


def test_format_status_shows_halt_reasons():
    text = formatters.format_status({"mode": "halted", "equity": 1.0, "day_pnl_usd": 0,
                                     "day_pnl_pct": 0, "open_positions": 0, "last_scan_ts": None,
                                     "integrity_ok": False, "halt_reasons": ["daily_loss"]})
    assert "MODE: halted" in text and "daily_loss" in text and "INTEGRITY: FAIL" in text


def test_format_mode_refuses_testnet():
    text = formatters.format_mode("paper")
    assert "testnet_exec" in text and "not implemented" in text


def test_format_ideas_empty_and_populated():
    assert "No ideas" in formatters.format_ideas([])
    text = formatters.format_ideas([{"id": "abc", "created_at": "t", "asset": "BTC", "timeframe": "1h",
                                     "setup_id": "trend_pullback", "direction": "long", "decision": "NO_TRADE"}])
    assert "NO_TRADE" in text and "trend_pullback" in text


def test_format_idea_missing_returns_not_found():
    assert formatters.format_idea(None) == "Idea not found."


def test_format_idea_reports_failed_hard_gates():
    text = formatters.format_idea({
        "id": "abc", "asset": "BTC", "timeframe": "1h", "setup_id": "trend_pullback", "direction": "long",
        "decision": "NO_TRADE", "decision_reason": ["min_r"],
        "geometry": {"entry": 100, "stop": 98, "targets": [103]},
        "costs": {"planned_r_after_costs": 0.9},
        "gates": {"hard": {"min_r": False, "data_valid": True}},
        "confidence": 0.4, "strategy_version_id": "sv_x", "prompt_version_id": None,
    })
    assert "HARD GATES FAILED: min_r" in text and "NO_TRADE" in text


def test_format_idea_includes_outcome_when_closed():
    text = formatters.format_idea({
        "id": "abc", "asset": "BTC", "timeframe": "1h", "setup_id": "s", "direction": "long",
        "decision": "TRADE_PAPER", "decision_reason": [], "geometry": {"entry": 1, "stop": 0.5, "targets": [2]},
        "costs": {}, "gates": {"hard": {}}, "confidence": 0.5,
        "strategy_version_id": "sv", "prompt_version_id": "pv",
        "outcome": {"exit_reason": "target", "realized_r": 1.42, "mfe_r": 1.8, "mae_r": 0.3},
    })
    assert "OUTCOME: target" in text and "1.42" in text


def test_format_positions_empty_and_populated():
    assert "No open paper positions" in formatters.format_positions([])
    text = formatters.format_positions([{"id": "p1", "asset": "ETH", "tf": "1h", "direction": "short",
                                         "entry": 2000.0, "stop": 2050.0, "mfe_r": 0.4, "mae_r": 0.2,
                                         "bars_held": 3}])
    assert "ETH" in text and "MFE 0.40R" in text


def test_format_health_reports_missing_as_never():
    text = formatters.format_health({"db_ok": True, "hl_last_success": None, "hl_down": False,
                                     "llm_last_success": None, "flags": []})
    assert "DB: ok" in text and "never" in text and "STALE FLAGS: none" in text


def test_format_regime_and_journal_empty_states():
    assert "No regime snapshots" in formatters.format_regime([])
    assert "Journal is empty" in formatters.format_journal([])


def test_format_version_reports_both_version_ids():
    text = formatters.format_version({"strategy_version_id": "sv_1", "prompt_version_id": "pv_1",
                                      "code_git_sha": "abc123", "mode": "paper"})
    assert "sv_1" in text and "pv_1" in text and "abc123" in text


# ---------------- circuit breakers ----------------

def test_loss_breakers_trip_at_frozen_thresholds():
    assert evaluate_loss_breakers(day_pnl=-200, week_pnl=0, equity=10000).daily_tripped
    assert evaluate_loss_breakers(day_pnl=0, week_pnl=-500, equity=10000).weekly_tripped


def test_loss_breakers_do_not_trip_just_below_threshold():
    breakers = evaluate_loss_breakers(day_pnl=-199, week_pnl=-499, equity=10000)
    assert not breakers.tripped and breakers.reasons == []


def test_loss_breakers_report_reasons():
    breakers = evaluate_loss_breakers(day_pnl=-300, week_pnl=-800, equity=10000)
    assert breakers.reasons == ["daily_loss", "weekly_loss"]


def test_loss_breakers_reject_nonpositive_equity():
    with pytest.raises(ValueError):
        evaluate_loss_breakers(day_pnl=0, week_pnl=0, equity=0)
