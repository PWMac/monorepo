"""Invest my cash: a suggested set of orders for one account, from a strategy and today's market.

1. Reserve: how much cash to keep back. Starts from the market's state, the S&P 500 against its 200-day average
   with the VIX (normal / caution / stress), plus how expensive the whole market is (Buffett Indicator zone).
   The strategy's own market-overlay settings are used when it has them switched on.
2. Sells and trims: holdings the strategy would sell (fails the checklist, Quality below its sell level) or trim
   (a nose-bleed price, or above the most one holding may weigh). Their proceeds can go toward the buys.
3. Buys: the strategy's Buy Zone names, best-ranked first, across at most N names, never pushing one holding above
   the strategy's most a new purchase may weigh, each as a limit order at the lower of today's price and the
   strategy's buy price.

Only a plan: nothing is sent to the broker. You place the orders yourself.
"""

from __future__ import annotations

import csv
import io
import json
import math
from datetime import date

from . import db, holdings, rules

OVERLAY_RESERVE = {"normal": 0.05, "caution": 0.25, "stress": 0.45}
ZONE_RESERVE = {"Undervalued": 0.0, "Fair": 0.0, "Modestly overvalued": 0.05, "Significantly overvalued": 0.10,
                "Extreme": 0.15, "Unknown": 0.0}
MAX_RESERVE = 0.70


def market_view(st: dict | None = None) -> dict:
    """Today's market state and the reserve it suggests, with the reasons in plain words."""
    from .engine import PriceBook
    from .market import Macro, overlay_factor, overlay_state

    st = st or rules.default_profile().settings
    pb = PriceBook(["SPY", "VTI"])
    macro = Macro()
    d = db.scalar("SELECT MAX(date) FROM prices WHERE ticker='SPY'") or date.today().isoformat()
    state = overlay_state(pb, macro, d, st)
    reg = macro.regime(d, st.get("mos_gate", 0.25), False, lambda x: pb.close("VTI", x))
    px, ma = pb.adj("SPY", d), pb.sma("SPY", d, int(st.get("overlay_ma_days", 200)))
    _, vix = macro.asof("VIXCLS", d)
    why = []
    if st.get("market_overlay"):
        base = 1 - overlay_factor(state, st)
        why.append(f"This strategy's market overlay: {state} → buy {overlay_factor(state, st):.0%} of the usual "
                   f"amount, so keep {base:.0%} back")
    else:
        base = OVERLAY_RESERVE[state]
        why.append({"normal": "S&P 500 above its 200-day average: keep a small cushion (5%)",
                    "caution": "S&P 500 below its 200-day average: keep a quarter back to buy lower",
                    "stress": "S&P 500 below its 200-day average and the VIX high: keep nearly half back"}[state])
    z = ZONE_RESERVE.get(reg.zone, 0.0)
    if z:
        why.append(f"Whole market {reg.zone.lower()} (Buffett Indicator {reg.ratio:.0%}): keep {z:.0%} more")
    return {"date": d, "state": state, "zone": reg.zone, "buffett": reg.ratio, "spy": px, "spy_ma": ma, "vix": vix,
            "spy_vs_ma": (px / ma - 1) if (px and ma) else None, "reserve": min(base + z, MAX_RESERVE), "why": why}


def _verdict(ev: dict | None, h: holdings.Holding, st: dict, total: float) -> tuple[str, str, float]:
    """(action, reason, fraction of the holding to sell) for one holding under the strategy."""
    if ev is None:
        return "HOLD", "not scored by this strategy (a fund, or outside its universe)", 0.0
    if not ev.get("gates_passed") and ev.get("failed"):
        return "SELL", "fails the checklist: " + ", ".join(ev["failed"][:3]), 1.0
    q = ev.get("quality")
    if q is not None and q < st.get("sell_quality", 55):
        return "SELL", f"Quality {q:.0f} is below the sell level {st.get('sell_quality', 55)}", 1.0
    p, iv = ev.get("price"), ev.get("iv")
    if p and iv and p >= st.get("nosebleed_iv_mult", 2.0) * iv and ev.get("pe_now") and ev.get("pe_median") and \
            ev["pe_now"] >= st.get("nosebleed_pe_mult", 2.0) * ev["pe_median"]:
        f = float(st.get("trim_fraction", 0.33))
        return "TRIM", f"nose-bleed price: {p / iv:.1f}× value and P/E {ev['pe_now']:.0f} vs usual " \
                       f"{ev['pe_median']:.0f}", f
    cap = st.get("max_position") or st.get("position_cap")
    tol = float(st.get("max_position_tolerance", 0.05))
    if cap and total and h.value and h.value / total > float(cap) + tol:
        over = h.value / total - float(cap)
        return "TRIM", f"{h.value / total:.0%} of the account, above the {float(cap):.0%} limit", over / (h.value / total)
    if ev.get("list") == "buy":
        return "ADD", "still in the Buy Zone", 0.0
    if ev.get("expensive_above") and p and p > ev["expensive_above"]:
        return "HOLD", "business intact, but expensive: hold, don't add", 0.0
    return "HOLD", "business intact", 0.0


