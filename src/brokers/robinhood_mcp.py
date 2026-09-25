"""Thin adapter for Hermes MCP Robinhood (OAuth).
Reuses existing Hermes MCP client + /opt/data/mcp-tokens/.
Calls only the 4 read tools. Never write/trade tools.
"""

from __future__ import annotations
from typing import Any

# In full impl, this would use the Hermes MCP OAuth client loaded from tokens.
# For v1 live, the CLI drives the MCP calls via the available tool system.

def fetch_wallet(account_number: str = "547525758") -> dict[str, Any]:
    """Fetch only the allowed read tools for the agentic account.
    Returns raw dict with keys matching the sample structure for normalize().
    """
    # The actual MCP calls are performed by the caller (CLI in live mode)
    # or by Hermes when the adapter is invoked inside the agent.
    # This thin layer just documents the contract.
    return {
        "account_number": account_number,
        "tools_used": ["get_accounts", "get_portfolio", "get_equity_positions", "get_option_positions"],
    }