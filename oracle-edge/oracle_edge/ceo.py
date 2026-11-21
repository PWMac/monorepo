"""CEO track record: how the company did for its owners while a chief executive was in charge.

Who and when come from Wikidata (sources/wikidata.py), checked against Yahoo's officer list, and can be corrected by
you (the ceo_manual table). The record itself is computed from the app's own data: SEC filings for the business and
adjusted prices for the stock, from the last annual report before the CEO started to the latest one in office.

Four parts, 25 points each, on fixed scales so any two CEOs compare directly:
  Owners' return   the stock's yearly total return minus the S&P 500's (-10 points = 0, even = 50, +10 = 100)
  Growth           sales growth a year and profit-per-share growth a year (0% = 0, 15%+ = 100; averaged)
  Returns          average ROIC in office (5% = 0, 25%+ = 100) and its change vs the 3 years before (-10 pts = 0,
                   unchanged = 50, +10 pts = 100)
  Capital          market value created per $1 kept ($0 = 0, $1 = 50, $2+ = 100), the return on capital added
                   (0% = 0, 20%+ = 100) and the share count a year (+3% = 0, flat = 50, -3% = 100)
Grade: A 80+, B 65-80, C 50-65, D 35-50, F under 35. Needs at least two full fiscal years in office.

Caveats shown with every rating: results also reflect the business the CEO inherited, the industry and the market;
a short tenure is mostly luck; the data start in 2009-2010, so a long tenure is judged on the years since.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date

from . import db

ITEMS = ("revenue", "net_income", "dividends_paid", "equity", "lt_debt", "cash", "acquisitions", "shares_diluted")


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def _years(a: str, b: str) -> float:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days / 365.25


def _annual(cik: int) -> dict[str, dict[str, tuple[float, str]]]:
    """item -> fy_end -> (value as first filed, filed)."""
    out: dict[str, dict[str, tuple[float, str]]] = defaultdict(dict)
    for r in db.rows(f"SELECT item, fy_end, value, filed FROM facts WHERE cik=? AND item IN "
                     f"({','.join('?' * len(ITEMS))}) ORDER BY filed", (cik, *ITEMS)):
        if r["value"] is not None and r["fy_end"] not in out[r["item"]]:
            out[r["item"]][r["fy_end"]] = (r["value"], r["filed"])
    return out


def _roic_by_year(cik: int) -> dict[str, float]:
    """ROIC per fiscal year from the latest annual score's 10-year series (default profile)."""
    from . import rules

    p = rules.default_profile()
    out: dict[str, float] = {}
    for row in db.rows("SELECT rules_json FROM snapshots WHERE cik=? AND profile_id=? AND profile_version=? ORDER BY filed",
                       (cik, *p.snap)):
        for r in json.loads(row["rules_json"] or "[]"):
            if r.get("id") == "r20":
                for y, v in zip(r.get("years") or [], r.get("series") or []):
                    if v is not None:
                        out[y] = v  # later reports overwrite with their (restated) figure
    return out


def _px(ticker: str, d: str, after: bool = False):
    """(adjusted close, close, date) on the last trading day up to d, or with after=True the first one from d on."""
    q = ("SELECT date, adj_close, close FROM prices WHERE ticker=? AND date>=? AND (adj_close>0 OR close>0) "
         "ORDER BY date LIMIT 1") if after else (
        "SELECT date, adj_close, close FROM prices WHERE ticker=? AND date<=? AND (adj_close>0 OR close>0) "
        "ORDER BY date DESC LIMIT 1")
    r = db.one(q, (ticker, d))
    return (r["adj_close"] or r["close"], r["close"], r["date"]) if r else (None, None, None)


