"""Bitcoin early-warning signal: a second market-temperature reading next to the Buffett Indicator.

Bitcoin's change over the last N trading days (default 20) has gone with the following month in tech stocks
(QQQ) mostly at the extremes: a drop in Bitcoin's worst 20% so far was followed by a weak QQQ month far more
often than usual. The cut-offs are recomputed each day from earlier data only, so the history never peeks
ahead. It is a warning, not a forecast and never a hard stop: Oracle Edge keeps following its rules, and the
Today page and email suggest what to do with new money.

Every daily run logs the day's call in `btc_calls`; once the horizon has passed the QQQ result is filled in,
so the record on data the rule has never seen builds up from the day it was switched on.
"""

from __future__ import annotations

import bisect
from datetime import date, timedelta

from . import db

BTC, QQQ = "BTC-USD", "QQQ"
DEFAULTS = {"lookback": 20, "horizon": 20, "bear_pct": 20, "bull_pct": 80, "min_obs": 250, "alerts": True}
ZONE_LABEL = {"bear": "Bearish extreme", "bull": "Bullish extreme", "normal": "Normal",
              "warming": "Not enough history yet"}


def settings() -> dict:
    s = dict(DEFAULTS)
    s.update(db.cfg("btc_signal", {}) or {})
    s["lookback"] = max(5, int(s["lookback"]))
    s["horizon"] = max(5, int(s["horizon"]))
    return s


def save_settings(s: dict) -> None:
    cur = settings()
    cur.update({k: s[k] for k in DEFAULTS if k in s})
    db.set_cfg("btc_signal", cur)
    _CACHE.clear()


# ------------------------------------------------------------------ prices
def refresh_prices() -> dict:
    """Bitcoin and QQQ daily closes (full history the first time, then the last couple of weeks)."""
    from .sources import prices

    today = date.today()
    known = {r["ticker"]: r["m"] for r in db.rows(
        "SELECT ticker, MAX(date) m FROM prices WHERE ticker IN (?,?) GROUP BY ticker", (BTC, QQQ))}
    out = {"rows": 0, "missing": []}
    for t in (BTC, QQQ):
        start = date(2014, 9, 1) if t not in known else min(date.fromisoformat(known[t]), today - timedelta(days=14))
        res = prices.fetch([t], start, today)
        out["rows"] += res["rows"]
        out["missing"] += res["missing"]
    _CACHE.clear()
    return out


def _closes(t: str) -> tuple[list[str], list[float]]:
    rows = db.rows("SELECT date, COALESCE(adj_close, close) c FROM prices WHERE ticker=? AND c > 0 ORDER BY date", (t,))
    return [r["date"] for r in rows], [float(r["c"]) for r in rows]


def _pctile(sorted_vals: list[float], p: float) -> float:
    """Linear-interpolated percentile (0-100) of an already sorted list."""
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


# ------------------------------------------------------------------ the signal
_CACHE: dict = {}


def history(s: dict | None = None) -> list[dict]:
    """One row per QQQ trading day: Bitcoin's N-day change, the cut-offs known that day, the zone, and QQQ's
    change over the following horizon (None until it has happened)."""
    s = s or settings()
    qd, qv = _closes(QQQ)
    bd, bv = _closes(BTC)
    key = (s["lookback"], s["horizon"], s["bear_pct"], s["bull_pct"], s["min_obs"], qd[-1] if qd else None,
           bd[-1] if bd else None, len(qd), len(bd))
    if key in _CACHE:
        return _CACHE[key]
    out: list[dict] = []
    if len(qd) < s["lookback"] + 2 or len(bd) < 30:
        return out
    # Bitcoin trades every day; read it as of each stock-market close so "20 days" means 20 trading days
    b = []
    for d in qd:
        i = bisect.bisect_right(bd, d) - 1
        b.append(bv[i] if i >= 0 and (date.fromisoformat(d) - date.fromisoformat(bd[i])).days <= 4 else None)
    past: list[float] = []
    L, H = s["lookback"], s["horizon"]
    for i in range(L, len(qd)):
        if b[i] is None or b[i - L] is None:
            continue
        ret = b[i] / b[i - L] - 1
        enough = len(past) >= s["min_obs"]
        bear_cut = _pctile(past, s["bear_pct"]) if enough else None
        bull_cut = _pctile(past, s["bull_pct"]) if enough else None
        if not enough:
            zone = "warming"
        elif ret <= bear_cut:
            zone = "bear"
        elif ret >= bull_cut:
            zone = "bull"
        else:
            zone = "normal"
        fwd = qv[i + H] / qv[i] - 1 if i + H < len(qd) else None
        out.append({"date": qd[i], "btc": b[i], "btc_ret": ret, "bear_cut": bear_cut, "bull_cut": bull_cut,
                    "zone": zone, "qqq": qv[i], "qqq_fwd": fwd})
        bisect.insort(past, ret)  # only after today's zone is set: the cut-offs never see the future
    _CACHE.clear()
    _CACHE[key] = out
    return out


