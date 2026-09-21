#!/usr/bin/env python3
"""
Robinhood MVP Trading App - CLI Prototype with Max Pain

Implements core MVP features using official Robinhood Crypto Trading API where possible,
yfinance for stock data (as proxy since no official stock API), mocks for portfolio, and
weekly options max pain calculation as a key trading condition (per user preference).

Supports authentication (via env for crypto), market data fetch, sample orders (crypto only, dry-run by default),
portfolio display, and maxpain command for options trading decisions.

Due to API limitations documented in research, full stock/options order placement requires Agentic MCP.
This prototype demonstrates the feasible parts, max pain integration, and provides clear extension points.

Usage: python rh_mvp_trader.py --help
"""

import os
import json
import datetime
import base64
import uuid
import logging
from typing import Any, Dict
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from datetime import date
import click
from dotenv import load_dotenv
import yfinance as yf
import requests
from nacl.signing import SigningKey

# Load env
load_dotenv()

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Config from env for Crypto API
API_KEY = os.getenv("RH_CRYPTO_API_KEY")
PRIVATE_KEY_B64 = os.getenv("RH_CRYPTO_PRIVATE_KEY")
BASE_URL = "https://trading.robinhood.com"
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"


class RobinhoodCryptoClient:
    """Official Robinhood Crypto Trading API client based on research sample."""
    
    def __init__(self):
        if not API_KEY or not PRIVATE_KEY_B64:
            logger.warning("RH_CRYPTO_API_KEY and RH_CRYPTO_PRIVATE_KEY not set. Crypto features limited to dry-run.")
        self.api_key = API_KEY
        self.private_key_b64 = PRIVATE_KEY_B64
    
    def _timestamp(self) -> int:
        return int(datetime.datetime.now(tz=datetime.timezone.utc).timestamp())
    
    def _headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        if not self.api_key or not self.private_key_b64:
            return {}
        ts = self._timestamp()
        msg = f"{self.api_key}{ts}{path}{method}{body}"
        signing_key = SigningKey(base64.b64decode(self.private_key_b64))
        signed = signing_key.sign(msg.encode("utf-8"))
        signature = base64.b64encode(signed.signature).decode("utf-8")
        return {
            "x-api-key": self.api_key,
            "x-timestamp": str(ts),
            "x-signature": signature,
        }
    
    def get_account(self) -> Dict:
        """Fetch crypto account info."""
        path = "/api/v1/crypto/trading/accounts/"
        headers = self._headers("GET", path)
        if not headers:
            return {"status": "dry_run", "message": "No credentials - mock account"}
        try:
            r = requests.get(BASE_URL + path, headers=headers, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"API error: {e}")
            return {"error": str(e), "status": "mock"}
    
    def place_market_buy(self, symbol: str, qty: str, dry_run: bool = True) -> Dict:
        """Place market buy order for crypto (with dry-run)."""
        if dry_run or DRY_RUN:
            logger.info(f"DRY RUN: Would buy {qty} of {symbol}")
            return {
                "id": str(uuid.uuid4()),
                "state": "dry_run_confirmed",
                "symbol": symbol,
                "side": "buy",
                "quantity": qty,
                "type": "market"
            }
        
        path = "/api/v1/crypto/trading/orders/"
        body_obj = {
            "client_order_id": str(uuid.uuid4()),
            "side": "buy",
            "type": "market",
            "symbol": symbol,
            "market_order_config": {"asset_quantity": qty},
        }
        body = json.dumps(body_obj)
        headers = self._headers("POST", path, body)
        
        try:
            r = requests.post(
                BASE_URL + path,
                headers=headers,
                json=body_obj,
                timeout=10,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Order error: {e}")
            return {"error": str(e)}


def get_stock_quote(ticker: str) -> Dict:
    """Fetch stock data using yfinance (proxy for market data)."""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        history = stock.history(period="5d")
        return {
            "symbol": ticker.upper(),
            "price": info.get("currentPrice") or info.get("regularMarketPrice"),
            "change": info.get("regularMarketChangePercent"),
            "volume": info.get("regularMarketVolume"),
            "bid": info.get("bid"),
            "ask": info.get("ask"),
            "last_updated": datetime.datetime.now().isoformat(),
            "source": "yfinance (proxy)"
        }
    except Exception as e:
        logger.error(f"Quote error for {ticker}: {e}")
        return {"symbol": ticker.upper(), "error": str(e), "source": "mock"}


def get_portfolio() -> Dict:
    """Mock portfolio for MVP (extend with real positions from API)."""
    return {
        "total_value": 12543.67,
        "todays_pnl": 234.56,
        "total_pnl": 1456.78,
        "positions": [
            {"symbol": "AAPL", "quantity": 10, "avg_cost": 185.2, "current_value": 2345.0, "unrealized_pnl": 234.5},
            {"symbol": "BTC", "quantity": 0.05, "avg_cost": 62000, "current_value": 3250.0, "unrealized_pnl": 150.0},
        ],
        "source": "mock (extend with get_account() + yfinance)",
        "note": "Use Agentic MCP or unofficial for full Robinhood positions"
    }


@dataclass(frozen=True)
class MaxPainResult:
    symbol: str
    expiration: str
    max_pain: float
    spot: float | None = None

    @property
    def above_max_pain(self) -> bool | None:
        if self.spot is None:
            return None
        return self.spot > self.max_pain

    @property
    def distance(self) -> float | None:
        if self.spot is None:
            return None
        return self.spot - self.max_pain


def compute_max_pain(
    call_oi: Mapping[float, float],
    put_oi: Mapping[float, float],
) -> float:
    """Return the strike that minimizes combined call+put intrinsic value.

    For each candidate expiry price P (each listed strike):
      pain(P) = Σ max(0, P - K) * call_OI[K]  +  Σ max(0, K - P) * put_OI[K]
    Max pain is argmin_P pain(P). Ties take the lowest strike.
    """
    strikes = sorted(set(call_oi) | set(put_oi))
    if not strikes:
        raise ValueError("cannot compute max pain with no strikes")

    min_pain: float | None = None
    max_pain_strike = strikes[0]
    for expiry_price in strikes:
        pain = 0.0
        for strike in strikes:
            call_interest = float(call_oi.get(strike, 0) or 0)
            put_interest = float(put_oi.get(strike, 0) or 0)
            pain += max(0.0, expiry_price - strike) * call_interest
            pain += max(0.0, strike - expiry_price) * put_interest
        if min_pain is None or pain < min_pain:
            min_pain = pain
            max_pain_strike = expiry_price
    return float(max_pain_strike)


def nearest_weekly_expiration(
    expirations: Sequence[str],
    as_of: date | None = None,
) -> str:
    """Return the current weekly expiry: nearest Friday on or after as_of.

    Falls back to the soonest listed expiration when no Friday is listed
    (monthlies-only names, or Thursday holiday weeklies).
    """
    as_of = as_of or date.today()
    future: list[tuple[str, date]] = []
    for raw in expirations:
        exp = raw if isinstance(raw, date) else date.fromisoformat(str(raw)[:10])
        if exp >= as_of:
            future.append((str(raw)[:10], exp))
    if not future:
        raise ValueError(f"no option expirations on or after {as_of.isoformat()}")
    fridays = [(raw, exp) for raw, exp in future if exp.weekday() == 4]
    pool = fridays or future
    return min(pool, key=lambda item: item[1])[0]


def _oi_by_strike(chain: Any) -> dict[float, float]:
    if chain is None:
        return {}
    empty = getattr(chain, "empty", None)
    if empty:
        return {}
    try:
        n = len(chain)
    except TypeError:
        return {}
    if n == 0:
        return {}

    strikes = chain["strike"]
    if hasattr(chain, "columns") and "openInterest" in chain.columns:
        raw_oi = chain["openInterest"]
        if hasattr(raw_oi, "fillna"):
            raw_oi = raw_oi.fillna(0)
    else:
        raw_oi = [0.0] * n

    out: dict[float, float] = {}
    for strike, interest in zip(strikes, raw_oi):
        try:
            key = float(strike)
        except (TypeError, ValueError):
            continue
        try:
            value = float(interest)
        except (TypeError, ValueError):
            value = 0.0
        if value != value:  # NaN
            value = 0.0
        out[key] = out.get(key, 0.0) + value
    return out


def _spot_price(ticker: Any) -> float | None:
    info = getattr(ticker, "fast_info", None)
    if info is None:
        return None
    for key in ("last_price", "lastPrice"):
        value = None
        try:
            if hasattr(info, "get"):
                value = info.get(key)
            if value is None:
                value = getattr(info, key, None)
        except Exception:
            value = None
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def get_weekly_max_pain(
    symbol: str,
    *,
    as_of: date | None = None,
    ticker: Any | None = None,
) -> MaxPainResult:
    """Fetch the weekly option chain via yfinance and return max pain.

    This fulfills the user's preference to use weekly options max pain as a trading condition.
    Max pain acts as a magnet for price; useful for deciding entries/exits on weekly options.
    """
    symbol = symbol.upper().strip()
    if not symbol:
        raise ValueError("symbol is required")
    client = ticker
    if client is None:
        client = yf.Ticker(symbol)
    expirations = list(client.options)
    expiration = nearest_weekly_expiration(expirations, as_of=as_of)
    chain = client.option_chain(expiration)
    max_pain = compute_max_pain(_oi_by_strike(chain.calls), _oi_by_strike(chain.puts))
    return MaxPainResult(
        symbol=symbol,
        expiration=expiration,
        max_pain=max_pain,
        spot=_spot_price(client),
    )


@click.group()
def cli():
    """Robinhood MVP Trader CLI with max pain trading condition."""
    pass


@cli.command()
def account():
    """Show account info (crypto)."""
    client = RobinhoodCryptoClient()
    acc = client.get_account()
    click.echo(json.dumps(acc, indent=2))


@cli.command()
@click.argument("ticker")
def quote(ticker):
    """Get real-time quote for ticker."""
    data = get_stock_quote(ticker)
    click.echo(json.dumps(data, indent=2))


@cli.command()
def portfolio():
    """Display portfolio overview."""
    port = get_portfolio()
    click.echo(json.dumps(port, indent=2))


@cli.command()
@click.argument("ticker", default="SPY")
def maxpain(ticker):
    """Compute weekly max pain for the ticker to use as trading condition."""
    try:
        result = get_weekly_max_pain(ticker)
        click.echo(f"Max Pain for {result.symbol} expiring {result.expiration}: ${result.max_pain:.2f}")
        if result.spot is not None:
            direction = "above" if result.above_max_pain else "below"
            click.echo(f"Current spot: ${result.spot:.2f} ({direction} max pain by ${abs(result.distance or 0):.2f})")
        click.echo("\nTrading insight: Price tends to gravitate toward max pain at expiration. ")
        click.echo("Consider this for weekly options entries (e.g. sell premium near max pain).")
    except Exception as e:
        click.echo(f"Error: {e}")
        click.echo("Note: Requires valid options data from yfinance.")


@cli.command()
@click.argument("symbol")
@click.option("--side", default="buy", type=click.Choice(["buy", "sell"]))
@click.option("--quantity", default="0.01", help="Quantity to trade")
@click.option("--dry-run/--live", default=True, help="Default dry-run for safety")
def order(symbol, side, quantity, dry_run):
    """Place sample order (crypto only in this MVP)."""
    if dry_run:
        click.echo(f"DRY RUN ORDER: {side.upper()} {quantity} of {symbol}")
        click.echo("In live mode, this would use official Crypto API.")
        click.echo("For stocks/options, integrate Agentic MCP per research.")
        return
    
    client = RobinhoodCryptoClient()
    result = client.place_market_buy(symbol.upper(), quantity, dry_run=False)
    click.echo(json.dumps(result, indent=2))
    logger.info(f"Order placed for {symbol}")


@cli.command()
def demo():
    """Run demo script showing data fetch, max pain, and dry-run order."""
    click.echo("=== Robinhood MVP Trader Demo with Max Pain ===\n")
    
    click.echo("1. Fetching portfolio...")
    port = get_portfolio()
    click.echo(f"Total Value: ${port['total_value']:,.2f} | Today's P&L: +${port['todays_pnl']:,.2f}\n")
    
    click.echo("2. Fetching market data for AAPL...")
    quote_data = get_stock_quote("AAPL")
    click.echo(f"AAPL: ${quote_data.get('price'):.2f} ({quote_data.get('change', 0):+.2f}%) - Source: {quote_data.get('source')}\n")
    
    click.echo("3. Computing weekly max pain for SPY (key trading condition)...")
    try:
        pain_result = get_weekly_max_pain("SPY")
        click.echo(f"Max Pain: ${pain_result.max_pain:.2f} (exp {pain_result.expiration}) | Spot ~${pain_result.spot:.2f}\n")
    except Exception as e:
        click.echo(f"Max pain calc note: {e} (may require market hours or data availability)\n")
    
    click.echo("4. Fetching crypto account info...")
    client = RobinhoodCryptoClient()
    acc = client.get_account()
    click.echo("Account status: " + ("Mock/Dry-run" if "mock" in str(acc) or "dry_run" in str(acc) else "Live"))
    
    click.echo("\n5. Placing sample dry-run order for BTC...")
    client.place_market_buy("BTC", "0.001", dry_run=True)
    click.echo("\nDemo completed successfully!")
    click.echo("\nMax pain integrated as trading condition. Use `maxpain TICKER` for decisions.")
    click.echo("To run live crypto orders: set DRY_RUN=false and provide valid API credentials.")
    click.echo("For full stock/options trading: Use Agentic Trading MCP as documented in research.")


if __name__ == "__main__":
    cli()