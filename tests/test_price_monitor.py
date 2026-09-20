from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.error import URLError

import pandas as pd
import pytest

from price_monitor import (
    DEFAULT_WATCHLIST,
    MAX_WATCHLIST,
    PollerLocked,
    acquire_poller_lock,
    closed_poll_interval,
    format_line,
    interval_for_session,
    load_watchlist,
    normalize_watchlist,
    open_poll_interval,
    outage_flag,
    parse_robinhood_crypto_quote,
    parse_robinhood_quote,
    poll_robinhood,
    poll_yahoo,
    session_at,
    should_poll_yahoo,
    snapshot_from_error,
    snapshots_to_jsonl,
    stale_after,
)


NY = ZoneInfo("America/New_York")


def _dt(year, month, day, hour, minute, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=NY)


# Friday 2026-09-18 is a weekday; Saturday 2026-09-19 is weekend.
FRI = dict(year=2026, month=9, day=18)
SAT = dict(year=2026, month=9, day=19)


def test_default_watchlist_is_the_approved_ten():
    assert DEFAULT_WATCHLIST == [
        "SPY",
        "QQQ",
        "IWM",
        "AAPL",
        "MSFT",
        "NVDA",
        "AMZN",
        "META",
        "GOOGL",
        "TLT",
    ]


def test_session_pre_regular_post_closed_on_weekday():
    assert session_at(_dt(**FRI, hour=4, minute=0)) == "pre"
    assert session_at(_dt(**FRI, hour=9, minute=29)) == "pre"
    assert session_at(_dt(**FRI, hour=9, minute=30)) == "regular"
    assert session_at(_dt(**FRI, hour=15, minute=59)) == "regular"
    assert session_at(_dt(**FRI, hour=16, minute=0)) == "post"
    assert session_at(_dt(**FRI, hour=19, minute=59)) == "post"
    assert session_at(_dt(**FRI, hour=20, minute=0)) == "closed"
    assert session_at(_dt(**FRI, hour=3, minute=59)) == "closed"


def test_weekend_is_closed_including_weekday_extended_hours_windows():
    assert session_at(_dt(**SAT, hour=10, minute=0)) == "closed"
    assert session_at(_dt(**SAT, hour=4, minute=0)) == "closed"
    assert session_at(_dt(**SAT, hour=16, minute=30)) == "closed"


def test_naive_datetime_is_interpreted_as_america_new_york():
    naive = datetime(2026, 9, 18, 9, 30)
    assert session_at(naive) == "regular"


def test_robinhood_price_prefers_extended_hours_trade_price():
    now = _dt(**FRI, hour=17, minute=5)
    snap = parse_robinhood_quote(
        {
            "symbol": "SPY",
            "last_trade_price": "500.00",
            "last_extended_hours_trade_price": "501.25",
            "bid_price": "501.20",
            "ask_price": "501.30",
            "updated_at": "2026-09-18T21:05:00Z",
        },
        now=now,
        fetched_at=now,
    )
    assert snap["symbol"] == "SPY"
    assert snap["price"] == 501.25
    assert snap["last_regular"] == 500.0
    assert snap["last_extended"] == 501.25
    assert snap["bid"] == 501.20
    assert snap["ask"] == 501.30
    assert snap["source"] == "robinhood"
    assert snap["session"] == "post"
    assert snap["stale"] is False
    assert snap["error"] is None
    assert snap["updated_at"] == "2026-09-18T21:05:00Z"
    assert snap["bar_time"] == snap["updated_at"]


def test_robinhood_price_falls_back_to_last_trade_when_extended_missing():
    now = _dt(**FRI, hour=10, minute=0)
    snap = parse_robinhood_quote(
        {
            "symbol": "AAPL",
            "last_trade_price": "220.10",
            "last_extended_hours_trade_price": None,
            "bid_price": "220.09",
            "ask_price": "220.11",
            "updated_at": "2026-09-18T14:00:00Z",
        },
        now=now,
        fetched_at=now,
    )
    assert snap["price"] == 220.10
    assert snap["last_extended"] is None
    assert snap["session"] == "regular"
    assert snap["stale"] is False


