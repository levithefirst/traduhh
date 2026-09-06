from datetime import datetime, timezone

import pytest

from agent.timeutil import require_utc, utc_now


def test_utc_now_is_aware_utc():
    value = utc_now()
    assert value.tzinfo is not None
    assert value.utcoffset() == timezone.utc.utcoffset(value)


def test_require_utc_rejects_naive():
    with pytest.raises(ValueError, match="UTC"):
        require_utc(datetime(2026, 9, 5, 12, 0))
