from __future__ import annotations
import logging
import json
from datetime import timedelta
from decimal import Decimal
from typing import Iterable
from agent.config import FROZEN_ASSETS, FROZEN_TIMEFRAMES
from agent.hl_client import Candle, HyperliquidClient
from agent.timeutil import utc_now
LOGGER=logging.getLogger(__name__)

def fetch_recent_candles(client, asset, timeframe, *, bars=5):
    if asset not in FROZEN_ASSETS: raise ValueError(f"unsupported asset: {asset}")
    if timeframe not in FROZEN_TIMEFRAMES: raise ValueError(f"unsupported timeframe: {timeframe}")
    if bars<=0 or bars>5000: raise ValueError("bars must be between 1 and 5000")
    ms={"15m":15*60_000,"1h":60*60_000,"4h":4*60*60_000}[timeframe]; end=int(utc_now().timestamp()*1000)
    return client.fetch_candles(asset,timeframe,end-bars*ms,end)

def backfill_btc_1h(client, *, bars=3000):
    if bars<=0 or bars>5000: raise ValueError("bars must be between 1 and 5000")
    end=int(utc_now().timestamp()*1000); candles=client.fetch_candles("BTC","1h",end-bars*3_600_000,end)
    LOGGER.info("btc_1h_backfill_fetched",extra={"event":"btc_1h_backfill_fetched","count":len(candles)})
    return candles

def upsert_candles(conn, candles: Iterable[Candle]) -> int:
    rows=list(candles)
    if not rows: return 0
    now=utc_now(); count=0
    intervals={"15m":900,"1h":3600,"4h":14400}
    with conn.transaction():
        with conn.cursor() as cur:
            for c in rows:
                cur.execute("SELECT close_time,h,l,c FROM candles WHERE venue=%s AND asset=%s AND timeframe=%s AND open_time=%s FOR UPDATE",(c.venue,c.asset,c.timeframe,c.open_time))
                existing=cur.fetchone(); anomaly=False
                if existing:
                    close,old_h,old_l,old_c=existing
                    if now-close>=timedelta(seconds=2*intervals[c.timeframe]) and c.close_time<=now:
                        anomaly=any(old is not None and old!=0 and abs((new-old)/old)>Decimal("0.0001") for old,new in ((old_h,c.h),(old_l,c.l),(old_c,c.c)))
                if anomaly:
                    cur.execute("UPDATE candles SET ingested_at=%s WHERE venue=%s AND asset=%s AND timeframe=%s AND open_time=%s",(now,c.venue,c.asset,c.timeframe,c.open_time))
                    cur.execute("INSERT INTO audit_log(ts,actor,action,payload) VALUES (%s,%s,%s,%s::jsonb)",(now,"hl_client","candle_anomaly",json.dumps({"venue":c.venue,"asset":c.asset,"timeframe":c.timeframe,"open_time":c.open_time.isoformat(),"reason":"closed_bar_ohlc_changed_gt_0.01pct"},separators=(",",":"))))
                else:
                    cur.execute("""INSERT INTO candles(venue,asset,timeframe,open_time,close_time,o,h,l,c,v,n_trades,source,ingested_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (venue,asset,timeframe,open_time) DO UPDATE SET close_time=EXCLUDED.close_time,o=EXCLUDED.o,h=EXCLUDED.h,l=EXCLUDED.l,c=EXCLUDED.c,v=EXCLUDED.v,n_trades=EXCLUDED.n_trades,source=EXCLUDED.source,ingested_at=EXCLUDED.ingested_at""",(c.venue,c.asset,c.timeframe,c.open_time,c.close_time,c.o,c.h,c.l,c.c,c.v,c.n_trades,c.source,now))
                count+=1
    return count
