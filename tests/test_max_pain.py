from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from max_pain import compute_max_pain, get_weekly_max_pain, nearest_weekly_expiration


def test_compute_max_pain_selects_strike_with_lowest_total_intrinsic_value():
    # Calls OI: 10 @ 50, 20 @ 55, 30 @ 60
    # Puts  OI: 40 @ 50, 20 @ 55, 10 @ 60
    # Pain(50)=200, Pain(55)=100, Pain(60)=200 → max pain is 55
    call_oi = {50.0: 10, 55.0: 20, 60.0: 30}
    put_oi = {50.0: 40, 55.0: 20, 60.0: 10}

    assert compute_max_pain(call_oi, put_oi) == 55.0


def test_nearest_weekly_expiration_skips_0dte_and_picks_next_friday():
    expirations = [
        "2026-09-21",  # Monday 0DTE
        "2026-09-22",  # Tuesday
        "2026-09-25",  # Friday weekly
        "2026-10-16",  # monthly
    ]

    assert nearest_weekly_expiration(expirations, as_of=date(2026, 9, 21)) == "2026-09-25"


class _FakeTicker:
    options = ("2026-09-21", "2026-09-25", "2026-10-16")

    def option_chain(self, expiration):
        assert expiration == "2026-09-25"
        calls = pd.DataFrame(
            {"strike": [50.0, 55.0, 60.0], "openInterest": [10, 20, 30]}
        )
        puts = pd.DataFrame(
            {"strike": [50.0, 55.0, 60.0], "openInterest": [40, 20, 10]}
        )
        return SimpleNamespace(calls=calls, puts=puts)

    @property
    def fast_info(self):
        return SimpleNamespace(last_price=57.0)


def test_get_weekly_max_pain_returns_strike_spot_and_expiration_for_trading():
    result = get_weekly_max_pain("spy", ticker=_FakeTicker(), as_of=date(2026, 9, 21))

    assert result.symbol == "SPY"
    assert result.expiration == "2026-09-25"
    assert result.max_pain == 55.0
    assert result.spot == 57.0
    assert result.above_max_pain is True


def test_compute_max_pain_raises_without_strikes():
    with pytest.raises(ValueError, match="no strikes"):
        compute_max_pain({}, {})


def test_nearest_weekly_expiration_keeps_friday_when_as_of_is_expiry_day():
    assert (
        nearest_weekly_expiration(["2026-09-25", "2026-10-02"], as_of=date(2026, 9, 25))
        == "2026-09-25"
    )


def test_nearest_weekly_expiration_falls_back_when_no_friday_is_listed():
    assert nearest_weekly_expiration(["2026-09-24"], as_of=date(2026, 9, 21)) == "2026-09-24"


def test_nan_open_interest_is_treated_as_zero():
    class Fake:
        options = ("2026-09-25",)

        def option_chain(self, expiration):
            calls = pd.DataFrame(
                {"strike": [50.0, 55.0], "openInterest": [10.0, float("nan")]}
            )
            puts = pd.DataFrame(
                {"strike": [50.0, 55.0], "openInterest": [float("nan"), 10.0]}
            )
            return SimpleNamespace(calls=calls, puts=puts)

        @property
        def fast_info(self):
            return SimpleNamespace(last_price=52.0)

    result = get_weekly_max_pain("QQQ", ticker=Fake(), as_of=date(2026, 9, 21))
    # Pain(50)=5*10=50, Pain(55)=5*10=50 → lowest strike wins
    assert result.max_pain == 50.0
    assert result.above_max_pain is True


def test_get_weekly_max_pain_rejects_blank_symbol():
    with pytest.raises(ValueError, match="symbol"):
        get_weekly_max_pain("  ")
