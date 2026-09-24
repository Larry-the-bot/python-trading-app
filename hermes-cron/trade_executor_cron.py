#!/usr/bin/env python3
"""Cron script wrapper for trade_executor --once (dry-run safe default).

Runs from the trading app workspace, activates venv implicitly via full path,
executes one tick of the executor, prints summary.
Intended for no_agent cron job.
"""
import subprocess
import sys
from pathlib import Path

WORKSPACE = Path("/opt/data/workspace/trading-python-app")
VENV_PYTHON = WORKSPACE / ".venv/bin/python"
EXECUTOR = WORKSPACE / "src/trade_executor.py"
BOOK = WORKSPACE / "desk/trade_book.json"

def main():
    if not VENV_PYTHON.exists():
        print("ERROR: venv python not found", file=sys.stderr)
        return 1
    cmd = [
        str(VENV_PYTHON),
        str(EXECUTOR),
        "--once",
        "--dry-run",
        "--book",
        str(BOOK),
    ]
    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=WORKSPACE)
    print("STDOUT:", result.stdout)
    if result.stderr:
        print("STDERR:", result.stderr, file=sys.stderr)
    print("Return code:", result.returncode)
    return result.returncode

if __name__ == "__main__":
    sys.exit(main())
