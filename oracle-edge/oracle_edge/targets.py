"""Price targets: what price to buy at, when to trim, and alerts as the price gets close.

The rule of the house: VALUE decides WHAT price is good; the chart only helps with WHEN inside that zone.

* Buy targets come from the valuation, never from a chart: strong-buy below, buy below (margin of safety
  and the 10% expected-return price, whichever is lower).
* Inside (or just under) the buy zone, the suggested limit price is the nearest SUPPORT level: the 50- or
  200-day average, a 52-week Fibonacci retracement (38.2 / 50 / 61.8 %) or the 52-week low. These are prices
  where buyers recently stepped in; a limit there often saves a few percent and costs nothing if missed,
  because the buy-below price still stands.
* In a downtrend (price under a falling 50-day average: the "falling knife"), the plan splits the buy into
  thirds at successive support levels instead of guessing the bottom.
* Sells stay Oracle-style and business-driven: SELL / REVIEW fill at the next open whatever the chart says;
  TRIM only at the nosebleed level (2x value). Charts never trigger a sale.
* "Approaching" uses a volatility band (ATR-14 = the average daily range), so a calm stock and a jumpy one
  both alert roughly the same number of typical days' moves before the target.
"""
from __future__ import annotations

from . import db

FIBS = (0.382, 0.5, 0.618)


# ------------------------------------------------------------------ technicals
def _sma(xs: list[float], n: int) -> float | None:
    return sum(xs[-n:]) / n if len(xs) >= n else None


def bars(ticker: str, asof: str | None = None, n: int = 320) -> list[tuple]:
    """(date, high, low, close) oldest first."""
    q = "SELECT date, high, low, close FROM prices WHERE ticker=? AND close IS NOT NULL"
    args: list = [ticker]
    if asof:
        q += " AND date<=?"
        args.append(asof)
    rs = db.rows(q + " ORDER BY date DESC LIMIT ?", (*args, n))
    return [(r["date"], r["high"] or r["close"], r["low"] or r["close"], r["close"]) for r in reversed(rs)]


def technicals_from(b: list[tuple]) -> dict | None:
    if len(b) < 30:
        return None
    dates = [x[0] for x in b]
    hi = [x[1] for x in b]
    lo = [x[2] for x in b]
    cl = [x[3] for x in b]
    last = cl[-1]
    sma50, sma200 = _sma(cl, 50), _sma(cl, 200)
    sma50_prev = _sma(cl[:-10], 50)
    sma200_prev = _sma(cl[:-20], 200)
    # Wilder's ATR-14
    trs = [max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1])) for i in range(1, len(b))]
    atr = sum(trs[:14]) / 14 if len(trs) >= 14 else sum(trs) / max(len(trs), 1)
    for tr in trs[14:]:
        atr = (atr * 13 + tr) / 14
    w = slice(max(0, len(b) - 252), len(b))
    whi, wlo = hi[w], lo[w]
    hi52, lo52 = max(whi), min(wlo)
    i_hi, i_lo = whi.index(hi52), wlo.index(lo52)
    swing = "up" if i_lo < i_hi else "down"  # the last big move: low -> high (up) or high -> low (down)
    rng = hi52 - lo52
    fibs = {}
    if rng > 0:
        for r in FIBS:
            # after a rise, retracements are measured down from the high; after a fall, rebounds up from the low
            fibs[r] = hi52 - r * rng if swing == "up" else lo52 + r * rng
    falling = bool(sma50 and sma50_prev and last < sma50 and sma50 < sma50_prev)
    if falling:
        trend = "down"
    elif sma200 and sma200_prev and last > sma200 and sma200 >= sma200_prev:
        trend = "up"
    else:
        trend = "sideways"
    mom = cl[-21] / cl[-252] - 1 if len(cl) >= 252 and cl[-252] else None
    return {"date": dates[-1], "close": last, "high": hi[-1], "low": lo[-1], "sma50": sma50, "sma200": sma200,
            "atr": atr, "atr_pct": atr / last if last else None, "hi52": hi52, "lo52": lo52, "swing": swing,
            "fibs": fibs, "trend": trend, "falling": falling, "mom_12_1": mom}


