"""Judging a fund by what it owns (look-through) and by how it is run: the Fund Score.

Look-through: each holding is matched to a company the app scores, and its weight is moved forward from the filing
date by the stock's price change since, so the mix is close to today's. Then:
- Business Score: the weighted average of the holdings' Combined scores ((Quality + Wonderfulness) / 2), by their
  share of the fund, over the holdings the app scores on both. How good the businesses it owns are, on average; price
  plays no part. The headline number.
- Wonderful (70+), quality (passes the checklist at or above the strategy's bar), both, at or below value, in the Buy
  Zone, failing the checklist: each as a share of the fund.
- Weighted Quality, Wonderfulness and Combined over the part the app scores; coverage says how big that part is.
- Look-through value: each valued holding's fair value / price, weighted. 1.20 means the holdings are worth 20% more
  than their prices. The fund's own fair value per share follows (unvalued holdings counted at their price).

Fund Score (0-100), four pillars, each from plain thresholds (see PILLARS):
- What it owns (35%): the Business Score, and the share in businesses that are both wonderful and quality.
- Price (15%): look-through value and expected return.
- Track record (25%): yearly return over up to 10 years against its benchmark (the S&P 500 for US stock funds),
  return per unit of risk (Sharpe), worst drop against the benchmark's, and how often it beat the benchmark over
  rolling 12-month periods.
- Cost & structure (25%): expense ratio (and against its category), size, age, trading volume, turnover, and how
  close the price stays to the value of what it holds (premium / discount to NAV).
A pillar with little data is pulled toward 50 (neutral); one with no data is left out and the rest re-weighted.
"""

from __future__ import annotations

import bisect
import json
import math
from datetime import date

from . import db, rules

WONDERFUL = 70.0
PILLAR_WEIGHTS = {"owns": 0.35, "price": 0.15, "track": 0.25, "cost": 0.25}
ETF_MOS = 0.10  # holdings at least 10% below their value: the fund is a buy on price


def ramp(x, lo, hi) -> float | None:
    """0 at lo, 100 at hi, straight line between (works for hi < lo too)."""
    if x is None:
        return None
    if hi == lo:
        return 100.0 if x >= hi else 0.0
    return max(0.0, min(100.0, (x - lo) / (hi - lo) * 100))


def _blend(parts: list[tuple[float | None, float]]) -> float | None:
    have = [(v, w) for v, w in parts if v is not None]
    tw = sum(w for _, w in have)
    return sum(v * w for v, w in have) / tw if tw else None


def _shrink(v: float | None, conf: float) -> float | None:
    """Pull a pillar toward 50 when it rests on little data (conf 0..1)."""
    return None if v is None else 50 + (v - 50) * max(0.0, min(1.0, conf))


# ===================================================================== company scores, prices
class Book:
    """Everything look-through needs about companies, loaded once: Quality, must-pass result, Wonderfulness, fair
    value, expected return, buy price, and closing prices on the days asked for."""

    def __init__(self, profile=None):
        p = profile or rules.default_profile()
        self.p = p
        self.qmin = float(p.settings.get("quality_min", 70))
        self.co: dict[int, dict] = {}
        for r in db.rows("SELECT cik, ticker, name, sic FROM securities WHERE ticker IS NOT NULL"):
            self.co[r["cik"]] = {"ticker": r["ticker"], "name": r["name"], "sic": r["sic"]}
        for r in db.rows(
                "SELECT s.cik, s.quality, s.gates_passed FROM snapshots s JOIN (SELECT cik, MAX(filed) f FROM snapshots "
                "WHERE profile_id=? AND profile_version=? GROUP BY cik) m ON s.cik=m.cik AND s.filed=m.f "
                "WHERE s.profile_id=? AND s.profile_version=?", (*p.snap, *p.snap)):
            if r["cik"] in self.co:
                self.co[r["cik"]].update(q=r["quality"], gates=bool(r["gates_passed"]))
        for r in db.rows("SELECT cik, score FROM wonder WHERE profile_id=? AND cohort='now'", (p.snap[0],)):
            if r["cik"] in self.co:
                self.co[r["cik"]]["w"] = r["score"]
        for r in db.rows("SELECT cik, iv, er, buy_below, price, pe_now FROM vd_values"):
            if r["cik"] in self.co and r["iv"] and r["iv"] > 0:
                self.co[r["cik"]].update(iv=r["iv"], er=r["er"], buy_below=r["buy_below"], pe=r["pe_now"],
                                         price=r["price"])
        self._px: dict[str, dict[str, float]] = {}
        self.spy_dates = [r["date"] for r in db.rows("SELECT date FROM prices WHERE ticker='SPY' ORDER BY date")]

    def trading_day(self, d: str | None) -> str | None:
        if not self.spy_dates:
            return None
        if d is None:
            return self.spy_dates[-1]
        i = bisect.bisect_right(self.spy_dates, d) - 1
        return self.spy_dates[max(i, 0)]

    def closes(self, d: str | None) -> dict[str, float]:
        """Every ticker's close on the trading day on or before d (None = the latest)."""
        td = self.trading_day(d)
        if td is None:
            return {}
        if td not in self._px:
            self._px[td] = {r["ticker"]: r["close"] for r in db.rows(
                "SELECT ticker, close FROM prices WHERE date=? AND close > 0", (td,))}
        return self._px[td]


