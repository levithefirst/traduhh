"""Cross-step invariants for the full Part 10 decision flow.

DATA VALID -> REGIME -> SETUPS -> HIST CELL -> VOL -> LIQUIDITY -> FUNDING/OI
-> NEWS/MACRO -> CONFLICT -> ENTRY -> INVALIDATION -> R AFTER COSTS
-> RISK GATES -> LLM -> CONFIDENCE -> WAIT -> FINAL -> TRADE_PAPER
-> PAPER MONITOR -> OUTCOME -> STATS

These tests pin the rules that must hold no matter which step changes.
"""

import inspect
from datetime import datetime, timezone

import pytest

from agent import learning, monitor, pipeline, stats
from agent.gates import HARD_GATES, GateResult
from agent.llm.schema import resolve_after_llm
from agent.telegram.alerts import should_alert_decision

T = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


# 1. Any hard gate failure is NO_TRADE.

@pytest.mark.parametrize("failing_gate", HARD_GATES)
def test_any_single_hard_gate_failure_is_no_trade(failing_gate):
    hard = {name: True for name in HARD_GATES}
    hard[failing_gate] = False
    result = GateResult(all(hard.values()), hard, [failing_gate], [])
    assert result.decision == "NO_TRADE"
    assert result.passed is False


def test_all_gates_passing_is_only_gated_pass_not_a_trade():
    hard = {name: True for name in HARD_GATES}
    assert GateResult(True, hard, [], []).decision == "GATED_PASS"


# 2 & 3. The LLM is never called on a failing gate and cannot override one.

def test_llm_is_unreachable_without_a_gated_pass():
    source = inspect.getsource(pipeline.evaluate)
    assert "if code_would_take and llm_client is not None:" in source
    before_guard = source.split("if code_would_take and llm_client is not None:")[0]
    assert "review_candidate(" not in before_guard


def test_review_candidate_refuses_failing_gates():
    failing = GateResult(False, {"min_r": False}, ["min_r"], [])
    with pytest.raises(AssertionError):
        pipeline.review_candidate(None, settings=None, idea_id="i", detection=None, geometry=None,
                                  costs=None, gates=failing, features={}, regime={}, ctx=None,
                                  book=None, hist_cell={}, portfolio={}, llm_client=None, asof=T)


def test_llm_take_cannot_resurrect_a_failed_gate():
    """Even a perfect TAKE only matters after the deterministic pass."""
    hard = {name: True for name in HARD_GATES}
    hard["min_r"] = False
    assert GateResult(False, hard, ["min_r"], []).decision == "NO_TRADE"


# 4 & 5. Invented levels and LLM failures resolve safely.

def test_invented_level_never_reaches_trade_paper():
    from agent.llm.schema import parse_decision

    body = {"schema": "agent.llm_decision.v1", "recommendation": "TAKE", "agree_with_code": True,
            "contradictions": [], "thesis": "target 99999.00", "invalidation_restated": "stop",
            "news_causal_claim": False, "used_invented_level": False, "confidence": 0.9,
            "what_would_change_decision": ""}
    outcome = resolve_after_llm(decision=parse_decision(body), error=None, allowlist={"65000.00"})
    assert outcome.decision == "NO_TRADE"


@pytest.mark.parametrize("error,expected", [
    ("llm_invalid_json", "NO_TRADE"),
    ("llm_unavailable", "WAIT"),
    ("llm_budget", "WAIT"),
])
def test_llm_failure_modes_default_safely(error, expected):
    assert resolve_after_llm(decision=None, error=error, allowlist=set()).decision == expected


def test_no_llm_outcome_can_be_trade_paper_without_take_and_agreement():
    from agent.llm.schema import parse_decision

    for recommendation, agree in [("NO_TRADE", True), ("WAIT", True), ("TAKE", False)]:
        body = {"schema": "agent.llm_decision.v1", "recommendation": recommendation,
                "agree_with_code": agree, "contradictions": [], "thesis": "ok",
                "invalidation_restated": "stop", "news_causal_claim": False,
                "used_invented_level": False, "confidence": 0.9, "what_would_change_decision": ""}
        assert resolve_after_llm(decision=parse_decision(body), error=None,
                                 allowlist=set()).decision != "TRADE_PAPER"


# 6. NO_TRADE is a stored, first-class outcome and is never alerted.

def test_no_trade_is_persisted_not_discarded():
    source = inspect.getsource(pipeline.evaluate)
    assert "_persist_idea(" in source
    persist_call = source.split("_persist_idea(")[1]
    assert "decision=decision" in persist_call  # whatever it is, including NO_TRADE


@pytest.mark.parametrize("decision", ["NO_TRADE", "WAIT", "GATED_PASS"])
def test_only_trade_paper_is_announced(decision):
    assert not should_alert_decision(decision)
    assert should_alert_decision("TRADE_PAPER")