def test_empty_extended_price_is_treated_as_missing():
    now = _dt(**FRI, hour=10, minute=0)
    snap = parse_robinhood_quote(
        {
            "symbol": "MSFT",
            "last_trade_price": "400.00",
            "last_extended_hours_trade_price": "",
            "bid_price": "399.90",
            "ask_price": "400.10",
            "updated_at": "2026-09-18T14:00:00Z",
        },
        now=now,
        fetched_at=now,
    )
    assert snap["price"] == 400.00
    assert snap["last_extended"] is None


def test_closed_session_marks_snapshot_stale():
    now = _dt(**FRI, hour=21, minute=0)
    snap = parse_robinhood_quote(
        {
            "symbol": "QQQ",
            "last_trade_price": "480.00",
            "last_extended_hours_trade_price": "479.50",
            "bid_price": "0",
            "ask_price": "0",
            "updated_at": "2026-09-19T00:00:00Z",
        },
        now=now,
        fetched_at=now,
    )
    assert snap["session"] == "closed"
    assert snap["stale"] is True
    assert snap["price"] == 479.50


def test_updated_at_older_than_120_seconds_is_stale_during_open_session():
    now = _dt(**FRI, hour=10, minute=0)
    snap = parse_robinhood_quote(
        {
            "symbol": "IWM",
            "last_trade_price": "220.00",
            "last_extended_hours_trade_price": None,
            "bid_price": "219.90",
            "ask_price": "220.10",
            "updated_at": (now - timedelta(seconds=121)).astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        },
        now=now,
        fetched_at=now,
    )
    assert snap["session"] == "regular"
    assert snap["stale"] is True


def test_updated_at_within_120_seconds_is_not_stale():
    now = _dt(**FRI, hour=10, minute=0)
    snap = parse_robinhood_quote(
        {
            "symbol": "TLT",
            "last_trade_price": "90.00",
            "last_extended_hours_trade_price": None,
            "bid_price": "89.99",
            "ask_price": "90.01",
            "updated_at": (now - timedelta(seconds=30)).astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        },
        now=now,
        fetched_at=now,
    )
    assert snap["stale"] is False


def test_missing_symbol_is_no_data_and_stale_without_a_price():
    now = _dt(**FRI, hour=10, minute=0)
    snap = snapshot_from_error("NVDA", error="no_data", now=now, source="robinhood")
    assert snap["symbol"] == "NVDA"
    assert snap["error"] == "no_data"
    assert snap["stale"] is True
    assert snap["price"] is None
    assert snap["source"] == "robinhood"
    assert snap["session"] == "regular"


def test_http_error_records_reason_and_does_not_guess_price():
    now = _dt(**FRI, hour=10, minute=0)
    snap = snapshot_from_error("AMZN", error="timeout", now=now, source="robinhood")
    assert snap["error"] == "timeout"
    assert snap["price"] is None
    assert snap["stale"] is True


def test_poll_robinhood_batches_all_symbols_in_one_request():
    now = _dt(**FRI, hour=10, minute=0)
    calls: list[str] = []

    def http_get(url: str):
        calls.append(url)
        return {
            "results": [
                {
                    "symbol": "SPY",
                    "last_trade_price": "500.00",
                    "last_extended_hours_trade_price": None,
                    "bid_price": "499.99",
                    "ask_price": "500.01",
                    "updated_at": "2026-09-18T14:00:00Z",
                }
            ]
        }

    snaps = poll_robinhood(["SPY", "QQQ"], now=now, http_get=http_get)
    assert len(calls) == 1
    assert "symbols=SPY,QQQ" in calls[0]
    by_symbol = {s["symbol"]: s for s in snaps}
    assert by_symbol["SPY"]["price"] == 500.00
    assert by_symbol["QQQ"]["error"] == "no_data"
    assert by_symbol["QQQ"]["stale"] is True
    assert by_symbol["QQQ"]["price"] is None


def test_poll_robinhood_http_failure_emits_error_row_per_symbol():
    now = _dt(**FRI, hour=10, minute=0)

    def http_get(url: str):
        raise URLError("timed out")

    snaps = poll_robinhood(["SPY", "QQQ"], now=now, http_get=http_get)
    assert [s["symbol"] for s in snaps] == ["SPY", "QQQ"]
    assert all(s["error"] for s in snaps)
    assert all(s["price"] is None for s in snaps)
    assert all(s["stale"] is True for s in snaps)