def _episodes(rows: list[dict], zone: str) -> int:
    n, prev = 0, None
    for r in rows:
        if r["zone"] == zone and prev != zone:
            n += 1
        prev = r["zone"]
    return n


def zone_stats(rows: list[dict]) -> dict:
    """What QQQ did over the following horizon after each kind of reading (overlapping days, so the number of
    separate episodes matters more than the number of days)."""
    out = {}
    for z in ("bear", "normal", "bull"):
        f = [r["qqq_fwd"] for r in rows if r["zone"] == z and r["qqq_fwd"] is not None]
        out[z] = {"days": len(f), "episodes": _episodes(rows, z),
                  "up": (sum(1 for x in f if x > 0) / len(f)) if f else None,
                  "avg": (sum(f) / len(f)) if f else None, "worst": min(f) if f else None,
                  "best": max(f) if f else None}
    return out


def episodes(rows: list[dict]) -> list[dict]:
    """Each warning or tailwind episode, judged on the day it started (the day the call was made): a warning is
    right if QQQ fell over the following horizon, a tailwind if QQQ rose. Pending until the horizon has passed."""
    out, prev = [], None
    for k, r in enumerate(rows):
        z = r["zone"]
        if z in ("bear", "bull") and z != prev:
            f = r["qqq_fwd"]
            verdict = None if f is None else ((f < 0) if z == "bear" else (f > 0))
            out.append({"zone": z, "start": r["date"], "end": r["date"], "btc_ret": r["btc_ret"], "qqq_fwd": f,
                        "correct": verdict, "i": k})
        if z in ("bear", "bull") and out and out[-1]["zone"] == z and prev == z:
            out[-1]["end"] = r["date"]
        prev = z
    return out


def scorecard(rows: list[dict]) -> dict:
    """Hit rates of the calls against the base rate (how often QQQ fell or rose over any horizon window)."""
    eps = episodes(rows)
    f_all = [r["qqq_fwd"] for r in rows if r["qqq_fwd"] is not None and r["zone"] != "warming"]
    out = {"base_down": (sum(1 for x in f_all if x < 0) / len(f_all)) if f_all else None}
    out["base_up"] = 1 - out["base_down"] if out["base_down"] is not None else None
    for z in ("bear", "bull"):
        done = [e for e in eps if e["zone"] == z and e["correct"] is not None]
        out[z] = {"episodes": len([e for e in eps if e["zone"] == z]), "judged": len(done),
                  "correct": sum(1 for e in done if e["correct"]),
                  "rate": (sum(1 for e in done if e["correct"]) / len(done)) if done else None,
                  "pending": len([e for e in eps if e["zone"] == z and e["correct"] is None])}
    return out


def current(s: dict | None = None) -> dict | None:
    s = s or settings()
    rows = history(s)
    if not rows:
        return None
    r = dict(rows[-1])
    k = len(rows) - 1
    while k > 0 and rows[k - 1]["zone"] == r["zone"]:
        k -= 1
    r["since"] = rows[k]["date"]
    r["days_in_zone"] = len(rows) - k
    r["start_price"] = None
    qd, _ = _closes(QQQ)
    if r["date"] in qd:
        i = qd.index(r["date"])
        if i >= s["lookback"]:
            r["start_date"] = qd[i - s["lookback"]]
            r["start_price"] = r["btc"] / (1 + r["btc_ret"])
    r["stats"] = zone_stats(rows)
    r["settings"] = s
    return r


# ------------------------------------------------------------------ call log (the honest forward test)
def record_and_resolve() -> dict:
    """Log today's call (once per trading day) and fill in QQQ results for calls whose horizon has passed."""
    s = settings()
    rows = history(s)
    if not rows:
        return {"logged": 0, "resolved": 0}
    con = db.get()
    last = rows[-1]
    cur = con.execute(
        "INSERT OR IGNORE INTO btc_calls(date,lookback,horizon,btc_ret,bear_cut,bull_cut,zone,qqq_px,logged_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)", (last["date"], s["lookback"], s["horizon"], last["btc_ret"], last["bear_cut"],
                                      last["bull_cut"], last["zone"], last["qqq"], db.now()))
    qd, qv = _closes(QQQ)
    resolved = 0
    for c in db.rows("SELECT date, horizon, qqq_px FROM btc_calls WHERE qqq_fwd IS NULL"):
        if c["date"] in qd:
            i = qd.index(c["date"])
            if i + c["horizon"] < len(qd) and c["qqq_px"]:
                con.execute("UPDATE btc_calls SET qqq_fwd=?, resolved_on=? WHERE date=?",
                            (qv[i + c["horizon"]] / c["qqq_px"] - 1, qd[i + c["horizon"]], c["date"]))
                resolved += 1
    return {"logged": cur.rowcount, "resolved": resolved}


