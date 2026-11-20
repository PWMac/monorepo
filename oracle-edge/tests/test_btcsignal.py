"""Bitcoin early warning: no look-ahead, zones, call log, market alerts, advice; quiet-day explanations."""

from __future__ import annotations

import math
from datetime import date, timedelta

from oracle_edge import btcsignal, db
from oracle_edge.market import Regime


def _seed(n=400, crash_at=None):
    con = db.get()
    con.execute("DELETE FROM prices WHERE ticker IN ('BTC-USD','QQQ')")
    d0 = date(2024, 1, 1)
    rows, k, day = [], 0, d0
    btc, qqq = 40000.0, 400.0
    while k < n:
        day += timedelta(days=1)
        btc *= 1 + 0.01 * math.sin(k / 7)  # BTC trades every day
        if crash_at is not None and crash_at <= k < crash_at + 20:
            btc *= 0.985
        rows.append(("BTC-USD", day.isoformat(), btc))
        if day.weekday() < 5:  # QQQ only on weekdays
            qqq *= 1.0005
            rows.append(("QQQ", day.isoformat(), qqq))
            k += 1
    con.executemany("INSERT INTO prices(ticker,date,close,adj_close,source) VALUES(?,?,?,?,'test')",
                    [(t, d, v, v) for t, d, v in rows])
    btcsignal._CACHE.clear()


def _settings(**kw):
    s = dict(btcsignal.DEFAULTS, min_obs=60)
    s.update(kw)
    return s


def test_cutoffs_never_see_the_future():
    _seed()
    s = _settings()
    rows = btcsignal.history(s)
    assert rows and rows[0]["zone"] == "warming"
    rets = [r["btc_ret"] for r in rows]
    i = 200
    past = sorted(rets[:i])  # only earlier days
    assert abs(rows[i]["bear_cut"] - btcsignal._pctile(past, 20)) < 1e-12
    assert rows[-1]["qqq_fwd"] is None and rows[-25]["qqq_fwd"] is not None  # last 20 days not known yet


def test_a_sharp_drop_is_a_warning():
    _seed(crash_at=380)
    cur = btcsignal.current(_settings())
    assert cur["zone"] == "bear" and cur["btc_ret"] < cur["bear_cut"]
    reg = Regime(1.7, "Significantly overvalued", 0.3, "2024-01-01", 0.045, 0.04)
    titles = [a["title"] for a in btcsignal.advice(reg, cur)]
    assert titles == ["Bitcoin early warning", "Expensive market", "Both readings are flashing"]


def test_call_log_and_resolution():
    _seed()
    db.get().execute("DELETE FROM btc_calls")
    db.set_cfg("btc_signal", {"min_obs": 60})
    out = btcsignal.record_and_resolve()
    assert out["logged"] == 1
    assert btcsignal.record_and_resolve()["logged"] == 0  # once per day
    first = db.scalar("SELECT date FROM btc_calls")
    # pretend the call was made 25 trading days ago: it resolves
    qd = [r["date"] for r in db.rows("SELECT date FROM prices WHERE ticker='QQQ' ORDER BY date")]
    db.get().execute("UPDATE btc_calls SET date=? WHERE date=?", (qd[-26], first))
    assert btcsignal.record_and_resolve()["resolved"] == 1
    fr = btcsignal.forward_record()
    assert fr["resolved"] == 1
    db.set_cfg("btc_signal", {})


def test_market_alerts_fire_on_change_only():
    db.get().execute("DELETE FROM market_alerts")
    db.set_cfg("market_alert_state", None)
    reg = Regime(1.3, "Modestly overvalued", 0.25, "2024-01-01", 0.045, 0.04)
    normal = {"zone": "normal", "btc_ret": 0.02, "bear_cut": -0.09, "bull_cut": 0.1, "date": "2026-11-18"}
    assert btcsignal.check_alerts(reg, normal) == []  # first check just remembers
    assert btcsignal.check_alerts(reg, normal) == []
    bear = dict(normal, zone="bear", btc_ret=-0.24, date="2026-11-19")
    ev = btcsignal.check_alerts(reg, bear)
    assert len(ev) == 1 and ev[0]["kind"] == "btc" and "worst" in ev[0]["message"]
    hot = Regime(1.6, "Significantly overvalued", 0.3, "2024-01-01", 0.045, 0.04)
    ev = btcsignal.check_alerts(hot, bear)
    assert [e["kind"] for e in ev] == ["bi"]
    assert len(btcsignal.recent_alerts(10000)) == 2


