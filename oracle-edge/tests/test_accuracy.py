"""Calculation audit (0.12.0): each fix checked against an independently worked answer."""

from __future__ import annotations

import random
from datetime import date

import pandas as pd
import pytest

from oracle_edge import db, rules
from oracle_edge.engine import on_basis, split_scale
from oracle_edge.metrics import build_statement, cagr, compute, fiscal_year_ends, fix_share_scale, years_between
from oracle_edge.splits import Splits, from_filings, nice_ratio
from oracle_edge.valuation import value, value_financial, value_score


@pytest.fixture(scope="module", autouse=True)
def _db():
    db.get()
    rules.seed_profiles()


def _sp(ticker, rows):
    sp = Splits(load=False)
    sp.d[ticker] = ([d for d, _ in rows], [r for _, r in rows])
    return sp


# ------------------------------------------------------------------ stock splits
def test_split_factor_and_nice_ratios():
    sp = _sp("ABC", [("2022-06-01", 10.0), ("2024-03-01", 2.0)])
    assert sp.factor("ABC", "2021-01-01") == 20.0                    # both splits after
    assert sp.factor("ABC", "2022-06-01") == 2.0                     # a filing ON the split day is already restated
    assert sp.factor("ABC", "2021-01-01", "2023-01-01") == 10.0
    assert sp.factor("XYZ", "2021-01-01") == 1.0
    assert nice_ratio(10.03) == 10 and nice_ratio(0.0999) == 0.1 and nice_ratio(1000.0) is None
    assert nice_ratio(1.012) is None


def test_older_filings_are_restated_to_the_snapshot_basis():
    sp = _sp("ABC", [("2022-06-01", 10.0)])
    state = {("shares_diluted", "2020-12-31"): (100.0, "2021-02-15"),  # filed before the split
             ("eps_diluted", "2020-12-31"): (2.0, "2021-02-15"),
             ("shares_diluted", "2022-12-31"): (1000.0, "2023-02-15"),
             ("eps_diluted", "2022-12-31"): (0.24, "2023-02-15"),
             ("net_income", "2020-12-31"): (200.0, "2021-02-15")}
    st = on_basis(state, "2023-02-15", sp, "ABC")
    assert st[("shares_diluted", "2020-12-31")] == 1000.0 and abs(st[("eps_diluted", "2020-12-31")] - 0.2) < 1e-12
    assert st[("eps_diluted", "2022-12-31")] == 0.24 and st[("net_income", "2020-12-31")] == 200.0
    # before the split the snapshot keeps the old basis
    st0 = on_basis({k: v for k, v in state.items() if v[1] < "2022-01-01"}, "2021-02-15", sp, "ABC")
    assert st0[("shares_diluted", "2020-12-31")] == 100.0


def test_valuation_inputs_follow_later_splits():
    vi = {"oe_ps": 10.0, "eps3": 8.0, "eps_last": 9.0, "bvps": 40.0, "shares": 1e6, "eps_by_fy": {"2023-12-31": 9.0}}
    out = split_scale(vi, 10.0)
    assert out["oe_ps"] == 1.0 and out["eps_last"] == 0.9 and out["shares"] == 1e7
    assert out["eps_by_fy"] == {"2023-12-31": 0.9} and vi["oe_ps"] == 10.0  # the input isn't modified


def test_a_split_no_longer_looks_like_a_bargain():
    """The LRCX case: a 10:1 split after the last annual report. Before the fix the snapshot's EPS (pre-split) was
    compared with the post-split price. Price and value must move together."""
    st = rules.default_profile().settings
    vi = {"oe_ps": 30.0, "eps3": 28.0, "eps_last": 29.0, "sust_g": 0.12, "eps_cagr": 0.15, "payout": 0.2, "roe": 0.4,
          "eps_by_fy": {}}
    pre = value(700.0, vi, st, 0.045, st["mos_gate"], {})
    post = value(70.0, split_scale(vi, 10.0), st, 0.045, st["mos_gate"], {})
    assert abs(pre.mos - post.mos) < 1e-9 and abs(pre.iv / 10 - post.iv) < 1e-9


