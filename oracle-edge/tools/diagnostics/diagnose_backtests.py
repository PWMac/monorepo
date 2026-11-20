"""Oracle Edge backtest diagnostics (read-only). Writes backtest_diag.json next to this script."""
import json
import sqlite3
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import date
from pathlib import Path

DB = Path(sys.argv[1] if len(sys.argv) > 1 else r"C:\OracleEdge\data\oracle_edge.db")
OUT = Path(__file__).with_name("backtest_diag.json")
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
con.row_factory = sqlite3.Row
q = lambda sql, a=(): [dict(r) for r in con.execute(sql, a).fetchall()]  # noqa: E731

_px = {}


def series(t):
    if t not in _px:
        rs = con.execute("SELECT date, COALESCE(adj_close, close) FROM prices WHERE ticker=? ORDER BY date", (t,)).fetchall()
        _px[t] = ([r[0] for r in rs], [r[1] for r in rs])
    return _px[t]


def px_on(t, d):
    ds, vs = series(t)
    i = bisect_right(ds, d) - 1
    return vs[i] if i >= 0 else None


def window(t, d0, d1):
    ds, vs = series(t)
    i0, i1 = bisect_right(ds, d0) - 1, bisect_right(ds, d1) - 1
    return list(zip(ds[max(i0, 0): i1 + 1], vs[max(i0, 0): i1 + 1]))


def yrs(d0, d1):
    return max((date.fromisoformat(d1) - date.fromisoformat(d0)).days / 365.25, 1e-9)


tk = {r["cik"]: r["ticker"] for r in q("SELECT cik, ticker FROM securities")}
names = {r["cik"]: r["name"] for r in q("SELECT cik, name FROM securities")}
out = {"db": str(DB), "runs": [], "megacaps": []}
runs = q("SELECT r.*, p.name AS pname FROM runs r LEFT JOIN profiles p ON p.id=r.profile_id ORDER BY r.id")
last_px_date = con.execute("SELECT MAX(date) FROM prices WHERE ticker='SPY'").fetchone()[0]

