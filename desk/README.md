# Desk trade book

Shared workspace for **this agent**, the **price-monitor** profile, and
`trading-python-app`. Canonical file:

`desk/trade_book.json`

This file is the **trade state machine**. Buy/sell are decided on the
**underlying last price**. The thing we buy or sell is `instrument`
(equity option, or leveraged crypto).

## States

```
disabled ──enable──► watching
watching ──live quote, valid instrument, underlying ≤ buy_price──► buy_ready
buy_ready ──executor sets position=long──► open
buy_ready ──price leaves level, quote not live, or instrument invalid──► watching
open ──live quote, underlying ≥ sell_price──► sell_ready
sell_ready ──executor sets position=flat──► watching
sell_ready ──price leaves level or quote not live──► open
any ──enabled=false──► disabled
```

Poller **never fills**. It only arms `buy_ready` / `sell_ready`. After a
real fill, set `position` to `long`; after an exit, set `position` to
`flat`. The next tick reconciles that into `open` / `watching`.

## Field ownership

| Field | Owner |
|---|---|
| `symbol`, `asset_class`, `enabled`, `buy_price`, `sell_price`, `instrument`, `notes` | operator |
| `position` | executor (fill/exit). Poller syncs it from state after reconcile. |
| `current_price`, `quote.*`, `state`, `signal`, `state_reason`, `state_changed_at` | poller |

A failed fetch keeps the last good `current_price` and sets `quote.error`.

## Guards (poller will not arm)

- Equity `session=closed` or `quote.stale=true`
- Missing/invalid `instrument`
- Option `expiration` before today
- Crypto instrument on an equity (or the reverse)
- No `buy_price` / `sell_price` on that side

## Instrument

Equity:

```json
{"kind": "option", "right": "call", "strike": 25.0, "expiration": "2026-09-25"}
```

Crypto:

```json
{"kind": "leveraged", "leverage": 2.0, "side": "long"}
```

`null` instrument = watched, not tradable.

## How to edit

Set levels and instrument here. Do not invent contracts or leverage.
After a fill, set `position` to `long` (do not jump `state` to `open`
yourself unless you also set position). Next poller tick rewrites
`state` / `signal`.
