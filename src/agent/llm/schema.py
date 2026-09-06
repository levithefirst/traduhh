"""Strict LLM response validation and veto resolution (spec 11.4, 11.5).

The deterministic pipeline is authoritative. Everything here can only make
the outcome *more* conservative: the LLM may veto or downgrade, never
promote. Any parse failure, schema violation, invented level, or transport
error resolves to a safe decision, never to a trade.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

DECISION_SCHEMA = "agent.llm_decision.v1"
RECOMMENDATIONS = ("TAKE", "WAIT", "NO_TRADE")

TRADE_PAPER = "TRADE_PAPER"
WAIT = "WAIT"
NO_TRADE = "NO_TRADE"

REQUIRED_KEYS = (
    "schema", "recommendation", "agree_with_code", "contradictions", "thesis",
    "invalidation_restated", "news_causal_claim", "used_invented_level",
    "confidence", "what_would_change_decision",
)

_PRICE_TOKEN = re.compile(r"\d[\d,]*\.?\d*")


class SchemaError(ValueError):
    pass


@dataclass(frozen=True)
class LLMDecision:
    recommendation: str
    agree_with_code: bool
    contradictions: list[str]
    thesis: str
    invalidation_restated: str
    news_causal_claim: bool
    used_invented_level: bool
    confidence: float
    what_would_change_decision: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DECISION_SCHEMA,
            "recommendation": self.recommendation,
            "agree_with_code": self.agree_with_code,
            "contradictions": list(self.contradictions),
            "thesis": self.thesis,
            "invalidation_restated": self.invalidation_restated,
            "news_causal_claim": self.news_causal_claim,
            "used_invented_level": self.used_invented_level,
            "confidence": self.confidence,
            "what_would_change_decision": self.what_would_change_decision,
        }


def parse_decision(raw: str | Mapping[str, Any]) -> LLMDecision:
    """Parse and strictly validate a model response. Raises SchemaError."""
    if isinstance(raw, Mapping):
        body = dict(raw)
    else:
        try:
            body = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise SchemaError("response was not valid JSON") from exc
    if not isinstance(body, dict):
        raise SchemaError("response must be a JSON object")

    missing = [key for key in REQUIRED_KEYS if key not in body]
    if missing:
        raise SchemaError(f"missing required keys: {', '.join(missing)}")
    if body["schema"] != DECISION_SCHEMA:
        raise SchemaError(f"unexpected schema: {body['schema']!r}")
    if body["recommendation"] not in RECOMMENDATIONS:
        raise SchemaError(f"invalid recommendation: {body['recommendation']!r}")
    for key in ("agree_with_code", "news_causal_claim", "used_invented_level"):
        if not isinstance(body[key], bool):
            raise SchemaError(f"{key} must be a boolean")
    if not isinstance(body["contradictions"], list) or any(not isinstance(x, str) for x in body["contradictions"]):
        raise SchemaError("contradictions must be a list of strings")
    for key in ("thesis", "invalidation_restated", "what_would_change_decision"):
        if not isinstance(body[key], str):
            raise SchemaError(f"{key} must be a string")
    confidence = body["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise SchemaError("confidence must be a number")
    if not 0.0 <= float(confidence) <= 1.0:
        raise SchemaError("confidence must be within [0, 1]")
    if len(body["thesis"].split()) > 80:
        raise SchemaError("thesis exceeds 80 words")

    return LLMDecision(
        recommendation=body["recommendation"],
        agree_with_code=body["agree_with_code"],
        contradictions=[str(x) for x in body["contradictions"]],
        thesis=body["thesis"],
        invalidation_restated=body["invalidation_restated"],
        news_causal_claim=body["news_causal_claim"],
        used_invented_level=body["used_invented_level"],
        confidence=float(confidence),
        what_would_change_decision=body["what_would_change_decision"],
    )


def mentions_invented_price(text: str, allowlist: set[str]) -> bool:
    """True when prose cites a price-like number absent from the packet.

    Small integers (R multiples, bar counts, percentages) are ignored; only
    values large enough to be a quoted price are checked.
    """
    for token in _PRICE_TOKEN.findall(text or ""):
        cleaned = token.replace(",", "")
        try:
            number = float(cleaned)
        except ValueError:
            continue
        if number < 100:  # not price-shaped for this universe
            continue
        candidates = {
            cleaned,
            cleaned.rstrip("0").rstrip(".") if "." in cleaned else cleaned,
            f"{number:.2f}",
            f"{number:.1f}",
            str(int(number)) if number.is_integer() else f"{number:g}",
        }
        if not (candidates & allowlist):
            return True
    return False


@dataclass(frozen=True)
class ReviewOutcome:
    decision: str
    reasons: list[str] = field(default_factory=list)
    llm_confidence: float | None = None
    agreement: float = 1.0
    valid: bool = True


def resolve_after_llm(*, decision: LLMDecision | None, error: str | None, allowlist: set[str]) -> ReviewOutcome:
    """Apply the frozen 11.5 veto rules. Never upgrades a decision."""
    if decision is None:
        # Transport failure, timeout, or a second schema failure.
        reason = error or "llm_unavailable"
        safe = NO_TRADE if reason == "llm_invalid_json" else WAIT
        return ReviewOutcome(decision=safe, reasons=[reason], valid=False, agreement=0.0)

    reasons: list[str] = []
    if decision.news_causal_claim:
        # Strip the claim and flag it; this alone never forces NO_TRADE.
        reasons.append("llm_news_causal_claim_stripped")

    if decision.used_invented_level or mentions_invented_price(decision.thesis, allowlist):
        return ReviewOutcome(decision=NO_TRADE, reasons=reasons + ["llm_invented_level"],
                             llm_confidence=decision.confidence, agreement=0.0)
    if decision.recommendation == NO_TRADE:
        return ReviewOutcome(decision=NO_TRADE, reasons=reasons + ["llm_no_trade"],
                             llm_confidence=decision.confidence, agreement=0.0)
    if decision.recommendation == WAIT:
        return ReviewOutcome(decision=WAIT, reasons=reasons + ["llm_wait"],
                             llm_confidence=decision.confidence, agreement=0.0)
    if not decision.agree_with_code:
        return ReviewOutcome(decision=NO_TRADE, reasons=reasons + ["llm_disagrees_with_code"],
                             llm_confidence=decision.confidence, agreement=0.0)
    return ReviewOutcome(decision=TRADE_PAPER, reasons=reasons, llm_confidence=decision.confidence, agreement=1.0)


def final_confidence(*, regime_confidence: float, hist_n: int, agreement: float) -> float:
    """Spec 10 step 16: min(regime.conf, 0.6 if n<30 else 0.7) * llm.agreement."""
    ceiling = 0.6 if int(hist_n or 0) < 30 else 0.7
    return min(float(regime_confidence or 0.0), ceiling) * float(agreement)
