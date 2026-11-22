"""The Total return calculator."""

from datetime import date, timedelta

import pytest

from oracle_edge import growth


def _series(days=3 * 365 + 1, rate=0.10, start=date(2020, 1, 1)):
    """A price growing exactly `rate` a year, every calendar day (weekends included, for easy arithmetic)."""
    return [((start + timedelta(days=k)).isoformat(), 100 * (1 + rate) ** (k / 365.25)) for k in range(days)]


def test_lump_sum_grows_at_the_stocks_rate():
    r = growth.simulate(_series(), "2020-01-01", "2022-12-31", 10_000)
    assert r["total_invested"] == 10_000
    assert r["cagr"] == pytest.approx(0.10, abs=1e-4) and r["irr"] == pytest.approx(0.10, abs=1e-3)
    assert r["final"] == pytest.approx(10_000 * 1.10 ** r["years"], rel=1e-6)
    assert r["max_drawdown"] == 0 and r["contributions"] == 1


def test_monthly_contributions_and_money_weighted_return():
    r = growth.simulate(_series(), "2020-01-01", "2022-12-31", 1_000, 100, "monthly")
    assert r["contributions"] == 1 + 35  # the start, then the 1st of every month to Dec 2022
    assert r["total_invested"] == 1_000 + 35 * 100
    # every dollar earned 10% a year from the day it went in: the money-weighted return is 10% too
    assert r["irr"] == pytest.approx(0.10, abs=1e-3)
    assert r["final"] > r["total_invested"]
    assert growth.schedule(date(2020, 1, 31), date(2020, 4, 30), "monthly") == [
        date(2020, 2, 29), date(2020, 3, 31), date(2020, 4, 30)]  # month ends handled
    assert len(growth.schedule(date(2020, 1, 1), date(2020, 3, 1), "weekly")) == 8
    assert growth.schedule(date(2020, 1, 1), date(2023, 1, 1), "yearly")[-1] == date(2023, 1, 1)


def test_drawdown_window_and_inflation():
    s = _series(days=10)
    s[5] = (s[5][0], 50.0)  # a crash to 50 from ~100 on day 5
    r = growth.simulate(s, "2020-01-01", "2020-01-10", 1_000)
    assert r["max_drawdown"] == pytest.approx(50 / s[4][1] - 1)
    assert growth.simulate(s, "2030-01-01", "2031-01-01", 1_000) is None  # no prices in the window
    cpi = [("2019-12-01", 100.0), ("2021-01-01", 110.0), ("2022-01-01", 121.0)]  # 10% a year
    r = growth.simulate(_series(), "2020-01-01", "2022-12-31", 10_000, cpi=cpi)
    assert r["inflation"] == pytest.approx(0.21)
    assert r["real_final"] == pytest.approx(r["final"] / 1.21)
    assert r["real_cagr"] == pytest.approx(1.10 / 1.21 ** (1 / r["years"]) - 1, abs=1e-6)


def test_xirr_and_weekly_thinning():
    flows = [(date(2020, 1, 1), -100.0), (date(2021, 1, 1), 110.0)]
    assert growth.xirr(flows) == pytest.approx(0.10, abs=1e-3)
    assert growth.xirr([(date(2020, 1, 1), -100.0)]) is None
    d = [(date(2024, 1, 1) + timedelta(days=k)).isoformat() for k in range(14)]  # Mon 1 Jan .. Sun 14 Jan
    wd, (v,) = growth.weekly(d, list(range(14)))
    assert wd == ["2024-01-07", "2024-01-14"] and v == [6, 13]
