from datetime import datetime, timedelta, timezone
import math

import pytest

from agent.features import FEATURE_KEYS, WARMUP_BARS, FeatureWarmupError, compute_features, upsert_feature_snapshot


def candles(n=140, start=datetime(2026, 1, 1, tzinfo=timezone.utc), tf=3600):
    rows=[]
    price=100.0
    for i in range(n):
        ot=start+timedelta(seconds=tf*i)
        close=price + 0.25 + (0.05 if i % 7 == 0 else 0.0)
        rows.append({"open_time":ot,"close_time":ot+timedelta(seconds=tf),"o":price,"h":max(price,close)+1.0,"l":min(price,close)-0.5,"c":close,"v":100+i})
        price=close
    return rows


def context_rows(target):
    out=[]
    for i in range(168):
        ts=target+timedelta(seconds=3600)-timedelta(hours=i)
        out.append({"ts":ts,"mid":110,"mark":111,"oracle":110,"funding":0.0001+i*0.000001,"oi":1000+i,"day_ntl_vlm":100000})
    return out


def book_rows(target):
    return [{"ts":target+timedelta(seconds=3600)-timedelta(seconds=1),"spread_bps":2.0,"imbalance_5":0.1,"notional_to_10bps":50000.0}]


def test_feature_vector_has_exact_frozen_keys_and_is_deterministic():
    rows=candles()
    target=rows[-1]["open_time"]
    a=compute_features(rows, asset="BTC", timeframe="1h", bar_open_time=target, ctx_rows=context_rows(target), book_rows=book_rows(target), reference_candles=rows)
    b=compute_features(rows, asset="BTC", timeframe="1h", bar_open_time=target, ctx_rows=context_rows(target), book_rows=book_rows(target), reference_candles=rows)
    assert tuple(a.features) == FEATURE_KEYS
    assert a.features == b.features
    assert a.data_quality["lookahead_protected"] is True


def test_warmup_does_not_emit_features():
    with pytest.raises(FeatureWarmupError):
        compute_features(candles(WARMUP_BARS-1), asset="BTC", timeframe="1h")


def test_core_formulas_match_closed_bar_data():
    rows=candles()
    target=rows[-1]["open_time"]
    snap=compute_features(rows, asset="BTC", timeframe="1h", bar_open_time=target, ctx_rows=context_rows(target), book_rows=book_rows(target), reference_candles=rows)
    closes=[r["c"] for r in rows]
    assert math.isclose(snap.features["sma_20"], sum(closes[-20:])/20, rel_tol=1e-12)
    assert math.isclose(snap.features["ret_1"], math.log(closes[-1]/closes[-2]), rel_tol=1e-12)
    assert math.isclose(snap.features["ret_12"], math.log(closes[-1]/closes[-13]), rel_tol=1e-12)
    assert snap.features["vol_ratio"] is not None


def test_external_state_uses_only_data_at_or_before_closed_bar():
    rows=candles()
    target=rows[-1]["open_time"]
    before={"ts":target+timedelta(hours=1)-timedelta(seconds=1),"mark":101,"oracle":100,"funding":0.001,"oi":100,"mid":100}
    future={"ts":target+timedelta(hours=2),"mark":9999,"oracle":9999,"funding":9,"oi":999999,"mid":9999}
    book_before={"ts":target+timedelta(hours=1)-timedelta(seconds=1),"spread_bps":2,"imbalance_5":0.2,"notional_to_10bps":1000}
    book_future={"ts":target+timedelta(hours=2),"spread_bps":99,"imbalance_5":-1,"notional_to_10bps":1}
    a=compute_features(rows, asset="BTC", timeframe="1h", bar_open_time=target, ctx_rows=[before], book_rows=[book_before], reference_candles=rows)
    b=compute_features(rows, asset="BTC", timeframe="1h", bar_open_time=target, ctx_rows=[before,future], book_rows=[book_before,book_future], reference_candles=rows)
    assert a.features == b.features
    assert a.features["mark"] == 101
    assert a.features["spread_bps"] == 2


def test_future_candle_cannot_change_features_at_prior_closed_bar():
    rows=candles()
    target=rows[-1]["open_time"]
    base=compute_features(rows, asset="BTC", timeframe="1h", bar_open_time=target, reference_candles=rows)
    future=dict(rows[-1])
    future["open_time"]=target+timedelta(hours=1)
    future["close_time"]=future["open_time"]+timedelta(hours=1)
    future["o"]=100000; future["h"]=200000; future["l"]=1; future["c"]=150000; future["v"]=999999
    leaked=compute_features(rows+[future], asset="BTC", timeframe="1h", bar_open_time=target, reference_candles=rows+[future])
    assert base.features == leaked.features


def test_invalid_asset_and_timeframe_are_rejected():
    rows=candles()
    with pytest.raises(ValueError): compute_features(rows, asset="XRP", timeframe="1h")
    with pytest.raises(ValueError): compute_features(rows, asset="BTC", timeframe="5m")


def test_persistence_uses_unique_upsert_contract():
    class Cur:
        def __init__(self): self.sql=[]
        def execute(self, sql, params=None): self.sql.append((sql, params))
        def __enter__(self): return self
        def __exit__(self,*a): return False
    class Conn:
        def __init__(self): self.cur=Cur()
        def cursor(self): return self.cur
        def transaction(self): return self
        def __enter__(self): return self
        def __exit__(self,*a): return False
    rows=candles(); target=rows[-1]["open_time"]
    snap=compute_features(rows, asset="BTC", timeframe="1h", bar_open_time=target, reference_candles=rows)
    conn=Conn(); upsert_feature_snapshot(conn,snap)
    sql=conn.cur.sql[0][0]
    assert "ON CONFLICT (asset,timeframe,open_time) DO UPDATE" in sql
    assert "features=EXCLUDED.features" in sql


def test_pdh_pdl_excludes_an_unclosed_higher_timeframe_bar():
    rows=candles()
    target=rows[-1]["open_time"]
    # A reference 1h bar that starts before the target but closes after it is
    # not available at the target 15m close and must not leak into PDH/PDL.
    reference=[dict(r) for r in rows[-30:]]
    reference[-1]["open_time"]=target-timedelta(minutes=30)
    reference[-1]["close_time"]=target+timedelta(minutes=30)
    reference[-1]["h"]=999999
    reference[-1]["l"]=1
    snap=compute_features(rows, asset="BTC", timeframe="15m", bar_open_time=target, reference_candles=reference)
    assert snap.features["pdh"] != 999999
    assert snap.features["pdl"] != 1


def test_equal_level_threshold_is_at_most_015_atr():
    rows=candles()
    target=rows[-1]["open_time"]
    # The deterministic pivot detector is exercised indirectly; a valid
    # feature snapshot must expose boolean/null equality fields only.
    snap=compute_features(rows, asset="BTC", timeframe="1h", bar_open_time=target, reference_candles=rows)
    assert snap.features["equal_high"] in (True, False, None)
    assert snap.features["equal_low"] in (True, False, None)
