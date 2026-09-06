import inspect

import pytest

from agent import learning
from agent.learning import (
    MIN_MEAN_R_DELTA,
    MIN_N_FOR_PROPOSAL,
    MIN_N_PER_BRANCH,
    STATUS_PROPOSED,
    naive_confidence_interval,
    propose_from_outcomes,
    rule_id,
    rule_key,
)


def row(idea_id, realized_r, adx=30.0, setup_id="trend_pullback", regime="TREND_UP"):
    return {"idea_id": idea_id, "setup_id": setup_id, "regime_primary": regime,
            "realized_r": realized_r, "features": {"adx_14": adx}}


def separable_journal(n_high=15, n_low=15, high_r=1.5, low_r=0.1):
    """High-ADX outcomes clearly better than low-ADX ones, on a sufficient sample."""
    rows = [row(f"h{i}", high_r, adx=30.0) for i in range(n_high)]
    rows += [row(f"l{i}", low_r, adx=10.0) for i in range(n_low)]
    return rows


# ---------------- insufficient evidence ----------------

def test_empty_journal_proposes_nothing():
    assert propose_from_outcomes([]) == []


def test_thin_setup_sample_proposes_nothing():
    rows = [row(f"i{i}", 2.0, adx=30.0 if i % 2 else 10.0) for i in range(MIN_N_FOR_PROPOSAL - 1)]
    assert propose_from_outcomes(rows) == []


def test_thin_branch_proposes_nothing_even_with_enough_total_rows():
    # 40 rows total, but only 3 match the predicate.
    rows = [row(f"h{i}", 3.0, adx=30.0) for i in range(3)]
    rows += [row(f"l{i}", 0.0, adx=10.0) for i in range(37)]
    assert all(p.definition["feature_predicate"] != "adx_14_ge_25" for p in propose_from_outcomes(rows))


def test_small_effect_proposes_nothing():
    # Both branches are large enough, but the difference is below the threshold.
    rows = [row(f"h{i}", 0.50, adx=30.0) for i in range(20)]
    rows += [row(f"l{i}", 0.45, adx=10.0) for i in range(20)]
    assert propose_from_outcomes(rows) == []


def test_no_proposal_is_manufactured_just_to_fill_the_table():
    rows = [row(f"i{i}", 0.2, adx=30.0) for i in range(50)]  # no contrast at all
    assert propose_from_outcomes(rows) == []


def test_thresholds_are_explicit_constants():
    assert MIN_N_FOR_PROPOSAL == 30 and MIN_N_PER_BRANCH == 10 and MIN_MEAN_R_DELTA == 0.25


# ---------------- valid proposal ----------------

def test_clear_separation_yields_a_proposal():
    proposals = propose_from_outcomes(separable_journal())
    assert proposals
    assert any(p.definition["feature_predicate"] == "adx_14_ge_25" for p in proposals)


def test_proposal_status_is_always_proposed():
    for proposal in propose_from_outcomes(separable_journal()):
        assert proposal.status == STATUS_PROPOSED
        assert proposal.to_row()["status"] == "proposed"


def test_proposal_carries_required_evidence_fields():
    proposal = propose_from_outcomes(separable_journal())[0]
    for field in ("setup_id", "regime", "feature_predicate", "baseline_n", "baseline_mean_r",
                  "matched_n", "matched_mean_r", "mean_r_delta", "evidence_idea_ids"):
        assert field in proposal.definition
    assert proposal.n > 0 and proposal.validation_period == "all"
    assert proposal.strategy_version_id


def test_proposal_traces_back_to_the_ideas_that_generated_it():
    proposal = propose_from_outcomes(separable_journal())[0]
    evidence = proposal.definition["evidence_idea_ids"]
    assert len(evidence) == proposal.n
    assert all(i.startswith("h") for i in evidence)


def test_proposal_records_its_own_uncertainty():
    proposal = propose_from_outcomes(separable_journal(high_r=1.5, low_r=0.1))[0]
    assert proposal.ci_low is not None and proposal.ci_high is not None
    assert proposal.ci_low <= proposal.mean_r <= proposal.ci_high


def test_proposal_is_marked_as_needing_operator_review():
    proposal = propose_from_outcomes(separable_journal())[0]
    assert "operator review" in proposal.definition["note"]


def test_naive_ci_is_undefined_for_a_single_sample():
    assert naive_confidence_interval([1.0]) == (None, None)


def test_negative_effects_are_also_proposable():
    rows = [row(f"h{i}", -1.0, adx=30.0) for i in range(15)]
    rows += [row(f"l{i}", 0.5, adx=10.0) for i in range(15)]
    proposals = propose_from_outcomes(rows)
    assert proposals and proposals[0].definition["mean_r_delta"] < 0


# ---------------- determinism and duplicates ----------------

def test_proposal_identity_is_deterministic():
    first = propose_from_outcomes(separable_journal())
    second = propose_from_outcomes(separable_journal())
    assert [p.rule_key for p in first] == [p.rule_key for p in second]


def test_rule_key_is_stable_for_identical_inputs():
    args = dict(setup_id="trend_pullback", regime="TREND_UP", predicate="adx_14_ge_25",
                strategy_version_id="sv_1")
    assert rule_key(**args) == rule_key(**args)


