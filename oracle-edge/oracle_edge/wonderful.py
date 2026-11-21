"""Wonderfulness: how wonderful a business is, on its own, with no regard to its share price.

Built from the November 2026 study on the app's own data (Guide, "Wonderfulness"). Every company-year from 2012 to
2022 was followed for three more years. What best predicted that a company would still be earning high returns on
capital and growing three years later was, in order: its returns on capital (level, consistency, and the return on
the capital it added), its financial strength, and its growth record (how fast, and how steadily, sales grew).
Past growth alone predicted future growth only weakly, as the research literature also finds; high returns on
capital persisted far more. The score therefore leans on returns on capital.

Each measure is turned into a percentile among every scored (standard, non-financial) company at the same time,
so a score of 90 means "better than about 90% of companies on these measures"; pillars are weighted averages of
their measures' percentiles; the score is the weighted average of the pillars, 0-100.

Pillars (weights): moat 40% (returns on capital and pricing power), growth 30% (record and consistency),
strength 20% (debt, interest, steadiness, cash), allocation 10% (the $1 test and the share count).
"""

from __future__ import annotations

import bisect
import json
import math
import re
from collections import defaultdict
from datetime import date

from . import db, rules

WEIGHTS = {"moat": 0.40, "growth": 0.30, "strength": 0.20, "allocation": 0.10}
PILLAR_NAMES = {"moat": "Moat: returns on capital and pricing power", "growth": "Growth record",
                "strength": "Financial strength and steadiness", "allocation": "Capital allocation (management)"}
# (feature, weight; negative = lower is better)
PILLARS = {
    "moat": [("roic_last", 1), ("roic_med", 1), ("roic_years", 1), ("roic_incr", 1), ("roic_min", 1),
             ("roe_last", 0.5), ("gm_med", 1), ("gm_years", 0.5)],
    "growth": [("rev_g5", 1), ("rev_g10", 0.5), ("rev_up_share", 1), ("predictability", 1), ("oe_growth", 0.5),
               ("eps_growth", 0.5), ("rev_g1", 1)],
    "strength": [("debt_payback", -1), ("interest_burden", -1), ("interest_years", 0.5), ("margin_volatility", -0.5),
                 ("cash_conversion", 0.5), ("profitable_years", 1), ("cash_to_debt", 0.5)],
    "allocation": [("dollar_test", 1), ("share_change", -1), ("share_cagr", -0.5)],
}
# how each feature is shown: (label, format, what "good" looks like)
FEATURES = {
    "roic_last": ("Return on invested capital, latest year", "pct", "15%+"),
    "roic_med": ("Return on invested capital, 10-year median", "pct", "15%+"),
    "roic_years": ("Years with ROIC at the 15% target", "share", "most years"),
    "roic_incr": ("Return on the capital added (incremental ROIC)", "pct", "15%+"),
    "roic_min": ("Worst-year ROIC", "pct", "10%+"),
    "roe_last": ("Return on equity, latest year", "pct", "15%+"),
    "gm_med": ("Gross margin, 10-year median (pricing power)", "pct", "40%+"),
    "gm_years": ("Years with gross margin at the 40% target", "share", "most years"),
    "rev_g5": ("Sales growth, 5 years a year", "pct", "8%+"),
    "rev_g10": ("Sales growth, 10 years a year", "pct", "8%+"),
    "rev_up_share": ("Years sales grew", "share", "8 of 10+"),
    "predictability": ("Earnings predictability", "share", "80%+"),
    "oe_growth": ("Owner-earnings growth a year", "pct", "8%+"),
    "eps_growth": ("Earnings-per-share growth a year", "pct", "8%+"),
    "rev_g1": ("Sales growth, latest year", "pct", "above 0"),
    "debt_payback": ("Years of earnings to repay long-term debt", "years", "under 3"),
    "interest_burden": ("Interest as a share of operating income", "pct", "under 15%"),
    "interest_years": ("Years with low interest burden", "share", "most years"),
    "margin_volatility": ("Net-margin swings (std. dev.)", "pct", "under 3 points"),
    "cash_conversion": ("Cash conversion (cash flow ÷ profit)", "pct", "90%+"),
    "profitable_years": ("Profitable years", "share", "every year"),
    "cash_to_debt": ("Cash ÷ debt", "x", "1x+"),
    "dollar_test": ("$1 test: market value created per $1 kept", "x", "$1+"),
    "share_change": ("Share count change a year", "pct", "flat or falling"),
    "share_cagr": ("Diluted shares, 10-year change a year", "pct", "flat or falling"),
}
RULE_MAP = {  # feature -> (rule id, field)
    "roic_last": ("r20", "last"), "roic_med": ("r20", "value"), "roic_years": ("r20", "consistency"),
    "roic_min": ("r20", "min"), "roe_last": ("r19", "last"), "gm_med": ("r1", "value"), "gm_years": ("r1", "consistency"),
    "predictability": ("r22", "value"), "oe_growth": ("r17", "value"), "eps_growth": ("r8", "value"),
    "debt_payback": ("r10", "value"), "interest_burden": ("r5", "value"), "interest_years": ("r5", "consistency"),
    "margin_volatility": ("r23", "value"), "cash_conversion": ("r18", "value"), "profitable_years": ("r9", "value"),
    "cash_to_debt": ("r11", "value"), "dollar_test": ("r21", "value"), "share_change": ("r14", "value"),
}
TIERS = [(80, "Exceptional"), (70, "Wonderful"), (60, "Good"), (45, "Average"), (0, "Weak")]


