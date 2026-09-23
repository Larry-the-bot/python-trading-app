---
# Trading App Process Flow

**Location:** `desk/PROCESS_FLOW.md` (alongside `PLAYBOOK.md` and `trade_book.json`)

This document describes the full end-to-end process for the Python trading desk (weekly options max pain + state machine buys/sells). It covers cron jobs, key functions, and exactly which functions write to which files.

## Cron Jobs & Responsibilities

| Cron / Schedule | Script | Purpose | Key Functions | Writes To |
| --- | --- | --- | --- | --- |
| Every ~30-60s (price-poller) | `src/price_stream.py` | Live quote stream from Yahoo WS + RH fallback; keeps trade book fresh | `run_stream`, `StreamWriter.flush/note_stream/note_robinhood`, `robinhood_refresh`, `snapshot_from_stream_tick`, `choose_snap`, `snapshots_to_jsonl`, `_connect`, `decode_message` | `desk/trade_book.json` (current_price, quote, stale, fetched_at, source), `data/quotes.jsonl` |
| Every 1m (state-monitor) | `src/state_monitor.py` | Compute armed set (buy_ready / sell_ready) from live book | `armed_set` (filters enabled + live quote + price levels) | Reads `desk/trade_book.json`; no direct writes (used by executor) |
| Every 1m (trade-executor) | `src/trade_executor.py` | React to buy_ready/sell_ready: call agent, update position, advance state | `execute_one`, `should_act_on_buy`, `should_act_on_sell`, `main` (calls `get_agent().buy/sell` then `apply_state`) | `desk/trade_book.json` (position, last_fill, state via apply_state) |
| On-demand / manual (playbook sync) | `src/playbook.py` | Sync operator playbook into trade book (thesis, zones, core instrument) | `sync`, `_prepare_entry`, `legal_instrument`, `_apply_operator`, `save_book` | `desk/trade_book.json` (enabled, buy_price, sell_price, instrument, notes, armed) |
| On-demand (telegram desk) | `src/telegram_desk.py` | Human-readable snapshot of book + playbook thesis | (observe-only; reads multiple) | Reads `desk/trade_book.json`, `desk/playbook.json` |
| Future / optional | `src/max_pain.py`, `src/monitor_poller.py` | Max pain bias, poller health | (not yet wired into executor) | TBD |

## File Write Map (Function → File)

- `trade_book.py:save_book`, `apply_quotes_if_present`, `apply_state` → `desk/trade_book.json` (canonical state machine + quotes)
- `price_stream.py:StreamWriter.flush`, `snapshots_to_jsonl` → `desk/trade_book.json` + `data/quotes.jsonl`
- `playbook.py:sync`, `save_book` → `desk/trade_book.json`
- `trade_executor.py:execute_one` (via `apply_state`) → `desk/trade_book.json` (position, last_fill, state transitions)
- `price_monitor.py` helpers (parse_robinhood_quote, poll_robinhood, snapshots_to_jsonl) → called by price_stream
- Manual / operator: `desk/playbook.json`, `data/watchlist.txt`, `desk/playbooks/*.md` (dated notes)
- `.hermes.md` (docs only, not runtime)

## Detailed End-to-End Flow

1. **Setup / Operator Input**
   - Edit `desk/playbook.json` (thesis, buy_zone, sell_price, stop_price, core instrument, do_not_buy, invalidation)
   - Edit `data/watchlist.txt` (cap 10 enabled)
   - Optional: add dated note in `desk/playbooks/2026-09-23-XRP.md`
   - Run `src/playbook.py sync --playbook desk/playbook.json --book desk/trade_book.json --watchlist data/watchlist.txt`
     - `playbook.py:sync` calls `_prepare_entry` + `legal_instrument` (rejects expired/invalid cores)
     - Writes `enabled`, `buy_price` (= buy_zone[1]), `sell_price`, `instrument` (legal keys only), `notes` into `trade_book.json`
     - Calls `apply_state` to initialize watching/buy_ready

