"""Shared trade book: underlying levels, instrument, and trade state machine.

Canonical path: desk/trade_book.json

Poller writes quote fields and advances state. It never fills.
Operator owns levels and instrument. Executor marks fills via position.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_BOOK = Path("desk/trade_book.json")
BOOK_VERSION = 3

STATES = ("disabled", "watching", "buy_ready", "open", "sell_ready")
SIGNAL_BY_STATE = {
    "disabled": "off",
    "watching": "watch",
    "buy_ready": "buy",
    "open": "watch",
    "sell_ready": "sell",
}
LIVE_SESSIONS = frozenset({"pre", "regular", "post", "crypto"})
QUOTE_FIELDS = ("bid", "ask", "session", "stale", "source", "fetched_at", "error")


def empty_book() -> dict[str, Any]:
    return {
        "version": BOOK_VERSION,
        "currency": "USD",
        "updated_at": None,
        "names": [],
    }


def load_book(path: str | Path) -> dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("trade book must be a JSON object")
    names = raw.get("names")
    if names is None:
        raw["names"] = []
    elif not isinstance(names, list):
        raise ValueError("trade book names must be a list")
    raw.setdefault("version", BOOK_VERSION)
    raw.setdefault("currency", "USD")
    return raw


def save_book(path: str | Path, book: dict[str, Any]) -> None:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(book)
    payload["version"] = payload.get("version") or BOOK_VERSION
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    tmp.replace(dest)


def _canonical_symbol(raw: str) -> str:
    from price_monitor import classify_symbol

    return classify_symbol(raw).symbol


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def quote_is_live(name: dict[str, Any]) -> bool:
    quote = name.get("quote") or {}
    if quote.get("error"):
        return False
    if quote.get("stale"):
        return False
    session = quote.get("session")
    return session in LIVE_SESSIONS


def instrument_is_valid(name: dict[str, Any], *, as_of: date | None = None) -> bool:
    inst = name.get("instrument")
    if not isinstance(inst, dict):
        return False
    kind = inst.get("kind")
    asset = name.get("asset_class") or ""
    if kind == "option":
        if asset and asset != "equity":
            return False
        right = str(inst.get("right") or "").lower()
        if right not in ("call", "put"):
            return False
        if _as_float(inst.get("strike")) is None:
            return False
        raw_exp = str(inst.get("expiration") or "")[:10]
        try:
            exp = date.fromisoformat(raw_exp)
        except ValueError:
            return False
        today = as_of or date.today()
        return exp >= today
    if kind == "leveraged":
        if asset and asset != "crypto":
            return False
        leverage = _as_float(inst.get("leverage"))
        if leverage is None or leverage <= 0:
            return False
        side = str(inst.get("side") or "").lower()
        return side in ("long", "short")
    return False


def next_state(name: dict[str, Any], *, as_of: date | None = None) -> tuple[str, str | None]:
    """Return (state, reason). Poller never returns a fill; executor sets position."""
    if not name.get("enabled", True):
        return "disabled", "disabled"

    state = str(name.get("state") or "watching")
    if state not in STATES:
        state = "watching"
    position = str(name.get("position") or "flat").lower()

    if position == "long" and state in ("watching", "buy_ready", "disabled"):
        state = "open"
    elif position == "flat" and state in ("open", "sell_ready"):
        state = "watching"

    price = _as_float(name.get("current_price"))
    buy = _as_float(name.get("buy_price"))
    sell = _as_float(name.get("sell_price"))
    live = quote_is_live(name)
    valid = instrument_is_valid(name, as_of=as_of)

    if state == "open":
        if live and price is not None and sell is not None and price >= sell:
            return "sell_ready", "underlying at or above sell_price"
        return "open", None

    if state == "sell_ready":
        if not live:
            return "open", "quote not live"
        if price is None or sell is None or price < sell:
            return "open", "sell level no longer valid"
        return "sell_ready", "underlying at or above sell_price"

    if state == "buy_ready":
        if not live:
            return "watching", "quote not live"
        if not valid:
            return "watching", "instrument invalid"
        if price is None or buy is None or price > buy:
            return "watching", "buy level no longer valid"
        return "buy_ready", "underlying at or below buy_price"

    if live and valid and price is not None and buy is not None and price <= buy:
        return "buy_ready", "underlying at or below buy_price"
    return "watching", None


def apply_state(name: dict[str, Any], *, as_of: date | None = None, now: datetime | None = None) -> dict[str, Any]:
    state, reason = next_state(name, as_of=as_of)
    prev = name.get("state")
    if prev != state:
        stamp = now or datetime.now(timezone.utc)
        name["state_changed_at"] = stamp.isoformat()
        name["state_reason"] = reason
    name["state"] = state
    name["signal"] = SIGNAL_BY_STATE[state]
    name["position"] = "long" if state in ("open", "sell_ready") else "flat"
    return name


def signal_for(name: dict[str, Any]) -> str:
    state, _reason = next_state(name)
    return SIGNAL_BY_STATE[state]


def enabled_symbols(book: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for name in book.get("names") or []:
        if not name.get("enabled", True):
            continue
        symbol = str(name.get("symbol") or "").strip()
        if symbol:
            out.append(symbol)
    return out


def apply_quotes(path: str | Path, snaps: list[dict[str, Any]]) -> dict[str, Any]:
    book = load_book(path)
    by_symbol = {}
    for snap in snaps:
        symbol = snap.get("symbol")
        if not symbol:
            continue
        by_symbol[_canonical_symbol(str(symbol))] = snap
    for name in book.get("names") or []:
        key = _canonical_symbol(str(name.get("symbol") or ""))
        snap = by_symbol.get(key)
        if snap is None:
            continue
        error = snap.get("error")
        price = snap.get("price")
        if error and price is None:
            quote = dict(name.get("quote") or {})
            quote["error"] = error
            if snap.get("fetched_at") is not None:
                quote["fetched_at"] = snap.get("fetched_at")
            if snap.get("stale") is not None:
                quote["stale"] = snap.get("stale")
            name["quote"] = quote
            continue
        name["current_price"] = price
        quote = dict(name.get("quote") or {})
        for field in QUOTE_FIELDS:
            if field in snap:
                quote[field] = snap.get(field)
        name["quote"] = quote
    now = datetime.now(timezone.utc)
    for name in book.get("names") or []:
        apply_state(name, now=now)
    save_book(path, book)
    return book


def apply_quotes_if_present(path: str | Path | None, snaps: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not path:
        return None
    dest = Path(path)
    if not dest.exists():
        return None
    return apply_quotes(dest, snaps)


def new_name(symbol: str, **overrides: Any) -> dict[str, Any]:
    from price_monitor import classify_symbol

    item = classify_symbol(symbol)
    row: dict[str, Any] = {
        "symbol": item.symbol,
        "asset_class": item.asset_class,
        "enabled": True,
        "current_price": None,
        "buy_price": None,
        "sell_price": None,
        "position": "flat",
        "state": "watching",
        "state_reason": None,
        "state_changed_at": None,
        "instrument": None,
        "signal": "watch",
        "quote": {},
        "notes": "",
    }
    row.update(overrides)
    apply_state(row)
    return row
