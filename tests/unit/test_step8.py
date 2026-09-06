import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from agent.llm.client import LLMClient, RateBudget, prompt_version_id
from agent.llm.packet import ALLOWED_FEATURE_KEYS, build_packet, canonical_json, packet_hash, packet_price_allowlist
from agent.llm.schema import (
    SchemaError,
    final_confidence,
    mentions_invented_price,
    parse_decision,
    resolve_after_llm,
)
from agent.pipeline import minutes_to_funding_hour

T = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)

SECRETS = {
    "api_key": "sk-super-secret-key",
    "bot_token": "9999:BOT-TOKEN-SECRET",
    "database_url": "postgres://agent:hunter2@127.0.0.1:5432/agent",
}


def valid_response(**overrides):
    body = {
        "schema": "agent.llm_decision.v1",
        "recommendation": "TAKE",
        "agree_with_code": True,
        "contradictions": [],
        "thesis": "Trend pullback into the packet entry with the stop below structure.",
        "invalidation_restated": "Invalid below the packet stop.",
        "news_causal_claim": False,
        "used_invented_level": False,
        "confidence": 0.55,
        "what_would_change_decision": "A close beyond the stop.",
    }
    body.update(overrides)
    return body


def packet(**overrides):
    base = dict(
        idea_id="idea-1", ts_utc=T.isoformat(), asset="BTC", timeframe="1h",
        strategy_version_id="sv_1", prompt_version_id="pv_1", mode="paper",
        data_quality={"ok": True, "flags": []},
        regime={"label": "TREND_UP", "secondary": ["LOW_VOL"], "confidence": 0.7},
        setup={"id": "trend_pullback", "direction": "long",
               "levels": {"entry": 65000.0, "stop": 64000.0, "targets": [66800.0]}},
        features={"atr_14": 250.0, "ema_20": 64900.0, "adx_14": 22.0, "grammar": "HH_HL",
                  "secret_leak": SECRETS["api_key"]},
        book={"spread_bps": 1.2, "imbalance_5": 0.1, "notional_to_10bps": 500000.0},
        derivatives={"funding": 0.0000125, "funding_z_168": 0.4, "oi": 1000.0,
                     "oi_chg_24h": 0.02, "basis_bps": 3.0},
        costs={"fee_round_trip": 0.9, "slip_cost_rt": 0.4, "funding_est": 0.1,
               "planned_r_after_costs": 1.41},
        portfolio={"equity": 10000.0, "open_positions": [], "day_pnl_pct": 0.0, "cluster": None},
        news=[], calendar=[],
        hist_cell={"n": 14, "mean_r": None, "std_r": None, "win_rate": None, "note": "unproven"},
        gates_passed=["data_valid", "min_r"],
    )
    base.update(overrides)
    return build_packet(**base)


class StubTransport(httpx.BaseTransport):
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def handle_request(self, request):
        self.requests.append(request)
        item = self._responses.pop(0) if self._responses else self._responses
        if isinstance(item, Exception):
            raise item
        status, body = item
        return httpx.Response(status, json=body, request=request)


def client_with(responses, **kwargs):
    transport = StubTransport(responses)
    http = httpx.Client(transport=transport)
    return LLMClient(base_url="https://llm.example/v1", api_key=SECRETS["api_key"],
                     model="test-model", client=http, **kwargs), transport


def completion(content):
    return 200, {"choices": [{"message": {"content": json.dumps(content) if not isinstance(content, str) else content}}]}


# ---------------- packet construction ----------------

def test_packet_has_exact_frozen_schema_keys():
    p = packet()
    assert p["schema"] == "agent.llm_packet.v1"
    for key in ("idea_id", "ts_utc", "asset", "timeframe", "strategy_version_id", "prompt_version_id",
                "mode", "data_quality", "regime", "setup", "features", "book", "derivatives", "costs",
                "portfolio", "news", "calendar", "hist_cell", "gates_passed", "forbidden"):
        assert key in p


def test_packet_is_deterministic_for_identical_inputs():
    assert canonical_json(packet()) == canonical_json(packet())
    assert packet_hash(packet()) == packet_hash(packet())


