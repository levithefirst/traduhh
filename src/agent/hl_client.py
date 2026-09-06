from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

import httpx

from agent.config import FROZEN_ASSETS, FROZEN_TIMEFRAMES, HL_CONNECT_TIMEOUT_S, HL_READ_TIMEOUT_S, HL_RETRY_DELAYS_S
from agent.timeutil import require_utc

LOGGER = logging.getLogger(__name__)
VENUE = "hyperliquid"

class HLClientError(RuntimeError): pass
class HLHTTPError(HLClientError): pass
class HLResponseError(HLClientError): pass

@dataclass(frozen=True)
class Candle:
    venue: str; asset: str; timeframe: str; open_time: Any; close_time: Any
    o: Decimal; h: Decimal; l: Decimal; c: Decimal; v: Decimal; n_trades: int
    source: str = "hyperliquid:candleSnapshot"

@dataclass(frozen=True)
class AssetContext:
    venue: str; asset: str; ts: Any
    mid: Decimal | None; mark: Decimal | None; oracle: Decimal | None
    funding: Decimal | None; premium: Decimal | None; oi: Decimal | None
    day_ntl_vlm: Decimal | None; impact_bid: Decimal | None; impact_ask: Decimal | None
    prev_day_px: Decimal | None; raw: dict[str, Any]; missing_fields: tuple[str, ...] = ()

@dataclass(frozen=True)
class BookSnapshot:
    venue: str; asset: str; ts: Any
    bid1: Decimal | None; ask1: Decimal | None; bid1_sz: Decimal | None; ask1_sz: Decimal | None
    spread: Decimal | None; spread_bps: Decimal | None; bid_sz_5: Decimal | None; ask_sz_5: Decimal | None
    imbalance_5: Decimal | None; notional_to_10bps: Decimal | None; raw_top: dict[str, Any]

def _decimal(value: Any, *, field: str, required: bool = True) -> Decimal | None:
    if value is None:
        if required: raise HLResponseError(f"missing required numeric field: {field}")
        return None
    try: number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc: raise HLResponseError(f"invalid numeric field: {field}") from exc
    if not number.is_finite(): raise HLResponseError(f"non-finite numeric field: {field}")
    return number

def _epoch_ms(value: Any, *, field: str):
    try: ms = int(value)
    except (TypeError, ValueError) as exc: raise HLResponseError(f"invalid timestamp field: {field}") from exc
    if ms <= 0: raise HLResponseError(f"invalid timestamp field: {field}")
    from datetime import datetime, timezone
    return require_utc(datetime.fromtimestamp(ms / 1000, tz=timezone.utc))

def _validate_asset(asset: str) -> str:
    if asset not in FROZEN_ASSETS: raise ValueError(f"unsupported Hyperliquid asset: {asset}")
    return asset

def _validate_timeframe(timeframe: str) -> str:
    if timeframe not in FROZEN_TIMEFRAMES: raise ValueError(f"unsupported timeframe: {timeframe}")
    return timeframe