def test_splits_inferred_from_restated_filings():
    facts = [{"item": "shares_diluted", "fy_end": "2021-12-31", "value": 100.0, "filed": "2022-02-10"},
             {"item": "shares_diluted", "fy_end": "2021-12-31", "value": 400.0, "filed": "2024-02-12"},  # 4:1 later
             {"item": "shares_diluted", "fy_end": "2022-12-31", "value": 101.0, "filed": "2023-02-10"},
             {"item": "shares_diluted", "fy_end": "2022-12-31", "value": 404.0, "filed": "2024-02-12"},
             {"item": "eps_diluted", "fy_end": "2022-12-31", "value": 4.0, "filed": "2023-02-10"},
             {"item": "eps_diluted", "fy_end": "2022-12-31", "value": 1.0, "filed": "2024-02-12"},
             {"item": "shares_diluted", "fy_end": "2020-12-31", "value": 100.0, "filed": "2021-02-10"},
             {"item": "shares_diluted", "fy_end": "2020-12-31", "value": 103.0, "filed": "2022-02-10"}]  # noise
    assert from_filings(facts) == [("2024-02-12", 4)]


# ------------------------------------------------------------------ stored price history
def _bars(t, rows):
    return pd.DataFrame([{"ticker": t, "date": d, "open": c, "high": c, "low": c, "close": c, "adj_close": a,
                          "volume": 1000} for d, c, a in rows])


def test_split_in_the_update_window_rescales_history_and_holdings():
    from oracle_edge.sources import prices

    con = db.get()
    con.execute("INSERT OR REPLACE INTO securities(cik,ticker,name) VALUES(7701,'SPLT','Split Co')")
    con.execute("DELETE FROM prices WHERE ticker='SPLT'")
    # stored: 100 per share up to the 3rd, then the stock split 2:1 and traded at 50 (already stored on the 4th)
    old = [("2026-03-01", 100.0, 100.0), ("2026-03-02", 100.0, 100.0), ("2026-03-03", 100.0, 100.0),
           ("2026-03-04", 50.0, 50.0)]
    prices.store(_bars("SPLT", old[:3]), "yahoo")
    con.execute("INSERT OR REPLACE INTO prices(ticker,date,open,high,low,close,adj_close,volume,source) "
                "VALUES('SPLT','2026-03-04',50,50,50,50,50,1000,'yahoo')")
    con.execute("INSERT INTO portfolios(id,run_id,name,kind,cash) VALUES(7701,7701,'m','model',0)")
    con.execute("INSERT INTO positions(portfolio_id,cik,units,avg_cost,opened_on) VALUES(7701,7701,10,90,'2026-01-02')")
    # the source now sends the window on the new basis (everything before the 4th halved)
    new = [("2026-03-02", 50.0, 50.0), ("2026-03-03", 50.0, 50.0), ("2026-03-04", 50.0, 50.0), ("2026-03-05", 51.0, 51.0)]
    prices.store(_bars("SPLT", new), "yahoo")
    rows = {r["date"]: r["close"] for r in db.rows("SELECT date, close FROM prices WHERE ticker='SPLT'")}
    assert rows == {"2026-03-01": 50.0, "2026-03-02": 50.0, "2026-03-03": 50.0, "2026-03-04": 50.0, "2026-03-05": 51.0}
    pos = db.one("SELECT units, avg_cost FROM positions WHERE portfolio_id=7701")
    assert pos["units"] == 20.0 and pos["avg_cost"] == 45.0          # same value, twice the shares
    assert db.one("SELECT date, ratio, source FROM splits WHERE ticker='SPLT'")["ratio"] == 2


