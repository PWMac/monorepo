"""Signal strength ratings and downside measures."""

import pytest

from oracle_edge import strength
from oracle_edge.ui.data import max_drawdown, worst_12m


def test_buy_stars():
    assert strength.buy_stars(60, 70, 0.26) == 1          # passed every gate, nothing more
    assert strength.buy_stars(70, 75, 0.30) == 2          # Rank >= 65
    assert strength.buy_stars(80, 85, 0.30) == 4          # Rank >= 75 and Quality >= 80
    assert strength.buy_stars(80, 85, 0.42) == 5          # plus a strong-buy price
    assert strength.buy_stars(None, 85, 0.42) is None


def test_sell_levels():
    assert strength.sell_level("SELL")[0].startswith("Act")
    assert strength.sell_level("REVIEW", "x · becomes SELL if the next filing confirms it")[0].startswith("High")
    assert strength.sell_level("REVIEW", "x · watch item, not a sell trigger")[0].startswith("Low")
    assert "★" in strength.strength_html("BUY", 80, 85, 0.42)


def test_drawdown_and_worst_year():
    assert max_drawdown([1.0, 1.2, 0.9, 1.3]) == pytest.approx(0.9 / 1.2 - 1)
    dates = ["2020-01-01", "2020-06-01", "2021-01-01", "2021-06-01", "2022-01-01"]
    vals = [100, 120, 90, 80, 100]
    assert worst_12m(dates, vals) == pytest.approx(80 / 120 - 1)   # Jun 2020 -> Jun 2021
    assert worst_12m(dates[:2], vals[:2]) is None                   # less than a year of data
