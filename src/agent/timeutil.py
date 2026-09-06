from __future__ import annotations

from datetime import datetime, timezone

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


def require_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() != timezone.utc.utcoffset(dt):
        raise ValueError("datetime must be timezone-aware UTC")
    return dt.astimezone(UTC)
