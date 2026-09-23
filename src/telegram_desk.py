"""Observe-only Telegram desk snapshot: watchlist, book state, live last.

Does not place orders. Does not write the trade book. Does not take the
price-poller lock.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from price_monitor import classify_symbol, load_watchlist, poll_once
from trade_book import load_book, new_name

# America/New_York timezone for all timestamps and session labels in output.
NY = ZoneInfo("America/New_York")

# Telegram hard limit on message length (characters). We truncate gracefully
# to stay under this when the full desk would exceed it.
TELEGRAM_MAX = 4096

# Default file locations relative to CWD when run as a script or module.
DEFAULT_BOOK = Path("desk/trade_book.json")
DEFAULT_WATCHLIST = Path("data/watchlist.txt")
DEFAULT_PLAYBOOK = Path("desk/playbook.json")

# Sort priority for desk cards. Lower rank appears first (more actionable on top).
# "sell_ready" and "open" (with position) come before "buy_ready", then passive states.
STATE_RANK = {
    "sell_ready": 0,
    "open": 1,
    "buy_ready": 2,
    "watching": 3,
    "disabled": 4,
}

# Type alias for the HTTP poster callable (used for injection in tests).
HttpPost = Callable[[str, dict[str, Any], int], dict[str, Any]]


def _as_float(value: Any) -> float | None:
    """Convert value to float if possible; return None for missing/empty/invalid.

    Used everywhere prices, levels, strikes etc. are read from JSON/dicts
    to avoid crashes on bad data.
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _canonical(raw: str) -> str:
    """Return the canonical symbol form (e.g. normalizes BTC-USD etc.).

    Delegates to price_monitor.classify_symbol so we stay consistent with
    the watchlist and poller.
    """
    return classify_symbol(raw).symbol


def _load_dotenv() -> None:
    """Load TELEGRAM_* variables from the first existing .env candidate.

    Candidates (in order):
    - /opt/data/.env (container/Hermes location)
    - $HERMES_HOME/.env or ~/.hermes/.env
    - ./.env (local)

    Never overwrites a key that is already set and non-empty in os.environ.
    Only keys starting with TELEGRAM_ are considered. Lines starting with #
    or without = are ignored. This is a minimal loader; no shell expansion.
    """
    candidates = [
        Path("/opt/data/.env"),
        Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / ".env",
        Path(".env"),
    ]
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            key = key.strip()
            if not key.startswith("TELEGRAM_"):
                continue
            if key in os.environ and os.environ[key]:
                continue
            os.environ[key] = value.strip().strip("'").strip('"')
    # Note: we deliberately do not call this in library use; only from main/CLI.


def fmt_price(value: Any) -> str:
    """Format a price for human/Telegram display.

    Rules:
    - None/empty -> "—"
    - >= 1000: $1,234.56 (comma, 2 decimals)
    - >= 100: $123.45 (2 decimals)
    - >= 1: $1.2345 with trailing zeros stripped, fallback to 2 decimals
    - < 1: $0.1234 (4 decimals)
    Negative prices keep the minus sign before the $.
    """
    price = _as_float(value)
    if price is None:
        return "—"
    sign = "-" if price < 0 else ""
    mag = abs(price)
    if mag >= 1000:
        return f"{sign}${mag:,.2f}"
    if mag >= 100:
        return f"{sign}${mag:.2f}"
    if mag >= 1:
        text = f"{mag:.4f}".rstrip("0").rstrip(".")
        if "." not in text:
            text = f"{mag:.2f}"
        return f"{sign}${text}"
    return f"{sign}${mag:.4f}"


def fmt_level(value: Any) -> str:
    """Format a level (buy/sell/stop/strike) for display.

    Similar to fmt_price but without the $ and with slightly different
    integer/decimal rules: integers shown as int, otherwise up to 4 decimals
    with trailing zeros stripped.
    """
    number = _as_float(value)
    if number is None:
        return "—"
    if number >= 1000:
        return f"{number:,.2f}"
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    text = f"{number:.4f}".rstrip("0").rstrip(".")
    return text


