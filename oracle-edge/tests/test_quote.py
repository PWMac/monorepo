"""Chart & key stats tab: quarter rebuilding from year-to-date figures, beta, ranges, key stats; ladder labels."""

from __future__ import annotations

from datetime import date, timedelta

from oracle_edge import db
from oracle_edge.ui import quote
from oracle_edge.ui.theme import _tick_groups, ladder_html

CIK = 880001


def _facts():
    con = db.get()
    con.execute("DELETE FROM qfacts WHERE cik=?", (CIK,))
    con.execute("DELETE FROM facts WHERE cik=?", (CIK,))
    q = [  # 10-Q cash flow is year to date; revenue has 3-month figures
        ("ocf", "2025-01-01", "2025-03-31", 100), ("ocf", "2025-01-01", "2025-06-30", 250),
        ("ocf", "2025-01-01", "2025-09-30", 420),
        ("revenue", "2025-01-01", "2025-03-31", 1000), ("revenue", "2025-04-01", "2025-06-30", 1100),
        ("revenue", "2025-07-01", "2025-09-30", 1200), ("revenue", "2025-01-01", "2025-09-30", 3300),
        ("equity", "", "2025-09-30", 5000),
    ]
    con.executemany("INSERT INTO qfacts(cik,item,start,end,value,filed,form) VALUES(?,?,?,?,?,?,'10-Q')",
                    [(CIK, i, s, e, v, "2025-11-01") for i, s, e, v in q])
    con.executemany("INSERT INTO facts(cik,item,fy_end,value,filed,form,unit) VALUES(?,?,?,?,?,'10-K','USD')",
                    [(CIK, "ocf", "2025-12-31", 600, "2026-02-15"), (CIK, "revenue", "2025-12-31", 4600, "2026-02-15"),
                     (CIK, "net_income", "2025-12-31", 460, "2026-02-15"), (CIK, "equity", "2025-12-31", 5200, "2026-02-15")])


def test_quarters_rebuilt_from_year_to_date_and_annual():
    _facts()
    q = quote.quarterly(CIK)
    assert q["ocf"] == {"2025-03-31": 100, "2025-06-30": 150, "2025-09-30": 170, "2025-12-31": 180}
    assert q["revenue"]["2025-06-30"] == 1100 and q["revenue"]["2025-12-31"] == 1300  # Q4 = year - 9 months
    assert q["equity"] == {"2025-09-30": 5000, "2025-12-31": 5200}
    ends, s = quote.statements(CIK, "quarterly")
    assert ends == ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"] and s["revenue"][-1] == 1300
    ends, s = quote.statements(CIK, "annual")
    assert ends == ["2025-12-31"] and s["net_income"] == [460]


def _bars(n, step=1.001, start=date(2020, 1, 1), noise=0.0):
    out, d, px, k = [], start, 100.0, 0
    while len(out) < n:
        if d.weekday() < 5:
            px *= step * (1 + (noise if k % 2 else -noise))
            out.append({"date": d.isoformat(), "open": px, "high": px * 1.01, "low": px * 0.99, "close": px,
                        "adj": px, "volume": 1000 + k})
            k += 1
        d += timedelta(days=1)
    return out


def test_beta_and_ranges():
    spy = _bars(1500, 1.0004, noise=0.01)
    lev = [dict(b, adj=b["adj"] ** 2 / 100) for b in spy]  # roughly twice the moves
    b = quote.beta(lev, spy)
    assert b is not None and 1.7 < b < 2.3
    assert abs(quote.beta(spy, spy) - 1) < 1e-9
    assert quote._since(spy, 0) == 0
    i = quote._since(spy, 365)
    assert (date.fromisoformat(spy[-1]["date"]) - date.fromisoformat(spy[i]["date"])).days <= 365
    labs = [r[0] for r in quote.period_returns(spy)]
    assert labs == ["1D", "5D", "1M", "6M", "YTD", "1Y", "5Y"]


def test_key_stats_and_chart_options():
    _facts()
    con = db.get()
    con.execute("INSERT OR REPLACE INTO securities(cik,ticker,name,is_foreign,currency) VALUES(?,?,?,0,'USD')",
                (CIK, "QTST", "Quote Test"))
    con.execute("INSERT OR REPLACE INTO shares_out(cik,as_of,value,filed) VALUES(?,?,?,?)", (CIK, "2026-01-31", 100.0, "2026-02-15"))
    bars = _bars(400)
    st = quote.key_stats({"cik": CIK, "ticker": "QTST", "is_foreign": 0, "last_filed": "2026-02-15"}, None, bars, bars)
    assert st["market_cap"] == bars[-1]["close"] * 100
    assert st["wk52_high"] >= st["close"] >= st["wk52_low"]
    assert abs(st["net_margin"] - 0.1) < 1e-9
    rows = dict((r[0], r[1]) for r in quote.stat_rows(st))
    assert rows["Market cap"].startswith("$") and "Previous close" in rows
    opt = quote.chart_options(bars, bars, 365)
    assert [s["name"] for s in opt["series"]] == ["Price", "50-day avg", "200-day avg", "Volume"]
    assert len(opt["xAxis"][0]["data"]) == len(opt["series"][0]["data"]) == len(opt["series"][1]["data"])
    opt = quote.chart_options(bars, bars, 183, compare=True)
    assert [s["name"] for s in opt["series"]][:2] == ["This stock", "S&P 500"] and opt["series"][0]["data"][0] == 0


def test_ladder_labels_do_not_overlap():
    html = ladder_html(111.22, 116, 116, 215, 323)
    assert "strong buy · buy below" in html and html.count("$116") == 1
    html = ladder_html(100, 110, 118, 215, 323)  # close but different prices: two rows
    assert "top:76px" in html and 'height:92px' in html
    assert [len(g) for g in _tick_groups([(1, 0, ""), (5, 0, ""), (40, 0, ""), (75, 0, "")])] == [2, 1, 1]