class HyperliquidClient:
    """Raw HTTP wrapper for Hyperliquid public mainnet /info market data."""
    def __init__(self, base_url: str, *, timeout: httpx.Timeout | None = None, transport: httpx.BaseTransport | None = None, client: httpx.Client | None = None, sleeper=time.sleep) -> None:
        self.base_url = base_url; self._sleeper = sleeper; self._owned_client = client is None
        self._client = client or httpx.Client(timeout=timeout or httpx.Timeout(HL_READ_TIMEOUT_S, connect=HL_CONNECT_TIMEOUT_S), transport=transport, headers={"Content-Type":"application/json"})
    def close(self) -> None:
        if self._owned_client: self._client.close()
    def __enter__(self): return self
    def __exit__(self, *_args): self.close()
    def _post_info(self, payload: Mapping[str, Any]) -> Any:
        delays = HL_RETRY_DELAYS_S
        for attempt in range(3):
            try:
                response = self._client.post(self.base_url, json=dict(payload))
                if response.status_code == 429 or response.status_code >= 500: raise HLHTTPError(f"Hyperliquid HTTP {response.status_code}")
                response.raise_for_status()
                try: return response.json()
                except ValueError as exc: raise HLResponseError("Hyperliquid response was not valid JSON") from exc
            except HLHTTPError as exc:
                if attempt == 2: raise
                LOGGER.warning("hl_request_retry", extra={"event":"hl_request_retry","attempt":attempt+1,"error":str(exc)})
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                if attempt == 2: raise HLHTTPError("Hyperliquid request timed out or connection failed") from exc
                LOGGER.warning("hl_request_retry", extra={"event":"hl_request_retry","attempt":attempt+1,"error":str(exc)})
            self._sleeper(delays[attempt])
        raise AssertionError("unreachable")
    def fetch_candles(self, asset: str, timeframe: str, start_ms: int, end_ms: int) -> list[Candle]:
        asset=_validate_asset(asset); timeframe=_validate_timeframe(timeframe)
        if start_ms < 0 or end_ms <= start_ms: raise ValueError("invalid candle time range")
        raw=self._post_info({"type":"candleSnapshot","req":{"coin":asset,"interval":timeframe,"startTime":start_ms,"endTime":end_ms}})
        if raw == []:
            LOGGER.warning("hl_empty_candles", extra={"event":"hl_empty_candles","asset":asset,"tf":timeframe}); return []
        if not isinstance(raw,list): raise HLResponseError("candleSnapshot response must be a list")
        out=[]
        for item in raw:
            if not isinstance(item,Mapping): raise HLResponseError("candleSnapshot item must be an object")
            if item.get("s") != asset: raise HLResponseError(f"unexpected candle symbol: {item.get('s')!r}")
            if item.get("i") != timeframe: raise HLResponseError(f"unexpected candle interval: {item.get('i')!r}")
            ot=_epoch_ms(item.get("t"),field="t"); ct=_epoch_ms(item.get("T"),field="T")
            if ct <= ot: raise HLResponseError("candle close_time must be after open_time")
            o,h,l,c,v=[_decimal(item.get(k),field=k) for k in ("o","h","l","c","v")]
            assert o is not None and h is not None and l is not None and c is not None and v is not None
            if min(o,h,l,c) <= 0 or v < 0 or h < max(o,c) or l > min(o,c) or h < l: raise HLResponseError("invalid OHLCV values")
            try: n=int(item.get("n"))
            except (TypeError,ValueError) as exc: raise HLResponseError("invalid candle trade count") from exc
            if n < 0: raise HLResponseError("invalid candle trade count")
            out.append(Candle(VENUE,asset,timeframe,ot,ct,o,h,l,c,v,n))
        return out
    def fetch_contexts(self) -> list[AssetContext]:
        raw=self._post_info({"type":"metaAndAssetCtxs"})
        if not isinstance(raw,list) or len(raw)!=2: raise HLResponseError("metaAndAssetCtxs response must be [meta, assetCtxs]")
        meta,contexts=raw
        if not isinstance(meta,Mapping) or not isinstance(contexts,list): raise HLResponseError("malformed metaAndAssetCtxs response")
        universe=meta.get("universe")
        if not isinstance(universe,list) or len(universe)!=len(contexts): raise HLResponseError("meta universe and assetCtxs are not index-aligned")
        from datetime import datetime, timezone
        ts=require_utc(datetime.now(timezone.utc)); out=[]
        fields=("midPx","markPx","oraclePx","funding","premium","openInterest","dayNtlVlm","prevDayPx")
        found_assets=set()
        for asset_meta,ctx in zip(universe,contexts):
            if not isinstance(asset_meta,Mapping) or not isinstance(ctx,Mapping): raise HLResponseError("malformed universe/context entry")
            asset=asset_meta.get("name")
            if asset not in FROZEN_ASSETS: continue
            found_assets.add(asset)
            missing=tuple(k for k in fields if k not in ctx)
            impact=ctx.get("impactPxs")
            if not isinstance(impact,Sequence) or isinstance(impact,(str,bytes)) or len(impact)<2: missing += ("impactPxs",); impact=None
            ib=_decimal(impact[0],field="impactPxs[0]",required=False) if impact is not None else None
            ia=_decimal(impact[1],field="impactPxs[1]",required=False) if impact is not None else None
            vals=[_decimal(ctx.get(k),field=k,required=False) for k in fields]
            out.append(AssetContext(
                VENUE, asset, ts,
                vals[0], vals[1], vals[2], vals[3], vals[4], vals[5], vals[6],
                ib, ia, vals[7], raw=dict(ctx), missing_fields=missing,
            ))
        missing_assets=sorted(set(FROZEN_ASSETS)-found_assets)
        if missing_assets:
            raise HLResponseError("required asset(s) missing from metaAndAssetCtxs: " + ", ".join(missing_assets))
        return out
    def fetch_book(self, asset: str) -> BookSnapshot:
        asset=_validate_asset(asset); raw=self._post_info({"type":"l2Book","coin":asset})
        if not isinstance(raw,Mapping): raise HLResponseError("l2Book response must be an object")
        if raw.get("coin") != asset: raise HLResponseError(f"unexpected book symbol: {raw.get('coin')!r}")
        ts=_epoch_ms(raw.get("time"),field="time"); levels=raw.get("levels")
        if not isinstance(levels,list) or len(levels)!=2: raise HLResponseError("l2Book levels must be [bids, asks]")
        bids,asks=levels
        if not isinstance(bids,list) or not isinstance(asks,list): raise HLResponseError("l2Book sides must be lists")
        def parse(side,name):
            parsed=[]
            for level in side[:20]:
                if not isinstance(level,Mapping): raise HLResponseError(f"malformed {name} level")
                px=_decimal(level.get("px"),field=f"{name}.px"); sz=_decimal(level.get("sz"),field=f"{name}.sz")
                try: n=int(level.get("n"))
                except (TypeError,ValueError) as exc: raise HLResponseError(f"invalid {name}.n") from exc
                if px is None or sz is None or px<=0 or sz<0 or n<0: raise HLResponseError(f"invalid {name} level")
                parsed.append((px,sz,n))
            return parsed
        if not bids or not asks:
            LOGGER.warning("hl_empty_book_side", extra={"event":"hl_empty_book_side","asset":asset})
            return BookSnapshot(VENUE,asset,ts,None,None,None,None,None,None,None,None,None,{"coin":asset,"time":raw.get("time"),"bids":bids[:20],"asks":asks[:20]})
        pb,pa=parse(bids,"bid"),parse(asks,"ask"); bid1,bid1_sz,_=pb[0]; ask1,ask1_sz,_=pa[0]
        if ask1 < bid1: raise HLResponseError("crossed L2 book")
        mid=(bid1+ask1)/2; spread=ask1-bid1; spread_bps=Decimal("10000")*spread/mid if mid else None
        bid_sz_5=sum((x[1] for x in pb[:5]),Decimal(0)); ask_sz_5=sum((x[1] for x in pa[:5]),Decimal(0)); total=bid_sz_5+ask_sz_5
        imbalance=(bid_sz_5-ask_sz_5)/total if total else None; lower=mid*Decimal("0.999"); upper=mid*Decimal("1.001")
        notional=sum((px*sz for px,sz,_ in pb if px>=lower),Decimal(0))+sum((px*sz for px,sz,_ in pa if px<=upper),Decimal(0))
        return BookSnapshot(VENUE,asset,ts,bid1,ask1,bid1_sz,ask1_sz,spread,spread_bps,bid_sz_5,ask_sz_5,imbalance,notional,{"coin":asset,"time":raw["time"],"bids":bids[:20],"asks":asks[:20]})
