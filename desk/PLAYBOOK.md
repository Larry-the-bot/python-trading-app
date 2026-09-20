# Desk playbook

Loader always reads `desk/playbook.json`. Optional dated copies may live under
`desk/playbooks/`. The poller does **not** read this file.

Thesis, shopping list, and the single core contract live here — not in
symbol branches in Python. Adding `COST` or `GME` later is a playbook object
plus `playbook.py sync`. No code change for names 4–10.

Trade book still only stores what `src/trade_book.py` already understands:
`enabled`, `buy_price`, `sell_price`, `instrument`, `notes`. `stop_price` is
playbook-only. Do not add it to `next_state()`.

## Smart buying (every name)

Executor must follow these for any symbol. Defaults are also in
`desk/playbook.json` → `defaults`.

1. **No chase.** If underlying last is above `buy_price` / `buy_zone[1]`,
   leave the name `watching`. Do not lift `buy_price`. Do not buy the call.
2. **Buy only inside the zone.** Fill only when last is in
   `[buy_zone[0], buy_zone[1]]` **and** book state is `buy_ready` **and**
   quote is live (`session` in `pre|regular|post` or `crypto`, `stale=false`,
   no `quote.error`).
3. **Stop overrides the zone.** If last ≤ `stop_price`, do not buy. If
   already long, flatten and set `enabled=false`. `stop_price` is
   playbook-only; the state machine does not own it.
4. **One instrument.** Buy the playbook `core` contract only. Do not
   substitute weeklies, farther OTM calls, or spreads unless `core` itself
   is missing at the broker (then abort, do not improvise).
5. **Limit orders only.** Equity options: limit at mid or better. If the
   spread is wider than 8% of mid, pass and retry next live quote. Never
   market-buy small-cap / high-IV names.
6. **Size.** Default `contracts=1`. Option premium risk must not exceed the
   dollar risk of `(buy_price − stop_price) × 100` unless the playbook says
   otherwise.
7. **Same contract out.** On `sell_ready` (underlying last ≥ `sell_price`),
   sell the **same** `instrument` with a limit. Do not roll unless
   `invalidation` says the thesis is dead — then flatten.
8. **Expired / wrong-date cores are invalid.** Sync and
   `instrument_is_valid()` reject `expiration < today`.
9. **Unarmed is valid.** A watchlist name with no playbook `core` stays
   `instrument=null`, levels null, state `watching`.

`buy_price` is the **top of the buy zone**. `sell_price` is the
**underlying last** at which the executor sells the same instrument. The
option premium is not the trigger.

## Schema

```json
{
  "version": 1,
  "updated_at": "ISO-8601",
  "macro": { "summary": "", "calendar": [], "bias": "" },
  "defaults": {
    "orders": "limit mid-or-better",
    "no_chase": true,
    "stop_owner": "executor",
    "max_enabled": 10
  },
  "names": { "SYMBOL": { } }
}
```

Each `names[SYMBOL]` (required if armed):

| field | required if armed | meaning |
|---|---|---|
| `symbol` | yes | must match watchlist + book (`SPCX`, `NVDA`, `BTC-USD`, …) |
| `asset_class` | yes | `equity` \| `crypto` |
| `enabled` | yes | |
| `thesis` | yes | story + why it is on the desk |
| `technical` | no | chart context |
| `buy_zone` | yes | `[low, high]` underlying. Book `buy_price` **must equal `buy_zone[1]`** |
| `buy_price` | yes | top of zone — poller arms at last ≤ this |
| `sell_price` | yes | **underlying last at which we sell the instrument** |
| `targets` | no | further underlying levels after first scale |
| `stop_price` | yes | executor hard stop on the underlying |
| `core` | yes | the single contract we buy and sell |
| `contracts` | no | default 1 |
| `do_not_buy` | no | banned contracts / behaviors |
| `invalidation` | no | thesis break |
| `notes` | no | one-liner copied onto the book row |

`core` for equity (sync writes legal keys only onto `trade_book.instrument`):

```json
{
  "kind": "option",
  "right": "call",
  "strike": 150.0,
  "expiration": "2026-10-16",
  "role": "core"
}
```

Crypto:

```json
{ "kind": "leveraged", "leverage": 2.0, "side": "long", "role": "core" }
```

Do not put debit spreads in `instrument`. Document them in a non-synced
`alternates` array if needed. The executor still buys `core`.

Legal book instrument keys:

```json
{"kind": "option", "right": "call", "strike": 150.0, "expiration": "2026-10-16"}
{"kind": "leveraged", "leverage": 2.0, "side": "long"}
```

## How any future name is added

1. Add `TICKER` to `data/watchlist.txt` or only to `desk/playbook.json`.
2. Write `names.TICKER` with thesis, `buy_zone`, **`sell_price` (underlying
   exit)**, `stop_price`, and **one** `core` call (or leveraged crypto
   instrument).
3. Run `playbook.py sync`.
4. Poller updates quotes; may flip `buy_ready` when last ≤ `buy_price`.
5. `state_monitor.py --once` emits the armed set.
6. Executor smart-buys `core` inside the zone; sells that same `core` when
   last ≥ `sell_price`.

No code change for names 4–10. Cap 10 enabled names.

```bash
.venv/bin/python src/playbook.py sync --playbook desk/playbook.json --book desk/trade_book.json --watchlist data/watchlist.txt
.venv/bin/python src/playbook.py show SYMBOL
```

Sync:

- upserts playbook symbols into the book via `trade_book.new_name` / update
- unions watchlist, cap 10 enabled
- writes `enabled`, `buy_price`, `sell_price`, `instrument` (legal keys of
  `core` only), `notes`
- calls `apply_state()` after operator fields are written
- does not delete book rows that lack a playbook key
- does not invent quotes or positions
- rejects expired option cores, inverted `buy_zone`, missing `sell_price` /
  `stop_price` on an armed name, asset-class mismatch

`show SYMBOL` prints thesis, buy zone, **underlying sell price**, stop,
core contract, and current book state.

## Executor contract

When `src/state_monitor.py --once` prints a non-empty armed set, for
**each** symbol:

1. Load `desk/playbook.json` → `names[symbol]`. If missing, abort.
2. If last ≤ `stop_price`: skip / flatten, `enabled=false`.
3. If `state==buy_ready` and last is inside `buy_zone`: buy `core` with a
   smart limit (`contracts` or 1).
4. After broker ack: `position=long`. Do not hand-set `state=open`.
5. If `state==sell_ready` (underlying last ≥ `sell_price`): sell the
   **same** `core` with a limit, then `position=flat`.

There is no broker adapter in this repo. Do not place live orders from
sync, poller, or state monitor. Keep `BOOK_VERSION` at 3.

## Seed this week

Armed rows live in `desk/playbook.json` (SPCX 150C, NVDA 220C, FLY 20C,
all 2026-10-16). Friday cash refs (2026-09-18): SPCX 152.71, NVDA 222.27,
FLY 20.97 — after sync those prints stay `watching` until a **live** dip
into the zone. GME / XRP-USD / BTC-USD stay unarmed unless operator levels
are added later.
