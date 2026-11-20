"""Risk controls (sizing, caps, sector / correlation limits, market overlay, costs), enhanced valuation,
deterioration warnings and the robustness score."""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

import pytest

from oracle_edge import db, metrics, pipeline, quarterly, rules, sectors
from oracle_edge.market import overlay_factor, overlay_state
from tests.test_engine import DAYS, FakeBook, _ev, _step
from tests.test_portfolio import _focused


@pytest.fixture(scope="module", autouse=True)
def _db():
    db.get()
    rules.seed_profiles()


# ------------------------------------------------------------------ sizing and caps
def test_value_quality_gives_more_to_better_business_at_better_price():
    sim = _focused(401, n=4, sizing="value_quality")
    sim.mos_gate = 0.25
    great_cheap = _ev(1, "AAA", q=95, mos=0.45)
    ok_fair = _ev(2, "BBB", q=72, mos=0.26)
    assert sim.value_quality_factor(great_cheap) > 1.4 > sim.value_quality_factor(ok_fair)
    assert sim._target_w(great_cheap) > sim._target_w(ok_fair)


def test_max_position_caps_buys_and_trims_back():
    up = {d: (100.0 if d < DAYS[6] else 300.0) for d in DAYS}
    sim = _focused(402, n=2, prices={"AAA": up}, max_position=0.35, max_buy_weight=0.9)
    sim.sizing = "equal"
    evs = [_ev(1, "AAA", rank=1), _ev(2, "BBB", rank=2)]
    for d in DAYS[:3]:
        _step(sim, d, evs)
    assert sim._cap() == 0.35
    buys = db.rows("SELECT units FROM signals WHERE run_id=? AND type IN ('BUY','STRONG_BUY')", (402,))
    assert buys and all(b["units"] <= 0.35 + 1e-9 for b in buys)  # never more than 35% per holding
    trimmed = False
    for d in DAYS[3:10]:
        r = _step(sim, d, evs)
        trimmed |= any(s["type"] == "TRIM" and s["cik"] == 1 for s in r.signals)
    assert trimmed  # AAA tripled past 35% + 5 points of tolerance


def test_sector_limit_blocks_new_money_into_a_full_sector(monkeypatch):
    sim = _focused(403, n=3, max_sector=0.40, max_buy_weight=0.9)
    sim.sizing = "equal"
    sim._sectors = {1: "Information Technology", 2: "Information Technology", 3: "Health Care"}
    evs = [_ev(1, "AAA", rank=1), _ev(2, "BBB", rank=2), _ev(3, "CCC", rank=3)]
    for d in DAYS[:4]:
        _step(sim, d, evs)
    vals = {c: p["units"] * 100.0 for c, p in sim.positions.items()}
    tech = vals.get(1, 0) + vals.get(2, 0)
    assert tech <= 0.40 * sim.total_on(DAYS[3]) + 1e-6


def _correlated_book(days):
    rnd = random.Random(7)
    a = b = c = 100.0
    out = {"AAA": [], "BBB": [], "CCC": [], "SPY": []}
    for d in days:
        m = rnd.gauss(0, 0.01)
        a *= 1 + m + rnd.gauss(0, 0.002)
        b *= 1 + m + rnd.gauss(0, 0.002)  # moves with AAA
        c *= 1 + rnd.gauss(0, 0.01)       # independent
        out["AAA"].append((d, a))
        out["BBB"].append((d, b))
        out["CCC"].append((d, c))
        out["SPY"].append((d, 100.0))
    return FakeBook(out)