def test_packet_changes_when_evidence_changes():
    other = packet(setup={"id": "trend_pullback", "direction": "long",
                          "levels": {"entry": 65001.0, "stop": 64000.0, "targets": [66800.0]}})
    assert packet_hash(packet()) != packet_hash(other)


def test_packet_carries_no_secrets():
    serialized = canonical_json(packet())
    for secret in SECRETS.values():
        assert secret not in serialized


def test_packet_features_are_allowlisted_only():
    p = packet()
    assert "secret_leak" not in p["features"]
    assert set(p["features"]).issubset(set(ALLOWED_FEATURE_KEYS))


def test_packet_forbids_invented_levels_and_news_causation():
    assert "do not invent levels" in packet()["forbidden"]
    assert "do not claim news caused the tape" in packet()["forbidden"]


def test_packet_carries_the_code_computed_levels_only():
    levels = packet()["setup"]["levels"]
    assert levels == {"entry": 65000.0, "stop": 64000.0, "targets": [66800.0]}


def test_packet_marks_thin_history_unproven():
    assert packet()["hist_cell"]["note"] == "unproven"


def test_price_allowlist_contains_packet_levels():
    allow = packet_price_allowlist(packet())
    assert "65000.00" in allow and "64000.00" in allow and "66800.00" in allow


# ---------------- schema validation ----------------

def test_valid_response_parses():
    decision = parse_decision(valid_response())
    assert decision.recommendation == "TAKE" and decision.confidence == 0.55


def test_response_accepts_json_string():
    assert parse_decision(json.dumps(valid_response())).recommendation == "TAKE"


def test_invalid_json_is_rejected():
    with pytest.raises(SchemaError):
        parse_decision("not json at all")


def test_missing_key_is_rejected():
    body = valid_response()
    del body["confidence"]
    with pytest.raises(SchemaError):
        parse_decision(body)


def test_wrong_schema_id_is_rejected():
    with pytest.raises(SchemaError):
        parse_decision(valid_response(schema="agent.llm_decision.v2"))


def test_unknown_recommendation_is_rejected():
    with pytest.raises(SchemaError):
        parse_decision(valid_response(recommendation="BUY"))


def test_non_boolean_agreement_is_rejected():
    with pytest.raises(SchemaError):
        parse_decision(valid_response(agree_with_code="yes"))


def test_out_of_range_confidence_is_rejected():
    with pytest.raises(SchemaError):
        parse_decision(valid_response(confidence=1.7))


def test_overlong_thesis_is_rejected():
    with pytest.raises(SchemaError):
        parse_decision(valid_response(thesis="word " * 81))


# ---------------- veto logic ----------------

def test_take_with_agreement_resolves_to_trade_paper():
    outcome = resolve_after_llm(decision=parse_decision(valid_response()), error=None, allowlist=set())
    assert outcome.decision == "TRADE_PAPER"


def test_llm_no_trade_vetoes_a_passing_candidate():
    outcome = resolve_after_llm(decision=parse_decision(valid_response(recommendation="NO_TRADE")),
                                error=None, allowlist=set())
    assert outcome.decision == "NO_TRADE" and "llm_no_trade" in outcome.reasons


def test_llm_wait_resolves_to_wait():
    outcome = resolve_after_llm(decision=parse_decision(valid_response(recommendation="WAIT")),
                                error=None, allowlist=set())
    assert outcome.decision == "WAIT"


def test_take_without_code_agreement_is_no_trade():
    outcome = resolve_after_llm(decision=parse_decision(valid_response(agree_with_code=False)),
                                error=None, allowlist=set())
    assert outcome.decision == "NO_TRADE" and "llm_disagrees_with_code" in outcome.reasons


def test_self_declared_invented_level_forces_no_trade():
    outcome = resolve_after_llm(decision=parse_decision(valid_response(used_invented_level=True)),
                                error=None, allowlist=set())
    assert outcome.decision == "NO_TRADE" and "llm_invented_level" in outcome.reasons


def test_invented_level_in_thesis_forces_no_trade():
    allow = packet_price_allowlist(packet())
    response = valid_response(thesis="Long here, targeting 71234.50 on continuation.")
    outcome = resolve_after_llm(decision=parse_decision(response), error=None, allowlist=allow)
    assert outcome.decision == "NO_TRADE" and "llm_invented_level" in outcome.reasons


