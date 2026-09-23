"""Pluggable TradingAgent protocol for the executor.

DryRunAgent (default) simulates fills without broker calls.
RealAgent (selected via TRADE_AGENT=real or explicit class) performs
live limit orders against a broker (e.g. Robinhood options/crypto).

The executor never places orders directly; it only calls the agent after
should_act_* gates pass. This keeps the state machine (trade_book.py)
and poller pure observers.

Contract:
- buy(name, playbook, dry_run) → {"success": bool, "fill": dict or None, "error": str or None}
- sell(...) same shape
- fill dict must include at minimum: side, instrument, ts, price, contracts
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any


class TradingAgent(ABC):
    """Abstract base for buy/sell execution.

    Implementations must be idempotent on repeated calls for the same
    (symbol, instrument) within a short window and must never mutate the
    trade_book themselves — the executor owns the position/last_fill update.
    """

    @abstractmethod
    def buy(
        self,
        name: dict[str, Any],
        playbook: dict[str, Any],
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Place (or simulate) a limit buy for the core instrument.

        Returns a result dict with:
            success: bool
            fill: dict | None   # on success: side, instrument, ts, price, contracts, order_id?
            error: str | None
        """
        raise NotImplementedError

    @abstractmethod
    def sell(
        self,
        name: dict[str, Any],
        playbook: dict[str, Any],
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Place (or simulate) a limit sell for the exact same core instrument."""
        raise NotImplementedError


class DryRunAgent(TradingAgent):
    """Safe no-op agent used for --dry-run, tests, and initial validation.

    Always succeeds, fabricates a plausible fill at the current mid or
    buy_price. Never touches any external system.
    """

    def buy(
        self,
        name: dict[str, Any],
        playbook: dict[str, Any],
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        inst = name.get("instrument") or playbook.get("core") or {}
        price = name.get("current_price") or name.get("buy_price") or 0.0
        contracts = playbook.get("contracts", 1)
        return {
            "success": True,
            "fill": {
                "side": "buy",
                "instrument": inst,
                "ts": datetime.now(timezone.utc).isoformat(),
                "price": price,
                "contracts": contracts,
                "order_id": f"DRY-BUY-{name.get('symbol', 'UNK')}",
            },
            "error": None,
        }

    def sell(
        self,
        name: dict[str, Any],
        playbook: dict[str, Any],
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        inst = name.get("instrument") or playbook.get("core") or {}
        price = name.get("current_price") or name.get("sell_price") or 0.0
        contracts = playbook.get("contracts", 1)
        return {
            "success": True,
            "fill": {
                "side": "sell",
                "instrument": inst,
                "ts": datetime.now(timezone.utc).isoformat(),
                "price": price,
                "contracts": contracts,
                "order_id": f"DRY-SELL-{name.get('symbol', 'UNK')}",
            },
            "error": None,
        }


class RealAgent(TradingAgent):
    """Live broker adapter (stub for now).

    Selected when TRADE_AGENT=real or when an explicit RealAgent instance
    is passed. In production this would talk to Robinhood / other APIs
    using the instrument details and limit pricing rules from PLAYBOOK.md
    (mid-or-better, spread < 8%, etc.).

    TODO: implement broker client, order placement, fill polling.
    """

    def __init__(self, broker_client: Any | None = None):
        self.client = broker_client

    def buy(
        self,
        name: dict[str, Any],
        playbook: dict[str, Any],
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        # Placeholder — real impl would construct limit order here
        if dry_run:
            return DryRunAgent().buy(name, playbook, dry_run=True)
        # real order logic would go here
        return {
            "success": False,
            "fill": None,
            "error": "RealAgent not yet wired to broker",
        }

    def sell(
        self,
        name: dict[str, Any],
        playbook: dict[str, Any],
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        if dry_run:
            return DryRunAgent().sell(name, playbook, dry_run=True)
        return {
            "success": False,
            "fill": None,
            "error": "RealAgent not yet wired to broker",
        }


def get_agent(live: bool = False) -> TradingAgent:
    """Factory used by execute_one.

    Respects TRADE_AGENT env var or the live flag.
    """
    if live or os.getenv("TRADE_AGENT") == "real":
        return RealAgent()
    return DryRunAgent()