def fmt_instrument(inst: Any) -> str:
    """Human string for the armed instrument (option or leveraged).

    - Option: "150C 10/16" (strike + C/P + short MM/DD expiration)
    - Leveraged: "3x long" or "2x short"
    - Anything else / unarmed: "unarmed"
    """
    if not isinstance(inst, dict):
        return "unarmed"
    kind = inst.get("kind")
    if kind == "option":
        right = "C" if str(inst.get("right") or "").lower() == "call" else "P"
        exp = str(inst.get("expiration") or "")
        exp_short = f"{exp[5:7]}/{exp[8:10]}" if len(exp) >= 10 else exp
        strike = _as_float(inst.get("strike"))
        strike_txt = fmt_level(strike)
        return f"{strike_txt}{right} {exp_short}"
    if kind == "leveraged":
        lev = _as_float(inst.get("leverage"))
        side = str(inst.get("side") or "")
        return f"{lev:g}x {side}" if lev is not None else side
    return "unarmed"


def _session_label(name: dict[str, Any]) -> str:
    """Return a short session descriptor for the header line.

    Uses the 'quote' sub-dict:
    - error present -> "error"
    - stale -> "regular/stale" (or "closed" if closed)
    - pre/regular/post/crypto -> "live" for regular/crypto, else the session name
    - no quote at all -> "no quote"
    - fallback -> the raw session or "—"
    """
    quote = name.get("quote") or {}
    if quote.get("error"):
        return "error"
    if quote.get("stale"):
        session = quote.get("session") or "stale"
        return f"{session}/stale" if session != "closed" else "closed"
    session = quote.get("session")
    if session in ("pre", "regular", "post", "crypto"):
        return "live" if session in ("regular", "crypto") else session
    if not quote:
        return "no quote"
    return str(session or "—")


def _vs_buy(name: dict[str, Any]) -> str | None:
    """Compute a short 'vs buy/sell' status string for an armed name.

    - If current >= sell_price: "+X.XX vs sell"
    - Else if current <= buy_price: "in buy zone"
    - Else if buy_price known: "+X.XX vs buy"
    - Otherwise: None (no annotation)
    """
    last = _as_float(name.get("current_price"))
    buy = _as_float(name.get("buy_price"))
    sell = _as_float(name.get("sell_price"))
    if last is None:
        return None
    if sell is not None and last >= sell:
        return f"+{(last - sell):.2f} vs sell"
    if buy is None:
        return None
    if last <= buy:
        return "in buy zone"
    delta = last - buy
    return f"+{delta:.2f} vs buy"


def collect_names(book: dict[str, Any], watchlist_path: str | Path | None) -> list[dict[str, Any]]:
    """Return the list of name rows to display.

    Starts with a shallow copy of book["names"] (if present).
    Then, if a watchlist_path is given and exists, any symbols from the
    watchlist that are *not* already in the book are appended as fresh rows
    (via trade_book.new_name). Existing book entries are never overwritten.
    """
    names = [dict(n) for n in (book.get("names") or []) if isinstance(n, dict)]
    by_symbol = {_canonical(str(n.get("symbol") or "")): n for n in names if n.get("symbol")}
    if not watchlist_path:
        return names
    path = Path(watchlist_path)
    if not path.exists():
        return names
    for item in load_watchlist(path):
        if item.symbol in by_symbol:
            continue
        row = new_name(item.symbol)
        names.append(row)
        by_symbol[item.symbol] = row
    return names


def overlay_live(name: dict[str, Any], snap: dict[str, Any] | None) -> dict[str, Any]:
    """Return a copy of name with live quote fields overlaid from snap.

    Only mutates the copy. Fields copied from snap when present:
    current_price, and quote sub-keys: bid/ask/session/stale/source/fetched_at/error.
    If no snap, returns an exact shallow copy of the original name.
    """
    out = dict(name)
    if not snap:
        return out
    price = snap.get("price")
    if price is not None:
        out["current_price"] = price
    quote = dict(out.get("quote") or {})
    for field in ("bid", "ask", "session", "stale", "source", "fetched_at", "error"):
        if field in snap:
            quote[field] = snap.get(field)
    out["quote"] = quote
    return out