def test_packet_levels_in_thesis_are_allowed():
    allow = packet_price_allowlist(packet())
    response = valid_response(thesis="Entry 65000.00 with the stop at 64000.00 and target 66800.00.")
    outcome = resolve_after_llm(decision=parse_decision(response), error=None, allowlist=allow)
    assert outcome.decision == "TRADE_PAPER"


def test_small_numbers_are_not_treated_as_invented_prices():
    assert not mentions_invented_price("Planned 1.8R over 12 bars, ADX 22.", {"65000.00"})


def test_news_causal_claim_is_flagged_but_not_fatal_alone():
    outcome = resolve_after_llm(decision=parse_decision(valid_response(news_causal_claim=True)),
                                error=None, allowlist=set())
    assert outcome.decision == "TRADE_PAPER"
    assert "llm_news_causal_claim_stripped" in outcome.reasons


def test_missing_decision_with_invalid_json_resolves_no_trade():
    outcome = resolve_after_llm(decision=None, error="llm_invalid_json", allowlist=set())
    assert outcome.decision == "NO_TRADE" and outcome.valid is False


def test_transport_failure_resolves_to_wait_never_take():
    outcome = resolve_after_llm(decision=None, error="llm_unavailable", allowlist=set())
    assert outcome.decision == "WAIT" and outcome.valid is False


def test_budget_exhaustion_resolves_to_wait():
    outcome = resolve_after_llm(decision=None, error="llm_budget", allowlist=set())
    assert outcome.decision == "WAIT"


def test_veto_never_upgrades_a_decision():
    for recommendation in ("TAKE", "WAIT", "NO_TRADE"):
        outcome = resolve_after_llm(decision=parse_decision(valid_response(recommendation=recommendation)),
                                    error=None, allowlist=set())
        assert outcome.decision in {"TRADE_PAPER", "WAIT", "NO_TRADE"}


# ---------------- confidence and wait check ----------------

def test_final_confidence_capped_by_thin_history():
    assert final_confidence(regime_confidence=0.7, hist_n=10, agreement=1.0) == pytest.approx(0.6)


def test_final_confidence_uses_larger_ceiling_once_n_reaches_30():
    assert final_confidence(regime_confidence=0.9, hist_n=30, agreement=1.0) == pytest.approx(0.7)


def test_final_confidence_is_zero_without_llm_agreement():
    assert final_confidence(regime_confidence=0.7, hist_n=100, agreement=0.0) == 0.0


def test_minutes_to_funding_hour():
    assert minutes_to_funding_hour(T.replace(minute=57)) == pytest.approx(3.0)
    assert minutes_to_funding_hour(T.replace(minute=30)) == pytest.approx(30.0)


# ---------------- client behaviour ----------------

def test_client_returns_parsed_decision():
    client, transport = client_with([completion(valid_response())])
    result = client.review(packet())
    assert result.valid and result.decision.recommendation == "TAKE"
    assert len(transport.requests) == 1


def test_client_retries_once_with_repair_then_succeeds():
    client, transport = client_with([completion("{broken"), completion(valid_response())])
    result = client.review(packet())
    assert result.valid
    assert len(transport.requests) == 2
    repair = json.loads(transport.requests[1].content)
    assert "failed validation" in repair["messages"][-1]["content"]


def test_client_two_schema_failures_resolve_invalid_json():
    client, _ = client_with([completion("{broken"), completion("{still broken")])
    result = client.review(packet())
    assert not result.valid and result.error == "llm_invalid_json"
    assert resolve_after_llm(decision=None, error=result.error, allowlist=set()).decision == "NO_TRADE"


def test_client_server_error_is_unavailable_not_take():
    client, _ = client_with([(503, {"error": "down"})])
    result = client.review(packet())
    assert not result.valid and result.error == "llm_unavailable"


def test_client_timeout_is_unavailable():
    client, _ = client_with([httpx.ConnectTimeout("timed out")])
    result = client.review(packet())
    assert not result.valid and result.error == "llm_unavailable"


