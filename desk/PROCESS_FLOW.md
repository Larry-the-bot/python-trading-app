---
# Trading App Process Flow

**Location:** `desk/PROCESS_FLOW.md` (right next to `PLAYBOOK.md` and `trade_book.json`)

This document explains how the entire trading desk works — from the moment you add a new ticker to the moment the executor buys or sells. It’s written for humans first, then the technical details.

## High-Level Picture

The system has three main jobs:

1. **Keep the trade book fresh** — a live stream writes the latest prices and quotes every few seconds.
2. **Decide what’s ready to trade** — the state machine flips names between `watching`, `buy_ready`, `open`, and `sell_ready`.
3. **Act when it’s time** — every minute the executor checks the ready names, asks the agent to buy or sell, and updates the book.

Everything else (playbook, watchlist, Telegram snapshot) supports these three jobs.

## The Cron Jobs (Who Does What When)

| Job | Runs | What it does | Main files it touches |
| --- | --- | --- | --- |
| **price-poller** | Every 30–60 seconds | Pulls live prices from Yahoo WebSocket + Robinhood fallback and writes them into the trade book | `desk/trade_book.json`, `data/quotes.jsonl` |
| **state-monitor** | Every minute | Looks at the live book and marks which names are `buy_ready` or `sell_ready` | Reads `desk/trade_book.json` |
| **trade-executor** | Every minute | The only job that actually buys or sells. Checks ready names, calls the agent, then updates position and state | `desk/trade_book.json` |
| **playbook sync** | On demand (when you run it) | Takes your thesis, zones, and core contract from the playbook and writes them into the trade book | `desk/trade_book.json` |
| **telegram-desk** | On demand | Pretty snapshot for humans (thesis + current state) | Reads the book and playbook |

## What Writes to What Files

- `trade_book.py` (`save_book`, `apply_state`, `apply_quotes_if_present`) → `desk/trade_book.json` (the single source of truth)
- `price_stream.py` (`StreamWriter.flush`, `snapshots_to_jsonl`) → `desk/trade_book.json` + `data/quotes.jsonl`
- `playbook.py` (`sync`) → `desk/trade_book.json` (enabled, prices, instrument, notes)
- `trade_executor.py` (`execute_one` + `apply_state`) → `desk/trade_book.json` (position, last_fill, state changes)
- You (operator) → `desk/playbook.json`, `data/watchlist.txt`, `desk/playbooks/*.md`

## A Typical Cycle (Step by Step)

1. **You set the strategy**
   - Edit `desk/playbook.json` with thesis, buy/sell zones, stop, and the one core contract you want to trade.
   - Add the ticker to `data/watchlist.txt`.
   - Run `src/playbook.py sync`.
   - Result: the trade book now knows the name is armed with the right levels and instrument.

2. **Prices start flowing**
   - The price-poller cron wakes up.
   - It connects to the Yahoo WebSocket, gets ticks, falls back to Robinhood when needed.
   - It writes the latest price, bid/ask, and “is this quote still fresh?” into `desk/trade_book.json`.
   - It also appends the full snapshot to `data/quotes.jsonl` for later analysis.

3. **The state machine reacts**
   - Every time a new price lands, `apply_state` (in `trade_book.py`) checks the rules.
   - If the price is at or below your `buy_price` and the quote is live → state becomes `buy_ready`.
   - If you’re already long and the price hits `sell_price` → state becomes `sell_ready`.

4. **The executor acts**
   - Every minute the trade-executor cron runs `execute_one`.
   - It looks for names in `buy_ready` or `sell_ready`.
   - For each one it calls `should_act_on_buy` (or sell) — this double-checks the playbook rules (in zone? not chasing? stop not hit? quote still live?).
   - If everything passes, it asks the agent (`trading_agent.py`) to place the order.
   - On success it writes `position=long` (or `flat`), the fill details, and calls `apply_state` again so the state machine immediately moves to `open` or `watching`.

5. **You stay in the loop**
   - Run the Telegram desk snapshot whenever you want a human-readable view of thesis + current state.
   - The poller keeps the book fresh even while you sleep.

## State Machine (Simple Version)

```
watching
   ↓ (price hits buy zone + live quote)
buy_ready
   ↓ (executor checks rules and buys)
open (position = long)
   ↓ (price hits sell price)
sell_ready
   ↓ (executor sells)
watching (position = flat)
```

`apply_state` in `trade_book.py` is the only place that decides what state a name should be in. Everything else just calls it after they make a change.

## Error Handling & Safety

- Stale or closed quotes → name stays in `watching`, never arms.
- Expired option or wrong asset class → playbook sync refuses to write it.
- Agent fails → executor logs the error but does **not** change the position or state.
- Locks (`data/quotes.lock`) stop two writers from fighting over the same file.
- Hard cap of 10 enabled names.

## Where to Look Next

- `desk/PLAYBOOK.md` — the full rules the executor must follow
- `src/trade_book.py` — the state machine itself
- `src/price_stream.py` — how the live quotes actually get written
- `src/trade_executor.py` — the buy/sell decision logic
- `src/playbook.py` — how your thesis becomes executable levels

**Last updated:** 2026-09-23 (made more human-readable, max pain scoped to playbook development only)