"""Value-driver valuation (0.16): inputs, the model, its range and reverse DCF, quarterly figures, and analyst
estimates parsing."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from oracle_edge import db, estimates, rules
from oracle_edge.engine import split_scale, ttm_inputs
from oracle_edge.metrics import rnd_capital, vd_inputs
from oracle_edge.valuation import blended_growth, value_driver, vd_value

ST = {"hurdle": 0.10, "mos_gate": 0.25, "terminal_growth": 0.03, "strong_extra_mos": 0.15}


@pytest.fixture(scope="module", autouse=True)
def _db():
    db.get()
    rules.seed_profiles()


def _stmt(n=8, rnd=None, lease=None, pension=None):
    years = [f"{2017 + i}-12-31" for i in range(n)]
    R = [1000.0 * 1.08 ** i for i in range(n)]
    s = {"years": years, "revenue": R, "operating_income": [r * 0.20 for r in R],
         "pretax_income": [r * 0.19 for r in R], "income_tax": [r * 0.19 * 0.21 for r in R],
         "equity": [800.0 + 40 * i for i in range(n)], "treasury_stock": [0.0] * n, "total_debt": [300.0] * n,
         "cash": [100.0] * n, "st_investments": [50.0] * n, "shares_diluted": [100.0] * n,
         "rnd": rnd or [None] * n, "op_lease_liability": lease or [None] * n,
         "pension_funded_status": pension or [None] * n, "preferred_stock": [None] * n,
         "minority_interest": [None] * (n - 1) + [20.0]}
    return s


def test_inputs_normalize_profit_and_list_the_claims_ahead_of_shareholders():
    v = vd_inputs(_stmt(lease=[None] * 7 + [200.0], pension=[None] * 7 + [-100.0]), {})
    assert v["vd_tax"] == pytest.approx(0.21)
    assert len(v["vd_sales"]) == 3 and len(v["vd_margins"]) == 7
    assert v["vd_margins"][-1] == pytest.approx(0.20 + 0.05 * 200 / v["vd_sales"][-1])  # lease interest added back
    assert v["vd_cash"] == 150.0
    c = v["vd_claims"]
    assert c["debt"] == 300 and c["leases"] == 200 and c["minority"] == 20
    assert c["pension"] == pytest.approx(100 * (1 - 0.21))  # an underfunded pension, after tax
    assert v["vd_rev5"] == pytest.approx(0.08, abs=1e-3)
    assert not v["vd_rnd"]["capitalized"]


def test_research_is_treated_as_an_investment_for_heavy_researchers():
    rnd = [100.0 * 1.10 ** i for i in range(8)]
    asset, amort = rnd_capital(rnd, 7)
    expect_asset = sum(rnd[7 - k] * (1 - k / 5) for k in range(5))
    assert asset == pytest.approx(expect_asset) and amort == pytest.approx(sum(rnd[7 - k] for k in range(1, 6)) / 5)
    a, b = vd_inputs(_stmt(rnd=rnd), {}), vd_inputs(_stmt(), {})
    assert a["vd_rnd"]["capitalized"]
    assert a["vd_margins"][-1] > b["vd_margins"][-1]  # growing R&D: this year's spend exceeds the write-off
    # before the record starts, R&D is assumed to match the earliest year
    assert rnd_capital([50.0, None, 70.0], 0)[1] == pytest.approx(50.0)


def _vi(**kw):
    vi = vd_inputs(_stmt(), {})
    vi.update({"sust_g": 0.10, "eps_cagr": 0.09, "eps_by_fy": {}, "eps_last": 1.5, "oe_ps": 1.4})
    vi["vd_roic"] = 0.25
    vi.update(kw)
    return vi


def test_blended_growth_averages_three_sources_and_is_capped():
    g = blended_growth(_vi(), {"vd_growth_cap": 0.12})
    assert g["g"] == pytest.approx((0.08 + 0.09 + 0.07) / 3, abs=1e-3)
    hot = blended_growth(_vi(vd_rev5=0.40, sust_g=0.30, eps_cagr=0.30), {"vd_growth_cap": 0.12})
    assert hot["g"] == 0.12


def test_value_range_expected_return_and_what_the_price_assumes_are_consistent():
    vi = _vi()
    v = value_driver(30.0, vi, ST, 0.25, {})
    x = v.extra
    # the value is discounted at the market's long-run return (9%), not the profile's 10% hurdle (0.17.8)
    assert x["required_return"] == 0.09 and x["hurdle"] == 0.10
    assert x["low"] < v.iv < x["high"]
    # buy below: what the cautious model (the buy signals' rule) is worth at the hurdle, less the margin of safety
    sig = value_driver(30.0, vi, ST, 0.25, {}, signal=True)
    assert sig.discount_rate == 0.10 and sig.iv < v.iv
    assert v.buy_below == pytest.approx(sig.iv * 0.75) == pytest.approx(sig.buy_below)
    assert x["hurdle_price"] == pytest.approx(sig.iv)
    # at the likely value the price earns exactly the discount rate and assumes exactly the blended growth
    at_value = value_driver(v.iv, vi, ST, 0.25, {})
    assert at_value.er == pytest.approx(0.09, abs=2e-4)
    assert at_value.extra["implied_growth"] == pytest.approx(x["growth"]["g"], abs=2e-4)
    # a higher price needs more growth and earns less
    dear = value_driver(v.iv * 1.5, vi, ST, 0.25, {})
    assert dear.extra["implied_growth"] > x["growth"]["g"] and dear.er < 0.09
    # a 15% hurdle lowers the buy price, not the value
    strict = value_driver(30.0, vi, {**ST, "hurdle": 0.15}, 0.25, {})
    assert strict.iv == pytest.approx(v.iv) and strict.buy_below < v.buy_below
    # the other buy rule: the fair value less the margin, capped at the price that earns the hurdle
    alt = value_driver(30.0, vi, {**ST, "vd_buy_rule": "value"}, 0.25, {})
    assert alt.buy_below == pytest.approx(min(v.iv * 0.75, alt.extra["hurdle_price"]))
    assert value_driver(alt.extra["hurdle_price"], vi, ST, 0.25, {}).er == pytest.approx(0.10, abs=2e-4)
    # the discount rate can still be set
    assert value_driver(30.0, vi, {**ST, "vd_required_return": 0.12}, 0.25, {}).iv < v.iv
    assert value_driver(30.0, vi, {**ST, "vd_discount_rate": 0.08}, 0.25, {}).iv > v.iv


def test_margin_dips_count_half_and_values_far_from_the_price_are_not_shown():
    from oracle_edge.valuation import vd_base

    vi = _vi(vd_margins=[0.20] * 5 + [0.02, 0.21])  # a write-off year: recent median 0.20, no dip
    assert vd_base(vi)["margin"] == pytest.approx(0.20)
    vi = _vi(vd_margins=[0.20] * 4 + [0.25, 0.02, 0.01])  # two bad years: halfway back to the 7-year median
    assert vd_base(vi)["margin"] == pytest.approx((0.02 + 0.20) / 2)
    vi = _vi(vd_margins=[0.10] * 4 + [0.20, 0.22, 0.24])  # improving: the recent margin
    assert vd_base(vi)["margin"] == pytest.approx(0.22)
    v = value_driver(30.0, _vi(), ST, 0.25, {})
    assert value_driver(v.iv / 11, _vi(), ST, 0.25, {}).iv is None  # 11x the price: bad inputs, not a bargain
    far = value_driver(v.iv * 11, _vi(), ST, 0.25, {})
    assert far.iv is None and "inputs look wrong" in far.note


def test_share_counts_in_thousands_are_rescaled():
    from oracle_edge.engine import sane_shares

    assert sane_shares(10_094.0, 10_094_000.0) == pytest.approx(10_094_000.0)
    assert sane_shares(60_333_000_000.0, 61_104_000.0) == pytest.approx(60_333_000.0)
    assert sane_shares(9_900_000.0, 10_094_000.0) == 9_900_000.0
    assert sane_shares(400_000.0, 10_094_000.0) is None  # off by 25x: neither unit slip nor plausible
    assert sane_shares(None, 1.0) is None and sane_shares(5.0, None) == 5.0


def test_net_cash_counts_per_diluted_share_and_losses_have_no_value():
    vi = _vi()
    v = value_driver(30.0, vi, ST, 0.25, {})
    more_debt = dict(vi, vd_claims=dict(vi["vd_claims"], debt=vi["vd_claims"]["debt"] + 500))
    assert value_driver(30.0, more_debt, ST, 0.25, {}).iv == pytest.approx(v.iv - 500 / 100.0)
    loss = dict(vi, vd_margins=[-0.05] * 7)
    assert value_driver(30.0, loss, ST, 0.25, {}).iv is None


def test_growth_that_earns_only_the_required_return_adds_nothing():
    # with ROIC = r the reinvestment eats the extra profit: value = next year's profit / r, at any growth path
    assert vd_value(100.0, 0.00, 0.10, 0.10, 0.03) == pytest.approx(100.0 / 0.10)
    assert vd_value(100.0, 0.08, 0.10, 0.10, 0.03) == pytest.approx(108.0 / 0.10)
    # with ROIC above r, growth adds value
    assert vd_value(100.0, 0.08, 0.30, 0.10, 0.03) > vd_value(100.0, 0.00, 0.30, 0.10, 0.03) * 1.2


def test_analyst_estimates_shape_only_the_first_two_years():
    vi = _vi()
    base = value_driver(30.0, vi, ST, 0.25, {})
    up = value_driver(30.0, vi, ST, 0.25, {}, est={"growth": 0.30, "analysts": 10})
    few = value_driver(30.0, vi, ST, 0.25, {}, est={"growth": 0.30, "analysts": 2})
    assert up.iv > base.iv and up.extra["estimates_used"]
    assert few.iv == pytest.approx(base.iv) and not few.extra["estimates_used"]
    off = value_driver(30.0, vi, {**ST, "vd_use_estimates": False}, 0.25, {}, est={"growth": 0.30, "analysts": 10})
    assert off.iv == pytest.approx(base.iv)


def test_trailing_twelve_months_replace_the_oldest_year():
    vi = _vi()
    q = {"filed": "2025-05-01", "metrics_json": '{"ttm_revenue": 2000, "ttm_operating_income": 500, "cash": 400, '
                                               '"st_investments": 0, "debt": 100, "shares": 50, "period_end": "2025-03-31"}'}
    t = ttm_inputs(q, vi, 2.0)
    assert t["sales"] == 2000 and t["shares"] == 100 and t["cash"] == 400
    a = value_driver(30.0, vi, ST, 0.25, {})
    b = value_driver(30.0, vi, ST, 0.25, {}, ttm=t)
    assert b.extra["ttm"] and b.iv > a.iv  # bigger sales and margin, more cash, less debt
    off = value_driver(30.0, vi, {**ST, "vd_use_ttm": False}, 0.25, {}, ttm=t)
    assert off.iv == pytest.approx(a.iv)


def test_split_and_foreign_scaling_reach_the_share_count():
    vi = split_scale({"vd_shares": 100.0, "shares": 100.0, "eps_by_fy": {}}, 4.0)
    assert vi["vd_shares"] == 400.0


def _df(rows: dict, cols: list[str]):
    return pd.DataFrame([[rows[k][c] for c in cols] for k in rows], index=list(rows), columns=cols)


def test_estimates_parse_and_near_term_growth():
    eps = _df({"0q": {"avg": 1.1, "low": 1.0, "high": 1.2, "yearAgoEps": 1.0, "numberOfAnalysts": 20, "growth": 0.1},
               "0y": {"avg": 4.4, "low": 4.1, "high": 4.8, "yearAgoEps": 4.0, "numberOfAnalysts": 25, "growth": 0.1},
               "+1y": {"avg": 4.84, "low": 4.3, "high": 5.4, "yearAgoEps": 4.4, "numberOfAnalysts": 24,
                       "growth": 0.1}}, ["avg", "low", "high", "yearAgoEps", "numberOfAnalysts", "growth"])
    trend = _df({"0y": {"current": 4.4, "7daysAgo": 4.38, "30daysAgo": 4.3, "60daysAgo": 4.25, "90daysAgo": 4.2}},
                ["current", "7daysAgo", "30daysAgo", "60daysAgo", "90daysAgo"])
    x = estimates.parse(eps, None, trend, None, {"low": 150, "high": 260, "mean": 210, "median": 205},
                        {"numberOfAnalystOpinions": 30, "lastFiscalYearEnd": 1767139200})
    assert x["periods"]["0y"]["eps"] == 4.4 and x["periods"]["0y"]["analysts"] == 25
    assert x["trend"]["0y"]["90d"] == 4.2 and x["targets"]["mean"] == 210 and x["last_fy_end"] == "2025-12-31"
    ng = estimates.near_term_growth(x)
    assert ng["years"] == 2 and ng["growth"] == pytest.approx((4.84 / 4.0) ** 0.5 - 1)


def test_surprises_merge_sources_and_the_beat_record():
    hist = _df({"2026-09-30": {"epsEstimate": 1.0, "epsActual": 1.1, "surprisePercent": 0.10},
                "2026-06-30": {"epsEstimate": 1.0, "epsActual": 0.9, "surprisePercent": -0.10}},
               ["epsEstimate", "epsActual", "surprisePercent"])
    dates = _df({"2026-10-28": {"EPS Estimate": 1.0, "Reported EPS": 1.1, "Surprise(%)": 10.0},  # same quarter
                 "2026-01-29": {"EPS Estimate": 0.8, "Reported EPS": 0.9, "Surprise(%)": 12.5},
                 "2027-01-28": {"EPS Estimate": 1.2, "Reported EPS": None, "Surprise(%)": None}},  # not reported
                ["EPS Estimate", "Reported EPS", "Surprise(%)"])
    sp = estimates.parse_surprises(hist, dates)
    assert [v["date"] for v in sp] == ["2026-09-30", "2026-06-30", "2026-01-29"]
    assert sp[2]["surprise"] == pytest.approx(0.125)
    rec = estimates.beat_record(sp)
    assert rec == {"n": 3, "beats": 2, "misses": 1, "median": pytest.approx(0.10), "streak": 1}


def test_estimates_are_used_only_when_they_existed_at_the_time():
    today = date.today()
    x = {"periods": {"0y": {"eps": 4.4, "eps_year_ago": 4.0, "analysts": 10}}}
    estimates.store("ZZTEST", x, [{"date": "2026-01-01", "est": 1, "actual": 1.1, "surprise": 0.1}],
                    day=today.isoformat())
    assert estimates.asof("ZZTEST", today.isoformat())["growth"] == pytest.approx(0.10)
    assert estimates.asof("ZZTEST", (today - timedelta(days=400)).isoformat()) is None  # a backtest date
    assert estimates.surprises("ZZTEST")[0]["actual"] == 1.1
    with db.tx() as con:
        con.execute("DELETE FROM estimates WHERE ticker='ZZTEST'")
        con.execute("DELETE FROM surprises WHERE ticker='ZZTEST'")


def test_prices_cover_every_company_unless_switched_off():
    from oracle_edge import pipeline

    universe = set(pipeline.universe_ciks())
    db.set_cfg("prices_all", True)
    assert set(pipeline.price_universe([])) == universe
    db.set_cfg("prices_all", False)
    assert pipeline.price_universe([123]) == [123]
    db.set_cfg("prices_all", True)


def test_the_accuracy_check_measures_value_against_what_it_turned_out_to_be_worth():
    from oracle_edge import valuecheck

    vi = _vi()
    iv = value_driver(30.0, vi, ST, 0.25, {}).iv
    # 40 stocks priced at the value that then returned exactly 9% a year for 10 years: worth exactly the value
    pts = [{"cik": i, "ticker": f"Z{i}", "year": "2015", "price": iv, "tr": 1.09 ** 10, "years": 10.0, "vi": vi}
           for i in range(40)]
    res = valuecheck.evaluate(pts, ST)
    assert res["n"] == 40 and res["bias"] == pytest.approx(1.0, abs=1e-6) and res["miss"] == pytest.approx(0.0, abs=1e-6)
    assert res["range_coverage"] == 1.0 and res["verdicts"]["about right"]["n"] == 40
    # priced at half the value, then doubling relative to a 9% return: the model called it cheap and was right
    pts = [dict(x, price=iv / 2, tr=2 * 1.09 ** 10) for x in pts]
    res = valuecheck.evaluate(pts, ST)
    assert res["verdicts"]["cheap"]["n"] == 40 and res["bias"] == pytest.approx(1.0, abs=1e-6)
    assert res["price_miss"] == pytest.approx(1.0, abs=1e-6)  # the price was 50% under what it turned out to be worth
    assert "past valuations" in valuecheck.summary({**res, "tickers": 40})


def test_buy_signals_keep_the_pre_0_17_8_model():
    # signal=True must reproduce the model the backtests were run with: 5 + 5 years, ROIC / 2, the 7-year median
    # margin x the 3-year average sales, discounted at the hurdle, buy below = value x (1 - margin of safety)
    from statistics import median

    from oracle_edge.valuation import vd_value

    vi = _vi()
    sig = value_driver(30.0, vi, ST, 0.25, {}, signal=True)
    sales = [v for v in vi["vd_sales"] if v]
    nopat = median(vi["vd_margins"]) * sum(sales) / len(sales) * (1 - vi["vd_tax"])
    g = sig.growth
    roic = min(max(vi["vd_roic"], 0.10), 1.0)
    gs = [0.0] + [g if t <= 5 else g + (0.03 - g) * (t - 5) / 5 for t in range(1, 11)] + [0.03]
    pv, e = 0.0, nopat
    for t in range(1, 11):
        e *= 1 + gs[t]
        pv += e * (1 - max(gs[t + 1], 0.0) / roic) / 1.10 ** t
    tv = e * 1.03 * (1 - 0.03 / max(0.10, roic / 2)) / (0.10 - 0.03)
    old = (pv + tv / 1.10 ** 10 + vi["vd_cash"] - sum(vi["vd_claims"].values())) / vi["vd_shares"]
    assert sig.iv == pytest.approx(old) and sig.buy_below == pytest.approx(old * 0.75)
    assert vd_value(nopat, g, roic, 0.10, 0.03, None, 5, 5, 0.5) == pytest.approx(pv + tv / 1.10 ** 10)
