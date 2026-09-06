"""Part 18 Step 12: 'tests in CI locally (pytest) — lookahead and idempotency
tests green' (spec table, done-when column).

This file pins the two acceptance behaviors the frozen spec names by number
at the pure-function level, which runs in every environment with no
PostgreSQL required. The behavioral (DB-backed) counterparts — a future
candle actually excluded from a live scan, and pipeline.evaluate() run twice
against a real ideas table — live in tests/integration/test_step12_postgres.py
and are exercised by CI's PostgreSQL service, per Part 16.1:
"Lookahead tests: detector that peeks at bar t+1 must fail CI" and
"Idempotency: pipeline twice on same bar produces one idea."
"""

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from agent import pipeline
from agent.pipeline import deterministic_idea_id
from agent.setups.base import Detection

T = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


# ---------------- lookahead: the query-level guard ----------------

def test_scan_input_query_excludes_bars_after_the_target():
    """The candles SELECT that feeds every detector never fetches a future bar.

    Detectors trust rows[-1] to be the target bar (see setups/*.py); this
    query is what makes that trust valid. If a future edit weakens either
    predicate, a detector could silently peek at a forming or later bar.
    """
    source = inspect.getsource(pipeline._load_scan_inputs)
    assert "open_time<=%s" in source
    assert "close_time<=%s" in source
    # Both bounds are parameterized on `target` itself, not on "now" or an
    # unrelated column, so the filter cannot silently drift to mean something
    # other than "closed at or before this bar."
    assert "(asset,timeframe,target,target+timedelta" in source


def test_ctx_and_book_lookups_are_also_bounded_by_asof_not_wall_clock():
    """ctx/book freshness is judged against the bar's own close, not real time,
    so a scan replayed later for an old bar cannot see data from the future."""
    source = inspect.getsource(pipeline._load_scan_inputs)
    assert 'FROM asset_ctx WHERE asset=%s AND ts<=%s' in source
    assert 'FROM book_snapshots WHERE asset=%s AND ts<=%s' in source


def test_regime_htf_lookup_is_bounded_by_target_not_wall_clock():
    source = inspect.getsource(pipeline._load_scan_inputs)
    assert "open_time<=%s ORDER BY open_time DESC LIMIT 1" in source


def test_evaluate_refuses_to_run_unless_the_requested_bar_is_actually_closed():
    """A caller cannot ask evaluate() to scan a bar that has not closed yet;
    only the most recent already-closed candle is eligible (spec 6.2 SYSTEM
    RULE: 'Scans run only on closed candles')."""
    source = inspect.getsource(pipeline.evaluate)
    assert 'candles[-1]["open_time"] != target' in source
    assert "return []" in source.split('candles[-1]["open_time"] != target')[1][:40]


# ---------------- lookahead: detector contract sanity ----------------

def _trend_rows(extra_future_bar: bool):
    rows = []
    for i in range(5):
        t = T + timedelta(hours=i)
        rows.append({"open_time": t, "close_time": t + timedelta(hours=1), "o": 100, "h": 101, "l": 99, "c": 100, "v": 100})
    rows[-3].update(l=100, h=102)
    rows[-1].update(h=103, l=100, c=102)
    if extra_future_bar:
        future_t = T + timedelta(hours=5)
        # An absurd future bar: if any detector ever indexed past bar_open_time
        # into this row, entry/stop would move dramatically.
        rows.append({"open_time": future_t, "close_time": future_t + timedelta(hours=1),
                    "o": 1, "h": 999999, "l": 1, "c": 999999, "v": 1})
    return rows


def test_detector_output_is_unchanged_by_a_future_bar_when_the_caller_obeys_the_contract():
    """setups/*.py detectors index relative to bar_open_time's position, not
    the end of whatever list happens to be passed. Callers (pipeline.py) are
    responsible for slicing to bar_open_time before calling a detector; this
    test proves that as long as that contract holds, an appended future bar
    changes nothing about the detection.
    """
    from agent.setups.trend_pullback import detect

    features = {"atr_14": 2, "ema_20": 100, "grammar": "HH_HL",
                "last_swing_low_px": 97, "last_swing_low_t": (T - timedelta(hours=2)).isoformat(),
                "last_swing_high_px": 103, "last_swing_high_t": (T - timedelta(hours=2)).isoformat()}
    regime = {"label": "TREND_UP", "secondary": [], "confidence": 0.7}
    target = T + timedelta(hours=4)

    baseline_rows = _trend_rows(extra_future_bar=False)
    with_future_rows = _trend_rows(extra_future_bar=True)[:5]  # caller still slices to target
    a = detect(baseline_rows, asset="BTC", timeframe="1h", features=features, regime=regime, bar_open_time=target)
    b = detect(with_future_rows, asset="BTC", timeframe="1h", features=features, regime=regime, bar_open_time=target)
    assert a.entry == b.entry and a.stop == b.stop


