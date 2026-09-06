"""Learned-rule proposer (spec 14.3). Proposals only.

This module writes rows with status='proposed' and nothing else. It cannot
promote a rule, cannot touch strategy_version, cannot change a detector,
risk fraction, the universe, or a timeframe, and cannot place or influence a
trade. Promotion is an operator action outside this MVP.

A proposal is only written when the evidence requirements are met. If the
sample is too thin the job does nothing at all — it never invents a rule or
a statistic to have something to show.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from agent.stats import MIN_N_TENTATIVE, compute_cell
from agent.timeutil import utc_now
from agent.versioning import STRATEGY_VERSION_ID

STATUS_PROPOSED = "proposed"

# A proposal needs enough evidence on BOTH sides of the split to be worth an
# operator's attention, and a difference large enough not to be noise at this
# sample size. These are process thresholds, not statistical proof.
MIN_N_FOR_PROPOSAL = MIN_N_TENTATIVE
MIN_N_PER_BRANCH = 10
MIN_MEAN_R_DELTA = 0.25

RULE_NAMESPACE = uuid.UUID("6f1d1f5a-7d9e-5a3c-9c2f-0f5f8f2a4d11")

# Feature predicates the proposer may consider. Adding to this list is a code
# change, exactly like changing a detector, so the search space cannot drift.
CANDIDATE_PREDICATES = (
    ("adx_14_ge_25", lambda f: _num(f.get("adx_14")) is not None and _num(f.get("adx_14")) >= 25),
    ("atr_pct_100_ge_1_2", lambda f: _num(f.get("atr_pct_100")) is not None and _num(f.get("atr_pct_100")) >= 1.2),
    ("vol_ratio_ge_1_2", lambda f: _num(f.get("vol_ratio")) is not None and _num(f.get("vol_ratio")) >= 1.2),
    ("rsi_14_ge_60", lambda f: _num(f.get("rsi_14")) is not None and _num(f.get("rsi_14")) >= 60),
)


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


@dataclass(frozen=True)
class Proposal:
    rule_key: str
    definition: dict[str, Any]
    setup_id: str
    regime: str | None
    n: int
    mean_r: float
    ci_low: float | None
    ci_high: float | None
    validation_period: str
    strategy_version_id: str
    status: str = STATUS_PROPOSED

    def to_row(self) -> dict[str, Any]:
        return {
            "rule_key": self.rule_key, "definition": self.definition, "setup_id": self.setup_id,
            "regime": self.regime, "n": self.n, "mean_r": self.mean_r, "ci_low": self.ci_low,
            "ci_high": self.ci_high, "validation_period": self.validation_period,
            "strategy_version_id": self.strategy_version_id, "status": self.status,
        }


def rule_key(*, setup_id: str, regime: str | None, predicate: str, strategy_version_id: str) -> str:
    """Deterministic identity: the same evidence shape always yields the same key."""
    payload = json.dumps(
        {"setup_id": setup_id, "regime": regime, "predicate": predicate,
         "strategy_version_id": strategy_version_id},
        sort_keys=True, separators=(",", ":"),
    )
    return "lr_" + hashlib.sha256(payload.encode()).hexdigest()[:16]


def rule_id(rule_key_value: str) -> str:
    """Stable uuid for the row, so a re-run updates rather than duplicates."""
    return str(uuid.uuid5(RULE_NAMESPACE, rule_key_value))


def naive_confidence_interval(values: Sequence[float]) -> tuple[float | None, float | None]:
    """Mean +/- 1.96 * naive standard error. Labelled naive because it is."""
    cell = compute_cell(list(values))
    if cell.n < 2 or cell.mean_r is None or cell.std_r is None:
        return None, None
    se = cell.std_r / (cell.n ** 0.5)
    return cell.mean_r - 1.96 * se, cell.mean_r + 1.96 * se


def propose_from_outcomes(rows: Sequence[Mapping[str, Any]], *,
                          strategy_version_id: str = STRATEGY_VERSION_ID,
                          validation_period: str = "all") -> list[Proposal]:
    """Pure proposal generation over closed outcomes.

    Returns an empty list whenever the evidence requirements are not met.
    Nothing here mutates strategy parameters or detector behaviour.
    """
    proposals: list[Proposal] = []
    setups = sorted({str(r.get("setup_id")) for r in rows if r.get("setup_id")})
    for setup_id in setups:
        setup_rows = [r for r in rows if r.get("setup_id") == setup_id]
        if len(setup_rows) < MIN_N_FOR_PROPOSAL:
            continue  # too thin to say anything; say nothing
        regimes = sorted({str(r.get("regime_primary")) for r in setup_rows if r.get("regime_primary")})
        for regime in (regimes or [None]):
            scope = [r for r in setup_rows if regime is None or r.get("regime_primary") == regime]
            if len(scope) < MIN_N_FOR_PROPOSAL:
                continue
            baseline = compute_cell([float(r["realized_r"]) for r in scope])
            for predicate_name, predicate in CANDIDATE_PREDICATES:
                matched = [r for r in scope if predicate(r.get("features") or {})]
                unmatched = [r for r in scope if not predicate(r.get("features") or {})]
                if len(matched) < MIN_N_PER_BRANCH or len(unmatched) < MIN_N_PER_BRANCH:
                    continue
                matched_values = [float(r["realized_r"]) for r in matched]
                matched_cell = compute_cell(matched_values)
                if matched_cell.mean_r is None or baseline.mean_r is None:
                    continue
                delta = matched_cell.mean_r - baseline.mean_r
                if abs(delta) < MIN_MEAN_R_DELTA:
                    continue
                ci_low, ci_high = naive_confidence_interval(matched_values)
                key = rule_key(setup_id=setup_id, regime=regime, predicate=predicate_name,
                               strategy_version_id=strategy_version_id)
                proposals.append(Proposal(
                    rule_key=key,
                    definition={
                        "setup_id": setup_id,
                        "regime": regime,
                        "feature_predicate": predicate_name,
                        "comparison": "mean_r of matching subset vs cell baseline",
                        "baseline_n": baseline.n,
                        "baseline_mean_r": baseline.mean_r,
                        "matched_n": matched_cell.n,
                        "matched_mean_r": matched_cell.mean_r,
                        "unmatched_n": len(unmatched),
                        "mean_r_delta": delta,
                        "evidence_idea_ids": sorted(str(r["idea_id"]) for r in matched if r.get("idea_id")),
                        "note": "proposal only; requires operator review and out-of-sample validation",
                    },
                    setup_id=setup_id,
                    regime=regime,
                    n=matched_cell.n,
                    mean_r=matched_cell.mean_r,
                    ci_low=ci_low,
                    ci_high=ci_high,
                    validation_period=validation_period,
                    strategy_version_id=strategy_version_id,
                ))
    return sorted(proposals, key=lambda p: p.rule_key)


def fetch_outcomes_for_learning(conn) -> list[dict[str, Any]]:
    """Closed paper outcomes joined to the evidence that produced them."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT i.id, i.setup_id, i.features, i.regime, p.realized_r
               FROM paper_positions p JOIN ideas i ON i.id = p.idea_id
               WHERE p.status = 'CLOSED' AND p.realized_r IS NOT NULL
               ORDER BY p.closed_at ASC"""
        )
        rows = cur.fetchall()
    out = []
    for idea_id, setup_id, features, regime, realized_r in rows:
        features = json.loads(features) if isinstance(features, str) else (features or {})
        regime = json.loads(regime) if isinstance(regime, str) else (regime or {})
        out.append({
            "idea_id": str(idea_id), "setup_id": setup_id, "features": features,
            "regime_primary": regime.get("label"), "realized_r": float(realized_r),
        })
    return out


def persist_proposals(conn, proposals: Sequence[Proposal]) -> int:
    """Insert proposals as status='proposed'. Re-running never duplicates.

    An existing row is refreshed in place only while it is still 'proposed';
    a rule an operator has already promoted, rejected or expired is left
    exactly as they left it.
    """
    written = 0
    with conn.transaction():
        with conn.cursor() as cur:
            for proposal in proposals:
                cur.execute(
                    """INSERT INTO learned_rules(id, rule_key, definition, setup_id, regime, n, mean_r,
                                                 ci_low, ci_high, validation_period, strategy_version_id, status)
                       VALUES (%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,'proposed')
                       ON CONFLICT (id) DO UPDATE SET
                           definition=EXCLUDED.definition, n=EXCLUDED.n, mean_r=EXCLUDED.mean_r,
                           ci_low=EXCLUDED.ci_low, ci_high=EXCLUDED.ci_high,
                           validation_period=EXCLUDED.validation_period
                       WHERE learned_rules.status = 'proposed'""",
                    (
                        rule_id(proposal.rule_key), proposal.rule_key,
                        json.dumps(proposal.definition, separators=(",", ":"), default=str),
                        proposal.setup_id, proposal.regime, proposal.n, proposal.mean_r,
                        proposal.ci_low, proposal.ci_high, proposal.validation_period,
                        proposal.strategy_version_id,
                    ),
                )
                written += cur.rowcount or 0
    return written


def run_learning_job(conn, *, settings=None) -> dict[str, int]:
    """Nightly proposer (spec 6.2 daily_stats window, 14.3 proposals only)."""
    rows = fetch_outcomes_for_learning(conn)
    proposals = propose_from_outcomes(rows)
    if not proposals:
        return {"outcomes": len(rows), "proposals": 0, "written": 0}
    written = persist_proposals(conn, proposals)
    return {"outcomes": len(rows), "proposals": len(proposals), "written": written}