def tier(score: float | None) -> str:
    if score is None:
        return "Not scored"
    return next(name for lo, name in TIERS if score >= lo)


def _cagr(a, b, n):
    if a is None or b is None or a <= 0 or b <= 0 or n <= 0:
        return None
    return (b / a) ** (1 / n) - 1


def _near(series: dict[str, float], fy: str, years: float):
    t = date.fromisoformat(fy)
    best = None
    for k, v in series.items():
        if abs((date.fromisoformat(k) - t).days - round(365.25 * years)) <= 45:
            best = v
    return best


def features(rules_json: list | str, val_inputs: dict | str, fy: str, revenue: dict[str, float]) -> dict:
    """The measures for one company at one annual report. revenue: fy_end -> annual revenue (any filing)."""
    rj = {r["id"]: r for r in (json.loads(rules_json) if isinstance(rules_json, str) else rules_json or [])}
    vi = json.loads(val_inputs) if isinstance(val_inputs, str) else (val_inputs or {})
    f: dict[str, float | None] = {}
    for k, (rid, field) in RULE_MAP.items():
        r = rj.get(rid)
        if not r:
            continue
        ser = [v for v in (r.get("series") or []) if v is not None]
        if field == "last":
            f[k] = ser[-1] if ser else None
        elif field == "min":
            f[k] = min(ser) if len(ser) >= 4 else None
        else:
            f[k] = r.get(field)
    f["roic_incr"] = vi.get("incr_roic")
    f["share_cagr"] = vi.get("share_cagr")
    r0 = revenue.get(fy)
    f["revenue"] = r0
    f["rev_g1"] = _cagr(_near(revenue, fy, -1), r0, 1)
    f["rev_g5"] = _cagr(_near(revenue, fy, -5), r0, 5)
    f["rev_g10"] = _cagr(_near(revenue, fy, -10), r0, 10)
    past = [(k, v) for k, v in sorted(revenue.items()) if k <= fy][-11:]
    if len(past) >= 6:
        ups = [b[1] > a[1] for a, b in zip(past, past[1:])]
        f["rev_up_share"] = sum(ups) / len(ups)
        f["rev_up"], f["rev_years"] = sum(ups), len(ups)
    return {k: v for k, v in f.items() if v is not None and not (isinstance(v, float) and math.isnan(v))}