def _from_snapshot(cik: int, p) -> dict | None:
    """A held company today's evaluation skipped (it fails the checklist, so it isn't a candidate): its latest
    scores under the strategy."""
    r = db.one("SELECT gates_passed, failed_gates, quality FROM snapshots WHERE cik=? AND profile_id=? AND "
               "profile_version=? ORDER BY filed DESC LIMIT 1", (cik, *p.snap))
    if not r:
        return None
    failed = [f.get("name") for f in json.loads(r["failed_gates"] or "[]") if isinstance(f, dict)
              and f.get("id") != "u1"]
    return {"gates_passed": bool(r["gates_passed"]), "failed": failed, "quality": r["quality"], "list": "out"}


def etf_candidates(min_score: float = 60) -> list[dict]:
    """ETFs in their own Buy Zone: holdings at least 10% below their value, Fund Score 60+, best score first."""
    from . import etfscore

    out = []
    for t, s in etfscore.scores().items():
        m = s.get("m") or {}
        if m.get("verdict") == "BUY" and (s.get("score") or 0) >= min_score and m.get("price"):
            out.append({"ticker": t, "list": "buy", "price": m["price"], "buy_below": None, "rank": None,
                        "rank_score": (s["score"] or 0) / 100, "quality": m.get("avg_q"), "iv": m.get("fair_value"),
                        "etf": True, "why": f"ETF: holdings {abs(m.get('price_vs_value') or 0):.0%} below their value · "
                                            f"Fund Score {s['score']:.0f} · Oracle Score {m.get('business') or 0:.0f}"})
    return sorted(out, key=lambda v: -v["rank_score"])


