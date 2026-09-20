from pathlib import Path

from trade_book import empty_book, save_book


def _live_equity(**extra):
    quote = {
        "session": "regular",
        "stale": False,
        "error": None,
        "source": "robinhood",
        "fetched_at": "2026-09-20T10:00:00-04:00",
    }
    quote.update(extra)
    return quote


def _option(**extra):
    inst = {
        "kind": "option",
        "right": "call",
        "strike": 25.0,
        "expiration": "2026-09-25",
    }
    inst.update(extra)
    return inst


def _name(**overrides):
    row = {
        "symbol": "GME",
        "asset_class": "equity",
        "enabled": True,
        "current_price": 19.0,
        "buy_price": 20.0,
        "sell_price": 25.0,
        "position": "flat",
        "state": "watching",
        "signal": "watch",
        "instrument": _option(),
        "quote": _live_equity(),
        "notes": "",
    }
    row.update(overrides)
    return row


def test_armed_set_includes_latched_buy_ready_with_live_quote_and_instrument():
    from state_monitor import armed_set

    book = empty_book()
    book["names"].append(
        _name(state="buy_ready", signal="buy", current_price=19.0)
    )
    items = armed_set(book)
    assert [row["symbol"] for row in items] == ["GME"]
    assert items[0]["state"] == "buy_ready"
    assert items[0]["instrument"] == _option()


def test_armed_set_includes_latched_sell_ready():
    from state_monitor import armed_set

    book = empty_book()
    book["names"].append(
        _name(
            state="sell_ready",
            signal="sell",
            position="long",
            current_price=25.0,
        )
    )
    items = armed_set(book)
    assert items[0]["symbol"] == "GME"
    assert items[0]["state"] == "sell_ready"


def test_armed_set_skips_watching_and_open():
    from state_monitor import armed_set

    book = empty_book()
    book["names"].append(_name(state="watching", signal="watch"))
    book["names"].append(
        _name(symbol="SPCX", state="open", signal="watch", position="long")
    )
    assert armed_set(book) == []


def test_does_not_recheck_price_against_buy_or_sell_levels():
    from state_monitor import armed_set

    book = empty_book()
    book["names"].append(
        _name(
            state="buy_ready",
            signal="buy",
            current_price=21.5,
            buy_price=20.0,
        )
    )
    items = armed_set(book)
    assert [row["symbol"] for row in items] == ["GME"]


def test_excludes_stale_or_closed_equity_even_if_state_is_buy_ready():
    from state_monitor import armed_set

    book = empty_book()
    book["names"].append(
        _name(
            state="buy_ready",
            signal="buy",
            quote={
                "session": "closed",
                "stale": True,
                "error": None,
                "source": "robinhood",
            },
        )
    )
    assert armed_set(book) == []


def test_excludes_invalid_instrument():
    from state_monitor import armed_set

    book = empty_book()
    book["names"].append(
        _name(state="buy_ready", signal="buy", instrument=None)
    )
    assert armed_set(book) == []


def test_payload_is_only_symbol_state_instrument_sorted():
    from state_monitor import armed_set, format_armed_set

    book = empty_book()
    book["names"].append(
        _name(
            symbol="SPCX",
            state="sell_ready",
            signal="sell",
            position="long",
            instrument=_option(strike=150.0),
        )
    )
    book["names"].append(_name(state="buy_ready", signal="buy"))
    items = armed_set(book)
    assert [row["symbol"] for row in items] == ["GME", "SPCX"]
    for row in items:
        assert set(row) == {"symbol", "state", "instrument"}
        assert "current_price" not in row
        assert "quote" not in row
    text = format_armed_set(items)
    assert "fetched_at" not in text
    assert "current_price" not in text
    assert text == format_armed_set(items)


def test_once_prints_armed_json_and_exits_zero(tmp_path: Path, capsys):
    from state_monitor import main

    path = tmp_path / "trade_book.json"
    book = empty_book()
    book["names"].append(_name(state="buy_ready", signal="buy"))
    save_book(path, book)

    code = main(["--once", "--book", str(path)])
    assert code == 0
    out = capsys.readouterr().out.strip()
    assert '"symbol":"GME"' in out.replace(" ", "")
    assert '"state":"buy_ready"' in out.replace(" ", "")
    assert "fetched_at" not in out


def test_once_prints_empty_array_when_nothing_armed(tmp_path: Path, capsys):
    from state_monitor import main

    path = tmp_path / "trade_book.json"
    book = empty_book()
    book["names"].append(_name(state="watching", signal="watch"))
    save_book(path, book)

    code = main(["--once", "--book", str(path)])
    assert code == 0
    assert capsys.readouterr().out.strip() == "[]"


def test_excludes_disabled_names():
    from state_monitor import armed_set

    book = empty_book()
    book["names"].append(
        _name(state="buy_ready", signal="buy", enabled=False)
    )
    assert armed_set(book) == []


def test_crypto_live_buy_ready_is_armed():
    from state_monitor import armed_set

    book = empty_book()
    book["names"].append(
        _name(
            symbol="BTC-USD",
            asset_class="crypto",
            state="buy_ready",
            signal="buy",
            quote={
                "session": "crypto",
                "stale": False,
                "error": None,
                "source": "robinhood",
            },
            instrument={"kind": "leveraged", "leverage": 2.0, "side": "long"},
        )
    )
    items = armed_set(book)
    assert items[0]["symbol"] == "BTC-USD"
    assert items[0]["state"] == "buy_ready"


def test_once_exits_one_when_book_missing(tmp_path: Path):
    from state_monitor import main

    code = main(["--once", "--book", str(tmp_path / "missing.json")])
    assert code == 1
