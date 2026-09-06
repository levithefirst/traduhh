"""OpenAI-compatible LLM client for packet review (spec 11.1, 11.5).

Provider-neutral: the base URL, key and model all come from config, so any
OpenAI-compatible endpoint works and no vendor is hard-coded. One repair
retry on schema failure, then the caller resolves to a safe decision.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Mapping

import httpx

from agent.llm.packet import canonical_json
from agent.llm.schema import DECISION_SCHEMA, LLMDecision, SchemaError, parse_decision
from agent.timeutil import require_utc, utc_now

LOGGER = logging.getLogger(__name__)

LLM_TIMEOUT_S = 45.0
MAX_CALLS_PER_HOUR = 30
MAX_CALLS_PER_DAY = 200

SYSTEM_PROMPT = """You review a pre-computed trading candidate. You are a reviewer, not a trader.

Process rules:
- NO TRADE is the default. The scanner running is not a reason to trade.
- Use ONLY levels and numbers present in the packet. Never invent or infer a price.
- Never compute indicators, sizes, R multiples, or ATR yourself. They are given.
- Never claim news caused price. News is a flag, not a cause.
- A small historical sample is unproven, not an edge.
- If the packet is incomplete or contradictory, return NO_TRADE.
- Hard gates already passed deterministically; you cannot override them, and
  agreeing does not make the candidate profitable.

Output STRICT JSON only, matching this schema exactly, with no prose outside it:
{"schema":"agent.llm_decision.v1","recommendation":"TAKE|WAIT|NO_TRADE",
"agree_with_code":true,"contradictions":[],"thesis":"<= 80 words, packet facts only",
"invalidation_restated":"must cite the packet stop","news_causal_claim":false,
"used_invented_level":false,"confidence":0.0,"what_would_change_decision":""}"""

OUTPUT_SCHEMA_TEXT = DECISION_SCHEMA


def prompt_version_id(*, system_prompt: str = SYSTEM_PROMPT, output_schema: str = OUTPUT_SCHEMA_TEXT,
                      model: str) -> str:
    """spec 14.4: 'pv_' + first 12 of sha256(system_prompt + output_schema + model)."""
    digest = hashlib.sha256((system_prompt + output_schema + model).encode()).hexdigest()
    return "pv_" + digest[:12]


class LLMError(RuntimeError):
    pass


class LLMUnavailable(LLMError):
    """Transport failure, timeout, 5xx, or budget exhaustion."""


class RateBudget:
    """30 calls/hour and 200/day (spec 11.1). Exceeding it is WAIT, never TAKE."""

    def __init__(self, *, per_hour: int = MAX_CALLS_PER_HOUR, per_day: int = MAX_CALLS_PER_DAY,
                 clock: Callable[[], Any] = utc_now):
        self._per_hour = per_hour
        self._per_day = per_day
        self._clock = clock
        self._calls: deque = deque()

    def _prune(self, now) -> None:
        while self._calls and now - self._calls[0] >= timedelta(days=1):
            self._calls.popleft()

    def allow(self) -> bool:
        now = require_utc(self._clock())
        self._prune(now)
        last_hour = sum(1 for ts in self._calls if now - ts < timedelta(hours=1))
        return last_hour < self._per_hour and len(self._calls) < self._per_day

    def record(self) -> None:
        self._calls.append(require_utc(self._clock()))


@dataclass(frozen=True)
class LLMResult:
    decision: LLMDecision | None
    error: str | None
    raw: str | None
    request: dict[str, Any]
    latency_ms: int
    model: str

    @property
    def valid(self) -> bool:
        return self.decision is not None


class LLMClient:
    def __init__(self, *, base_url: str, api_key: str, model: str,
                 client: httpx.Client | None = None, budget: RateBudget | None = None,
                 timeout_s: float = LLM_TIMEOUT_S, clock: Callable[[], float] = time.monotonic):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._owned = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout_s, connect=8.0))
        self._budget = budget or RateBudget()
        self._clock = clock

    @property
    def model(self) -> str:
        return self._model

    @property
    def prompt_version_id(self) -> str:
        return prompt_version_id(model=self._model)

    def close(self) -> None:
        if self._owned:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def _messages(self, packet: Mapping[str, Any], repair_errors: str | None = None) -> list[dict[str, str]]:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": canonical_json(packet)},
        ]
        if repair_errors:
            messages.append({
                "role": "user",
                "content": ("Your previous reply failed validation: " + repair_errors +
                            ". Reply again with strict JSON matching the schema and nothing else."),
            })
        return messages

    def _post(self, messages: list[dict[str, str]]) -> str:
        body = {"model": self._model, "messages": messages, "temperature": 0,
                "response_format": {"type": "json_object"}}
        try:
            response = self._client.post(
                f"{self._base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"llm_transport_error:{type(exc).__name__}") from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise LLMUnavailable(f"llm_http_{response.status_code}")
        if response.status_code >= 400:
            raise LLMUnavailable(f"llm_http_{response.status_code}")
        try:
            payload = response.json()
            return payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMUnavailable("llm_malformed_envelope") from exc

    def review(self, packet: Mapping[str, Any]) -> LLMResult:
        """One review with at most one repair retry. Never raises to the caller."""
        started = self._clock()
        request = {"model": self._model, "packet": dict(packet)}
        if not self._budget.allow():
            return LLMResult(None, "llm_budget", None, request, 0, self._model)

        raw: str | None = None
        error: str | None = None
        for attempt in (0, 1):
            try:
                self._budget.record()
                raw = self._post(self._messages(packet, error if attempt else None))
            except LLMUnavailable as exc:
                LOGGER.warning("llm_unavailable", extra={"event": "llm_unavailable", "error": str(exc)})
                return LLMResult(None, "llm_unavailable", None, request,
                                 int((self._clock() - started) * 1000), self._model)
            try:
                decision = parse_decision(raw)
            except SchemaError as exc:
                error = str(exc)
                LOGGER.warning("llm_schema_invalid", extra={"event": "llm_schema_invalid", "attempt": attempt + 1})
                continue
            return LLMResult(decision, None, raw, request,
                             int((self._clock() - started) * 1000), self._model)
        return LLMResult(None, "llm_invalid_json", raw, request,
                         int((self._clock() - started) * 1000), self._model)


def persist_review(conn, *, idea_id: str, result: LLMResult) -> None:
    """Store the review in the existing llm_reviews table (spec 5.8)."""
    import uuid

    from agent.timeutil import utc_now as _now

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO llm_reviews(id, idea_id, request, response_raw, response_json,
                                           valid, error, model, latency_ms, created_at)
                   VALUES (%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,%s,%s,%s)""",
                (
                    str(uuid.uuid4()), idea_id,
                    json.dumps(result.request, separators=(",", ":"), default=str),
                    result.raw,
                    json.dumps(result.decision.to_dict(), separators=(",", ":")) if result.decision else None,
                    result.valid, result.error, result.model, result.latency_ms, _now(),
                ),
            )


def ensure_prompt_version(conn, *, model: str) -> str:
    """Register the frozen prompt version row (spec 5.8 prompt_version)."""
    pv_id = prompt_version_id(model=model)
    template_hash = hashlib.sha256((SYSTEM_PROMPT + OUTPUT_SCHEMA_TEXT).encode()).hexdigest()
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO prompt_version(id, created_at, template_hash, model, notes)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (id) DO UPDATE SET template_hash=EXCLUDED.template_hash, model=EXCLUDED.model""",
                (pv_id, utc_now(), template_hash, model, "frozen reviewer prompt; packet-only levels"),
            )
    return pv_id
