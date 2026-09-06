from __future__ import annotations

import os

from agent.db import connect, run_migrations
from agent.hl_client import HyperliquidClient
from agent.ingest.candles import backfill_btc_1h, upsert_candles


def main() -> int:
    database_url = os.environ["DATABASE_URL"]
    info_url = os.environ.get("HL_INFO_URL", "https://api.hyperliquid.xyz/info")
    with HyperliquidClient(info_url) as client:
        candles = backfill_btc_1h(client)
        with connect(database_url) as conn:
            run_migrations(conn)
            count = upsert_candles(conn, candles)
    print({"event": "btc_1h_backfill_complete", "fetched": len(candles), "persisted": count})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