def score_cross_section(feats: dict[int, dict]) -> dict[int, dict]:
    """Percentiles within the group, pillar scores, the score and the rank."""
    keys = {k for _, items in PILLARS.items() for k, _ in items}
    sorted_vals = {k: sorted(f[k] for f in feats.values() if k in f) for k in keys}
    out = {}
    for cik, f in feats.items():
        pct = {}
        for k in keys:
            vals = sorted_vals[k]
            if k not in f or len(vals) < 10:
                continue
            r = (bisect.bisect_left(vals, f[k]) + bisect.bisect_right(vals, f[k])) / 2 / len(vals)
            sign = next(w for items in PILLARS.values() for kk, w in items if kk == k)
            pct[k] = r if sign > 0 else 1 - r
        pillars = {}
        for p, items in PILLARS.items():
            have = [(pct[k], abs(w)) for k, w in items if k in pct]
            if sum(w for _, w in have) >= 0.5 * sum(abs(w) for _, w in items):
                pillars[p] = 100 * sum(v * w for v, w in have) / sum(w for _, w in have)
        if "moat" not in pillars or len(pillars) < 2:
            continue
        # a pillar without enough data counts as average (50), so a gap never lifts a company up the ranking
        missing = [p for p in PILLARS if p not in pillars]
        full = {p: pillars.get(p, 50.0) for p in PILLARS}
        out[cik] = {"score": sum(full[p] * WEIGHTS[p] for p in PILLARS), "pillars": pillars, "pct": pct,
                    "feats": {**f, "missing": missing} if missing else f}
    order = sorted(out, key=lambda c: -out[c]["score"])
    for i, c in enumerate(order):
        out[c]["rank"], out[c]["n"] = i + 1, len(order)
    return out


def _revenue_by_cik() -> dict[int, dict[str, float]]:
    rev: dict[int, dict[str, float]] = defaultdict(dict)
    for r in db.rows("SELECT cik, fy_end, value FROM facts WHERE item='revenue' AND value IS NOT NULL ORDER BY filed"):
        rev[r["cik"]][r["fy_end"]] = r["value"]
    return rev


_FIN_NAME = re.compile(r"\b(bank|banco|bancorp|bancshares|bankshares|banca|financial group|insurance|assurance)\b",
                       re.I)


def financial_ciks() -> set[int]:
    """Banks, lenders, brokers and insurers: their returns on capital and debt can't be compared with an operating
    business's, so they aren't given a Wonderfulness score (the bank and insurer rulebooks judge them instead)."""
    out = set()
    for r in db.rows("SELECT cik, sic, name, category FROM securities"):
        sic = r["sic"] or 0
        if r["category"] in ("bank", "insurer") or 6000 <= sic <= 6211 or 6300 <= sic <= 6411 or \
                _FIN_NAME.search(r["name"] or ""):
            out.add(r["cik"])
    lend = defaultdict(set)
    for r in db.rows("SELECT DISTINCT cik, item FROM facts WHERE item IN ('deposits','loans','net_charge_offs')"):
        lend[r["cik"]].add(r["item"])
    out |= {c for c, s in lend.items() if "deposits" in s or {"loans", "net_charge_offs"} <= s}
    return out


HISTORY_VERSION = 2
"""Bumped when the year-by-year scores need recomputing. v2 (0.15.0): years ending in the first week of January
(52/53-week fiscal years) are labelled with the year they mostly cover, so two years no longer share a label."""


def fy_label(fy_end: str) -> str:
    """'FY2019' for a fiscal year ending 2019-12-28 and also for one ending 2020-01-02 (a 53-week year)."""
    y, m, d = int(fy_end[:4]), int(fy_end[5:7]), int(fy_end[8:10])
    return f"FY{y - 1 if m == 1 and d <= 7 else y}"


