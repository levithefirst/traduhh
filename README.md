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

Step 6 does not change how an idea's `decision` becomes `TRADE_PAPER` — that transition is the Step 8 LLM veto layer, which remains unbuilt. `paper.py` only reacts to ideas that already carry that decision, so it is exercised today through the Step 6 test suite's synthetic `TRADE_PAPER` ideas and is ready to receive real ones once Step 8 exists.

`tests/unit/test_step6.py` covers the entry/stop/exit fill math, MFE/MAE, same-candle stop-wins, time-stop, halt-flatten, no-lookahead, realized R, funding (including the missing-data flag), and equity/drawdown as pure, DB-free functions. `tests/integration/test_step6_postgres.py` covers position creation, duplicate prevention, and restart idempotency against a real PostgreSQL instance; like the Step 1 integration test it is skipped (not faked) when `DATABASE_URL` is unset, which is the case in this environment.

No Step 7 Telegram layer, LLM integration, execution, or wallet/order-signing code exists.