def build(account_id: int | None, profile_id: int | None = None, reserve: float | None = None, max_names: int | None
          = None, use_sales: bool = True, cash: float | None = None, evals: dict | None = None,
          universe: str = "stocks") -> dict:
    """The plan. `evals` (from pipeline.evaluate_now) can be passed in to skip the evaluation."""
    from .pipeline import evaluate_now

    p = rules.get_profile(profile_id) if profile_id else rules.default_profile()
    st = p.settings
    scope = holdings.Scope([account_id] if account_id else None)
    hs, px = holdings.holdings(scope)
    have_cash = holdings.cash(scope) if cash is None else float(cash)
    invested = sum(h.value or 0 for h in hs)
    total = invested + have_cash
    mv = market_view(st)
    res_pct = mv["reserve"] if reserve is None else float(reserve)
    ev_all = (evals or evaluate_now(p))["evals"]
    by_ticker = {v.get("ticker"): v for v in ev_all.values() if v.get("ticker")}
    rows, notes = [], []
    proceeds = 0.0
    held = {h.ticker: h for h in hs}
    for h in hs:
        ev = by_ticker.get(h.ticker) or (_from_snapshot(h.cik, p) if h.cik else None)
        act, why, frac = _verdict(ev, h, st, total)
        h_row = {"ticker": h.ticker, "verdict": act, "why": why, "weight": h.value / total if total else None}
        notes.append(h_row)
        if act in ("SELL", "TRIM") and h.price:
            n = h.shares if act == "SELL" else math.floor(h.shares * frac)
            if n <= 0:
                continue
            lim = round(h.price, 2)
            rows.append({"include": True, "action": act, "ticker": h.ticker, "shares": n, "limit": lim,
                         "amount": n * lim, "reason": why, "type": "Limit"})
            proceeds += n * lim
    reserve_amt = total * res_pct
    budget = max(have_cash + (proceeds if use_sales else 0.0) - reserve_amt, 0.0)
    cands = [v for v in ev_all.values() if v.get("list") == "buy" and v.get("price") and v.get("ticker")]
    cands.sort(key=lambda v: (v["rank"] if v.get("rank") is not None else 10 ** 6, -(v.get("rank_score") or 0)))
    if universe in ("both", "etfs"):
        ec = etf_candidates()
        if universe == "etfs":
            cands = ec
        else:  # take turns: best stock, best ETF, ...
            mixed = []
            for i in range(max(len(cands), len(ec))):
                mixed += ([cands[i]] if i < len(cands) else []) + ([ec[i]] if i < len(ec) else [])
            cands = mixed
    selling = {r["ticker"] for r in rows if r["action"] == "SELL"}
    cands = [v for v in cands if v["ticker"] not in selling]
    n_names = max_names or st.get("max_buys_per_week") or 4
    cap = float(st.get("max_buy_weight") or 0.25)
    picks = []
    left = budget
    for v in cands:
        if len(picks) >= n_names or left < 1:
            break
        picks.append(v)
    each = budget / len(picks) if picks else 0.0
    for v in picks:
        h = held.get(v["ticker"])
        room = max(cap * total - (h.value if h and h.value else 0.0), 0.0)
        amt = min(each, room, left)
        lim = round(min(v["price"], v["buy_below"]) if v.get("buy_below") else v["price"], 2)
        n = math.floor(amt / lim) if lim else 0
        if n <= 0:
            continue
        if v.get("etf"):
            why = v["why"]
        else:
            why = f"Buy Zone #{v['rank']}" if v.get("rank") else "in the Buy Zone"
            if v.get("quality") is not None:
                why += f" · Quality {v['quality']:.0f}"
            if v.get("iv"):
                why += f" · value {v['iv']:,.2f} ({v['price'] / v['iv'] - 1:+.0%})"
        if h and h.value and room < each:
            why += f" · capped at {cap:.0%} of the account"
        rows.append({"include": True, "action": "BUY", "ticker": v["ticker"], "shares": n, "limit": lim,
                     "amount": n * lim, "reason": why, "type": "Limit"})
        left -= n * lim
    buys = sum(r["amount"] for r in rows if r["action"] == "BUY")
    return {"date": mv["date"], "account_id": account_id, "profile_id": p.id, "profile": p.name, "cash": have_cash,
            "invested": invested, "total": total, "market": mv, "reserve_pct": res_pct, "reserve": reserve_amt,
            "proceeds": proceeds, "budget": budget, "buys": buys, "cash_after": have_cash + proceeds - buys,
            "rows": rows, "holdings": notes, "max_names": n_names, "candidates": len(cands),
            "outside": len(ev_all) == 0}


def to_csv(plan: dict) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Action", "Symbol", "Quantity", "Order type", "Limit price", "Estimated amount", "Reason"])
    for r in plan["rows"]:
        if r.get("include", True):
            w.writerow([r["action"].replace("TRIM", "SELL"), r["ticker"], r["shares"], r.get("type", "Limit"),
                        f"{r['limit']:.2f}", f"{r['amount']:.2f}", r["reason"]])
    return out.getvalue()


def to_text(plan: dict) -> str:
    lines = [f"Orders for {date.today().isoformat()} ({plan['profile']}):"]
    for r in plan["rows"]:
        if r.get("include", True):
            lines.append(f"{r['action'].replace('TRIM', 'SELL'):4} {r['shares']:>6g} {r['ticker']:<6} limit "
                         f"${r['limit']:,.2f}  (~${r['amount']:,.0f})  {r['reason']}")
    return "\n".join(lines)


def save(plan: dict, name: str = "") -> int:
    cur = db.get().execute("INSERT INTO order_plans(created_at, name, plan_json) VALUES(?,?,?)",
                           (db.now(), name or f"{plan['profile']} · {date.today().isoformat()}",
                            json.dumps(plan, default=str)))
    return int(cur.lastrowid)


def saved(limit: int = 20) -> list[dict]:
    out = []
    for r in db.rows("SELECT * FROM order_plans ORDER BY id DESC LIMIT ?", (limit,)):
        x = dict(r)
        x["plan"] = json.loads(x.pop("plan_json"))
        out.append(x)
    return out


def delete_saved(pid: int) -> None:
    db.get().execute("DELETE FROM order_plans WHERE id=?", (pid,))
