from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from price_stream import choose_snap, round_price, snapshot_from_stream_tick, tick_time, venue_time_from_quote
from trade_book import quote_is_live

NY = ZoneInfo("America/New_York")


def _now_closed() -> datetime:
    return datetime(2026, 9, 22, 21, 50, tzinfo=NY)


def _now_regular() -> datetime:
    return datetime(2026, 9, 22, 10, 0, tzinfo=NY)


def _tick(**overrides):
    base = {
        "id": "NVDA",
        "price": 228.369995,
        "time": "1790128287000",
        "price_hint": "2",
        "market_hours": 4,
    }
    base.update(overrides)
    return base


def test_tick_time_is_milliseconds():
    parsed = tick_time({"time": "1790128287000"})
    assert parsed is not None
    assert parsed == datetime.fromtimestamp(1790128287, tz=timezone.utc)


def test_round_price_uses_price_hint():
    assert round_price(_tick()) == 228.37


def test_closed_stream_tick_updates_price_but_is_not_live():
    now = datetime.fromtimestamp(1790128287, tz=timezone.utc).astimezone(NY)
    snap = snapshot_from_stream_tick(_tick(), now=now)
    assert snap is not None
    assert snap["price"] == 228.37
    assert snap["source"] == "yahoo_stream"
    assert snap["session"] == "closed"
    assert snap["stale"] is True
    assert quote_is_live({"quote": snap}) is False


def test_regular_fresh_tick_is_live():
    now = _now_regular()
    trade_ms = int(now.timestamp() * 1000)
    snap = snapshot_from_stream_tick(_tick(time=str(trade_ms), market_hours=1), now=now)
    assert snap is not None
    assert snap["session"] == "regular"
    assert snap["stale"] is False
    assert quote_is_live({"quote": snap}) is True


def test_regular_old_tick_is_stale():
    now = _now_regular()
    old = now - timedelta(seconds=180)
    snap = snapshot_from_stream_tick(_tick(time=str(int(old.timestamp() * 1000))), now=now)
    assert snap is not None
    assert snap["stale"] is True
    assert quote_is_live({"quote": snap}) is False


def test_choose_snap_keeps_newer_stream_over_robinhood_close():
    stream = {
        "symbol": "NVDA",
        "price": 228.37,
        "updated_at": "2026-09-23T01:51:27+00:00",
        "fetched_at": "2026-09-23T01:51:27+00:00",
        "source": "yahoo_stream",
        "session": "closed",
        "stale": True,
        "bid": None,
        "ask": None,
        "error": None,
    }
    robinhood = {
        "symbol": "NVDA",
        "price": 228.56,
        "updated_at": "2026-09-22T23:59:55+00:00",
        "fetched_at": "2026-09-23T01:46:00+00:00",
        "source": "robinhood",
        "session": "closed",
        "stale": True,
        "bid": 226.99,
        "ask": 228.8,
        "error": None,
    }
    chosen = choose_snap(stream, robinhood)
    assert chosen is not None
    assert chosen["price"] == 228.37
    assert chosen["source"] == "yahoo_stream"
    assert chosen["bid"] == 226.99
    assert chosen["ask"] == 228.8
    assert chosen["stale"] is True


def test_choose_snap_prefers_newer_robinhood_print():
    stream = {
        "symbol": "NVDA",
        "price": 228.10,
        "updated_at": "2026-09-23T14:00:00+00:00",
        "source": "yahoo_stream",
        "session": "regular",
        "stale": False,
        "bid": None,
        "ask": None,
    }
    robinhood = {
        "symbol": "NVDA",
        "price": 228.40,
        "updated_at": "2026-09-23T14:00:05+00:00",
        "source": "robinhood",
        "session": "regular",
        "stale": False,
        "bid": 228.3,
        "ask": 228.5,
    }
    chosen = choose_snap(stream, robinhood)
    assert chosen is not None
    assert chosen["price"] == 228.40
    assert chosen["source"] == "robinhood"


def test_venue_time_uses_extended_stamp_when_extended_price_present():
    quote = {
        "last_extended_hours_trade_price": "228.560000",
        "venue_last_non_reg_trade_time": "2026-09-22T23:59:55.824623857Z",
        "venue_last_trade_time": "2026-09-22T19:59:59.997103092Z",
        "updated_at": "2026-09-23T00:00:00Z",
    }
    parsed = venue_time_from_quote(quote)
    assert parsed is not None
    assert parsed.hour == 23 and parsed.minute == 59


def test_missing_price_is_not_a_snapshot():
    assert snapshot_from_stream_tick({"id": "NVDA", "time": "1790128287000"}, now=_now_closed()) is None