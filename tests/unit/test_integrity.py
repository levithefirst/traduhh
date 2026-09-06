from datetime import datetime, timedelta, timezone
from decimal import Decimal

from agent.integrity import detect_candle_gaps, detect_stale


def dt(hour=0, minute=0):
    return datetime(2026, 9, 5, hour, minute, tzinfo=timezone.utc)


def test_complete_sequence_has_no_gap():
    candles = [(dt(0), dt(1)), (dt(1), dt(2)), (dt(2), dt(3))]
    assert detect_candle_gaps(candles, "BTC", "1h", now=dt(5)) == ()


def test_gap_is_not_flagged_until_two_intervals_old():
    candles = [(dt(0), dt(1)), (dt(2), dt(3))]
    assert detect_candle_gaps(candles, "BTC", "1h", now=dt(2, 30)) == ()
    gaps = detect_candle_gaps(candles, "BTC", "1h", now=dt(4))
    assert len(gaps) == 1
    assert gaps[0].missing_bars == 1


def test_multiple_missing_candles_are_counted_deterministically():
    candles = [(dt(0), dt(1)), (dt(4), dt(5))]
    gaps = detect_candle_gaps(candles, "BTC", "1h", now=dt(7))
    assert len(gaps) == 1
    assert gaps[0].missing_bars == 3
    assert gaps[0].next_expected_open == dt(1)


def test_stale_requires_missing_or_age_over_60_seconds():
    assert detect_stale(None)
    assert detect_stale(timedelta(seconds=61))
    assert not detect_stale(timedelta(seconds=60))


def test_gap_preserves_utc_and_timeframe_boundaries():
    candles = [
        (datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc), dt(1)),
        (datetime(2026, 9, 5, 4, 0, tzinfo=timezone.utc), dt(5)),
    ]
    gaps = detect_candle_gaps(candles, "BTC", "4h", now=dt(8))
    assert len(gaps) == 0
    assert gaps == ()
