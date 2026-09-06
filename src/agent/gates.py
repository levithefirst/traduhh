from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


HARD_GATES = (
    "data_valid", "regime_ok", "setup_complete", "invalidation_clear", "min_r",
    "stop_vs_noise", "stop_too_wide", "liquidity", "funding_carry", "cluster",
    "daily_loss", "weekly_loss", "circuit", "hist_cell_fatal",
)


@dataclass(frozen=True)
class GateResult:
    passed: bool
    hard: dict[str, bool]
    reasons: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)

    @property
    def decision(self) -> str:
        return "NO_TRADE" if not all(self.hard.values()) or len(self.soft) >= 3 else "GATED_PASS"


def evaluate_gates(*, detection, geometry, costs, features: Mapping[str, Any], regime: Mapping[str, Any],
                   integrity_ok: bool = True, warmup_ok: bool = True, ctx_age_s: float | None = 0,
                   book_age_s: float | None = 0, mark_present: bool = True, paper_positions: Sequence[Mapping[str, Any]] = (),
                   current_equity: float = 10000.0, day_pnl_pct: float = 0.0, week_pnl_pct: float = 0.0,
                   halted: bool = False, hist_cell: Mapping[str, Any] | None = None,
                   day_ntl_vlm: float | None = None, notional_to_10bps: float | None = None, min_r_after_costs: float = 1.2, max_concurrent: int = 3) -> GateResult:
    primary = regime.get("label"); secondary = set(regime.get("secondary") or [])
    direction = detection.direction
    compatible = (
        (detection.setup_id == "trend_pullback" and primary == ("TREND_UP" if direction == "long" else "TREND_DOWN"))
        or (detection.setup_id == "breakout_retest" and ("BREAKOUT_CLIMATE" in secondary or (primary not in {"RANGE", "UNKNOWN"})))
        or (detection.setup_id == "sweep_reclaim" and primary in {"RANGE", "TREND_UP", "TREND_DOWN"})
    )
    data_valid = bool(integrity_ok and warmup_ok and mark_present and (ctx_age_s is None or ctx_age_s <= 60) and (book_age_s is None or book_age_s <= 60))
    regime_ok = bool(primary != "UNKNOWN" and "PANIC" not in secondary and "EVENT_HIGH" not in secondary and compatible)
    higher_tf = regime.get("higher_tf")
    if higher_tf is not None and (higher_tf.get("label") == "UNKNOWN" or "PANIC" in set(higher_tf.get("secondary") or []) or "EVENT_HIGH" in set(higher_tf.get("secondary") or [])):
        regime_ok = False
    stop_side = geometry.stop < geometry.entry if direction == "long" else geometry.stop > geometry.entry
    hard = {
        "data_valid": data_valid,
        "regime_ok": regime_ok,
        "setup_complete": bool(geometry.targets and geometry.entry > 0 and geometry.stop > 0),
        "invalidation_clear": bool(stop_side and geometry.stop != geometry.entry),
        "min_r": costs.planned_r_after_costs >= min_r_after_costs,
        "stop_vs_noise": geometry.risk_per_unit >= 0.40 * float(features.get("atr_14") or 0),
        "stop_too_wide": geometry.risk_per_unit <= 2.5 * float(features.get("atr_14") or 0),
        "liquidity": True,
        "funding_carry": costs.funding_est <= 0.35 * geometry.risk_cash,
        "cluster": True,
        "daily_loss": day_pnl_pct > -0.02,
        "weekly_loss": week_pnl_pct > -0.05,
        "circuit": not halted,
        "hist_cell_fatal": True,
    }
    if day_ntl_vlm is not None and day_ntl_vlm > 0:
        hard["liquidity"] = geometry.notional <= 0.05 * day_ntl_vlm
    if notional_to_10bps is not None and notional_to_10bps > 0:
        hard["liquidity"] = hard["liquidity"] and geometry.notional <= 0.30 * notional_to_10bps
    same_asset = any(str(p.get("asset")) == detection.asset for p in paper_positions if str(p.get("status", "OPEN")) == "OPEN")
    same_dir = sum(1 for p in paper_positions if str(p.get("status", "OPEN")) == "OPEN" and str(p.get("direction")) == direction)
    hard["cluster"] = not same_asset and same_dir < 2 and len([p for p in paper_positions if str(p.get("status", "OPEN")) == "OPEN"]) < max_concurrent
    if hist_cell and int(hist_cell.get("n", 0) or 0) >= 80 and float(hist_cell.get("mean_r", 0) or 0) < -0.15:
        hard["hist_cell_fatal"] = False
    soft: list[str] = []
    if float(features.get("vol_ratio") or 0) < 0.6: soft.append("low_volume")
    adx = float(features.get("adx_14") or 0)
    if 16 <= adx <= 20: soft.append("borderline_adx")
    spread = features.get("spread_bps")
    limit = 12 if detection.asset == "SOL" else 5
    if spread is not None and float(spread) > limit: soft.append("wide_spread")
    if not features.get("lookahead_protected", True): soft.append("llm_packet_incomplete")
    reasons = [name for name, passed in hard.items() if not passed]
    return GateResult(all(hard.values()) and len(soft) < 3, hard, reasons, soft)
