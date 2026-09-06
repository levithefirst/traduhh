from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from agent.setups.base import Detection, finite


def detect(rows: Sequence[Mapping[str, Any]], *, asset: str, timeframe: str, features: Mapping[str, Any], regime: Mapping[str, Any], bar_open_time: datetime) -> Detection | None:
    if not rows or regime.get("confidence", 0) < 0.5:
        return None
    primary = regime.get("label")
    secondary = set(regime.get("secondary") or [])
    grammar = features.get("grammar")
    atr = finite(features.get("atr_14")); ema = finite(features.get("ema_20")); close = finite(rows[-1].get("c"))
    prev_close = finite(rows[-2].get("c")) if len(rows) >= 2 else None
    swing_low = finite(features.get("last_swing_low_px")); swing_high = finite(features.get("last_swing_high_px"))
    if atr is None or atr <= 0 or ema is None or close is None or prev_close is None:
        return None
    if "PANIC" in secondary or "EVENT_HIGH" in secondary:
        return None
    band = 0.25 * atr
    last3 = rows[-3:]
    if primary == "TREND_UP" and grammar == "HH_HL":
        touched = any(finite(r.get("l")) is not None and finite(r.get("l")) <= ema + band and finite(r.get("h")) >= ema - band for r in last3)
        if not touched or swing_low is None or close <= ema or close <= prev_close:
            return None
        # A confirmed swing is intact only if no later bar has traded below it.
        swing_time = features.get("last_swing_low_t")
        if swing_time:
            lows_after = [finite(r.get("l")) for r in rows if r.get("open_time") and r.get("open_time").isoformat() >= str(swing_time)]
            if any(x is not None and x < swing_low for x in lows_after):
                return None
        pullback_low = min(x for x in (finite(r.get("l")) for r in last3) if x is not None)
        stop = min(pullback_low, swing_low) - 0.25 * atr
        return Detection("trend_pullback", asset, timeframe, "long", bar_open_time, len(rows)-1, close, stop, [],
                         {"last_swing_low": swing_low}, {"ema20": ema, "atr14": atr, "pullback_bars": 3})
    if primary == "TREND_DOWN" and grammar == "LH_LL":
        touched = any(finite(r.get("h")) is not None and finite(r.get("h")) >= ema - band and finite(r.get("l")) <= ema + band for r in last3)
        if not touched or swing_high is None or close >= ema or close >= prev_close:
            return None
        swing_time = features.get("last_swing_high_t")
        if swing_time:
            highs_after = [finite(r.get("h")) for r in rows if r.get("open_time") and r.get("open_time").isoformat() >= str(swing_time)]
            if any(x is not None and x > swing_high for x in highs_after):
                return None
        pullback_high = max(x for x in (finite(r.get("h")) for r in last3) if x is not None)
        stop = max(pullback_high, swing_high) + 0.25 * atr
        return Detection("trend_pullback", asset, timeframe, "short", bar_open_time, len(rows)-1, close, stop, [],
                         {"last_swing_high": swing_high}, {"ema20": ema, "atr14": atr, "pullback_bars": 3})
    return None
