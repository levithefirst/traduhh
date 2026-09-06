"""Deterministic Step 4 feature engine.

Features are calculated only from observations available at the target closed
bar. The module deliberately keeps the required vector small and explicit.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from agent.config import FROZEN_ASSETS, FROZEN_TIMEFRAMES
from agent.timeutil import require_utc, utc_now

WARMUP_BARS = 120
TF_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400}

FEATURE_KEYS = (
    "ema_20", "ema_50", "sma_20", "atr_14", "atr_pct_100", "adx_14", "plus_di", "minus_di",
    "rsi_14", "vol_sma_20", "vol_ratio", "ret_1", "ret_12", "z_close_20", "swing_high",
    "swing_low", "last_swing_high_px", "last_swing_high_t", "last_swing_low_px", "last_swing_low_t",
    "grammar", "pdh", "pdl", "equal_high", "equal_low", "dist_ema20_atr", "spread_bps",
    "imbalance_5", "notional_to_10bps", "funding", "funding_z_168", "oi", "oi_chg_1h",
    "oi_chg_24h", "mark", "oracle", "basis_bps",
)


class FeatureError(ValueError):
    pass


class FeatureWarmupError(FeatureError):
    pass


@dataclass(frozen=True)
class FeatureSnapshot:
    asset: str
    timeframe: str
    open_time: datetime
    features: dict[str, Any]
    data_quality: dict[str, Any]
    computed_at: datetime


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _frame(rows: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["open_time", "close_time", "o", "h", "l", "c", "v"])
    df = pd.DataFrame(rows).copy()
    required = ["open_time", "o", "h", "l", "c", "v"]
    if any(c not in df.columns for c in required):
        raise FeatureError("candle rows missing required columns")
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    df = df.sort_values("open_time", kind="mergesort").drop_duplicates("open_time", keep="first")
    for col in ("o", "h", "l", "c", "v"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if df[["o", "h", "l", "c", "v"]].isna().any().any():
        raise FeatureError("malformed candle numeric data")
    if (df[["o", "h", "l", "c"]] <= 0).any().any() or (df["v"] < 0).any():
        raise FeatureError("invalid candle values")
    return df.reset_index(drop=True)


def _wilder(series: pd.Series, period: int) -> pd.Series:
    values = series.to_numpy(dtype=float)
    out = np.full(len(values), np.nan, dtype=float)
    if len(values) < period:
        return pd.Series(out, index=series.index)
    seed = float(np.mean(values[:period]))
    out[period - 1] = seed
    prev = seed
    alpha = 1.0 / period
    for i in range(period, len(values)):
        prev = prev + alpha * (values[i] - prev)
        out[i] = prev
    return pd.Series(out, index=series.index)


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=2.0 / (period + 1), adjust=False, min_periods=period).mean()


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev = df["c"].shift(1)
    tr = pd.concat([(df["h"] - df["l"]), (df["h"] - prev).abs(), (df["l"] - prev).abs()], axis=1).max(axis=1)
    tr.iloc[0] = df["h"].iloc[0] - df["l"].iloc[0]
    return _wilder(tr, period)


def _dmi(df: pd.DataFrame, period: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    up = df["h"].diff()
    down = -df["l"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    prev = df["c"].shift(1)
    tr = pd.concat([(df["h"] - df["l"]), (df["h"] - prev).abs(), (df["l"] - prev).abs()], axis=1).max(axis=1)
    tr.iloc[0] = df["h"].iloc[0] - df["l"].iloc[0]
    atr = _wilder(tr, period)
    plus = 100.0 * _wilder(plus_dm, period) / atr.replace(0, np.nan)
    minus = 100.0 * _wilder(minus_dm, period) / atr.replace(0, np.nan)
    dx = 100.0 * (plus - minus).abs() / (plus + minus).replace(0, np.nan)
    adx = _wilder(dx, period)
    return adx, plus, minus


def _rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["c"].diff().fillna(0.0)
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = _wilder(gain, period)
    avg_loss = _wilder(loss, period)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100.0 - 100.0 / (1.0 + rs)
    result[(avg_loss == 0) & (avg_gain > 0)] = 100.0
    result[(avg_gain == 0) & (avg_loss > 0)] = 0.0
    return result


def _pivots(df: pd.DataFrame, n: int = 2) -> tuple[pd.Series, pd.Series, list[tuple[int, float]], list[tuple[int, float]]]:
    high_flags = pd.Series(False, index=df.index)
    low_flags = pd.Series(False, index=df.index)
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    for i in range(n, len(df) - n):
        h = float(df["h"].iloc[i])
        l = float(df["l"].iloc[i])
        if (df["h"].iloc[i - n:i] < h).all() and (df["h"].iloc[i + 1:i + n + 1] < h).all():
            high_flags.iloc[i] = True
            highs.append((i, h))
        if (df["l"].iloc[i - n:i] > l).all() and (df["l"].iloc[i + 1:i + n + 1] > l).all():
            low_flags.iloc[i] = True
            lows.append((i, l))
    return high_flags, low_flags, highs, lows


def _confirmed_pivots(df: pd.DataFrame, asof_idx: int, n: int = 2):
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    max_center = asof_idx - n
    for i in range(n, max_center + 1):
        h = float(df["h"].iloc[i])
        l = float(df["l"].iloc[i])
        if (df["h"].iloc[i - n:i] < h).all() and (df["h"].iloc[i + 1:i + n + 1] < h).all():
            highs.append((i, h))
        if (df["l"].iloc[i - n:i] > l).all() and (df["l"].iloc[i + 1:i + n + 1] > l).all():
            lows.append((i, l))
    return highs, lows


def _grammar(highs: list[tuple[int, float]], lows: list[tuple[int, float]]) -> str | None:
    if len(highs) < 2 or len(lows) < 2:
        return None
    hh = all(a[1] > b[1] for a, b in zip(highs[-3:], highs[-3:-1])) if len(highs) >= 3 else highs[-1][1] > highs[-2][1]
    hl = all(a[1] > b[1] for a, b in zip(lows[-3:], lows[-3:-1])) if len(lows) >= 3 else lows[-1][1] > lows[-2][1]
    lh = all(a[1] < b[1] for a, b in zip(highs[-3:], highs[-3:-1])) if len(highs) >= 3 else highs[-1][1] < highs[-2][1]
    ll = all(a[1] < b[1] for a, b in zip(lows[-3:], lows[-3:-1])) if len(lows) >= 3 else lows[-1][1] < lows[-2][1]
    if hh and hl:
        return "HH_HL"
    if lh and ll:
        return "LH_LL"
    return "MIXED"


def _previous_day(reference_df: pd.DataFrame, target: datetime) -> tuple[float | None, float | None]:
    day = pd.Timestamp(target.date(), tz="UTC")
    prior = reference_df[(reference_df["open_time"] >= day - pd.Timedelta(days=1)) & (reference_df["open_time"] < day)]
    if prior.empty:
        return None, None
    return float(prior["h"].max()), float(prior["l"].min())


def _equal_levels(highs: list[tuple[int, float]], lows: list[tuple[int, float]], atr: float | None) -> tuple[bool | None, bool | None]:
    if atr is None or atr <= 0:
        return None, None
    high_equal = any(abs(highs[i][1] - highs[j][1]) <= 0.15 * atr for i, j in ((-1, -2), (-1, -3)) if len(highs) >= -j)
    low_equal = any(abs(lows[i][1] - lows[j][1]) <= 0.15 * atr for i, j in ((-1, -2), (-1, -3)) if len(lows) >= -j)
    return high_equal if len(highs) >= 2 else False, low_equal if len(lows) >= 2 else False


def _latest(rows: Sequence[Mapping[str, Any]], target: datetime, field: str = "ts") -> Mapping[str, Any] | None:
    target = require_utc(target)
    best = None
    best_ts: datetime | None = None
    for row in rows:
        ts = row.get(field)
        if not isinstance(ts, datetime):
            continue
        ts = require_utc(ts)
        if ts <= target and (best_ts is None or ts > best_ts):
            best, best_ts = row, ts
    return best


def _num(row: Mapping[str, Any] | None, key: str) -> float | None:
    return _finite(None if row is None else row.get(key))


def _hourly_history(rows: Sequence[Mapping[str, Any]], target: datetime, key: str, hours: int = 168) -> list[float]:
    out: list[float] = []
    for n in range(hours):
        row = _latest(rows, target - timedelta(hours=n), "ts")
        value = _num(row, key)
        if value is not None:
            out.append(value)
    return out


def compute_features(
    candles: Sequence[Mapping[str, Any]], *, asset: str, timeframe: str,
    bar_open_time: datetime | None = None, ctx_rows: Sequence[Mapping[str, Any]] = (),
    book_rows: Sequence[Mapping[str, Any]] = (), reference_candles: Sequence[Mapping[str, Any]] | None = None,
    asof_time: datetime | None = None,
) -> FeatureSnapshot:
    if asset not in FROZEN_ASSETS:
        raise FeatureError(f"unsupported asset: {asset}")
    if timeframe not in FROZEN_TIMEFRAMES:
        raise FeatureError(f"unsupported timeframe: {timeframe}")
    df_all = _frame(candles)
    if len(df_all) < WARMUP_BARS:
        raise FeatureWarmupError(f"warmup requires {WARMUP_BARS} closed bars")
    target = require_utc(bar_open_time or df_all["open_time"].iloc[-1].to_pydatetime())
    idxs = df_all.index[df_all["open_time"] == pd.Timestamp(target)]
    if len(idxs) != 1:
        raise FeatureError("feature bar_open_time is not a stored candle")
    target_idx = int(idxs[0])
    if target_idx < WARMUP_BARS - 1:
        raise FeatureWarmupError(f"warmup requires {WARMUP_BARS} closed bars at target")
    df = df_all.iloc[:target_idx + 1].copy()
    target_close = asof_time
    if target_close is None:
        raw_close = df["close_time"].iloc[-1] if "close_time" in df.columns else pd.NaT
        target_close = raw_close.to_pydatetime() if pd.notna(raw_close) else target + timedelta(seconds=TF_SECONDS[timeframe])
    target_close = require_utc(target_close)

    close = df["c"]
    ema20 = _ema(close, 20); ema50 = _ema(close, 50); sma20 = close.rolling(20, min_periods=20).mean()
    atr14 = _atr(df, 14); atr_pct100 = atr14 / atr14.rolling(100, min_periods=100).median().replace(0, np.nan)
    adx14, plus_di, minus_di = _dmi(df, 14); rsi14 = _rsi(df, 14)
    vol_sma20 = df["v"].rolling(20, min_periods=20).mean(); vol_ratio = df["v"] / vol_sma20.replace(0, np.nan)
    ret1 = np.log(close / close.shift(1)); ret12 = np.log(close / close.shift(12))
    stdev20 = close.rolling(20, min_periods=20).std(ddof=1); z20 = (close - sma20) / stdev20.replace(0, np.nan)
    swing_high, swing_low, _, _ = _pivots(df, 2)
    confirmed_highs, confirmed_lows = _confirmed_pivots(df, len(df) - 1, 2)
    last_high = confirmed_highs[-1] if confirmed_highs else None; last_low = confirmed_lows[-1] if confirmed_lows else None
    grammar = _grammar(confirmed_highs, confirmed_lows); atr = _finite(atr14.iloc[-1]); eq_high, eq_low = _equal_levels(confirmed_highs, confirmed_lows, atr)
    ref = _frame(reference_candles) if reference_candles is not None else df
    if "close_time" in ref.columns:
        ref = ref[pd.to_datetime(ref["close_time"], utc=True) <= pd.Timestamp(target_close)].copy()
    pdh, pdl = _previous_day(ref, target)

    ctx = _latest(ctx_rows, target_close, "ts"); book = _latest(book_rows, target_close, "ts")
    funding = _num(ctx, "funding"); funding_history = _hourly_history(ctx_rows, target_close, "funding", 168)
    funding_z = None
    if funding is not None and len(funding_history) >= 2:
        std = float(np.std(funding_history, ddof=1))
        if std > 0:
            funding_z = (funding - float(np.mean(funding_history))) / std
    oi = _num(ctx, "oi")
    oi_1h = _latest(ctx_rows, target_close - timedelta(hours=1), "ts"); oi_24h = _latest(ctx_rows, target_close - timedelta(hours=24), "ts")
    oi1 = _num(oi_1h, "oi"); oi24 = _num(oi_24h, "oi")
    oi_chg_1h = None if oi is None or oi1 in (None, 0) else oi / oi1 - 1.0
    oi_chg_24h = None if oi is None or oi24 in (None, 0) else oi / oi24 - 1.0
    mark = _num(ctx, "mark"); oracle = _num(ctx, "oracle"); basis = None if mark is None or oracle in (None, 0) else 10000.0 * (mark - oracle) / oracle

    features = {
        "ema_20": _finite(ema20.iloc[-1]), "ema_50": _finite(ema50.iloc[-1]), "sma_20": _finite(sma20.iloc[-1]),
        "atr_14": atr, "atr_pct_100": _finite(atr_pct100.iloc[-1]), "adx_14": _finite(adx14.iloc[-1]),
        "plus_di": _finite(plus_di.iloc[-1]), "minus_di": _finite(minus_di.iloc[-1]), "rsi_14": _finite(rsi14.iloc[-1]),
        "vol_sma_20": _finite(vol_sma20.iloc[-1]), "vol_ratio": _finite(vol_ratio.iloc[-1]),
        "ret_1": _finite(ret1.iloc[-1]), "ret_12": _finite(ret12.iloc[-1]), "z_close_20": _finite(z20.iloc[-1]),
        "swing_high": bool(swing_high.iloc[-1]), "swing_low": bool(swing_low.iloc[-1]),
        "last_swing_high_px": None if last_high is None else last_high[1],
        "last_swing_high_t": None if last_high is None else df["open_time"].iloc[last_high[0]].to_pydatetime().isoformat(),
        "last_swing_low_px": None if last_low is None else last_low[1],
        "last_swing_low_t": None if last_low is None else df["open_time"].iloc[last_low[0]].to_pydatetime().isoformat(),
        "grammar": grammar, "pdh": pdh, "pdl": pdl, "equal_high": eq_high, "equal_low": eq_low,
        "dist_ema20_atr": None if atr in (None, 0) or _finite(ema20.iloc[-1]) is None else (float(close.iloc[-1]) - float(ema20.iloc[-1])) / atr,
        "spread_bps": _num(book, "spread_bps"), "imbalance_5": _num(book, "imbalance_5"),
        "notional_to_10bps": _num(book, "notional_to_10bps"), "funding": funding, "funding_z_168": funding_z,
        "oi": oi, "oi_chg_1h": oi_chg_1h, "oi_chg_24h": oi_chg_24h, "mark": mark, "oracle": oracle, "basis_bps": basis,
    }
    if tuple(features.keys()) != FEATURE_KEYS:
        raise AssertionError("feature vector contract drift")
    return FeatureSnapshot(asset, timeframe, target, features, {"warmup_insufficient": False, "lookahead_protected": True}, utc_now())


def compute_feature_series(candles, *, asset: str, timeframe: str, ctx_rows=(), book_rows=(), reference_candles=None) -> list[FeatureSnapshot]:
    df = _frame(candles)
    if len(df) < WARMUP_BARS:
        return []
    return [compute_features(candles, asset=asset, timeframe=timeframe,
                             bar_open_time=df["open_time"].iloc[i].to_pydatetime(), ctx_rows=ctx_rows,
                             book_rows=book_rows, reference_candles=reference_candles)
            for i in range(WARMUP_BARS - 1, len(df))]


def load_feature_inputs(conn, asset: str, timeframe: str, target: datetime):
    target = require_utc(target)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT open_time,close_time,o,h,l,c,v,n_trades FROM candles
               WHERE asset=%s AND timeframe=%s AND open_time<=%s ORDER BY open_time DESC LIMIT 400""",
            (asset, timeframe, target),
        )
        candles = list(reversed(cur.fetchall()))
        asof = target + timedelta(seconds=TF_SECONDS[timeframe])
        cur.execute("SELECT ts,mid,mark,oracle,funding,premium,oi,day_ntl_vlm FROM asset_ctx WHERE asset=%s AND ts<=%s ORDER BY ts", (asset, asof))
        ctx = cur.fetchall()
        cur.execute("SELECT ts,spread_bps,imbalance_5,notional_to_10bps FROM book_snapshots WHERE asset=%s AND ts<=%s ORDER BY ts", (asset, asof))
        books = cur.fetchall()
        cur.execute("SELECT ts_start,ts_end,name,impact,assets,source FROM calendar_events WHERE ts_start <= %s AND ts_end >= %s", (target + timedelta(minutes=15), target - timedelta(minutes=30)))
        events = cur.fetchall()
        ref = candles
        if timeframe == "4h":
            cur.execute("SELECT open_time,close_time,o,h,l,c,v,n_trades FROM candles WHERE asset=%s AND timeframe='1h' AND open_time <= %s ORDER BY open_time DESC LIMIT 600", (asset, target))
            ref = list(reversed(cur.fetchall()))
    candle_rows = [dict(zip(("open_time","close_time","o","h","l","c","v","n_trades"), r)) for r in candles]
    ctx_rows = [dict(zip(("ts","mid","mark","oracle","funding","premium","oi","day_ntl_vlm"), r)) for r in ctx]
    book_rows = [dict(zip(("ts","spread_bps","imbalance_5","notional_to_10bps"), r)) for r in books]
    event_rows = [dict(zip(("ts_start","ts_end","name","impact","assets","source"), r)) for r in events]
    ref_rows = [dict(zip(("open_time","close_time","o","h","l","c","v","n_trades"), r)) for r in ref]
    return candle_rows, ctx_rows, book_rows, event_rows, ref_rows


