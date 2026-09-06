import inspect

import pytest

from agent import stats
from agent.stats import (
    ELIGIBLE,
    TENTATIVE,
    UNPROVEN,
    build_report,
    compute_cell,
    format_report,
    max_drawdown_r,
    sample_label,
    split_by_llm,
)


def outcome(setup_id="trend_pullback", realized_r=1.0, llm_involved=False, mfe_r=1.5, mae_r=0.4, bars=6):
    return {"setup_id": setup_id, "asset": "BTC", "timeframe": "1h", "llm_involved": llm_involved,
            "realized_r": realized_r, "mfe_r": mfe_r, "mae_r": mae_r, "bars_held": bars,
            "outcome_class": "target_hit" if realized_r > 0 else "stop_hit", "closed_at": None}


# ---------------- empty dataset ----------------

def test_empty_cell_reports_zero_not_a_number():
    cell = compute_cell([])
    assert cell.n == 0
    assert cell.mean_r is None and cell.profit_factor is None and cell.max_dd_r is None
    assert cell.label == UNPROVEN and cell.is_edge is False


def test_empty_report_says_nothing_to_report():
    text = format_report(build_report([]))
    assert "No closed paper outcomes" in text
    assert "edge" not in text.lower() or "NOT AN EDGE" not in text


def test_empty_report_for_a_named_setup_mentions_that_setup():
    text = format_report(build_report([], setup_id="sweep_reclaim"), setup_id="sweep_reclaim")
    assert "sweep_reclaim" in text


# ---------------- sample-size honesty ----------------

def test_sample_label_thresholds_are_frozen():
    assert sample_label(0) == UNPROVEN
    assert sample_label(29) == UNPROVEN
    assert sample_label(30) == TENTATIVE
    assert sample_label(79) == TENTATIVE
    assert sample_label(80) == ELIGIBLE


def test_small_positive_sample_is_never_an_edge():
    cell = compute_cell([3.0, 2.5, 4.0])  # spectacular mean R, tiny n
    assert cell.mean_r > 2
    assert cell.label == UNPROVEN
    assert cell.is_edge is False


def test_tentative_sample_is_still_not_an_edge():
    cell = compute_cell([1.0] * 50)
    assert cell.label == TENTATIVE and cell.is_edge is False


def test_eligible_positive_sample_may_be_called_an_edge():
    cell = compute_cell([0.4] * 80)
    assert cell.label == ELIGIBLE and cell.is_edge is True


def test_eligible_negative_sample_is_not_an_edge():
    cell = compute_cell([-0.2] * 80)
    assert cell.label == ELIGIBLE and cell.is_edge is False


def test_report_labels_unproven_cells_explicitly():
    text = format_report(build_report([outcome(realized_r=2.0), outcome(realized_r=1.5)]))
    assert "[UNPROVEN]" in text
    assert "NOT AN EDGE" in text
    assert "hypotheses, not results" in text


def test_report_documents_the_thresholds():
    text = format_report(build_report([outcome()]))
    assert "n<30 unproven" in text and "80 eligible" in text


# ---------------- metrics ----------------

def test_mean_and_median_r():
    cell = compute_cell([1.0, -0.5, 2.0, -1.0])
    assert cell.mean_r == pytest.approx(0.375)
    assert cell.median_r == pytest.approx(0.25)


def test_win_rate_and_average_win_loss():
    cell = compute_cell([2.0, -1.0, 1.0, -1.0])
    assert cell.win_rate == pytest.approx(0.5)
    assert cell.avg_win_r == pytest.approx(1.5)
    assert cell.avg_loss_r == pytest.approx(-1.0)


def test_profit_factor_matches_gross_win_over_gross_loss():
    cell = compute_cell([2.0, 1.0, -1.0, -0.5])
    assert cell.profit_factor == pytest.approx(3.0 / 1.5)


def test_profit_factor_is_undefined_without_losses():
    cell = compute_cell([1.0, 2.0, 3.0])
    assert cell.profit_factor is None  # not "infinity", not a fabricated large number


def test_profit_factor_is_zero_when_there_are_no_wins():
    cell = compute_cell([-1.0, -2.0])
    assert cell.profit_factor == pytest.approx(0.0)


def test_std_is_undefined_for_a_single_sample():
    assert compute_cell([1.0]).std_r is None


def test_max_drawdown_of_a_rising_path_is_zero():
    assert max_drawdown_r([1.0, 1.0, 1.0]) == pytest.approx(0.0)


def test_max_drawdown_measures_peak_to_trough():
    # cumulative: 2, 1, -1, 0 -> peak 2, trough -1 -> drawdown 3
    assert max_drawdown_r([2.0, -1.0, -2.0, 1.0]) == pytest.approx(3.0)


