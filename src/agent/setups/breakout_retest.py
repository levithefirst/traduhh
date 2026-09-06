from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from agent.setups.base import Detection, finite


def detect(rows: Sequence[Mapping[str, Any]], *, asset: str, timeframe: str, features: Mapping[str, Any], regime: Mapping[str, Any], bar_open_time: datetime) -> Detection | None:
    primary = regime.get("label")
    secondary = set(regime.get("secondary") or [])
    if primary == "RANGE" or "PANIC" in secondary or "EVENT_HIGH" in secondary or not rows:
        return None
    atrs = [finite(r.get("atr_14")) for r in rows]
    # ATR is persisted in the feature snapshot only for the target bar. The detector
    # uses the target-bar ATR for all coded thresholds, avoiding a second indicator engine.
    atr = finite(features.get("atr_14"))
    if atr is None or atr <= 0 or len(rows) < 22:
        return None
    target = len(rows) - 1
    # The confirmation bar is the target. Search only historical break bars.
    for k in range(target, max(20, target - 12), -1):
        break_idx = k - 1
        if break_idx < 20:
            continue
        window = rows[break_idx-20:break_idx]
        highs = [finite(r.get("h")) for r in window]; lows = [finite(r.get("l")) for r in window]
        if any(x is None for x in highs + lows):
            continue
        long_boundary = max(highs); short_boundary = min(lows)
        br_close = finite(rows[break_idx].get("c")); br_high = finite(rows[break_idx].get("h")); br_low = finite(rows[break_idx].get("l"))
        if br_close is None or br_high is None or br_low is None:
            continue
        # Long break then retest.
        if br_close >= long_boundary + 0.10 * atr and br_close - long_boundary <= 2.0 * atr:
            confirm = rows[target]
            ch = finite(confirm.get("h")); cl = finite(confirm.get("l")); cc = finite(confirm.get("c"))
            if ch is not None and cl is not None and cc is not None and cl <= long_boundary + 0.20 * atr and cl >= long_boundary - 0.20 * atr and cc > long_boundary:
                stop = cl - 0.20 * atr
                return Detection("breakout_retest", asset, timeframe, "long", bar_open_time, target, cc, stop, [],
                                 {"break_boundary": long_boundary, "break_index": break_idx}, {"retest_window": target-break_idx})
        # Short break then retest.
        if br_close <= short_boundary - 0.10 * atr and short_boundary - br_close <= 2.0 * atr:
            confirm = rows[target]
            ch = finite(confirm.get("h")); cl = finite(confirm.get("l")); cc = finite(confirm.get("c"))
            if ch is not None and cl is not None and cc is not None and ch >= short_boundary - 0.20 * atr and ch <= short_boundary + 0.20 * atr and cc < short_boundary:
                stop = ch + 0.20 * atr
                return Detection("breakout_retest", asset, timeframe, "short", bar_open_time, target, cc, stop, [],
                                 {"break_boundary": short_boundary, "break_index": break_idx}, {"retest_window": target-break_idx})
    return None