def test_quiet_day_reasons():
    from oracle_edge.ui import data

    con = db.get()
    con.execute("INSERT OR REPLACE INTO securities(cik,ticker,name) VALUES(7001,'AAA','A'),(7002,'BBB','B')")
    con.execute("INSERT INTO runs(kind,name,status) VALUES('live','t','active')")
    rid = db.scalar("SELECT MAX(id) FROM runs")
    con.execute("INSERT INTO portfolios(run_id,name,kind) VALUES(?,'m','model')", (rid,))
    pid = db.scalar("SELECT MAX(id) FROM portfolios")
    con.execute("INSERT INTO badges(run_id,portfolio_id,date,cik,badge,reason) VALUES(?,?,?,?,?,?)",
                (rid, pid, "2026-11-19", 7001, "WATCH", "Buy Zone, but all 7 slots hold better or newer ideas"))
    al = [{"cik": 7001, "ticker": "AAA", "kind": "buy_below", "state": "reached", "date": "2026-11-19"},
          {"cik": 7002, "ticker": "BBB", "kind": "strong_buy", "state": "approaching", "date": "2026-11-18"},
          {"cik": 7001, "ticker": "AAA", "kind": "limit", "state": "reached", "date": "2026-11-17"}]
    out = data.quiet_day_reasons(rid, "2026-11-19", al)
    assert [o["ticker"] for o in out] == ["AAA", "BBB"]
    assert "all 7 slots" in out[0]["why"] and "approaching" in out[1]["why"]


def test_buffett_indicator_uses_nonfinancial_equities_and_quarter_end():
    from oracle_edge.market import Macro

    with db.tx() as con:
        con.execute("DELETE FROM macro WHERE series IN ('NCBEILQ027S','BOGZ1LM893064105Q','GDP')")
        con.executemany("INSERT INTO macro(series,date,value) VALUES(?,?,?)",
                        [("NCBEILQ027S", "2026-04-01", 70_000_000.0), ("BOGZ1LM893064105Q", "2026-04-01", 120_000_000.0),
                         ("GDP", "2026-04-01", 31_000.0)])
    m = Macro()
    r, q = m.buffett_indicator("2026-11-19")
    assert q == "2026-04-01" and abs(r - 70_000 / 31_000) < 1e-9
    seen = []
    r2, _ = m.buffett_indicator("2026-11-19", lambda d: seen.append(d) or 100.0)
    assert seen[0] == "2026-06-30"  # the level is as of the quarter's last day


def test_episodes_and_scorecard():
    rows = []
    zones = ["normal", "bear", "bear", "normal", "bull", "bull", "normal", "bear"]
    fwd = [0.01, -0.02, 0.03, 0.01, -0.01, 0.02, -0.01, None]
    for i, (z, f) in enumerate(zip(zones, fwd)):
        rows.append({"date": f"2026-01-{i + 1:02d}", "zone": z, "btc_ret": -0.2 if z == "bear" else 0.1,
                     "qqq_fwd": f, "qqq": 100})
    eps = btcsignal.episodes(rows)
    assert [(e["zone"], e["start"], e["end"], e["correct"]) for e in eps] == [
        ("bear", "2026-01-02", "2026-01-03", True), ("bull", "2026-01-05", "2026-01-06", False),
        ("bear", "2026-01-08", "2026-01-08", None)]
    sc = btcsignal.scorecard(rows)
    assert sc["bear"] == {"episodes": 2, "judged": 1, "correct": 1, "rate": 1.0, "pending": 1}
    assert sc["bull"]["rate"] == 0.0 and abs(sc["base_down"] - 3 / 7) < 1e-9
