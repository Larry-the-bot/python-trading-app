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

NY = ZoneInfo("America/New_York")
TELEGRAM_MAX = 4096
DEFAULT_BOOK = Path("desk/trade_book.json")
DEFAULT_WATCHLIST = Path("data/watchlist.txt")
DEFAULT_PLAYBOOK = Path("desk/playbook.json")
STATE_RANK = {
    "sell_ready": 0,
    "open": 1,
    "buy_ready": 2,
    "watching": 3,
    "disabled": 4,
}
HttpPost = Callable[[str, dict[str, Any], int], dict[str, Any]]


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _canonical(raw: str) -> str:
    return classify_symbol(raw).symbol


def _load_dotenv() -> None:
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


def fmt_price(value: Any) -> str:
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
    by_symbol: dict[str, dict[str, Any]] = {}
    for snap in snaps or []:
        symbol = snap.get("symbol")
        if not symbol:
            continue
        by_symbol[_canonical(str(symbol))] = snap
    return [overlay_live(name, by_symbol.get(_canonical(str(name.get("symbol") or "")))) for name in book.get("names") or []]


def load_playbook_extras(
    path: str | Path,
) -> tuple[dict[str, float], dict[str, list[float]], dict[str, str], str | None]:
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
    now = now or datetime.now(NY)
    if now.tzinfo is None:
        now = now.replace(tzinfo=NY)
    else:
        now = now.astimezone(NY)
    stops = stops or {}
    zones = zones or {}
    theses = theses or {}
    names = sorted((n for n in (book.get("names") or []) if isinstance(n, dict)), key=_sort_key)
    n = len(names)
    n_open = sum(1 for x in names if x.get("position") == "long" or x.get("state") in ("open", "sell_ready"))
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
    return os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_HOME_CHANNEL") or None


def _fetch_live(symbols: list[str]) -> list[dict[str, Any]]:
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
                        if _canonical(str(s.get("symbol") or "")) == _canonical(str(n.get("symbol") or ""))
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
