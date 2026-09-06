"""Deterministic Step 4 regime classifier."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from typing import Any, Mapping, Sequence

from agent.config import FROZEN_ASSETS, FROZEN_TIMEFRAMES
from agent.features import FeatureSnapshot, FeatureWarmupError, _frame, compute_features
from agent.timeutil import require_utc, utc_now

PRIMARY = ("TREND_UP", "TREND_DOWN", "RANGE", "UNKNOWN")
SECONDARY = ("HIGH_VOL", "LOW_VOL", "PANIC", "EVENT_HIGH", "BREAKOUT_CLIMATE")


@dataclass(frozen=True)
class RegimeSnapshot:
    asset: str
    timeframe: str
    open_time: datetime
    label: str
    secondary: list[str]
    confidence: float
    features_used: dict[str, Any]


def _event_high(events: Sequence[Mapping[str, Any]], asset: str, target: datetime) -> bool:
    target = require_utc(target)
    for event in events:
        if str(event.get("impact", "")).lower() != "high":
            continue
        start, end = event.get("ts_start"), event.get("ts_end")
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            continue
        assets = event.get("assets")
        if assets and asset not in assets:
            continue
        if require_utc(start) - timedelta(minutes=30) <= target <= require_utc(end) + timedelta(minutes=15):
            return True
    return False


def classify_regime(features: Mapping[str, Any], *, asset: str, timeframe: str, open_time: datetime,
                    calendar_events: Sequence[Mapping[str, Any]] = (), integrity_ok: bool = True,
                    warmup_ok: bool = True) -> RegimeSnapshot:
    if asset not in FROZEN_ASSETS or timeframe not in FROZEN_TIMEFRAMES:
        raise ValueError("unsupported asset or timeframe")
    target = require_utc(open_time)
    adx, z, atr_pct = features.get("adx_14"), features.get("z_close_20"), features.get("atr_pct_100")
    vol_ratio, grammar = features.get("vol_ratio"), features.get("grammar")
    ema20, ema50, close = features.get("ema_20"), features.get("ema_50"), features.get("close")
    if close is None:
        raise ValueError("regime classification requires closed-bar close")

    matches: list[str] = []
    complete = warmup_ok and integrity_ok and all(x is not None for x in (ema20, ema50, adx, z, grammar, atr_pct, vol_ratio))
    if complete:
        if ema20 > ema50 and close > ema20 and grammar == "HH_HL" and adx >= 18: matches.append("TREND_UP")
        if ema20 < ema50 and close < ema20 and grammar == "LH_LL" and adx >= 18: matches.append("TREND_DOWN")
        if adx < 18 and abs(z) < 1.8: matches.append("RANGE")
    secondary: list[str] = []
    if atr_pct is not None and atr_pct >= 1.6: secondary.append("HIGH_VOL")
    if atr_pct is not None and atr_pct <= 0.7: secondary.append("LOW_VOL")
    if atr_pct is not None and atr_pct >= 2.5: secondary.append("PANIC")
    high_low_3 = features.get("high_low_last3")
    if high_low_3 is not None and features.get("atr_14") is not None and high_low_3 >= 4 * features["atr_14"] and "PANIC" not in secondary:
        secondary.append("PANIC")
    if features.get("atr_pct_100_20_ago") is not None and atr_pct is not None and vol_ratio is not None:
        if atr_pct > features["atr_pct_100_20_ago"] and vol_ratio > 1.2 and "RANGE" not in matches:
            secondary.append("BREAKOUT_CLIMATE")
    if _event_high(calendar_events, asset, target): secondary.append("EVENT_HIGH")

    panic = "PANIC" in secondary
    label = matches[0] if len(matches) == 1 else "UNKNOWN"
    if not integrity_ok or not warmup_ok or len(matches) != 1 or panic:
        label = "UNKNOWN"
    if adx is not None and 16 <= adx <= 20:
        confidence = 0.3
    elif label != "UNKNOWN":
        confidence = 0.7
    else:
        confidence = 0.2
    return RegimeSnapshot(asset, timeframe, target, label, secondary, confidence, dict(features))


def classify_snapshot(snapshot: FeatureSnapshot, *, candles, calendar_events=(), integrity_ok: bool = True) -> RegimeSnapshot:
    df = _frame(candles)
    target = require_utc(snapshot.open_time)
    idxs = df.index[df["open_time"] == target]
    if len(idxs) != 1:
        raise ValueError("snapshot target is not in candles")
    i = int(idxs[0])
    f = dict(snapshot.features)
    f["close"] = float(df["c"].iloc[i])
    window = df.iloc[max(0, i - 2):i + 1]
    f["high_low_last3"] = float(window["h"].max() - window["l"].min())
    if i >= 20 and i - 20 >= 119:
        prior = compute_features(candles, asset=snapshot.asset, timeframe=snapshot.timeframe,
                                  bar_open_time=df["open_time"].iloc[i - 20].to_pydatetime())
        f["atr_pct_100_20_ago"] = prior.features.get("atr_pct_100")
    return classify_regime(f, asset=snapshot.asset, timeframe=snapshot.timeframe, open_time=target,
                           calendar_events=calendar_events, integrity_ok=integrity_ok,
                           warmup_ok=not snapshot.data_quality.get("warmup_insufficient", False))


def upsert_regime_snapshot(conn, snapshot: RegimeSnapshot) -> None:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO regime_snapshots(asset,timeframe,open_time,label,secondary,confidence,features_used)
                   VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)
                   ON CONFLICT (asset,timeframe,open_time) DO UPDATE
                   SET label=EXCLUDED.label,secondary=EXCLUDED.secondary,confidence=EXCLUDED.confidence,
                       features_used=EXCLUDED.features_used""",
                (snapshot.asset, snapshot.timeframe, snapshot.open_time, snapshot.label, snapshot.secondary,
                 snapshot.confidence, json.dumps(snapshot.features_used, separators=(",", ":"), allow_nan=False)),
            )


def upsert_regime_snapshots(conn, snapshots: Sequence[RegimeSnapshot]) -> int:
    for snapshot in snapshots:
        upsert_regime_snapshot(conn, snapshot)
    return len(snapshots)