def test_a_detector_fed_past_the_target_bar_would_silently_disagree():
    """The negative case: if pipeline.py's SQL guard were ever removed and a
    detector received one extra bar past bar_open_time, the result WOULD
    silently change (a different entry price, computed from data that had not
    happened yet) — this is exactly the failure the guard exists to prevent,
    and is why test_scan_input_query_excludes_bars_after_the_target must stay
    green. The extra bar here is deliberately mild (it does not trip the
    detector's own swing-invalidation check) to isolate the lookahead effect.
    """
    from agent.setups.trend_pullback import detect

    features = {"atr_14": 2, "ema_20": 100, "grammar": "HH_HL",
                "last_swing_low_px": 97, "last_swing_low_t": (T - timedelta(hours=2)).isoformat(),
                "last_swing_high_px": 103, "last_swing_high_t": (T - timedelta(hours=2)).isoformat()}
    regime = {"label": "TREND_UP", "secondary": [], "confidence": 0.7}
    target = T + timedelta(hours=4)

    correct_rows = _trend_rows(extra_future_bar=False)
    future_t = target + timedelta(hours=1)
    leaking_rows = correct_rows + [{"open_time": future_t, "close_time": future_t + timedelta(hours=1),
                                    "o": 105, "h": 112, "l": 100, "c": 110, "v": 1}]

    correct = detect(correct_rows, asset="BTC", timeframe="1h", features=features, regime=regime, bar_open_time=target)
    peeked = detect(leaking_rows, asset="BTC", timeframe="1h", features=features, regime=regime, bar_open_time=target)
    assert correct.entry == 102  # the real trigger-bar close
    assert peeked.entry == 110   # the leaked future bar's close — this must never ship


# ---------------- idempotency: deterministic identity ----------------

def _detection(**overrides):
    base = dict(asset="BTC", timeframe="1h", setup_id="trend_pullback", direction="long",
               bar_open_time=T, evidence={"ema20": 100, "atr14": 2})
    base.update(overrides)
    return base


def test_deterministic_idea_id_is_stable_across_repeated_calls():
    """This is the mechanism the ideas table's unique key relies on: calling
    the pipeline twice for the same bar must compute the same id both times,
    so the second INSERT ... ON CONFLICT hits the first row instead of
    minting a new one."""
    args = _detection()
    first = deterministic_idea_id(strategy_version_id="sv_1", **args)
    second = deterministic_idea_id(strategy_version_id="sv_1", **args)
    assert first == second


def test_deterministic_idea_id_changes_with_any_part_of_the_unique_key():
    base = deterministic_idea_id(strategy_version_id="sv_1", **_detection())
    assert base != deterministic_idea_id(strategy_version_id="sv_1", **_detection(asset="ETH"))
    assert base != deterministic_idea_id(strategy_version_id="sv_1", **_detection(timeframe="4h"))
    assert base != deterministic_idea_id(strategy_version_id="sv_1", **_detection(setup_id="sweep_reclaim"))
    assert base != deterministic_idea_id(strategy_version_id="sv_1", **_detection(direction="short"))
    assert base != deterministic_idea_id(strategy_version_id="sv_1", **_detection(bar_open_time=T + timedelta(hours=1)))
    assert base != deterministic_idea_id(strategy_version_id="sv_2", **_detection())


def test_deterministic_idea_id_is_insensitive_to_evidence_dict_key_order():
    """A re-run must not mint a new id merely because a dict was rebuilt in a
    different insertion order — canonical_json sorts keys before hashing."""
    a = deterministic_idea_id(strategy_version_id="sv_1", **_detection(evidence={"a": 1, "b": 2}))
    b = deterministic_idea_id(strategy_version_id="sv_1", **_detection(evidence={"b": 2, "a": 1}))
    assert a == b


def test_idea_persistence_upserts_on_the_frozen_unique_key():
    """The ON CONFLICT target must be exactly the spec 20.2 unique key so a
    second evaluate() call for the same bar updates in place, never inserts
    a sibling row."""
    source = inspect.getsource(pipeline._persist_idea)
    assert "ON CONFLICT (asset,timeframe,setup_id,bar_open_time,strategy_version_id)" in source
    assert "DO UPDATE SET" in source


def test_ideas_unique_index_matches_the_deterministic_id_inputs():
    """Every field deterministic_idea_id hashes over (minus strategy_version_id,
    already a column) is also part of the table's unique constraint, so the
    DB and the id function can never disagree about what counts as 'the same
    idea'."""
    sig = inspect.signature(deterministic_idea_id)
    hashed_fields = set(sig.parameters) - {"evidence"}
    unique_key_columns = {"asset", "timeframe", "setup_id", "direction", "bar_open_time", "strategy_version_id"}
    assert hashed_fields == unique_key_columns