def snapshot_names(book: dict[str, Any], snaps: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Produce a list of names with live data overlaid (used for live mode).

    Builds a symbol->snap index (canonicalized), then maps overlay_live over
    the book's names. Does not mutate the input book or its names list.
    """
    by_symbol: dict[str, dict[str, Any]] = {}
    for snap in snaps or []:
        symbol = snap.get("symbol")
        if not symbol:
            continue
        by_symbol[_canonical(str(symbol))] = snap
    return [
        overlay_live(
            name, by_symbol.get(_canonical(str(name.get("symbol") or "")))
        )
        for name in book.get("names") or []
    ]


def load_playbook_extras(
    path: str | Path,
) -> tuple[dict[str, float], dict[str, list[float]], dict[str, str], str | None]:
    """Parse desk/playbook.json (or equivalent) for display extras.

    Returns:
        stops:   {canonical_symbol: stop_price}
        zones:   {canonical_symbol: [low, high]}  (buy zone)
        theses:  {canonical_symbol: thesis_text}
        macro:   optional top-level summary or bias string (for header)

    Missing file or invalid JSON structure returns empty dicts + None.
    Only "names" entries that are dicts are processed. Zones must be exactly
    a 2-element list of numbers.
    """
    stops: dict[str, float] = {}
    zones: dict[str, list[float]] = {}
    theses: dict[str, str] = {}
    dest = Path(path)
    if not dest.exists():
        return stops, zones, theses, None
    raw = json.loads(dest.read_text(encoding="utf-8"))
    macro_obj = raw.get("macro") if isinstance(raw.get("macro"), dict) else {}
    summary = str((macro_obj or {}).get("summary") or "").strip()
    bias = str((macro_obj or {}).get("bias") or "").strip()
    macro = summary or bias or None
    for key, entry in (raw.get("names") or {}).items():
        if not isinstance(entry, dict):
            continue
        symbol = _canonical(str(entry.get("symbol") or key))
        stop = _as_float(entry.get("stop_price"))
        if stop is not None:
            stops[symbol] = stop
        zone = entry.get("buy_zone")
        if isinstance(zone, (list, tuple)) and len(zone) == 2:
            low, high = _as_float(zone[0]), _as_float(zone[1])
            if low is not None and high is not None:
                zones[symbol] = [low, high]
        thesis = str(entry.get("thesis") or "").strip()
        if thesis:
            theses[symbol] = thesis
    return stops, zones, theses, macro


def _sort_key(name: dict[str, Any]) -> tuple[int, int, str]:
    """Key for sorting names in the desk output.

    Primary: state rank (sell_ready first, disabled last).
    Secondary: armed (0) before unarmed (1).
    Tertiary: symbol string for stable order.
    """
    state = str(name.get("state") or "watching")
    rank = STATE_RANK.get(state, 5)
    armed = 0 if name.get("instrument") else 1
    return rank, armed, str(name.get("symbol") or "")


def _name_card(
    name: dict[str, Any],
    *,
    stops: dict[str, float],
    zones: dict[str, list[float]],
    theses: dict[str, str],
    compact: bool,
) -> str:
    """Build one HTML card (or header line) for a single name.

    Always starts with: <b>SYMBOL</b>  $price  state · session
    If armed and not compact:
        next line: buy-zone / sell / stop levels (using playbook overrides)
        next line: instrument fmt + optional "+X.XX vs buy" annotation
        optional final line: thesis (HTML-escaped)
    Unarmed or compact mode produces a single-line summary.
    """
    symbol = html.escape(str(name.get("symbol") or "?"))
    state = html.escape(str(name.get("state") or "watching"))
    price = fmt_price(name.get("current_price"))
    session = html.escape(_session_label(name))
    head = f"<b>{symbol}</b>  {html.escape(price)}  {state} · {session}"
    if compact or not name.get("instrument"):
        return head
    symbol_key = _canonical(str(name.get("symbol") or ""))
    zone = zones.get(symbol_key)
    buy = zone[1] if zone else name.get("buy_price")
    low = zone[0] if zone else None
    sell = name.get("sell_price")
    stop = stops.get(symbol_key)
    bits = []
    if low is not None and buy is not None:
        bits.append(f"{fmt_level(low)}–{fmt_level(buy)} buy")
    elif buy is not None:
        bits.append(f"buy {fmt_level(buy)}")
    if sell is not None:
        bits.append(f"{fmt_level(sell)} sell")
    if stop is not None:
        bits.append(f"{fmt_level(stop)} stop")
    lines = [head]
    if bits:
        lines.append(" · ".join(bits))
    core = fmt_instrument(name.get("instrument"))
    vs = _vs_buy(name)
    extra = [core]
    if vs:
        extra.append(vs)
    lines.append(" · ".join(extra))
    thesis = theses.get(symbol_key)
    if thesis:
        lines.append(html.escape(thesis))
    return "\n".join(lines)


def format_desk(
    book: dict[str, Any],
    *,
    now: datetime | None = None,
    stops: dict[str, float] | None = None,
    zones: dict[str, list[float]] | None = None,
    theses: dict[str, str] | None = None,
    macro: str | None = None,
    limit: int = TELEGRAM_MAX,
) -> str:
    """Render the full desk snapshot as Telegram HTML (truncated if needed).

    Header always includes:
        Desk
        <timestamp> · N names · N open · N armed [· N ready]

    Optional macro line right after header.

    Sorting: _sort_key (actionable states first, armed before unarmed).

    Rendering strategy (tries in order until under limit):
    1. Full detail, all armed expanded, unarmed expanded.
    2. Compact prices/levels on armed, unarmed still expanded.
    3. Compact armed + "unarmed: T00, T01, ..." collapsed list.

    If still too long after step 3, hard-truncates with "…".

    The inner render() closure handles the three modes without code duplication.
    """
    now = now or datetime.now(NY)
    if now.tzinfo is None:
        now = now.replace(tzinfo=NY)
    else:
        now = now.astimezone(NY)
    stops = stops or {}
    zones = zones or {}
    theses = theses or {}
    names = sorted(
        (n for n in (book.get("names") or []) if isinstance(n, dict)), key=_sort_key
    )
    n = len(names)
    n_open = sum(
        1 for x in names if x.get("position") == "long" or x.get("state") in ("open", "sell_ready")
    )
    n_armed = sum(1 for x in names if x.get("instrument"))
    n_ready = sum(1 for x in names if x.get("state") in ("buy_ready", "sell_ready"))
    stamp = now.strftime("%a %d %b %H:%M ET")
    header = (
        f"<b>Desk</b>\n{html.escape(stamp)} · {n} names · {n_open} open · "
        f"{n_armed} armed"
        + (f" · {n_ready} ready" if n_ready else "")
    )
    if macro:
        header = f"{header}\n{html.escape(macro)}"

    def render(*, compact: bool, collapse_unarmed: bool) -> str:
        """Inner renderer for one of the three progressive compactness modes."""
        armed_cards = []
        unarmed_bits = []
        for name in names:
            if name.get("instrument") and not collapse_unarmed:
                armed_cards.append(
                    _name_card(name, stops=stops, zones=zones, theses=theses, compact=compact)
                )
            elif name.get("instrument"):
                armed_cards.append(
                    _name_card(name, stops=stops, zones=zones, theses=theses, compact=True)
                )
            else:
                if collapse_unarmed:
                    unarmed_bits.append(html.escape(str(name.get("symbol") or "?")))
                else:
                    unarmed_bits.append(
                        f"{html.escape(str(name.get('symbol') or '?'))} {html.escape(fmt_price(name.get('current_price')))}"
                    )
        parts = [header]
        if armed_cards:
            parts.append("\n\n".join(armed_cards))
        if unarmed_bits:
            if collapse_unarmed:
                parts.append("unarmed: " + ", ".join(unarmed_bits))
            else:
                parts.append("<b>Watching</b>\n" + " · ".join(unarmed_bits))
        return "\n\n".join(parts).strip()

    text = render(compact=False, collapse_unarmed=False)
    if len(text) <= limit:
        return text
    text = render(compact=True, collapse_unarmed=False)
    if len(text) <= limit:
        return text
    text = render(compact=True, collapse_unarmed=True)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def default_http_post(url: str, payload: dict[str, Any], timeout: int = 10) -> dict[str, Any]:
    """Minimal urllib-based JSON POST (used by send_telegram).

    Sets a distinctive User-Agent. Raises on HTTP errors via urlopen.
    Returns the parsed JSON response body.
    """
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "trading-python-app-telegram-desk/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def send_telegram(
    text: str,
    *,
    token: str,
    chat_id: str,
    http_post: HttpPost | None = None,
) -> dict[str, Any]:
    """Send a message via Telegram Bot API using HTML parse mode.

    Uses the provided http_post (or default_http_post).
    Raises RuntimeError if the API returns ok=false.
    Returns the raw result dict on success.
    """
    poster = http_post or default_http_post
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    result = poster(url, payload, 10)
    if not result.get("ok"):
        raise RuntimeError(result.get("description") or "telegram send failed")
    return result


def _resolve_chat_id() -> str | None:
    """Return the first non-empty chat id from env (home channel preferred)."""
    return (
        os.environ.get("TELEGRAM_CHAT_ID")
        or os.environ.get("TELEGRAM_HOME_CHANNEL")
        or None
    )


def _fetch_live(symbols: list[str]) -> list[dict[str, Any]]:
    """Fetch a live snapshot for the given symbols via price_monitor.

    On any exception returns [] (silent fallback to book data only).
    Source is hardcoded to "robinhood" for the desk snapshot.
    """
    if not symbols:
        return []
    try:
        return poll_once(symbols, source="robinhood")
    except Exception:
        return []


def build_message(
    *,
    book_path: str | Path,
    watchlist_path: str | Path | None,
    playbook_path: str | Path | None,
    live: bool,
    now: datetime | None = None,
) -> str:
    """Assemble the complete desk message text.

    - Loads book (or empty if missing).
    - Merges watchlist symbols (if path given).
    - If live=True: fetches fresh Robinhood prices and overlays them
      (via snapshot_names / overlay_live). Never mutates the on-disk book.
    - Loads playbook extras (stops/zones/theses/macro).
    - Formats via format_desk.
    """
    dest = Path(book_path)
    book = load_book(dest) if dest.exists() else {"names": []}
    names = collect_names(book, watchlist_path)
    if live:
        symbols = [str(n.get("symbol")) for n in names if n.get("symbol")]
        snaps = _fetch_live(symbols)
        names = [
            overlay_live(
                n,
                next(
                    (
                        s
                        for s in snaps
                        if _canonical(str(s.get("symbol") or ""))
                        == _canonical(str(n.get("symbol") or ""))
                    ),
                    None,
                ),
            )
            for n in names
        ]
    book = dict(book)
    book["names"] = names
    stops, zones, theses, macro = load_playbook_extras(playbook_path or DEFAULT_PLAYBOOK)
    return format_desk(book, now=now, stops=stops, zones=zones, theses=theses, macro=macro)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for telegram-desk.

    --once is required (or --dry-run) to actually run; otherwise errors.
    --dry-run: build and print, never send.
    --no-live: skip Robinhood fetch, use only book + watchlist data.
    --chat-id: override the env chat id for this run.

    Loads .env for TELEGRAM_* vars first.
    Returns 0 on success (or dry-run), 1 on runtime error, 2 on missing token/chat.
    """
    parser = argparse.ArgumentParser(description="Send a desk snapshot to Telegram (observe only).")
    parser.add_argument("--book", default=str(DEFAULT_BOOK))
    parser.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST))
    parser.add_argument("--playbook", default=str(DEFAULT_PLAYBOOK))
    parser.add_argument("--once", action="store_true", help="build one message and exit")
    parser.add_argument("--dry-run", action="store_true", help="print the message, do not send")
    parser.add_argument("--no-live", action="store_true", help="use book quotes only, skip Robinhood")
    parser.add_argument("--chat-id", help="override TELEGRAM_HOME_CHANNEL / TELEGRAM_CHAT_ID")
    args = parser.parse_args(argv)

    _load_dotenv()
    try:
        text = build_message(
            book_path=args.book,
            watchlist_path=args.watchlist if Path(args.watchlist).exists() else None,
            playbook_path=args.playbook,
            live=not args.no_live,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"telegram-desk error: {exc}", file=sys.stderr)
        return 1

    if args.dry_run or not args.once:
        if not args.once and not args.dry_run:
            parser.error("pass --once (or --dry-run)")
        print(text)
        if args.dry_run:
            return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    chat_id = args.chat_id or _resolve_chat_id() or ""
    if not token or not chat_id:
        print("telegram-desk error: TELEGRAM_BOT_TOKEN and chat id required", file=sys.stderr)
        return 2
    try:
        send_telegram(text, token=token, chat_id=chat_id)
    except (RuntimeError, OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"telegram-desk error: {exc}", file=sys.stderr)
        return 1
    print("sent", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
