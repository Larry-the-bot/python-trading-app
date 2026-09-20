"""Last-price poller for a 10-name equity/crypto watchlist.

Observe only. Robinhood public quotes are the live pre/post (and 24/7 crypto)
feed; Yahoo 1-minute bars are RTH equity OHLC/volume only.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

DEFAULT_WATCHLIST = [
    "SPY",
    "QQQ",
    "IWM",
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "META",
    "GOOGL",
    "TLT",
]

MAX_WATCHLIST = 10

# Bare tickers treated as crypto. Homonyms of US equities (UNI, W, COMP, …)
# must be written as UNI-USD / W-USD.
CRYPTO_SHORTCUTS = frozenset(
    {
        "BTC",
        "ETH",
        "SOL",
        "DOGE",
        "XRP",
        "ADA",
        "AVAX",
        "DOT",
        "LINK",
        "LTC",
        "BCH",
        "SHIB",
        "PEPE",
        "BONK",
        "ATOM",
        "XLM",
        "ETC",
        "AAVE",
        "ARB",
        "SUI",
        "APT",
        "FIL",
        "HBAR",
        "RENDER",
        "INJ",
        "LDO",
        "CRV",
        "GRT",
        "SNX",
        "ZEC",
        "XTZ",
        "ALGO",
        "TRUMP",
        "WIF",
        "PENGU",
        "FLOKI",
    }
)

ROBINHOOD_QUOTES_URL = "https://api.robinhood.com/quotes/"
ROBINHOOD_FOREX_QUOTES_URL = "https://api.robinhood.com/marketdata/forex/quotes/"
STALE_SECONDS = 120
OPEN_INTERVAL_SECONDS = 60
CLOSED_INTERVAL_SECONDS = 900
FAILURES_BEFORE_DATA_BAD = 3
HTTP_TIMEOUT_SECONDS = 10

HttpGet = Callable[[str], Any]
Download = Callable[..., Any]


class PollerLocked(RuntimeError):
    """Another price poller already holds the quotes lock."""


@dataclass(frozen=True)
class WatchSymbol:
    requested: str
    symbol: str
    rh_symbol: str
    asset_class: str


@dataclass
class _Lock:
    fd: int
    path: Path

    def release(self) -> None:
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


def open_poll_interval() -> int:
    return OPEN_INTERVAL_SECONDS


def closed_poll_interval() -> int:
    return CLOSED_INTERVAL_SECONDS


def stale_after() -> int:
    return STALE_SECONDS


def interval_for_session(
    session: str,
    requested: int | None = None,
    has_crypto: bool = False,
) -> int:
    if has_crypto:
        floor = OPEN_INTERVAL_SECONDS
    elif session == "closed":
        floor = CLOSED_INTERVAL_SECONDS
    else:
        floor = OPEN_INTERVAL_SECONDS
    if requested is None:
        return floor
    return max(int(requested), floor)


def outage_flag(consecutive_failures: int) -> str | None:
    if consecutive_failures >= FAILURES_BEFORE_DATA_BAD:
        return "DATA_BAD"
    return None


def classify_symbol(raw: str) -> WatchSymbol:
    requested = str(raw).strip()
    token = requested.upper().replace(" ", "").replace("/", "-")
    if not token:
        raise ValueError("symbol is required")
    if token.endswith("-USD") and len(token) > 4:
        base = token[:-4]
        return WatchSymbol(requested, f"{base}-USD", f"{base}USD", "crypto")
    if token.endswith("USD") and len(token) > 3:
        base = token[:-3]
        if base.isalpha() or base.isalnum():
            return WatchSymbol(requested, f"{base}-USD", f"{base}USD", "crypto")
    if token in CRYPTO_SHORTCUTS:
        return WatchSymbol(requested, f"{token}-USD", f"{token}USD", "crypto")
    return WatchSymbol(requested, token, token, "equity")


def normalize_watchlist(symbols: Sequence[str]) -> list[WatchSymbol]:
    cleaned = [str(s).strip() for s in symbols if str(s).strip() and not str(s).strip().startswith("#")]
    if len(cleaned) > MAX_WATCHLIST:
        raise ValueError(f"watchlist is capped at {MAX_WATCHLIST} symbols")
    if not cleaned:
        raise ValueError("watchlist is empty")
    out: list[WatchSymbol] = []
    seen: set[str] = set()
    for raw in cleaned:
        item = classify_symbol(raw)
        if item.symbol in seen:
            continue
        seen.add(item.symbol)
        out.append(item)
    return out


def load_watchlist(path: str | Path) -> list[WatchSymbol]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return normalize_watchlist(lines)


def session_at(now: datetime | None = None) -> str:
    """Return pre | regular | post | closed in America/New_York."""
    if now is None:
        now = datetime.now(NY)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=NY)
    else:
        now = now.astimezone(NY)
    if now.weekday() >= 5:
        return "closed"
    minutes = now.hour * 60 + now.minute
    if 4 * 60 <= minutes < 9 * 60 + 30:
        return "pre"
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return "regular"
    if 16 * 60 <= minutes < 20 * 60:
        return "post"
    return "closed"


def should_poll_yahoo(session: str, source: str) -> bool:
    if source == "yahoo":
        return True
    if source == "both":
        return session == "regular"
    return False


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=NY)
        return value
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_stale(*, session: str, updated_at: datetime | None, now: datetime) -> bool:
    if session == "closed":
        return True
    if updated_at is None:
        return True
    age = (now - updated_at.astimezone(now.tzinfo or NY)).total_seconds()
    return age > STALE_SECONDS


def snapshot_from_error(
    symbol: str,
    *,
    error: str,
    now: datetime,
    source: str,
    fetched_at: datetime | None = None,
    asset_class: str = "equity",
) -> dict[str, Any]:
    fetched = fetched_at or now
    row: dict[str, Any] = {
        "symbol": symbol,
        "asset_class": asset_class,
        "price": None,
        "bar_time": None,
        "updated_at": None,
        "fetched_at": fetched.isoformat(),
        "source": source,
        "session": "crypto" if asset_class == "crypto" else session_at(now),
        "stale": True,
        "error": error,
    }
    if source == "robinhood":
        row.update(bid=None, ask=None, last_regular=None, last_extended=None)
        if asset_class == "crypto":
            row.update(open=None, high=None, low=None, volume=None)
    else:
        row.update(open=None, high=None, low=None, volume=None)
    return row


def _updated_at_text(updated_raw: Any) -> str | None:
    if isinstance(updated_raw, datetime):
        return updated_raw.isoformat()
    if updated_raw:
        return str(updated_raw)
    return None


def parse_robinhood_quote(
    quote: dict[str, Any],
    *,
    now: datetime,
    fetched_at: datetime,
) -> dict[str, Any]:
    symbol = str(quote.get("symbol") or "").upper()
    last_regular = _to_float(quote.get("last_trade_price"))
    last_extended = _to_float(quote.get("last_extended_hours_trade_price"))
    price = last_extended if last_extended is not None else last_regular
    updated_raw = quote.get("updated_at")
    updated_dt = _parse_ts(updated_raw)
    session = session_at(now)
    error = None if price is not None else "no_data"
    stale = True if error else _is_stale(session=session, updated_at=updated_dt, now=now)
    updated_at = _updated_at_text(updated_raw)
    return {
        "symbol": symbol,
        "asset_class": "equity",
        "price": price,
        "bar_time": updated_at,
        "updated_at": updated_at,
        "fetched_at": fetched_at.isoformat(),
        "source": "robinhood",
        "session": session,
        "bid": _to_float(quote.get("bid_price")),
        "ask": _to_float(quote.get("ask_price")),
        "last_regular": last_regular,
        "last_extended": last_extended,
        "stale": stale,
        "error": error,
    }


def parse_robinhood_crypto_quote(
    quote: dict[str, Any],
    *,
    now: datetime,
    fetched_at: datetime,
    display_symbol: str | None = None,
) -> dict[str, Any]:
    rh = str(quote.get("symbol") or "").upper()
    base = rh[:-3] if rh.endswith("USD") and len(rh) > 3 else rh
    symbol = display_symbol or f"{base}-USD"
    bid = _to_float(quote.get("bid_price"))
    ask = _to_float(quote.get("ask_price"))
    price = _to_float(quote.get("mark_price"))
    updated_raw = quote.get("updated_at")
    updated_dt = _parse_ts(updated_raw)
    error = None if price is not None else "no_data"
    stale = True if error else _is_stale(session="crypto", updated_at=updated_dt, now=now)
    updated_at = _updated_at_text(updated_raw)
    return {
        "symbol": symbol,
        "asset_class": "crypto",
        "price": price,
        "bar_time": updated_at,
        "updated_at": updated_at,
        "fetched_at": fetched_at.isoformat(),
        "source": "robinhood",
        "session": "crypto",
        "bid": bid,
        "ask": ask,
        "last_regular": None,
        "last_extended": None,
        "open": _to_float(quote.get("open_price")),
        "high": _to_float(quote.get("high_price")),
        "low": _to_float(quote.get("low_price")),
        "volume": _to_float(quote.get("volume")),
        "stale": stale,
        "error": error,
    }


def _error_reason(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, urllib.error.HTTPError):
        return f"http_{exc.code}"
    if isinstance(exc, urllib.error.URLError):
        reason = str(getattr(exc, "reason", exc) or exc)
        lowered = reason.lower()
        if "timed out" in lowered or "timeout" in lowered:
            return "timeout"
        return f"http:{reason}"[:80]
    return str(exc)[:80]


def default_http_get(url: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "trading-python-app-price-monitor/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode())


def _extract_results(payload: Any) -> list[Any]:
    if isinstance(payload, dict):
        return list(payload.get("results") or [])
    if isinstance(payload, list):
        return payload
    return []


def _fetch_batch(
    url: str,
    getter: HttpGet,
    items: list[WatchSymbol],
    by_rh: dict[str, tuple[str, dict[str, Any]]],
    errors_by_rh: dict[str, str],
    kind: str,
) -> None:
    try:
        payload = getter(url)
    except Exception as exc:
        reason = _error_reason(exc)
        for item in items:
            errors_by_rh[item.rh_symbol] = reason
        return
    for quote in _extract_results(payload):
        if not isinstance(quote, dict):
            continue
        sym = str(quote.get("symbol") or "").upper()
        if sym:
            by_rh[sym] = (kind, quote)


def poll_robinhood(
    symbols: list[str],
    *,
    now: datetime | None = None,
    http_get: HttpGet | None = None,
) -> list[dict[str, Any]]:
    now = now or datetime.now(NY)
    fetched_at = now
    items = normalize_watchlist(symbols)
    getter = http_get or default_http_get
    equities = [item for item in items if item.asset_class == "equity"]
    cryptos = [item for item in items if item.asset_class == "crypto"]
    by_rh: dict[str, tuple[str, dict[str, Any]]] = {}
    errors_by_rh: dict[str, str] = {}

    if equities:
        url = ROBINHOOD_QUOTES_URL + "?symbols=" + ",".join(item.rh_symbol for item in equities)
        _fetch_batch(url, getter, equities, by_rh, errors_by_rh, "equity")
    if cryptos:
        url = ROBINHOOD_FOREX_QUOTES_URL + "?symbols=" + ",".join(item.rh_symbol for item in cryptos)
        _fetch_batch(url, getter, cryptos, by_rh, errors_by_rh, "crypto")

    snaps: list[dict[str, Any]] = []
    for item in items:
        if item.rh_symbol in errors_by_rh:
            snaps.append(
                snapshot_from_error(
                    item.symbol,
                    error=errors_by_rh[item.rh_symbol],
                    now=now,
                    source="robinhood",
                    fetched_at=fetched_at,
                    asset_class=item.asset_class,
                )
            )
            continue
        packed = by_rh.get(item.rh_symbol)
        if packed is None:
            snaps.append(
                snapshot_from_error(
                    item.symbol,
                    error="no_data",
                    now=now,
                    source="robinhood",
                    fetched_at=fetched_at,
                    asset_class=item.asset_class,
                )
            )
            continue
        kind, quote = packed
        if kind == "crypto":
            snaps.append(
                parse_robinhood_crypto_quote(
                    quote, now=now, fetched_at=fetched_at, display_symbol=item.symbol
                )
            )
        else:
            snap = parse_robinhood_quote(quote, now=now, fetched_at=fetched_at)
            snap["symbol"] = item.symbol
            snap["asset_class"] = "equity"
            snaps.append(snap)
    return snaps


def _column(frame: Any, name: str) -> Any:
    for col in frame.columns:
        label = col[0] if isinstance(col, tuple) else col
        if str(label).lower() == name.lower():
            return frame[col]
    raise KeyError(name)


def _frame_for_symbol(data: Any, symbol: str, *, n_symbols: int) -> Any:
    if data is None or getattr(data, "empty", True):
        return None
    columns = getattr(data, "columns", None)
    if columns is not None and getattr(columns, "nlevels", 1) > 1:
        level0 = set(map(str, columns.get_level_values(0)))
        level1 = set(map(str, columns.get_level_values(1)))
        if symbol in level0:
            return data[symbol]
        if symbol in level1:
            return data.xs(symbol, axis=1, level=1)
        return None
    if n_symbols == 1:
        return data
    if columns is not None and symbol in columns:
        return data[symbol]
    return None


def parse_yahoo_bar(
    symbol: str,
    frame: Any,
    *,
    now: datetime,
    fetched_at: datetime,
) -> dict[str, Any]:
    session = session_at(now)
    base: dict[str, Any] = {
        "symbol": symbol,
        "asset_class": "equity",
        "price": None,
        "bar_time": None,
        "updated_at": None,
        "fetched_at": fetched_at.isoformat(),
        "source": "yahoo",
        "session": session,
        "open": None,
        "high": None,
        "low": None,
        "volume": None,
        "stale": True,
        "error": None,
    }
    if frame is None or getattr(frame, "empty", True):
        base["error"] = "no_data"
        return base
    try:
        close = _column(frame, "Close")
    except KeyError:
        base["error"] = "no_data"
        return base
    valid = close.dropna()
    if len(valid) == 0:
        base["error"] = "no_data"
        return base
    last_idx = valid.index[-1]

    def cell(name: str) -> float | None:
        try:
            series = _column(frame, name)
            value = series.loc[last_idx]
        except Exception:
            return None
        return _to_float(value)

    price = _to_float(valid.iloc[-1])
    bar_dt = _parse_ts(last_idx.to_pydatetime() if hasattr(last_idx, "to_pydatetime") else last_idx)
    bar_time = bar_dt.isoformat() if bar_dt is not None else str(last_idx)
    volume = cell("Volume")
    base.update(
        price=price,
        bar_time=bar_time,
        updated_at=bar_time,
        open=cell("Open"),
        high=cell("High"),
        low=cell("Low"),
        volume=int(volume) if volume is not None else None,
        stale=True if session != "regular" else _is_stale(session=session, updated_at=bar_dt, now=now),
        error=None if price is not None else "no_data",
    )
    return base


def _yf_download(*args: Any, **kwargs: Any) -> Any:
    import yfinance as yf

    return yf.download(*args, **kwargs)


def poll_yahoo(
    symbols: list[str],
    *,
    now: datetime | None = None,
    download: Download | None = None,
) -> list[dict[str, Any]]:
    now = now or datetime.now(NY)
    fetched_at = now
    items = [classify_symbol(s) for s in symbols if str(s).strip()]
    items = [item for item in items if item.asset_class == "equity"]
    tickers = [item.rh_symbol for item in items]
    if not tickers:
        return []
    downloader = download or _yf_download
    try:
        data = downloader(
            tickers,
            period="1d",
            interval="1m",
            auto_adjust=False,
            progress=False,
            threads=False,
        )
    except Exception as exc:
        return [
            snapshot_from_error(
                item.symbol,
                error=_error_reason(exc),
                now=now,
                source="yahoo",
                fetched_at=fetched_at,
            )
            for item in items
        ]
    return [
        parse_yahoo_bar(
            item.symbol,
            _frame_for_symbol(data, item.rh_symbol, n_symbols=len(tickers)),
            now=now,
            fetched_at=fetched_at,
        )
        for item in items
    ]


def format_line(snap: dict[str, Any]) -> str:
    ext = snap.get("last_extended")
    ordered = [
        ("symbol", snap.get("symbol")),
        ("asset_class", snap.get("asset_class")),
        ("price", snap.get("price")),
        ("ext", ext),
        ("bid", snap.get("bid")),
        ("ask", snap.get("ask")),
        ("last_regular", snap.get("last_regular")),
        ("last_extended", snap.get("last_extended")),
        ("open", snap.get("open")),
        ("high", snap.get("high")),
        ("low", snap.get("low")),
        ("volume", snap.get("volume")),
        ("bar_time", snap.get("bar_time")),
        ("updated_at", snap.get("updated_at")),
        ("fetched_at", snap.get("fetched_at")),
        ("source", snap.get("source")),
        ("session", snap.get("session")),
        ("stale", snap.get("stale")),
        ("error", snap.get("error")),
    ]
    parts: list[str] = []
    seen = set()
    for key, value in ordered:
        if key in snap or key == "ext":
            if key != "ext" and key not in snap:
                continue
            if key in seen:
                continue
            seen.add(key)
            if value is None:
                rendered = ""
            elif isinstance(value, bool):
                rendered = "true" if value else "false"
            else:
                rendered = str(value)
            parts.append(f"{key}={rendered}")
    return " ".join(parts)


def snapshots_to_jsonl(path: str | Path, snaps: list[dict[str, Any]]) -> None:
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        for snap in snaps:
            handle.write(json.dumps(snap, default=str) + "\n")


def acquire_poller_lock(lock_path: str | Path) -> _Lock:
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise PollerLocked(f"price poller already running ({path})") from exc
    return _Lock(fd=fd, path=path)


def _batch_failed(snaps: list[dict[str, Any]]) -> bool:
    return bool(snaps) and all(s.get("error") for s in snaps)


def poll_once(
    symbols: list[str],
    *,
    source: str,
    now: datetime | None = None,
    http_get: HttpGet | None = None,
    download: Download | None = None,
) -> list[dict[str, Any]]:
    now = now or datetime.now(NY)
    session = session_at(now)
    items = normalize_watchlist(symbols)
    records: list[dict[str, Any]] = []
    if source in ("robinhood", "both"):
        records.extend(poll_robinhood([item.requested for item in items], now=now, http_get=http_get))
    if source == "yahoo" or (source == "both" and should_poll_yahoo(session, source)):
        equities = [item.rh_symbol for item in items if item.asset_class == "equity"]
        if equities:
            records.extend(poll_yahoo(equities, now=now, download=download))
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watchlist last-price poller (observe only).")
    parser.add_argument(
        "--source",
        choices=("robinhood", "yahoo", "both"),
        default="robinhood",
        help="robinhood = live pre/post last trade + 24/7 crypto; yahoo = RTH 1m OHLC; both = compare",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=OPEN_INTERVAL_SECONDS,
        help="requested seconds; open floor is 60, closed equity floor is 900, crypto stays 60",
    )
    parser.add_argument("--log", default="data/quotes.jsonl", help="JSONL handoff path")
    parser.add_argument("--watchlist", help="text file, one symbol per line (max 10)")
    parser.add_argument("--once", action="store_true", help="one snapshot then exit")
    parser.add_argument(
        "symbols",
        nargs="*",
        help="up to 10 equity or crypto symbols; default is the desk equity list",
    )
    args = parser.parse_args(argv)

    if args.symbols and args.watchlist:
        print("error=pass symbols or --watchlist, not both", file=sys.stderr)
        return 2

    try:
        if args.symbols:
            items = normalize_watchlist(args.symbols)
        elif args.watchlist:
            items = load_watchlist(args.watchlist)
        else:
            items = normalize_watchlist(DEFAULT_WATCHLIST)
    except (ValueError, OSError) as exc:
        print(f"error={exc}", file=sys.stderr)
        return 2

    symbols = [item.requested for item in items]
    has_crypto = any(item.asset_class == "crypto" for item in items)

    log_path = Path(args.log)
    lock_path = log_path.with_suffix(".lock")
    try:
        lock = acquire_poller_lock(lock_path)
    except PollerLocked as exc:
        print(str(exc), file=sys.stderr)
        return 1

    consecutive_failures = 0
    try:
        while True:
            now = datetime.now(NY)
            session = session_at(now)
            records = poll_once(symbols, source=args.source, now=now)
            live_source = "robinhood" if args.source in ("robinhood", "both") else "yahoo"
            live_rows = [r for r in records if r.get("source") == live_source]
            if _batch_failed(live_rows):
                consecutive_failures += 1
            else:
                consecutive_failures = 0
            flag = outage_flag(consecutive_failures)
            if flag:
                print(f"{flag} consecutive_failures={consecutive_failures}", flush=True)
            snapshots_to_jsonl(log_path, records)
            for row in records:
                print(format_line(row), flush=True)
            if args.once:
                return 0
            time.sleep(interval_for_session(session, requested=args.interval, has_crypto=has_crypto))
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