def technicals(ticker: str, asof: str | None = None) -> dict | None:
    return technicals_from(bars(ticker, asof)) if ticker else None


TREND_TEXT = {"up": "Uptrend: above a rising 200-day average",
              "down": "Downtrend: below a falling 50-day average",
              "sideways": "Sideways / basing"}


def support_levels(t: dict) -> list[tuple[float, str]]:
    lv = []
    if t.get("sma50"):
        lv.append((t["sma50"], "50-day average"))
    if t.get("sma200"):
        lv.append((t["sma200"], "200-day average"))
    for r, v in (t.get("fibs") or {}).items():
        lv.append((v, f"Fibonacci {r * 100:.1f}%"))
    if t.get("lo52"):
        lv.append((t["lo52"], "52-week low"))
    return sorted(lv, key=lambda x: -x[0])


# ------------------------------------------------------------------ the plan
def band(price: float, atr: float | None, st: dict) -> float:
    raw = st.get("alert_band_atr", 1.5) * (atr or 0) / price if price else 0
    return min(st.get("alert_band_max", 0.08), max(st.get("alert_band_min", 0.02), raw))


def plan(price: float | None, buy_below: float | None, strong: float | None, iv: float | None, t: dict | None,
         st: dict) -> dict:
    """Suggested buy limit / staged entry, trim level, and context. Pure function (easy to test)."""
    out = {"price": price, "buy_below": buy_below, "strong": strong, "iv": iv,
           "trim_above": st.get("nosebleed_iv_mult", 2.0) * iv if iv else None,
           "limit": None, "limit_label": None, "tranches": [], "status": None, "note": "", "levels": [],
           "trend": t["trend"] if t else None, "band": None}
    if not price or not buy_below or buy_below <= 0:
        out["note"] = "Targets appear once the stock can be valued."
        return out
    atr = (t or {}).get("atr") or price * 0.02
    out["band"] = band(price, atr, st)
    levels = support_levels(t) if t else []
    out["levels"] = levels
    strong = strong or buy_below * 0.8
    stage = bool(t and t["trend"] == "down" and st.get("stage_in_downtrend", True))

    def next_below(p, gap):
        """Highest support at least `gap` under p (and above zero)."""
        for v, lab in levels:
            if 0 < v <= p - gap:
                return v, lab
        return None

    if price <= buy_below:
        near = [(v, lab) for v, lab in levels if price - atr <= v <= price]
        first = near[0] if near else (price, "today's price (at market)")
        out["status"] = "in_zone"
    else:
        inzone = [(v, lab) for v, lab in levels if v <= buy_below]
        first = inzone[0] if inzone and inzone[0][0] >= strong else (buy_below, "buy-below price")
        out["status"] = "above_zone"
    out["limit"], out["limit_label"] = first
    if stage:
        t2 = next_below(first[0], atr) or (max(first[0] - 2 * atr, first[0] * 0.9), "2 average daily ranges lower")
        if strong < t2[0] - 0.5 * atr:
            t3 = (strong, "strong-buy price")
        else:
            t3 = next_below(t2[0], atr) or (t2[0] - 2 * atr, "2 more daily ranges lower")
        out["tranches"] = [(first[0], 1 / 3, first[1]), (t2[0], 1 / 3, t2[1]), (t3[0], 1 / 3, t3[1])]
        out["note"] = ("The price is sliding (below a falling 50-day average), so the plan buys in thirds at "
                       "successive support levels instead of guessing the bottom. Whatever doesn't fill, the "
                       "buy-below price still stands.")
    else:
        out["tranches"] = [(first[0], 1.0, first[1])]
        if out["status"] == "in_zone":
            out["note"] = ("Already in the Buy Zone." + (f" A limit at the {first[1]} is within one average day's "
                                                         "move, so it may fill cheaper." if near else
                                                         " No support level is close, so buying at the open is fine."))
        else:
            gap = price / first[0] - 1
            out["note"] = (f"Not cheap enough yet: {gap:.1%} above the suggested limit. A good-till-cancelled limit at "
                           f"the {first[1]} buys only once the price is right.")
    return out


