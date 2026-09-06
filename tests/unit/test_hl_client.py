from datetime import timezone
from decimal import Decimal

import httpx
import pytest

from agent.hl_client import HLClientError, HLResponseError, HyperliquidClient


def make_client(payload, status=200, sleeper=lambda _x: None):
    def handler(request):
        return httpx.Response(status, json=payload, request=request)
    return HyperliquidClient("https://api.hyperliquid.xyz/info", transport=httpx.MockTransport(handler), sleeper=sleeper)


def candle_payload():
    return [{"t": 1720000000000, "T": 1720003599999, "s": "BTC", "i": "1h", "o": "60000", "c": "60100", "h": "60200", "l": "59900", "v": "12.5", "n": 42}]


def test_candle_normalization():
    with make_client(candle_payload()) as client:
        candle = client.fetch_candles("BTC", "1h", 1, 2)[0]
    assert candle.asset == "BTC"
    assert candle.timeframe == "1h"
    assert candle.o == Decimal("60000")
    assert candle.open_time.tzinfo == timezone.utc
    assert candle.close_time > candle.open_time


def test_rejects_unexpected_symbol_and_interval():
    bad = candle_payload()
    bad[0]["s"] = "DOGE"
    with make_client(bad) as client:
        with pytest.raises(HLResponseError, match="unexpected candle symbol"):
            client.fetch_candles("BTC", "1h", 1, 2)
    with make_client(candle_payload()) as client:
        with pytest.raises(ValueError, match="unsupported timeframe"):
            client.fetch_candles("BTC", "5m", 1, 2)


def test_empty_candle_response_is_explicit_empty():
    with make_client([]) as client:
        assert client.fetch_candles("BTC", "1h", 1, 2) == []


def test_context_normalization_and_missing_field_tracking():
    payload = [
        {"universe": [{"name": "BTC"}, {"name": "ETH"}, {"name": "SOL"}, {"name": "DOGE"}]},
        [
            {"midPx": "60000", "markPx": "60001", "oraclePx": "59999", "funding": "0.00001", "premium": "0.0001", "openInterest": "100", "dayNtlVlm": "1000000", "impactPxs": ["59990", "60010"], "prevDayPx": "59000"},
            {"midPx": "3000", "markPx": "3001", "oraclePx": "2999", "funding": "0.00002", "premium": "0", "openInterest": "200", "dayNtlVlm": "2000000", "prevDayPx": "2900"},
            {"midPx": "150", "markPx": "151", "oraclePx": "149", "funding": "0.00003", "premium": "0", "openInterest": "300", "dayNtlVlm": "3000000", "impactPxs": ["149.9", "150.1"], "prevDayPx": "145"},
            {},
        ],
    ]
    with make_client(payload) as client:
        contexts = client.fetch_contexts()
    assert [x.asset for x in contexts] == ["BTC", "ETH", "SOL"]
    assert contexts[0].mark == Decimal("60001")
    assert contexts[1].missing_fields == ("impactPxs",)
    assert contexts[1].impact_bid is None


def test_context_rejects_misaligned_universe():
    with make_client([{"universe": [{"name": "BTC"}]}, []]) as client:
        with pytest.raises(HLResponseError, match="index-aligned"):
            client.fetch_contexts()


def test_book_normalization_and_depth_summary():
    payload = {
        "coin": "BTC", "time": 1720000000123,
        "levels": [
            [{"px": "59999", "sz": "2", "n": 1}, {"px": "59990", "sz": "3", "n": 2}],
            [{"px": "60001", "sz": "1.5", "n": 1}, {"px": "60010", "sz": "2.5", "n": 1}],
        ],
    }
    with make_client(payload) as client:
        book = client.fetch_book("BTC")
    assert book.bid1 == Decimal("59999")
    assert book.ask1 == Decimal("60001")
    assert book.spread == Decimal("2")
    assert book.bid_sz_5 == Decimal("5")
    assert book.ask_sz_5 == Decimal("4.0")
    assert book.imbalance_5 == Decimal("1") / Decimal("9")
    assert book.notional_to_10bps == Decimal("59999")*2 + Decimal("60001")*Decimal("1.5") + Decimal("59990")*3 + Decimal("60010")*Decimal("2.5")


def test_book_rejects_unexpected_asset():
    with make_client({}) as client:
        with pytest.raises(ValueError, match="unsupported Hyperliquid asset"):
            client.fetch_book("DOGE")


def test_http_5xx_retries_three_times_then_fails():
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(503, json={"error": "busy"}, request=request)
    with HyperliquidClient("https://api.hyperliquid.xyz/info", transport=httpx.MockTransport(handler), sleeper=lambda _x: None) as client:
        with pytest.raises(HLClientError):
            client.fetch_book("BTC")
    assert len(calls) == 3


def test_malformed_json_shape_fails():
    with make_client({"unexpected": True}) as client:
        with pytest.raises(HLResponseError, match="candleSnapshot response"):
            client.fetch_candles("BTC", "1h", 1, 2)


def test_context_rejects_missing_frozen_asset():
    payload = [
        {"universe": [{"name": "BTC"}, {"name": "ETH"}]},
        [{}, {}],
    ]
    with make_client(payload) as client:
        with pytest.raises(HLResponseError, match="required asset"):
            client.fetch_contexts()


def test_four_hundred_error_is_not_retried():
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"error": "bad request"}, request=request)
    with HyperliquidClient("https://api.hyperliquid.xyz/info", transport=httpx.MockTransport(handler), sleeper=lambda _x: None) as client:
        with pytest.raises(httpx.HTTPStatusError):
            client.fetch_book("BTC")
    assert len(calls) == 1


def test_invalid_candle_timestamp_fails():
    payload = candle_payload()
    payload[0]["t"] = 0
    with make_client(payload) as client:
        with pytest.raises(HLResponseError, match="timestamp"):
            client.fetch_candles("BTC", "1h", 1, 2)
