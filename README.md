# python-trading-app

Weekly options **max pain** as a trading condition, plus a last-price poller
for the desk watchlist.

Shared trade list and conditions live in `desk/trade_book.json`. It is the
trade state machine: underlying buy/sell levels plus the instrument to
trade (equity option, or leveraged crypto). The poller updates prices and
advances `state`; it never fills. See `desk/README.md`.

Max pain is the strike that minimizes the total intrinsic value of open
call and put interest if the underlying expired at that strike.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Max pain

```python
from max_pain import get_weekly_max_pain

result = get_weekly_max_pain("SPY")
print(result.max_pain, result.expiration, result.spot, result.above_max_pain)
```

`get_weekly_max_pain` loads the nearest weekly option chain via
[yfinance](https://github.com/ranaroussi/yfinance), computes max pain from
open interest, and returns a `MaxPainResult`.

Pure helpers (`compute_max_pain`, `nearest_weekly_expiration`) take in-memory
data and do not hit the network.

## Price poller

`src/price_monitor.py` is observe-only. Watch any 10 Robinhood equities or
crypto at a time. Equities go to public `/quotes/` in one batch; crypto goes to
public `/marketdata/forex/quotes/` in one batch (`BTCUSD`, not `BTC-USD`).
Snapshots append to `data/quotes.jsonl`.

Crypto names: `BTC-USD`, `BTCUSD`, or a shortcut (`BTC`, `ETH`, `SOL`, …).
Tickers that are also stocks (`UNI`, `W`, `COMP`) are equity unless you add
`-USD`.

Pass `--watchlist data/watchlist.txt` or up to 10 CLI symbols (not both).
`--book desk/trade_book.json` is the shared conditions file; after each
snapshot the poller patches `current_price` / `quote` / `signal` for matching
names and leaves buy/sell/instrument/position alone. If you pass neither symbols nor
`--watchlist`, enabled book names are the list when the book exists; otherwise
the hardcoded desk list is:

`SPY QQQ IWM AAPL MSFT NVDA AMZN META GOOGL TLT`

Session clock is America/New_York for equities. Crypto is 24/7 (`session=crypto`)
and stays on a 60-second poll even when cash equities are closed.

| Session | Window | Poll |
|---|---|---|
| pre | weekday 04:00–09:30 | every 60 seconds, Robinhood |
| regular | weekday 09:30–16:00 | every 60 seconds, Robinhood (Yahoo 1m OHLC if `--source both`; `--source yahoo` also fetches here and outside RTH but marks those bars stale) |
| post | weekday 16:00–20:00 | every 60 seconds, Robinhood |
| closed | 20:00–04:00 weekdays, all weekend | 15-minute heartbeat for equity-only lists, expect `stale=true`; 60 seconds if any crypto is on the list |
| crypto | 24/7 | every 60 seconds; stale only if the quote is older than 120 seconds |

Do not run faster than 60 seconds. Yahoo last bars are not live extended-hours
or crypto prices. After 3 consecutive full-batch failures the poller prints
`DATA_BAD` and keeps waiting.

```bash
cd /opt/data/workspace/trading-python-app
.venv/bin/python src/price_monitor.py --source robinhood --interval 60 --log data/quotes.jsonl
.venv/bin/python src/price_monitor.py --source robinhood --once
.venv/bin/python src/price_monitor.py --watchlist data/watchlist.txt --once
.venv/bin/python src/price_monitor.py --source robinhood --once SPY QQQ AAPL MSFT NVDA BTC-USD ETH-USD SOL-USD DOGE-USD XRP-USD
```

Only one live poller may write `data/quotes.jsonl` (lock file `data/quotes.lock`).
A second process exits 1. `--once` still exits 0 after writing error rows.

## Poller monitor

`src/monitor_poller.py` is a read-only 1-minute health check of `data/quotes.jsonl`.
It does not fetch prices and does not start a second poller.

Statuses: `POLLER_OK`, `POLLER_LAG`, `POLLER_DOWN`. Default lag SLA is 90s while
the latest cycle is live (including crypto); 990s for an equity-only `session=closed`
15-minute heartbeat. `--once` exits 1 on lag/down.

```bash
.venv/bin/python src/monitor_poller.py --once
.venv/bin/python src/monitor_poller.py --interval 60 --log data/quotes.jsonl
.venv/bin/python src/monitor_poller.py --once --max-age 90
```

## State monitor

`src/state_monitor.py` is a read-only 1-minute gate on latched trade-book
state. It does not fetch prices and does not fill. Cron wrapper:
`~/.hermes/scripts/state-monitor.py`.

It prints a deterministic JSON array of names whose `state` is already
`buy_ready` or `sell_ready`, with a live quote and a valid `instrument`.
Payload is sorted `symbol` / `state` / `instrument` only — no prices or
clocks — so an identical armed set does not re-wake the executor.

```bash
.venv/bin/python src/state_monitor.py --once --book desk/trade_book.json
```

Hermes cron `monitor=state-monitor.py` (every 1m) wakes the executor
agent only when that payload changes. Empty `[]` is a silent baseline.

## Playbook

`desk/playbook.json` is the generic per-name thesis, buy zone, underlying
sell price, stop, and **one** core instrument. `src/playbook.py sync`
writes operator fields onto the trade book. The poller does not read it.
See `desk/PLAYBOOK.md`.

```bash
.venv/bin/python src/playbook.py sync --playbook desk/playbook.json --book desk/trade_book.json --watchlist data/watchlist.txt
.venv/bin/python src/playbook.py show SPCX
```

## Tests

```bash
pytest
```
