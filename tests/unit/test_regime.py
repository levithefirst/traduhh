from datetime import datetime, timedelta, timezone

from agent.regime import classify_regime, classify_snapshot
from agent.features import compute_features

T=datetime(2026,9,5,12,tzinfo=timezone.utc)

def f(**overrides):
    x={"ema_20":110,"ema_50":100,"close":115,"adx_14":25,"z_close_20":0.5,"atr_pct_100":1.0,"vol_ratio":1.0,"grammar":"HH_HL","atr_14":2.0,"high_low_last3":3.0}
    x.update(overrides); return x

def test_trend_up_exact_thresholds():
    s=classify_regime(f(adx_14=18),asset="BTC",timeframe="1h",open_time=T)
    assert s.label=="TREND_UP" and s.confidence==0.3

def test_trend_down_exact_thresholds():
    s=classify_regime(f(ema_20=90,ema_50=100,close=85,adx_14=21,grammar="LH_LL"),asset="ETH",timeframe="4h",open_time=T)
    assert s.label=="TREND_DOWN" and s.confidence==0.7

def test_range_boundary_is_strict_on_adx_and_z():
    assert classify_regime(f(adx_14=17.999,z_close_20=1.799),asset="BTC",timeframe="15m",open_time=T).label=="RANGE"
    assert classify_regime(f(adx_14=18),asset="BTC",timeframe="15m",open_time=T).label=="TREND_UP"
    assert classify_regime(f(adx_14=17,z_close_20=1.8),asset="BTC",timeframe="15m",open_time=T).label=="UNKNOWN"

def test_breakout_climate_requires_rising_atr_pct_and_volume_and_not_range():
    s=classify_regime(f(atr_pct_100=1.2,atr_pct_100_20_ago=1.1,vol_ratio=1.2001),asset="BTC",timeframe="1h",open_time=T)
    assert "BREAKOUT_CLIMATE" in s.secondary
    s2=classify_regime(f(adx_14=17, z_close_20=0.2, atr_pct_100=1.2, atr_pct_100_20_ago=1.1, vol_ratio=2),asset="BTC",timeframe="1h",open_time=T)
    assert "BREAKOUT_CLIMATE" not in s2.secondary

def test_high_low_vol_and_panic_flags_are_exact():
    assert "HIGH_VOL" in classify_regime(f(atr_pct_100=1.6),asset="BTC",timeframe="1h",open_time=T).secondary
    assert "LOW_VOL" in classify_regime(f(atr_pct_100=0.7),asset="BTC",timeframe="1h",open_time=T).secondary
    s=classify_regime(f(atr_pct_100=2.5),asset="BTC",timeframe="1h",open_time=T)
    assert "PANIC" in s.secondary and s.label=="UNKNOWN"

def test_panic_from_last_three_bar_range():
    s=classify_regime(f(high_low_last3=8.0,atr_14=2.0),asset="BTC",timeframe="1h",open_time=T)
    assert "PANIC" in s.secondary and s.label=="UNKNOWN"

def test_event_high_uses_frozen_window_and_asset_filter():
    event={"ts_start":T+timedelta(minutes=10),"ts_end":T+timedelta(minutes=20),"impact":"high","assets":["BTC"]}
    s=classify_regime(f(),asset="BTC",timeframe="1h",open_time=T,calendar_events=[event])
    assert "EVENT_HIGH" in s.secondary
    eth=classify_regime(f(),asset="ETH",timeframe="1h",open_time=T,calendar_events=[event])
    assert "EVENT_HIGH" not in eth.secondary

def test_integrity_failure_forces_unknown():
    s=classify_regime(f(),asset="BTC",timeframe="1h",open_time=T,integrity_ok=False)
    assert s.label=="UNKNOWN" and s.confidence==0.2

def test_snapshot_regime_uses_only_target_and_prior_candles():
    rows=[]; price=100
    for i in range(140):
        ot=T-timedelta(hours=139-i); c=price+0.2; rows.append({"open_time":ot,"close_time":ot+timedelta(hours=1),"o":price,"h":c+1,"l":price-1,"c":c,"v":100}); price=c
    snap=compute_features(rows,asset="BTC",timeframe="1h",bar_open_time=T,reference_candles=rows)
    r=classify_snapshot(snap,candles=rows)
    assert r.open_time==T and r.timeframe=="1h"
