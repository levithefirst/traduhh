from datetime import datetime, timedelta, timezone

import pytest

from agent.monitor import walk_position
from agent.outcomes import (
    classify_outcome,
    compute_equity_snapshot,
    compute_funding,
    compute_realized,
    entry_slippage_cost,
    exit_fill_price,
    exit_slippage_cost,
    hour_buckets,
)
from agent.paper import build_entry, entry_fill_price, stop_fill_price

T = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


def bar(i, h, l, c, base=T):
    t = base + timedelta(hours=i)
    return {"open_time": t, "close_time": t + timedelta(hours=1), "h": h, "l": l, "c": c}


def idea(direction="long", entry=100.0, stop=98.0, target=103.0, slip_bps=2.0, decision="TRADE_PAPER"):
    return {
        "id": "idea-1", "asset": "BTC", "timeframe": "1h", "direction": direction,
        "bar_open_time": T, "decision": decision,
        "geometry": {"entry": entry, "stop": stop, "targets": [target], "size": 0.25, "notional": 25.0, "risk_cash": 50.0},
        "costs": {"slip_bps": slip_bps, "fee_round_trip": 1.5},
    }


# ---------- entry fill model ----------

def test_entry_fill_long_uses_min_ask_close_when_ask_below_close():
    px = entry_fill_price(direction="long", close=100.0, bid1=99.9, ask1=99.8, slip_bps=2.0)
    assert px == pytest.approx(99.8 * 1.0002)


def test_entry_fill_long_uses_min_ask_close_when_close_below_ask():
    px = entry_fill_price(direction="long", close=100.0, bid1=99.9, ask1=100.05, slip_bps=2.0)
    assert px == pytest.approx(100.0 * 1.0002)


def test_entry_fill_long_missing_book_falls_back_to_close():
    px = entry_fill_price(direction="long", close=100.0, bid1=None, ask1=None, slip_bps=2.0)
    assert px == pytest.approx(100.0 * 1.0002)


def test_entry_fill_short_uses_max_bid_close_when_bid_above_close():
    px = entry_fill_price(direction="short", close=100.0, bid1=100.2, ask1=100.3, slip_bps=2.0)
    assert px == pytest.approx(100.2 * 0.9998)


def test_entry_fill_short_uses_max_bid_close_when_close_above_bid():
    px = entry_fill_price(direction="short", close=100.0, bid1=99.95, ask1=100.1, slip_bps=2.0)
    assert px == pytest.approx(100.0 * 0.9998)  # max(99.95, 100.0) == 100.0 (close), adverse = lower fill


def test_entry_fill_short_missing_book_falls_back_to_close():
    px = entry_fill_price(direction="short", close=100.0, bid1=None, ask1=None, slip_bps=2.0)
    assert px == pytest.approx(100.0 * 0.9998)


def test_entry_fill_rejects_bad_direction():
    with pytest.raises(ValueError):
        entry_fill_price(direction="sideways", close=100.0, bid1=None, ask1=None, slip_bps=2.0)


def test_build_entry_immediate_hypothetical_fill_long():
    fill = build_entry(idea("long"), close=100.0, bid1=None, ask1=None)
    assert fill.entry_fill == pytest.approx(100.0 * 1.0002)
    assert fill.stop == 98.0 and fill.target == 103.0 and fill.risk_cash == 50.0


def test_build_entry_immediate_hypothetical_fill_short():
    fill = build_entry(idea("short", entry=100.0, stop=102.0, target=97.0), close=100.0, bid1=None, ask1=None)
    assert fill.entry_fill == pytest.approx(100.0 * 0.9998)


def test_build_entry_rejects_non_trade_paper_idea():
    with pytest.raises(ValueError):
        build_entry(idea(decision="NO_TRADE"), close=100.0, bid1=None, ask1=None)


# ---------- stop fill model (adverse slippage) ----------

def test_stop_fill_long_is_worse_than_stop():
    px = stop_fill_price(direction="long", stop=98.0, slip_bps=10.0)
    assert px == pytest.approx(98.0 * (1 - 0.5 * 10.0 / 10000.0))
    assert px < 98.0


