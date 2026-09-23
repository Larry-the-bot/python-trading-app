#!/usr/bin/env python3
"""No-LLM trade-book readout for Telegram.

One watchlist name per line. Stop and contract count come from the playbook
(stop is not stored on the book). Does not write the book.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

APP = Path("/opt/data/workspace/trading-python-app")
BOOK = APP / "desk/trade_book.json"
PLAYBOOK = APP / "desk/playbook.json"
NY = ZoneInfo("America/New_York")


def _fmt_price(value) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(number) >= 1000:
        return f"{number:.2f}"
    if abs(number) >= 1:
        return f"{number:.4f}".rstrip("0").rstrip(".")
    return f"{number:.6f}".rstrip("0").rstrip(".")


def _as_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_core(inst) -> str:
    if not isinstance(inst, dict) or not inst:
        return "—"
    kind = inst.get("kind")
    if kind == "option":
        right = inst.get("right") or "?"
        strike = _fmt_price(inst.get("strike"))
        exp = inst.get("expiration") or "?"
        return f"{right} {strike} {exp}"
    if kind == "leveraged":
        try:
            label = f"{float(inst.get('leverage')):g}x"
        except (TypeError, ValueError):
            label = "lev=?"
        side = inst.get("side") or "long"
        return f"{label} {side}"
    return str(kind or "—")


def _aliases(symbol: str) -> set[str]:
    symbol = symbol.strip()
    aliases = {symbol}
    if symbol.endswith("-USD") and len(symbol) > 4:
        aliases.add(symbol[:-4])
    elif symbol and not symbol.endswith("-USD"):
        aliases.add(f"{symbol}-USD")
    return aliases


def load_playbook_names() -> list[dict]:
    if not PLAYBOOK.exists():
        return []
    try:
        raw = json.loads(PLAYBOOK.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    names = raw.get("names") if isinstance(raw, dict) else None
    if not isinstance(names, dict):
        return []
    rows: list[dict] = []
    for key, entry in names.items():
        if not isinstance(entry, dict):
            continue
        row = dict(entry)
        row["_key"] = str(key)
        rows.append(row)
    return rows


def match_playbook(symbol: str, buy_price, rows: list[dict]) -> dict | None:
    """Join a book row to a playbook name.

    Prefer the entry whose buy_price matches the book, including a ``-USD``
    alias. Otherwise use the exact symbol. A second thesis must not supply
    the stop for levels it does not own.
    """
    aliases = _aliases(symbol)
    exact: list[dict] = []
    alias: list[dict] = []
    for row in rows:
        key = str(row.get("_key") or "")
        sym = str(row.get("symbol") or key)
        if symbol and (sym == symbol or key == symbol):
            exact.append(row)
        elif sym in aliases or key in aliases:
            alias.append(row)
    candidates = exact + alias
    if not candidates:
        return None
    if buy_price is not None:
        hits = [row for row in candidates if _as_float(row.get("buy_price")) == buy_price]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            for row in hits:
                if row in exact:
                    return row
            return hits[0]
    return exact[0] if exact else alias[0]


def _fmt_money(value) -> str:
    number = _as_float(value)
    if number is None:
        return "—"
    if number == int(number):
        return f"${int(number)}"
    return f"${number:.2f}"


def format_name(name: dict, playbook: dict | None) -> str:
    symbol = str(name.get("symbol") or "?")
    quote = name.get("quote") if isinstance(name.get("quote"), dict) else {}
    inst = name.get("instrument") if isinstance(name.get("instrument"), dict) else None
    if not inst and isinstance((playbook or {}).get("core"), dict):
        inst = playbook.get("core")

    stop = name.get("stop_price")
    pb_label = None
    if stop is None and playbook:
        stop = playbook.get("stop_price")
        pb_key = str(playbook.get("symbol") or playbook.get("_key") or "")
        if pb_key and pb_key != symbol:
            pb_label = pb_key

    alloc = name.get("allocation")
    if alloc is None and playbook:
        alloc = playbook.get("allocation")
    qty = None if not playbook else playbook.get("contracts")
    lines = [
        symbol,
        f"px: {_fmt_price(name.get('current_price'))}",
        f"pos: {name.get('position') or 'flat'}",
        f"state: {name.get('state') or 'watching'}",
        f"buy: {_fmt_price(name.get('buy_price'))}",
        f"sell: {_fmt_price(name.get('sell_price'))}",
        f"stop: {_fmt_price(stop)}",
        f"alloc: {_fmt_money(alloc)}",
        f"core: {_fmt_core(inst)}",
    ]
    if qty is not None and qty != "":
        lines.append(f"qty: {qty}")
    lines.append(f"signal: {name.get('signal') or 'watch'}")
    if pb_label:
        lines.append(f"pb: {pb_label}")
    if not name.get("enabled", True):
        lines.append("off")
    session = quote.get("session")
    if session and session not in ("regular",):
        lines.append(f"session: {session}")
    if quote.get("stale"):
        lines.append("stale")
    err = quote.get("error")
    if err:
        lines.append(f"error: {err}")
    reason = name.get("state_reason")
    if reason:
        lines.append(f"why: {reason}")
    return "\n".join(lines)


def main() -> int:
    if not BOOK.exists():
        print(f"missing {BOOK}")
        return 1
    try:
        book = json.loads(BOOK.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"trade_book read error: {exc}")
        return 1
    now = datetime.now(NY).strftime("%Y-%m-%d %H:%M ET")
    names = book.get("names") or []
    if not names:
        print(f"{now}\n(empty book)")
        return 0
    playbook_rows = load_playbook_names()
    lines = []
    for name in names:
        if not isinstance(name, dict):
            continue
        playbook = match_playbook(
            str(name.get("symbol") or ""),
            _as_float(name.get("buy_price")),
            playbook_rows,
        )
        lines.append(format_name(name, playbook))
    print(now + "\n\n" + "\n\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
