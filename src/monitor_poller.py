"""Read-only 1-minute health check of the price poller JSONL feed.

Does not fetch quotes and does not take the poller lock. Run beside
`src/price_monitor.py`, not instead of it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
DEFAULT_LOG = "data/quotes.jsonl"
DEFAULT_INTERVAL = 60
DEFAULT_MAX_AGE = 90
CLOSED_HEARTBEAT_MAX_AGE = 990


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


def latest_snapshots(path: str | Path) -> dict[str, dict[str, Any]]:
    log_path = Path(path)
    latest: dict[str, dict[str, Any]] = {}
    if not log_path.exists():
        return latest
    with log_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            symbol = row.get("symbol")
            if symbol:
                latest[str(symbol)] = row
    return latest


def latest_cycle(latest: dict[str, dict[str, Any]], *, slop_seconds: float = 1.0) -> dict[str, dict[str, Any]]:
    times: list[datetime] = []
    parsed: dict[str, datetime] = {}
    for symbol, row in latest.items():
        fetched = _parse_ts(row.get("fetched_at"))
        if fetched is None:
            continue
        parsed[symbol] = fetched
        times.append(fetched)
    if not times:
        return {}
    newest = max(times)
    return {
        symbol: latest[symbol]
        for symbol, fetched in parsed.items()
        if abs((newest - fetched).total_seconds()) <= slop_seconds
    }


def lag_limit_for_cycle(latest: dict[str, dict[str, Any]], max_age: float | None = None) -> float:
    if max_age is not None:
        return float(max_age)
    if not latest:
        return float(DEFAULT_MAX_AGE)
    sessions = {row.get("session") for row in latest.values()}
    classes = {row.get("asset_class") for row in latest.values()}
    equity_only_closed = sessions <= {"closed"} and "crypto" not in classes
    if equity_only_closed:
        return float(CLOSED_HEARTBEAT_MAX_AGE)
    return float(DEFAULT_MAX_AGE)


def health_from_log(
    path: str | Path,
    *,
    now: datetime,
    max_age: float | None = None,
) -> dict[str, Any]:
    log_path = Path(path)
    empty = {
        "status": "POLLER_DOWN",
        "age_seconds": None,
        "n_symbols": 0,
        "n_ok": 0,
        "n_stale": 0,
        "n_error": 0,
        "symbols": {},
    }
    if not log_path.exists():
        return empty
    latest = latest_cycle(latest_snapshots(log_path))
    if not latest:
        return empty

    limit = lag_limit_for_cycle(latest, max_age)
    symbols: dict[str, dict[str, Any]] = {}
    ages: list[float] = []
    n_ok = n_stale = n_error = 0
    for symbol, row in latest.items():
        fetched = _parse_ts(row.get("fetched_at"))
        age = (now - fetched.astimezone(now.tzinfo or NY)).total_seconds() if fetched else None
        if age is not None:
            ages.append(age)
        ok = age is not None and age <= limit and not row.get("error")
        if row.get("error"):
            n_error += 1
        if row.get("stale"):
            n_stale += 1
        if ok:
            n_ok += 1
        symbols[symbol] = {
            "ok": ok,
            "age_seconds": age,
            "stale": bool(row.get("stale")),
            "error": row.get("error"),
            "price": row.get("price"),
            "session": row.get("session"),
            "asset_class": row.get("asset_class"),
        }

    oldest = max(ages) if ages else None
    status = "POLLER_OK" if oldest is not None and oldest <= limit else "POLLER_LAG"
    return {
        "status": status,
        "age_seconds": oldest,
        "n_symbols": len(latest),
        "n_ok": n_ok,
        "n_stale": n_stale,
        "n_error": n_error,
        "symbols": symbols,
    }


def summarize_health(report: dict[str, Any]) -> str:
    age = report.get("age_seconds")
    age_s = "" if age is None else f"{age:.1f}"
    return (
        f"{report.get('status')} age_s={age_s} symbols={report.get('n_symbols')} "
        f"ok={report.get('n_ok')} stale={report.get('n_stale')} error={report.get('n_error')}"
    )


def format_symbol_line(symbol: str, info: dict[str, Any]) -> str:
    age = info.get("age_seconds")
    age_s = "" if age is None else f"{age:.1f}"
    return (
        f"symbol={symbol} asset_class={info.get('asset_class') or ''} "
        f"price={info.get('price')} session={info.get('session') or ''} "
        f"stale={'true' if info.get('stale') else 'false'} "
        f"ok={'true' if info.get('ok') else 'false'} age_s={age_s} "
        f"error={info.get('error') or ''}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Monitor price_monitor.py JSONL output every minute (read-only)."
    )
    parser.add_argument("--log", default=DEFAULT_LOG, help="JSONL path written by the poller")
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        help="seconds between health checks (floor 60)",
    )
    parser.add_argument(
        "--max-age",
        type=float,
        default=None,
        help="override lag SLA in seconds (default: 90 live, 990 for equity-only closed heartbeat)",
    )
    parser.add_argument("--once", action="store_true", help="one health check then exit")
    parser.add_argument("--cycles", type=int, default=0, help="stop after N checks (0 = forever)")
    args = parser.parse_args(argv)

    interval = max(int(args.interval), DEFAULT_INTERVAL)
    cycles = 0
    while True:
        now = datetime.now(NY)
        report = health_from_log(args.log, now=now, max_age=args.max_age)
        print(summarize_health(report), flush=True)
        for symbol, info in report.get("symbols", {}).items():
            print(format_symbol_line(symbol, info), flush=True)
        cycles += 1
        if args.once or (args.cycles and cycles >= args.cycles):
            return 0 if report.get("status") == "POLLER_OK" else 1
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
