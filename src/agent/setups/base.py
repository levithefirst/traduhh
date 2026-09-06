from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class Detection:
    setup_id: str
    asset: str
    timeframe: str
    direction: str
    bar_open_time: datetime
    trigger_index: int
    entry: float
    stop: float
    targets: list[float]
    structural_reference: dict[str, Any]
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def finite(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def direction_ok(direction: str) -> bool:
    return direction in {"long", "short"}