def test_yahoo_snapshot_includes_ohlcv_from_last_1m_bar():
    now = _dt(**FRI, hour=10, minute=1)
    idx = pd.DatetimeIndex([_dt(**FRI, hour=10, minute=0)])
    frame = pd.DataFrame(
        {
            "Open": [100.0],
            "High": [101.0],
            "Low": [99.5],
            "Close": [100.5],
            "Volume": [12345],
        },
        index=idx,
    )

    def download(tickers, **kwargs):
        assert kwargs["period"] == "1d"
        assert kwargs["interval"] == "1m"
        return frame

    snaps = poll_yahoo(["SPY"], now=now, download=download)
    assert len(snaps) == 1
    snap = snaps[0]
    assert snap["symbol"] == "SPY"
    assert snap["source"] == "yahoo"
    assert snap["price"] == 100.5
    assert snap["open"] == 100.0
    assert snap["high"] == 101.0
    assert snap["low"] == 99.5
    assert snap["volume"] == 12345
    assert snap["session"] == "regular"
    assert snap["stale"] is False


def test_yahoo_is_stale_outside_regular_hours():
    now = _dt(**FRI, hour=17, minute=0)
    idx = pd.DatetimeIndex([_dt(**FRI, hour=15, minute=59)])
    frame = pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [1]},
        index=idx,
    )
    snaps = poll_yahoo(["SPY"], now=now, download=lambda *a, **k: frame)
    assert snaps[0]["session"] == "post"
    assert snaps[0]["stale"] is True


def test_should_not_poll_yahoo_every_minute_in_pre_or_post_when_source_is_both():
    assert should_poll_yahoo("regular", source="both") is True
    assert should_poll_yahoo("pre", source="both") is False
    assert should_poll_yahoo("post", source="both") is False
    assert should_poll_yahoo("closed", source="both") is False
    assert should_poll_yahoo("pre", source="yahoo") is True


def test_open_interval_is_60_seconds_and_will_not_go_faster():
    assert open_poll_interval() == 60
    assert interval_for_session("pre", requested=30) == 60
    assert interval_for_session("regular", requested=60) == 60
    assert interval_for_session("post", requested=5) == 60


def test_closed_interval_is_15_minutes():
    assert closed_poll_interval() == 900
    assert interval_for_session("closed", requested=60) == 900


def test_stale_after_policy_is_120_seconds():
    assert stale_after() == 120


def test_format_line_includes_ext_when_after_hours_traded():
    now = _dt(**FRI, hour=17, minute=5)
    snap = parse_robinhood_quote(
        {
            "symbol": "SPY",
            "last_trade_price": "500.00",
            "last_extended_hours_trade_price": "501.25",
            "bid_price": "501.20",
            "ask_price": "501.30",
            "updated_at": "2026-09-18T21:05:00Z",
        },
        now=now,
        fetched_at=now,
    )
    line = format_line(snap)
    assert "symbol=SPY" in line
    assert "ext=501.25" in line
    assert "price=501.25" in line
    assert "session=post" in line
    assert "source=robinhood" in line
    assert "stale=false" in line


def test_data_bad_after_three_consecutive_failures():
    assert outage_flag(0) is None
    assert outage_flag(2) is None
    assert outage_flag(3) == "DATA_BAD"
    assert outage_flag(5) == "DATA_BAD"


def test_second_poller_cannot_acquire_lock(tmp_path: Path):
    lock_path = tmp_path / "quotes.lock"
    first = acquire_poller_lock(lock_path)
    try:
        with pytest.raises(PollerLocked):
            acquire_poller_lock(lock_path)
    finally:
        first.release()


def test_jsonl_writes_one_record_per_symbol(tmp_path: Path):
    now = _dt(**FRI, hour=10, minute=0)
    snaps = [
        snapshot_from_error("SPY", error="no_data", now=now, source="robinhood"),
        snapshot_from_error("QQQ", error="timeout", now=now, source="robinhood"),
    ]
    path = tmp_path / "quotes.jsonl"
    snapshots_to_jsonl(path, snaps)
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert '"symbol": "SPY"' in lines[0]
    assert '"symbol": "QQQ"' in lines[1]


def test_watchlist_is_capped_at_ten():
    assert MAX_WATCHLIST == 10
    with pytest.raises(ValueError, match="10"):
        normalize_watchlist(["AAPL"] * 11)


