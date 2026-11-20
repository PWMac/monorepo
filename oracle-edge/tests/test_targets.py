"""Price targets: technicals, the buy plan, alert state machine, scan, timing filter."""

from __future__ import annotations

import math

import pytest

from oracle_edge import db, rules, targets
from tests.test_engine import DAYS, FakeBook, _ev, _sim, _step

ST = {"alert_band_atr": 1.5, "alert_band_min": 0.02, "alert_band_max": 0.08, "stage_in_downtrend": True,
      "nosebleed_iv_mult": 2.0, "price_alerts": True}


@pytest.fixture(scope="module", autouse=True)
def _db():
    db.get()
    rules.seed_profiles()


def _bars(closes, spread=0.01):
    return [(f"d{i:04d}", c * (1 + spread), c * (1 - spread), c) for i, c in enumerate(closes)]


def test_technicals_uptrend_and_fibs():
    closes = [50 + 0.2 * i + 2 * math.sin(i / 7) for i in range(300)]
    t = targets.technicals_from(_bars(closes))
    assert t["trend"] == "up" and t["swing"] == "up" and not t["falling"]
    assert t["lo52"] < t["fibs"][0.618] < t["fibs"][0.5] < t["fibs"][0.382] < t["hi52"]
    assert t["fibs"][0.5] == pytest.approx((t["hi52"] + t["lo52"]) / 2)
    assert 0 < t["atr_pct"] < 0.1


def test_technicals_downtrend():
    closes = [150 - 0.3 * i for i in range(300)]
    t = targets.technicals_from(_bars(closes))
    assert t["trend"] == "down" and t["falling"] and t["swing"] == "down"


def test_plan_above_zone_uses_first_support_inside_zone():
    t = {"trend": "up", "atr": 2.0, "sma50": 118.0, "sma200": 95.0, "fibs": {0.382: 110.0, 0.5: 104.0, 0.618: 98.0},
         "lo52": 80.0}
    p = targets.plan(120.0, 100.0, 80.0, 133.0, t, ST)
    assert p["status"] == "above_zone"
    assert p["limit"] == 98.0 and "61.8" in p["limit_label"]  # highest support at or under $100 buy-below
    assert len(p["tranches"]) == 1 and p["trim_above"] == pytest.approx(266.0)


def test_plan_above_zone_without_support_uses_buy_below():
    t = {"trend": "sideways", "atr": 2.0, "sma50": 130.0, "sma200": 125.0, "fibs": {}, "lo52": 60.0}
    p = targets.plan(140.0, 100.0, 80.0, 133.0, t, ST)
    assert p["limit"] == 100.0 and p["limit_label"] == "buy-below price"  # 52-week low is below strong buy


def test_plan_in_zone_nearby_support():
    t = {"trend": "up", "atr": 3.0, "sma50": 93.0, "sma200": 80.0, "fibs": {}, "lo52": 70.0}
    p = targets.plan(95.0, 100.0, 80.0, 133.0, t, ST)
    assert p["status"] == "in_zone" and p["limit"] == 93.0  # within one ATR under today's price


def test_plan_in_zone_downtrend_stages_in_thirds():
    t = {"trend": "down", "atr": 2.0, "sma50": 99.0, "sma200": 110.0, "fibs": {0.382: 91.0}, "lo52": 85.0}
    p = targets.plan(98.0, 100.0, 80.0, 133.0, t, ST)
    prices = [x[0] for x in p["tranches"]]
    assert len(prices) == 3 and prices == sorted(prices, reverse=True)
    assert prices[0] == 98.0 and prices[1] == 91.0 and prices[2] == 80.0  # market, next support, strong buy
    assert sum(x[1] for x in p["tranches"]) == pytest.approx(1.0)


def test_band_is_volatility_scaled_and_clamped():
    assert targets.band(100, 1.0, ST) == pytest.approx(0.02)  # 1.5% raw -> 2% floor
    assert targets.band(100, 3.0, ST) == pytest.approx(0.045)
    assert targets.band(100, 10.0, ST) == pytest.approx(0.08)  # cap