# ===================================================================== look-through
def look_through(etf: str, book: Book | None = None) -> dict:
    """The fund's holdings joined to company scores, weights moved to today's prices. Returns
    {"rows": [...], "m": metrics} (metrics as fractions of the whole fund unless named *_scored)."""
    book = book or Book()
    hs = [dict(r) for r in db.rows("SELECT * FROM etf_holdings WHERE etf=? AND weight > 0", (etf,))]
    if not hs:
        return {"rows": [], "m": {}}
    asof = hs[0]["asof"]
    now, then = book.closes(None), book.closes(asof)
    # weights moved forward by each stock's price change since the filing (the rest by the average change)
    drifts = []
    for h in hs:
        c = book.co.get(h["cik"]) if h["cik"] else None
        t = c["ticker"] if c else (h["ticker"] or "")
        p0, p1 = then.get(t), now.get(t)
        h["price"] = p1
        h["drift"] = (p1 / p0) if (p0 and p1) else None
        if h["drift"]:
            drifts.append((h["weight"], h["drift"]))
    avg = (sum(w * d for w, d in drifts) / sum(w for w, _ in drifts)) if drifts else 1.0
    tot0 = sum(h["weight"] for h in hs)
    for h in hs:
        h["w_now"] = h["weight"] * (h["drift"] or avg)
    s = sum(h["w_now"] for h in hs) or 1.0
    for h in hs:
        h["w"] = h["w_now"] / s  # fraction of the fund's long positions, today
    m = {"asof": asof, "n": len(hs), "filed_weight": tot0 / 100, **score_rows(hs, book)}
    hs.sort(key=lambda h: -h["w"])
    return {"rows": hs, "m": m}


def score_rows(hs: list[dict], book: Book) -> dict:
    """Look-through metrics for weighted positions (each {"w": fraction, "cik", "price", "asset_cat"}); adds the
    company's scores and a flag to each row. Used for one fund and for a whole portfolio (the X-ray)."""
    m: dict = {}
    acc = dict.fromkeys(("scored", "wonderful", "quality", "both", "valued", "at_value", "buy_zone", "fails",
                         "equity"), 0.0)
    qn = wn = cn = 0.0
    qs = ws = cs = 0.0
    vr_w = vr = er_w = er = 0.0
    for h in hs:
        w = h["w"]
        c = book.co.get(h.get("cik")) if h.get("cik") else None
        cat = h.get("asset_cat") or ("EC" if c else "")
        if cat in ("EC", "EP") or c:
            acc["equity"] += w
        h.update(company=c is not None, q=None, wscore=None, iv=None, ratio=None, flag="")
        if not c:
            continue
        q, ww = c.get("q"), c.get("w")
        h.update(q=q, wscore=ww, gates=c.get("gates"), ticker_m=c["ticker"], name_m=c["name"])
        if q is None and ww is None:
            continue
        acc["scored"] += w
        is_w = ww is not None and ww >= WONDERFUL
        is_q = bool(c.get("gates")) and q is not None and q >= book.qmin
        if is_w:
            acc["wonderful"] += w
        if is_q:
            acc["quality"] += w
        if is_w and is_q:
            acc["both"] += w
        if c.get("gates") is False:
            acc["fails"] += w
        if q is not None:
            qs += w * q
            qn += w
        if ww is not None:
            ws += w * ww
            wn += w
        if q is not None and ww is not None:
            cs += w * (q + ww) / 2
            cn += w
        px = h.get("price") or c.get("price")
        if c.get("iv") and px:
            ratio = c["iv"] / px
            h.update(iv=c["iv"], ratio=ratio)
            acc["valued"] += w
            vr += w * ratio
            vr_w += w
            if ratio >= 1:
                acc["at_value"] += w
            if c.get("buy_below") and px <= c["buy_below"]:
                acc["buy_zone"] += w
            if c.get("er") is not None:
                er += w * c["er"]
                er_w += w
        h["flag"] = "oracle" if (is_w and is_q and h["ratio"] and h["ratio"] >= 1) else (
            "both" if is_w and is_q else "wonderful" if is_w else "quality" if is_q else
            "fails" if c.get("gates") is False else "")
    m.update(acc)
    sc = acc["scored"] or None
    m.update({
        "coverage": acc["scored"], "wonderful_scored": acc["wonderful"] / sc if sc else None,
        "quality_scored": acc["quality"] / sc if sc else None, "both_scored": acc["both"] / sc if sc else None,
        "avg_q": qs / qn if qn else None, "avg_w": ws / wn if wn else None, "avg_comb": cs / cn if cn else None,
        "business": cs / cn if cn else None, "business_cov": cn,
        "value_ratio": vr / vr_w if vr_w else None, "lt_er": er / er_w if er_w else None,
        "price_vs_value": (vr_w / vr - 1) if vr else None})
    tot = sum(h["w"] for h in hs) or 1.0
    ws_sorted = sorted((h["w"] / tot for h in hs), reverse=True)
    m["top10"] = sum(ws_sorted[:10])
    m["eff_n"] = 1 / sum(x * x for x in ws_sorted) if ws_sorted else None
    return m


