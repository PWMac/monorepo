"""How accurate is the value? The value-driver model run on past annual reports, checked against what each company
turned out to be worth.

For every company with an annual report filed in 2013-2019 (one per company per year; not banks, insurers or foreign
filers) the model values the stock as it could have then: the report's figures and the price the day it was filed.
What it was really worth then ("hindsight value") is what an owner actually got from that day to today (the total
return, dividends reinvested, from adjusted prices), discounted back at the model's discount rate: if the stock
went on to return exactly that rate, it was worth exactly its price.

Reported: the typical gap between the value and the hindsight value, the same for the price itself (the market's
own estimate; a model can't expect to beat it stock by stock), how often the hindsight value fell inside the
low-high range, and the yearly return of stocks the model called cheap, about right and dear.

Needs price histories for the whole universe (Data -> Maintenance -> download every company's prices); with only
the shortlist's prices the sample leans toward today's winners."""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from statistics import median

from . import db, rules, wonderful
from .engine import split_scale
from .splits import Splits
from .valuation import value_driver

YEARS = range(2013, 2020)


def _price(ticker: str, d: str):
    return db.one("SELECT date, close, adj_close FROM prices WHERE ticker=? AND date>=? AND date<=? ORDER BY date LIMIT 1",
                  (ticker, d, (date.fromisoformat(d) + timedelta(days=10)).isoformat()))


def points(profile=None, progress=None) -> list[dict]:
    p = profile or rules.default_profile()
    fin = wonderful.financial_ciks()
    sec = {r["cik"]: r["ticker"] for r in db.rows("SELECT cik, ticker FROM securities WHERE ticker IS NOT NULL AND "
                                                  "COALESCE(is_foreign, 0)=0")}
    last = {r["ticker"]: dict(r) for r in db.rows(
        "SELECT p.ticker, p.date, p.adj_close FROM prices p JOIN (SELECT ticker, MAX(date) d FROM prices GROUP BY ticker) "
        "m ON p.ticker=m.ticker AND p.date=m.d")}
    recent = (date.today() - timedelta(days=30)).isoformat()
    sp = Splits()
    out, seen = [], set()
    snaps = db.rows("SELECT cik, filed, val_inputs, routed FROM snapshots WHERE profile_id=? AND profile_version=? AND "
                    "filed>=? AND filed<? ORDER BY filed", (*p.snap, f"{YEARS[0]}-01-01", f"{YEARS[-1] + 1}-01-01"))
    for i, s in enumerate(snaps):
        c, y = s["cik"], s["filed"][:4]
        t = sec.get(c)
        if not t or (c, y) in seen or c in fin or (s["routed"] or "") in ("bank", "insurer"):
            continue
        end = last.get(t)
        if not end or end["date"] < recent or not end["adj_close"]:
            continue
        p0 = _price(t, s["filed"])
        if not p0 or not p0["close"] or not p0["adj_close"]:
            continue
        vi = json.loads(s["val_inputs"] or "{}")
        if not vi.get("vd_sales"):
            continue
        seen.add((c, y))
        n = (date.fromisoformat(end["date"]) - date.fromisoformat(p0["date"])).days / 365.25
        out.append({"cik": c, "ticker": t, "year": y, "price": p0["close"], "tr": end["adj_close"] / p0["adj_close"],
                    "years": n, "vi": split_scale(vi, sp.factor(t, s["filed"]))})
        if progress and i % 500 == 0:
            progress(0.8 * i / max(len(snaps), 1))
    return out


def evaluate(pts: list[dict], st: dict) -> dict:
    """The accuracy figures for these points under settings st."""
    lv, lp, cover, hv, verdict, yrs = [], [], 0, [], {"cheap": [], "about right": [], "dear": []}, {}
    for x in pts:
        try:
            v = value_driver(x["price"], x["vi"], st, st.get("mos_gate", 0.25), {})
        except Exception:
            continue
        if not v.iv:
            continue
        r = v.extra["required_return"]
        h = x["price"] * x["tr"] / (1 + r) ** x["years"]
        if h <= 0:
            continue
        lv.append(math.log(v.iv / h))
        lp.append(math.log(x["price"] / h))
        hv.append(h / v.iv)
        cover += v.extra["low"] <= h <= v.extra["high"]
        q = v.iv / x["price"]
        k = "cheap" if q >= 1 / 0.75 else "dear" if q <= 0.75 else "about right"
        ret = x["tr"] ** (1 / x["years"]) - 1
        verdict[k].append(ret)
        yrs.setdefault(x["year"], []).append(q)
    if len(lv) < 30:
        return {"n": len(lv)}
    hv.sort()
    n = len(hv)
    within = lambda ls: sum(abs(z) <= math.log(1.3) for z in ls) / len(ls)  # noqa: E731
    return {"n": n, "rate": st.get("vd_required_return") or st.get("vd_discount_rate") or 0.09,
            "bias": math.exp(median(lv)), "miss": math.exp(median(abs(z) for z in lv)) - 1,
            "price_miss": math.exp(median(abs(z) for z in lp)) - 1, "within30": within(lv), "price_within30": within(lp),
            "range_coverage": cover / n, "middle_half": [hv[n // 4], hv[3 * n // 4]],
            "verdicts": {k: {"n": len(v), "return": median(v) if v else None} for k, v in verdict.items()},
            "value_to_price_by_year": {y: median(v) for y, v in sorted(yrs.items())}}


def run(profile=None, progress=None) -> dict:
    p = profile or rules.default_profile()
    pts = points(p, progress)
    res = evaluate(pts, dict(p.settings))
    res.update(at=db.now(), tickers=len({x["ticker"] for x in pts}))
    db.set_cfg("value_check", res)
    return res


def summary(res: dict | None) -> str:
    if not res or res.get("n", 0) < 30:
        return "Not enough past valuations with prices to check (download every company's prices first)."
    v = res["verdicts"]
    ret = lambda k: f"{v[k]['return']:.1%}" if v[k]["return"] is not None else "—"  # noqa: E731
    return (f"{res['n']:,} past valuations ({res['tickers']:,} companies, reports filed 2013–2019) against what each "
            f"company turned out to be worth, at a {res['rate']:.0%} discount rate. On average the value was "
            f"{abs(res['bias'] - 1):.0%} {'low' if res['bias'] < 1 else 'high'}. Stock by stock it typically missed by "
            f"{res['miss']:.0%} (the price itself missed by {res['price_miss']:.0%}); within 30% {res['within30']:.0%} "
            f"of the time (the price {res['price_within30']:.0%}). The hindsight value fell inside the low–high range "
            f"{res['range_coverage']:.0%} of the time; half the time it was between {res['middle_half'][0]:.2f}× and "
            f"{res['middle_half'][1]:.2f}× the value. Yearly return since: stocks the model called cheap (value a third "
            f"or more above the price) {ret('cheap')}, about right {ret('about right')}, dear (price a third or more "
            f"above the value) {ret('dear')}.")
