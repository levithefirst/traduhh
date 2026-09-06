from decimal import Decimal

import pytest

from agent.config import ConfigError, load_settings


BASE_ENV = {
    "DATABASE_URL": "postgres://agent:agent@127.0.0.1:5432/agent",
    "TELEGRAM_BOT_TOKEN": "token",
    "TELEGRAM_ALLOWED_CHAT_IDS": "123, -456",
    "LLM_BASE_URL": "https://example.invalid/v1",
    "LLM_API_KEY": "key",
    "LLM_MODEL": "model",
    "HL_INFO_URL": "https://api.hyperliquid.xyz/info",
    "WS_ENABLED": "false",
    "AGENT_MODE": "paper",
    "PAPER_EQUITY_USD": "10000",
    "RISK_FRACTION": "0.005",
    "MIN_R_AFTER_COSTS": "1.2",
    "MAX_CONCURRENT_PAPER": "3",
    "TAKER_FEE_BPS": "4.5",
    "SLIPPAGE_BPS_FLOOR": "2.0",
    "HOLD_BARS_DEFAULT": "12",
    "LOG_LEVEL": "INFO",
    "TZ": "UTC",
}


def set_env(monkeypatch, values=None):
    for key, value in (values or BASE_ENV).items():
        monkeypatch.setenv(key, value)


def test_loads_frozen_defaults(monkeypatch):
    set_env(monkeypatch)
    settings = load_settings()
    assert settings.telegram_allowed_chat_ids == (123, -456)
    assert settings.hl_ws_url == "wss://api.hyperliquid.xyz/ws"
    assert settings.paper_equity_usd == Decimal("10000")
    assert settings.taker_fee_bps == Decimal("4.5")


def test_missing_required_env_refuses_start(monkeypatch):
    set_env(monkeypatch)
    monkeypatch.delenv("LLM_API_KEY")
    with pytest.raises(ConfigError, match="LLM_API_KEY"):
        load_settings()


def test_testnet_mode_refuses_start(monkeypatch):
    set_env(monkeypatch, {**BASE_ENV, "AGENT_MODE": "testnet_exec"})
    with pytest.raises(ConfigError, match="paper or halted"):
        load_settings()


def test_non_utc_refuses_start(monkeypatch):
    set_env(monkeypatch, {**BASE_ENV, "TZ": "Africa/Lagos"})
    with pytest.raises(ConfigError, match="TZ must be UTC"):
        load_settings()


def test_private_key_env_refuses_start(monkeypatch):
    set_env(monkeypatch)
    monkeypatch.setenv("HL_AGENT_PRIVATE_KEY", "should-never-be-loaded")
    with pytest.raises(ConfigError, match="forbidden credential"):
        load_settings()
