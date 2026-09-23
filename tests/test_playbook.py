from datetime import date
from pathlib import Path

import pytest

from trade_book import empty_book, load_book, save_book

AS_OF = date(2026, 9, 20)


def _live_equity(**extra):
    quote = {
        "session": "regular",
        "stale": False,
        "error": None,
        "source": "robinhood",
    }
    quote.update(extra)
    return quote


def _option(**extra):
    inst = {
        "kind": "option",
        "right": "call",
        "strike": 50.0,
        "expiration": "2026-10-16",
        "role": "core",
    }
    inst.update(extra)
    return inst


def _leveraged(**extra):
    inst = {"kind": "leveraged", "leverage": 2.0, "side": "long", "role": "core"}
    inst.update(extra)
    return inst


def _armed_entry(symbol: str, **overrides):
    asset = "crypto" if "-USD" in symbol or symbol in {"BTC", "XRP"} else "equity"
    if asset == "crypto":
        core = _leveraged()
        buy_zone = [90.0, 100.0]
        buy_price = 100.0
        sell_price = 120.0
        stop_price = 80.0
    else:
        core = _option()
        buy_zone = [48.0, 50.0]
        buy_price = 50.0
        sell_price = 55.0
        stop_price = 45.0
    row = {
        "symbol": symbol,
        "asset_class": asset,
        "enabled": True,
        "thesis": f"{symbol} thesis",
        "buy_zone": buy_zone,
        "buy_price": buy_price,
        "sell_price": sell_price,
        "stop_price": stop_price,
        "core": core,
        "notes": f"{symbol} note",
    }
    row.update(overrides)
    return row