def record(cik: int, ticker: str, start: str, end: str | None = None) -> dict:
    """The company's record from the last annual report before `start` to the latest one up to `end` (or today)."""
    from .splits import Splits

    end = end or date.today().isoformat()
    a = _annual(cik)
    fys = sorted(a.get("net_income", {}))
    base = [y for y in fys if y <= start]
    inside = [y for y in fys if start < y <= end]
    out: dict = {"start": start, "end": end}
    if not inside:
        out["note"] = "no annual report yet in office"
        return out
    y0 = base[-1] if base else None
    y1 = inside[-1]
    if y0 is None:
        y0 = inside[0]
        out["note"] = "the data start after the CEO did, so the record begins with the first year on file"
        inside = inside[1:]
    out["fy0"], out["fy1"] = y0, y1
    n = _years(y0, y1)
    out["years"] = n
    sp = Splits([ticker])

    def v(item, y):
        x = a.get(item, {}).get(y)
        return x[0] if x else None

    def shares(y):
        x = a.get("shares_diluted", {}).get(y)
        return x[0] * sp.factor(ticker, x[1]) if x else None  # on today's share basis

    def cagr(x0, x1):
        return (x1 / x0) ** (1 / n) - 1 if x0 and x1 and x0 > 0 and x1 > 0 and n > 0 else None

    out["rev_cagr"] = cagr(v("revenue", y0), v("revenue", y1))
    out["ni_cagr"] = cagr(v("net_income", y0), v("net_income", y1))
    s0, s1 = shares(y0), shares(y1)
    out["share_cagr"] = cagr(s0, s1)
    if s0 and s1 and v("net_income", y0) and v("net_income", y1):
        out["nips_cagr"] = cagr(v("net_income", y0) / s0, v("net_income", y1) / s1)
    # returns on capital
    roic = _roic_by_year(cik)
    during = [roic[y] for y in roic if y0 < y <= y1]
    before = [roic[y] for y in sorted(roic) if y <= y0][-3:]
    out["roic_during"] = sum(during) / len(during) if during else None
    out["roic_before"] = sum(before) / len(before) if before else None
    # capital allocation
    retained = 0.0
    have = False
    for y in [y for y in fys if y0 < y <= y1]:
        ni = v("net_income", y)
        if ni is not None:
            retained += ni - abs(v("dividends_paid", y) or 0.0)
            have = True

    def ic(y):
        e = v("equity", y)
        return None if e is None else e + (v("lt_debt", y) or 0.0) - (v("cash", y) or 0.0)
    d_ni = (v("net_income", y1) or 0) - (v("net_income", y0) or 0) if v("net_income", y1) is not None and \
        v("net_income", y0) is not None else None
    i0, i1 = ic(y0), ic(y1)
    if d_ni is not None and i0 is not None and i1 is not None:
        d_ic = i1 - i0
        out["incr_return"] = (d_ni / d_ic) if d_ic > 0 else (1.0 if d_ni > 0 else None)
    out["acquisitions"] = sum(abs(v("acquisitions", y) or 0.0) for y in fys if y0 < y <= y1)
    r0, r1 = v("revenue", y0), v("revenue", y1)
    out["revenue_added"] = (r1 - r0) if r0 is not None and r1 is not None else None
    # the stock: total return vs the S&P 500, and market value created per dollar kept
    p0, c0, d0 = _px(ticker, start, after=True)
    p1, c1, d1 = _px(ticker, end)
    b0, _, _ = _px("SPY", d0 or start, after=True)
    b1, _, _ = _px("SPY", d1 or end)
    if p0 and p1 and b0 and b1 and d0 and d1 and _years(d0, d1) >= 0.5:
        t = _years(d0, d1)
        out["tsr"] = (p1 / p0) ** (1 / t) - 1
        out["spy"] = (b1 / b0) ** (1 / t) - 1
        out["excess"] = out["tsr"] - out["spy"]
        out["price_from"], out["price_to"] = d0, d1
    if have and retained > 0 and c0 and c1 and s0 and s1:
        # market value at the start and end (split-adjusted close x shares on the same basis)
        out["dollar_test"] = (c1 * s1 - c0 * s0) / retained
    out["rating"] = rate(out)
    return out