def compute(profile=None, history: bool = True, progress=None) -> int:
    """Score every standard company: 'now' (each company's latest annual report) and, with history, each
    fiscal year (companies compared with others reporting the same fiscal year). Stored in the wonder table."""
    p = profile or rules.default_profile()
    pid, ver = p.snap
    rev = _revenue_by_cik()
    rows = db.rows("SELECT cik, filed, fy_end, rules_json, val_inputs, routed FROM snapshots WHERE profile_id=? AND "
                   "profile_version=? AND fy_end IS NOT NULL ORDER BY cik, filed", (pid, ver))
    fin = financial_ciks()
    by_cik: dict[int, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        if (r["routed"] or "") in ("bank", "insurer") or r["cik"] in fin:
            continue
        by_cik[r["cik"]].setdefault(r["fy_end"], dict(r))  # the report that first covered the year
    latest = {cik: fys[max(fys)] for cik, fys in by_cik.items()}
    # for the current score use the newest filing of the latest year (e.g. an amended 10-K)
    for r in rows:
        if r["cik"] in latest and r["fy_end"] == latest[r["cik"]]["fy_end"] and r["filed"] > latest[r["cik"]]["filed"]:
            latest[r["cik"]] = dict(r)
    groups: dict[str, dict[int, dict]] = {"now": {}}
    filed: dict[tuple[str, int], tuple[str, str]] = {}
    for cik, s in latest.items():
        f = features(s["rules_json"], s["val_inputs"], s["fy_end"], rev.get(cik, {}))
        if f:
            groups["now"][cik] = f
            filed[("now", cik)] = (s["fy_end"], s["filed"])
    for cik, mc in market_caps(latest).items():
        if cik in groups["now"]:
            groups["now"][cik]["mcap"] = mc
    if history:
        for cik, fys in by_cik.items():
            for fy, s in sorted(fys.items()):
                c = fy_label(fy)
                if (c, cik) in filed:  # two reports for one fiscal year (a changed year end): keep the first
                    continue
                f = features(s["rules_json"], s["val_inputs"], fy, rev.get(cik, {}))
                if f:
                    groups.setdefault(c, {})[cik] = f
                    filed[(c, cik)] = (fy, s["filed"])
    n = 0
    with db.tx() as con:
        con.execute("DELETE FROM wonder WHERE profile_id=?" + ("" if history else " AND cohort='now'"), (pid,))
        for k, (c, feats) in enumerate(sorted(groups.items())):
            if len(feats) < 30:
                continue
            res = score_cross_section(feats)
            con.executemany(
                "INSERT OR REPLACE INTO wonder(cik,profile_id,cohort,as_of,filed,score,rank,n,pillars_json,feats_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                [(cik, pid, c, filed[(c, cik)][0], filed[(c, cik)][1], x["score"], x["rank"], x["n"],
                  json.dumps(x["pillars"]), json.dumps({"f": x["feats"], "pct": x["pct"]})) for cik, x in res.items()])
            n += len(res)
            if progress:
                progress((k + 1) / len(groups))
    if history:
        db.set_cfg("wonder_history_version", HISTORY_VERSION)
    return n


def history_stale() -> bool:
    return int(db.cfg("wonder_history_version", 1) or 1) < HISTORY_VERSION


def market_caps(latest: dict[int, dict]) -> dict[int, float]:
    """Market value today: the latest close x diluted shares from the latest annual report, on today's share basis.
    Foreign filers use the evaluation's figure (it converts ADRs), when the live run has one."""
    from .splits import Splits

    sec = {r["cik"]: r for r in db.rows("SELECT cik, ticker, is_foreign FROM securities")}
    last = {r["ticker"]: r["close"] for r in db.rows(
        "SELECT p.ticker, p.close FROM prices p JOIN (SELECT ticker, MAX(date) d FROM prices GROUP BY ticker) m "
        "ON p.ticker=m.ticker AND p.date=m.d")}
    ev = {}
    rid = db.scalar("SELECT id FROM runs WHERE kind='live' ORDER BY id DESC LIMIT 1")
    if rid:
        d = db.scalar("SELECT MAX(date) FROM evaluations WHERE run_id=?", (rid,))
        ev = {r["cik"]: r["m"] for r in db.rows("SELECT cik, json_extract(detail,'$.mcap') m FROM evaluations "
                                                 "WHERE run_id=? AND date=?", (rid, d)) if r["m"]}
    sp = Splits()
    out = {}
    for cik, s in latest.items():
        if cik in ev:
            out[cik] = ev[cik]
            continue
        info = sec.get(cik)
        if not info or info["is_foreign"]:
            continue
        sh = (json.loads(s["val_inputs"] or "{}") if isinstance(s["val_inputs"], str) else s["val_inputs"] or {}).get(
            "shares")
        px = last.get(info["ticker"])
        if sh and px:
            out[cik] = px * sh * sp.factor(info["ticker"], s["filed"])
    return out


