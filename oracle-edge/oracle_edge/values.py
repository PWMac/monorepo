"""Every company's value-driver valuation at its latest price, for the Screener (stored in vd_values).

The stock page's "What it's worth" card values one company on the spot; this does the same for every scored
company in one pass (about 4,000; ~10-30 seconds), after each daily update and after a rebuild. Banks and
insurers are left out (the model doesn't fit them)."""

from __future__ import annotations

import json
from datetime import date, timedelta
from statistics import median

from . import db, estimates, rules, wonderful
from .engine import _foreign_scale, split_scale, ttm_inputs
from .splits import Splits
from .valuation import value_driver


def _fy_prices(ticker: str, fy_ends: list[str]) -> dict[str, float]:
    """Close on (or up to a week before) each fiscal year end, on today's share basis (stored prices)."""
    out = {}
    for y in fy_ends:
        r = db.one("SELECT close FROM prices WHERE ticker=? AND date<=? AND date>=? ORDER BY date DESC LIMIT 1",
                   (ticker, y, (date.fromisoformat(y) - timedelta(days=7)).isoformat()))
        if r and r["close"]:
            out[y] = r["close"]
    return out


def compute_all(profile=None, progress=None) -> int:
    p = profile or rules.default_profile()
    st = p.settings
    today = date.today().isoformat()
    fin = wonderful.financial_ciks()
    sec = {r["cik"]: dict(r) for r in db.rows("SELECT cik, ticker, is_foreign, currency, adr_ratio, adr_source "
                                              "FROM securities WHERE ticker IS NOT NULL")}
    snaps = {r["cik"]: r for r in db.rows(
        "SELECT s.cik, s.filed, s.val_inputs, s.routed FROM snapshots s JOIN (SELECT cik, MAX(filed) f FROM snapshots "
        "WHERE profile_id=? AND profile_version=? GROUP BY cik) m ON s.cik=m.cik AND s.filed=m.f "
        "WHERE s.profile_id=? AND s.profile_version=?", (*p.snap, *p.snap))}
    last = {r["ticker"]: (r["date"], r["close"]) for r in db.rows(
        "SELECT p.ticker, p.date, p.close FROM prices p JOIN (SELECT ticker, MAX(date) d FROM prices GROUP BY ticker) m "
        "ON p.ticker=m.ticker AND p.date=m.d")}
    qs = {r["cik"]: r for r in db.rows(
        "SELECT q.cik, q.filed, q.metrics_json FROM qsnaps q JOIN (SELECT cik, MAX(filed) f FROM qsnaps GROUP BY cik) m "
        "ON q.cik=m.cik AND q.filed=m.f")}
    est = {}
    old = (date.today() - timedelta(days=estimates.STALE_DAYS)).isoformat()
    for r in db.rows("SELECT e.ticker, e.json FROM estimates e JOIN (SELECT ticker, MAX(day) d FROM estimates GROUP BY "
                     "ticker) m ON e.ticker=m.ticker AND e.day=m.d WHERE e.day>=?", (old,)):
        est[r["ticker"]] = estimates.near_term_growth(json.loads(r["json"]))
    sp = Splits()
    mac = None
    rows = []
    todo = [c for c in snaps if c in sec and c not in fin and (snaps[c]["routed"] or "") not in ("bank", "insurer")]
    for i, cik in enumerate(todo):
        info, s = sec[cik], snaps[cik]
        t = info["ticker"]
        lp = last.get(t)
        if not lp or not lp[1] or lp[0] < (date.today() - timedelta(days=10)).isoformat():
            continue
        vi = json.loads(s["val_inputs"] or "{}")
        if not vi.get("vd_sales"):
            continue
        try:
            vi = split_scale(vi, sp.factor(t, s["filed"]))
            foreign = bool(info["is_foreign"])
            if foreign:
                if mac is None:
                    from .market import Macro

                    mac = Macro()
                vi, _why = _foreign_scale(vi, info, today, mac, None)
                if vi is None:
                    continue
            q = qs.get(cik)
            ttm = ttm_inputs(q, vi, sp.factor(t, q["filed"])) if (q and not foreign and q["filed"] > s["filed"]) else None
            e = None if foreign else est.get(t)
            v = value_driver(lp[1], vi, st, st["mos_gate"], _fy_prices(t, list(vi.get("eps_by_fy", {}).keys())), ttm, e)
        except Exception as ex:  # one odd company must never stop the pass
            db.log("values", "value_driver", False, f"{t}: {ex}"[:200])
            continue
        if not v.iv:
            continue
        x = v.extra
        rows.append((cik, lp[0], lp[1], v.iv, x["low"], x["high"], v.er, v.mos, v.buy_below, x["implied_growth"],
                     x["growth"]["g"], v.pe_now, v.pe_median, x.get("hurdle") or x["required_return"], int(bool(x["ttm"])),
                     int(bool(x["estimates_used"])), db.now()))
        if progress and i % 200 == 0:
            progress(i / max(len(todo), 1))
    with db.tx() as con:
        con.execute("DELETE FROM vd_values")
        con.executemany("INSERT INTO vd_values VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    db.set_cfg("vd_values_at", db.now())
    db.set_cfg("vd_values_version", VALUES_VERSION)
    return len(rows)


def all_values() -> dict[int, dict]:
    return {r["cik"]: dict(r) for r in db.rows("SELECT * FROM vd_values")}


VALUES_VERSION = 2  # 0.17.8: recalibrated model (9% discount rate, 10 + 10 years, recent margins)


def ensure(profile=None) -> None:
    """First visit after installing (or after the model changed): value everything once (later, the daily update
    keeps it current)."""
    if not db.scalar("SELECT COUNT(*) FROM vd_values", default=0) or \
            int(db.cfg("vd_values_version", 1) or 1) < VALUES_VERSION:
        try:
            compute_all(profile)
        except Exception as e:
            db.log("values", "compute", False, str(e)[:200])


def stats() -> dict:
    v = [r["mos"] for r in db.rows("SELECT mos FROM vd_values WHERE mos IS NOT NULL")]
    return {"n": len(v), "median_mos": median(v) if v else None}