def xray(positions: list[tuple[str, float]], book: Book | None = None) -> dict:
    """A portfolio opened up: [(ticker, dollar value)] -> every underlying company with its direct and through-fund
    dollars, look-through metrics for the whole, and how much the funds overlap. Funds without holdings on file and
    anything that isn't a scored company stay as they are."""
    book = book or Book()
    by_t = {v["ticker"]: k for k, v in book.co.items()}
    tot = sum(v for _, v in positions if v) or 1.0
    expo: dict[str, dict] = {}
    fund_w: dict[str, dict[str, float]] = {}
    for t, val in positions:
        if not val:
            continue
        lt = look_through(t, book) if db.scalar("SELECT 1 FROM etf_holdings WHERE etf=? LIMIT 1", (t,)) else None
        if lt and lt["rows"]:
            fund_w[t] = {}
            for h in lt["rows"]:
                key = f"c{h['cik']}" if h.get("cik") else f"n{(h.get('name') or h.get('title') or '?').lower()}"
                e = expo.setdefault(key, {"cik": h.get("cik"), "name": h.get("name_m") or h.get("name") or h.get("title"),
                                          "ticker": h.get("ticker_m") or h.get("ticker"), "direct": 0.0, "via": 0.0,
                                          "funds": {}, "price": h.get("price"), "asset_cat": h.get("asset_cat")})
                d = val * h["w"]
                e["via"] += d
                e["funds"][t] = e["funds"].get(t, 0.0) + d
                fund_w[t][key] = fund_w[t].get(key, 0.0) + h["w"]
        else:
            cik = by_t.get(t)
            key = f"c{cik}" if cik else f"t{t}"
            e = expo.setdefault(key, {"cik": cik, "name": book.co[cik]["name"] if cik else t, "ticker": t,
                                      "direct": 0.0, "via": 0.0, "funds": {}, "price": book.closes(None).get(t),
                                      "asset_cat": "EC" if cik else None})
            e["direct"] += val
    rows = []
    for e in expo.values():
        e["total"] = e["direct"] + e["via"]
        e["w"] = e["total"] / tot
        rows.append(e)
    m = score_rows(rows, book)
    rows.sort(key=lambda e: -e["total"])
    ft = sorted(fund_w)
    overlap = {(a, b): sum(min(fund_w[a].get(k, 0), fund_w[b].get(k, 0)) for k in fund_w[a])
               for i, a in enumerate(ft) for b in ft[i + 1:]}
    return {"rows": rows, "m": m, "overlap": overlap, "funds": ft, "total": tot}


def sectors(rows: list[dict], book: Book) -> list[tuple[str, float]]:
    from .sectors import sector_of_sic

    by: dict[str, float] = {}
    for h in rows:
        c = book.co.get(h["cik"]) if h.get("cik") else None
        k = sector_of_sic(c["sic"]) if c else ("Bonds & cash" if (h.get("asset_cat") or "").startswith(("DBT", "STIV"))
                                              else "Not scored")
        k = "Other" if k == "Unknown" else k
        by[k] = by.get(k, 0.0) + h["w"]
    return sorted(by.items(), key=lambda x: -x[1])