def compute_and_persist_latest(conn, *, asset: str, timeframe: str, bar_open_time: datetime | None = None) -> tuple[FeatureSnapshot, Any]:
    if bar_open_time is None:
        with conn.cursor() as cur:
            cur.execute("SELECT open_time FROM candles WHERE asset=%s AND timeframe=%s AND close_time<=%s ORDER BY open_time DESC LIMIT 1", (asset, timeframe, utc_now()))
            row = cur.fetchone()
        if not row:
            raise FeatureWarmupError("no closed candle available")
        target = require_utc(row[0])
    else:
        target = require_utc(bar_open_time)
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM system_state WHERE key='integrity'")
        integrity_row = cur.fetchone()
    if integrity_row and isinstance(integrity_row[0], dict) and not integrity_row[0].get("ok", True):
        raise FeatureError("integrity state is not OK; feature emission halted")
    candles, ctx, books, events, ref = load_feature_inputs(conn, asset, timeframe, target)
    snapshot = compute_features(candles, asset=asset, timeframe=timeframe, bar_open_time=target,
                                ctx_rows=ctx, book_rows=books, reference_candles=ref)
    from agent.regime import classify_snapshot
    regime = classify_snapshot(snapshot, candles=candles, calendar_events=events, integrity_ok=True)
    upsert_feature_snapshot(conn, snapshot)
    from agent.regime import upsert_regime_snapshot
    upsert_regime_snapshot(conn, regime)
    return snapshot, regime


def upsert_feature_snapshot(conn, snapshot: FeatureSnapshot) -> None:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO feature_snapshots(asset,timeframe,open_time,features,computed_at)
                   VALUES (%s,%s,%s,%s::jsonb,%s)
                   ON CONFLICT (asset,timeframe,open_time) DO UPDATE
                   SET features=EXCLUDED.features,computed_at=EXCLUDED.computed_at""",
                (snapshot.asset, snapshot.timeframe, snapshot.open_time,
                 json.dumps(snapshot.features, separators=(",", ":"), allow_nan=False), snapshot.computed_at),
            )


def upsert_feature_snapshots(conn, snapshots: Sequence[FeatureSnapshot]) -> int:
    for snapshot in snapshots:
        upsert_feature_snapshot(conn, snapshot)
    return len(snapshots)