def ensure(profile=None) -> None:
    p = profile or rules.default_profile()
    if not db.scalar("SELECT COUNT(*) FROM wonder WHERE profile_id=? AND cohort='now'", (p.snap[0],), 0):
        compute(p, history=False)  # quick; the year-by-year history comes with the next daily update


def get(cik: int, profile=None) -> dict | None:
    p = profile or rules.default_profile()
    r = db.one("SELECT * FROM wonder WHERE cik=? AND profile_id=? AND cohort='now'", (cik, p.snap[0]))
    if not r:
        return None
    x = dict(r)
    x["pillars"] = json.loads(x.pop("pillars_json"))
    fj = json.loads(x.pop("feats_json"))
    x["f"], x["pct"] = fj["f"], fj["pct"]
    x["tier"] = tier(x["score"])
    return x


def _quality_by_year(prof, cik: int | None = None) -> dict[int, dict[str, float]]:
    """Quality (the Oracle checklist score) per company and fiscal-year label, from the newest report for each year."""
    q: dict[int, dict[str, tuple[str, float]]] = defaultdict(dict)
    sql = ("SELECT cik, fy_end, filed, quality FROM snapshots WHERE profile_id=? AND profile_version=? AND fy_end IS NOT "
           "NULL AND quality IS NOT NULL" + (" AND cik=?" if cik else ""))
    for r in db.rows(sql, (*prof.snap, cik) if cik else prof.snap):
        lab = fy_label(r["fy_end"])
        old = q[r["cik"]].get(lab)
        if old is None or r["filed"] > old[0]:
            q[r["cik"]][lab] = (r["filed"], r["quality"])
    return {c: {k: v[1] for k, v in d.items()} for c, d in q.items()}


def history(cik: int, profile=None) -> list[dict]:
    """Year by year: Wonderfulness (against the companies reporting that year), its rank and pillars, Quality and the
    average of the two. The latest year shows today's score, so the line ends where the ranking does."""
    p = profile or rules.default_profile()
    rows = db.rows("SELECT cohort, as_of, filed, score, rank, n, pillars_json FROM wonder WHERE cik=? AND profile_id=? "
                   "AND (cohort LIKE 'FY%' OR cohort='now') ORDER BY as_of, cohort", (cik, p.snap[0]))
    qual = _quality_by_year(p, cik).get(cik, {})
    now = next((r for r in rows if r["cohort"] == "now"), None)
    out: dict[str, dict] = {}
    for r in rows:
        if r["cohort"] == "now":
            continue
        out[r["cohort"]] = {"fy": r["cohort"], "fy_end": r["as_of"], "filed": r["filed"], "score": r["score"],
                            "rank": r["rank"], "n": r["n"], "now": False, **json.loads(r["pillars_json"])}
    if now:
        lab = fy_label(now["as_of"])
        out[lab] = {"fy": lab, "fy_end": now["as_of"], "filed": now["filed"], "score": now["score"], "rank": now["rank"],
                    "n": now["n"], "now": True, **json.loads(now["pillars_json"])}
    for lab, x in out.items():
        x["quality"] = qual.get(lab)
        x["combined"] = (x["score"] + x["quality"]) / 2 if x["quality"] is not None else None
    return [out[k] for k in sorted(out)]


