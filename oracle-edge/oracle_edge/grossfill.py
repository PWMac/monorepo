"""Gross margin for companies that report their cost of sales in pieces with no total.

Some companies (Cadence since 2016, for example) show "cost of product" and "cost of services" as separate lines
and no total. The SEC's company-facts file, the app's source, carries only undivided figures, so those companies
have no cost of sales and no gross margin from the SEC. Yahoo Finance adds the pieces up; for these companies
(and only these: companies that DID report a total cost of sales in earlier years) the daily update takes Yahoo's
yearly cost of revenue, checks Yahoo's sales against the SEC's for the same year (within 3%), and stores it as
that year's cost of sales, dated as filed with that year's annual report (which did contain the figures).

Companies that never report a cost of sales (payment networks, asset managers, many service firms) are left
alone: gross margin doesn't apply to them, and the app says so."""

from __future__ import annotations

import math
from datetime import date, timedelta

from . import db

COST_ITEMS = ("cogs", "gross_profit")


def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def parse_income(df) -> dict[str, dict]:
    """yfinance income statement (rows = line items, columns = period ends) -> {period_end: {revenue, cogs}}."""
    if df is None or getattr(df, "empty", True):
        return {}
    idx = {str(i).strip().lower(): i for i in df.index}

    def row(*names):
        for n in names:
            if n in idx:
                return idx[n]
        return None
    r_rev, r_cost, r_gp = row("total revenue", "operating revenue"), row("cost of revenue", "reconciled cost of revenue"), \
        row("gross profit")
    out = {}
    for col in df.columns:
        d = str(col)[:10]
        rev = _f(df.at[r_rev, col]) if r_rev is not None else None
        cost = _f(df.at[r_cost, col]) if r_cost is not None else None
        if cost is None and r_gp is not None and rev is not None:
            gp = _f(df.at[r_gp, col])
            cost = rev - gp if gp is not None else None
        if rev and cost is not None and 0 <= cost < rev:
            out[d] = {"revenue": rev, "cogs": cost}
    return out


def fetch(ticker: str) -> dict[str, dict]:  # pragma: no cover - network
    import yfinance as yf

    return parse_income(yf.Ticker(ticker).income_stmt)


def kind(cik: int) -> str:
    """'ok' (has a cost of sales for its latest year), 'gap' (reported one before, not lately) or 'none' (never)."""
    last = db.scalar("SELECT MAX(fy_end) FROM facts WHERE cik=? AND item='revenue'", (cik,))
    if last and db.scalar("SELECT 1 FROM facts WHERE cik=? AND fy_end=? AND item IN ('cogs','gross_profit')",
                          (cik, last)):
        return "ok"
    return "gap" if db.scalar("SELECT 1 FROM facts WHERE cik=? AND item IN ('cogs','gross_profit')", (cik,)) else "none"


def kinds() -> dict[int, str]:
    """kind() for every company with revenue, in two queries."""
    last = {r["cik"]: r["m"] for r in db.rows("SELECT cik, MAX(fy_end) m FROM facts WHERE item='revenue' GROUP BY cik")}
    have_last, have_any = set(), set()
    for r in db.rows("SELECT DISTINCT cik, fy_end FROM facts WHERE item IN ('cogs','gross_profit')"):
        have_any.add(r["cik"])
        if last.get(r["cik"]) == r["fy_end"]:
            have_last.add(r["cik"])
    return {c: ("ok" if c in have_last else "gap" if c in have_any else "none") for c in last}


def apply(cik: int, yahoo: dict[str, dict], tolerance: float = 0.03) -> int:
    """Store Yahoo's cost of revenue for the fiscal years the SEC data lacks one, when Yahoo's sales match the SEC's
    for that year. Returns the number of years added."""
    rev = {}
    for r in db.rows("SELECT fy_end, value, MIN(filed) filed, unit FROM facts WHERE cik=? AND item='revenue' "
                     "GROUP BY fy_end", (cik,)):
        rev[r["fy_end"]] = r
    have = {r["fy_end"] for r in db.rows("SELECT DISTINCT fy_end FROM facts WHERE cik=? AND item IN ('cogs',"
                                         "'gross_profit')", (cik,))}
    added = 0
    with db.tx() as con:
        for d, y in yahoo.items():
            fy = min(rev, key=lambda k: abs((date.fromisoformat(k) - date.fromisoformat(d)).days), default=None)
            if not fy or abs((date.fromisoformat(fy) - date.fromisoformat(d)).days) > 20 or fy in have:
                continue
            sec_rev = rev[fy]["value"]
            if not sec_rev or abs(y["revenue"] / sec_rev - 1) > tolerance:
                continue  # Yahoo's sales don't match the filing: don't trust its cost figure either
            con.execute("INSERT OR REPLACE INTO facts(cik,item,fy_end,value,filed,form,unit) VALUES(?,?,?,?,?,?,?)",
                        (cik, "cogs", fy, y["cogs"], rev[fy]["filed"], "YF", rev[fy]["unit"] or "USD"))
            added += 1
    return added


def targets(max_age_days: int = 30) -> list[tuple[int, str]]:
    """Companies with a cost-of-sales gap that weren't checked recently (or were filled and since lost it, e.g. a
    new filing re-read the company)."""
    old = (date.today() - timedelta(days=max_age_days)).isoformat()
    checked = {r["cik"]: r for r in db.rows("SELECT * FROM cost_fill")}
    tick = {r["cik"]: r["ticker"] for r in db.rows("SELECT cik, ticker FROM securities WHERE ticker IS NOT NULL AND "
                                                   "COALESCE(is_foreign,0)=0")}
    out = []
    for cik, k in kinds().items():
        if k != "gap" or cik not in tick:
            continue
        c = checked.get(cik)
        if c and c["checked_at"] >= old and not c["added"]:
            continue  # looked recently and Yahoo had nothing usable
        out.append((cik, tick[cik]))
    return out


def run(limit: int | None = 80, progress=None) -> list[int]:
    """Fill the gaps; returns the companies that got new figures (re-score them)."""
    todo = targets()[: limit or None]
    changed = []
    for i, (cik, t) in enumerate(todo):
        try:
            n = apply(cik, fetch(t))
            note = ""
        except Exception as e:
            n, note = 0, str(e)[:200]
        db.get().execute("INSERT OR REPLACE INTO cost_fill(cik, checked_at, added, note) VALUES(?,?,?,?)",
                         (cik, db.now(), n, note))
        if n:
            changed.append(cik)
        if progress and i % 5 == 0:
            progress(i / max(len(todo), 1))
    return changed