# ------------------------------------------------------------------ alerts
def _targets_for(cik: int, p: dict, owned: bool, custom: list) -> list[tuple[str, float, str, str]]:
    """(kind, target, direction, label). direction 'down' = alert as price falls to it, 'up' = as it rises."""
    out = []
    if p.get("buy_below"):
        if not owned:
            if p.get("limit") and abs(p["limit"] / p["buy_below"] - 1) > 0.005 and p["limit"] < p["buy_below"]:
                out.append(("limit", p["limit"], "down", f"suggested limit ({p['limit_label']})"))
            out.append(("buy_below", p["buy_below"], "down", "buy-below price"))
        if p.get("strong"):
            out.append(("strong_buy", p["strong"], "down", "strong-buy price" + (" (add zone)" if owned else "")))
        if owned and p.get("trim_above"):
            out.append(("trim", p["trim_above"], "up", "trim level (2× value)"))
    for c in custom:
        if c["cik"] == cik:
            out.append((f"custom:{c['id']}", c["price"], "down" if c["direction"] == "below" else "up",
                        "your alert price" + (f" ({c['note']})" if c["note"] else "")))
    return out


def classify(direction: str, target: float, close: float, low: float, high: float, bnd: float) -> tuple[str, float]:
    """Return (zone, distance). distance > 0 means the target is still ahead."""
    if direction == "down":
        dist = close / target - 1
        if close <= target or low <= target:
            return "reached", dist
    else:
        dist = 1 - close / target
        if close >= target or high >= target:
            return "reached", dist
    if dist <= bnd:
        return "approaching", dist
    if dist > 2 * bnd:
        return "far", dist
    return "between", dist  # hysteresis zone: keep whatever state we were in


RANK = {"idle": 0, "approaching": 1, "reached": 2}


def step(prev: str, zone: str) -> tuple[str, bool]:
    """State machine with hysteresis. Returns (new_state, emit_event)."""
    prev = prev or "idle"
    if zone == "far":
        return "idle", False
    if zone == "between":
        return prev, False
    if RANK[zone] > RANK[prev]:
        return zone, True
    return prev, False


def watch_set(run_id: int, d: str) -> list[dict]:
    pid = db.scalar("SELECT id FROM portfolios WHERE run_id=? ORDER BY id LIMIT 1", (run_id,))
    return [dict(r) for r in db.rows(
        """SELECT e.cik, s.ticker, s.name, e.list, e.price, e.buy_below, e.strong_buy_below, e.iv,
                  (p.cik IS NOT NULL OR h.cik IS NOT NULL) AS owned
           FROM evaluations e JOIN securities s ON s.cik=e.cik
           LEFT JOIN positions p ON p.portfolio_id=? AND p.cik=e.cik
           LEFT JOIN real_holdings h ON h.cik=e.cik
           WHERE e.run_id=? AND e.date=? AND s.ticker IS NOT NULL
             AND (e.list IN ('buy','pricey','near') OR p.cik IS NOT NULL OR h.cik IS NOT NULL
                  OR e.cik IN (SELECT cik FROM custom_targets WHERE active=1))""", (pid, run_id, d))]


def plan_for(run_id: int, cik: int, st: dict) -> tuple[dict, dict | None]:
    r = db.one("""SELECT e.price, e.buy_below, e.strong_buy_below, e.iv, s.ticker FROM evaluations e
                  JOIN securities s ON s.cik=e.cik WHERE e.run_id=? AND e.cik=? ORDER BY e.date DESC LIMIT 1""",
               (run_id, cik))
    if not r:
        return plan(None, None, None, None, None, st), None
    t = technicals(r["ticker"])
    price = t["close"] if t else r["price"]
    return plan(price, r["buy_below"], r["strong_buy_below"], r["iv"], t, st), t


