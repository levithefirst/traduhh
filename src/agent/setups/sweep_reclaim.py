from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from agent.setups.base import Detection, finite


def detect(rows: Sequence[Mapping[str, Any]], *, asset: str, timeframe: str, features: Mapping[str, Any], regime: Mapping[str, Any], bar_open_time: datetime) -> Detection | None:
    primary = regime.get("label")
    secondary = set(regime.get("secondary") or [])
    if primary not in {"RANGE", "TREND_UP", "TREND_DOWN"} or "PANIC" in secondary or "EVENT_HIGH" in secondary:
        return None
    atr = finite(features.get("atr_14"))
    if atr is None or atr <= 0 or len(rows) < 2:
        return None
    pools: list[tuple[str, float, str]] = []
    if features.get("equal_low") and finite(features.get("last_swing_low_px")) is not None:
        pools.append(("equal_low", float(features["last_swing_low_px"]), "long"))
    if finite(features.get("pdl")) is not None:
        pools.append(("pdl", float(features["pdl"]), "long"))
    if features.get("equal_high") and finite(features.get("last_swing_high_px")) is not None:
        pools.append(("equal_high", float(features["last_swing_high_px"]), "short"))
    if finite(features.get("pdh")) is not None:
        pools.append(("pdh", float(features["pdh"]), "short"))
    target = len(rows)-1
    # A reclaim may be on the sweep bar or one of the next three closed bars.
    for sweep_idx in range(max(0, target-3), target+1):
        bar = rows[sweep_idx]
        high = finite(bar.get("h")); low = finite(bar.get("l"));
        if high is None or low is None:
            continue
        for name, pool, direction in pools:
            if direction == "long" and low <= pool - 0.10 * atr:
                for reclaim_idx in range(sweep_idx, min(target, sweep_idx+3)+1):
                    close = finite(rows[reclaim_idx].get("c"))
                    if close is not None and close > pool:
                        stop = low - 0.15 * atr
                        if abs(close-stop) < 0.35 * atr:
                            continue
                        return Detection("sweep_reclaim", asset, timeframe, "long", bar_open_time, reclaim_idx, close, stop, [],
                                         {"pool": pool, "pool_type": name, "sweep_index": sweep_idx}, {"reclaim_index": reclaim_idx})
            if direction == "short" and high >= pool + 0.10 * atr:
                for reclaim_idx in range(sweep_idx, min(target, sweep_idx+3)+1):
                    close = finite(rows[reclaim_idx].get("c"))
                    if close is not None and close < pool:
                        stop = high + 0.15 * atr
                        if abs(stop-close) < 0.35 * atr:
                            continue
                        return Detection("sweep_reclaim", asset, timeframe, "short", bar_open_time, reclaim_idx, close, stop, [],
                                         {"pool": pool, "pool_type": name, "sweep_index": sweep_idx}, {"reclaim_index": reclaim_idx})
    return None
