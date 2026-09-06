BEGIN;

CREATE TABLE candles (
    id bigserial,
    venue text,
    asset text,
    timeframe text,
    open_time timestamptz,
    close_time timestamptz,
    o numeric(38,18),
    h numeric(38,18),
    l numeric(38,18),
    c numeric(38,18),
    v numeric(38,18),
    n_trades int,
    source text,
    ingested_at timestamptz,
    UNIQUE (venue, asset, timeframe, open_time)
);

CREATE TABLE asset_ctx (
    id bigserial,
    venue text,
    asset text,
    ts timestamptz,
    mid numeric(38,18),
    mark numeric(38,18),
    oracle numeric(38,18),
    funding numeric(38,18),
    premium numeric(38,18),
    oi numeric(38,18),
    day_ntl_vlm numeric(38,18),
    impact_bid numeric(38,18),
    impact_ask numeric(38,18),
    prev_day_px numeric(38,18),
    raw jsonb,
    UNIQUE (venue, asset, ts)
);

CREATE TABLE book_snapshots (
    id bigserial,
    venue text,
    asset text,
    ts timestamptz,
    bid1 numeric(38,18),
    ask1 numeric(38,18),
    bid1_sz numeric(38,18),
    ask1_sz numeric(38,18),
    spread numeric(38,18),
    spread_bps numeric(38,18),
    bid_sz_5 numeric(38,18),
    ask_sz_5 numeric(38,18),
    imbalance_5 numeric(38,18),
    notional_to_10bps numeric(38,18),
    raw_top jsonb,
    UNIQUE (venue, asset, ts)
);

CREATE TABLE news_items (
    id bigserial,
    source text,
    external_id text,
    ts timestamptz,
    title text,
    url text,
    body text,
    assets text[],
    raw jsonb,
    UNIQUE (source, external_id)
);

CREATE TABLE calendar_events (
    id bigserial,
    ts_start timestamptz,
    ts_end timestamptz,
    name text,
    impact text CHECK (impact IN ('low', 'medium', 'high')),
    assets text[],
    source text
);

CREATE TABLE feature_snapshots (
    id bigserial,
    asset text,
    timeframe text,
    open_time timestamptz,
    features jsonb NOT NULL,
    computed_at timestamptz,
    UNIQUE (asset, timeframe, open_time)
);

CREATE TABLE regime_snapshots (
    id bigserial,
    asset text,
    timeframe text,
    open_time timestamptz,
    label text,
    secondary text[],
    confidence numeric(38,18),
    features_used jsonb,
    UNIQUE (asset, timeframe, open_time)
);

CREATE TABLE ideas (
    id uuid PRIMARY KEY,
    created_at timestamptz,
    asset text,
    timeframe text,
    direction text CHECK (direction IN ('long', 'short', 'none')),
    setup_id text,
    strategy_version_id text,
    prompt_version_id text,
    bar_open_time timestamptz,
    decision text,
    decision_reason text[],
    gates jsonb,
    geometry jsonb,
    costs jsonb,
    features jsonb,
    regime jsonb,
    ctx jsonb,
    book jsonb,
    news jsonb,
    calendar jsonb,
    hist_cell jsonb,
    llm_review jsonb,
    packet_hash text,
    data_quality jsonb,
    confidence numeric(38,18),
    UNIQUE (asset, timeframe, setup_id, bar_open_time, strategy_version_id)
);

CREATE TABLE paper_positions (
    id uuid PRIMARY KEY,
    idea_id uuid,
    asset text,
    direction text,
    tf text,
    status text,
    entry numeric(38,18),
    stop numeric(38,18),
    targets jsonb,
    size numeric(38,18),
    notional numeric(38,18),
    risk_cash numeric(38,18),
    opened_at timestamptz,
    closed_at timestamptz,
    exit_px numeric(38,18),
    exit_reason text,
    realized_r numeric(38,18),
    pnl_usd numeric(38,18),
    fees_usd numeric(38,18),
    funding_usd numeric(38,18),
    slip_usd numeric(38,18),
    mfe_r numeric(38,18),
    mae_r numeric(38,18),
    mfe_px numeric(38,18),
    mae_px numeric(38,18),
    bars_held int,
    outcome_class text,
    counterfactuals jsonb
);