def forward_record() -> dict:
    rows = [dict(r) for r in db.rows("SELECT * FROM btc_calls ORDER BY date")]
    done = [r for r in rows if r["qqq_fwd"] is not None]
    by = {}
    for z in ("bear", "normal", "bull"):
        f = [r["qqq_fwd"] for r in done if r["zone"] == z]
        by[z] = {"n": len(f), "up": (sum(1 for x in f if x > 0) / len(f)) if f else None,
                 "avg": (sum(f) / len(f)) if f else None}
    return {"first": rows[0]["date"] if rows else None, "calls": len(rows), "resolved": len(done), "by_zone": by}


# ------------------------------------------------------------------ alerts and advice
BI_ALERT_ZONES = ("Significantly overvalued", "Extreme")


def advice(reg, btc: dict | None) -> list[dict]:
    """Plain-language recommendations when either market reading is at an extreme. Never a hard stop."""
    out = []
    bear = btc and btc["zone"] == "bear"
    bull = btc and btc["zone"] == "bull"
    hot = reg is not None and reg.zone in BI_ALERT_ZONES
    if bear:
        st = btc["stats"]["bear"]
        hist = (f" After past readings like this, QQQ rose over the next month {st['up']:.0%} of the time and "
                f"averaged {st['avg']:+.1%} ({st['episodes']} separate episodes)." if st["up"] is not None else "")
        out.append({"level": "warn", "title": "Bitcoin early warning",
                    "text": f"Bitcoin is {btc['btc_ret']:+.1%} over {btc['settings']['lookback']} trading days, in its "
                            f"worst {btc['settings']['bear_pct']}% so far (cut-off {btc['bear_cut']:+.1%})." + hist +
                            " Suggested: hold off adding new money to tech-heavy holdings for the coming weeks, "
                            "and if you want less risk, trim part of a position rather than selling all of it."})
    if hot:
        out.append({"level": "warn" if reg.zone == "Extreme" else "info", "title": "Expensive market",
                    "text": f"The Buffett Indicator is {reg.ratio:.0%} ({reg.zone.lower()}). Suggested: insist on "
                            "the full margin of safety, let new money build up for strong buys, and expect "
                            "fewer bargains."})
    if bear and hot:
        out.append({"level": "warn", "title": "Both readings are flashing",
                    "text": "An expensive market and a Bitcoin selloff at the same time: the most cautious "
                            "setting. Keep new purchases small until at least one of them eases."})
    if bull and not hot:
        out.append({"level": "info", "title": "Bitcoin tailwind",
                    "text": f"Bitcoin is {btc['btc_ret']:+.1%} over {btc['settings']['lookback']} trading days, in "
                            f"its best {100 - btc['settings']['bull_pct']}% so far. Historically a supportive month "
                            "for tech stocks. No action needed."})
    return out


def check_alerts(reg, btc: dict | None) -> list[dict]:
    """Record a market alert when either reading moves into or out of an extreme. The first check only
    remembers where things stand (no alert for a state that already existed)."""
    s = settings()
    state = db.cfg("market_alert_state") or {}
    now = {"btc": btc["zone"] if btc else None, "bi": reg.zone if reg else None}
    events = []
    if state and s.get("alerts", True):
        d = btc["date"] if btc else date.today().isoformat()
        if now["btc"] and now["btc"] != state.get("btc") and "warming" not in (now["btc"], state.get("btc")):
            if now["btc"] == "bear":
                msg = (f"Bitcoin {btc['btc_ret']:+.1%} over {s['lookback']} trading days: entered its worst "
                       f"{s['bear_pct']}% (cut-off {btc['bear_cut']:+.1%}). Hold off adding to tech-heavy holdings.")
            elif now["btc"] == "bull":
                msg = f"Bitcoin {btc['btc_ret']:+.1%} over {s['lookback']} trading days: entered its best zone."
            else:
                msg = f"Bitcoin back to normal ({btc['btc_ret']:+.1%} over {s['lookback']} trading days)."
            events.append(("btc", now["btc"], msg, d))
        if now["bi"] and state.get("bi") and now["bi"] != state.get("bi") and (
                now["bi"] in BI_ALERT_ZONES or state.get("bi") in BI_ALERT_ZONES):
            msg = f"Buffett Indicator moved from {state['bi'].lower()} to {now['bi'].lower()} ({reg.ratio:.0%})."
            events.append(("bi", now["bi"], msg, d))
    con = db.get()
    for kind, st, msg, d in events:
        con.execute("INSERT INTO market_alerts(date,kind,state,message,created_at) VALUES(?,?,?,?,?)",
                    (d, kind, st, msg, db.now()))
    db.set_cfg("market_alert_state", now)
    return [{"kind": k, "state": st, "message": m, "date": d} for k, st, m, d in events]


def recent_alerts(days: int = 14) -> list[dict]:
    return [dict(r) for r in db.rows(
        "SELECT * FROM market_alerts WHERE date >= date('now', ?) ORDER BY date DESC, id DESC", (f"-{days} days",))]


def daily(reg) -> dict:
    """Called from the daily run after prices: refresh Bitcoin and QQQ, log the call, raise alerts."""
    out = {"prices": refresh_prices()}
    out.update(record_and_resolve())
    out["alerts"] = check_alerts(reg, current())
    return out