def _playbook(names: dict, **extra):
    doc = {
        "version": 1,
        "updated_at": "2026-09-20T00:00:00+00:00",
        "macro": {"summary": "test", "calendar": [], "bias": "support-only"},
        "defaults": {
            "orders": "limit mid-or-better",
            "no_chase": True,
            "stop_owner": "executor",
            "max_enabled": 10,
        },
        "names": names,
    }
    doc.update(extra)
    return doc


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(__import__("json").dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _write_watchlist(path: Path, symbols: list[str]) -> Path:
    path.write_text("\n".join(symbols) + "\n", encoding="utf-8")
    return path


def _sync(tmp_path: Path, playbook: dict, book: dict, watchlist: list[str], **kwargs):
    from playbook import sync
    play_path = _write_json(tmp_path / "playbook.json", playbook)
    book_path = tmp_path / "trade_book.json"
    save_book(book_path, book)
    wl_path = _write_watchlist(tmp_path / "watchlist.txt", watchlist)
    result = sync(play_path, book_path, wl_path, as_of=kwargs.get("as_of", AS_OF))
    return result, load_book(book_path), book_path


def test_sync_copies_buy_zone_high_to_book_buy_price_and_sell_price_to_book(tmp_path: Path):
    book = empty_book()
    result, loaded, _ = _sync(
        tmp_path,
        _playbook({"AAA": _armed_entry("AAA", buy_zone=[10.0, 12.5], buy_price=12.5, sell_price=18.0)}),
        book,
        ["AAA"],
    )
    name = loaded["names"][0]
    assert name["symbol"] == "AAA"
    assert name["buy_price"] == 12.5
    assert name["sell_price"] == 18.0
    assert result["names"][0]["buy_price"] == 12.5


def test_sync_writes_legal_instrument_keys_only_and_strips_role(tmp_path: Path):
    _, loaded, _ = _sync(
        tmp_path,
        _playbook({"AAA": _armed_entry("AAA")}),
        empty_book(),
        ["AAA"],
    )
    inst = loaded["names"][0]["instrument"]
    assert inst == {
        "kind": "option",
        "right": "call",
        "strike": 50.0,
        "expiration": "2026-10-16",
    }
    assert "role" not in inst


def test_live_valid_option_and_last_at_or_below_buy_price_is_buy_ready(tmp_path: Path):
    book = empty_book()
    book["names"].append(
        {
            "symbol": "AAA",
            "asset_class": "equity",
            "enabled": True,
            "current_price": 49.0,
            "buy_price": None,
            "sell_price": None,
            "position": "flat",
            "state": "watching",
            "instrument": None,
            "signal": "watch",
            "quote": _live_equity(),
            "notes": "",
        }
    )
    _, loaded, _ = _sync(
        tmp_path,
        _playbook({"AAA": _armed_entry("AAA", buy_zone=[48.0, 50.0], buy_price=50.0)}),
        book,
        ["AAA"],
    )
    name = loaded["names"][0]
    assert name["state"] == "buy_ready"
    assert name["signal"] == "buy"
    assert name["position"] == "flat"


def test_last_above_buy_price_stays_watching(tmp_path: Path):
    book = empty_book()
    book["names"].append(
        {
            "symbol": "AAA",
            "asset_class": "equity",
            "enabled": True,
            "current_price": 51.0,
            "buy_price": None,
            "sell_price": None,
            "position": "flat",
            "state": "watching",
            "instrument": None,
            "signal": "watch",
            "quote": _live_equity(),
            "notes": "",
        }
    )
    _, loaded, _ = _sync(
        tmp_path,
        _playbook({"AAA": _armed_entry("AAA", buy_zone=[48.0, 50.0], buy_price=50.0)}),
        book,
        ["AAA"],
    )
    assert loaded["names"][0]["state"] == "watching"
    assert loaded["names"][0]["signal"] == "watch"


def test_live_long_at_or_above_sell_price_is_sell_ready(tmp_path: Path):
    book = empty_book()
    book["names"].append(
        {
            "symbol": "AAA",
            "asset_class": "equity",
            "enabled": True,
            "current_price": 55.0,
            "buy_price": None,
            "sell_price": None,
            "position": "long",
            "state": "open",
            "instrument": None,
            "signal": "watch",
            "quote": _live_equity(),
            "notes": "",
        }
    )
    _, loaded, _ = _sync(
        tmp_path,
        _playbook({"AAA": _armed_entry("AAA", sell_price=55.0)}),
        book,
        ["AAA"],
    )
    name = loaded["names"][0]
    assert name["state"] == "sell_ready"
    assert name["signal"] == "sell"
    assert name["position"] == "long"


def test_expired_core_is_rejected(tmp_path: Path):
    from playbook import PlaybookError

    with pytest.raises(PlaybookError, match="expired"):
        _sync(
            tmp_path,
            _playbook(
                {
                    "AAA": _armed_entry(
                        "AAA",
                        core=_option(expiration="2026-09-01"),
                    )
                }
            ),
            empty_book(),
            ["AAA"],
        )


def test_leveraged_core_rejected_on_equity(tmp_path: Path):
    from playbook import PlaybookError

    with pytest.raises(PlaybookError, match="leveraged"):
        _sync(
            tmp_path,
            _playbook(
                {
                    "AAA": _armed_entry(
                        "AAA",
                        asset_class="equity",
                        core=_leveraged(),
                    )
                }
            ),
            empty_book(),
            ["AAA"],
        )


def test_leveraged_core_allowed_on_crypto(tmp_path: Path):
    _, loaded, _ = _sync(
        tmp_path,
        _playbook({"BTC-USD": _armed_entry("BTC-USD")}),
        empty_book(),
        ["BTC"],
    )
    name = loaded["names"][0]
    assert name["symbol"] == "BTC-USD"
    assert name["asset_class"] == "crypto"
    assert name["instrument"] == {"kind": "leveraged", "leverage": 2.0, "side": "long"}


def test_inverted_buy_zone_rejected(tmp_path: Path):
    from playbook import PlaybookError

    with pytest.raises(PlaybookError, match="buy_zone"):
        _sync(
            tmp_path,
            _playbook({"AAA": _armed_entry("AAA", buy_zone=[50.0, 40.0], buy_price=40.0)}),
            empty_book(),
            ["AAA"],
        )


def test_armed_name_missing_sell_or_stop_rejected(tmp_path: Path):
    from playbook import PlaybookError

    with pytest.raises(PlaybookError, match="sell_price"):
        _sync(
            tmp_path,
            _playbook({"AAA": _armed_entry("AAA", sell_price=None)}),
            empty_book(),
            ["AAA"],
        )
    with pytest.raises(PlaybookError, match="stop_price"):
        _sync(
            tmp_path,
            _playbook({"AAA": _armed_entry("AAA", stop_price=None)}),
            empty_book(),
            ["BBB"],
        )


def test_asset_class_mismatch_rejected(tmp_path: Path):
    from playbook import PlaybookError

    with pytest.raises(PlaybookError, match="asset_class"):
        _sync(
            tmp_path,
            _playbook({"AAA": _armed_entry("AAA", asset_class="crypto")}),
            empty_book(),
            ["AAA"],
        )


def test_unarmed_name_without_core_keeps_null_levels(tmp_path: Path):
    entry = {
        "symbol": "AAA",
        "asset_class": "equity",
        "enabled": True,
        "thesis": "watched only",
    }
    _, loaded, _ = _sync(tmp_path, _playbook({"AAA": entry}), empty_book(), ["AAA"])
    name = loaded["names"][0]
    assert name["instrument"] is None
    assert name["buy_price"] is None
    assert name["sell_price"] is None
    assert name["state"] == "watching"


def test_sync_does_not_invent_quotes_or_positions(tmp_path: Path):
    book = empty_book()
    book["names"].append(
        {
            "symbol": "AAA",
            "asset_class": "equity",
            "enabled": True,
            "current_price": 49.5,
            "buy_price": None,
            "sell_price": None,
            "position": "flat",
            "state": "watching",
            "instrument": None,
            "signal": "watch",
            "quote": {"bid": 49.4, "ask": 49.6, "session": "closed", "stale": True, "error": None},
            "notes": "old",
        }
    )
    _, loaded, _ = _sync(
        tmp_path,
        _playbook({"AAA": _armed_entry("AAA")}),
        book,
        ["AAA"],
    )
    name = loaded["names"][0]
    assert name["current_price"] == 49.5
    assert name["quote"]["bid"] == 49.4
    assert name["quote"]["session"] == "closed"
    assert name["position"] == "flat"
    assert "stop_price" not in name
    assert "allocation" not in name


def test_sync_is_idempotent_and_does_not_drop_non_playbook_rows(tmp_path: Path):
    book = empty_book()
    book["names"].append(
        {
            "symbol": "ORPHAN",
            "asset_class": "equity",
            "enabled": True,
            "current_price": 1.0,
            "buy_price": None,
            "sell_price": None,
            "position": "flat",
            "state": "watching",
            "instrument": None,
            "signal": "watch",
            "quote": {},
            "notes": "keep",
        }
    )
    play = _playbook({"AAA": _armed_entry("AAA")})
    _, first, book_path = _sync(tmp_path, play, book, ["AAA"])
    from playbook import sync

    sync(tmp_path / "playbook.json", book_path, tmp_path / "watchlist.txt", as_of=AS_OF)
    second = load_book(book_path)
    symbols = [n["symbol"] for n in second["names"]]
    assert "ORPHAN" in symbols
    assert "AAA" in symbols
    orphan = next(n for n in second["names"] if n["symbol"] == "ORPHAN")
    assert orphan["notes"] == "keep"
    assert orphan["current_price"] == 1.0
    aaa = next(n for n in second["names"] if n["symbol"] == "AAA")
    first_aaa = next(n for n in first["names"] if n["symbol"] == "AAA")
    assert aaa["buy_price"] == first_aaa["buy_price"]
    assert aaa["sell_price"] == first_aaa["sell_price"]
    assert aaa["instrument"] == first_aaa["instrument"]
    assert aaa["notes"] == first_aaa["notes"]
    assert aaa["enabled"] == first_aaa["enabled"]


def test_sync_unions_watchlist_symbols_not_in_playbook(tmp_path: Path):
    _, loaded, _ = _sync(
        tmp_path,
        _playbook({"AAA": _armed_entry("AAA")}),
        empty_book(),
        ["AAA", "BBB"],
    )
    symbols = [n["symbol"] for n in loaded["names"]]
    assert "AAA" in symbols
    assert "BBB" in symbols
    bbb = next(n for n in loaded["names"] if n["symbol"] == "BBB")
    assert bbb["instrument"] is None
    assert bbb["buy_price"] is None
    assert bbb["state"] == "watching"


def test_enabled_names_capped_at_ten(tmp_path: Path):
    from playbook import PlaybookError

    names = {}
    for i in range(11):
        sym = f"T{i:02d}"
        names[sym] = _armed_entry(sym)
    with pytest.raises(PlaybookError, match="10"):
        _sync(tmp_path, _playbook(names), empty_book(), ["T00"])


def test_armed_set_requires_enabled_armed_state_matching_signal_live_quote_valid_instrument():
    from state_monitor import armed_set
    from trade_book import apply_state

    live = {
        "symbol": "AAA",
        "asset_class": "equity",
        "enabled": True,
        "current_price": 49.0,
        "buy_price": 50.0,
        "sell_price": 55.0,
        "position": "flat",
        "state": "buy_ready",
        "signal": "buy",
        "instrument": {
            "kind": "option",
            "right": "call",
            "strike": 50.0,
            "expiration": "2026-10-16",
        },
        "quote": _live_equity(),
    }
    book = empty_book()
    book["names"].append(dict(live))
    assert armed_set(book, as_of=AS_OF)[0]["symbol"] == "AAA"

    disabled = dict(live, enabled=False)
    apply_state(disabled, as_of=AS_OF)
    book["names"] = [disabled]
    assert armed_set(book, as_of=AS_OF) == []

    watching = dict(live, state="watching", signal="watch")
    book["names"] = [watching]
    assert armed_set(book, as_of=AS_OF) == []

    mismatched = dict(live, signal="watch")
    book["names"] = [mismatched]
    assert armed_set(book, as_of=AS_OF) == []

    stale = dict(live, quote={"session": "closed", "stale": True, "error": None})
    book["names"] = [stale]
    assert armed_set(book, as_of=AS_OF) == []

    no_inst = dict(live, instrument=None)
    book["names"] = [no_inst]
    assert armed_set(book, as_of=AS_OF) == []


def test_show_prints_thesis_zone_underlying_sell_stop_core_and_book_state(tmp_path: Path, capsys):
    from playbook import main

    book = empty_book()
    _sync(
        tmp_path,
        _playbook(
            {
                "AAA": _armed_entry(
                    "AAA",
                    thesis="support bounce",
                    buy_zone=[48.0, 50.0],
                    buy_price=50.0,
                    sell_price=55.0,
                    stop_price=45.0,
                    allocation=1,
                    notes="one-liner",
                )
            }
        ),
        book,
        ["AAA"],
    )
    code = main(
        [
            "show",
            "AAA",
            "--playbook",
            str(tmp_path / "playbook.json"),
            "--book",
            str(tmp_path / "trade_book.json"),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "support bounce" in out
    assert "48.0" in out and "50.0" in out
    assert "55.0" in out
    assert "45.0" in out
    assert "Allocation: $1" in out
    assert "2026-10-16" in out
    assert "watching" in out or "buy_ready" in out


def test_sync_cli_writes_operator_fields(tmp_path: Path):
    from playbook import main

    play = _playbook({"AAA": _armed_entry("AAA", buy_zone=[1.0, 2.0], buy_price=2.0, sell_price=3.0)})
    _write_json(tmp_path / "playbook.json", play)
    save_book(tmp_path / "trade_book.json", empty_book())
    _write_watchlist(tmp_path / "watchlist.txt", ["AAA"])
    code = main(
        [
            "sync",
            "--playbook",
            str(tmp_path / "playbook.json"),
            "--book",
            str(tmp_path / "trade_book.json"),
            "--watchlist",
            str(tmp_path / "watchlist.txt"),
        ]
    )
    assert code == 0
    name = load_book(tmp_path / "trade_book.json")["names"][0]
    assert name["buy_price"] == 2.0
    assert name["sell_price"] == 3.0


def test_buy_price_must_equal_buy_zone_high(tmp_path: Path):
    from playbook import PlaybookError

    with pytest.raises(PlaybookError, match="buy_price"):
        _sync(
            tmp_path,
            _playbook({"AAA": _armed_entry("AAA", buy_zone=[48.0, 50.0], buy_price=49.0)}),
            empty_book(),
            ["AAA"],
        )


def test_seed_playbook_has_one_core_call_per_armed_name():
    from playbook import load_playbook, legal_instrument

    path = Path("desk/playbook.json")
    if not path.exists():
        pytest.skip("seed playbook not written yet")
    doc = load_playbook(path)
    expected = {
        "SPCX": {
            "strike": 150.0,
            "expiration": "2026-10-16",
            "buy_zone": [145.0, 151.0],
            "sell_price": 157.0,
            "stop_price": 144.0,
        },
        "NVDA": {
            "strike": 220.0,
            "expiration": "2026-10-16",
            "buy_zone": [216.5, 219.5],
            "sell_price": 226.0,
            "stop_price": 209.5,
        },
        "FLY": {
            "strike": 20.0,
            "expiration": "2026-10-16",
            "buy_zone": [20.2, 20.9],
            "sell_price": 22.8,
            "stop_price": 19.4,
        },
    }
    for symbol, want in expected.items():
        entry = doc["names"][symbol]
        assert entry["enabled"] is True
        assert entry["buy_zone"] == want["buy_zone"]
        assert entry["buy_price"] == want["buy_zone"][1]
        assert entry["sell_price"] == want["sell_price"]
        assert entry["stop_price"] == want["stop_price"]
        inst = legal_instrument(entry["core"], entry["asset_class"])
        assert inst["kind"] == "option"
        assert inst["right"] == "call"
        assert inst["strike"] == want["strike"]
        assert inst["expiration"] == want["expiration"]
        assert entry["core"].get("kind") == "option"
        assert "spread" not in str(entry["core"].get("kind", "")).lower()