def scan(run_id: int, st: dict, d: str | None = None) -> list[dict]:
    """Update alert states for the watch set; store and return new alert events."""
    if not st.get("price_alerts", True):
        return []
    d = d or db.scalar("SELECT MAX(date) FROM evaluations WHERE run_id=?", (run_id,))
    if not d:
        return []
    custom = [dict(c) for c in db.rows("SELECT * FROM custom_targets WHERE active=1")]
    states = {(r["cik"], r["kind"]): r["state"] for r in db.rows("SELECT cik, kind, state FROM alert_state")}
    events = []
    con = db.get()
    rows = watch_set(run_id, d)
    # custom targets on stocks that aren't evaluated today still need watching
    seen = {r["cik"] for r in rows}
    for c in custom:
        if c["cik"] not in seen:
            s = db.one("SELECT cik, ticker, name FROM securities WHERE cik=?", (c["cik"],))
            if s and s["ticker"]:
                rows.append({"cik": s["cik"], "ticker": s["ticker"], "name": s["name"], "list": "", "price": None,
                             "buy_below": None, "strong_buy_below": None, "iv": None, "owned": 0})
                seen.add(s["cik"])
    con.execute("BEGIN IMMEDIATE")
    try:
        for r in rows:
            t = technicals(r["ticker"], d)
            if not t:
                continue
            p = plan(t["close"], r["buy_below"], r["strong_buy_below"], r["iv"], t, st)
            bnd = p["band"] or band(t["close"], t["atr"], st)
            for kind, target, direction, label in _targets_for(r["cik"], p, bool(r["owned"]), custom):
                if not target or target <= 0:
                    continue
                zone, dist = classify(direction, target, t["close"], t["low"], t["high"], bnd)
                new, emit = step(states.get((r["cik"], kind)), zone)
                con.execute("INSERT OR REPLACE INTO alert_state(cik,kind,state,target,updated) VALUES(?,?,?,?,?)",
                            (r["cik"], kind, new, target, t["date"]))
                if emit:
                    msg = _message(r["ticker"], new, label, target, t, dist, direction)
                    con.execute("INSERT INTO price_alerts(cik,kind,date,state,target,price,distance,message,created_at)"
                                " VALUES(?,?,?,?,?,?,?,?,?)",
                                (r["cik"], kind, t["date"], new, target, t["close"], dist, msg, db.now()))
                    events.append({"cik": r["cik"], "ticker": r["ticker"], "name": r["name"], "kind": kind,
                                   "state": new, "target": target, "price": t["close"], "message": msg})
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return events


def _message(ticker, state, label, target, t, dist, direction) -> str:
    ref = label if label.startswith("your") else f"its {label}"
    if state == "reached":
        closed = t["close"] <= target if direction == "down" else t["close"] >= target
        how = "closed at" if closed else "touched it during the day; closed at"
        return f"{ticker} reached {ref} ${target:,.2f} ({how} ${t['close']:,.2f})"
    days = abs(dist) * t["close"] / t["atr"] if t.get("atr") else None
    side = "above" if direction == "down" else "below"
    return (f"{ticker} is {abs(dist):.1%} {side} {ref} ${target:,.2f}"
            + (f" (about {days:.1f} average days' move)" if days else ""))


def recent_alerts(days: int = 7, cik: int | None = None) -> list[dict]:
    q = """SELECT a.*, s.ticker, s.name FROM price_alerts a JOIN securities s ON s.cik=a.cik
           WHERE a.date >= date((SELECT MAX(date) FROM price_alerts), ?)"""
    args: list = [f"-{days} days"]
    if cik:
        q += " AND a.cik=?"
        args.append(cik)
    return [dict(r) for r in db.rows(q + " ORDER BY a.date DESC, a.state DESC, s.ticker", args)]
