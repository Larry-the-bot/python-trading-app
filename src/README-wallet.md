# Robinhood MCP Wallet Reader (v1)

Official Hermes MCP only (agentic account 547525758).

## Usage
- Dry-run (samples + fakes): `--dry-run`
- Live: `HERMES_HOME=/opt/data .venv/bin/python -m src.robinhood_wallet --once --json --write`
- Tokens: /opt/data/mcp-tokens/ (Hermes OAuth, never in repo)

## Schema
- source, account_number (selected), accounts list (both, with agentic flag)
- cash: only fields present in get_portfolio (no invented 0.0)
- equities/options/crypto: nonzero only
- totals, errors, stale, updated_at, as_of, note about main account invisibility

## Telegram
trade-book-watchlist appends short "Agentic wallet" block (redacted account, cash, counts, errors). Display-only.

## Out of scope
- Main account positions
- Cron poller / 1m updates
- Trade gating or place_* tools
- Unofficial web auth

Atomic write; on failure keeps previous snapshot.