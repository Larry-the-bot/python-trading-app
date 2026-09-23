"""Basic tests for the trade executor and agent integration."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from trade_book import load_book, new_name, save_book, apply_state
from trade_executor import execute_one, should_act_on_buy, should_act_on_sell


def _live_quote(**extra):
    q = {
        "session": "regular",
        "stale": False,
        "error": None,
        "source": "robinhood",
        "fetched_at": "2026-09-21T10:00:00-04:00",
    }
    q.update(extra)
    return q


def _option_instrument(**extra):
    inst = {"kind": "option", "right": "call", "strike": 150.0, "expiration": "2026-10-16"}
    inst.update(extra)
    return inst


def test_should_act_on_buy_happy_path():
    name = new_name("SPCX")
    name.update({
        "current_price": 149.0,
        "buy_price": 151.0,
        "quote": _live_quote(),
        "instrument": _option_instrument(),
        "state": "buy_ready",
        "enabled": True,
    })
    pb = {"buy_zone": [145.0, 151.0], "stop_price": 144.0}
    allowed, reason = should_act_on_buy(name, pb, None)
    assert allowed is True
    assert reason == "ok"


def test_should_act_on_buy_outside_zone():
    name = new_name("SPCX")
    name.update({
        "current_price": 152.0,
        "buy_price": 151.0,
        "quote": _live_quote(),
        "instrument": _option_instrument(),
        "state": "buy_ready",
        "enabled": True,
    })
    pb = {"buy_zone": [145.0, 151.0]}
    allowed, reason = should_act_on_buy(name, pb, None)
    assert allowed is False
    assert "outside buy_zone" in reason or "chase" in reason.lower()


def test_should_act_on_sell_happy():
    name = new_name("SPCX")
    name.update({
        "current_price": 158.0,
        "sell_price": 157.0,
        "quote": _live_quote(),
        "state": "sell_ready",
        "enabled": True,
    })
    allowed, reason = should_act_on_sell(name, {})
    assert allowed is True


def test_executor_buy_then_position_update(tmp_path):
    book_path = tmp_path / "book.json"
    pb_path = tmp_path / "playbook.json"

    # Minimal playbook
    pb_path.write_text(json.dumps({
        "version": 1,
        "defaults": {"contracts": 1},
        "names": {
            "SPCX": {
                "buy_zone": [145, 151],
                "stop_price": 144,
                "contracts": 1,
            }
        }
    }))

    # Book with buy_ready
    book = {"version": 3, "names": []}
    name = new_name("SPCX")
    name.update({
        "current_price": 149.5,
        "buy_price": 151.0,
        "sell_price": 157.0,
        "instrument": _option_instrument(),
        "quote": _live_quote(),
        "enabled": True,
    })
    apply_state(name)
    assert name["state"] == "buy_ready"
    book["names"] = [name]
    save_book(book_path, book)

    summary = execute_one(
        book_path, pb_path,
        dry_run=True,
        use_max_pain=False,
    )
    assert summary["actions_taken"] == 1
    assert summary["actions"][0]["action"] == "buy"
    assert summary["actions"][0]["success"] is True

    updated = load_book(book_path)
    row = updated["names"][0]
    assert row["position"] == "long"
    assert row["state"] == "open"
    assert "last_fill" in row
    assert row["last_fill"]["side"] == "buy"


def test_executor_sell_path(tmp_path):
    book_path = tmp_path / "book.json"
    pb_path = tmp_path / "playbook.json"
    pb_path.write_text(json.dumps({"version": 1, "names": {"SPCX": {}}}))

    book = {"version": 3, "names": []}
    name = new_name("SPCX")
    name.update({
        "current_price": 158.0,
        "buy_price": 151.0,
        "sell_price": 157.0,
        "instrument": _option_instrument(),
        "quote": _live_quote(),
        "position": "long",
        "state": "sell_ready",
        "enabled": True,
    })
    book["names"] = [name]
    save_book(book_path, book)

    summary = execute_one(book_path, pb_path, dry_run=True, use_max_pain=False)
    assert summary["actions_taken"] == 1
    assert summary["actions"][0]["action"] == "sell"

    updated = load_book(book_path)
    assert updated["names"][0]["position"] == "flat"
    assert updated["names"][0]["state"] == "watching"