def test_dividend_adjustment_carries_on_and_one_day_fix_does_not_rescale():
    from oracle_edge.sources import prices

    con = db.get()
    con.execute("INSERT OR REPLACE INTO securities(cik,ticker,name) VALUES(7702,'DIVY','Div Co')")
    con.execute("DELETE FROM prices WHERE ticker='DIVY'")
    prices.store(_bars("DIVY", [("2026-03-01", 100.0, 100.0), ("2026-03-02", 100.0, 100.0),
                                ("2026-03-03", 99.0, 99.0)]), "yahoo")
    con.execute("INSERT INTO portfolios(id,run_id,name,kind,cash) VALUES(7702,7702,'m','model',0)")
    con.execute("INSERT INTO positions(portfolio_id,cik,units,avg_cost,opened_on) VALUES(7702,7702,10,100,'2026-01-02')")
    # a $1 dividend went ex on the 3rd: the total-return close of earlier days is scaled by 0.99, the close isn't
    prices.store(_bars("DIVY", [("2026-03-02", 100.0, 99.0), ("2026-03-03", 99.0, 99.0)]), "yahoo")
    r = {x["date"]: (x["close"], x["adj_close"]) for x in db.rows("SELECT * FROM prices WHERE ticker='DIVY'")}
    assert r["2026-03-01"] == (100.0, 99.0) and r["2026-03-03"] == (99.0, 99.0)
    assert abs(db.scalar("SELECT units FROM positions WHERE portfolio_id=7702") - 10 / 0.99) < 1e-9  # reinvested
    # a source correcting only its latest close is not a basis change
    prices.store(_bars("DIVY", [("2026-03-02", 100.0, 99.0), ("2026-03-03", 99.2, 99.2)]), "yahoo")
    r = {x["date"]: x["adj_close"] for x in db.rows("SELECT * FROM prices WHERE ticker='DIVY'")}
    assert r["2026-03-01"] == 99.0 and r["2026-03-03"] == 99.2


# ------------------------------------------------------------------ statements
def test_two_fiscal_years_ending_in_one_calendar_year_are_both_kept():
    ends = ["2021-01-02", "2022-01-01", "2022-12-31", "2023-12-30", "2023-12-31"]
    g = fiscal_year_ends(ends)
    assert [c for c, _ in g] == ["2021-01-02", "2022-01-01", "2022-12-31", "2023-12-31"]
    state = {("net_income", e): float(i + 1) for i, e in enumerate(["2021-01-02", "2022-01-01", "2022-12-31",
                                                                   "2023-12-31"])}
    state[("revenue", "2023-12-30")] = 50.0  # the same year tagged a day apart
    state[("equity", "2023-12-30")] = 500.0
    s = build_statement(state, 10)
    assert s["years"] == ["2021-01-02", "2022-01-01", "2022-12-31", "2023-12-31"]
    assert s["net_income"] == [1.0, 2.0, 3.0, 4.0] and s["revenue"][-1] == 50.0 and s["equity"][-1] == 500.0


def test_share_counts_tagged_in_thousands_are_rescaled():
    s = {"years": ["2022", "2023", "2024"], "shares_diluted": [273_298.0, 270_000_000.0, None],
         "net_income": [1.38e9, 1.4e9, 1.5e9], "eps_diluted": [5.05, 5.2, None]}
    assert fix_share_scale(s) == 1
    assert abs(s["shares_diluted"][0] - 273_298_000.0) < 1


def test_growth_rates_count_years_by_date():
    assert abs(years_between("2015-12-31", "2020-12-31") - 5.0) < 0.01
    st = {("net_income", y): 100.0 for y in ("2014-12-31", "2015-12-31", "2018-12-31", "2019-12-31")}
    st.update({("eps_diluted", "2014-12-31"): 1.0, ("eps_diluted", "2015-12-31"): 1.1,
               ("eps_diluted", "2018-12-31"): 1.4, ("eps_diluted", "2019-12-31"): 1.61051})
    s = build_statement(st, 10)
    m = compute(s)
    # 1.0 -> 1.61051 over FIVE years (2014-2019) is 10%/yr; counting the 4 listed years would say 12.6%
    assert abs(m["eps_growth"].value - 0.10) < 0.001
    assert abs(cagr(1.0, 1.61051, years_between("2014-12-31", "2019-12-31")) - 0.10) < 0.001


# ------------------------------------------------------------------ valuation
def test_buy_below_is_exactly_the_buy_zone_test_for_standard_companies():
    """Random companies: price <= buy-below must mean exactly MOS >= gate AND expected return >= hurdle."""
    st = dict(rules.default_profile().settings)
    rng = random.Random(4)
    for _ in range(300):
        vi = {"oe_ps": rng.uniform(0.5, 12), "eps3": rng.uniform(0.5, 12), "eps_last": rng.uniform(0.5, 12),
              "sust_g": rng.uniform(0, 0.25), "eps_cagr": rng.uniform(-0.05, 0.25), "payout": rng.uniform(0, 0.6),
              "eps_by_fy": {}}
        if rng.random() < 0.2:
            vi["oe_ps"] = None
        for hurdle in (0.10, 0.15):
            st["hurdle"] = hurdle
            ref = value(100.0, vi, st, 0.045, 0.25, {})
            if ref.buy_below is None:
                continue
            for p in (ref.buy_below * 0.995, ref.buy_below * 1.005):
                v = value(p, vi, st, 0.045, 0.25, {})
                old_rule = v.mos >= 0.25 and v.er >= hurdle
                assert old_rule == (p <= v.buy_below), (vi, hurdle, p, v)