def summarize(series: list[tuple[str, float]], years: int = 3) -> dict:
    """Trend of a yearly score: change over the last `years` fiscal years, direction and consistency."""
    if not series:
        return {}
    last_lab, last = series[-1]
    y0 = int(last_lab[2:]) - years
    base = next(((lab, v) for lab, v in series if int(lab[2:]) >= y0), series[0])
    chg = last - base[1] if base[0] != last_lab else None
    direction = ("new" if chg is None else "up" if chg >= 5 else "down" if chg <= -5 else "flat")
    first_exc = next((lab for lab, v in series if v >= 80), None)
    return {"chg": chg, "since": base[0], "dir": direction, "years": len(series),
            "years_70": sum(1 for _, v in series if v >= 70), "first_80": first_exc,
            "low": min(v for _, v in series), "high": max(v for _, v in series)}


def trends(profile=None) -> dict[int, dict]:
    """For every scored company: its yearly Wonderfulness and Quality (latest year = today's score) and a trend
    summary. One pass over the tables, for the ranking page."""
    p = profile or rules.default_profile()
    w: dict[int, dict[str, float]] = defaultdict(dict)
    now: dict[int, tuple[str, float]] = {}
    for r in db.rows("SELECT cik, cohort, as_of, score FROM wonder WHERE profile_id=? AND (cohort LIKE 'FY%' OR "
                     "cohort='now')", (p.snap[0],)):
        if r["cohort"] == "now":
            now[r["cik"]] = (fy_label(r["as_of"]), r["score"])
        else:
            w[r["cik"]][r["cohort"]] = r["score"]
    qual = _quality_by_year(p)
    out = {}
    for cik, (lab, sc) in now.items():
        ws = w.get(cik, {})
        ws = {k: v for k, v in ws.items() if k <= lab}  # a cohort newer than today's year can't happen; be safe
        ws[lab] = sc
        wser = sorted(ws.items())
        qser = sorted((k, v) for k, v in qual.get(cik, {}).items() if k <= lab)
        out[cik] = {"w": wser, "q": qser, **summarize(wser), "q_chg": summarize(qser).get("chg")}
    return out


# ---------------------------------------------------------------- strengths and red flags
def _fmt(v, f) -> str:
    if v is None:
        return "—"
    if f == "pct":
        return f"{v * 100:.1f}%" if abs(v) < 0.1 else f"{v * 100:.0f}%"
    if f == "share":
        return f"{v * 100:.0f}%"
    if f == "years":
        return f"{v:.1f} yrs" if v < 90 else "never (losses)"
    if f == "x":
        return f"{v:.2f}x"
    return f"{v:g}"


