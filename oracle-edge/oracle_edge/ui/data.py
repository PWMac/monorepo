"""Read-side queries for the screens."""

from __future__ import annotations

import json
import math
from datetime import date, timedelta

from .. import db, rules
from ..market import Macro


def live_runs() -> list[dict]:
    return [dict(r) for r in db.rows(
        "SELECT r.*, p.name pname FROM runs r JOIN profiles p ON p.id=r.profile_id WHERE r.kind='live' ORDER BY r.id")]


def all_runs() -> list[dict]:
    return [dict(r) for r in db.rows(
        "SELECT r.*, p.name pname FROM runs r JOIN profiles p ON p.id=r.profile_id WHERE r.kind != 'sweep' "
        "ORDER BY r.kind DESC, r.id DESC")]


def default_run_id() -> int | None:
    act = rules.active_profiles()
    for p in act:
        r = db.one("SELECT id FROM runs WHERE kind='live' AND profile_id=?", (p.id,))
        if r:
            return r["id"]
    r = db.one("SELECT id FROM runs WHERE kind='live' ORDER BY id LIMIT 1")
    return r["id"] if r else None


def run(run_id) -> dict | None:
    r = db.one("SELECT r.*, p.name pname FROM runs r JOIN profiles p ON p.id=r.profile_id WHERE r.id=?", (run_id,))
    return dict(r) if r else None


def latest_date(run_id) -> str | None:
    return db.scalar("SELECT MAX(date) FROM evaluations WHERE run_id=?", (run_id,))


def evaluations(run_id, d) -> list[dict]:
    rows = db.rows(
        """SELECT e.*, s.ticker, s.name, s.exchange, s.sic_desc, b.badge, b.reason badge_reason,
                  n.outside_circle
           FROM evaluations e JOIN securities s ON s.cik=e.cik
           LEFT JOIN portfolios f ON f.run_id=e.run_id AND f.kind='model'
           LEFT JOIN badges b ON b.run_id=e.run_id AND b.portfolio_id=f.id AND b.date=e.date AND b.cik=e.cik
           LEFT JOIN user_notes n ON n.cik=e.cik
           WHERE e.run_id=? AND e.date=?""", (run_id, d))
    out = []
    for r in rows:
        x = dict(r)
        x["detail"] = json.loads(x["detail"] or "{}")
        out.append(x)
    return out


def actions(run_id, d) -> list[dict]:
    return [dict(r) for r in db.rows(
        """SELECT g.*, s.ticker, s.name, e.buy_below, e.strong_buy_below, e.iv, e.rank, e.rank_score, e.quality,
                  e.value AS value_score, e.mos, e.exp_return
           FROM signals g JOIN securities s ON s.cik=g.cik
           LEFT JOIN evaluations e ON e.run_id=g.run_id AND e.date=g.date AND e.cik=g.cik
           WHERE g.run_id=? AND g.date=? ORDER BY g.id""", (run_id, d))]


def holdings(run_id) -> list[dict]:
    pid = db.scalar("SELECT id FROM portfolios WHERE run_id=? AND kind='model'", (run_id,))
    if not pid:
        return []
    d = latest_date(run_id)
    rows = db.rows(
        """SELECT p.*, s.ticker, s.name, b.badge, b.reason,
                  (SELECT adj_close FROM prices WHERE ticker=s.ticker ORDER BY date DESC LIMIT 1) px
           FROM positions p JOIN securities s ON s.cik=p.cik
           LEFT JOIN badges b ON b.run_id=? AND b.portfolio_id=p.portfolio_id AND b.date=? AND b.cik=p.cik
           WHERE p.portfolio_id=?""", (run_id, d, pid))
    out = [dict(r) for r in rows]
    tot = sum((r["units"] or 0) * (r["px"] or 0) for r in out) or 1
    for r in out:
        r["value"] = (r["units"] or 0) * (r["px"] or 0)
        r["weight"] = r["value"] / tot
        r["gain"] = (r["px"] / r["avg_cost"] - 1) if r["avg_cost"] and r["px"] else None
    return sorted(out, key=lambda r: -r["weight"])


def equity(run_id) -> list[dict]:
    return [dict(r) for r in db.rows("SELECT date, nav, bench_nav, holdings, cash, value, bench_value, contributed FROM equity_curve WHERE run_id=? ORDER BY date",
                                     (run_id,))]