2. **Live Quote Ingestion (price-poller cron)**
   - `src/price_stream.py:run_stream` starts WebSocket to Yahoo, subscribes to equity symbols
   - On tick: `decode_message` → `snapshot_from_stream_tick` (builds row with session, stale, bid/ask)
   - Periodic RH refresh: `robinhood_refresh` + `snaps_from_robinhood_payload` (venue_time_from_quote)
   - `StreamWriter.note_stream` + `note_robinhood` → `choose_snap` (prefers newer stream)
   - `flush` (if price changed or timeout): `apply_quotes_if_present` (trade_book.py) writes current_price/quote/stale/fetched_at/source to `desk/trade_book.json`
   - Also writes full snapshots to `data/quotes.jsonl` via `snapshots_to_jsonl`
   - `apply_state` (trade_book.py) re-evaluates state: if last ≤ buy_price + live quote → `buy_ready`; if position=long + last ≥ sell_price → `sell_ready`

3. **State Monitoring (state-monitor cron)**
   - `src/state_monitor.py:armed_set` scans `trade_book.json` for enabled names with live quote + correct state (buy_ready or sell_ready)
   - Emits the armed set for the executor (filters stale/closed/invalid instrument)

4. **Execution (trade-executor 1m cron)**
   - `src/trade_executor.py:execute_one` (called with --once by cron)
     - Loads `desk/trade_book.json` + `desk/playbook.json`
     - For each armed name:
       - If `state == "buy_ready"`: `should_act_on_buy` (enforces PLAYBOOK.md: in zone, no chase, stop not hit, live quote)
       - If allowed: `get_agent(live=...) .buy(name, pbe, dry_run=...)` (DryRunAgent or RealAgent)
       - On success: set `position="long"`, `last_fill=fill`, call `apply_state(row)` → advances to `"open"`
     - Symmetric for `sell_ready` → `should_act_on_sell` → agent.sell → `position="flat"` → `apply_state` → `"watching"`
   - Saves updated `trade_book.json`

5. **Human / Telegram View (on-demand)**
   - `src/telegram_desk.py` reads `trade_book.json` + `playbook.json` and renders thesis + current state

6. **Cleanup / Invalidation**
   - Poller `apply_state` reverts buy_ready if price moves out of zone or quote goes stale
   - Executor never invents instruments or places orders directly (only via agent)
   - Playbook sync rejects expired cores, inverted zones, asset-class mismatches

## State Machine (text diagram)

```
watching
  │ (poller: last ≤ buy_price + live quote + armed)
  ▼
buy_ready
  │ (executor 1m: should_act_on_buy passes)
  ▼
[position=long via agent.buy] → apply_state → open
  │ (poller: last ≥ sell_price + position=long)
  ▼
sell_ready
  │ (executor 1m: should_act_on_sell passes)
  ▼
[position=flat via agent.sell] → apply_state → watching
```

**apply_state (trade_book.py)** is the single source of truth for transitions. It is called after every quote update and after every executor action.

## Error / Edge Handling
- Stale/closed quotes → state stays watching, never arms
- Expired core or missing instrument → playbook sync rejects, executor skips
- Agent failure → executor logs, does not change position/state
- Lock files (`data/quotes.lock`, `desk/*.lock`) prevent concurrent writers
- Max 10 enabled names (enforced in playbook sync)

## Future / TODO
- Wire `use_max_pain` bias into `should_act_on_buy` (trade_executor.py)
- RealAgent broker integration (currently stub)
- Cron health monitoring (`src/monitor_poller.py`)

## References
- `desk/PLAYBOOK.md` — full rules and schema
- `src/trade_book.py` — apply_state, save/load, new_name
- `src/price_stream.py` — full stream writer
- `src/trade_executor.py` — executor logic + should_act_*
- `src/playbook.py` — sync and validation
- `.hermes.md` — high-level architecture

**Last updated:** 2026-09-23 (after TradeExecutor merge + price stream addition)