def test_state_machine_hysteresis():
    b = 0.03
    seq = [110, 102.5, 102.0, 104.0, 99.5, 101.0, 104.5, 107.0, 102.0]
    state, events = "idle", []
    for px in seq:
        zone, _ = targets.classify("down", 100.0, px, px, px, b)
        state, emit = targets.step(state, zone)
        if emit:
            events.append((px, state))
    # approach once, reach once, stay quiet in the hysteresis band, re-arm only past 2x band (>6%), approach again
    assert events == [(102.5, "approaching"), (99.5, "reached"), (102.0, "approaching")]


def test_touch_intraday_counts_as_reached():
    zone, _ = targets.classify("down", 100.0, 101.0, 99.8, 102.0, 0.03)
    assert zone == "reached"
    zone, _ = targets.classify("up", 200.0, 199.0, 197.0, 200.5, 0.03)
    assert zone == "reached"


def _prices(ticker, closes):
    con = db.get()
    con.execute("DELETE FROM prices WHERE ticker=?", (ticker,))
    con.executemany("INSERT INTO prices(ticker,date,open,high,low,close,adj_close,volume,source) VALUES(?,?,?,?,?,?,?,?,?)",
                    [(ticker, f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", c, c * 1.01, c * 0.99, c, c, 1e6, "t")
                     for i, c in enumerate(closes)])


def test_scan_emits_once_and_custom_alerts():
    con = db.get()
    con.execute("INSERT OR REPLACE INTO securities(cik,ticker,name) VALUES(9001,'TGT','Target Test Co')")
    con.execute("INSERT INTO runs(id,kind,name,profile_id,start_date) VALUES(901,'live','t',1,'2026-01-01')")
    con.execute("INSERT INTO portfolios(run_id,name,kind,last_nav) VALUES(901,'m','model',1.0)")
    closes = [130.0] * 200 + [130 - i * 0.5 for i in range(56)]  # slides to 102.5
    _prices("TGT", closes)
    last = f"2026-{1 + 255 // 28:02d}-{1 + 255 % 28:02d}"
    con.execute("INSERT INTO evaluations(run_id,date,cik,list,price,buy_below,strong_buy_below,iv,detail) "
                "VALUES(901,?,9001,'pricey',102.5,100,80,133,'{}')", (last,))
    con.execute("INSERT INTO custom_targets(cik,price,direction,note,created_at) VALUES(9001,125,'above','',?)",
                (db.now(),))
    ev1 = targets.scan(901, ST)
    kinds = {(e["kind"], e["state"]) for e in ev1}
    assert ("buy_below", "approaching") in kinds
    assert not any(k.startswith("custom") for k, _ in kinds)  # 125 above is far away
    assert targets.scan(901, ST) == []  # same day again: nothing new
    assert db.scalar("SELECT COUNT(*) FROM price_alerts WHERE cik=9001") == len(ev1)


def test_timing_filter_waits_in_downtrend():
    sim, _ = _sim(950, {})
    sim.st = dict(sim.st, timing_filter=True, timing_max_wait=3)
    sim.pb.downtrend = lambda t, d: True
    badges = []
    for d in DAYS[:5]:
        r = _step(sim, d, [_ev(1, "AAA")])
        badges.append(r.badges[1])
    # day 1 confirming; days 2-4 waiting for the slide to stop (3 = max wait); day 5 buys anyway
    assert [b[0] for b in badges] == ["WATCH"] * 4 + ["BUY"]
    assert "falling 50-day" in badges[1][1] and "(3/3)" in badges[3][1]


def test_pricebook_downtrend():
    pts = [(f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}", 200 - i) for i in range(100)]
    pb = FakeBook({"X": pts})
    assert pb.downtrend("X", pts[-1][0]) is True
    up = [(d, 100 + i) for i, (d, _) in enumerate(pts)]
    assert FakeBook({"Y": up}).downtrend("Y", up[-1][0]) is False


def test_profiles_have_timing_comparison():
    assert db.scalar("SELECT COUNT(*) FROM profiles WHERE name=?", (rules.TIMING_NAME,)) == 1
    pid = db.scalar("SELECT id FROM profiles WHERE name=?", (rules.TIMING_NAME,))
    assert rules.get_profile(pid).settings["timing_filter"] is True
    assert rules.default_profile().settings["timing_filter"] is False