def stats(curve: list[dict]) -> dict:
    if len(curve) < 2:
        return {}
    d0, d1 = date.fromisoformat(curve[0]["date"]), date.fromisoformat(curve[-1]["date"])
    yrs = max((d1 - d0).days / 365.25, 1 / 365)
    nav = [c["nav"] for c in curve]
    bench = [c["bench_nav"] or 1 for c in curve]
    cagr = nav[-1] ** (1 / yrs) - 1
    bcagr = (bench[-1] / bench[0]) ** (1 / yrs) - 1 if bench[0] else None
    rets = [nav[i] / nav[i - 1] - 1 for i in range(1, len(nav)) if nav[i - 1]]
    mu = sum(rets) / len(rets) if rets else 0
    sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / max(len(rets) - 1, 1)) if rets else 0
    sharpe = (mu * 252) / (sd * math.sqrt(252)) if sd else None
    dates = [c["date"] for c in curve]
    return {"cagr": cagr, "bench_cagr": bcagr, "total": nav[-1] - 1, "bench_total": bench[-1] / bench[0] - 1,
            "sharpe": sharpe, "max_dd": max_drawdown(nav), "bench_max_dd": max_drawdown(bench),
            "worst_12m": worst_12m(dates, nav), "bench_worst_12m": worst_12m(dates, bench), "years": yrs}


def max_drawdown(vals) -> float | None:
    """Worst fall from a previous peak (e.g. -0.34 = -34%)."""
    peak, mdd = None, 0.0
    for v in vals:
        if not v:
            continue
        peak = v if peak is None else max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return mdd if peak is not None else None


def worst_12m(dates, vals) -> float | None:
    """Worst return over any 12-month (365-day) window."""
    from bisect import bisect_left

    pts = [(date.fromisoformat(d), v) for d, v in zip(dates, vals) if v]
    if len(pts) < 2 or (pts[-1][0] - pts[0][0]).days < 365:
        return None
    ds = [p[0] for p in pts]
    worst = None
    for i, (d0, v0) in enumerate(pts):
        j = bisect_left(ds, d0 + timedelta(days=365))
        if j >= len(pts):
            break
        r = pts[j][1] / v0 - 1
        worst = r if worst is None else min(worst, r)
    return worst


def episodes(run_id) -> list[dict]:
    return [dict(r) for r in db.rows(
        """SELECT e.*, s.ticker, s.name FROM episodes e JOIN securities s ON s.cik=e.cik
           WHERE e.run_id=? ORDER BY e.entry_date DESC""", (run_id,))]


def open_episode_returns(run_id) -> dict[int, float]:
    out = {}
    for h in holdings(run_id):
        ep = db.one("SELECT invested, proceeds FROM episodes WHERE run_id=? AND cik=? AND status='open'", (run_id, h["cik"]))
        if ep and ep["invested"]:
            out[h["cik"]] = (ep["proceeds"] + h["value"]) / ep["invested"] - 1
    return out


def rank_buckets(run_id) -> list[dict]:
    rows = db.rows("SELECT entry_rank, return_pct, excess_pct FROM episodes WHERE run_id=? AND status='closed'", (run_id,))
    st = db.rows("SELECT entry_rank, return_pct, bench_return_pct, fwd_1y FROM stints WHERE run_id=?", (run_id,))
    buckets = [("#1–5", 1, 5), ("#6–15", 6, 15), ("#16+", 16, 9999)]
    out = []
    for name, a, b in buckets:
        f = [r for r in st if r["entry_rank"] and a <= r["entry_rank"] <= b]
        fw = [r["fwd_1y"] for r in f if r["fwd_1y"] is not None]
        ep = [r for r in rows if r["entry_rank"] and a <= r["entry_rank"] <= b]
        out.append({"bucket": name, "stints": len(f), "avg_fwd_1y": sum(fw) / len(fw) if fw else None,
                    "episodes": len(ep),
                    "avg_excess": (sum(r["excess_pct"] or 0 for r in ep) / len(ep)) if ep else None})
    return out


def stints_summary(run_id) -> dict:
    r = db.one("""SELECT COUNT(*) n, AVG(return_pct) r, AVG(bench_return_pct) b, AVG(fwd_1y) f,
                  SUM(CASE WHEN return_pct > bench_return_pct THEN 1 ELSE 0 END) beat
                  FROM stints WHERE run_id=? AND status='closed'""", (run_id,))
    return dict(r) if r else {}


def stock(ticker: str) -> dict | None:
    s = db.one("SELECT * FROM securities WHERE ticker=?", (ticker.upper(),))
    return dict(s) if s else None