CREATE TABLE paper_fills (
    id uuid PRIMARY KEY,
    position_id uuid,
    ts timestamptz,
    side text,
    px numeric(38,18),
    sz numeric(38,18),
    kind text,
    note text
);

CREATE TABLE paper_equity (
    ts timestamptz,
    equity numeric(38,18),
    open_risk numeric(38,18),
    drawdown_from_peak numeric(38,18)
);

CREATE TABLE llm_reviews (
    id uuid PRIMARY KEY,
    idea_id uuid,
    request jsonb,
    response_raw text,
    response_json jsonb,
    valid boolean,
    error text,
    model text,
    latency_ms int,
    created_at timestamptz
);

CREATE TABLE learned_rules (
    id uuid PRIMARY KEY,
    rule_key text,
    definition jsonb,
    setup_id text,
    regime text,
    n int,
    mean_r numeric(38,18),
    ci_low numeric(38,18),
    ci_high numeric(38,18),
    validation_period text,
    strategy_version_id text,
    status text CHECK (status IN ('proposed', 'rejected', 'promoted', 'expired'))
);

CREATE TABLE strategy_version (
    id text PRIMARY KEY,
    created_at timestamptz,
    params jsonb,
    code_git_sha text,
    notes text,
    frozen boolean
);

CREATE TABLE prompt_version (
    id text PRIMARY KEY,
    created_at timestamptz,
    template_hash text,
    model text,
    notes text
);

CREATE TABLE system_state (
    key text PRIMARY KEY,
    value jsonb,
    updated_at timestamptz
);

CREATE TABLE job_runs (
    id uuid PRIMARY KEY,
    job_name text,
    scheduled_for timestamptz,
    started_at timestamptz,
    finished_at timestamptz,
    status text,
    error text,
    stats jsonb,
    UNIQUE (job_name, scheduled_for)
);

CREATE TABLE audit_log (
    id bigserial PRIMARY KEY,
    ts timestamptz,
    actor text,
    action text,
    payload jsonb
);

CREATE INDEX candles_asset_timeframe_open_time_idx
    ON candles (asset, timeframe, open_time DESC);
CREATE INDEX asset_ctx_asset_ts_idx
    ON asset_ctx (asset, ts DESC);
CREATE INDEX book_snapshots_asset_ts_idx
    ON book_snapshots (asset, ts DESC);
CREATE INDEX feature_snapshots_asset_timeframe_open_time_idx
    ON feature_snapshots (asset, timeframe, open_time DESC);
CREATE INDEX regime_snapshots_asset_timeframe_open_time_idx
    ON regime_snapshots (asset, timeframe, open_time DESC);
CREATE INDEX ideas_created_at_idx
    ON ideas (created_at DESC);
CREATE INDEX ideas_asset_setup_decision_created_at_idx
    ON ideas (asset, setup_id, decision, created_at DESC);
CREATE INDEX ideas_packet_hash_idx
    ON ideas (packet_hash);
CREATE INDEX paper_positions_status_idx
    ON paper_positions (status);
CREATE INDEX paper_positions_idea_id_idx
    ON paper_positions (idea_id);
CREATE INDEX paper_positions_asset_status_idx
    ON paper_positions (asset, status);
CREATE INDEX news_items_ts_idx
    ON news_items (ts DESC);
CREATE INDEX news_items_assets_gin_idx
    ON news_items USING GIN (assets);
CREATE INDEX calendar_events_ts_start_ts_end_idx
    ON calendar_events (ts_start, ts_end);
CREATE INDEX learned_rules_status_setup_regime_idx
    ON learned_rules (status, setup_id, regime);

COMMIT;