# ===================================================================== track record, trading
def track(etf: str, bench: str) -> dict:
    from .holdings import Px, stats

    px = Px([etf, bench])
    if not px.has(etf):
        return {}
    dates = px.d[etf]
    if px.has(bench):
        dates = [d for d in dates if d >= px.d[bench][0]]
    if len(dates) < 60:
        return {"years": len(dates) / 252}
    end = dates[-1]
    start10 = date.fromisoformat(end).replace(year=date.fromisoformat(end).year - 10).isoformat()
    dates = [d for d in dates if d >= start10]
    g = [px.adj(etf, d) for d in dates]
    b = [px.adj(bench, d) for d in dates] if px.has(bench) else None
    st = stats(dates, g, b)
    out = {"years": st.get("years"), "ann": st.get("ann"), "vol": st.get("vol"), "max_dd": st.get("max_dd"),
           "sharpe": st.get("sharpe"), "sortino": st.get("sortino"), "beta": st.get("beta"),
           "bench_ann": st.get("bench_ann"), "excess": st.get("excess"), "up_capture": st.get("up_capture"),
           "down_capture": st.get("down_capture"), "start": dates[0], "bench": bench}
    for yrs in (1, 3, 5, 10):
        d0 = date.fromisoformat(end).replace(year=date.fromisoformat(end).year - yrs).isoformat()
        if dates[0] <= d0:
            a0, a1 = px.adj(etf, d0), px.adj(etf, end)
            out[f"r{yrs}"] = (a1 / a0) ** (1 / yrs) - 1 if a0 else None
            if b:
                b0, b1 = px.adj(bench, d0), px.adj(bench, end)
                out[f"b{yrs}"] = (b1 / b0) ** (1 / yrs) - 1 if b0 else None
    if b:
        bpk, bdd = b[0], 0.0
        for v in b:
            bpk = max(bpk, v)
            bdd = min(bdd, v / bpk - 1)
        out["bench_max_dd"] = bdd
        # rolling 12-month periods (month ends, last 5 years): how often it beat the benchmark
        me = []
        for i, d in enumerate(dates):
            if i + 1 == len(dates) or dates[i + 1][:7] != d[:7]:
                me.append((g[i], b[i]))
        me = me[-61:]
        wins = [1.0 if me[i][0] / me[i - 12][0] > me[i][1] / me[i - 12][1] else 0.0 for i in range(12, len(me))]
        out["beat_share"] = sum(wins) / len(wins) if wins else None
    return out


def trading(etf: str) -> dict:
    rows = db.rows("SELECT close, volume FROM prices WHERE ticker=? ORDER BY date DESC LIMIT 63", (etf,))
    dv = [r["close"] * r["volume"] for r in rows if r["close"] and r["volume"]]
    return {"dollar_volume": sum(dv) / len(dv) if dv else None, "price": rows[0]["close"] if rows else None}


# ===================================================================== the Fund Score
def fund_score(f: dict, lt: dict, tr: dict, tx: dict) -> dict:
    """Pillars and the overall score, each with the parts behind it (shown on the fund's page)."""
    m = lt or {}
    cov = m.get("coverage") or 0.0
    eq = m.get("equity") or 0.0
    parts: dict[str, list] = {"owns": [], "price": [], "track": [], "cost": []}
    if eq >= 0.5 and cov > 0:
        parts["owns"] = [("Business Score", ramp(m.get("business"), 40, 80), 0.75),
                         ("Share in wonderful and quality businesses (of what's scored)",
                          ramp(m.get("both_scored"), 0.0, 0.6), 0.25)]
        if m.get("value_ratio") is not None:
            parts["price"] = [("Holdings' value vs price", ramp(m.get("value_ratio"), 0.6, 1.2), 0.7),
                              ("Expected return of the holdings", ramp(m.get("lt_er"), 0.02, 0.10), 0.3)]
    if tr.get("ann") is not None:
        parts["track"] = [("Yearly return vs benchmark", ramp(tr.get("excess"), -0.04, 0.04), 0.4),
                          ("Return per unit of risk (Sharpe)", ramp(tr.get("sharpe"), 0.0, 1.2), 0.25),
                          ("Worst drop vs benchmark's", ramp((tr.get("max_dd") or 0) - (tr.get("bench_max_dd") or 0),
                                                              -0.15, 0.05) if tr.get("bench_max_dd") is not None
                           else None, 0.15),
                          ("Beat the benchmark (rolling 12 months)", ramp(tr.get("beat_share"), 0.2, 0.8), 0.2)]
    exp = f.get("expense")
    age = ((date.today() - date.fromisoformat(f["inception"])).days / 365.25) if f.get("inception") else (
        tr.get("years"))
    prem = (tx["price"] / f["nav"] - 1) if (tx.get("price") and f.get("nav")) else None
    parts["cost"] = [
        ("Expense ratio", ramp(exp, 0.0075, 0.0005) if exp is not None else None, 0.25),
        ("Expense vs its category", ramp((f.get("expense_cat") or 0) - exp, -0.002, 0.003)
         if (exp is not None and f.get("expense_cat")) else None, 0.10),
        ("Size (assets)", ramp(math.log10(f["aum"]), 8, 10) if f.get("aum") else None, 0.20),
        ("Age", ramp(age, 1, 10) if age is not None else None, 0.10),
        ("Trading volume", ramp(math.log10(tx["dollar_volume"]), 6, 8) if tx.get("dollar_volume") else None, 0.15),
        ("Turnover", ramp(f.get("turnover"), 1.0, 0.05) if f.get("turnover") is not None else None, 0.10),
        ("Price vs NAV", ramp(abs(prem), 0.01, 0.001) if prem is not None else None, 0.10)]
    pill = {}
    for k, ps in parts.items():
        v = _blend([(v, w) for _, v, w in ps])
        if k == "owns":
            v = _shrink(v, (m.get("business_cov") or 0) / 0.6)  # little of the fund scored: closer to neutral
        if k == "price":
            v = _shrink(v, cov / 0.6)
        if k == "track":
            v = _shrink(v, (tr.get("years") or 0) / 3)
        pill[k] = v
    score = _blend([(pill[k], PILLAR_WEIGHTS[k]) for k in pill])
    return {"score": score, "pillars": pill,
            "parts": {k: [(lab, v, w) for lab, v, w in ps] for k, ps in parts.items()},
            "premium": prem, "age": age}


