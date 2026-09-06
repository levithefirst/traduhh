from datetime import datetime, timedelta, timezone
from decimal import Decimal

from agent.hl_client import AssetContext, BookSnapshot, Candle
from agent.ingest.candles import backfill_btc_1h, upsert_candles
from agent.ingest.context import upsert_contexts
from agent.ingest.book import upsert_book


class FakeCursor:
    def __init__(self): self.calls=[]; self.fetchone_value=None
    def execute(self, sql, params=None): self.calls.append((sql, params))
    def fetchone(self): return self.fetchone_value
    def __enter__(self): return self
    def __exit__(self,*a): pass

class FakeConn:
    def __init__(self): self.cursor_obj=FakeCursor(); self.transactions=0
    def cursor(self): return self.cursor_obj
    def transaction(self): return self
    def __enter__(self): self.transactions += 1; return self
    def __exit__(self,*a): pass


def candle():
    t=datetime(2026,9,5,10,tzinfo=timezone.utc)
    return Candle("hyperliquid","BTC","1h",t,t+timedelta(hours=1),Decimal("60000"),Decimal("60200"),Decimal("59900"),Decimal("60100"),Decimal("10"),4)


def test_candle_persistence_uses_unique_upsert_and_is_restart_safe():
    conn=FakeConn(); assert upsert_candles(conn,[candle()]) == 1
    sqls=[x[0] for x in conn.cursor_obj.calls]
    assert any("ON CONFLICT (venue,asset,timeframe,open_time) DO UPDATE" in s for s in sqls)


def test_context_persistence_uses_unique_upsert():
    conn=FakeConn(); t=datetime(2026,9,5,10,tzinfo=timezone.utc)
    ctx=AssetContext("hyperliquid","BTC",t,Decimal("1"),Decimal("1"),Decimal("1"),Decimal("0"),Decimal("0"),Decimal("1"),Decimal("2"),Decimal("1"),Decimal("1"),Decimal("1"),{})
    assert upsert_contexts(conn,[ctx]) == 1
    assert any("ON CONFLICT (venue,asset,ts) DO UPDATE" in s for s,_ in conn.cursor_obj.calls)


def test_book_persistence_uses_unique_upsert_and_retention():
    conn=FakeConn(); t=datetime(2026,9,5,10,tzinfo=timezone.utc)
    book=BookSnapshot("hyperliquid","BTC",t,Decimal("1"),Decimal("2"),Decimal("1"),Decimal("1"),Decimal("1"),Decimal("10000"),Decimal("1"),Decimal("1"),Decimal("0"),Decimal("3"),{})
    assert upsert_book(conn,book) == 1
    assert any("ON CONFLICT (venue,asset,ts) DO UPDATE" in s for s,_ in conn.cursor_obj.calls)
    assert any("DELETE FROM book_snapshots" in s for s,_ in conn.cursor_obj.calls)


def test_backfill_contract_requests_btc_1h_3000(monkeypatch):
    class Stub:
        def __init__(self): self.args=None
        def fetch_candles(self,*args): self.args=args; return []
    stub=Stub(); assert backfill_btc_1h(stub)==[]
    asset,tf,start,end=stub.args
    assert asset=="BTC" and tf=="1h"
    assert end-start == 3000*3600*1000
