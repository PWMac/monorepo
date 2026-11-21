"""Stock splits: putting per-share figures from SEC filings on the same share basis as the prices.

Price sources back-adjust the whole price history whenever a stock splits, so every stored price is on today's
share basis. SEC filings are not: a filing states per-share figures (EPS) and share counts on the basis of the
day it was filed (a filing restates earlier years only for splits that happened before it was filed). Without a
conversion, a stock that later split 10:1 would look ten times cheaper in the history than it was, and a stock
that just split would look ten times cheaper until its next annual report.

Conventions: `ratio` is new shares per old share (10 for a 10-for-1 split, 0.1 for a 1-for-10 reverse split).
`factor(t, a, b)` = product of the ratios of splits on dates in (a, b]: multiply a share count stated on basis `a`
by it (or divide a per-share figure) to restate it on basis `b`.
"""

from __future__ import annotations

from bisect import bisect_right

from . import db

NICE = (2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 50, 1.5, 2.5, 1.25, 4 / 3)
"""Ratios a stock split normally has. A price-basis change near one of these is a split, not a dividend."""


class Splits:
    """All splits in memory (fast lookups during scoring and replays)."""

    def __init__(self, tickers: list[str] | None = None, load: bool = True):
        self.d: dict[str, tuple[list[str], list[float]]] = {}
        self.priced: set[str] = set()
        if not load:
            return
        # tickers whose split dates come from the price source (Yahoo reports every split); for the others,
        # splits are inferred from the filings themselves (see from_filings)
        self.priced = {r["ticker"] for r in db.rows("SELECT DISTINCT ticker FROM prices WHERE source='yahoo'")}
        q = "SELECT ticker, date, ratio FROM splits WHERE ratio > 0 AND ratio != 1"
        rows = db.rows(q + " ORDER BY ticker, date")
        want = set(tickers) if tickers is not None else None
        for r in rows:
            if want is not None and r["ticker"] not in want:
                continue
            a = self.d.setdefault(r["ticker"], ([], []))
            a[0].append(r["date"])
            a[1].append(float(r["ratio"]))

    def factor(self, ticker: str | None, after: str | None, upto: str | None = None) -> float:
        """Product of split ratios on dates in (after, upto]; upto=None means every split known."""
        a = self.d.get(ticker or "")
        if not a or not after:
            return 1.0
        i = bisect_right(a[0], after)
        j = len(a[0]) if upto is None else bisect_right(a[0], upto)
        f = 1.0
        for r in a[1][i:j]:
            f *= r
        return f

    def has(self, ticker: str | None) -> bool:
        return bool(self.d.get(ticker or ""))

    def for_company(self, ticker: str | None, facts) -> "Splits":
        """The splits to use for one company's filings: the price source's when it has the ticker's history,
        otherwise those the filings themselves show (restated share counts)."""
        if ticker and (ticker in self.priced or self.has(ticker)):
            return self
        found = from_filings(facts)
        if not found:
            return self
        local = Splits(load=False)
        local.d[ticker or ""] = ([d for d, _ in found], [r for _, r in found])
        return local


def from_filings(facts) -> list[tuple[str, float]]:
    """Splits seen in SEC data: a later filing restating an earlier year's diluted share count (or EPS) by a usual
    split ratio. Dated at the restating filing (the first point from which the filings use the new basis), which is
    all the per-share conversion of older filings needs. facts: rows with item, fy_end, value, filed."""
    by: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for f in facts:
        if f["item"] in ("shares_diluted", "eps_diluted") and f["value"]:
            by.setdefault((f["item"], f["fy_end"]), []).append((f["filed"], float(f["value"])))
    found: dict[str, float] = {}
    for (item, _fy), vals in by.items():
        vals.sort()
        for (f0, v0), (f1, v1) in zip(vals, vals[1:]):
            if f1 <= f0 or v0 == 0:
                continue
            r = (v1 / v0) if item == "shares_diluted" else (v0 / v1 if v1 else 0)
            if r > 0 and not (0.87 <= r <= 1.15):
                nr = nice_ratio(r, 0.04)
                if nr and f1 not in found:
                    found[f1] = nr
    # one split restates several years in the same filing; keep one entry per filing date
    return sorted(found.items())


def nice_ratio(r: float, tol: float = 0.03) -> float | None:
    """r (new/old shares) if it is close to a usual split ratio or its inverse (a reverse split), else None."""
    if r <= 0:
        return None
    for n in NICE:
        for cand in (n, 1.0 / n):
            if abs(r / cand - 1) <= tol:
                return cand
    return None


def store(ticker: str, rows: list[tuple[str, float]], source: str) -> int:
    """Insert splits (date, ratio). A split already known within 5 days keeps its original row."""
    known = {r["date"]: r for r in db.rows("SELECT date, ratio FROM splits WHERE ticker=?", (ticker,))}
    from datetime import date as _d

    new = []
    for d, ratio in rows:
        if not ratio or ratio == 1:
            continue
        near = [k for k in known if abs((_d.fromisoformat(k) - _d.fromisoformat(d)).days) <= 5]
        if near and source == "inferred":
            continue
        if near and source != "inferred":  # an exact split from the data source replaces an inferred one
            for k in near:
                if known[k] and k != d:
                    db.get().execute("DELETE FROM splits WHERE ticker=? AND date=? AND source='inferred'", (ticker, k))
        new.append((ticker, d, float(ratio), source))
    if new:
        db.get().executemany("INSERT OR REPLACE INTO splits(ticker,date,ratio,source) VALUES(?,?,?,?)", new)
    return len(new)