def verdict(m: dict, score: float | None) -> tuple[str, str]:
    """A badge for the fund on price: BUY when what it holds trades at least 10% below its value (with most of the
    fund valued), PRICEY when 25%+ above, else FAIR."""
    pv, val = m.get("price_vs_value"), m.get("valued") or 0
    if pv is None or val < 0.4:
        return "HOLD", "too little of the fund is valued to judge its price"
    if pv <= -ETF_MOS and (score or 0) >= 55:
        return "BUY", f"its holdings trade {abs(pv):.0%} below their value"
    if pv >= 0.25:
        return "TRIM" if pv >= 0.6 else "WATCH", f"its holdings trade {pv:.0%} above their value"
    return "HOLD", f"its holdings trade {'+' if pv >= 0 else '−'}{abs(pv):.0%} vs their value"


def score_one(t: str, book: Book) -> dict | None:
    from . import etfs

    f = etfs.get(t) or {}
    lt = look_through(t, book)
    bench = f.get("benchmark") or etfs.benchmark_for(f.get("category"), t)
    tr = track(t, bench)
    tx = trading(t)
    if not (lt["m"] or tr or f.get("expense") is not None):
        return None
    fs = fund_score(f, lt["m"], tr, tx)
    m = {**lt["m"], **{f"t_{k}": v for k, v in tr.items()}, **tx, "premium": fs["premium"], "age": fs["age"]}
    if m.get("value_ratio") and tx.get("price"):
        valued = m.get("valued") or 0
        m["fair_value"] = tx["price"] * (1 + valued * (m["value_ratio"] - 1))
    m["verdict"], m["verdict_why"] = verdict(lt["m"], fs["score"])
    db.get().execute("INSERT OR REPLACE INTO etf_scores(etf, date, score, pillars_json, metrics_json, updated_at) "
                     "VALUES(?,?,?,?,?,?)", (t, date.today().isoformat(), fs["score"],
                                             json.dumps({"pillars": fs["pillars"], "parts": fs["parts"]}),
                                             json.dumps(m, default=str), db.now()))
    return {"score": fs["score"], **fs, "m": m}


def score_all(tickers: list[str] | None = None) -> int:
    book = Book()
    tks = tickers or [r["ticker"] for r in db.rows("SELECT ticker FROM etfs")]
    n = 0
    for t in tks:
        try:
            if score_one(t, book):
                n += 1
        except Exception as e:
            db.log("etf", "score", False, f"{t}: {e}"[:300])
    return n


def scores() -> dict[str, dict]:
    """Every fund's stored score and metrics: ticker -> {score, pillars, parts, m}."""
    out = {}
    for r in db.rows("SELECT * FROM etf_scores"):
        pj = json.loads(r["pillars_json"] or "{}")
        out[r["etf"]] = {"score": r["score"], "pillars": pj.get("pillars", {}), "parts": pj.get("parts", {}),
                         "m": json.loads(r["metrics_json"] or "{}"), "date": r["date"]}
    return out