def rate(m: dict) -> dict | None:
    if (m.get("years") or 0) < 2:
        return None
    parts = {}
    if m.get("excess") is not None:
        parts["owners"] = _clamp(50 + 500 * m["excess"])
    g = [x for x in (m.get("rev_cagr"), m.get("nips_cagr") if m.get("nips_cagr") is not None else m.get("ni_cagr"))
         if x is not None]
    if g:
        parts["growth"] = sum(_clamp(100 * x / 0.15) for x in g) / len(g)
    r = []
    if m.get("roic_during") is not None:
        r.append(_clamp(100 * (m["roic_during"] - 0.05) / 0.20))
        if m.get("roic_before") is not None:
            r.append(_clamp(50 + 500 * (m["roic_during"] - m["roic_before"])))
    if r:
        parts["returns"] = sum(r) / len(r)
    c = []
    if m.get("dollar_test") is not None:
        c.append(_clamp(50 * m["dollar_test"]))
    if m.get("incr_return") is not None:
        c.append(_clamp(100 * m["incr_return"] / 0.20))
    if m.get("share_cagr") is not None:
        c.append(_clamp(50 - 1667 * m["share_cagr"]))
    if c:
        parts["capital"] = sum(c) / len(c)
    if len(parts) < 3:
        return None
    score = sum(parts.values()) / len(parts)
    grade = "A" if score >= 80 else "B" if score >= 65 else "C" if score >= 50 else "D" if score >= 35 else "F"
    return {"score": score, "grade": grade, "parts": parts}


def career(tenures: list[dict]) -> dict | None:
    """A CEO's career grade: each rated tenure's score weighted by the years it covers (a 10-year record counts five
    times a 2-year one), and each part weighted the same way. tenures: [{years, rating}, ...]."""
    rated = [t for t in tenures if t.get("rating") and (t.get("years") or 0) > 0]
    if not rated:
        return None
    yrs = sum(t["years"] for t in rated)
    score = sum(t["rating"]["score"] * t["years"] for t in rated) / yrs
    parts: dict[str, float] = {}
    for k in ("owners", "growth", "returns", "capital"):
        w = [(t["rating"]["parts"][k], t["years"]) for t in rated if k in t["rating"]["parts"]]
        if w:
            parts[k] = sum(v * y for v, y in w) / sum(y for _, y in w)
    grade = "A" if score >= 80 else "B" if score >= 65 else "C" if score >= 50 else "D" if score >= 35 else "F"
    return {"score": score, "grade": grade, "years": yrs, "n": len(rated), "parts": parts,
            "weights": [t["years"] / yrs for t in rated]}


def career_tenures(cik: int, ticker: str, cur: dict, led: list[dict]) -> list[dict]:
    """Every CEO tenure of the current CEO: this one first, then other companies (and any earlier stint here) from
    Wikidata. Each gets its record and rating where the app has the company's data."""
    out = []
    m = record(cik, ticker, cur["start"])
    out.append({"label": "this company", "ticker": ticker, "start": cur["start"], "end": None, "current": True,
                "years": m.get("years") if m.get("rating") else None, "rating": m.get("rating"), "record": m})
    for c in led:
        if not c.get("start"):
            continue
        here = c.get("cik") == cik or (c.get("ticker") or "").upper() == ticker.upper()
        if here and (not c.get("end") or abs(_years(c["start"], cur["start"])) < 1.1):
            continue  # the current tenure itself
        t = None
        if c.get("cik"):
            t = db.scalar("SELECT ticker FROM securities WHERE cik=?", (c["cik"],))
        if not t and c.get("ticker"):
            t = db.scalar("SELECT ticker FROM securities WHERE ticker=?", (c["ticker"].upper(),))
        x = {"label": c.get("label") or "?", "ticker": t, "start": c["start"], "end": c.get("end"), "current": False}
        if not t:
            x["why"] = "not in the app's data (private, foreign or no longer listed)"
        elif c.get("end") and c["end"] < "2011-01-01":
            x["why"] = "before the app's data (2010)"
        else:
            ccik = db.scalar("SELECT cik FROM securities WHERE ticker=?", (t,))
            mm = record(ccik, t, c["start"], c.get("end"))
            x.update(record=mm, rating=mm.get("rating"), years=mm.get("years") if mm.get("rating") else None)
            if not mm.get("rating"):
                x["why"] = mm.get("note") or "under two fiscal years of data"
        out.append(x)
    return out


# ---------------------------------------------------------------- who is the CEO
def manual(cik: int) -> dict | None:
    r = db.one("SELECT name, start FROM ceo_manual WHERE cik=?", (cik,))
    return dict(r) if r and r["start"] else None


