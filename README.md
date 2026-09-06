# Trading Agent

Paper/research MVP foundation for the private AI crypto trading agent.

The implementation follows `handbook/Trading_Agent_System_Specification.pdf` and keeps both handbook PDFs frozen under `handbook/`.

## Implemented through Part 18 Step 4

Step 1 provides repository/configuration, Docker Compose, PostgreSQL migration foundation, the complete domain schema, indexes, startup checks, and foundation tests.

Step 2 provides only public Hyperliquid market-data ingestion:

- raw HTTP `POST /info` client
- `candleSnapshot` normalization and persistence
- `metaAndAssetCtxs` context normalization and persistence
- `l2Book` normalization, L1/depth summaries, persistence, and seven-day retention
- BTC 1h historical backfill of 3,000 bars
- BTC, ETH, SOL only
- 15m, 1h, 4h only
- UTC timestamp normalization
- restart-safe PostgreSQL upserts
- malformed/empty/unexpected response validation
- retry policy for 429, 5xx, timeouts, and connection failures
- closed-bar OHLC anomaly auditing without rewriting old closed values after the two-interval threshold

Step 3 adds only the infrastructure/data-integrity layer:

- deterministic UTC candle-gap detection after the specification's two-interval delay
- stale context/book checks at 60s, missing-mark checks, candle boundary/shape checks, BTC/ETH 15m jump checks, and venue-clock skew checks
- durable `job_runs` claim/finish/recovery helpers using the frozen `(job_name, scheduled_for)` unique key
- overlap protection: an existing RUNNING occurrence is skipped, not queued
- failed occurrences remain FAILED and may be retried for the same scheduled key; worker restart marks RUNNING rows FAILED before scheduling resumes
- JSON stdout logging and durable audit entries for scheduler skips/failures and Hyperliquid-down transitions
- APScheduler skeleton for the Step 2-compatible market-data polls and 30s integrity job only

Step 3 does not implement features, regime, setups, gates, paper trading, LLM, Telegram, or execution. The specification's later `scan_event` / `scan_events` reference remains unresolved because §5 does not define its schema, so no such table is invented.

Step 4 adds only deterministic features and regime classification:

- the exact §7.1 feature vector only
- 120-closed-bar warmup before feature emission
- UTC-safe closed-bar calculations with no future-candle or future-context access
- N=2 confirmed swings, structure grammar, previous-day levels, book/context derivatives, and required return/volatility statistics
- deterministic primary regimes `TREND_UP`, `TREND_DOWN`, `RANGE`, `UNKNOWN` plus the specified secondary flags
- frozen event-window handling for `EVENT_HIGH`
- idempotent `feature_snapshots` and `regime_snapshots` upserts using the existing unique keys
- live feature/regime persistence is performed immediately after each existing closed-candle ingestion job; no new scheduler architecture or Step 5 pipeline is introduced

The feature implementation uses explicit pandas/numpy calculations for the frozen formulas. The existing `pandas-ta` dependency remains declared by the frozen project dependency set, but it could not be installed in the current offline verification environment.

## Hyperliquid interface

The client uses the frozen public mainnet info endpoint from the specification:

`POST https://api.hyperliquid.xyz/info`

Step 2 uses only `candleSnapshot`, `metaAndAssetCtxs`, and `l2Book`.

Request retries are limited to three attempts with 0.5s, 1s, and 2s backoff, and the client uses an 8s connect timeout and 15s read timeout.

## Local execution

The repository expects Python 3.11+ and PostgreSQL 16. The existing Docker Compose stack remains the runtime path for the database and worker.

The one-shot BTC 1h backfill entrypoint is:

```bash
python scripts/backfill_candles.py
```

It fetches the specification-defined 3,000 BTC 1h bars and persists them through the existing migration/database layer.

## Tests

Run:

```bash
pytest
```

The PostgreSQL integration test runs only when a real PostgreSQL connection is available. It is skipped when PostgreSQL is unavailable rather than replaced with a fake database.

## Frozen boundaries

The handbook and system specification are source-of-truth artifacts. They are not executable strategy logic and must not be modified.

`NO_TRADE` remains the governing default. No wallet keys, signing, order placement, testnet execution, or mainnet execution exist in the MVP.

The specification's later `scan_event` / `scan_events` reference remains unresolved because its schema is not defined in §5. Step 2 does not invent that table.

## Part 18 Step 5: reconstructed

Step 5 is a source reconstruction from the recovered Step 4 tree and frozen v1 specification. The original Step 5 commit was not recovered. This reconstruction adds only the three specified setup detectors, deterministic geometry/cost/gate evaluation, deterministic idea identity, existing `ideas` persistence, and closed-bar scan integration. No Step 6 lifecycle or Step 7 Telegram layer is included.

The specification requires a 4h regime for 15m/1h gating but does not define an exact directional cross-timeframe acceptance predicate. The reconstruction therefore records the latest persisted 4h regime and conservatively blocks when that regime is unavailable/UNKNOWN/PANIC/EVENT_HIGH, without inventing a directional alignment rule.

## Part 18 Step 6: paper monitoring and outcomes

Step 6 adds only the frozen Part 13 paper contract on top of the existing schema and scheduler:

- immediate hypothetical entry fill for a `TRADE_PAPER` idea (`paper.py`): `min(ask1, close)` / `max(bid1, close)` plus modeled slippage, falling back to `close * (1 ± slip_bps/1e4)` when the book is missing
- adverse stop-fill slippage; the Step 5 target level itself with no added slippage; time-stop and halt-flatten fills at the bar close
- deterministic, restart-safe bar walking (`monitor.py`): MFE/MAE and the first exit condition are recomputed from stored closed candles on every tick rather than mutated incrementally, so a duplicate tick or a crash mid-tick can never double-count or double-exit a position; same-candle target+stop resolves STOP first
- realized R, fees, and funding computed from the actual modeled fills, never from the idealized signal entry; funding is summed from real hourly `asset_ctx` rows with the correct directional sign, and any hour with no funding data is flagged (`funding_missing`) rather than assumed zero
- durable `paper_equity` snapshots (equity, open risk, drawdown from a peak tracked in `system_state`)
- migration `0002_step6_paper_contract.sql` adds the uniqueness the spec's restart-recovery rule requires: one paper position per idea, one fill per `(position, kind)`
- `monitor_open` (15s) and `equity_snap` (1 min) run on the existing Step 3 APScheduler/worker process; no new worker, service, or queue was introduced

Step 6 does not itself decide anything: `paper.py` only reacts to ideas that already carry `decision='TRADE_PAPER'`. That transition is made by the Step 8 review layer, which the pipeline now performs after every hard gate passes.

`tests/unit/test_step6.py` covers the entry/stop/exit fill math, MFE/MAE, same-candle stop-wins, time-stop, halt-flatten, no-lookahead, realized R, funding (including the missing-data flag), and equity/drawdown as pure, DB-free functions. `tests/integration/test_step6_postgres.py` covers position creation, duplicate prevention, and restart idempotency against a real PostgreSQL instance; like the Step 1 integration test it is skipped (not faked) when `DATABASE_URL` is unset, which is the case in this environment.

No execution or wallet/order-signing code exists.

## Part 18 Steps 7-11

**Step 7 - Telegram control surface (`telegram/bot.py`, `telegram/formatters.py`, `circuit.py`).** The frozen Part 12 command set on an allowlist-only long-poll listener running as a thread inside the same worker. An update from an unknown chat id gets no reply at all; commands are rate limited to 10/min/chat. `circuit.py` holds mode, halt/resume and the Part 15.3 loss breakers: `/halt` stops new `TRADE_PAPER` ideas while the Step 6 monitor keeps flattening open positions, and `/resume` refuses unless integrity is clean. No command can change setup parameters, risk fraction, timeframes or the universe - those need a code change and a new `strategy_version`, so no such handler exists. Raw Bot API HTTPS is used rather than a framework wrapper, per the spec's rule to pick the simpler option; the token is never logged (httpx errors are logged by type, because their string form embeds the token URL).

**Step 8 - LLM review layer (`llm/packet.py`, `llm/schema.py`, `llm/client.py`).** The deterministic pipeline stays authoritative and the model is a reviewer that can only hold a decision back. It is consulted solely when every hard gate has passed, behind a `code_would_take` guard. The Part 11.2 packet is built from evidence the pipeline already computed, with features restricted to an allowlisted subset of 7.1 so nothing stray can leave the process. Responses are strictly validated against 11.4, and the 11.5 veto rules apply: a veto, a disagreement with code, a self-declared or prose-detected invented level, two schema failures, a timeout or an exhausted budget all resolve to `NO_TRADE` or `WAIT`, never to a trade. The client is provider-neutral (base URL, key and model come from config). Migration 0003 adds `code_decision_before_llm`, `code_would_take` and `llm_involved` plus a `book` flag, keeping the CODE_ONLY baseline comparable with CODE_PLUS_LLM.

**Step 9 - Paper trade alerts (`telegram/alerts.py`).** One Part 12.3 message per idea reaching `TRADE_PAPER`, plus paper fill and paper close follow-ups. Notification only - nothing places, signs or simulates an order. `NO_TRADE`, `WAIT`, gate failures and LLM vetoes are journaled but never alerted. De-duplication is durable: the key is derived from the event itself and claimed in `alerts_sent` (migration 0004) before the send, so a restart mid-dispatch cannot re-send and a failed send cannot later become a duplicate.

**Step 10 - Statistics (`stats.py`).** Part 14.1 cells over closed paper positions, with the 14.2 CODE vs LLM split. Sample-size honesty is structural: every cell carries a label from the frozen thresholds (n<30 unproven, 30-79 tentative, >=80 eligible) and `is_edge` can never be True below the eligible threshold however good the mean R looks. Undefined metrics stay undefined - profit factor with no losses is `None`, not infinity. Only `status='CLOSED' AND realized_r IS NOT NULL` rows are read, so an open position's eventual outcome cannot leak into today's numbers.

**Step 11 - Learned-rule proposer (`learning.py`).** Writes `learned_rules` rows with `status='proposed'` and nothing else. It cannot promote a rule, touch `strategy_version`, change a detector, risk, the universe or a timeframe, or influence a trade. A setup needs 30 closed outcomes, each side of a predicate split needs 10, and the mean-R delta must reach 0.25 before anything is written; below that the job does nothing rather than inventing a rule. Each proposal carries baseline and matched n/mean R, a naive confidence interval labelled as naive, and the idea ids behind it. Identity is deterministic (`uuid5` of the rule key), and the upsert refreshes a row only while it is still `proposed`, leaving operator decisions untouched. Runs nightly as `daily_stats` at 00:05 UTC on the existing scheduler.

All five steps run in the one worker process. No new service, queue, or database was introduced.

`tests/unit/test_integration_flow.py` pins the cross-step invariants: every hard gate failure is `NO_TRADE`, the LLM is unreachable without a gated pass and cannot override a gate, LLM failures default safely, `NO_TRADE` stays a stored first-class outcome, only `TRADE_PAPER` is announced, no module contains order or signing code, the proposer is isolated from the decision path, every durable artifact (ideas, positions, fills, alerts, proposals, job runs) is idempotently keyed, and no future bar or unclosed outcome leaks backward.
