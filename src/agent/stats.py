"""Read-only journal analytics (spec 14.1, 14.2).

Everything here is computed from closed paper positions that actually exist.
Nothing is fabricated: a metric that is undefined for the sample (profit
factor with no losses, drawdown with no path) is reported as None, not as a
flattering number.

Sample-size honesty is enforced structurally: every cell carries a label from
the frozen thresholds (n<30 unproven, 30-79 tentative, >=80 eligible for
review) and `is_edge` is never True below the eligible threshold, regardless
of how good the mean R looks.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

UNPROVEN = "unproven"
TENTATIVE = "tentative"
ELIGIBLE = "eligible"

MIN_N_TENTATIVE = 30
MIN_N_ELIGIBLE = 80

BOOK_CODE_ONLY = "CODE_ONLY"
BOOK_CODE_PLUS_LLM = "CODE_PLUS_LLM"


def sample_label(n: int) -> str:
    """Frozen 14.1 thresholds. These are process thresholds, not statistical laws."""
    if n < MIN_N_TENTATIVE:
        return UNPROVEN
    if n < MIN_N_ELIGIBLE:
        return TENTATIVE
    return ELIGIBLE


@dataclass(frozen=True)
class Cell:
    key: dict[str, Any]
    n: int
    mean_r: float | None = None
    std_r: float | None = None
    median_r: float | None = None
    win_rate: float | None = None
    avg_win_r: float | None = None
    avg_loss_r: float | None = None
    profit_factor: float | None = None
    expectancy_r: float | None = None
    max_dd_r: float | None = None
    avg_mfe_r: float | None = None
    avg_mae_r: float | None = None
    avg_bars_held: float | None = None
    label: str = UNPROVEN

    @property
    def is_edge(self) -> bool:
        """An edge claim requires an eligible sample AND a positive mean.

        A tiny positive sample is never an edge; that is the whole point of
        the frozen thresholds.
        """
        return self.label == ELIGIBLE and (self.mean_r or 0) > 0

    def to_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items()}
        data["is_edge"] = self.is_edge
        return data


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def max_drawdown_r(returns: Sequence[float]) -> float | None:
    """Peak-to-trough drawdown of the cumulative R path (spec 14.1 max_dd_r_sumpath)."""
    if not returns:
        return None
    cumulative = 0.0
    peak = 0.0
    worst = 0.0
    for r in returns:
        cumulative += r
        peak = max(peak, cumulative)
        worst = min(worst, cumulative - peak)
    return abs(worst)


def compute_cell(returns: Sequence[float], *, key: Mapping[str, Any] | None = None,
                 mfe: Sequence[float] = (), mae: Sequence[float] = (),
                 bars_held: Sequence[float] = ()) -> Cell:
    """Build one evaluation cell from realized R values. Undefined stays None."""
    values = [float(r) for r in returns]
    n = len(values)
    if n == 0:
        return Cell(key=dict(key or {}), n=0, label=UNPROVEN)

    wins = [r for r in values if r > 0]
    losses = [r for r in values if r < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    # Profit factor is undefined without losses; reporting "infinite" would be a lie.
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None

    return Cell(
        key=dict(key or {}),
        n=n,
        mean_r=statistics.fmean(values),
        std_r=statistics.stdev(values) if n > 1 else None,
        median_r=statistics.median(values),
        win_rate=len(wins) / n,
        avg_win_r=_mean(wins),
        avg_loss_r=_mean(losses),
        profit_factor=profit_factor,
        expectancy_r=statistics.fmean(values),
        max_dd_r=max_drawdown_r(values),
        avg_mfe_r=_mean([float(x) for x in mfe]),
        avg_mae_r=_mean([float(x) for x in mae]),
        avg_bars_held=_mean([float(x) for x in bars_held]),
        label=sample_label(n),
    )


@dataclass
class CellGroup:
    setup_id: str
    code_only: Cell
    code_plus_llm: Cell
    combined: Cell


def _rows_to_cell(rows: Sequence[Mapping[str, Any]], key: Mapping[str, Any]) -> Cell:
    return compute_cell(
        [r["realized_r"] for r in rows],
        key=key,
        mfe=[r["mfe_r"] for r in rows if r.get("mfe_r") is not None],
        mae=[r["mae_r"] for r in rows if r.get("mae_r") is not None],
        bars_held=[r["bars_held"] for r in rows if r.get("bars_held") is not None],
    )


def split_by_llm(rows: Sequence[Mapping[str, Any]], *, setup_id: str) -> CellGroup:
    """CODE_ONLY vs CODE_PLUS_LLM on the same journal (spec 14.2)."""
    code_only = [r for r in rows if not r.get("llm_involved")]
    with_llm = [r for r in rows if r.get("llm_involved")]
    return CellGroup(
        setup_id=setup_id,
        code_only=_rows_to_cell(code_only, {"setup_id": setup_id, "book": BOOK_CODE_ONLY}),
        code_plus_llm=_rows_to_cell(with_llm, {"setup_id": setup_id, "book": BOOK_CODE_PLUS_LLM}),
        combined=_rows_to_cell(rows, {"setup_id": setup_id, "book": "ALL"}),
    )


# ---------------------------------------------------------------- data access

def fetch_closed_outcomes(conn, *, setup_id: str | None = None) -> list[dict[str, Any]]:
    """Closed paper positions only. Open positions have no realized R yet.

    Filtering on status='CLOSED' AND realized_r IS NOT NULL is what keeps an
    in-flight position's eventual outcome from leaking into today's numbers.
    """
    sql = """SELECT i.setup_id, i.asset, i.timeframe, i.llm_involved,
                    p.realized_r, p.mfe_r, p.mae_r, p.bars_held, p.outcome_class, p.closed_at
             FROM paper_positions p JOIN ideas i ON i.id = p.idea_id
             WHERE p.status = 'CLOSED' AND p.realized_r IS NOT NULL"""
    params: list[Any] = []
    if setup_id:
        sql += " AND i.setup_id = %s"
        params.append(setup_id)
    sql += " ORDER BY p.closed_at ASC"
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return [
        {"setup_id": r[0], "asset": r[1], "timeframe": r[2], "llm_involved": bool(r[3]),
         "realized_r": float(r[4]), "mfe_r": float(r[5]) if r[5] is not None else None,
         "mae_r": float(r[6]) if r[6] is not None else None,
         "bars_held": int(r[7]) if r[7] is not None else None,
         "outcome_class": r[8], "closed_at": r[9]}
        for r in rows
    ]


def build_report(rows: Sequence[Mapping[str, Any]], *, setup_id: str | None = None) -> list[CellGroup]:
    setups = sorted({r["setup_id"] for r in rows}) if not setup_id else [setup_id]
    return [split_by_llm([r for r in rows if r["setup_id"] == s], setup_id=s) for s in setups]


# ---------------------------------------------------------------- rendering

def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.{digits}f}"


def format_report(groups: Sequence[CellGroup], *, setup_id: str | None = None) -> str:
    if not groups or all(group.combined.n == 0 for group in groups):
        scope = f" for setup {setup_id}" if setup_id else ""
        return (f"No closed paper outcomes{scope} yet. Nothing to report — "
                "statistics appear once positions close.")
    lines = ["STATS (closed paper positions only)"]
    for group in groups:
        combined = group.combined
        if combined.n == 0:
            continue
        lines.append("")
        lines.append(f"{group.setup_id}: n={combined.n} [{combined.label.upper()}]")
        lines.append(
            f"  mean R {_fmt(combined.mean_r)} | median {_fmt(combined.median_r)} | "
            f"win {_fmt((combined.win_rate or 0) * 100, 1)}%"
        )
        lines.append(
            f"  PF {_fmt(combined.profit_factor)} | maxDD {_fmt(combined.max_dd_r)}R | "
            f"MFE {_fmt(combined.avg_mfe_r)}R | MAE {_fmt(combined.avg_mae_r)}R"
        )
        lines.append(
            f"  CODE_ONLY n={group.code_only.n} meanR {_fmt(group.code_only.mean_r)} | "
            f"CODE+LLM n={group.code_plus_llm.n} meanR {_fmt(group.code_plus_llm.mean_r)}"
        )
        if not combined.is_edge:
            lines.append(f"  NOT AN EDGE: sample is {combined.label}; these are hypotheses, not results.")
    lines.append("")
    lines.append(f"Labels: n<{MIN_N_TENTATIVE} unproven, {MIN_N_TENTATIVE}-{MIN_N_ELIGIBLE - 1} tentative, "
                 f">={MIN_N_ELIGIBLE} eligible for review.")
    return "\n".join(lines)


def format_stats_command(conn, *, setup_id: str | None = None) -> str:
    """Backing implementation for /stats and /stats [setup]."""
    rows = fetch_closed_outcomes(conn, setup_id=setup_id)
    return format_report(build_report(rows, setup_id=setup_id), setup_id=setup_id)