def test_rule_key_changes_with_the_evidence_shape():
    base = dict(setup_id="trend_pullback", regime="TREND_UP", predicate="adx_14_ge_25",
                strategy_version_id="sv_1")
    assert rule_key(**base) != rule_key(**{**base, "regime": "RANGE"})
    assert rule_key(**base) != rule_key(**{**base, "predicate": "rsi_14_ge_60"})
    assert rule_key(**base) != rule_key(**{**base, "strategy_version_id": "sv_2"})


def test_rule_id_is_a_stable_uuid_derived_from_the_key():
    key = rule_key(setup_id="s", regime="R", predicate="p", strategy_version_id="sv")
    assert rule_id(key) == rule_id(key)
    assert rule_id(key) != rule_id(key + "x")


def test_reruns_produce_the_same_rule_ids_so_restart_cannot_duplicate():
    first = {rule_id(p.rule_key) for p in propose_from_outcomes(separable_journal())}
    second = {rule_id(p.rule_key) for p in propose_from_outcomes(separable_journal())}
    assert first == second and first


def test_upsert_never_overwrites_an_operator_decision():
    source = inspect.getsource(learning.persist_proposals)
    assert "ON CONFLICT (id) DO UPDATE" in source
    assert "WHERE learned_rules.status = 'proposed'" in source
    assert "status=EXCLUDED.status" not in source


# ---------------- the proposer cannot act ----------------

def test_proposer_never_writes_any_status_but_proposed():
    source = inspect.getsource(learning)
    for forbidden in ("'promoted'", "'rejected'", "'expired'"):
        assert forbidden not in source


def test_proposer_does_not_touch_strategy_or_prompt_versions():
    source = inspect.getsource(learning)
    assert "UPDATE strategy_version" not in source
    assert "INSERT INTO strategy_version" not in source
    assert "prompt_version" not in source


def test_proposer_writes_only_to_learned_rules():
    import re

    source = inspect.getsource(learning)
    # Statement-initiating write verbs only; "DO UPDATE SET" belongs to the
    # learned_rules upsert and is not a separate target.
    targets = re.findall(r"(?:INSERT\s+INTO|DELETE\s+FROM|(?<!DO\s)\bUPDATE)\s+([A-Za-z_][\w.]*)", source)
    assert targets, "expected at least one write statement"
    assert set(targets) == {"learned_rules"}, f"unexpected write targets: {sorted(set(targets))}"


def test_proposer_cannot_place_or_influence_a_trade():
    source = inspect.getsource(learning)
    for forbidden in ("paper_fills", "TRADE_PAPER", "order", "sign", "wallet", "testnet"):
        assert forbidden not in source


def test_proposer_does_not_mutate_config_or_detector_parameters():
    source = inspect.getsource(learning)
    for forbidden in ("risk_fraction", "FROZEN_ASSETS", "FROZEN_TIMEFRAMES", "settings."):
        assert forbidden not in source


def test_candidate_predicate_space_is_a_fixed_code_level_list():
    names = [name for name, _ in learning.CANDIDATE_PREDICATES]
    assert names == sorted(set(names), key=names.index)  # no duplicates
    assert len(names) == 4  # widening the search space requires a code change


def test_run_learning_job_reports_nothing_written_on_thin_evidence(monkeypatch):
    monkeypatch.setattr(learning, "fetch_outcomes_for_learning", lambda conn: [])

    def explode(*_a, **_k):
        raise AssertionError("persist_proposals must not run without proposals")

    monkeypatch.setattr(learning, "persist_proposals", explode)
    assert learning.run_learning_job(object()) == {"outcomes": 0, "proposals": 0, "written": 0}


def test_run_learning_job_persists_when_evidence_supports_it(monkeypatch):
    monkeypatch.setattr(learning, "fetch_outcomes_for_learning", lambda conn: separable_journal())
    captured = {}

    def capture(conn, proposals):
        captured["proposals"] = list(proposals)
        return len(proposals)

    monkeypatch.setattr(learning, "persist_proposals", capture)
    result = learning.run_learning_job(object())
    assert result["proposals"] >= 1 and result["written"] == result["proposals"]
    assert all(p.status == STATUS_PROPOSED for p in captured["proposals"])


def test_learning_query_reads_only_closed_outcomes():
    source = inspect.getsource(learning.fetch_outcomes_for_learning)
    assert "p.status = 'CLOSED'" in source and "p.realized_r IS NOT NULL" in source


def test_nightly_job_is_registered_on_the_existing_scheduler():
    from agent.__main__ import scheduled_for
    from datetime import datetime, timezone

    key = scheduled_for("daily_stats", datetime(2026, 9, 5, 13, 22, tzinfo=timezone.utc))
    assert key.hour == 0 and key.minute == 5 and key.tzinfo is timezone.utc


def test_nightly_job_key_is_stable_within_a_day_so_reruns_are_skipped():
    from agent.__main__ import scheduled_for
    from datetime import datetime, timezone

    morning = scheduled_for("daily_stats", datetime(2026, 9, 5, 1, 0, tzinfo=timezone.utc))
    evening = scheduled_for("daily_stats", datetime(2026, 9, 5, 23, 0, tzinfo=timezone.utc))
    assert morning == evening  # one occurrence per UTC day, claimed once by job_runs
