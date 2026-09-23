"""Long-running last-print stream for the desk book.

Single writer of ``data/quotes.jsonl`` and ``desk/trade_book.json`` quote
fields. Holds ``data/quotes.lock`` for the process lifetime so the 1-minute
``price_monitor.py --once`` tick cannot overwrite a newer print.

Equities come from the public Yahoo quote websocket
(``wss://streamer.finance.yahoo.com``). Robinhood public ``/quotes/`` is the
seed and the fallback; it must not replace a newer stream print. Crypto, when
named, still uses Robinhood ``mark_price`` only.

Closed equity session stays ``stale=true`` so the executor does not arm.
The price written to the book is still the latest stream print.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from price_monitor import (
    NY,
    STALE_SECONDS,
    acquire_poller_lock,
    classify_symbol,
    load_watchlist,
    parse_robinhood_quote,
    poll_robinhood,
    session_at,
    snapshots_to_jsonl,
)
from trade_book import apply_quotes_if_present

STREAM_URL = "wss://streamer.finance.yahoo.com/?version=2"
BOOK_FLUSH_SECONDS = 5
JSONL_FLUSH_SECONDS = 30
RH_OPEN_SECONDS = 60
RH_CLOSED_SECONDS = 900
SOCKET_RETRY_SECONDS = 3
LOCK_RETRY_SECONDS = 20


def _parse_ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def tick_time(tick: dict[str, Any]) -> datetime | None:
    raw = tick.get("time")
    if raw is None or raw == "":
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    if number > 10_000_000_000:
        number = number / 1000.0
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def round_price(tick: dict[str, Any]) -> float | None:
    raw = tick.get("price")
    if raw is None or raw == "":
        return None
    try:
        price = float(raw)
    except (TypeError, ValueError):
        return None
    hint = tick.get("price_hint")
    try:
        places = int(hint) if hint is not None and hint != "" else 2
    except (TypeError, ValueError):
        places = 2
    places = min(max(places, 0), 8)
    return round(price, places)


def snapshot_from_stream_tick(
    tick: dict[str, Any],
    *,
    now: datetime,
    symbol: str | None = None,
) -> dict[str, Any] | None:
    """Build a book/JSONL row from one Yahoo pricing tick.

    Session comes from the desk clock, not Yahoo ``market_hours``. A closed
    equity session is always stale so a fresh overnight print cannot arm.
    """
    price = round_price(tick)
    if price is None:
        return None
    name = str(symbol or tick.get("id") or "").upper()
    if not name:
        return None
    trade_at = tick_time(tick)
    session = session_at(now)
    age = None if trade_at is None else (now - trade_at).total_seconds()
    stale = session == "closed" or trade_at is None or age is None or age is None or age > STALE_SECONDS
    bid = tick.get("bid")
    ask = tick.get("ask")
    try:
        bid_f = float(bid) if bid not in (None, "") else None
    except (TypeError, ValueError):
        bid_f = None
    try:
        ask_f = float(ask) if ask not in (None, "") else None
    except (TypeError, ValueError):
        ask_f = None
    return {
        "symbol": name,
        "asset_class": "equity",
        "price": price,
        "bar_time": trade_at.isoformat() if trade_at else None,
        "updated_at": trade_at.isoformat() if trade_at else None,
        "fetched_at": now.isoformat(),
        "source": "yahoo_stream",
        "session": session,
        "bid": bid_f,
        "ask": ask_f,
        "stale": stale,
        "error": None,
    }


def choose_snap(stream_snap: dict[str, Any] | None, rh_snap: dict[str, Any] | None) -> dict[str, Any] | None:
    """Prefer the newer print. A tie, or a stream tick at least as new, keeps the stream.

    Robinhood bid/ask fill gaps when the stream tick omitted them. A newer
    Robinhood venue/update time replaces the stream price.
    """
    if stream_snap is None and rh_snap is None:
        return None
    if stream_snap is None:
        return dict(rh_snap) if rh_snap else None
    if rh_snap is None:
        return dict(stream_snap)
    stream_at = _parse_ts(stream_snap.get("updated_at"))
    rh_at = _parse_ts(rh_snap.get("updated_at"))
    stream_wins = True
    if stream_at and rh_at and rh_at > stream_at + timedelta(seconds=1):
        stream_wins = False
    if not stream_wins:
        chosen = dict(rh_snap)
    else:
        chosen = dict(stream_snap)
        for field in ("bid", "ask"):
            if chosen.get(field) is None and rh_snap.get(field) is not None:
                chosen[field] = rh_snap[field]
    fetched = stream_snap.get("fetched_at") or rh_snap.get("fetched_at")
    if fetched:
        chosen["fetched_at"] = fetched
    return chosen


def robinhood_refresh(symbols: list[str], *, now: datetime | None = None) -> list[dict[str, Any]]:
    """One Robinhood batch per asset class. Equity rows carry the venue trade time."""
    from price_monitor import ROBINHOOD_QUOTES_URL, default_http_get

    now = now or datetime.now(NY)
    if not symbols:
        return []
    equities = [s for s in symbols if classify_symbol(s).asset_class == "equity"]
    cryptos = [s for s in symbols if classify_symbol(s).asset_class == "crypto"]
    snaps: list[dict[str, Any]] = []
    if equities:
        url = ROBINHOOD_QUOTES_URL + "?symbols=" + ",".join(
            classify_symbol(s).rh_symbol for s in equities
        )
        snaps.extend(snaps_from_robinhood_payload(default_http_get(url), equities, now=now))
    if cryptos:
        snaps.extend(poll_robinhood(cryptos, now=now))
    return snaps


def venue_time_from_quote(quote: dict[str, Any]) -> datetime | None:
    """Later of the venue trade stamps that match the price Robinhood would publish."""
    extended = quote.get("last_extended_hours_trade_price")
    if extended not in (None, ""):
        raw = quote.get("venue_last_non_reg_trade_time") or quote.get("updated_at")
    else:
        raw = quote.get("venue_last_trade_time") or quote.get("updated_at")
    return _parse_ts(raw)


def snaps_from_robinhood_payload(
    payload: Any,
    symbols: list[str],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    """Parse a Robinhood ``/quotes/`` payload and stamp venue trade time onto updated_at."""
    from price_monitor import _extract_results

    by_symbol: dict[str, dict[str, Any]] = {}
    for quote in _extract_results(payload):
        if not isinstance(quote, dict):
            continue
        sym = str(quote.get("symbol") or "").upper()
        if sym:
            by_symbol[sym] = quote
    out: list[dict[str, Any]] = []
    for raw in symbols:
        item = classify_symbol(raw)
        if item.asset_class != "equity":
            continue
        quote = by_symbol.get(item.rh_symbol)
        if quote is None:
            continue
        snap = parse_robinhood_quote(quote, now=now, fetched_at=now)
        snap["symbol"] = item.symbol
        venue = venue_time_from_quote(quote)
        if venue is not None:
            snap["updated_at"] = venue.isoformat()
            snap["bar_time"] = venue.isoformat()
        out.append(snap)
    return out


class StreamWriter:
    """In-memory latest prints. Flushes the book and a JSONL cycle."""

    def __init__(self, book: Path, log_path: Path) -> None:
        self.book = book
        self.log_path = log_path
        self.stream: dict[str, dict[str, Any]] = {}
        self.robinhood: dict[str, dict[str, Any]] = {}
        self._last_book = 0.0
        self._last_jsonl = 0.0
        self._last_prices: dict[str, float | None] = {}

    def note_stream(self, snap: dict[str, Any]) -> bool:
        symbol = str(snap.get("symbol") or "")
        if not symbol:
            return False
        previous = self.stream.get(symbol)
        self.stream[symbol] = snap
        return previous is None or previous.get("price") != snap.get("price")

    def note_robinhood(self, snaps: list[dict[str, Any]]) -> None:
        for snap in snaps:
            symbol = str(snap.get("symbol") or "")
            if symbol:
                self.robinhood[symbol] = snap

    def rows(self, symbols: list[str], *, now: datetime) -> list[dict[str, Any]]:
        fetched = now.isoformat()
        rows: list[dict[str, Any]] = []
        for symbol in symbols:
            chosen = choose_snap(self.stream.get(symbol), self.robinhood.get(symbol))
            if chosen is None:
                continue
            chosen = dict(chosen)
            chosen["fetched_at"] = fetched
            chosen["symbol"] = symbol
            rows.append(chosen)
        return rows

    def flush(
        self,
        symbols: list[str],
        *,
        now: datetime,
        force_book: bool = False,
        force_jsonl: bool = False,
    ) -> list[dict[str, Any]]:
        wall = time.monotonic()
        price_changed = False
        rows = self.rows(symbols, now=now)
        for row in rows:
            symbol = str(row.get("symbol") or "")
            price = row.get("price")
            if symbol and self._last_prices.get(symbol) != price:
                price_changed = True
                self._last_prices[symbol] = price
        write_book = force_book or price_changed or (wall - self._last_book) >= BOOK_FLUSH_SECONDS
        write_jsonl = force_jsonl or price_changed or (wall - self._last_jsonl) >= JSONL_FLUSH_SECONDS
        if write_book and rows:
            apply_quotes_if_present(self.book, rows)
            self._last_book = wall
        if write_jsonl and rows:
            snapshots_to_jsonl(self.log_path, rows)
            self._last_jsonl = wall
        return rows if write_book or write_jsonl else []


def decode_message(raw: str | bytes) -> dict[str, Any] | None:
    from yfinance.live import BaseWebSocket

    text = raw.decode() if isinstance(raw, bytes) else raw
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    encoded = payload.get("message")
    if not encoded:
        return None
    decoded = BaseWebSocket(verbose=False)._decode_message(encoded)
    if not isinstance(decoded, dict) or decoded.get("error"):
        return None
    return decoded


def equity_symbols(symbols: list[str]) -> list[str]:
    out: list[str] = []
    for raw in symbols:
        item = classify_symbol(raw)
        if item.asset_class == "equity":
            out.append(item.rh_symbol)
    return out


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def pid_is_stream(pid: int) -> bool:
    if not pid_alive(pid):
        return False
    cmdline = Path(f"/proc/{pid}/cmdline")
    try:
        blob = cmdline.read_bytes()
    except OSError:
        return False
    return b"price_stream.py" in blob


def read_pidfile(path: Path) -> tuple[int | None, float | None]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, None
    if not lines:
        return None, None
    try:
        pid = int(lines[0].strip())
    except ValueError:
        return None, None
    mtime = None
    if len(lines) > 1:
        try:
            mtime = float(lines[1].strip())
        except ValueError:
            mtime = None
    return pid, mtime


def write_pidfile(path: Path, pid: int, script: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n{script.stat().st_mtime}\n", encoding="utf-8")


def streamer_should_restart(pidfile: Path, script: Path) -> bool:
    pid, recorded = read_pidfile(pidfile)
    if pid is None or not pid_is_stream(pid):
        return True
    try:
        current = script.stat().st_mtime
    except OSError:
        return False
    if recorded is None:
        return True
    return current > recorded + 1e-6


def _load_symbols(watchlist: Path | None, book: Path | None) -> list[str]:
    if watchlist and watchlist.exists():
        return [item.symbol for item in load_watchlist(watchlist)]
    if book and book.exists():
        from trade_book import enabled_symbols, load_book
        names = enabled_symbols(load_book(book))
        if names:
            return names
    raise SystemExit("error=watchlist is empty")


def _connect(symbols: list[str]):
    from websockets.sync.client import connect

    ws = connect(STREAM_URL, open_timeout=15, close_timeout=5, legacy=True)
    equities = equity_symbols(symbols)
    if equities:
        ws.send(json.dumps({"subscribe": equities}))
    return ws


def _rh_interval(symbols: list[str], now: datetime) -> int:
    if any(classify_symbol(s).asset_class == "crypto" for s in symbols):
        return RH_OPEN_SECONDS
    if session_at(now) == "closed":
        return RH_CLOSED_SECONDS
    return RH_OPEN_SECONDS


def run_stream(
    *,
    watchlist: Path | None,
    book: Path,
    log_path: Path,
    pidfile: Path,
) -> int:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    lock_path = log_path.with_suffix(".lock")
    lock = None
    deadline = time.monotonic() + LOCK_RETRY_SECONDS
    while lock is None:
        try:
            lock = acquire_poller_lock(lock_path)
        except Exception as exc:
            if time.monotonic() >= deadline:
                print(f"price_stream lock: {exc}", file=sys.stderr)
                return 1
            time.sleep(0.5)
    write_pidfile(pidfile, os.getpid(), Path(__file__).resolve())
    print(f"price_stream pid={os.getpid()} book={book}", flush=True)
    ws = None
    writer = StreamWriter(book, log_path)
    last_rh = 0.0
    last_subscribe = 0.0
    symbols = _load_symbols(watchlist, book)
    try:
        while True:
            now = datetime.now(NY)
            try:
                symbols = _load_symbols(watchlist, book)
            except SystemExit:
                symbols = symbols
            if time.monotonic() - last_rh >= _rh_interval(symbols, now) or last_rh == 0.0:
                try:
                    writer.note_robinhood(robinhood_refresh(symbols, now=now))
                    last_rh = time.monotonic()
                except Exception as exc:
                    print(f"robinhood_refresh error={exc}", flush=True)
                    if last_rh == 0.0:
                        last_rh = time.monotonic()
                writer.flush(symbols, now=now, force_book=True, force_jsonl=True)
            if ws is None:
                try:
                    ws = _connect(symbols)
                    last_subscribe = time.monotonic()
                    print(f"subscribed {','.join(equity_symbols(symbols))}", flush=True)
                except Exception as exc:
                    print(f"stream_connect error={exc}", flush=True)
                    time.sleep(SOCKET_RETRY_SECONDS)
                    continue
            if time.monotonic() - last_subscribe >= 15:
                equities = equity_symbols(symbols)
                if equities:
                    try:
                        ws.send(json.dumps({"subscribe": equities}))
                        last_subscribe = time.monotonic()
                    except Exception as exc:
                        print(f"stream_resubscribe error={exc}", flush=True)
                        ws = None
                        continue
            try:
                raw = ws.recv(timeout=1.0)
            except TimeoutError:
                writer.flush(symbols, now=datetime.now(NY))
                continue
            except Exception as exc:
                print(f"stream_recv error={exc}", flush=True)
                try:
                    ws.close()
                except Exception:
                    pass
                ws = None
                time.sleep(SOCKET_RETRY_SECONDS)
                continue
            tick = decode_message(raw)
            if not tick:
                continue
            symbol = str(tick.get("id") or "").upper()
            if symbol not in set(equity_symbols(symbols)):
                continue
            now = datetime.now(NY)
            snap = snapshot_from_stream_tick(tick, now=now, symbol=symbol)
            if snap is None:
                continue
            changed = writer.note_stream(snap)
            writer.flush(symbols, now=now, force_book=changed, force_jsonl=changed)
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        try:
            if pidfile.exists() and read_pidfile(pidfile)[0] == os.getpid():
                pidfile.unlink()
        except OSError:
            pass
        if lock is not None:
            lock.release()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stream last prints into the trade book.")
    parser.add_argument("--watchlist", default="data/watchlist.txt")
    parser.add_argument("--book", default="desk/trade_book.json")
    parser.add_argument("--log", default="data/quotes.jsonl")
    parser.add_argument("--pidfile", default="data/price_stream.pid")
    args = parser.parse_args(argv)
    watchlist = Path(args.watchlist)
    return run_stream(
        watchlist=watchlist if watchlist.exists() else None,
        book=Path(args.book),
        log_path=Path(args.log),
        pidfile=Path(args.pidfile),
    )


if __name__ == "__main__":
    raise SystemExit(main())