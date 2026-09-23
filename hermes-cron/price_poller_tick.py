#!/opt/data/workspace/trading-python-app/.venv/bin/python
"""No-LLM price-poller tick.

Keeps src/price_stream.py alive so desk/trade_book.json follows the public
quote stream. Silent when the streamer is healthy. Prints only start failures.
Does not run price_monitor.py --once — that REST snapshot would overwrite a
newer stream print with the Robinhood close.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

APP = Path("/opt/data/workspace/trading-python-app")
PYTHON = APP / ".venv/bin/python"
STREAMER = APP / "src/price_stream.py"
PIDFILE = APP / "data/price_stream.pid"
LOG = APP / "data/price_stream.log"
WATCHLIST = APP / "data/watchlist.txt"
BOOK = APP / "desk/trade_book.json"
JSONL = APP / "data/quotes.jsonl"

sys.path.insert(0, str(APP / "src"))
from price_stream import pid_is_stream, read_pidfile, streamer_should_restart  # noqa: E402


def _rotate_log() -> None:
    try:
        if LOG.exists() and LOG.stat().st_size > 1_000_000:
            LOG.write_text("", encoding="utf-8")
    except OSError:
        pass


def _stop(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not pid_is_stream(pid):
            return
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _start() -> int:
    _rotate_log()
    log = LOG.open("a", encoding="utf-8")
    cmd = [
        str(PYTHON),
        str(STREAMER),
        "--book",
        str(BOOK),
        "--log",
        str(JSONL),
        "--pidfile",
        str(PIDFILE),
    ]
    if WATCHLIST.exists():
        cmd.extend(["--watchlist", str(WATCHLIST)])
    proc = subprocess.Popen(
        cmd,
        cwd=str(APP),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        pid, _mtime = read_pidfile(PIDFILE)
        if pid and pid_is_stream(pid):
            return 0
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    tail = ""
    try:
        tail = LOG.read_text(encoding="utf-8")[-500:]
    except OSError:
        tail = ""
    print(tail.strip() or f"price_stream failed to start pid={proc.pid}")
    return 1


def main() -> int:
    if not STREAMER.exists():
        print(f"missing {STREAMER}")
        return 1
    pid, _mtime = read_pidfile(PIDFILE)
    if not streamer_should_restart(PIDFILE, STREAMER):
        return 0
    if pid and pid_is_stream(pid):
        _stop(pid)
    return _start()


if __name__ == "__main__":
    raise SystemExit(main())
