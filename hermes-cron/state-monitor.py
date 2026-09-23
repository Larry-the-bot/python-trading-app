"""No-LLM 1-minute latched-state gate.

Prints a deterministic armed set from desk/trade_book.json (sorted
symbol/state/instrument only). `[]` when nothing is buy_ready/sell_ready.
Cron `monitor=` wakes the executor agent only when this payload changes.
Does not fetch quotes, does not re-test last vs levels, does not fill.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

APP = Path("/opt/data/workspace/trading-python-app")
PYTHON = APP / ".venv/bin/python"
MONITOR = APP / "src/state_monitor.py"
BOOK = APP / "desk/trade_book.json"


def main() -> int:
    cmd = [
        str(PYTHON),
        str(MONITOR),
        "--once",
        "--book",
        str(BOOK),
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(APP),
        capture_output=True,
        text=True,
        timeout=30,
    )
    out = (proc.stdout or "").rstrip("\n")
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        print(err or out or f"state-monitor exit={proc.returncode}", file=sys.stderr)
        return proc.returncode
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
