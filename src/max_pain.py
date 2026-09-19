"""Weekly max-pain lookup for use as a trading condition.

Max pain is the strike that minimizes the total intrinsic value of
open call and put interest if the underlying expired at that strike.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any


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

    `ticker` is injectable for tests; live calls use `yfinance.Ticker`.
    """
    symbol = symbol.upper().strip()
    if not symbol:
        raise ValueError("symbol is required")
    client = ticker
    if client is None:
        import yfinance as yf

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