def test_correlation_limit_counts_closely_correlated_holdings_together():
    days = [(date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(400)]
    sim = _focused(404, n=3, max_correlated=0.40, corr_threshold=0.7, max_buy_weight=0.9)
    sim.pb = _correlated_book(days)
    assert sim.corr(1, 2, days[-1]) > 0.9 and abs(sim.corr(1, 3, days[-1])) < 0.3
    held = {1: 30.0}
    assert sim.limit_room(2, days[-1], 100.0, held) == pytest.approx(10.0)  # 40% group limit, 30 already in AAA
    assert sim.limit_room(3, days[-1], 100.0, held) == float("inf")         # unrelated stock: no limit


# ------------------------------------------------------------------ market overlay
def test_overlay_states_and_new_money_only():
    days = [(date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(260)]
    rising = FakeBook({"SPY": [(d, 100 + i * 0.1) for i, d in enumerate(days)]})
    falling = FakeBook({"SPY": [(d, 200 - i * 0.3) for i, d in enumerate(days)]})

    class M:
        def __init__(self, v):
            self.v = v

        def asof(self, s, d):
            return d, self.v

    st = {"overlay_ma_days": 200, "overlay_vix_stress": 30}
    assert overlay_state(rising, M(15), days[-1], st) == "normal"
    assert overlay_state(falling, M(20), days[-1], st) == "caution"
    assert overlay_state(falling, M(40), days[-1], st) == "stress"
    assert overlay_factor("stress", {}) < overlay_factor("caution", {}) < overlay_factor("normal", {}) == 1.0


def test_overlay_holds_back_new_purchases():
    days = [(date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(230)]
    sim = _focused(405, n=2, market_overlay=True, overlay_caution_buy=0.25, max_buy_weight=0.9)
    sim.sizing = "equal"
    sim.pb = FakeBook({"SPY": [(d, 200 - i * 0.3) for i, d in enumerate(days)],
                       "AAA": [(d, 100.0) for d in days], "BBB": [(d, 100.0) for d in days]})
    sim.policy = "spread"
    evs = [_ev(1, "AAA", rank=1), _ev(2, "BBB", rank=2)]
    for d in days[-4:-2]:
        r = _step(sim, d, evs)
    spent = sum(s["units"] for s in r.signals if s["type"] in ("BUY", "STRONG_BUY"))
    assert sim.overlay == "caution"
    assert spent == pytest.approx(0.25, rel=1e-6)  # a quarter of the cash, the rest waits


# ------------------------------------------------------------------ costs and turnover
def test_trading_costs_reduce_units_and_count_turnover():
    sim = _focused(406, n=1, trade_cost_bps=100, max_buy_weight=1.0)
    sim.sizing = "equal"
    for d in DAYS[:3]:
        _step(sim, d, [_ev(1, "AAA", rank=1)])
    assert sim.positions[1]["units"] == pytest.approx(0.99 / 100.0)  # 1% cost on a $1 buy at $100
    assert sim.traded == pytest.approx(1.0) and sim.costs == pytest.approx(0.01)


# ------------------------------------------------------------------ robustness score
def test_robustness_rewards_consistency_and_smaller_drawdowns():
    steady = {"period_cagr": [0.18, 0.20, 0.19], "thirds_excess": [0.03, 0.04, 0.02], "max_dd": -0.20,
              "worst_12m": -0.10, "turnover": 0.4}
    lucky = {"period_cagr": [0.60, -0.05, 0.10], "thirds_excess": [0.45, -0.20, -0.05], "max_dd": -0.45,
             "worst_12m": -0.35, "turnover": 1.5}
    assert pipeline.robustness(steady)["score"] > pipeline.robustness(lucky)["score"]
    assert set(pipeline.robustness(steady)["parts"]) == set(pipeline.ROBUSTNESS_WEIGHTS)


# ------------------------------------------------------------------ sectors
def test_sector_mapping():
    assert sectors.sector_of_sic(7372) == "Information Technology"
    assert sectors.sector_of_sic(2834) == "Health Care"
    assert sectors.sector_of_sic(6022) == "Financials"
    assert sectors.sector_of_sic(6798) == "Real Estate"
    assert sectors.sector_of_sic(None) == "Unknown"


# ------------------------------------------------------------------ enhanced valuation
def _stmt(n=10, buyback_crush=False):
    years = [f"{2015 + i}-12-31" for i in range(n)]
    rev = [1000 * 1.08 ** i for i in range(n)]
    s = {k: [None] * n for k in ("revenue", "net_income", "operating_income", "pretax_income", "income_tax",
                                 "equity", "treasury_stock", "total_debt", "cash", "capex", "da", "ppe_net",
                                 "shares_diluted", "ocf", "buybacks", "sbc")}
    s["years"] = years
    for i in range(n):
        s["revenue"][i] = rev[i]
        s["operating_income"][i] = 0.25 * rev[i]
        s["pretax_income"][i] = 0.24 * rev[i]
        s["income_tax"][i] = 0.05 * rev[i]
        s["net_income"][i] = 0.19 * rev[i]
        s["equity"][i] = (800 - 70 * i) if buyback_crush else 800 + 60 * i
        s["treasury_stock"][i] = 70 * i if buyback_crush else 0
        s["total_debt"][i] = 200
        s["cash"][i] = 100
        s["capex"][i] = 0.08 * rev[i]
        s["da"][i] = 0.05 * rev[i]
        s["ppe_net"][i] = 0.5 * rev[i]
        s["shares_diluted"][i] = 100 * (0.99 ** i if buyback_crush else 1.0)
        s["ocf"][i] = 0.26 * rev[i]
        s["buybacks"][i] = 60 if buyback_crush else 0
        s["sbc"][i] = 0.01 * rev[i]
    return s


def test_incremental_roic_and_maintenance_capex():
    s = _stmt()
    nop, ic = metrics.nopat_and_capital(s)
    inc = metrics.incremental_roic(nop, ic, 3)
    assert inc is not None and 0 < inc <= 1.0
    maint = metrics.maintenance_capex(s)
    assert all(m is None or m <= c + 1e-9 for m, c in zip(maint, s["capex"]))
    assert maint[-1] < s["capex"][-1]  # part of capex funded growth


def test_enhanced_valuation_scenarios_and_dilution():
    from oracle_edge.valuation import value, value_enhanced

    st = rules.load_yaml_profile()["settings"]
    vi = {"oe_ps": 2.0, "oe_norm_ps": 2.0, "eps3": 1.9, "eps_norm": 1.9, "eps_last": 2.0, "eps_cagr": 0.08,
          "roe": 0.30, "roic_avg": 0.18, "incr_roic": 0.12, "payout": 0.3, "buyback_payout": 0.0, "share_cagr": 0.0,
          "eps_by_fy": {}, "predictability": 0.9}
    v = value_enhanced(30.0, vi, st, 0.045, 0.25, {})
    sc = v.extra["scenarios"]
    assert sc["bear"]["iv"] < sc["base"]["iv"] < sc["bull"]["iv"]
    diluted = value_enhanced(30.0, {**vi, "share_cagr": 0.03}, st, 0.045, 0.25, {})
    assert diluted.iv < v.iv  # dilution lowers per-share value
    # ROE flattered by buybacks: classic grows at ROE x retention, enhanced uses the lower incremental ROIC
    vi2 = {**vi, "eps_cagr": None, "sust_g": 0.30 * 0.7}
    assert value_enhanced(30.0, vi2, st, 0.045, 0.25, {}).growth < value(30.0, vi2, st, 0.045, 0.25, {}).growth


def test_predictability_margin_of_safety():
    from oracle_edge.valuation import predictability_mos

    st = {"predictability_mos": True, "predictability_mos_add": 0.20}
    assert predictability_mos(0.25, {"predictability": 1.0}, st) == pytest.approx(0.25)
    assert predictability_mos(0.25, {"predictability": 0.5}, st) == pytest.approx(0.35)
    assert predictability_mos(0.25, {"predictability": 0.5}, {}) == 0.25


# ------------------------------------------------------------------ deterioration warnings
def _qstate():
    stt = quarterly._State()
    ends = ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30"]
    starts = ["2025-01-01", "2025-04-01", "2025-07-01", "2025-10-01", "2026-01-01", "2026-04-01", "2026-07-01"]
    for i, (s0, e) in enumerate(zip(starts, ends)):
        rev = 100.0 + i
        gm = 0.50 if i < 4 else 0.45  # margins slipped in the last three quarters
        stt.put_q("revenue", s0, e, rev)
        stt.put_q("gross_profit", s0, e, rev * gm)
    stt.put_a("revenue", "2025-12-31", 406.0)
    # receivables doubled while sales grew a few percent
    stt.put_q("receivables", "", "2025-09-30", 40.0)
    stt.put_q("receivables", "", "2026-09-30", 80.0)
    for e, s0 in (("2025-09-30", "2025-01-01"), ("2026-09-30", "2026-01-01")):
        stt.put_q("revenue", s0, e, 300.0 if e.startswith("2025") else 318.0)
    stt.put_a("revenue", "2024-12-31", 390.0)
    return stt


def test_gross_margin_and_receivable_warnings():
    stt = _qstate()
    flags = quarterly.deterioration(stt, "2026-09-30", rules.load_yaml_profile()["settings"])
    ids = {f["id"] for f in flags}
    assert "gm_decline" in ids
    assert all(not f["hard"] for f in flags)  # soft: REVIEW only, never a forced sale