def latest_snapshot(cik: int, profile_id: int) -> dict | None:
    p = rules.get_profile(profile_id)
    r = db.one("SELECT * FROM snapshots WHERE cik=? AND profile_id=? AND profile_version=? ORDER BY filed DESC LIMIT 1",
               (cik, *p.snap))
    if not r:
        return None
    x = dict(r)
    for k in ("rules_json", "category_scores", "val_inputs", "failed_gates"):
        x[k] = json.loads(x[k] or ("[]" if k in ("rules_json", "failed_gates") else "{}"))
    x["near_miss"] = json.loads(x["near_miss"]) if x["near_miss"] else None
    return x


def stock_eval(run_id, cik) -> dict | None:
    r = db.one("SELECT * FROM evaluations WHERE run_id=? AND cik=? ORDER BY date DESC LIMIT 1", (run_id, cik))
    if not r:
        return None
    x = dict(r)
    x["detail"] = json.loads(x["detail"] or "{}")
    return x


def stock_badge(run_id, cik):
    return db.one("""SELECT b.badge, b.reason, b.date FROM badges b JOIN portfolios f ON f.id=b.portfolio_id
                     WHERE b.run_id=? AND b.cik=? ORDER BY b.date DESC LIMIT 1""", (run_id, cik))


def stock_prices(ticker, since="2016-01-01") -> list[dict]:
    return [dict(r) for r in db.rows("SELECT date, close FROM prices WHERE ticker=? AND date>=? ORDER BY date",
                                     (ticker, since))]


def stock_value_history(run_id, cik) -> list[dict]:
    return [dict(r) for r in db.rows(
        "SELECT date, iv, buy_below FROM evaluations WHERE run_id=? AND cik=? ORDER BY date", (run_id, cik))]


def stock_signals(cik) -> list[dict]:
    return [dict(r) for r in db.rows(
        """SELECT g.*, r.name run_name, r.kind, e.rank_score, e.quality, e.mos FROM signals g
           JOIN runs r ON r.id=g.run_id
           LEFT JOIN evaluations e ON e.run_id=g.run_id AND e.date=g.date AND e.cik=g.cik
           WHERE g.cik=? ORDER BY g.date DESC LIMIT 50""", (cik,))]


def notes(cik) -> dict:
    r = db.one("SELECT * FROM user_notes WHERE cik=?", (cik,))
    return dict(r) if r else {"cik": cik, "outside_circle": 0, "understand": 0, "trust_mgmt": 0, "moat_20y": 0,
                              "note": ""}


def save_notes(n: dict) -> None:
    db.get().execute(
        "INSERT OR REPLACE INTO user_notes(cik,outside_circle,understand,trust_mgmt,moat_20y,note,updated_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (n["cik"], int(n["outside_circle"]), int(n["understand"]), int(n["trust_mgmt"]), int(n["moat_20y"]),
         n.get("note", ""), db.now()))


def market_now() -> dict:
    m = Macro()
    vti = {r["date"]: r["adj_close"] for r in db.rows(
        "SELECT date, adj_close FROM prices WHERE ticker='VTI' ORDER BY date")}
    dates = sorted(vti)

    def vti_adj(d):
        import bisect

        i = bisect.bisect_right(dates, d) - 1
        return vti[dates[i]] if i >= 0 else None

    today = date.today().isoformat()
    p = rules.default_profile()
    reg = m.regime(today, p.settings["mos_gate"], p.settings.get("regime_modifier", False), vti_adj)
    hist = []
    for d, _ in zip(*m.s.get("GDP", ([], []))):
        r, _q = m.buffett_indicator(d)
        if r:
            hist.append({"date": d, "ratio": r})
    return {"regime": reg, "history": hist, "t30_hist": list(zip(*m.s.get("DGS30", ([], []))))}


def search(q: str) -> list[dict]:
    q = q.strip().upper()
    if not q:
        return []
    return [dict(r) for r in db.rows(
        "SELECT ticker, name FROM securities WHERE ticker LIKE ? OR UPPER(name) LIKE ? ORDER BY "
        "CASE WHEN ticker=? THEN 0 ELSE 1 END, ticker LIMIT 12", (q + "%", "%" + q + "%", q))]


_SLOW_CACHE: dict = {}


def _cached(key, fn, ttl=900):
    """Whole-table counts are slow on a big database; reuse them for 15 minutes."""
    import time

    hit = _SLOW_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _SLOW_CACHE[key] = (time.monotonic(), val)
    return val


