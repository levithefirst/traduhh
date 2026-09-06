from pathlib import Path


ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "src" / "agent" / "migrations" / "0001_init.sql"
SQL = MIGRATION.read_text(encoding="utf-8")

EXPECTED_TABLES = {
    "candles",
    "asset_ctx",
    "book_snapshots",
    "news_items",
    "calendar_events",
    "feature_snapshots",
    "regime_snapshots",
    "ideas",
    "paper_positions",
    "paper_fills",
    "paper_equity",
    "llm_reviews",
    "learned_rules",
    "strategy_version",
    "prompt_version",
    "system_state",
    "job_runs",
    "audit_log",
}


def test_all_spec_domain_tables_are_declared():
    for table in EXPECTED_TABLES:
        assert f"CREATE TABLE {table}" in SQL


def test_required_unique_constraints_are_declared():
    assert "UNIQUE (venue, asset, timeframe, open_time)" in SQL
    assert "UNIQUE (venue, asset, ts)" in SQL
    assert "UNIQUE (asset, timeframe, open_time)" in SQL
    assert "UNIQUE (asset, timeframe, setup_id, bar_open_time, strategy_version_id)" in SQL
    assert "UNIQUE (job_name, scheduled_for)" in SQL


def test_explicit_enum_checks_are_declared():
    assert "direction IN ('long', 'short', 'none')" in SQL
    assert "impact IN ('low', 'medium', 'high')" in SQL
    assert "status IN ('proposed', 'rejected', 'promoted', 'expired')" in SQL


def test_required_indexes_are_declared():
    required_fragments = [
        "ON candles (asset, timeframe, open_time DESC)",
        "ON asset_ctx (asset, ts DESC)",
        "ON book_snapshots (asset, ts DESC)",
        "ON feature_snapshots (asset, timeframe, open_time DESC)",
        "ON regime_snapshots (asset, timeframe, open_time DESC)",
        "ON ideas (created_at DESC)",
        "ON ideas (asset, setup_id, decision, created_at DESC)",
        "ON ideas (packet_hash)",
        "ON paper_positions (status)",
        "ON paper_positions (idea_id)",
        "ON paper_positions (asset, status)",
        "ON news_items (ts DESC)",
        "ON news_items USING GIN (assets)",
        "ON calendar_events (ts_start, ts_end)",
        "ON learned_rules (status, setup_id, regime)",
    ]
    for fragment in required_fragments:
        assert fragment in SQL


def test_timestamps_use_timestamptz():
    assert SQL.count("timestamptz") >= 20
    assert "open_time timestamptz" in SQL
    assert "created_at timestamptz" in SQL
    assert "updated_at timestamptz" in SQL