def test_banks_earn_their_cost_of_equity_at_fair_value_and_buy_below_matches():
    st = dict(rules.default_profile().settings)
    st["hurdle"] = 0.15
    vi = {"book_ps": 50.0, "roe": 0.15, "payout": 0.6, "sust_g": 0.06, "eps3": 7.5, "eps_last": 7.5, "eps_by_fy": {}}
    v = value_financial(50.0, vi, st, 0.045, 0.25, {})
    # justified P/B = (15% - 6%) / (15% - 6%) = 1, so book = value; return = 6% growth + 9% dividends = 15%
    assert abs(v.iv_a - 50.0) < 1e-9 and abs(v.er_a - 0.15) < 1e-9
    for p in (v.buy_below * 0.999, v.buy_below * 1.001):
        w = value_financial(p, vi, st, 0.045, 0.25, {})
        assert (w.mos >= 0.25 and w.er >= 0.15) == (p <= w.buy_below)


def test_value_score_with_high_hurdles():
    assert value_score(0.10, {"target": 0.10}) == 70.0 and value_score(0.20, {"target": 0.10}) == 100.0
    assert value_score(0.20, {"target": 0.20}) == 70.0 and value_score(0.25, {"target": 0.20}) == 100.0
    assert value_score(0.19, {"target": 0.20}) < 70.0


# ------------------------------------------------------------------ performance
def test_cagr_uses_the_first_point_of_the_curve():
    from oracle_edge.ui.data import stats

    curve = [{"date": "2020-01-01", "nav": 2.0, "bench_nav": 1.0}, {"date": "2022-01-01", "nav": 2.42, "bench_nav": 1.21}]
    st = stats(curve)
    assert abs(st["cagr"] - 0.10) < 0.001 and abs(st["total"] - 0.21) < 1e-9 and abs(st["bench_cagr"] - 0.10) < 0.001


def test_world_buffett_indicator_estimate():
    from oracle_edge.market import Macro

    with db.tx() as con:
        con.execute("DELETE FROM macro WHERE series LIKE 'WB_WLD_%'")
        con.executemany("INSERT INTO macro(series,date,value) VALUES(?,?,?)",
                        [("WB_WLD_MCAP", "2024-12-31", 120e12), ("WB_WLD_GDP", "2024-12-31", 110e12),
                         ("WB_WLD_MCAP", "2023-12-31", 100e12), ("WB_WLD_GDP", "2023-12-31", 105e12)])
    m = Macro()
    w = m.world_now("2026-11-20", lambda d: 120.0 if d == "2026-11-20" else 100.0)
    assert w["year_end"] == "2024-12-31"
    # GDP grown from the end of 2024 to the reading date at 2024's growth (110 / 105, over 366 days)
    yrs = (date(2026, 11, 20) - date(2024, 12, 31)).days / 365.25
    g = (110 / 105) ** (365.25 / 366) - 1
    assert abs(w["gdp_growth"] - g) < 1e-12 and abs(w["gdp"] - 110e12 * (1 + g) ** yrs) < 1
    assert abs(w["ratio"] - 120 * 1.2 / (110 * (1 + g) ** yrs)) < 1e-12
    assert [round(h["ratio"], 6) for h in m.world_history()] == [round(100 / 105, 6), round(120 / 110, 6)]


def test_signal_history_keeps_live_and_current_version_backtests_only():
    import json

    from oracle_edge.ui import data

    con = db.get()
    p = rules.default_profile()
    con.execute("INSERT OR REPLACE INTO securities(cik,ticker,name) VALUES(7801,'SIGS','Sig Co')")
    runs = [(7801, "live", {}), (7802, "backtest", {"profile_version": p.version}),
            (7803, "backtest", {"profile_version": p.version + 5}), (7804, "sweep", {})]
    for rid, kind, sj in runs:
        con.execute("INSERT INTO runs(id,kind,name,profile_id,settings_json) VALUES(?,?,?,?,?)",
                    (rid, kind, kind, p.id, json.dumps(sj)))
        con.execute("INSERT INTO signals(run_id,portfolio_id,cik,date,type,reason) VALUES(?,0,7801,'2026-01-05','BUY','x')",
                    (rid,))
    assert sorted(g["run_id"] for g in data.stock_signals(7801, p)) == [7801, 7802]
    assert [g["run_id"] for g in data.stock_signals(7801, run_id=7804)] == [7804]