def _heavy_counts() -> dict:
    return {
        "with_facts": db.scalar("SELECT COUNT(DISTINCT cik) FROM facts", default=0),
        "price_tickers": db.scalar("SELECT COUNT(DISTINCT ticker) FROM prices", default=0),
        "sources": [dict(r) for r in db.rows("SELECT source, COUNT(*) n, MAX(date) d FROM prices GROUP BY source")],
    }


def data_health() -> dict:
    return {**_data_health(), **_cached("health", _heavy_counts)}


def _data_health() -> dict:
    return {
        "companies": db.scalar("SELECT COUNT(*) FROM securities WHERE active=1", default=0),
        "last_price": db.scalar("SELECT MAX(date) FROM prices WHERE ticker='SPY'"),
        "last_daily": db.cfg("last_daily_run"),
        "last_backup": db.cfg("last_backup"),
        "last_prices": db.cfg("last_price_fetch"),
        "errors": [dict(r) for r in db.rows("SELECT * FROM data_log WHERE ok=0 ORDER BY id DESC LIMIT 40")],
        "recent": [dict(r) for r in db.rows("SELECT * FROM data_log ORDER BY id DESC LIMIT 15")],
        "categories": [dict(r) for r in db.rows("SELECT category, COUNT(*) n FROM securities GROUP BY category")],
    }


GP_BASED = {"SG&A margin", "R&D margin", "Depreciation margin"}


def _real_gaps(missing: list[str], priced: bool) -> list[str]:
    """Leave out what isn't a data gap: margins measured against gross profit for companies that report no
    cost of sales (railroads, card networks, asset managers...), and the $1 test for companies the app doesn't
    download prices for (only the shortlist gets prices)."""
    out = [m for m in missing if not (m in GP_BASED and "Gross margin" in missing)]
    out = [m for m in out if m != "Gross margin"]
    if not priced:
        out = [m for m in out if m != 'The "$1 test"']
    return out


def coverage_gaps(limit=50) -> list[dict]:
    """Companies whose latest snapshot is missing core line items (helps improve the concept map)."""
    return _cached(("gaps", limit), lambda: _coverage_gaps(limit))


def _coverage_gaps(limit=50) -> list[dict]:
    out = []
    seen = set()
    rows = db.rows("""SELECT s.cik, s.ticker, s.name, sn.rules_json FROM snapshots sn JOIN securities s ON s.cik=sn.cik
                      JOIN (SELECT cik, MAX(filed) m FROM snapshots GROUP BY cik) x ON x.cik=sn.cik AND x.m=sn.filed
                      WHERE sn.quality IS NOT NULL AND COALESCE(s.category,'standard')='standard' LIMIT 20000""")
    priced = {r["ticker"] for r in db.rows("SELECT DISTINCT ticker FROM prices")}
    for r in rows:
        if r["cik"] in seen:  # snapshots repeat once per profile
            continue
        seen.add(r["cik"])
        rs = json.loads(r["rules_json"] or "[]")
        missing = [x["name"] for x in rs if x.get("value") is None and x["mode"] in ("gate", "scored")]
        missing = _real_gaps(missing, r["ticker"] in priced)
        if len(missing) >= 3:
            out.append({"ticker": r["ticker"], "name": r["name"], "missing": ", ".join(missing[:6]), "n": len(missing)})
    return sorted(out, key=lambda x: -x["n"])[:limit]