def set_manual(cik: int, name: str, start: str | None) -> None:
    if not start:
        db.get().execute("DELETE FROM ceo_manual WHERE cik=?", (cik,))
        return
    date.fromisoformat(start)  # validate
    db.get().execute("INSERT OR REPLACE INTO ceo_manual(cik, name, start, updated_at) VALUES(?,?,?,?)",
                     (cik, (name or "").strip(), start, db.now()))


def _same_person(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    last = lambda s: [w for w in s.replace(".", " ").replace(",", " ").lower().split() if len(w) > 1]  # noqa: E731
    la, lb = last(a), last(b)
    return bool(la and lb and (la[-1] in lb or lb[-1] in la))


def who(cik: int, ticker: str, yahoo_officers: list[dict] | None = None) -> dict:
    """The current CEO and the company's CEO history.
    Returns {current: {name, start, source, qid?}, history: [...], conflict: str?}."""
    from .sources import wikidata

    hist = wikidata.ceos(cik, ticker)
    ycea = next((o for o in yahoo_officers or [] if o.get("title") and
                 ("chief executive" in o["title"].lower() or "ceo" in o["title"].lower().replace(",", " ").split())),
                None)
    out: dict = {"history": hist or [], "wikidata_ok": hist is not None, "yahoo_ceo": ycea["name"] if ycea else None}
    m = manual(cik)
    if m:
        out["current"] = {"name": m["name"] or (ycea or {}).get("name") or "CEO", "start": m["start"], "source": "you"}
        return out
    cur = [h for h in hist or [] if not h["end"] or h["end"] >= date.today().isoformat()]
    cur = sorted(cur, key=lambda h: h["start"] or "0000")
    if cur:
        c = cur[-1]
        out["current"] = {"name": c["name"], "start": c["start"], "qid": c["qid"], "source": "Wikidata"}
        if ycea and not _same_person(c["name"], ycea["name"]):
            out["conflict"] = (f"Yahoo lists {ycea['name']} as CEO but Wikidata has {c['name']}; Wikidata may be out "
                               "of date. Enter the right CEO and start date below.")
    elif ycea:
        out["current"] = {"name": ycea["name"], "start": None, "source": "Yahoo (no start date)"}
    return out


# ---------------------------------------------------------------- the grade, for the ranking page
def assess(cik: int, ticker: str, hint_start: str | None = None, refresh: bool = False,
           source: str | None = None) -> dict:
    """Who runs the company and their record: the CEO card's data (network: Yahoo officers, Wikidata). Saves the
    grade for the ranking page, and records a change of CEO.

    refresh: look again now (drop the cached Wikidata answer and Yahoo profile). hint_start: the filing date of an
    SEC 8-K reporting an officer change; when Yahoo then names a different CEO than Wikidata (which often lags by
    weeks), Yahoo's name is taken, starting on that date."""
    from .sources import company, wikidata

    if refresh:
        db.get().execute("DELETE FROM ceo_cache WHERE key=?", (f"ceos:{cik}",))
    info = company.get(ticker, refresh=refresh)
    res = who(cik, ticker, (info or {}).get("officers"))
    cur = res.get("current")
    if hint_start and res.get("yahoo_ceo") and (cur or {}).get("source") != "you" and (
            not cur or not _same_person(cur.get("name"), res["yahoo_ceo"])):
        cur = {"name": res["yahoo_ceo"], "start": hint_start, "source": "Yahoo; start date from the SEC 8-K"}
        res["current"] = cur
        res.pop("conflict", None)
    res["change_source"] = source
    if cur and cur.get("start"):
        led = wikidata.companies_led(cur.get("qid"), None if cur.get("qid") else cur.get("name")) \
            if res.get("wikidata_ok") or cur.get("qid") else []
        res["tenures"] = career_tenures(cik, ticker, cur, led)
        res["record"] = res["tenures"][0]["record"]
        res["career"] = career(res["tenures"])
    # save only a real answer: when Wikidata couldn't be reached, a missing CEO means "unknown", not "none"
    res["saved"] = bool(res.get("wikidata_ok") or (cur or {}).get("source") == "you")
    if res["saved"]:
        save_grade(cik, res)
    return res


def save_grade(cik: int, res: dict) -> None:
    """Keep the career grade (or this tenure's when the career can't be graded) for the ranking page; a different
    CEO from the one on record is logged as a change (and flagged in the daily email)."""
    cur = res.get("current") or {}
    prev = db.one("SELECT name FROM ceo_grades WHERE cik=?", (cik,))
    if prev and prev["name"] and cur.get("name") and not _same_person(prev["name"], cur["name"]):
        record_change(cik, prev["name"], cur["name"], cur.get("start"), res.get("change_source") or cur.get("source"))
    car = res.get("career")
    rec = (res.get("record") or {}).get("rating")
    g = car or rec
    note = ""
    if not cur.get("name"):
        note = "CEO not found"
    elif not cur.get("start"):
        note = "start date unknown"
    elif not g:
        note = "under two fiscal years in the role" if (res.get("record") or {}).get("years", 0) < 2 else "not enough data"
    db.get().execute(
        "INSERT OR REPLACE INTO ceo_grades(cik, name, start, grade, score, basis, years, note, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (cik, cur.get("name"), cur.get("start"), g["grade"] if g else None, g["score"] if g else None,
         "career" if car and car.get("n", 1) > 1 else "this company" if g else None,
         (car or {}).get("years") or (res.get("record") or {}).get("years"), note, db.now()))


