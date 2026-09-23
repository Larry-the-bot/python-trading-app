"""Trade executor: the 1-minute cron job that turns armed states
(buy_ready / sell_ready) into actual positions.

Flow from buy_ready:
1. state_monitor.py detects buy_ready (live quote + valid instrument +
   price <= buy_price) and emits the armed set.
2. Cron wakes execute_one only on payload change.
3. For each name:
   - Load matching playbook entry (required).
   - should_act_on_buy: enforces PLAYBOOK.md rules (in zone, not chasing,
     stop not hit, quote live, optional max_pain bias).
   - If allowed: call TradingAgent.buy(core instrument) — limit order.
   - On success: executor writes position="long" + last_fill; does NOT
     hand-set state (poller + apply_state reconcile to "open").
4. Symmetric sell path from sell_ready.
5. Poller continues to update quotes and can revert states if levels break.

Never place orders from poller, state_monitor, or playbook sync.
All risk rules, sizing, and broker calls live here or in the agent.

See:
- desk/PLAYBOOK.md "Executor contract" and "Smart buying"
- src/trade_book.py for the canonical state machine
- tests/test_trade_executor.py for expected behavior
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path
from typing import Any

from trade_book import apply_state, load_book, save_book
from playbook import load_playbook
from trading_agent import DryRunAgent, RealAgent, get_agent

DEFAULT_BOOK = "desk/trade_book.json"
DEFAULT_PLAYBOOK = "desk/playbook.json"


def should_act_on_buy(
    name: dict[str, Any],
    pb: dict[str, Any],
    max_pain: Any | None = None,
) -> tuple[bool, str]:
    """Return (allowed, reason) for a buy_ready name.

    Enforces the "Smart buying" rules from desk/PLAYBOOK.md:
    - Must be in buy_ready state
    - Current price must be inside [buy_zone[0], buy_zone[1]]
    - Must not be above buy_price (no chase)
    - Stop_price not hit (price > stop_price)
    - Quote must be live (handled upstream but re-checked)
    - Optional max_pain bias (future)

    pb is the playbook entry for the symbol (or defaults).
    """
    if name.get("state") != "buy_ready":
        return False, "not in buy_ready state"

    price = name.get("current_price")
    if price is None:
        return False, "no current_price"

    buy_price = name.get("buy_price")
    if buy_price is not None and price > buy_price:
        return False, "chase: price above buy_price"

    zone = pb.get("buy_zone")
    if not zone or len(zone) != 2:
        return False, "missing or invalid buy_zone in playbook"

    low, high = float(zone[0]), float(zone[1])
    if not (low <= price <= high):
        return False, f"outside buy_zone [{low}, {high}]"

    stop = pb.get("stop_price")
    if stop is not None and price <= float(stop):
        return False, f"stop_price hit ({stop})"

    # Re-validate quote liveness (belt-and-suspenders)
    quote = name.get("quote") or {}
    if quote.get("error") or quote.get("stale"):
        return False, "quote not live"

    session = quote.get("session")
    if session not in ("pre", "regular", "post", "crypto"):
        return False, f"invalid session {session}"

    # TODO: integrate max_pain bias when use_max_pain=True
    if max_pain is not None:
        # placeholder for future filter
        pass

    return True, "ok"


def should_act_on_sell(
    name: dict[str, Any],
    pb: dict[str, Any],
) -> tuple[bool, str]:
    """Return (allowed, reason) for a sell_ready name.

    Rules: state==sell_ready, price >= sell_price, quote live.
    """
    if name.get("state") != "sell_ready":
        return False, "not in sell_ready state"

    price = name.get("current_price")
    sell_price = name.get("sell_price")
    if price is None or sell_price is None:
        return False, "missing price or sell_price"

    if price < float(sell_price):
        return False, "price below sell_price"

    quote = name.get("quote") or {}
    if quote.get("error") or quote.get("stale"):
        return False, "quote not live"

    return True, "ok"


def _get_playbook_entry(pb: dict[str, Any], symbol: str) -> dict[str, Any]:
    """Return the names[symbol] entry or defaults, with safe fallbacks."""
    names = pb.get("names", {}) or {}
    entry = names.get(symbol, {}) or {}
    defaults = pb.get("defaults", {}) or {}
    # merge defaults into entry for missing keys
    merged = {**defaults, **entry}
    return merged


def execute_one(
    book_path: str | Path = DEFAULT_BOOK,
    playbook_path: str | Path = DEFAULT_PLAYBOOK,
    *,
    dry_run: bool = True,
    use_max_pain: bool = False,
    live: bool = False,
) -> dict[str, Any]:
    """Main entry: scan armed names, decide actions, call agent, update book.

    Returns summary dict with actions_taken and list of action records.
    On success for a buy/sell it mutates the book row (position + last_fill),
    then calls apply_state() so the state machine immediately reflects
    open/watching. This matches test expectations and keeps the book
    consistent without waiting for the next poller tick.
    """
    book = load_book(book_path)
    try:
        pb = load_playbook(playbook_path)
    except Exception:
        # fallback to raw json if playbook loader strict
        pb = json.loads(Path(playbook_path).read_text(encoding="utf-8"))

    agent = get_agent(live=live)
    actions: list[dict[str, Any]] = []
    max_pain_result = None  # future: compute if use_max_pain

    for row in book.get("names", []):
        if not row.get("enabled", True):
            continue
        symbol = str(row.get("symbol") or "").strip()
        if not symbol:
            continue

        pbe = _get_playbook_entry(pb, symbol)
        if row.get("state") == "buy_ready":
            allowed, reason = should_act_on_buy(row, pbe, max_pain_result)
            if allowed:
                res = agent.buy(row, pbe, dry_run=dry_run)
                if res.get("success"):
                    fill = res.get("fill") or {}
                    row["position"] = "long"
                    row["last_fill"] = fill
                    apply_state(row)
                    actions.append(
                        {
                            "action": "buy",
                            "symbol": symbol,
                            "success": True,
                            "reason": reason,
                            "fill": fill,
                        }
                    )
                else:
                    actions.append(
                        {
                            "action": "buy",
                            "symbol": symbol,
                            "success": False,
                            "reason": res.get("error") or "agent buy failed",
                        }
                    )

        elif row.get("state") == "sell_ready":
            allowed, reason = should_act_on_sell(row, pbe)
            if allowed:
                res = agent.sell(row, pbe, dry_run=dry_run)
                if res.get("success"):
                    fill = res.get("fill") or {}
                    row["position"] = "flat"
                    row["last_fill"] = fill
                    apply_state(row)
                    actions.append(
                        {
                            "action": "sell",
                            "symbol": symbol,
                            "success": True,
                            "reason": reason,
                            "fill": fill,
                        }
                    )

    if actions:
        save_book(book_path, book)

    return {
        "actions_taken": len([a for a in actions if a.get("success")]),
        "actions": actions,
        "dry_run": dry_run,
        "live": live,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Executor: act on buy_ready/sell_ready states via agent."
    )
    parser.add_argument("--book", default=DEFAULT_BOOK, help="trade book path")
    parser.add_argument("--playbook", default=DEFAULT_PLAYBOOK, help="playbook path")
    parser.add_argument(
        "--once", action="store_true", help="single pass then exit (for cron)"
    )
    parser.add_argument("--dry-run", action="store_true", default=True, help="simulate only")
    parser.add_argument("--live", action="store_true", help="use real agent (dangerous)")
    parser.add_argument(
        "--use-max-pain", action="store_true", help="apply max pain bias on buys"
    )
    args = parser.parse_args(argv)

    dry_run = not args.live and args.dry_run
    summary = execute_one(
        args.book,
        args.playbook,
        dry_run=dry_run,
        use_max_pain=args.use_max_pain,
        live=args.live,
    )
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary.get("actions_taken", 0) >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())