# ---- stock lists behind the Today cards: valuation + the business metrics Buffett looks at -------------------------
# (key, column header, rule ids by rulebook, format, tooltip)
BUFFETT_METRICS = [
    ("roe", "ROE", ["r19", "b2", "i3"], "pct",
     "Return on equity, 10-year average. Buffett looks for 15%+ earned without heavy debt."),
    ("roic", "ROIC", ["r20"], "pct", "Return on invested capital (debt + equity). High and steady = a moat."),
    ("gm", "Gross margin", ["r1"], "pct", "40%+ often signals pricing power; under 20% a commodity business."),
    ("nm", "Net margin", ["r7"], "pct", "Profit kept from each $ of sales. 20%+ is excellent."),
    ("epsg", "EPS growth", ["r8"], "pct", "Earnings per share growth per year over the lookback."),
    ("oeg", "Owner earnings growth", ["r17"], "pct",
     "Growth of owner earnings (net income + depreciation - maintenance capex): Buffett's true cash profit."),
    ("cashconv", "Cash conversion", ["r18"], "pct", "Operating cash flow ÷ net income. Near or above 100% = real profits."),
    ("capex", "Capex ÷ net income", ["r16"], "pct", "Lower is better: under 25% means growth doesn't eat the profits."),
    ("debt", "Debt payback (yrs)", ["r10"], "years", "Years of earnings to pay off long-term debt. Buffett: under 3–4."),
    ("de", "Adj. debt ÷ equity", ["r12"], "x", "Debt vs equity with buybacks added back. Under 0.8 is conservative."),
    ("shares", "Share count / yr", ["r14", "b8", "i8"], "pct", "Yearly change in shares. Negative = buybacks (good)."),
    ("dollar", "$1 test", ["r21", "b9"], "x",
     "Market value added per $1 of earnings kept. Above 1 = management reinvests well."),
    ("pred", "Predictability", ["r22"], "ratio", "How steady earnings have been (0–100%). Buffett wants predictable."),
]


def _latest_rule_values(profile_id: int | None, ciks: list[int]) -> dict[int, dict]:
    """Latest stored rule values per company for a profile's scores: {cik: {rule_id: value}}."""
    if not profile_id or not ciks:
        return {}
    try:
        snap = rules.get_profile(profile_id).snap
    except Exception:
        return {}
    out: dict[int, dict] = {}
    for i in range(0, len(ciks), 400):
        part = ciks[i:i + 400]
        q = ",".join("?" * len(part))
        for r in db.rows(f"""SELECT sn.cik, sn.rules_json FROM snapshots sn
                             JOIN (SELECT cik, MAX(filed) m FROM snapshots WHERE profile_id=? AND profile_version=?
                                   AND cik IN ({q}) GROUP BY cik) x ON x.cik=sn.cik AND x.m=sn.filed
                             WHERE sn.profile_id=? AND sn.profile_version=?""", (*snap, *part, *snap)):
            vals = {}
            for rr in json.loads(r["rules_json"] or "[]"):
                vals[rr.get("id")] = rr.get("value")
            out[r["cik"]] = vals
    return out


def stock_table(run_id: int, kind: str) -> list[dict]:
    """Rows for the list pages: buy | pricey | near | holdings | wonderful (buy + pricey)."""
    d = latest_date(run_id)
    if not d:
        return []
    evs = evaluations(run_id, d)
    by = {e["cik"]: e for e in evs}
    hold = {h["cik"]: h for h in holdings(run_id)}
    if kind == "holdings":
        picked = [by.get(c) or {"cik": c, "ticker": h["ticker"], "name": h["name"], "detail": {}} for c, h in hold.items()]
    elif kind == "wonderful":
        picked = [e for e in evs if e["list"] in ("buy", "pricey") and not e.get("outside_circle")]
    else:
        picked = [e for e in evs if e["list"] == kind and (kind == "near" or not e.get("outside_circle"))]
    rv = _latest_rule_values((run(run_id) or {}).get("profile_id"), [e["cik"] for e in picked])
    rows = []
    for e in picked:
        det = e.get("detail") or {}
        price, bb = e.get("price"), e.get("buy_below")
        row = {
            "ticker": e.get("ticker"), "name": e.get("name"), "sector": e.get("sic_desc") or "",
            "list": e.get("list") or "", "badge": e.get("badge") or "", "rank": e.get("rank"),
            "rank_score": e.get("rank_score"), "quality": e.get("quality"), "value": e.get("value"),
            "mos": e.get("mos"), "er": e.get("exp_return"), "price": price, "buy_below": bb,
            "strong": e.get("strong_buy_below"), "iv": e.get("iv"),
            "away": (price / bb - 1) if price and bb else None,
            "ey": det.get("ey"), "pe": det.get("pe_now"), "pe_med": det.get("pe_median"), "pepg": det.get("pepg"),
            "mcap": det.get("mcap"), "category": det.get("category") or "standard",
        }
        vals = rv.get(e["cik"], {})
        for key, _, ids, _, _ in BUFFETT_METRICS:
            row[key] = next((vals[i] for i in ids if vals.get(i) is not None), None)
        h = hold.get(e["cik"])
        if h:
            row.update({"weight": h.get("weight"), "gain": h.get("gain"), "pos_value": h.get("value"),
                        "opened": h.get("opened_on"), "badge": h.get("badge") or row["badge"]})
        rows.append(row)
    return rows