def test_max_drawdown_of_empty_path_is_undefined():
    assert max_drawdown_r([]) is None


def test_averages_of_mfe_mae_and_bars_held():
    cell = compute_cell([1.0, -1.0], mfe=[2.0, 0.5], mae=[0.2, 1.2], bars_held=[4, 8])
    assert cell.avg_mfe_r == pytest.approx(1.25)
    assert cell.avg_mae_r == pytest.approx(0.7)
    assert cell.avg_bars_held == pytest.approx(6.0)


def test_metrics_are_not_invented_when_inputs_are_absent():
    cell = compute_cell([1.0], mfe=[], mae=[], bars_held=[])
    assert cell.avg_mfe_r is None and cell.avg_mae_r is None and cell.avg_bars_held is None


# ---------------- CODE vs LLM split ----------------

def test_code_vs_llm_split_separates_the_books():
    rows = [outcome(realized_r=1.0, llm_involved=False), outcome(realized_r=2.0, llm_involved=False),
            outcome(realized_r=-1.0, llm_involved=True)]
    group = split_by_llm(rows, setup_id="trend_pullback")
    assert group.code_only.n == 2 and group.code_plus_llm.n == 1
    assert group.code_only.mean_r == pytest.approx(1.5)
    assert group.code_plus_llm.mean_r == pytest.approx(-1.0)
    assert group.combined.n == 3


def test_split_handles_an_entirely_code_only_journal():
    group = split_by_llm([outcome(llm_involved=False)], setup_id="s")
    assert group.code_plus_llm.n == 0 and group.code_plus_llm.mean_r is None


def test_report_shows_both_books():
    rows = [outcome(realized_r=1.0, llm_involved=False), outcome(realized_r=-1.0, llm_involved=True)]
    text = format_report(build_report(rows))
    assert "CODE_ONLY n=1" in text and "CODE+LLM n=1" in text


def test_split_cell_keys_name_their_book():
    group = split_by_llm([outcome()], setup_id="s")
    assert group.code_only.key["book"] == stats.BOOK_CODE_ONLY
    assert group.code_plus_llm.key["book"] == stats.BOOK_CODE_PLUS_LLM


# ---------------- setup filtering ----------------

def test_report_groups_by_setup():
    rows = [outcome(setup_id="trend_pullback"), outcome(setup_id="breakout_retest")]
    groups = build_report(rows)
    assert sorted(g.setup_id for g in groups) == ["breakout_retest", "trend_pullback"]


def test_report_can_be_restricted_to_one_setup():
    rows = [outcome(setup_id="trend_pullback", realized_r=1.0),
            outcome(setup_id="breakout_retest", realized_r=-1.0)]
    groups = build_report(rows, setup_id="trend_pullback")
    assert len(groups) == 1
    assert groups[0].setup_id == "trend_pullback"
    assert groups[0].combined.n == 1
    assert groups[0].combined.mean_r == pytest.approx(1.0)


def test_filtered_report_excludes_other_setups_from_the_text():
    rows = [outcome(setup_id="trend_pullback"), outcome(setup_id="sweep_reclaim")]
    text = format_report(build_report(rows, setup_id="trend_pullback"), setup_id="trend_pullback")
    assert "trend_pullback" in text and "sweep_reclaim" not in text


# ---------------- closed positions and leakage ----------------

def test_query_counts_only_closed_positions_with_a_realized_r():
    source = inspect.getsource(stats.fetch_closed_outcomes)
    assert "p.status = 'CLOSED'" in source
    assert "p.realized_r IS NOT NULL" in source


def test_query_joins_outcomes_to_their_originating_idea():
    source = inspect.getsource(stats.fetch_closed_outcomes)
    assert "JOIN ideas i ON i.id = p.idea_id" in source


def test_statistics_use_only_supplied_realized_outcomes():
    """No forward-looking field can reach a metric: the cell only sees realized R."""
    signature = inspect.signature(compute_cell)
    assert set(signature.parameters) == {"returns", "key", "mfe", "mae", "bars_held"}


def test_open_position_rows_are_not_representable_in_a_cell():
    # A row without realized_r cannot be built into a cell; there is no default.
    with pytest.raises(TypeError):
        compute_cell([None])


def test_report_totals_match_the_rows_given():
    rows = [outcome(realized_r=r) for r in (1.0, -1.0, 0.5)]
    group = build_report(rows)[0]
    assert group.combined.n == 3
    assert group.combined.mean_r == pytest.approx((1.0 - 1.0 + 0.5) / 3)


def test_cell_to_dict_exposes_the_edge_verdict():
    data = compute_cell([1.0] * 80).to_dict()
    assert data["is_edge"] is True and data["label"] == ELIGIBLE
    assert data["n"] == 80