def test_watchlist_classifies_crypto_pairs_and_equity_tickers():
    items = normalize_watchlist(["spy", "BTC-USD", "ETHUSD", "SOL", "AAPL"])
    assert [i.symbol for i in items] == ["SPY", "BTC-USD", "ETH-USD", "SOL-USD", "AAPL"]
    assert [i.rh_symbol for i in items] == ["SPY", "BTCUSD", "ETHUSD", "SOLUSD", "AAPL"]
    assert [i.asset_class for i in items] == [
        "equity",
        "crypto",
        "crypto",
        "crypto",
        "equity",
    ]


def test_ambiguous_ticker_is_equity_unless_usd_suffix():
    items = normalize_watchlist(["UNI", "UNI-USD", "W", "W-USD"])
    assert items[0].asset_class == "equity"
    assert items[0].symbol == "UNI"
    assert items[1].asset_class == "crypto"
    assert items[1].rh_symbol == "UNIUSD"
    assert items[2].asset_class == "equity"
    assert items[3].asset_class == "crypto"


def test_load_watchlist_file_skips_comments_and_blanks(tmp_path: Path):
    path = tmp_path / "watchlist.txt"
    path.write_text("# desk list\nSPY\n\nBTC-USD\nETH\n")
    items = load_watchlist(path)
    assert [i.symbol for i in items] == ["SPY", "BTC-USD", "ETH-USD"]


def test_crypto_quote_uses_mark_price_and_is_live_on_weekend():
    now = _dt(**SAT, hour=21, minute=0)
    snap = parse_robinhood_crypto_quote(
        {
            "symbol": "BTCUSD",
            "mark_price": "81275.56",
            "bid_price": "80512.11",
            "ask_price": "82039.00",
            "open_price": "81112.61",
            "high_price": "81921.76",
            "low_price": "80888.79",
            "volume": "12.5",
            "updated_at": "2026-09-20T01:00:00.000Z",
        },
        now=now,
        fetched_at=now,
        display_symbol="BTC-USD",
    )
    assert snap["symbol"] == "BTC-USD"
    assert snap["asset_class"] == "crypto"
    assert snap["price"] == 81275.56
    assert snap["bid"] == 80512.11
    assert snap["ask"] == 82039.00
    assert snap["session"] == "crypto"
    assert snap["stale"] is False
    assert snap["source"] == "robinhood"
    assert snap["open"] == 81112.61
    assert snap["volume"] == 12.5


def test_crypto_keeps_60s_poll_when_equity_session_is_closed():
    assert interval_for_session("closed", requested=60, has_crypto=True) == 60
    assert interval_for_session("closed", requested=60, has_crypto=False) == 900


def test_mixed_watchlist_batches_equity_and_crypto_separately():
    now = _dt(**FRI, hour=10, minute=0)
    calls: list[str] = []

    def http_get(url: str):
        calls.append(url)
        if "forex" in url:
            return {
                "results": [
                    {
                        "symbol": "BTCUSD",
                        "mark_price": "100.0",
                        "bid_price": "99.0",
                        "ask_price": "101.0",
                        "updated_at": "2026-09-18T14:00:00Z",
                    }
                ]
            }
        return {
            "results": [
                {
                    "symbol": "SPY",
                    "last_trade_price": "500.00",
                    "last_extended_hours_trade_price": None,
                    "bid_price": "499.99",
                    "ask_price": "500.01",
                    "updated_at": "2026-09-18T14:00:00Z",
                }
            ]
        }

    snaps = poll_robinhood(["SPY", "BTC-USD"], now=now, http_get=http_get)
    assert len(calls) == 2
    assert any("quotes/?symbols=SPY" in c and "forex" not in c for c in calls)
    assert any("marketdata/forex/quotes/?symbols=BTCUSD" in c for c in calls)
    by_symbol = {s["symbol"]: s for s in snaps}
    assert by_symbol["SPY"]["price"] == 500.00
    assert by_symbol["SPY"]["asset_class"] == "equity"
    assert by_symbol["BTC-USD"]["price"] == 100.0
    assert by_symbol["BTC-USD"]["asset_class"] == "crypto"
    assert [s["symbol"] for s in snaps] == ["SPY", "BTC-USD"]
