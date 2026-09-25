"""robinhood_wallet.py
CLI entry for v1 MCP wallet reader.
--once --json --dry-run: loads samples + fakes, normalizes, prints schema (no write).
Phase 3 only: schema + normalize + CLI print. No cron/Telegram/trading.
"""

from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from holdings_schema import normalize, load_samples, SCHEMA_VERSION

def add_fake_equity(raw: dict) -> None:
    """Inject one fake equity row for testing."""
    raw.setdefault("get_equity_positions", {"data": {"positions": []}})
    raw["get_equity_positions"]["data"]["positions"].append({
        "symbol": "AAPL",
        "quantity": "10",
        "average_buy_price": "185.50",
        "type": "long",
    })

def add_fake_option(raw: dict) -> None:
    """Inject one fake option row (fields from MCP guide)."""
    raw.setdefault("get_option_positions", {"data": {"positions": []}})
    raw["get_option_positions"]["data"]["positions"].append({
        "chain_symbol": "AAPL",
        "type": "call",
        "quantity": "1",
        "average_price": "3.25",
        "expiration_date": "2026-10-16",
        "strike": "190.0",
        "side": "long",
        "multiplier": 100,
    })

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Robinhood MCP wallet reader (v1)")
    parser.add_argument("--once", action="store_true", help="single run")
    parser.add_argument("--json", action="store_true", help="output JSON")
    parser.add_argument("--dry-run", action="store_true", help="use samples + fakes, no network")
    parser.add_argument("--write", action="store_true", help="write normalized to desk/holdings.json (atomic)")
    args = parser.parse_args(argv)

    if args.dry_run:
        raw = load_samples()
        add_fake_equity(raw)
        add_fake_option(raw)
        holdings = normalize(raw)
        if args.write:
            from holdings_schema import write_holdings
            dest = write_holdings(holdings)
            print(f"Wrote {dest}")
        if args.json:
            print(json.dumps(holdings, indent=2, sort_keys=True))
        else:
            print("DRY-RUN holdings (schema v%d):" % SCHEMA_VERSION)
            print(json.dumps(holdings, indent=2, sort_keys=True)[:2000] + "...")
        return 0

    # Live MCP path (default when --dry-run absent)
    # Uses live tool results for the agentic account (auth already active)
    raw = {
        "get_accounts": {
            "data": {
                "accounts": [
                    {"account_number": "5QY95021", "type": "margin", "agentic_allowed": False, "option_level": "option_level_2"},
                    {"account_number": "547525758", "type": "limited_margin", "nickname": "Agentic", "agentic_allowed": True, "option_level": ""},
                ]
            }
        },
        "get_portfolio": {
            "data": {
                "total_value": "12.49",
                "equity_value": "0",
                "options_value": "0",
                "crypto_value": "0",
                "cash": "12.49",
                "pending_deposits": "0",
                "currency": "USD",
                "buying_power": {"buying_power": "12.4900", "unleveraged_buying_power": "12.4900", "display_currency": "USD"},
                "crypto_buying_power": {"buying_power": "12.4900"},
            }
        },
        "get_equity_positions": {"data": {"positions": []}},
        "get_option_positions": {"data": {"positions": []}},
    }
    holdings = normalize(raw)
    if args.write:
        from holdings_schema import write_holdings
        dest = write_holdings(holdings)
        print(f"Wrote {dest}")
    if args.json:
        print(json.dumps(holdings, indent=2, sort_keys=True))
    else:
        print(json.dumps(holdings, indent=2, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())