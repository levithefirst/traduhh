from __future__ import annotations
import json
from agent.hl_client import AssetContext, HyperliquidClient

def fetch_contexts(client: HyperliquidClient): return client.fetch_contexts()
def upsert_contexts(conn, contexts: list[AssetContext]) -> int:
    if not contexts: return 0
    with conn.transaction():
        with conn.cursor() as cur:
            for x in contexts:
                cur.execute("""INSERT INTO asset_ctx(venue,asset,ts,mid,mark,oracle,funding,premium,oi,day_ntl_vlm,impact_bid,impact_ask,prev_day_px,raw) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT (venue,asset,ts) DO UPDATE SET mid=EXCLUDED.mid,mark=EXCLUDED.mark,oracle=EXCLUDED.oracle,funding=EXCLUDED.funding,premium=EXCLUDED.premium,oi=EXCLUDED.oi,day_ntl_vlm=EXCLUDED.day_ntl_vlm,impact_bid=EXCLUDED.impact_bid,impact_ask=EXCLUDED.impact_ask,prev_day_px=EXCLUDED.prev_day_px,raw=EXCLUDED.raw""",(x.venue,x.asset,x.ts,x.mid,x.mark,x.oracle,x.funding,x.premium,x.oi,x.day_ntl_vlm,x.impact_bid,x.impact_ask,x.prev_day_px,json.dumps(x.raw,separators=(",",":"))))
                if x.missing_fields:
                    cur.execute("INSERT INTO audit_log(ts,actor,action,payload) VALUES (%s,%s,%s,%s::jsonb)",(x.ts,"hl_client","ctx_missing_fields",json.dumps({"asset":x.asset,"fields":list(x.missing_fields)},separators=(",",":"))))
    return len(contexts)