def test_stop_fill_short_is_worse_than_stop():
    px = stop_fill_price(direction="short", stop=102.0, slip_bps=10.0)
    assert px == pytest.approx(102.0 * (1 + 0.5 * 10.0 / 10000.0))
    assert px > 102.0


# ---------- walk_position: exits, MFE/MAE, no lookahead ----------

def test_walk_target_exit_long():
    bars = [bar(1, 101, 99.5, 100.5), bar(2, 104, 100, 103.5)]
    r = walk_position(direction="long", entry=100, stop=98, target=103, bars=bars, hold_bars_limit=24, halted=False)
    assert r.exit_reason == "target"
    assert r.bars_held == 2
    assert r.exit_bar is bars[1]


def test_walk_stop_exit_long():
    bars = [bar(1, 101, 97.5, 98)]
    r = walk_position(direction="long", entry=100, stop=98, target=103, bars=bars, hold_bars_limit=24, halted=False)
    assert r.exit_reason == "stop"
    assert r.bars_held == 1


def test_walk_target_exit_short():
    bars = [bar(1, 99, 96.5, 97)]
    r = walk_position(direction="short", entry=100, stop=102, target=97, bars=bars, hold_bars_limit=24, halted=False)
    assert r.exit_reason == "target"


def test_walk_stop_exit_short():
    bars = [bar(1, 102.5, 99, 101)]
    r = walk_position(direction="short", entry=100, stop=102, target=97, bars=bars, hold_bars_limit=24, halted=False)
    assert r.exit_reason == "stop"


def test_walk_same_candle_target_and_stop_stop_wins_long():
    # single bar's range covers BOTH stop (98) and target (103)
    bars = [bar(1, 104, 97, 100)]
    r = walk_position(direction="long", entry=100, stop=98, target=103, bars=bars, hold_bars_limit=24, halted=False)
    assert r.exit_reason == "stop"


def test_walk_same_candle_target_and_stop_stop_wins_short():
    bars = [bar(1, 103, 96, 100)]
    r = walk_position(direction="short", entry=100, stop=102, target=97, bars=bars, hold_bars_limit=24, halted=False)
    assert r.exit_reason == "stop"


def test_walk_time_stop_exact_bar_count():
    bars = [bar(i, 100.5, 99.5, 100) for i in range(1, 6)]  # never touches stop/target
    r = walk_position(direction="long", entry=100, stop=90, target=200, bars=bars, hold_bars_limit=5, halted=False)
    assert r.exit_reason == "time_stop"
    assert r.bars_held == 5
    assert r.exit_bar is bars[4]


def test_walk_halt_flatten_after_at_least_one_bar():
    bars = [bar(1, 100.5, 99.5, 100.2), bar(2, 100.6, 99.6, 100.3)]
    r = walk_position(direction="long", entry=100, stop=90, target=200, bars=bars, hold_bars_limit=24, halted=True)
    assert r.exit_reason == "halt_flatten"
    assert r.exit_bar is bars[-1]


def test_walk_halted_but_zero_bars_stays_open():
    r = walk_position(direction="long", entry=100, stop=90, target=200, bars=[], hold_bars_limit=24, halted=True)
    assert r.exit_reason is None
    assert r.bars_held == 0


def test_walk_mfe_mae_long():
    bars = [bar(1, 103, 99, 101), bar(2, 105, 98, 100), bar(3, 104, 99.5, 100.5)]
    r = walk_position(direction="long", entry=100, stop=90, target=200, bars=bars, hold_bars_limit=24, halted=False)
    assert r.mfe_px == 105  # highest high across all bars
    assert r.mae_px == 98   # lowest low across all bars
    assert r.mfe_r == pytest.approx((105 - 100) / 10)
    assert r.mae_r == pytest.approx((100 - 98) / 10)


def test_walk_mfe_mae_short():
    bars = [bar(1, 101, 97, 99), bar(2, 102, 95, 96), bar(3, 100.5, 96.5, 97)]
    r = walk_position(direction="short", entry=100, stop=110, target=0, bars=bars, hold_bars_limit=24, halted=False)
    assert r.mfe_px == 95    # lowest low = most favorable for a short
    assert r.mae_px == 102   # highest high = most adverse for a short
    assert r.mfe_r == pytest.approx((100 - 95) / 10)
    assert r.mae_r == pytest.approx((102 - 100) / 10)


