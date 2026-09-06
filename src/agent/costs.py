from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Mapping

from agent.geometry import TF_HOURS


@dataclass(frozen=True)
class CostModel:
    fee_round_trip: float
    impact_bps: float
    slip_bps: float
    slip_cost_rt: float
    funding_est: float
    cost_r: float
    planned_r_after_costs: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


def calculate_costs(*, notional: float, size: float, notional_to_10bps: float | None, funding: float | None,
                    timeframe: str, risk_cash: float, raw_r: float, taker_fee_bps: float, slippage_bps_floor: float,
                    hold_bars: int) -> CostModel:
    if timeframe not in TF_HOURS:
        raise ValueError("unsupported timeframe")
    fee = 2.0 * float(taker_fee_bps) / 10000.0 * notional
    depth = float(notional_to_10bps) if notional_to_10bps is not None else 0.0
    size_notional = abs(size * 1.0) if depth <= 0 else notional
    impact = 0.0 if depth > 0 and size_notional <= depth else min(25.0, 10.0 * size_notional / max(depth, 1e-9))
    slip_bps = float(slippage_bps_floor) + impact
    slip_cost_rt = 2.0 * slip_bps / 10000.0 * notional
    funding_est = abs(float(funding or 0.0)) * int(hold_bars) * TF_HOURS[timeframe] * notional
    if risk_cash <= 0:
        raise ValueError("risk_cash must be positive")
    cost_r = (fee + slip_cost_rt + funding_est) / risk_cash
    return CostModel(fee, impact, slip_bps, slip_cost_rt, funding_est, cost_r, raw_r - cost_r)