def test_news_parsing_both_yahoo_layouts_and_rss():
    from oracle_edge import news

    new = [{"id": "1", "content": {"title": "Chipmaker beats", "pubDate": "2026-11-20T14:05:00Z",
                                   "provider": {"displayName": "Reuters"},
                                   "canonicalUrl": {"url": "https://example.com/a"}}}]
    old = [{"title": "Old style", "publisher": "AP", "link": "https://example.com/b", "providerPublishTime": 1795100000}]
    a, b = news.parse_yahoo(new)[0], news.parse_yahoo(old)[0]
    assert (a["title"], a["source"], a["url"], a["when"]) == ("Chipmaker beats", "Reuters", "https://example.com/a",
                                                              "2026-11-20T14:05:00+00:00")
    assert b["source"] == "AP" and b["when"].startswith("2026-")
    rss = ("<rss><channel><item><title>Stocks rise - MarketWatch</title><link>https://example.com/c</link>"
           "<pubDate>Thu, 19 Nov 2026 21:08:53 GMT</pubDate><source url='x'>MarketWatch</source></item>"
           "<item><title>No link</title></item></channel></rss>")
    items = news.parse_rss(rss, "Google News")
    assert items == [{"title": "Stocks rise", "url": "https://example.com/c", "source": "MarketWatch",
                      "when": "2026-11-19T21:08:53+00:00"}]


def test_us_buffett_indicator_nowcast_between_releases():
    """Each part is the official figure once published, and an estimate until then: equities moved by VTI's price,
    GDP grown at its past-four-quarter rate from the end of its quarter."""
    from oracle_edge.market import Macro

    gdp = [("2025-04-01", 30_000.0), ("2025-07-01", 30_300.0), ("2025-10-01", 30_600.0), ("2026-01-01", 30_900.0),
           ("2026-04-01", 31_500.0)]
    with db.tx() as con:
        con.execute("DELETE FROM macro WHERE series IN ('NCBEILQ027S','BOGZ1LM893064105Q','GDP')")
        con.executemany("INSERT INTO macro(series,date,value) VALUES(?,?,?)",
                        [("NCBEILQ027S", "2026-04-01", 70_000_000.0)] + [("GDP", d, v) for d, v in gdp])
    m = Macro()
    c = m.us_components("2026-09-30", lambda d: 105.0 if d == "2026-09-30" else 100.0)
    g = 31_500 / 30_000 - 1  # Q2 2026 over Q2 2025
    yrs = (date(2026, 9, 30) - date(2026, 6, 30)).days / 365.25
    assert c["quarter_end"] == "2026-06-30" and c["gdp_quarter_end"] == "2026-06-30"
    assert abs(c["gdp_growth"] - g) < 1e-12 and abs(c["gdp"] - 31_500e9 * (1 + g) ** yrs) < 1
    assert abs(c["ratio"] - 70e12 * 1.05 / (31_500e9 * (1 + g) ** yrs)) < 1e-12
    # on its quarter's last day nothing is estimated: the reading equals the official quarterly history point
    c0 = m.us_components("2026-06-30", lambda d: 100.0, point_in_time=False)
    assert abs(c0["ratio"] - m.us_history()[-1]["ratio"]) < 1e-12 and c0["gdp_scale"] == 1.0
    # from today on, a release already downloaded is used at once (no waiting for the usual publication lag)
    from datetime import date as _d

    t = _d.today()
    qs = _d(t.year, (t.month - 1) // 3 * 3 + 1, 1)
    prev_q = _d(qs.year - 1, 10, 1) if qs.month == 1 else _d(qs.year, qs.month - 3, 1)  # the quarter just ended
    t = t.isoformat()
    with db.tx() as con:
        con.execute("INSERT INTO macro(series,date,value) VALUES('NCBEILQ027S',?,?)", (prev_q.isoformat(), 71_000_000.0))
    assert Macro().us_components(t)["equities_q"] == 71e12
