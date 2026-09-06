from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Mapping

from agent.setups.base import Detection, finite

TF_HOURS = {"15m": 0.25, "1h": 1.0, "4h": 4.0}


@dataclass(frozen=True)
class Geometry:
    entry: float
    stop: float
    targets: list[float]
    structural_reference: dict[str, Any]
    risk_per_unit: float
    raw_r: float
    risk_cash: float
    size: float
    notional: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_geometry(detection: Detection, *, features: Mapping[str, Any], settings, current_equity: float | None = None) -> Geometry:
    entry = float(detection.entry); stop = float(detection.stop)
    atr = finite(features.get("atr_14"))
    if atr is None or atr <= 0 or entry <= 0 or stop <= 0:
        raise ValueError("geometry requires positive entry, stop and ATR")
    risk = abs(entry - stop)
    if risk <= 0:
        raise ValueError("stop must differ from entry")
    raw_target = entry + 1.8 * risk if detection.direction == "long" else entry - 1.8 * risk
    target = raw_target
    structural = dict(detection.structural_reference)
    swing_key = "last_swing_high_px" if detection.direction == "long" else "last_swing_low_px"
    swing = finite(features.get(swing_key))
    if swing is not None and ((detection.direction == "long" and swing > entry) or (detection.direction == "short" and swing < entry)):
        swing_r = abs(swing-entry)/risk
        if swing_r >= float(settings.min_r_after_costs):
            target = min(raw_target, swing) if detection.direction == "long" else max(raw_target, swing)
            structural["target_reference"] = swing_key
    if detection.setup_id == "sweep_reclaim":
        # The Step 4 feature contract has no range-mid field. Therefore no midpoint
        # is invented; the coded target remains 1.5R as specified.
        target = entry + 1.5*risk if detection.direction == "long" else entry - 1.5*risk
    if detection.setup_id == "breakout_retest":
        target = raw_target
    raw_r = abs(target-entry)/risk
    equity = float(current_equity if current_equity is not None else settings.paper_equity_usd)
    risk_cash = float(settings.risk_fraction) * equity
    # Solve the frozen sizing equation with the available fee/slippage floor and
    # size-dependent L2 impact. Impact is a function of notional, so use a small
    # deterministic fixed-point iteration rather than inventing a separate sizing model.
    depth = float(features.get("notional_to_10bps") or 0.0)
    size = risk_cash / risk
    for _ in range(12):
        notional = size * entry
        impact_bps = 0.0 if depth > 0 and notional <= depth else min(25.0, 10.0 * notional / max(depth, 1e-9))
        per_unit_exit_cost = 2.0 * entry * (float(settings.taker_fee_bps) + float(settings.slippage_bps_floor) + impact_bps) / 10000.0
        new_size = risk_cash / (risk + per_unit_exit_cost)
        if abs(new_size - size) <= max(1e-12, abs(size) * 1e-10):
            size = new_size
            break
        size = new_size
    notional = size * entry
    return Geometry(entry, stop, [target], structural, risk, raw_r, risk_cash, size, notional)