# 7, 8, 9. Paper only. No execution anywhere in the tree.

def test_no_module_contains_order_placement_or_signing():
    import agent.circuit, agent.costs, agent.gates, agent.geometry, agent.outcomes, agent.paper
    import agent.llm.client, agent.telegram.alerts, agent.telegram.bot

    modules = [pipeline, monitor, stats, learning, agent.paper, agent.outcomes, agent.circuit,
               agent.llm.client, agent.telegram.alerts, agent.telegram.bot]
    banned = ("private_key", "mnemonic", "sign_order", "place_order", "exchange_order",
              "testnet_exec", "mainnet_exec")
    for module in modules:
        source = inspect.getsource(module).lower()
        for term in banned:
            assert term not in source, f"{module.__name__} mentions {term}"


def test_paper_fills_are_hypothetical_by_construction():
    import agent.paper

    source = inspect.getsource(agent.paper)
    assert "hypothetical" in source.lower()
    assert "httpx" not in source  # the paper layer talks to no venue at all


# 10. The proposer cannot alter live strategy behaviour.

def test_proposer_is_isolated_from_the_decision_path():
    decision_modules = [pipeline, monitor]
    for module in decision_modules:
        source = inspect.getsource(module)
        assert "learned_rules" not in source
        assert "import agent.learning" not in source and "from agent.learning" not in source


def test_learned_rules_never_feed_gates_or_geometry():
    import agent.gates, agent.geometry

    for module in (agent.gates, agent.geometry):
        assert "learned_rule" not in inspect.getsource(module)


# 11. Restart safety across every durable artifact.

def test_ideas_are_keyed_idempotently():
    source = inspect.getsource(pipeline._persist_idea)
    assert "ON CONFLICT (asset,timeframe,setup_id,bar_open_time,strategy_version_id)" in source


def test_positions_and_fills_are_keyed_idempotently():
    import agent.paper

    source = inspect.getsource(agent.paper)
    assert "ON CONFLICT (idea_id) DO NOTHING" in source
    assert "ON CONFLICT (position_id, kind) DO NOTHING" in source


def test_alerts_are_claimed_before_send():
    import agent.telegram.alerts as alerts

    source = inspect.getsource(alerts.claim_alert)
    assert "ON CONFLICT (dedupe_key) DO NOTHING" in source
    broadcast = inspect.getsource(alerts.AlertDispatcher._broadcast)
    assert broadcast.index("claim_alert(") < broadcast.index("self._sender(")


def test_proposals_are_keyed_idempotently():
    assert "ON CONFLICT (id) DO UPDATE" in inspect.getsource(learning.persist_proposals)


def test_jobs_are_claimed_on_a_deterministic_occurrence_key():
    from agent.__main__ import scheduled_for

    for job in ("ctx_poll", "integrity", "candle_1h", "monitor_open", "equity_snap", "daily_stats"):
        assert scheduled_for(job, T) == scheduled_for(job, T)


# 12. No future information leaks backward.

def test_monitor_walks_only_supplied_closed_bars():
    result = monitor.walk_position(direction="long", entry=100, stop=98, target=103,
                                   bars=[{"open_time": T, "close_time": T, "h": 104, "l": 99.5, "c": 103.5},
                                         {"open_time": T, "close_time": T, "h": 100, "l": 90, "c": 95}],
                                   hold_bars_limit=24, halted=False)
    assert result.exit_reason == "target" and result.bars_held == 1
    assert result.mae_px == 99.5  # the later bar is never observed


def test_monitor_query_excludes_the_forming_bar():
    source = inspect.getsource(monitor._bars_since_entry)
    assert "close_time <= %s" in source and "open_time > %s" in source


def test_stats_read_only_closed_realized_outcomes():
    source = inspect.getsource(stats.fetch_closed_outcomes)
    assert "p.status = 'CLOSED'" in source and "p.realized_r IS NOT NULL" in source


def test_learning_reads_only_closed_realized_outcomes():
    source = inspect.getsource(learning.fetch_outcomes_for_learning)
    assert "p.status = 'CLOSED'" in source and "p.realized_r IS NOT NULL" in source


# Flow ordering: the decision sequence itself.

def test_confidence_floor_and_wait_check_run_after_the_llm():
    source = inspect.getsource(pipeline.review_candidate)
    llm_at = source.index("llm_client.review(")
    assert llm_at < source.index("confidence_below_floor")
    assert llm_at < source.index("funding_hour_imminent")


def test_final_decision_vocabulary_is_closed():
    source = inspect.getsource(pipeline.review_candidate)
    for token in ('"TRADE_PAPER"', '"WAIT"', '"NO_TRADE"'):
        assert token in source
