"""Deterministic LLM packet construction (spec 11.2).

The packet is built entirely from evidence the deterministic pipeline already
computed. It carries no secrets: no API keys, no bot token, no database URL,
no wallet material (none of which exists in this MVP anyway). Construction is
pure and order-stable so the same candidate always hashes to the same packet.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

PACKET_SCHEMA = "agent.llm_packet.v1"

# Only these feature keys may leave the process (a subset of spec 7.1).
ALLOWED_FEATURE_KEYS = (
    "ema_20", "ema_50", "sma_20", "atr_14", "atr_pct_100", "adx_14", "plus_di", "minus_di",
    "rsi_14", "vol_ratio", "ret_1", "ret_12", "z_close_20", "grammar",
    "last_swing_high_px", "last_swing_low_px", "pdh", "pdl", "dist_ema20_atr",
)

FORBIDDEN = (
    "do not invent levels",
    "do not claim news caused the tape",
)


def _round(value: Any, digits: int = 8) -> Any:
    """Stable numeric rendering; non-numerics pass through unchanged."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return round(float(value), digits)
    return value


def _features_subset(features: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _round(features[key]) for key in ALLOWED_FEATURE_KEYS if key in features}


def build_packet(*, idea_id: str, ts_utc: str, asset: str, timeframe: str,
                 strategy_version_id: str, prompt_version_id: str, mode: str,
                 data_quality: Mapping[str, Any], regime: Mapping[str, Any],
                 setup: Mapping[str, Any], features: Mapping[str, Any],
                 book: Mapping[str, Any], derivatives: Mapping[str, Any],
                 costs: Mapping[str, Any], portfolio: Mapping[str, Any],
                 news: Sequence[Mapping[str, Any]], calendar: Sequence[Mapping[str, Any]],
                 hist_cell: Mapping[str, Any], gates_passed: Sequence[str]) -> dict[str, Any]:
    """Build the exact spec 11.2 packet. Deterministic for identical inputs."""
    levels = setup.get("levels") or {}
    return {
        "schema": PACKET_SCHEMA,
        "idea_id": str(idea_id),
        "ts_utc": ts_utc,
        "asset": asset,
        "timeframe": timeframe,
        "strategy_version_id": strategy_version_id,
        "prompt_version_id": prompt_version_id,
        "mode": mode,
        "data_quality": {"ok": bool(data_quality.get("ok", True)), "flags": list(data_quality.get("flags") or [])},
        "regime": {
            "primary": regime.get("label") or regime.get("primary"),
            "secondary": list(regime.get("secondary") or []),
            "confidence": _round(regime.get("confidence")),
        },
        "setup": {
            "id": setup.get("id"),
            "direction": setup.get("direction"),
            "trigger_desc": "coded",
            "levels": {
                "entry": _round(levels.get("entry")),
                "stop": _round(levels.get("stop")),
                "targets": [_round(t) for t in (levels.get("targets") or [])],
            },
        },
        "features": _features_subset(features),
        "book": {
            "spread_bps": _round(book.get("spread_bps")),
            "imbalance_5": _round(book.get("imbalance_5")),
            "notional_to_10bps": _round(book.get("notional_to_10bps")),
        },
        "derivatives": {
            "funding": _round(derivatives.get("funding")),
            "funding_z_168": _round(derivatives.get("funding_z_168")),
            "oi": _round(derivatives.get("oi")),
            "oi_chg_24h": _round(derivatives.get("oi_chg_24h")),
            "basis_bps": _round(derivatives.get("basis_bps")),
        },
        "costs": {
            "fee_rt": _round(costs.get("fee_round_trip")),
            "slip_est": _round(costs.get("slip_cost_rt")),
            "funding_est": _round(costs.get("funding_est")),
            "planned_R_after_costs": _round(costs.get("planned_r_after_costs")),
        },
        "portfolio": {
            "equity": _round(portfolio.get("equity")),
            "open_positions": list(portfolio.get("open_positions") or []),
            "day_pnl_pct": _round(portfolio.get("day_pnl_pct")),
            "cluster": portfolio.get("cluster"),
        },
        "news": [{"ts": n.get("ts"), "title": n.get("title"), "source": n.get("source")} for n in news],
        "calendar": [{"name": c.get("name"), "impact": c.get("impact"), "minutes_to": c.get("minutes_to")} for c in calendar],
        "hist_cell": {
            "n": hist_cell.get("n", 0),
            "mean_r": _round(hist_cell.get("mean_r")),
            "std_r": _round(hist_cell.get("std_r")),
            "win_rate": _round(hist_cell.get("win_rate")),
            "note": hist_cell.get("note", "unproven"),
        },
        "gates_passed": list(gates_passed),
        "forbidden": list(FORBIDDEN),
    }


def canonical_json(packet: Mapping[str, Any]) -> str:
    return json.dumps(packet, sort_keys=True, separators=(",", ":"), default=str)


def packet_hash(packet: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(packet).encode()).hexdigest()


def packet_price_allowlist(packet: Mapping[str, Any]) -> set[str]:
    """Every price the LLM is permitted to cite, as normalized strings.

    Used to detect invented levels in the model's prose (spec 11.5).
    """
    levels = ((packet.get("setup") or {}).get("levels") or {})
    values: list[Any] = [levels.get("entry"), levels.get("stop"), *(levels.get("targets") or [])]
    features = packet.get("features") or {}
    for key in ("ema_20", "ema_50", "sma_20", "last_swing_high_px", "last_swing_low_px", "pdh", "pdl"):
        if features.get(key) is not None:
            values.append(features[key])
    allow: set[str] = set()
    for value in values:
        if value is None or isinstance(value, str):
            continue
        number = float(value)
        allow.add(f"{number:.8f}".rstrip("0").rstrip("."))
        allow.add(f"{number:.4f}".rstrip("0").rstrip("."))
        allow.add(f"{number:.2f}")
        allow.add(f"{number:.1f}")
        allow.add(str(int(number)) if float(number).is_integer() else f"{number:g}")
    return allow
