"""Total return calculator: what an investment would have grown to, dividends reinvested (the stock page's
Total return tab, in the spirit of totalrealreturns.com).

An initial amount bought on the first trading day on or after the start date, plus optional recurring contributions
(weekly, monthly or yearly, each bought at the first trading day on or after its date), all held to the end date.
Prices are dividend-adjusted closes, so dividends are reinvested on their ex-dates. With consumer prices (CPI-U),
every figure can also be shown in start-date dollars ("after inflation").
"""

from __future__ import annotations

import bisect
from datetime import date, timedelta

FREQS = {"none": "No recurring contributions", "weekly": "Every week", "monthly": "Every month", "yearly": "Every year"}


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue
    return date(y, m, 28)


def schedule(start: date, end: date, freq: str) -> list[date]:
    """Contribution dates after the start, up to the end (the initial amount is on the start date itself)."""
    out: list[date] = []
    if freq not in ("weekly", "monthly", "yearly"):
        return out
    k = 1
    while True:
        d = (start + timedelta(weeks=k) if freq == "weekly" else
             _add_months(start, k) if freq == "monthly" else _add_months(start, 12 * k))
        if d > end:
            return out
        out.append(d)
        k += 1


def xirr(flows: list[tuple[date, float]]) -> float | None:
    """Money-weighted yearly return: the rate at which the dated cash flows (money in negative, the end value
    positive) sum to zero. Bisection on [-99%, +1000%]."""
    if not flows or not any(f < 0 for _, f in flows) or not any(f > 0 for _, f in flows):
        return None
    t0 = flows[0][0]

    def npv(r):
        return sum(f / (1 + r) ** ((d - t0).days / 365.25) for d, f in flows)
    lo, hi = -0.99, 10.0
    flo, fhi = npv(lo), npv(hi)
    if flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = npv(mid)
        if (fm > 0) == (flo > 0):
            lo, flo = mid, fm
        else:
            hi = mid
    return (lo + hi) / 2


def simulate(series: list[tuple[str, float]], start: str, end: str, initial: float, amount: float = 0.0,
             freq: str = "none", cpi: list[tuple[str, float]] | None = None) -> dict | None:
    """series: (date, dividend-adjusted close), ascending. Returns None when there are no prices in the window.

    Result: dates and values (the holding's worth each trading day), invested (money put in so far), the final
    value, total invested, gain, total return, the stock's own yearly return over the period (CAGR, time-weighted,
    what a single lump sum earned), your yearly return (money-weighted IRR, which counts when each contribution went
    in), the worst fall from a high, and the same in start-date dollars when cpi is given."""
    ds = [d for d, _ in series]
    i0 = bisect.bisect_left(ds, start)
    i1 = bisect.bisect_right(ds, end)
    if i0 >= i1:
        return None
    win = [(d, p) for d, p in series[i0:i1] if p and p > 0]
    if len(win) < 2:
        return None
    wd = [d for d, _ in win]
    first, last = date.fromisoformat(wd[0]), date.fromisoformat(wd[-1])
    buys: dict[int, float] = {0: initial} if initial > 0 else {}
    for c in schedule(first, last, freq):
        j = bisect.bisect_left(wd, c.isoformat())
        if j < len(wd) and amount > 0:
            buys[j] = buys.get(j, 0.0) + amount
    shares = invested = 0.0
    values, inv, peak, dd = [], [], 0.0, 0.0
    flows: list[tuple[date, float]] = []
    for k, (d, p) in enumerate(win):
        if k in buys:
            shares += buys[k] / p
            invested += buys[k]
            flows.append((date.fromisoformat(d), -buys[k]))
        values.append(shares * p)
        inv.append(invested)
        peak = max(peak, p)
        dd = min(dd, p / peak - 1)
    if invested <= 0:
        return None
    years = max((last - first).days / 365.25, 1e-9)
    final = values[-1]
    out = {"dates": wd, "values": values, "invested": inv, "start": wd[0], "end": wd[-1], "years": years,
           "final": final, "total_invested": invested, "gain": final - invested, "total_return": final / invested - 1,
           "cagr": (win[-1][1] / win[0][1]) ** (1 / years) - 1, "irr": xirr(flows + [(last, final)]),
           "max_drawdown": dd, "contributions": len(buys)}
    if cpi:
        cd = [d for d, _ in cpi]

        def level(d: str) -> float | None:
            j = bisect.bisect_right(cd, d) - 1
            return cpi[j][1] if j >= 0 else None
        base = level(wd[0])
        if base:
            defl = [base / (level(d) or base) for d in wd]
            real = [v * f for v, f in zip(values, defl)]
            real_inv, acc = [], 0.0
            for k in range(len(wd)):  # each contribution in start-date dollars
                acc += buys.get(k, 0.0) * defl[k]
                real_inv.append(acc)
            infl = 1 / defl[-1] - 1
            out.update(real_values=real, real_invested=real_inv, real_final=real[-1],
                       real_cagr=(1 + out["cagr"]) / (1 + infl) ** (1 / years) - 1 if years else None,
                       real_irr=None if out["irr"] is None else (1 + out["irr"]) / (1 + infl) ** (1 / years) - 1,
                       inflation=infl)
    return out


def weekly(dates: list[str], *cols: list[float]) -> tuple[list[str], list[list[float]]]:
    """Thin a daily series to one point a week (the last trading day of each week) for charting."""
    keep = []
    for k, d in enumerate(dates):
        if k + 1 == len(dates) or date.fromisoformat(dates[k + 1]).isocalendar()[:2] != \
                date.fromisoformat(d).isocalendar()[:2]:
            keep.append(k)
    return [dates[k] for k in keep], [[c[k] for k in keep] for c in cols]
