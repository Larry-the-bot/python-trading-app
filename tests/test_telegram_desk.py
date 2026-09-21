from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import json

from trade_book import empty_book, save_book

NY = ZoneInfo("America/New_York")


def _quote(**extra):
    quote = {
        "bid": 152.6,
        "ask": 154.0,
        "session": "regular",
        "stale": False,
        "source": "robinhood",
        "fetched_at": "2026-09-20T10:00:00-04:00",
        "error": None,
    }
    quote.update(extra)
    return quote


def _name(**overrides):
    row = {
        "symbol": "SPCX",
        "asset_class": "equity",
        "enabled": True,
        "current_price": 152.64,
        "buy_price": 151.0,
        "sell_price": 157.0,
        "position": "flat",
        "state": "watching",
        "signal": "watch",
        "instrument": {
            "kind": "option",
            "right": "call",
            "strike": 150.0,
            "expiration": "2026-10-16",
        },
        "quote": _quote(),
        "notes": "Starship F14",
    }
    row.update(overrides)
    return row


def _book(*names):
    book = empty_book()
    book["names"] = list(names)
    return book


def test_formats_header_price_state_zone_and_core():
    from telegram_desk import format_desk

    text = format_desk(
        _book(_name()),
        now=datetime(2026, 9, 20, 18, 11, tzinfo=NY),
        stops={"SPCX": 144.0},
        zones={"SPCX": [145.0, 151.0]},
    )
    assert "Desk" in text
    assert "SPCX" in text
    assert "152.64" in text
    assert "watching" in text
    assert "151" in text
    assert "157" in text
    assert "144" in text
    assert "150C" in text
    assert "10/16" in text


def test_armed_card_includes_playbook_thesis():
    from telegram_desk import format_desk

    text = format_desk(
        _book(_name()),
        now=datetime(2026, 9, 20, 18, 11, tzinfo=NY),
        theses={"SPCX": "Public SpaceX. Catalyst is Starship Flight 14."},
    )
    assert "Public SpaceX" in text
    assert "Starship Flight 14" in text
    assert "Starship F14" not in text


def test_header_includes_macro_picture():
    from telegram_desk import format_desk

    text = format_desk(
        _book(_name()),
        now=datetime(2026, 9, 20, 18, 11, tzinfo=NY),
        macro="Fed hiked 25 bp. Bias: support-only longs, no chase.",
    )
    assert "Fed hiked 25 bp" in text
    assert "support-only longs, no chase" in text


def test_load_playbook_extras_reads_thesis_and_macro(tmp_path: Path):
    from telegram_desk import load_playbook_extras

    path = tmp_path / "playbook.json"
    path.write_text(
        json.dumps(
            {
                "macro": {"summary": "Fed hiked 25 bp. Bias: support-only longs."},
                "names": {
                    "SPCX": {
                        "symbol": "SPCX",
                        "thesis": "Public SpaceX. Catalyst is Starship Flight 14.",
                        "notes": "Starship F14; 150C Oct16; buy 145-151",
                        "stop_price": 144.0,
                        "buy_zone": [145.0, 151.0],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    stops, zones, theses, macro = load_playbook_extras(path)
    assert theses["SPCX"] == "Public SpaceX. Catalyst is Starship Flight 14."
    assert macro == "Fed hiked 25 bp. Bias: support-only longs."
    assert stops["SPCX"] == 144.0
    assert zones["SPCX"] == [145.0, 151.0]


def test_open_and_armed_states_sort_before_unarmed():
    from telegram_desk import format_desk

    text = format_desk(
        _book(
            _name(symbol="GME", instrument=None, buy_price=None, sell_price=None, current_price=22.57),
            _name(symbol="FLY", state="buy_ready", signal="buy", current_price=20.5, buy_price=20.9, sell_price=22.8),
            _name(
                symbol="NVDA",
                state="open",
                signal="watch",
                position="long",
                current_price=221.0,
                buy_price=219.5,
                sell_price=226.0,
            ),
        )
    )
    nvda = text.index("NVDA")
    fly = text.index("FLY")
    gme = text.index("GME")
    assert nvda < fly < gme


def test_unarmed_names_collapse_when_message_would_exceed_limit():
    from telegram_desk import TELEGRAM_MAX, format_desk

    names = []
    for i in range(10):
        names.append(
            _name(
                symbol=f"T{i:02d}",
                notes="x" * 400,
                current_price=100.0 + i,
            )
        )
    text = format_desk(_book(*names), limit=800)
    assert len(text) <= 800
    assert len(text) <= TELEGRAM_MAX
    assert "T00" in text


def test_html_escapes_symbol():
    from telegram_desk import format_desk

    text = format_desk(_book(_name(symbol="A&B")))
    assert "A&amp;B" in text
    assert "A&B" not in text.replace("&amp;", "")


def test_overlay_live_price_does_not_write_book(tmp_path: Path):
    from telegram_desk import overlay_live, snapshot_names

    path = tmp_path / "trade_book.json"
    book = _book(_name(current_price=152.64))
    save_book(path, book)
    names = snapshot_names(
        book,
        snaps=[{"symbol": "SPCX", "price": 148.2, "session": "regular", "stale": False, "error": None}],
    )
    assert names[0]["current_price"] == 148.2
    loaded = __import__("json").loads(path.read_text())
    assert loaded["names"][0]["current_price"] == 152.64


def test_send_posts_html_to_telegram_api():
    from telegram_desk import send_telegram

    captured = {}

    def fake_post(url, payload, timeout=10):
        captured["url"] = url
        captured["payload"] = payload
        return {"ok": True, "result": {"message_id": 1}}

    send_telegram("hello <b>desk</b>", token="123:abc", chat_id="8211111111", http_post=fake_post)
    assert captured["url"].endswith("/bot123:abc/sendMessage")
    assert captured["payload"]["chat_id"] == "8211111111"
    assert captured["payload"]["parse_mode"] == "HTML"
    assert captured["payload"]["text"] == "hello <b>desk</b>"


def test_dry_run_cli_prints_and_does_not_post(tmp_path: Path, capsys):
    from telegram_desk import main

    path = tmp_path / "trade_book.json"
    save_book(path, _book(_name()))
    code = main(
        [
            "--once",
            "--dry-run",
            "--book",
            str(path),
            "--no-live",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "SPCX" in out
    assert "Desk" in out


def test_missing_token_exits_nonzero_when_sending(tmp_path: Path, monkeypatch):
    from telegram_desk import main

    monkeypatch.setattr("telegram_desk._load_dotenv", lambda: None)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_HOME_CHANNEL", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    path = tmp_path / "trade_book.json"
    save_book(path, _book(_name()))
    code = main(["--once", "--book", str(path), "--no-live"])
    assert code != 0


def test_unions_watchlist_symbols_missing_from_book(tmp_path: Path):
    from telegram_desk import collect_names
    from price_monitor import classify_symbol

    book = _book(_name())
    wl = tmp_path / "watchlist.txt"
    wl.write_text("SPCX\nNVDA\n", encoding="utf-8")
    names = collect_names(book, wl)
    symbols = [classify_symbol(n["symbol"]).symbol for n in names]
    assert "SPCX" in symbols
    assert "NVDA" in symbols
