"""Latched buy/sell gate for the 1-minute state monitor.

Reads desk/trade_book.json. Trusts poller-latched `buy_ready` /
`sell_ready`. Does not re-test last vs buy_price/sell_price, does not
place orders, and does not spawn an agent. Cron `monitor=` wakes the
executor when this payload changes.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from trade_book import instrument_is_valid, load_book, quote_is_live

DEFAULT_BOOK = "desk/trade_book.json"
DEFAULT_INTERVAL = 60
ARMED_STATES = ("buy_ready", "sell_ready")
SIGNAL_BY_ARMED_STATE = {
    "buy_ready": "buy",
    "sell_ready": "sell",
}


def is_armed(name: dict[str, Any], *, as_of: date | None = None) -> bool:
    if not name.get("enabled", True):
        return False
    state = str(name.get("state") or "")
    if state not in ARMED_STATES:
        return False
    if name.get("signal") != SIGNAL_BY_ARMED_STATE[state]:
        return False
    if not quote_is_live(name):
        return False
    if not instrument_is_valid(name, as_of=as_of):
        return False
    return True


def armed_payload(name: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": name.get("symbol"),
        "state": name.get("state"),
        "instrument": name.get("instrument"),
    }


def armed_set(book: dict[str, Any], *, as_of: date | None = None) -> list[dict[str, Any]]:
    items = [
        armed_payload(name)
        for name in book.get("names") or []
        if isinstance(name, dict) and is_armed(name, as_of=as_of)
    ]
    return sorted(items, key=lambda row: str(row.get("symbol") or ""))


def format_armed_set(items: list[dict[str, Any]]) -> str:
    return json.dumps(items, sort_keys=True, separators=(",", ":"))


def assess_book(path: str | Path, *, as_of: date | None = None) -> str:
    return format_armed_set(armed_set(load_book(path), as_of=as_of))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Emit latched buy_ready/sell_ready names (read-only)."
    )
    parser.add_argument("--book", default=DEFAULT_BOOK, help="trade book JSON path")
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        help="seconds between assessments (floor 60)",
    )
    parser.add_argument("--once", action="store_true", help="one assessment then exit")
    parser.add_argument("--cycles", type=int, default=0, help="stop after N checks (0 = forever)")
    args = parser.parse_args(argv)

    book_path = Path(args.book)
    interval = max(int(args.interval), DEFAULT_INTERVAL)
    cycles = 0
    while True:
        if not book_path.exists():
            print(f"missing {book_path}", file=sys.stderr)
            return 1
        try:
            payload = assess_book(book_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"state-monitor error: {exc}", file=sys.stderr)
            return 1
        print(payload, flush=True)
        cycles += 1
        if args.once or (args.cycles and cycles >= args.cycles):
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
