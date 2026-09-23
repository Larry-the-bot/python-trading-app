---
# Trading App Process Flow (Fundamentals)

**Location:** `desk/PROCESS_FLOW.md`

## Core Cron Jobs

| Cron Job | Runs | Purpose | Key Functions | Writes To |
|----------|------|---------|---------------|-----------|
| price-poller | ~30-60s | Live price stream | `run_stream`, `StreamWriter.flush`, `snapshot_from_stream_tick`, `choose_snap` | `desk/trade_book.json`, `data/quotes.jsonl` |
| state-monitor | 1m | Mark buy_ready / sell_ready | `armed_set` | Reads `desk/trade_book.json` |
| trade-executor | 1m | Buy/sell when ready | `execute_one`, `should_act_on_buy`, `should_act_on_sell`, `apply_state` | `desk/trade_book.json` (position, last_fill, state) |
| playbook sync | on-demand | Sync thesis/levels into book | `sync`, `_prepare_entry`, `legal_instrument` | `desk/trade_book.json` (enabled, prices, instrument, notes) |

## Core Files & Writers

- `trade_book.py` (`save_book`, `apply_state`, `apply_quotes_if_present`) → `desk/trade_book.json`
- `price_stream.py` (`StreamWriter.flush`, `snapshots_to_jsonl`) → `desk/trade_book.json` + `data/quotes.jsonl`
- `playbook.py` (`sync`) → `desk/trade_book.json`
- `trade_executor.py` (`execute_one`) → `desk/trade_book.json`

## State Machine

```
watching → buy_ready (price ≤ buy_price + live quote)
          ↓
       open (position=long after buy)
          ↓
sell_ready (price ≥ sell_price)
          ↓
watching (position=flat after sell)
```

`apply_state` (trade_book.py) is the only function that changes state.