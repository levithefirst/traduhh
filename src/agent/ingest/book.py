from __future__ import annotations
import json
from datetime import timedelta
from agent.config import BOOK_RETENTION_DAYS, FROZEN_ASSETS
from agent.hl_client import BookSnapshot, HyperliquidClient
from agent.timeutil import utc_now

def fetch_book(client: HyperliquidClient, asset: str) -> BookSnapshot:
    if asset not in FROZEN_ASSETS: raise ValueError(f"unsupported asset: {asset}")
    return client.fetch_book(asset)
def upsert_book(conn, snapshot: BookSnapshot) -> int:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO book_snapshots(venue,asset,ts,bid1,ask1,bid1_sz,ask1_sz,spread,spread_bps,bid_sz_5,ask_sz_5,imbalance_5,notional_to_10bps,raw_top) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT (venue,asset,ts) DO UPDATE SET bid1=EXCLUDED.bid1,ask1=EXCLUDED.ask1,bid1_sz=EXCLUDED.bid1_sz,ask1_sz=EXCLUDED.ask1_sz,spread=EXCLUDED.spread,spread_bps=EXCLUDED.spread_bps,bid_sz_5=EXCLUDED.bid_sz_5,ask_sz_5=EXCLUDED.ask_sz_5,imbalance_5=EXCLUDED.imbalance_5,notional_to_10bps=EXCLUDED.notional_to_10bps,raw_top=EXCLUDED.raw_top""",(snapshot.venue,snapshot.asset,snapshot.ts,snapshot.bid1,snapshot.ask1,snapshot.bid1_sz,snapshot.ask1_sz,snapshot.spread,snapshot.spread_bps,snapshot.bid_sz_5,snapshot.ask_sz_5,snapshot.imbalance_5,snapshot.notional_to_10bps,json.dumps(snapshot.raw_top,separators=(",",":"))))
            if snapshot.bid1 is None or snapshot.ask1 is None:
                cur.execute(
                    "INSERT INTO audit_log(ts,actor,action,payload) VALUES (%s,%s,%s,%s::jsonb)",
                    (snapshot.ts, "hl_client", "book_missing_top", json.dumps({"asset": snapshot.asset}, separators=(",", ":"))),
                )
            cur.execute("DELETE FROM book_snapshots WHERE venue=%s AND asset=%s AND ts < %s",(snapshot.venue,snapshot.asset,utc_now()-timedelta(days=BOOK_RETENTION_DAYS)))
    return 1