def record_change(cik: int, old: str, new: str, start: str | None, source: str | None) -> None:
    from datetime import date as _d

    d = start or _d.today().isoformat()
    if db.scalar("SELECT 1 FROM ceo_changes WHERE cik=? AND new_name=?", (cik, new)):
        return
    db.get().execute("INSERT INTO ceo_changes(cik, date, old_name, new_name, source, created_at) VALUES(?,?,?,?,?,?)",
                     (cik, d, old, new, source or "", db.now()))
    t = db.scalar("SELECT ticker FROM securities WHERE cik=?", (cik,)) or str(cik)
    try:
        db.get().execute("INSERT INTO market_alerts(date, kind, state, message, created_at) VALUES(?,?,?,?,?)",
                         (_d.today().isoformat(), "ceo_change", "new",
                          f"{t}: new CEO {new} (was {old})" + (f", from {d}" if start else "") +
                          (f" · source: {source}" if source else ""), db.now()))
    except Exception:
        pass


def recent_changes(days: int = 365) -> dict[int, dict]:
    """Each company's latest CEO change within `days`: {cik: {date, old_name, new_name, source}}."""
    from datetime import date as _d, timedelta as _td

    since = (_d.today() - _td(days=days)).isoformat()
    out = {}
    for r in db.rows("SELECT * FROM ceo_changes WHERE date>=? ORDER BY date", (since,)):
        out[r["cik"]] = dict(r)
    return out


def check_changes(eightk: dict[int, str], annual: set[int] | None = None) -> dict:
    """The daily update's CEO step. eightk: companies that filed an 8-K since the last check (cik -> first filing
    date). Those we follow (graded, or Wonderfulness 60+) whose 8-K reports item 5.02 are looked up again now
    (fresh Wikidata and Yahoo). Companies with a new annual report are re-graded (their record changed)."""
    from datetime import date as _d, timedelta as _td

    from . import rules
    from .sources import sec

    p = rules.default_profile()
    followed = {r["cik"] for r in db.rows("SELECT cik FROM ceo_grades")} | {
        r["cik"] for r in db.rows("SELECT cik FROM wonder WHERE profile_id=? AND cohort='now' AND score>=60",
                                  (p.snap[0],))}
    tick = {r["cik"]: r["ticker"] for r in db.rows("SELECT cik, ticker FROM securities WHERE ticker IS NOT NULL")}
    out = {"checked_8k": 0, "officer_changes": 0, "regraded": 0, "changes": []}
    before = db.scalar("SELECT COALESCE(MAX(id), 0) FROM ceo_changes", default=0)
    for cik, first in sorted(eightk.items()):
        if cik not in followed or cik not in tick:
            continue
        out["checked_8k"] += 1
        try:
            since = (_d.fromisoformat(first) - _td(days=3)).isoformat()
            dates = sec.officer_changes(cik, since)
            if dates:
                out["officer_changes"] += 1
                assess(cik, tick[cik], hint_start=dates[0], refresh=True, source=f"SEC 8-K item 5.02 filed {dates[0]}")
        except Exception as e:
            db.log("ceo", "8-K check", False, f"{tick.get(cik)}: {e}"[:200])
    graded = {r["cik"] for r in db.rows("SELECT cik FROM ceo_grades")}
    for cik in sorted((annual or set()) & graded):
        if cik in tick:
            try:
                assess(cik, tick[cik])
                out["regraded"] += 1
            except Exception as e:
                db.log("ceo", "regrade", False, f"{tick.get(cik)}: {e}"[:200])
    out["changes"] = [dict(r) for r in db.rows("SELECT * FROM ceo_changes WHERE id>?", (before,))]
    db.set_cfg("last_ceo_check", {"at": db.now(), **{k: v for k, v in out.items() if k != "changes"},
                                  "changes": len(out["changes"])})
    return out