def test_walk_no_lookahead_beyond_first_qualifying_bar():
    # bar 1 hits target; bar 2 would hit stop if it were ever consulted. The
    # walk must stop at bar 1 and never let bar 2 change the outcome.
    bars = [bar(1, 104, 99.5, 103.5), bar(2, 100, 90, 95)]
    r = walk_position(direction="long", entry=100, stop=98, target=103, bars=bars, hold_bars_limit=24, halted=False)
    assert r.exit_reason == "target"
    assert r.bars_held == 1
    assert r.exit_bar is bars[0]
    assert r.mae_px == 99.5  # bar 2's lower low (90) must never be observed


def test_walk_is_pure_and_idempotent():
    bars = [bar(1, 101, 99.5, 100.5), bar(2, 104, 100, 103.5)]
    r1 = walk_position(direction="long", entry=100, stop=98, target=103, bars=bars, hold_bars_limit=24, halted=False)
    r2 = walk_position(direction="long", entry=100, stop=98, target=103, bars=bars, hold_bars_limit=24, halted=False)
    assert r1 == r2


def test_walk_rejects_zero_risk():
    with pytest.raises(ValueError):
        walk_position(direction="long", entry=100, stop=100, target=103, bars=[], hold_bars_limit=24, halted=False)


# ---------- outcome classification + exit fill ----------

def test_classify_outcome_all_reasons():
    assert classify_outcome("target") == "target_hit"
    assert classify_outcome("stop") == "stop_hit"
    assert classify_outcome("time_stop") == "time_stop"
    assert classify_outcome("halt_flatten") == "halt_flatten"


def test_classify_outcome_rejects_unknown():
    with pytest.raises(ValueError):
        classify_outcome("mystery")


def test_exit_fill_target_uses_step5_target_no_extra_slip():
    assert exit_fill_price(direction="long", exit_reason="target", stop=98, target=103, bar_close=110, slip_bps=50) == 103


def test_exit_fill_stop_applies_adverse_slippage():
    px = exit_fill_price(direction="long", exit_reason="stop", stop=98, target=103, bar_close=97, slip_bps=10)
    assert px == pytest.approx(stop_fill_price(direction="long", stop=98, slip_bps=10))


def test_exit_fill_time_stop_and_halt_use_bar_close_no_slip():
    assert exit_fill_price(direction="long", exit_reason="time_stop", stop=98, target=103, bar_close=101.25, slip_bps=50) == 101.25
    assert exit_fill_price(direction="short", exit_reason="halt_flatten", stop=102, target=97, bar_close=99.5, slip_bps=50) == 99.5


# ---------- realized R, fees, slippage ----------

def test_compute_realized_r_long_win_matches_manual_formula():
    out = compute_realized(direction="long", entry_fill=100.2, exit_fill=103.0, size=10, risk_cash=50,
                           fees_usd=1.5, funding_usd=0.4, entry_slip_usd=0.2, exit_slip_usd=0.0)
    raw_pnl = (103.0 - 100.2) * 10
    net_pnl = raw_pnl - 1.5 - 0.4
    assert out.pnl_usd == pytest.approx(net_pnl)
    assert out.realized_r == pytest.approx(net_pnl / 50)
    assert out.slip_usd == pytest.approx(0.2)


def test_compute_realized_r_short_win():
    out = compute_realized(direction="short", entry_fill=100.0, exit_fill=97.0, size=10, risk_cash=50,
                           fees_usd=1.0, funding_usd=-0.5, entry_slip_usd=0.1, exit_slip_usd=0.2)
    raw_pnl = (97.0 - 100.0) * 10 * -1  # short: price down = profit
    net_pnl = raw_pnl - 1.0 - (-0.5)
    assert out.pnl_usd == pytest.approx(net_pnl)
    assert out.realized_r == pytest.approx(net_pnl / 50)