for r in runs:
    eq = q("SELECT date, nav, bench_nav, holdings FROM equity_curve WHERE run_id=? ORDER BY date", (r["id"],))
    if len(eq) < 20:
        continue
    d0, d1 = eq[0]["date"], eq[-1]["date"]
    nav0, nav1 = eq[0]["nav"], eq[-1]["nav"]
    b0 = next((e["bench_nav"] for e in eq if e["bench_nav"]), None)
    b1 = eq[-1]["bench_nav"]
    years = defaultdict(dict)
    for e in eq:
        y = e["date"][:4]
        years[y].setdefault("nav0", e["nav"]); years[y].setdefault("b0", e["bench_nav"])
        years[y]["nav1"], years[y]["b1"] = e["nav"], e["bench_nav"]
    prev = None
    yearly = []
    for y in sorted(years):
        v = years[y]
        a = prev or (v["nav0"], v["b0"])
        yearly.append({"year": y, "model": v["nav1"] / a[0] - 1 if a[0] else None,
                       "sp500": (v["b1"] / a[1] - 1) if (a[1] and v["b1"]) else None})
        prev = (v["nav1"], v["b1"])
    # drawdown
    peak, mdd, mdd_at = 0, 0, None
    for e in eq:
        peak = max(peak, e["nav"])
        dd = e["nav"] / peak - 1
        if dd < mdd:
            mdd, mdd_at = dd, e["date"]
    hold = [e["holdings"] for e in eq]
    zero_days = sum(1 for h in hold if not h)
    first_hold = next((e["date"] for e in eq if e["holdings"]), None)
    pid = con.execute("SELECT id FROM portfolios WHERE run_id=? ORDER BY id LIMIT 1", (r["id"],)).fetchone()
    pid = pid[0] if pid else None
    units = {p["cik"]: p["units"] for p in q("SELECT cik, units FROM positions WHERE portfolio_id=?", (pid,))}
    eps = q("SELECT * FROM episodes WHERE run_id=? ORDER BY entry_date", (r["id"],))
    rows = []
    for e in eps:
        t = tk.get(e["cik"])
        end = e["exit_date"] or d1
        w = window(t, e["entry_date"], end) if t else []
        open_val = 0.0
        if e["status"] == "open" and t and e["cik"] in units:
            open_val = units[e["cik"]] * (px_on(t, d1) or 0)
        ret = e["return_pct"]
        if ret is None and e["invested"]:
            ret = (e["proceeds"] + open_val) / e["invested"] - 1
        b_a, b_b = px_on("SPY", e["entry_date"]), px_on("SPY", end)
        bret = (b_b / b_a - 1) if (b_a and b_b) else None
        # path stats + counterfactual exits
        cf = {}
        under20 = None
        if w:
            p0 = w[0][1]
            peak = p0
            for d, v in w:
                if under20 is None and v <= p0 * 0.8:
                    under20 = d
                peak = max(peak, v)
            for name, test in (("stop_-25%", lambda v, p0, pk: v <= p0 * 0.75),
                               ("trail_-30%", lambda v, p0, pk: v <= pk * 0.70),
                               ("stop_-35%", lambda v, p0, pk: v <= p0 * 0.65)):
                pk = p0
                hit = None
                for d, v in w:
                    pk = max(pk, v)
                    if test(v, p0, pk):
                        hit = (d, v / p0 - 1)
                        break
                cf[name] = hit
        fwd = None
        if e["status"] == "closed" and t:
            from datetime import timedelta
            d_after = (date.fromisoformat(e["exit_date"]) + timedelta(days=182)).isoformat()
            if d_after <= (last_px_date or ""):
                a1, a2 = px_on(t, e["exit_date"]), px_on(t, d_after)
                s1, s2 = px_on("SPY", e["exit_date"]), px_on("SPY", d_after)
                if a1 and a2 and s1 and s2:
                    fwd = {"stock_6m_after_exit": a2 / a1 - 1, "sp500_6m_after_exit": s2 / s1 - 1}
        rows.append({"ticker": t, "name": (names.get(e["cik"]) or "")[:40], "entry": e["entry_date"],
                     "exit": e["exit_date"], "status": e["status"], "return": ret, "sp500_same_period": bret,
                     "days": (date.fromisoformat(end) - date.fromisoformat(e["entry_date"])).days,
                     "max_dd": e["max_dd_pct"], "max_gain": e["max_gain_pct"], "entry_rank": e["entry_rank"],
                     "exit_reason": (e["exit_reason"] or "")[:160], "invested": e["invested"],
                     "pnl": (e["proceeds"] + open_val - e["invested"]) if e["invested"] else None,
                     "first_down_20pct": under20, "counterfactual_exits": cf, "after_exit": fwd})
    sig_counts = {s["type"]: s["n"] for s in q("SELECT type, COUNT(*) n FROM signals WHERE run_id=? GROUP BY type",
                                              (r["id"],))}
    stints = q("SELECT return_pct, bench_return_pct, fwd_1y, entry_rank, status FROM stints WHERE run_id=?", (r["id"],))
    out["runs"].append({
        "id": r["id"], "kind": r["kind"], "name": r["name"], "profile": r["pname"], "settings": r["settings_json"],
        "start": d0, "end": d1, "first_holding_date": first_hold,
        "cagr_model": (nav1 / nav0) ** (1 / yrs(d0, d1)) - 1 if nav0 else None,
        "cagr_sp500": (b1 / b0) ** (1 / yrs(d0, d1)) - 1 if (b0 and b1) else None,
        "max_dd": mdd, "max_dd_date": mdd_at, "days": len(eq), "days_with_zero_holdings": zero_days,
        "avg_holdings": sum(hold) / len(hold), "max_holdings": max(hold), "yearly": yearly,
        "signals": sig_counts, "episodes": rows,
        "stints": {"n": len(stints),
                   "avg_return": sum(s["return_pct"] or 0 for s in stints) / max(len(stints), 1),
                   "avg_bench": sum(s["bench_return_pct"] or 0 for s in stints) / max(len(stints), 1),
                   "avg_fwd_1y": (lambda xs: sum(xs) / len(xs) if xs else None)(
                       [s["fwd_1y"] for s in stints if s["fwd_1y"] is not None])},
    })

# Big index weights: why were they (not) bought?
MEGA = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "BRK-B", "LLY", "JPM", "V", "MA", "COST",
        "NFLX", "WMT", "ORCL", "UNH", "HD", "KO", "AXP"]
default_pid = con.execute("SELECT id FROM profiles WHERE name='Oracle Classic'").fetchone()
default_pid = default_pid[0] if default_pid else None
bt_ids = [x["id"] for x in out["runs"] if x["kind"] == "backtest"]
for t in MEGA:
    s = con.execute("SELECT cik, category FROM securities WHERE ticker=?", (t,)).fetchone()
    if not s:
        out["megacaps"].append({"ticker": t, "missing": True})
        continue
    cik = s[0]
    snaps = q("SELECT filed, quality, gates_passed, failed_gates FROM snapshots WHERE cik=? AND profile_id=? "
              "AND filed>='2018-01-01' ORDER BY filed", (cik, default_pid))
    lists = {}
    for rid in bt_ids:
        lists[rid] = {x["list"]: x["n"] for x in q(
            "SELECT list, COUNT(*) n FROM evaluations WHERE run_id=? AND cik=? GROUP BY list", (rid, cik))}
    ds, vs = series(t)
    i0 = bisect_right(ds, "2019-01-02") - 1
    total = (vs[-1] / vs[i0] - 1) if (vs and i0 >= 0) else None
    out["megacaps"].append({
        "ticker": t, "category": s[1], "return_2019_to_now": total,
        "snapshots": [{"filed": x["filed"], "quality": round(x["quality"], 1) if x["quality"] is not None else None,
                       "gates_passed": x["gates_passed"],
                       "failed": [f.get("name") + " " + str(round(f["value"], 3) if isinstance(f.get("value"), (int, float)) else f.get("value"))
                                  for f in json.loads(x["failed_gates"] or "[]")]} for x in snaps[-8:]],
        "days_by_list_per_backtest": lists})

OUT.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
print(f"Done. Wrote {OUT}")