def grades() -> dict[int, dict]:
    return {r["cik"]: dict(r) for r in db.rows("SELECT * FROM ceo_grades")}


def to_rate(min_score: float = 60.0, max_age_days: int = 30, profile=None) -> list[tuple[int, str]]:
    """Companies on the ranking page worth a CEO grade (Wonderfulness >= min_score) that have none or an old one,
    best first."""
    from datetime import date, timedelta

    from . import rules

    p = profile or rules.default_profile()
    old = (date.today() - timedelta(days=max_age_days)).isoformat()
    return [(r["cik"], r["ticker"]) for r in db.rows(
        "SELECT w.cik, s.ticker FROM wonder w JOIN securities s ON s.cik=w.cik LEFT JOIN ceo_grades g ON g.cik=w.cik "
        "WHERE w.profile_id=? AND w.cohort='now' AND w.score>=? AND s.ticker IS NOT NULL "
        "AND (g.updated_at IS NULL OR g.updated_at < ?) ORDER BY w.score DESC", (p.snap[0], min_score, old))]


# ---------------------------------------------------------------- background grading (the Screener fills in as you watch)
import threading as _threading  # noqa: E402

_Q: list[tuple[int, str]] = []
_LOCK = _threading.Lock()
_STATE = {"running": False, "done": 0, "failed": 0, "current": None, "started": None}


def queue_grades(items: list[tuple[int, str]], front: bool = True) -> int:
    """Grade these companies' CEOs in a background thread, in order (front = before anything already waiting).
    Returns how many were added. Companies already waiting aren't added twice."""
    with _LOCK:
        waiting = {c for c, _ in _Q}
        new = [(c, t) for c, t in dict.fromkeys(items) if c not in waiting and t]
        if front:
            _Q[:0] = new
        else:
            _Q.extend(new)
        if new and not _STATE["running"]:
            _STATE.update(running=True, started=db.now())
            _threading.Thread(target=_worker, daemon=True, name="oe-ceo-grades").start()
    return len(new)


def _worker() -> None:
    import time

    while True:
        with _LOCK:
            if not _Q:
                _STATE.update(running=False, current=None)
                return
            cik, t = _Q.pop(0)
            _STATE["current"] = t
        try:
            if assess(cik, t).get("saved"):
                _STATE["done"] += 1
            else:
                _STATE["failed"] += 1
        except Exception as e:  # offline, or Wikidata/Yahoo changed: note it and carry on
            _STATE["failed"] += 1
            try:
                db.log("ceo", "grade", False, f"{t}: {e}"[:200])
            except Exception:
                pass
        time.sleep(0.2)  # be gentle with Wikidata


def grading_status() -> dict:
    with _LOCK:
        return {"pending": len(_Q), **_STATE}


def graded_since(ciks: list[int]) -> dict[int, dict]:
    """The stored grades of these companies (for refreshing the Screener)."""
    if not ciks:
        return {}
    out = {}
    for i in range(0, len(ciks), 500):
        chunk = ciks[i:i + 500]
        for r in db.rows(f"SELECT * FROM ceo_grades WHERE cik IN ({','.join('?' * len(chunk))})", chunk):
            out[r["cik"]] = dict(r)
    return out