def test_compute_realized_rejects_nonpositive_risk_cash():
    with pytest.raises(ValueError):
        compute_realized(direction="long", entry_fill=100, exit_fill=101, size=1, risk_cash=0,
                         fees_usd=0, funding_usd=0, entry_slip_usd=0, exit_slip_usd=0)


def test_entry_slippage_cost_is_never_negative_and_matches_formula():
    cost = entry_slippage_cost(direction="long", entry_fill=100.2, reference_price=100.0, size=10)
    assert cost == pytest.approx(2.0)


def test_exit_slippage_cost_only_applies_to_stop():
    assert exit_slippage_cost(direction="long", exit_reason="target", exit_fill=103, ideal_price=103, size=10) == 0.0
    cost = exit_slippage_cost(direction="long", exit_reason="stop", exit_fill=97.9, ideal_price=98.0, size=10)
    assert cost == pytest.approx(1.0)


# ---------- funding ----------

def test_hour_buckets_counts_distinct_hours():
    start = T
    end = T + timedelta(hours=3, minutes=10)
    buckets = hour_buckets(start, end)
    assert buckets == [T + timedelta(hours=i) for i in range(4)]


def test_hour_buckets_empty_when_no_time_elapsed():
    assert hour_buckets(T, T) == []


def test_compute_funding_long_pays_when_rate_positive():
    rates = {T: 0.0001, T + timedelta(hours=1): 0.0001}
    funding_usd, missing = compute_funding(direction="long", notional=10000, opened_at=T, closed_at=T + timedelta(hours=2), hourly_rates=rates)
    assert funding_usd == pytest.approx(0.0001 * 10000 * 2)
    assert missing is False


def test_compute_funding_short_receives_when_rate_positive():
    rates = {T: 0.0001, T + timedelta(hours=1): 0.0001}
    funding_usd, missing = compute_funding(direction="short", notional=10000, opened_at=T, closed_at=T + timedelta(hours=2), hourly_rates=rates)
    assert funding_usd == pytest.approx(-0.0001 * 10000 * 2)
    assert missing is False


def test_compute_funding_missing_hour_is_flagged_not_invented():
    rates = {T: 0.0001}  # second hour bucket missing entirely
    funding_usd, missing = compute_funding(direction="long", notional=10000, opened_at=T, closed_at=T + timedelta(hours=2), hourly_rates=rates)
    assert missing is True
    assert funding_usd == pytest.approx(0.0001 * 10000)  # only the known hour is summed


def test_compute_funding_completely_missing_flags_and_returns_zero():
    funding_usd, missing = compute_funding(direction="long", notional=10000, opened_at=T, closed_at=T + timedelta(hours=1), hourly_rates={})
    assert missing is True
    assert funding_usd == 0.0


def test_compute_funding_zero_duration_is_not_flagged_missing():
    funding_usd, missing = compute_funding(direction="long", notional=10000, opened_at=T, closed_at=T, hourly_rates={})
    assert funding_usd == 0.0 and missing is False


# ---------- equity snapshots + drawdown ----------

def test_equity_snapshot_new_peak_has_zero_drawdown():
    snap = compute_equity_snapshot(starting_equity=10000, closed_pnl_total=500, open_mtm_total=100,
                                   open_risk_total=50, prior_peak_equity=10000)
    assert snap.equity == pytest.approx(10600)
    assert snap.peak_equity == pytest.approx(10600)
    assert snap.drawdown_from_peak == 0.0


def test_equity_snapshot_below_peak_has_positive_drawdown():
    snap = compute_equity_snapshot(starting_equity=10000, closed_pnl_total=-300, open_mtm_total=0,
                                   open_risk_total=50, prior_peak_equity=10600)
    assert snap.equity == pytest.approx(9700)
    assert snap.peak_equity == pytest.approx(10600)
    assert snap.drawdown_from_peak == pytest.approx((10600 - 9700) / 10600)


def test_equity_snapshot_open_risk_passthrough():
    snap = compute_equity_snapshot(starting_equity=10000, closed_pnl_total=0, open_mtm_total=0,
                                   open_risk_total=150, prior_peak_equity=10000)
    assert snap.open_risk == 150
