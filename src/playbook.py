"""Generic watchlist playbook: thesis, levels, one core instrument, sync.

Canonical path: desk/playbook.json
Poller does not read this file. Sync writes operator fields onto the trade book.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from price_monitor import classify_symbol, load_watchlist
from trade_book import apply_state, empty_book, load_book, new_name, save_book

DEFAULT_PLAYBOOK = Path("desk/playbook.json")
DEFAULT_BOOK = Path("desk/trade_book.json")
DEFAULT_WATCHLIST = Path("data/watchlist.txt")
DEFAULT_MAX_ENABLED = 10
OPTION_KEYS = ("kind", "right", "strike", "expiration")
LEVERAGED_KEYS = ("kind", "leverage", "side")


class PlaybookError(ValueError):
    """Invalid playbook or sync refused."""


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _canonical(raw: str) -> str:
    return classify_symbol(raw).symbol


def load_playbook(path: str | Path) -> dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise PlaybookError("playbook must be a JSON object")
    names = raw.get("names")
    if names is None:
        raw["names"] = {}
    elif not isinstance(names, dict):
        raise PlaybookError("playbook names must be an object")
    return raw


def legal_instrument(
    core: dict[str, Any],
    asset_class: str,
    *,
    as_of: date | None = None,
    symbol: str = "",
) -> dict[str, Any]:
    """Return book-legal keys of core. Reject expired options and asset-class mismatch."""
    if not isinstance(core, dict):
        raise PlaybookError(f"{symbol}: core must be an object")
    kind = core.get("kind")
    label = symbol or "core"
    if kind == "option":
        if asset_class and asset_class != "equity":
            raise PlaybookError(f"{label}: option core only on equity")
        right = str(core.get("right") or "").lower()
        if right not in ("call", "put"):
            raise PlaybookError(f"{label}: invalid option right")
        strike = _as_float(core.get("strike"))
        if strike is None:
            raise PlaybookError(f"{label}: strike required")
        raw_exp = str(core.get("expiration") or "")[:10]
        try:
            exp = date.fromisoformat(raw_exp)
        except ValueError as exc:
            raise PlaybookError(f"{label}: invalid expiration") from exc
        today = as_of or date.today()
        if exp < today:
            raise PlaybookError(f"{label}: expired core {raw_exp}")
        return {
            "kind": "option",
            "right": right,
            "strike": strike,
            "expiration": raw_exp,
        }
    if kind == "leveraged":
        if asset_class and asset_class != "crypto":
            raise PlaybookError(f"{label}: leveraged core only on crypto")
        leverage = _as_float(core.get("leverage"))
        if leverage is None or leverage <= 0:
            raise PlaybookError(f"{label}: invalid leverage")
        side = str(core.get("side") or "").lower()
        if side not in ("long", "short"):
            raise PlaybookError(f"{label}: invalid leveraged side")
        return {"kind": "leveraged", "leverage": leverage, "side": side}
    raise PlaybookError(f"{label}: core kind must be option or leveraged")


def _prepare_entry(key: str, entry: Any, *, as_of: date) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise PlaybookError(f"{key}: name must be an object")
    classified = classify_symbol(str(entry.get("symbol") or key))
    symbol = classified.symbol
    expected_class = classified.asset_class
    key_symbol = classify_symbol(str(key)).symbol
    if symbol != key_symbol:
        raise PlaybookError(f"{key}: symbol {symbol} does not match key")

    asset = entry.get("asset_class")
    if asset is not None and asset != expected_class:
        raise PlaybookError(
            f"{symbol}: asset_class mismatch (playbook={asset}, expected={expected_class})"
        )
    asset = expected_class

    core = entry.get("core")
    armed = isinstance(core, dict) and bool(core)
    notes = str(entry.get("notes") or "")
    if not armed:
        return {
            "symbol": symbol,
            "asset_class": asset,
            "enabled": bool(entry.get("enabled", True)),
            "buy_price": None,
            "sell_price": None,
            "instrument": None,
            "notes": notes,
            "armed": False,
            "thesis": str(entry.get("thesis") or ""),
            "buy_zone": None,
            "stop_price": None,
            "core": None,
        }

    if "enabled" not in entry:
        raise PlaybookError(f"{symbol}: enabled is required")
    if not str(entry.get("thesis") or "").strip():
        raise PlaybookError(f"{symbol}: thesis is required")

    zone = entry.get("buy_zone")
    if not isinstance(zone, (list, tuple)) or len(zone) != 2:
        raise PlaybookError(f"{symbol}: buy_zone must be [low, high]")
    low = _as_float(zone[0])
    high = _as_float(zone[1])
    if low is None or high is None:
        raise PlaybookError(f"{symbol}: buy_zone must be numeric")
    if low > high:
        raise PlaybookError(f"{symbol}: inverted buy_zone")

    buy_price = _as_float(entry.get("buy_price"))
    if buy_price is None:
        raise PlaybookError(f"{symbol}: buy_price is required")
    if buy_price != high:
        raise PlaybookError(f"{symbol}: buy_price must equal buy_zone[1]")

    sell_price = _as_float(entry.get("sell_price"))
    if sell_price is None:
        raise PlaybookError(f"{symbol}: sell_price is required")

    stop_price = _as_float(entry.get("stop_price"))
    if stop_price is None:
        raise PlaybookError(f"{symbol}: stop_price is required")

    if not isinstance(core, dict):
        raise PlaybookError(f"{symbol}: core must be an object")
    inst = legal_instrument(core, asset, as_of=as_of, symbol=symbol)
    return {
        "symbol": symbol,
        "asset_class": asset,
        "enabled": bool(entry["enabled"]),
        "buy_price": high,
        "sell_price": sell_price,
        "instrument": inst,
        "notes": notes,
        "armed": True,
        "thesis": str(entry["thesis"]),
        "buy_zone": [low, high],
        "stop_price": stop_price,
        "core": core,
    }


def _apply_operator(row: dict[str, Any], prepared: dict[str, Any]) -> None:
    row["enabled"] = prepared["enabled"]
    row["buy_price"] = prepared["buy_price"]
    row["sell_price"] = prepared["sell_price"]
    row["instrument"] = prepared["instrument"]
    row["notes"] = prepared["notes"]


def _index_names(book: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name in book.get("names") or []:
        if not isinstance(name, dict):
            continue
        symbol = str(name.get("symbol") or "").strip()
        if not symbol:
            continue
        out[_canonical(symbol)] = name
    return out


def sync(
    playbook_path: str | Path,
    book_path: str | Path,
    watchlist_path: str | Path,
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Upsert playbook names into the book, union watchlist, cap enabled names.

    Does not delete book rows that lack a playbook key. Does not invent quotes
    or positions. Poller never reads the playbook.
    """
    today = as_of or date.today()
    playbook = load_playbook(playbook_path)
    dest = Path(book_path)
    book = load_book(dest) if dest.exists() else empty_book()
    watch_items = load_watchlist(watchlist_path)
    max_enabled = _as_float((playbook.get("defaults") or {}).get("max_enabled"))
    cap = int(max_enabled) if max_enabled else DEFAULT_MAX_ENABLED

    prepared_rows = [
        _prepare_entry(str(key), entry, as_of=today)
        for key, entry in (playbook.get("names") or {}).items()
    ]

    names = list(book.get("names") or [])
    book["names"] = names
    by_symbol = _index_names(book)

    for prepared in prepared_rows:
        symbol = prepared["symbol"]
        row = by_symbol.get(symbol)
        if row is None:
            row = new_name(symbol)
            names.append(row)
            by_symbol[symbol] = row
        _apply_operator(row, prepared)
        apply_state(row, as_of=today)

    for item in watch_items:
        symbol = item.symbol
        if symbol in by_symbol:
            continue
        row = new_name(symbol)
        names.append(row)
        by_symbol[symbol] = row
        apply_state(row, as_of=today)

    enabled_count = sum(1 for name in names if name.get("enabled", True))
    if enabled_count > cap:
        raise PlaybookError(f"enabled names exceed cap of {cap}")

    save_book(dest, book)
    return book