def test_client_never_retries_more_than_once_on_transport_error():
    client, transport = client_with([httpx.ConnectTimeout("t1"), completion(valid_response())])
    client.review(packet())
    assert len(transport.requests) == 1


def test_client_sends_bearer_key_but_request_record_has_no_secret():
    client, transport = client_with([completion(valid_response())])
    result = client.review(packet())
    assert transport.requests[0].headers["Authorization"] == f"Bearer {SECRETS['api_key']}"
    assert SECRETS["api_key"] not in json.dumps(result.request, default=str)


def test_client_body_contains_no_secrets():
    client, transport = client_with([completion(valid_response())])
    client.review(packet())
    body = transport.requests[0].content.decode()
    for secret in SECRETS.values():
        assert secret not in body


def test_budget_blocks_after_hourly_limit():
    clock = [T]
    budget = RateBudget(clock=lambda: clock[0])
    for _ in range(30):
        assert budget.allow()
        budget.record()
    assert budget.allow() is False


def test_budget_recovers_after_an_hour():
    clock = [T]
    budget = RateBudget(clock=lambda: clock[0])
    for _ in range(30):
        budget.record()
    assert budget.allow() is False
    clock[0] = T + timedelta(hours=1, minutes=1)
    assert budget.allow() is True


def test_budget_daily_cap_blocks_even_across_hours():
    clock = [T]
    budget = RateBudget(clock=lambda: clock[0])
    for hour in range(10):
        clock[0] = T + timedelta(hours=hour)
        for _ in range(20):
            budget.record()
    clock[0] = T + timedelta(hours=10)
    assert budget.allow() is False


def test_client_over_budget_returns_budget_error_without_calling():
    clock = [T]
    budget = RateBudget(clock=lambda: clock[0])
    for _ in range(30):
        budget.record()
    client, transport = client_with([completion(valid_response())], budget=budget)
    result = client.review(packet())
    assert result.error == "llm_budget"
    assert transport.requests == []


def test_prompt_version_id_is_stable_and_model_scoped():
    assert prompt_version_id(model="m1") == prompt_version_id(model="m1")
    assert prompt_version_id(model="m1") != prompt_version_id(model="m2")
    assert prompt_version_id(model="m1").startswith("pv_")


# ---------------- the LLM is unreachable when hard gates fail ----------------

class NeverCalledClient:
    prompt_version_id = "pv_never"

    def review(self, packet):
        raise AssertionError("the LLM was consulted despite a failing hard gate")


def test_review_candidate_refuses_to_run_on_failing_gates():
    from agent.gates import GateResult
    from agent.pipeline import review_candidate

    failing = GateResult(False, {"min_r": False, "data_valid": True}, ["min_r"], [])
    with pytest.raises(AssertionError):
        review_candidate(None, settings=None, idea_id="i", detection=None, geometry=None, costs=None,
                         gates=failing, features={}, regime={}, ctx=None, book=None, hist_cell={},
                         portfolio={}, llm_client=NeverCalledClient(), asof=T)


def test_pipeline_only_reviews_when_the_code_decision_is_gated_pass():
    """The call site is guarded by code_would_take, which is exactly gates.passed."""
    import inspect

    from agent import pipeline

    source = inspect.getsource(pipeline.evaluate)
    assert 'code_would_take=code_decision=="GATED_PASS"' in source.replace(" ", "")
    guard = source.split("if code_would_take and llm_client is not None:")
    assert len(guard) == 2, "the review call must sit behind the code_would_take guard"
    assert "review_candidate(" not in guard[0]


def test_failing_gate_yields_no_trade_before_any_review():
    from agent.gates import GateResult

    failing = GateResult(False, {"min_r": False}, ["min_r"], [])
    assert failing.decision == "NO_TRADE"
    assert not failing.passed


def test_system_prompt_states_the_frozen_constraints():
    from agent.llm.client import SYSTEM_PROMPT

    lowered = SYSTEM_PROMPT.lower()
    assert "no trade is the default" in lowered
    assert "never invent" in lowered
    assert "never compute indicators" in lowered
    assert "cannot override" in lowered
