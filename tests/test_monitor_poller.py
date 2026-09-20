from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from monitor_poller import health_from_log, latest_snapshots, summarize_health


NY = ZoneInfo("America/New_York")


def _row(symbol, fetched_at, *, stale=False, price=1.0, session="regular"):
    return {
        "symbol": symbol,
        "price": price,
        "fetched_at": fetched_at.isoformat(),
        "source": "robinhood",
        "session": session,
        "stale": stale,
        "error": None,
    }


def test_latest_snapshots_keep_last_row_per_symbol(tmp_path: Path):
    now = datetime(2026, 9, 18, 10, 0, tzinfo=NY)
    path = tmp_path / "quotes.jsonl"
    from price_monitor import snapshots_to_jsonl

    snapshots_to_jsonl(
        path,
        [
            _row("SPY", now - timedelta(seconds=120), price=1.0),
            _row("BTC-USD", now - timedelta(seconds=120), price=2.0, session="crypto"),
            _row("SPY", now - timedelta(seconds=5), price=1.5),
        ],
    )
    latest = latest_snapshots(path)
    assert latest["SPY"]["price"] == 1.5
    assert latest["BTC-USD"]["price"] == 2.0


def test_health_ok_when_latest_fetch_is_within_a_minute(tmp_path: Path):
    now = datetime(2026, 9, 18, 10, 0, tzinfo=NY)
    path = tmp_path / "quotes.jsonl"
    from price_monitor import snapshots_to_jsonl

    snapshots_to_jsonl(path, [_row("SPY", now - timedelta(seconds=20))])
    report = health_from_log(path, now=now, max_age=90)
    assert report["status"] == "POLLER_OK"
    assert report["symbols"]["SPY"]["ok"] is True


def test_health_lag_when_jsonl_is_older_than_a_minute(tmp_path: Path):
    now = datetime(2026, 9, 18, 10, 0, tzinfo=NY)
    path = tmp_path / "quotes.jsonl"
    from price_monitor import snapshots_to_jsonl

    snapshots_to_jsonl(path, [_row("SPY", now - timedelta(seconds=180))])
    report = health_from_log(path, now=now, max_age=90)
    assert report["status"] == "POLLER_LAG"
    assert report["symbols"]["SPY"]["ok"] is False


def test_health_down_when_log_missing(tmp_path: Path):
    report = health_from_log(tmp_path / "missing.jsonl", now=datetime.now(NY), max_age=90)
    assert report["status"] == "POLLER_DOWN"


def test_summarize_health_is_one_line():
    line = summarize_health(
        {
            "status": "POLLER_OK",
            "age_seconds": 12.0,
            "n_symbols": 2,
            "n_ok": 2,
            "n_stale": 0,
            "n_error": 0,
        }
    )
    assert "POLLER_OK" in line
    assert "symbols=2" in line


def test_health_ignores_symbols_from_an_older_watchlist(tmp_path: Path):
    now = datetime(2026, 9, 18, 10, 0, tzinfo=NY)
    path = tmp_path / "quotes.jsonl"
    from price_monitor import snapshots_to_jsonl

    snapshots_to_jsonl(
        path,
        [
            _row("IWM", now - timedelta(seconds=180), price=1.0),
            _row("SPY", now - timedelta(seconds=5), price=2.0),
            _row("BTC-USD", now - timedelta(seconds=5), price=3.0, session="crypto"),
        ],
    )
    report = health_from_log(path, now=now, max_age=90)
    assert report["status"] == "POLLER_OK"
    assert set(report["symbols"]) == {"SPY", "BTC-USD"}
    assert "IWM" not in report["symbols"]


def test_health_ok_for_closed_equity_heartbeat_under_15_minutes(tmp_path: Path):
    now = datetime(2026, 9, 19, 21, 0, tzinfo=NY)
    path = tmp_path / "quotes.jsonl"
    from price_monitor import snapshots_to_jsonl

    snapshots_to_jsonl(
        path,
        [
            _row(
                "SPY",
                now - timedelta(seconds=200),
                stale=True,
                session="closed",
            )
            | {"asset_class": "equity"}
        ],
    )
    report = health_from_log(path, now=now)
    assert report["status"] == "POLLER_OK"


def test_health_lags_when_closed_equity_heartbeat_is_overdue(tmp_path: Path):
    now = datetime(2026, 9, 19, 21, 0, tzinfo=NY)
    path = tmp_path / "quotes.jsonl"
    from price_monitor import snapshots_to_jsonl

    snapshots_to_jsonl(
        path,
        [
            _row(
                "SPY",
                now - timedelta(seconds=1000),
                stale=True,
                session="closed",
            )
            | {"asset_class": "equity"}
        ],
    )
    report = health_from_log(path, now=now)
    assert report["status"] == "POLLER_LAG"
