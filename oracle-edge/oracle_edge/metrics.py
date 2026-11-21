"""Per-company metrics from normalized annual statements.

A statement is a dict: {"years": [fy_end,...] ascending, item: [value or None per year], ...}.
Each metric returns a MetricResult with an aggregate value, the yearly series (for consistency
checks and sparklines) and optional extra detail.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import median

CORE_ITEMS = ["revenue", "net_income", "equity", "ocf", "capex", "shares_diluted"]


@dataclass
class MetricResult:
    value: float | None
    series: list[float | None] = field(default_factory=list)
    years: list[str] = field(default_factory=list)
    detail: str = ""
    ok_years: float | None = None  # consistency override (share of years meeting a metric-specific test)


# ------------------------------------------------------------------ helpers
def _div(a, b):
    if a is None or b is None or b == 0:
        return None
    return a / b


def _clean(xs):
    return [x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]


def _med(xs):
    xs = _clean(xs)
    return median(xs) if xs else None


def _avg(xs):
    xs = _clean(xs)
    return sum(xs) / len(xs) if xs else None


def cagr(first, last, years):
    if first is None or last is None or years <= 0 or first <= 0 or last <= 0:
        return None
    return (last / first) ** (1 / years) - 1


def years_between(a: str, b: str) -> float:
    """Years between two fiscal-year ends, from the dates (a gap in the record isn't counted as one year)."""
    from datetime import date as _d

    return (_d.fromisoformat(b) - _d.fromisoformat(a)).days / 365.25


def _mid(dates: list[str]) -> str:
    from datetime import date as _d, timedelta as _td

    ds = [_d.fromisoformat(x) for x in dates]
    base = ds[0]
    return (base + _td(days=sum((x - base).days for x in ds) / len(ds))).isoformat()


def fiscal_year_ends(ends) -> list[tuple[str, list[str]]]:
    """Group fiscal-year end dates into fiscal years: ends within 20 days of each other are the same year reported
    with slightly different dates (52/53-week calendars, a tag using the 31st and another the 30th). Two years
    ending in the same calendar year (Jan 1 and Dec 31) stay separate. Returns [(canonical end = latest, members)]."""
    from datetime import date as _d

    groups: list[list[str]] = []
    for e in sorted(set(ends)):
        if groups and (_d.fromisoformat(e) - _d.fromisoformat(groups[-1][-1])).days <= 20:
            groups[-1].append(e)
        else:
            groups.append([e])
    return [(g[-1], g) for g in groups]


def slope(xs):
    pts = [(i, x) for i, x in enumerate(xs) if x is not None]
    if len(pts) < 3:
        return 0.0
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    den = sum((p[0] - mx) ** 2 for p in pts)
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / den if den else 0.0


def r2_loglinear(xs):
    pts = [(i, math.log(x)) for i, x in enumerate(xs) if x is not None and x > 0]
    if len(pts) < 4:
        return None
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    syy = sum((p[1] - my) ** 2 for p in pts)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
    if sxx == 0 or syy == 0:
        return 1.0 if syy == 0 else None
    r2 = sxy * sxy / (sxx * syy)
    # a falling trend is not "predictable growth"
    return r2 if sxy > 0 else r2 * 0.3


# ------------------------------------------------------------------ statement building
def build_statement(state: dict[tuple[str, str], float], lookback: int) -> dict:
    """state maps (item, fy_end) -> value (already point-in-time). Returns statement dict."""
    by_item: dict[str, dict[str, float]] = {}
    for (item, fy), v in state.items():
        by_item.setdefault(item, {})[fy] = v
    # one entry per fiscal year: near-duplicate ends (52/53-week calendars) merge, the latest date stands for the year
    groups = fiscal_year_ends(set(by_item.get("net_income", {})) | set(by_item.get("revenue", {})))[-lookback:]
    years = [canon for canon, _ in groups]
    s: dict = {"years": years}
    # each fiscal year owns the dates from 20 days before its first member to 20 days after its last; an item
    # reported on any of them belongs to that year (the canonical date, then other members, then nearest wins)
    from bisect import bisect_right
    from datetime import date as _d, timedelta as _td

    lo = [(_d.fromisoformat(g[0]) - _td(days=20)).isoformat() for _, g in groups]
    hi = [(_d.fromisoformat(g[-1]) + _td(days=20)).isoformat() for _, g in groups]
    members = [set(g) for _, g in groups]
    for item, m in by_item.items():
        best: dict[int, tuple[int, str, float]] = {}
        for k, v in m.items():
            if v is None:
                continue
            j = bisect_right(lo, k) - 1
            if j < 0 or k > hi[j]:
                continue
            pr = 2 if k == years[j] else (1 if k in members[j] else 0)
            cur = best.get(j)
            if cur is None or (pr, k) > (cur[0], cur[1]):
                best[j] = (pr, k, v)
        s[item] = [best[j][2] if j in best else None for j in range(len(years))]
    for item in ("revenue", "cogs", "gross_profit", "sga", "selling_marketing", "rnd", "da", "interest",
                 "operating_income", "pretax_income", "income_tax", "net_income", "eps_diluted", "shares_diluted",
                 "ocf", "capex", "dividends_paid", "buybacks", "cash", "st_investments", "lt_debt", "st_debt",
                 "total_assets", "total_liabilities", "current_assets", "current_liabilities", "equity",
                 "liabilities_and_equity", "treasury_stock", "preferred_stock", "retained_earnings",
                 "deposits", "loans", "premiums_earned"):
        s.setdefault(item, [None] * len(years))
    from .sources.sec import concept_map

    for item in concept_map():
        s.setdefault(item, [None] * len(years))
    n = len(years)
    fix_share_scale(s)  # on the reported figures, before anything is derived from the share count
    for i in range(n):
        if s["gross_profit"][i] is None and s["revenue"][i] is not None and s["cogs"][i] is not None:
            s["gross_profit"][i] = s["revenue"][i] - s["cogs"][i]
        if s["sga"][i] is None and (s["selling_marketing"][i] is not None or s["gna"][i] is not None):
            s["sga"][i] = (s["selling_marketing"][i] or 0.0) + (s["gna"][i] or 0.0)
        if s["sga"][i] is None and s["gross_profit"][i] is not None and s["operating_income"][i] is not None:
            # SG&A tagged only with a company-specific tag: gross profit - operating income - R&D. Anything else
            # between the two lines (amortization, restructuring) lands here too, so this errs on the high side.
            v = s["gross_profit"][i] - s["operating_income"][i] - abs(s["rnd"][i] or 0.0)
            if v > 0:
                s["sga"][i] = v
        if s["da"][i] is None and s["depreciation"][i] is not None:
            s["da"][i] = abs(s["depreciation"][i]) + abs(s["amortization"][i] or 0.0)
        if s["operating_income"][i] is None:
            # no operating-income line: revenue - total costs and expenses, else pre-tax income + interest (EBIT)
            if s["revenue"][i] is not None and s["costs_and_expenses"][i] is not None:
                s["operating_income"][i] = s["revenue"][i] - s["costs_and_expenses"][i]
            elif s["pretax_income"][i] is not None:
                s["operating_income"][i] = s["pretax_income"][i] + abs(s["interest"][i] or 0.0)
        if s["total_liabilities"][i] is None and s["liabilities_and_equity"][i] is not None and s["equity"][i] is not None:
            s["total_liabilities"][i] = s["liabilities_and_equity"][i] - s["equity"][i]
        for k in ("capex", "dividends_paid", "buybacks", "treasury_stock", "interest", "net_charge_offs",
                  "goodwill", "intangibles"):
            if s[k][i] is not None:
                s[k][i] = abs(s[k][i])
        if s["shares_diluted"][i] is None and s["net_income"][i] and s["eps_diluted"][i]:
            s["shares_diluted"][i] = s["net_income"][i] / s["eps_diluted"][i]
        if s["eps_diluted"][i] is None and s["net_income"][i] is not None and s["shares_diluted"][i]:
            s["eps_diluted"][i] = s["net_income"][i] / s["shares_diluted"][i]
    s["total_debt"] = [
        (a or 0) + (b or 0) if (a is not None or b is not None) else 0.0 for a, b in zip(s["lt_debt"], s["st_debt"])
    ]
    return s


SCALES = (1e3, 1e6, 1e-3, 1e-6)


def fix_share_scale(s: dict) -> int:
    """Some filers tag a year's share count in thousands or millions while the unit says shares (one company's
    273 million shares arrive as 273,298), which makes every per-share figure 1,000x off. Net income ÷ EPS gives
    the share count implied by the same filing: a reported count that is a thousand or million times that is
    rescaled; one that disagrees for another reason is replaced by the implied count. Years without EPS are
    checked against the other years' median. Returns how many years were corrected."""
    n = len(s["years"])
    sh, ni, eps = s["shares_diluted"], s["net_income"], s["eps_diluted"]
    fixed = 0
    for i in range(n):
        if not sh[i] or not ni[i] or eps[i] is None or abs(eps[i]) < 0.05:
            continue
        implied = ni[i] / eps[i]
        if implied <= 0:
            continue
        r = sh[i] / implied
        if 0.5 <= r <= 2.0:
            continue
        for k in SCALES:
            if 0.67 <= r / k <= 1.5:
                sh[i] = sh[i] / k
                break
        else:
            sh[i] = implied
        fixed += 1
    good = sorted(v for v in sh if v)
    if len(good) >= 3:
        med = good[len(good) // 2]
        for i in range(n):
            if sh[i] and not (0.1 <= sh[i] / med <= 10):
                for k in SCALES:
                    if 0.25 <= sh[i] / med / k <= 4:
                        sh[i] = sh[i] / k
                        fixed += 1
                        break
    return fixed


def data_years(s) -> int:
    return sum(1 for i in range(len(s["years"])) if all(s[k][i] is not None for k in ("revenue", "net_income", "equity")))


# ------------------------------------------------------------------ metrics
def compute(s: dict, fy_prices: dict[str, float] | None = None) -> dict[str, MetricResult]:
    y = s["years"]
    n = len(y)
    R = s["revenue"]
    GP = s["gross_profit"]
    NI = s["net_income"]
    EQ = s["equity"]
    out: dict[str, MetricResult] = {}

    def series(fn):
        return [fn(i) for i in range(n)]

    gm = series(lambda i: _div(GP[i], R[i]))
    out["gross_margin"] = MetricResult(_med(gm), gm, y)
    sga = series(lambda i: _div(s["sga"][i], GP[i]) if GP[i] and GP[i] > 0 else None)
    out["sga_to_gp"] = MetricResult(_med(sga), sga, y)
    rnd = series(lambda i: (_div(s["rnd"][i] or 0.0, GP[i]) if GP[i] and GP[i] > 0 else None))
    out["rnd_to_gp"] = MetricResult(_med(rnd), rnd, y)
    da = series(lambda i: _div(s["da"][i], GP[i]) if GP[i] and GP[i] > 0 else None)
    out["da_to_gp"] = MetricResult(_med(da), da, y)

    def _int(i):
        oi = s["operating_income"][i]
        it = s["interest"][i]
        if oi is None:
            return None
        if oi <= 0:
            return 1.0
        return (it or 0.0) / oi

    it = series(_int)
    out["interest_to_opinc"] = MetricResult(_med(it), it, y)

    def _tax(i):
        p, t = s["pretax_income"][i], s["income_tax"][i]
        if p is None or t is None or p <= 0:
            return None
        return t / p

    tx = series(_tax)
    out["tax_rate"] = MetricResult(_med(tx), tx, y)
    nm = series(lambda i: _div(NI[i], R[i]))
    out["net_margin"] = MetricResult(_med(nm), nm, y)

    eps = s["eps_diluted"]
    eps_clean = [e for e in eps if e is not None]
    g = None
    if len(eps_clean) >= 3:
        first_idx = next((i for i, e in enumerate(eps) if e is not None and e > 0), None)
        last_idx = max(i for i, e in enumerate(eps) if e is not None)
        if first_idx is not None and last_idx > first_idx:
            g = cagr(eps[first_idx], eps[last_idx], years_between(y[first_idx], y[last_idx]))
            if g is None and eps[last_idx] is not None and eps[last_idx] <= 0:
                g = -0.10
    ups = [1.0 if (eps[i] is not None and eps[i - 1] is not None and eps[i] > eps[i - 1]) else 0.0
           for i in range(1, n) if eps[i] is not None and eps[i - 1] is not None]
    out["eps_growth"] = MetricResult(g, eps, y, ok_years=(sum(ups) / len(ups)) if ups else None)

    prof = [1.0 if v is not None and v > 0 else 0.0 for v in NI if v is not None]
    out["profitable_years"] = MetricResult(sum(prof) / len(prof) if prof else None, NI, y,
                                           detail=f"{int(sum(prof))} of {len(prof)} years profitable")

    ni3 = _avg(NI[-3:])
    ltd = s["lt_debt"][-1] if n else None
    if ltd is None and n:
        ltd = s["total_debt"][-1] if s["st_debt"][-1] is None else (s["total_debt"][-1] - (s["st_debt"][-1] or 0))
    payback = None
    if ltd is not None and ni3 is not None:
        payback = (ltd / ni3) if ni3 > 0 else 99.0
        if ltd <= 0:
            payback = 0.0
    out["debt_payback"] = MetricResult(payback, [_div(s["lt_debt"][i], NI[i]) for i in range(n)], y)

    def _c2d(i):
        c = (s["cash"][i] or 0) + (s["st_investments"][i] or 0)
        d = s["total_debt"][i]
        if s["cash"][i] is None:
            return None
        return 10.0 if not d else c / d

    c2d = series(_c2d)
    out["cash_to_debt"] = MetricResult(c2d[-1] if c2d else None, c2d, y)

    def _ade(i):
        tl, e = s["total_liabilities"][i], EQ[i]
        if tl is None or e is None:
            return None
        base = e + (s["treasury_stock"][i] or 0)
        return tl / base if base > 0 else 9.9

    ade = series(_ade)
    out["adj_de"] = MetricResult(ade[-1] if ade else None, ade, y)

    re_ = s["retained_earnings"]
    pairs = [(re_[i - 1], re_[i]) for i in range(1, n) if re_[i] is not None and re_[i - 1] is not None]
    rg = (sum(1 for a, b in pairs if b > a) / len(pairs)) if pairs else None
    out["retained_growth"] = MetricResult(rg, re_, y)

    sh = s["shares_diluted"]
    sh_idx = [i for i, v in enumerate(sh) if v]
    sc = None
    if len(sh_idx) >= 3:
        sc = cagr(sh[sh_idx[0]], sh[sh_idx[-1]], years_between(y[sh_idx[0]], y[sh_idx[-1]]))
    out["share_change"] = MetricResult(sc, sh, y)

    pref = s["preferred_stock"][-1] if n else None
    out["has_preferred"] = MetricResult(1.0 if pref and pref > 0 else 0.0, s["preferred_stock"], y)

    capni = series(lambda i: _div(s["capex"][i], NI[i]) if NI[i] and NI[i] > 0 else (5.0 if s["capex"][i] else None))
    out["capex_to_ni"] = MetricResult(_med(capni), capni, y)

    oe = series(lambda i: (NI[i] + (s["da"][i] or 0) - (s["capex"][i] or 0)) if NI[i] is not None else None)
    oe_i = [i for i, v in enumerate(oe) if v is not None]
    oe_c = [oe[i] for i in oe_i]
    oeg = None
    if len(oe_c) >= 6:
        # average of the first three years vs the last three, compounded over the years between their middles
        a, b = _avg(oe_c[:3]), _avg(oe_c[-3:])
        span = years_between(_mid([y[i] for i in oe_i[:3]]), _mid([y[i] for i in oe_i[-3:]]))
        oeg = cagr(a, b, span) if a and b else None
        if oeg is None and b is not None and b <= 0:
            oeg = -0.10
    pos = [1.0 if v > 0 else 0.0 for v in oe_c]
    out["owner_earnings_growth"] = MetricResult(oeg, oe, y, ok_years=(sum(pos) / len(pos)) if pos else None)

    cc = series(lambda i: _div((s["ocf"][i] - (s["capex"][i] or 0)) if s["ocf"][i] is not None else None, NI[i])
                if NI[i] and NI[i] > 0 else None)
    out["cash_conversion"] = MetricResult(_med(cc), cc, y)

    def _eq(i):
        e = EQ[i]
        if e is None:
            return None
        if e <= 0 and (s["treasury_stock"][i] or 0) > 0:
            e = e + s["treasury_stock"][i]
        return e if e > 0 else None

    def _roe(i):
        e1 = _eq(i)
        e0 = _eq(i - 1) if i > 0 else None
        base = (e1 + e0) / 2 if (e1 and e0) else e1
        return _div(NI[i], base) if base else None

    roe = series(_roe)
    roe_c = _clean(roe)
    out["roe"] = MetricResult(_avg(roe), roe, y, detail=f"lowest year {min(roe_c):.1%}" if roe_c else "")
    out["roe"].min_year = min(roe_c) if roe_c else None  # type: ignore[attr-defined]

    def _roic(i):
        oi = s["operating_income"][i]
        if oi is None:
            return None
        t = tx[i] if tx[i] is not None else 0.21
        t = min(max(t, 0.0), 0.35)
        ic = (s["total_debt"][i] or 0) + (_eq(i) or 0) - (s["cash"][i] or 0)
        return _div(oi * (1 - t), ic) if ic > 0 else None

    roic = series(_roic)
    out["roic"] = MetricResult(_avg(roic), roic, y)

    # $1 test needs prices at fiscal year ends
    dt = None
    dt_note = ""
    if fy_prices:
        idx = [i for i in range(n) if y[i] in fy_prices and sh[i]]
        if len(idx) >= 3:
            i0, i1 = idx[0], idx[-1]
            mc0, mc1 = fy_prices[y[i0]] * sh[i0], fy_prices[y[i1]] * sh[i1]
            earned = sum((NI[i] or 0) for i in range(i0 + 1, i1 + 1))
            retained = sum((NI[i] or 0) - (s["dividends_paid"][i] or 0) for i in range(i0 + 1, i1 + 1))
            if retained > 0:
                dt = (mc1 - mc0) / retained
            elif earned <= 0:
                dt = 0.0 if mc1 <= mc0 else None  # lost money over the period: no retained dollar to grow
                dt_note = "lost money over the period"
            else:
                dt_note = "paid out everything it earned: nothing retained to test"
    out["dollar_test"] = MetricResult(dt, [], y, detail="" if dt is not None else (dt_note or "needs price history"))

    pr = [v for v in (r2_loglinear(R), r2_loglinear(eps)) if v is not None]
    out["predictability"] = MetricResult(_avg(pr), R, y)

    om = series(lambda i: _div(s["operating_income"][i], R[i]))
    omc = _clean(om)
    sd = None
    if len(omc) >= 4:
        mu = sum(omc) / len(omc)
        sd = math.sqrt(sum((x - mu) ** 2 for x in omc) / (len(omc) - 1))
    res = MetricResult(sd, om, y)
    res.trend = slope(om)  # type: ignore[attr-defined]
    out["margin_stability"] = res

    out["data_years"] = MetricResult(float(data_years(s)), [], y)
    return out


def valuation_inputs(s: dict, m: dict[str, MetricResult]) -> dict:
    """Price-independent inputs the daily valuation needs."""
    n = len(s["years"])
    if n == 0:
        return {}
    NI, sh, eps = s["net_income"], s["shares_diluted"], s["eps_diluted"]
    oe_ps = []
    for i in range(max(0, n - 5), n):
        if NI[i] is not None and sh[i]:
            oe_ps.append((NI[i] + (s["da"][i] or 0) - (s["capex"][i] or 0)) / sh[i])
    payouts = [_div(s["dividends_paid"][i], NI[i]) for i in range(n) if NI[i] and NI[i] > 0]
    payout = min(max(_avg(payouts) or 0.0, 0.0), 1.0)
    roe = m["roe"].value or 0.0
    eps3 = _avg([e for e in eps[-3:] if e is not None])
    ebitda = [(s["operating_income"][i] + (s["da"][i] or 0)) if s["operating_income"][i] is not None else None
              for i in range(n)]
    eb_i = [i for i in range(max(0, n - 6), n) if ebitda[i] is not None]
    ebitda_g = (cagr(ebitda[eb_i[0]], ebitda[eb_i[-1]], years_between(s["years"][eb_i[0]], s["years"][eb_i[-1]]))
                if len(eb_i) >= 3 else None)
    eq_last = s["equity"][-1]
    enh = enhanced_inputs(s, m)
    return {
        **enh,
        "oe_ps": _avg(oe_ps),
        "eps3": eps3,
        "eps_last": eps[-1],
        "eps_cagr": m["eps_growth"].value,
        "roe": roe,
        "payout": payout,
        "sust_g": roe * (1 - payout),
        "shares": sh[-1],
        "bvps": _div(eq_last, sh[-1]),
        "ebitda_g": ebitda_g,
        "eps_by_fy": {y: e for y, e in zip(s["years"], eps) if e is not None},
        "fy_end": s["years"][-1],
    }


# ------------------------------------------------------------------ enhanced valuation inputs
def nopat_and_capital(s: dict) -> tuple[list, list]:
    """After-tax operating profit and invested capital (debt + equity - cash) per year."""
    n = len(s["years"])
    nop, ic = [], []
    for i in range(n):
        oi = s["operating_income"][i]
        p, t = s["pretax_income"][i], s["income_tax"][i]
        tr = (t / p) if (p and t is not None and p > 0) else 0.21
        tr = min(max(tr, 0.0), 0.35)
        nop.append(oi * (1 - tr) if oi is not None else None)
        e = s["equity"][i]
        if e is not None and e <= 0 and (s["treasury_stock"][i] or 0) > 0:
            e = e + s["treasury_stock"][i]  # buybacks can push book equity below zero: add them back
        c = (s["total_debt"][i] or 0) + (e or 0) - (s["cash"][i] or 0)
        ic.append(c if (e is not None and c > 0) else None)
    return nop, ic


def incremental_roic(nop: list, ic: list, window: int, end: int | None = None) -> float | None:
    """Extra after-tax operating profit earned per extra dollar of capital over `window` years ending at `end`.
    Unlike ROE, this isn't flattered by buybacks shrinking equity."""
    end = (len(nop) - 1) if end is None else end
    a = end - window
    if a < 0 or nop[end] is None or nop[a] is None or ic[end] is None or ic[a] is None:
        return None
    d_ic = ic[end] - ic[a]
    d_nop = nop[end] - nop[a]
    if abs(d_ic) < 1e-9 or d_ic <= 0:
        # capital didn't grow: profits grew without needing more of it (excellent) or shrank (poor)
        return 0.50 if d_nop > 0 else (0.0 if d_nop == 0 else -0.10)
    return max(min(d_nop / d_ic, 1.0), -1.0)


def maintenance_capex(s: dict) -> list:
    """Capex needed just to stay in place (Greenwald method): total capex minus the capex that funded growth,
    where growth capex = average PP&E-to-sales × the increase in sales. Falls back to depreciation."""
    n = len(s["years"])
    R, cap, da, ppe = s["revenue"], s["capex"], s["da"], s.get("ppe_net", [None] * n)
    ratios = [ppe[i] / R[i] for i in range(n) if ppe[i] and R[i] and R[i] > 0]
    pr = _avg(ratios[-5:]) if ratios else None
    out = []
    for i in range(n):
        c = cap[i]
        if c is None:
            out.append(None)
            continue
        if pr is not None and i > 0 and R[i] is not None and R[i - 1] is not None:
            growth = pr * max(R[i] - R[i - 1], 0.0)
            out.append(min(max(c - growth, 0.0), c))
        else:
            out.append(min(c, da[i]) if da[i] is not None else c)
    return out


def enhanced_inputs(s: dict, m: dict) -> dict:
    n = len(s["years"])
    R, NI, sh = s["revenue"], s["net_income"], s["shares_diluted"]
    nop, ic = nopat_and_capital(s)
    inc5 = incremental_roic(nop, ic, min(5, n - 1)) if n >= 3 else None
    inc3 = [incremental_roic(nop, ic, 3, e) for e in range(n)]
    maint = maintenance_capex(s)
    sh_last = sh[-1] if n else None
    rev3 = _avg([r for r in R[-3:] if r is not None])
    rev_ps = (rev3 / sh_last) if (rev3 and sh_last) else None
    nm = [_div(NI[i], R[i]) for i in range(n)]
    nm_med = _med(nm)
    oe_enh = [(NI[i] + (s["da"][i] or 0) - (maint[i] or 0)) if NI[i] is not None else None for i in range(n)]
    oe_m = _med([_div(oe_enh[i], R[i]) for i in range(n)])
    sh_idx = [i for i, v in enumerate(sh) if v]
    dil = None
    if len(sh_idx) >= 3:
        k0 = sh_idx[max(0, len(sh_idx) - 6)]
        dil = cagr(sh[k0], sh[sh_idx[-1]], years_between(s["years"][k0], s["years"][sh_idx[-1]]))
    sbc = s.get("sbc", [None] * n)
    fcf = [(s["ocf"][i] - (s["capex"][i] or 0)) if s["ocf"][i] is not None else None for i in range(n)]
    sbc_fcf = [(_div(sbc[i], fcf[i]) if (sbc[i] is not None and fcf[i] and fcf[i] > 0) else None) for i in range(n)]
    buyback_payout = _avg([_div(s["buybacks"][i], NI[i]) for i in range(n) if NI[i] and NI[i] > 0])
    return {
        "roic_avg": m["roic"].value if "roic" in m else None,
        "incr_roic": inc5,
        "incr_roic_3y": inc3[-4:],
        "eps_norm": (nm_med * rev_ps) if (nm_med is not None and rev_ps) else None,
        "oe_norm_ps": (oe_m * rev_ps) if (oe_m is not None and rev_ps) else None,
        "maint_capex_share": _avg([_div(maint[i], s["capex"][i]) for i in range(n)
                                   if s["capex"][i] and maint[i] is not None]),
        "share_cagr": dil,
        "sbc_to_fcf": sbc_fcf[-1] if sbc_fcf else None,
        "buyback_payout": buyback_payout,
        "predictability": m["predictability"].value if "predictability" in m else None,
    }