def format_show(symbol: str, playbook_path: str | Path, book_path: str | Path) -> str:
    playbook = load_playbook(playbook_path)
    canon = _canonical(symbol)
    entry = None
    key_used = symbol
    for key, val in (playbook.get("names") or {}).items():
        if not isinstance(val, dict):
            continue
        if _canonical(str(key)) == canon or _canonical(str(val.get("symbol") or key)) == canon:
            entry = val
            key_used = str(key)
            break
    if entry is None:
        raise PlaybookError(f"{symbol}: not in playbook")

    prepared = _prepare_entry(key_used, entry, as_of=date.today())
    book_row = None
    dest = Path(book_path)
    if dest.exists():
        book = load_book(dest)
        book_row = _index_names(book).get(canon)

    state = (book_row or {}).get("state") or "watching"
    lines = [
        f"{prepared['symbol']}  state={state}  enabled={prepared['enabled']}",
        f"Thesis: {prepared.get('thesis') or entry.get('thesis') or ''}",
    ]
    zone = prepared.get("buy_zone") or entry.get("buy_zone")
    if zone:
        lines.append(
            f"Buy zone (underlying): {zone[0]} – {zone[1]}  (arm at last ≤ {zone[1]})"
        )
    else:
        lines.append("Buy zone (underlying): (unarmed)")
    sell = prepared.get("sell_price")
    if sell is not None:
        lines.append(f"Sell the core when underlying last ≥ {sell}")
    else:
        lines.append("Underlying sell price: (unarmed)")
    stop = prepared.get("stop_price")
    if stop is None:
        stop = _as_float(entry.get("stop_price"))
    if stop is not None:
        lines.append(f"Stop: {stop}")
    else:
        lines.append("Stop: (none)")
    inst = prepared.get("instrument")
    if inst:
        if inst.get("kind") == "option":
            lines.append(
                f"Core: option {inst['right']} strike={inst['strike']} "
                f"expiration={inst['expiration']}"
            )
        else:
            lines.append(f"Core: leveraged {inst.get('leverage')}x {inst.get('side')}")
    else:
        lines.append("Core: (none — unarmed)")
    if book_row is not None:
        lines.append(
            f"Book: buy_price={book_row.get('buy_price')} "
            f"sell_price={book_row.get('sell_price')} "
            f"instrument={book_row.get('instrument')} "
            f"state={book_row.get('state')}"
        )
    else:
        lines.append("Book: (not in trade book — run sync)")
    notes = prepared.get("notes") or ""
    if notes:
        lines.append(f"Notes: {notes}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sync playbook operator fields onto the trade book (no orders)."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("sync", help="upsert playbook names into the book")
    p_sync.add_argument("--playbook", default=str(DEFAULT_PLAYBOOK))
    p_sync.add_argument("--book", default=str(DEFAULT_BOOK))
    p_sync.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST))

    p_show = sub.add_parser("show", help="print thesis, zone, sell, stop, core, book state")
    p_show.add_argument("symbol")
    p_show.add_argument("--playbook", default=str(DEFAULT_PLAYBOOK))
    p_show.add_argument("--book", default=str(DEFAULT_BOOK))

    args = parser.parse_args(argv)
    try:
        if args.cmd == "sync":
            book = sync(args.playbook, args.book, args.watchlist)
            n = len(book.get("names") or [])
            print(f"synced {n} names")
            return 0
        print(format_show(args.symbol, args.playbook, args.book))
        return 0
    except (PlaybookError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"playbook error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
