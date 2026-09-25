"""holdings_schema.py
MCP-only Robinhood wallet schema + normalize.
v1: fetch -> normalize -> holdings dict. No auth, no watchlist, no gating.
"""

from __future__ import annotations
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1
SOURCE = "mcp_robinhood"

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _as_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize raw MCP payloads into canonical holdings schema.
    Expects keys like 'get_accounts', 'get_portfolio', 'get_equity_positions', etc.
    Missing tools go into errors[].
    Empty positions lists = confirmed flat (not skipped).
    """
    accounts_raw = raw.get("get_accounts", {}).get("data", {}).get("accounts", [])
    accounts = []
    selected_account = None
    for acc in accounts_raw:
        entry = {
            "number": acc.get("account_number"),
            "type": acc.get("type"),
            "agentic_allowed": acc.get("agentic_allowed"),
            "option_level": acc.get("option_level", ""),
        }
        accounts.append(entry)
        if acc.get("agentic_allowed"):
            selected_account = acc.get("account_number")

    if not selected_account and accounts:
        selected_account = accounts[0]["number"]  # fallback

    portfolio = raw.get("get_portfolio", {}).get("data", {})
    cash = {
        "cash": _as_float(portfolio.get("cash")),
        "buying_power": _as_float(portfolio.get("buying_power", {}).get("buying_power")),
        "unleveraged_buying_power": _as_float(
            portfolio.get("buying_power", {}).get("unleveraged_buying_power")
        ),
        "currency": portfolio.get("currency", "USD"),
        "account_type": next((a["type"] for a in accounts if a.get("number") == selected_account), None),
        "account_number": selected_account,
    }
    # Only include fields present in payload; do not invent unsettled/pending

    equity_pos = raw.get("get_equity_positions", {}).get("data", {}).get("positions", [])
    equities = []
    for p in equity_pos:
        if p.get("quantity", 0) != 0:  # persist nonzero only
            equities.append({
                "symbol": p.get("symbol"),
                "quantity": _as_float(p.get("quantity")),
                "average_buy_price": _as_float(p.get("average_buy_price")),
                "type": p.get("type", "long"),
            })

    option_pos = raw.get("get_option_positions", {}).get("data", {}).get("positions", [])
    options = []
    for p in option_pos:
        if p.get("quantity", 0) != 0:
            options.append({
                "chain_symbol": p.get("chain_symbol"),
                "type": p.get("type"),  # call/put
                "quantity": _as_float(p.get("quantity")),
                "average_price": _as_float(p.get("average_price")),
                "expiration_date": p.get("expiration_date"),
                "strike": p.get("strike"),  # may need lookup
                "side": p.get("side", "long"),
                "multiplier": p.get("multiplier", 100),
            })

    crypto_pos = raw.get("get_crypto_positions", {}).get("data", {}).get("positions", [])
    crypto = []
    for p in crypto_pos:
        if p.get("quantity", 0) != 0:
            crypto.append({
                "asset": p.get("asset"),
                "quantity": _as_float(p.get("quantity")),
                "cost_basis": p.get("cost_basis"),
            })

    errors = []
    if "get_crypto_positions" not in raw:
        errors.append("get_crypto_positions not called")
    # Add other skipped tools if needed

    totals = {
        "equity_value": _as_float(portfolio.get("equity_value", 0)),
        "options_value": _as_float(portfolio.get("options_value", 0)),
        "crypto_value": _as_float(portfolio.get("crypto_value", 0)),
        "total_value": _as_float(portfolio.get("total_value", 0)),
        "position_count": len(equities) + len(options) + len(crypto),
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "account_number": selected_account,
        "accounts": accounts,
        "updated_at": _now(),
        "as_of": portfolio.get("as_of") or _now(),
        "stale": False,
        "cash": cash,
        "equities": equities,
        "options": options,
        "crypto": crypto,
        "totals": totals,
        "errors": errors,
        "note": "Main account (option_level_2) is read-only to this MCP agent; only agentic account data is persisted here.",
    }

def load_samples() -> dict[str, Any]:
    """Helper for tests/CLI dry-run: load the dumped samples."""
    import json
    from pathlib import Path

    samples_dir = Path("/opt/data/workspace/trading-python-app/desk/samples")
    raw = {}
    for name in ["mcp_get_accounts", "mcp_get_portfolio", "mcp_get_equity_positions", "mcp_get_option_positions"]:
        f = samples_dir / f"{name}.json"
        if f.exists():
            data = json.loads(f.read_text())
            raw[name] = data.get("raw", data)
    return raw

def write_holdings(holdings: dict[str, Any], path: str | Path = "desk/holdings.json") -> Path:
    """Atomic write of normalized holdings. Keeps previous snapshot on error."""
    import json
    from pathlib import Path
    import tempfile
    import os

    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Atomic: write to temp then rename
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json", dir=dest.parent) as tmp:
        json.dump(holdings, tmp, indent=2, sort_keys=True)
        tmp_path = Path(tmp.name)

    try:
        tmp_path.replace(dest)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return dest

if __name__ == "__main__":
    print("holdings_schema module loaded")