# (feature, good test, flag test, what would need to change)
CHECKS = [
    ("roic_med", lambda v: v >= 0.20, lambda v: v < 0.12,
     "Return on capital needs to reach 15%+ and stay there for years; that is how a moat shows up in the numbers."),
    ("roic_years", lambda v: v >= 0.9, lambda v: v < 0.5,
     "High returns need to be the rule, not the exception: 15%+ in most years."),
    ("roic_incr", lambda v: v >= 0.20, lambda v: v < 0.10,
     "New investment needs to earn 15%+; growth that earns less than that destroys value."),
    ("gm_med", lambda v: v >= 0.45, lambda v: v < 0.25,
     "A thin gross margin means little pricing power; it needs a product customers will pay up for (40%+ is typical "
     "of a moat)."),
    ("rev_g5", lambda v: v >= 0.10, lambda v: v < 0.03,
     "Sales need to grow faster than inflation, ideally 8%+ a year, for the business to compound."),
    ("rev_up_share", lambda v: v >= 0.9, lambda v: v < 0.7,
     "Sales need to grow in most years; frequent declines point to a cyclical or shrinking business."),
    ("predictability", lambda v: v >= 0.9, lambda v: v < 0.6,
     "Earnings need to be steady enough to forecast; erratic profits make the future hard to judge."),
    ("rev_g1", lambda v: v >= 0.10, lambda v: v < 0.0,
     "Last year's sales fell: the next reports need to show growth returning."),
    ("debt_payback", lambda v: v <= 1.0, lambda v: v > 4.0,
     "Long-term debt needs to come down to under 3-4 years of earnings."),
    ("interest_burden", lambda v: v <= 0.05, lambda v: v > 0.15,
     "Interest needs to take a smaller bite of operating income (under 15%)."),
    ("profitable_years", lambda v: v >= 1.0, lambda v: v < 0.9,
     "A wonderful business makes money every year; losses need to stop for good."),
    ("margin_volatility", lambda v: v <= 0.01, lambda v: v > 0.05,
     "Profit margins swing a lot; they need to settle into a steady range."),
    ("cash_conversion", lambda v: v >= 1.0, lambda v: v < 0.8,
     "Reported profit needs to turn into cash (cash flow at least 90% of net income)."),
    ("dollar_test", lambda v: v >= 2.0, lambda v: v < 1.0,
     "Each dollar management keeps needs to create at least a dollar of market value."),
    ("share_change", lambda v: v <= -0.01, lambda v: v > 0.01,
     "The share count needs to stop growing; dilution hands owners a smaller slice each year."),
]


def explain(w: dict) -> tuple[list[dict], list[dict]]:
    """Strengths (what sets it apart) and red flags (with what would need to change), most decisive first."""
    f, pct = w.get("f", {}), w.get("pct", {})
    strengths, flags = [], []
    for k, good, bad, change in CHECKS:
        if k not in f:
            continue
        v = f[k]
        label, fmt, target = FEATURES[k]
        p = pct.get(k)
        val = _fmt(v, fmt)
        if k == "rev_up_share" and f.get("rev_years"):
            val = f"{f['rev_up']} of {f['rev_years']} years"
        if good(v) or (p is not None and p >= 0.9 and not bad(v)):
            strengths.append({"key": k, "label": label, "value": val, "pct": p,
                              "text": f"{label}: {val}" + (f", better than {p * 100:.0f}% of companies" if p else "")})
        elif bad(v):
            flags.append({"key": k, "label": label, "value": val, "pct": p, "target": target, "change": change,
                          "text": f"{label}: {val} (wonderful: {target})"})
    strengths.sort(key=lambda s: -(s["pct"] or 0))
    flags.sort(key=lambda s: (s["pct"] or 0))
    return strengths, flags


def capital_record(cik: int, years: int = 10) -> dict | None:
    """What management did with the money over the last `years` annual reports (from SEC filings)."""
    items = ("net_income", "ocf", "capex", "dividends_paid", "buybacks", "acquisitions", "sbc", "revenue")
    vals: dict[str, dict[str, float]] = defaultdict(dict)
    for r in db.rows(f"SELECT item, fy_end, value FROM facts WHERE cik=? AND item IN ({','.join('?' * len(items))}) "
                     "ORDER BY filed", (cik, *items)):
        if r["value"] is not None:
            vals[r["item"]][r["fy_end"]] = r["value"]
    fys = sorted(vals.get("net_income", {}))[-years:]
    if len(fys) < 3:
        return None

    def tot(item):
        xs = [abs(vals[item][y]) if item in ("dividends_paid", "buybacks", "acquisitions", "capex") else vals[item][y]
              for y in fys if y in vals.get(item, {})]
        return sum(xs) if xs else None
    out = {"years": len(fys), "from": fys[0][:4], "to": fys[-1][:4]}
    for it in items:
        out[it] = tot(it)
    if out["ocf"] is not None and out["capex"] is not None:
        out["fcf"] = out["ocf"] - out["capex"]
    r = vals.get("revenue", {})
    out["revenue_added"] = (r.get(fys[-1]) - r.get(fys[0])) if r.get(fys[-1]) and r.get(fys[0]) else None
    return out
