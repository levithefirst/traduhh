from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

REQUIRED_ENV = (
    "DATABASE_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_CHAT_IDS",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "HL_INFO_URL",
    "WS_ENABLED",
    "AGENT_MODE",
    "PAPER_EQUITY_USD",
    "RISK_FRACTION",
    "MIN_R_AFTER_COSTS",
    "MAX_CONCURRENT_PAPER",
    "TAKER_FEE_BPS",
    "SLIPPAGE_BPS_FLOOR",
    "HOLD_BARS_DEFAULT",
    "LOG_LEVEL",
    "TZ",
)

DEFAULTS = {
    "HL_WS_URL": "wss://api.hyperliquid.xyz/ws",
}

FROZEN_ASSETS = ("BTC", "ETH", "SOL")
FROZEN_TIMEFRAMES = ("15m", "1h", "4h")
HL_CONNECT_TIMEOUT_S = 8.0
HL_READ_TIMEOUT_S = 15.0
HL_RETRY_DELAYS_S = (0.5, 1.0, 2.0)
CANDLE_BACKFILL_COUNTS = {"15m": 2000, "1h": 3000, "4h": 2000}
HL_CANDLE_MAX = 5000
BOOK_RETENTION_DAYS = 7

# The MVP explicitly forbids exchange private keys and wallet credentials.
# The named future-phase key is included, plus generic private-key/mnemonic names.
FORBIDDEN_SECRET_NAME_PARTS = ("PRIVATE_KEY", "MNEMONIC")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    database_url: str
    telegram_bot_token: str
    telegram_allowed_chat_ids: tuple[int, ...]
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    hl_info_url: str
    hl_ws_url: str
    ws_enabled: bool
    agent_mode: str
    paper_equity_usd: Decimal
    risk_fraction: Decimal
    min_r_after_costs: Decimal
    max_concurrent_paper: int
    taker_fee_bps: Decimal
    slippage_bps_floor: Decimal
    hold_bars_default: int
    log_level: str
    tz: str


def _env(name: str) -> str:
    value = os.getenv(name, DEFAULTS.get(name))
    if value is None or not value.strip():
        raise ConfigError(f"missing required environment variable: {name}")
    return value.strip()


def _decimal(name: str) -> Decimal:
    try:
        return Decimal(_env(name))
    except InvalidOperation as exc:
        raise ConfigError(f"invalid decimal for {name}") from exc


def _int(name: str) -> int:
    try:
        return int(_env(name))
    except ValueError as exc:
        raise ConfigError(f"invalid integer for {name}") from exc


def _bool(name: str) -> bool:
    value = _env(name).lower()
    if value not in {"true", "false"}:
        raise ConfigError(f"{name} must be true or false")
    return value == "true"


def load_settings() -> Settings:
    missing = [name for name in REQUIRED_ENV if os.getenv(name) is None and name not in DEFAULTS]
    if missing:
        raise ConfigError("missing required environment variable(s): " + ", ".join(missing))

    forbidden = sorted(
        name for name in os.environ
        if any(part in name.upper() for part in FORBIDDEN_SECRET_NAME_PARTS)
    )
    if forbidden:
        raise ConfigError("forbidden credential environment variable(s) present: " + ", ".join(forbidden))

    chat_ids_raw = _env("TELEGRAM_ALLOWED_CHAT_IDS")
    try:
        chat_ids = tuple(int(x.strip()) for x in chat_ids_raw.split(",") if x.strip())
    except ValueError as exc:
        raise ConfigError("TELEGRAM_ALLOWED_CHAT_IDS must be comma-separated int64 values") from exc
    if not chat_ids:
        raise ConfigError("TELEGRAM_ALLOWED_CHAT_IDS must contain at least one chat id")
    if any(i < -(2**63) or i > 2**63 - 1 for i in chat_ids):
        raise ConfigError("TELEGRAM_ALLOWED_CHAT_IDS contains an int64 out of range")

    mode = _env("AGENT_MODE")
    if mode not in {"paper", "halted"}:
        raise ConfigError("AGENT_MODE must be paper or halted in MVP")
    if _env("TZ") != "UTC":
        raise ConfigError("TZ must be UTC")

    settings = Settings(
        database_url=_env("DATABASE_URL"),
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_allowed_chat_ids=chat_ids,
        llm_base_url=_env("LLM_BASE_URL"),
        llm_api_key=_env("LLM_API_KEY"),
        llm_model=_env("LLM_MODEL"),
        hl_info_url=_env("HL_INFO_URL"),
        hl_ws_url=_env("HL_WS_URL"),
        ws_enabled=_bool("WS_ENABLED"),
        agent_mode=mode,
        paper_equity_usd=_decimal("PAPER_EQUITY_USD"),
        risk_fraction=_decimal("RISK_FRACTION"),
        min_r_after_costs=_decimal("MIN_R_AFTER_COSTS"),
        max_concurrent_paper=_int("MAX_CONCURRENT_PAPER"),
        taker_fee_bps=_decimal("TAKER_FEE_BPS"),
        slippage_bps_floor=_decimal("SLIPPAGE_BPS_FLOOR"),
        hold_bars_default=_int("HOLD_BARS_DEFAULT"),
        log_level=_env("LOG_LEVEL"),
        tz=_env("TZ"),
    )

    if settings.paper_equity_usd <= 0:
        raise ConfigError("PAPER_EQUITY_USD must be > 0")
    if settings.risk_fraction <= 0:
        raise ConfigError("RISK_FRACTION must be > 0")
    if settings.min_r_after_costs <= 0:
        raise ConfigError("MIN_R_AFTER_COSTS must be > 0")
    if settings.max_concurrent_paper < 0:
        raise ConfigError("MAX_CONCURRENT_PAPER must be >= 0")
    if settings.taker_fee_bps < 0 or settings.slippage_bps_floor < 0:
        raise ConfigError("cost parameters must be >= 0")
    if settings.hold_bars_default <= 0:
        raise ConfigError("HOLD_BARS_DEFAULT must be > 0")
    return settings
