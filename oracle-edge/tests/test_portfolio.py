"""Focused portfolios: fixed number of holdings, cash, swaps, add limits, top-ups; sweep scoring."""

from __future__ import annotations

import pytest

from oracle_edge import db, pipeline, rules
from oracle_edge.signals import PortfolioSim
from tests.test_engine import DAYS, FakeBook, _ev, _step


@pytest.fixture(scope="module", autouse=True)
def _db():
    db.get()
    rules.seed_profiles()


def _focused(run_id, n=2, prices=None, sizing="equal", **st_over):
    profile = rules.default_profile()
    db.get().execute("INSERT INTO runs(id,kind,name,profile_id,start_date) VALUES(?,?,?,?,?)",
                     (run_id, "backtest", "t", profile.id, DAYS[0]))
    db.get().execute("INSERT INTO portfolios(run_id,name,kind,max_positions,position_cap,trim_to,sizing,cash,"
                     "last_value,last_nav,mode,contribution) VALUES(?,?,?,?,?,?,?,1.0,1.0,1.0,'focused',0)",
                     (run_id, "m", "model", n, 0.40, 0.30, sizing))
    pid = db.scalar("SELECT id FROM portfolios WHERE run_id=?", (run_id,))
    tk = {1: "AAA", 2: "BBB", 3: "CCC", 4: "DDD"}
    prices = prices or {}
    series = {t: [(d, (prices.get(t) or {}).get(d, (prices.get(t) or {}).get("*", 100.0))) for d in DAYS]
              for t in ["AAA", "BBB", "CCC", "DDD", "SPY"]}
    pb = FakeBook(series)
    sim = PortfolioSim(run_id, pid, profile, pb, tk)
    sim.st = dict(sim.st, **st_over)
    return sim


def test_focused_buys_only_best_n_with_cash():
    sim = _focused(301, n=2)
    evs = [_ev(1, "AAA", rank=1, rs=90), _ev(2, "BBB", rank=2, rs=85), _ev(3, "CCC", rank=3, rs=80)]
    _step(sim, DAYS[0], evs)                      # confirming
    r = _step(sim, DAYS[1], evs)                  # buy the best two
    assert {s["cik"] for s in r.signals if s["type"] in ("BUY", "STRONG_BUY")} == {1, 2}
    assert r.badges[3][0] == "WATCH" and "slot" in r.badges[3][1]
    _step(sim, DAYS[2], evs)                      # fills at this open
    assert set(sim.positions) == {1, 2}
    assert sim.cash == pytest.approx(0.0, abs=1e-6)
    assert sim.total_on(DAYS[2]) == pytest.approx(1.0, rel=1e-6)


def test_focused_nav_tracks_holdings():
    up = {d: (110.0 if d >= DAYS[5] else 100.0) for d in DAYS}
    sim = _focused(302, n=2, prices={"AAA": up, "BBB": up})
    evs = [_ev(1, "AAA", rank=1), _ev(2, "BBB", rank=2)]
    for d in DAYS[:6]:
        _step(sim, d, evs)
    assert sim.last_nav == pytest.approx(1.10, rel=1e-3)  # fully invested, both up 10%


def test_swap_needs_min_hold_and_big_gap():
    sim = _focused(303, n=1, swap_min_hold_days=0)
    a = [_ev(1, "AAA", rank=1, rs=70, er=0.11)]
    for d in DAYS[:3]:
        _step(sim, d, a)
    assert set(sim.positions) == {1}
    better = [_ev(1, "AAA", rank=2, rs=70, er=0.11), _ev(4, "DDD", rank=1, rs=92, er=0.20)]
    _step(sim, DAYS[3], better)
    r = _step(sim, DAYS[4], better)
    types = {(s["cik"], s["type"]) for s in r.signals}
    assert (1, "SWAP") in types and any(c == 4 and t in ("BUY", "STRONG_BUY") for c, t in types)
    # with a 1-year minimum hold, the same idea only gets a WATCH
    sim2 = _focused(304, n=1, swap_min_hold_days=365)
    for d in DAYS[:3]:
        _step(sim2, d, a)
    _step(sim2, DAYS[3], better)
    r2 = _step(sim2, DAYS[4], better)
    assert not any(s["type"] == "SWAP" for s in r2.signals) and r2.badges[4][0] == "WATCH"


def test_add_limit_stops_averaging_down_variable_mode():
    from tests.test_engine import _sim

    sim, _ = _sim(305, {})
    falling = {d: 100.0 * (0.88 ** i) for i, d in enumerate(DAYS)}
    sim.pb = FakeBook({t: [(d, falling[d] if t == "AAA" else 100.0) for d in DAYS] for t in ["AAA", "SPY"]})
    sim.st = dict(sim.st, add_cooldown_days=0, max_adds=2)
    adds = 0
    for d in DAYS[:16]:
        r = _step(sim, d, [_ev(1, "AAA", rank=1)])
        adds += sum(1 for s in r.signals if s["type"] == "ADD")
    assert adds == 2


def test_new_money_never_pushes_a_holding_past_the_cap():
    falling = {d: 100.0 * (0.9 ** i) for i, d in enumerate(DAYS)}
    sim = _focused(308, n=4, prices={"AAA": falling}, max_buy_weight=0.25)
    sim.weekly = 0.5  # lots of new money every week
    for d in DAYS[:20]:
        _step(sim, d, [_ev(1, "AAA", rank=1, er=0.30)])
    w = sim.value_on(DAYS[19]) / sim.total_on(DAYS[19])
    assert w <= 0.26  # a falling stock is never averaged into beyond its cap; the rest waits in cash


def test_no_add_while_review_open():
    falling = {d: 100.0 * (0.85 ** i) for i, d in enumerate(DAYS)}
    sim = _focused(306, n=2, prices={"AAA": falling}, add_cooldown_days=0)
    for d in DAYS[:3]:
        _step(sim, d, [_ev(1, "AAA", rank=1, q=85)])
    r = _step(sim, DAYS[3], [_ev(1, "AAA", rank=1, q=72)])  # quality fell 13 pts -> hard REVIEW
    assert r.badges[1][0] == "REVIEW"
    assert not any(s["type"] == "ADD" for s in r.signals)


def test_spare_cash_tops_up_best_holding():
    sim = _focused(307, n=3)
    evs = [_ev(1, "AAA", rank=1, rs=90), _ev(2, "BBB", rank=2, rs=85)]
    for d in DAYS[:3]:
        _step(sim, d, evs)
    assert set(sim.positions) == {1, 2} and sim.cash == pytest.approx(1 / 3, rel=1e-3)
    # one slot is empty and nothing new qualifies: the spare third waits in cash (no top-up past each slice)
    r = _step(sim, DAYS[3], evs)
    assert not any(s["type"] == "ADD" for s in r.signals)
    # sell BBB (thesis broken): its cash goes back to AAA only up to AAA's slice; the rest waits for a new idea
    broken = [_ev(1, "AAA", rank=1, rs=90), _ev(2, "BBB", rank=2, gates=False, failed=[{"id": "r19", "name": "ROE"}])]
    _step(sim, DAYS[4], broken)
    _step(sim, DAYS[5], [_ev(1, "AAA", rank=1, rs=90)])
    assert 2 not in sim.positions
    assert sim.cash == pytest.approx(2 / 3, rel=1e-2)


def test_recommend_prefers_reliable_size():
    base = {"mode": "focused", "sizing": "equal", "bench_cagr": 0.10, "max_dd": -0.30, "avg_holdings": 0, "avg_cash": 0,
            "label": ""}
    res = [dict(base, run_id=1, n=5, cagr=0.20, thirds_excess=[0.30, -0.10, -0.05]),   # one lucky stretch
           dict(base, run_id=2, n=10, cagr=0.14, thirds_excess=[0.04, 0.03, 0.05]),
           dict(base, run_id=3, n=15, cagr=0.13, thirds_excess=[0.03, 0.04, 0.02]),
           dict(base, run_id=4, n=20, cagr=0.11, thirds_excess=[0.01, 0.0, 0.02]),
           {"mode": "variable", "sizing": "equal", "max_dd": -0.30, "cagr": 0.1, "run_id": 9, "thirds_excess": []}]
    rec = pipeline.recommend(res)
    assert rec["n"] in (10, 15)


def test_portfolio_only_change_keeps_scores():
    p = rules.default_profile()
    body = dict(p.body, settings=dict(p.settings, max_holdings=7, sizing="conviction"))
    assert rules.scoring_hash(body) == rules.scoring_hash(p.body)
    body2 = dict(p.body, settings=dict(p.settings, lookback_years=5))
    assert rules.scoring_hash(body2) != rules.scoring_hash(p.body)


def test_copied_profile_finds_the_same_candidates():
    import copy

    base = rules.default_profile()
    db.get().execute("INSERT INTO snapshots(cik,profile_id,profile_version,filed,quality,gates_passed) "
                     "VALUES(777001,?,?, '2025-01-01', 80, 1)", base.snap)
    body = copy.deepcopy(base.body)
    body["settings"]["hurdle"] = 0.20          # not a scoring setting
    pid = rules.save_as(base.id, "copy-hurdle-20", body)
    assert rules.get_profile(pid).snap == base.snap
    assert 777001 in pipeline.candidate_ciks([pid])


def _two_holdings(run_id, policy, **st):
    sim = _focused(run_id, n=3, **st)
    sim.policy = policy
    evs = [_ev(1, "AAA", rank=2, rs=88, er=0.12, mos=0.30), _ev(2, "BBB", rank=1, rs=90, er=0.18, mos=0.45)]
    for d in DAYS[:3]:
        _step(sim, d, evs)
    return sim, evs


def test_best_value_sends_new_money_to_highest_expected_return():
    sim, evs = _two_holdings(320, "best_value", max_buy_weight=0.6)
    sim.cash += 0.2
    r = _step(sim, DAYS[3], evs)
    adds = [s for s in r.signals if s["type"] == "ADD"]
    assert adds and adds[0]["cik"] == 2  # BBB: 18% expected return beats AAA's 12%


def test_best_rank_and_spread():
    sim, evs = _two_holdings(321, "best_rank", max_buy_weight=0.6)
    sim.cash += 0.2
    r = _step(sim, DAYS[3], [_ev(1, "AAA", rank=1, rs=95, er=0.12), _ev(2, "BBB", rank=2, rs=80, er=0.18)])
    assert [s["cik"] for s in r.signals if s["type"] == "ADD"][0] == 1
    sim2, evs2 = _two_holdings(322, "spread", max_buy_weight=0.6)
    sim2.cash += 0.2
    r2 = _step(sim2, DAYS[3], evs2)
    amts = sorted(s["units"] for s in r2.signals if s["type"] == "ADD")
    assert len(amts) == 2 and amts[0] == pytest.approx(amts[1], rel=1e-6)


def test_patient_waits_for_strong_buy_prices():
    sim = _focused(323, n=3, patient_max_weeks=26)
    sim.policy = "patient"
    ok = [_ev(1, "AAA", rank=1, mos=0.30)]          # in the Buy Zone, but not a strong-buy price
    for d in DAYS[:4]:
        _step(sim, d, ok)
    assert not sim.positions and sim.cash == pytest.approx(1.0)
    cheap = [_ev(1, "AAA", rank=1, mos=0.45)]       # 25% gate + 15% extra reached
    for d in DAYS[4:7]:
        _step(sim, d, cheap)
    assert 1 in sim.positions


def test_weekly_money_and_sp500_comparison():
    sim = _focused(324, n=2)
    sim.weekly = 0.1
    days = [f"2026-02-{x:02d}" for x in range(2, 28)]
    sim.pb = FakeBook({t: [(d, 100.0) for d in days] for t in ["AAA", "BBB", "SPY"]})
    for d in days:
        _step(sim, d, [_ev(1, "AAA", rank=1)])
    weeks = len({__import__("datetime").date.fromisoformat(d).isocalendar()[1] for d in days}) - 1
    assert sim.contributed == pytest.approx(1.0 + 0.1 * weeks)
    assert sim.bench_units * 100.0 == pytest.approx(sim.contributed)  # flat S&P: worth exactly what went in


def test_editor_rounding_noise_is_not_a_rule_change():
    import copy

    p = rules.default_profile()
    noisy, clean = copy.deepcopy(p.body), copy.deepcopy(p.body)
    noisy["rules"][0]["target"] = 7.000000000000001 / 100   # what a 7 -> /100 round trip can produce
    clean["rules"][0]["target"] = 0.07
    assert rules.scoring_hash(noisy) == rules.scoring_hash(clean)
    noisy["rules"][0]["floor"] = float(clean["rules"][0]["floor"])   # 3 vs 3.0 is not a change either
    assert rules.scoring_hash(noisy) == rules.scoring_hash(clean)
    clean["rules"][0]["target"] = 0.08                      # a real change still counts
    assert rules.scoring_hash(noisy) != rules.scoring_hash(clean)


def test_reserve_keeps_part_at_ordinary_price_and_spends_it_on_a_strong_buy():
    sim = _focused(330, n=1, reserve_min_deploy=0.5, reserve_max=0.6, reserve_max_weeks=26, max_buy_weight=1.0)
    sim.policy = "reserve"
    ok = [_ev(1, "AAA", rank=1, mos=0.25, q=85)]       # just inside the Buy Zone: strength 0
    _step(sim, DAYS[0], ok)
    r = _step(sim, DAYS[1], ok)
    buy = [s for s in r.signals if s["type"] in ("BUY", "STRONG_BUY")]
    assert buy and buy[0]["units"] == pytest.approx(0.5 * 1.0, rel=1e-3)  # half now
    _step(sim, DAYS[2], ok)
    assert sim.cash == pytest.approx(0.5, rel=1e-3)                       # half kept in reserve
    strong = [_ev(1, "AAA", rank=1, mos=0.45, q=85)]   # strong-buy price: reserve goes in
    r = _step(sim, DAYS[3], strong)
    adds = [s for s in r.signals if s["type"] == "ADD"]
    assert adds and adds[0]["units"] == pytest.approx(0.5, rel=1e-2)
    assert "reserve" in adds[0]["reason"]


def test_reserve_never_grows_past_its_cap():
    sim = _focused(331, n=1, reserve_min_deploy=0.0, reserve_max=0.10, max_buy_weight=0.9)
    sim.policy = "reserve"
    ok = [_ev(1, "AAA", rank=1, mos=0.25)]
    _step(sim, DAYS[0], ok)
    r = _step(sim, DAYS[1], ok)
    buy = [s for s in r.signals if s["type"] in ("BUY", "STRONG_BUY")]
    assert buy and buy[0]["units"] == pytest.approx(0.9, rel=1e-3)        # keeps only 10%


def test_full_slots_trim_weaker_holdings_to_fund_a_strong_buy():
    sim = _focused(332, n=2, swap_min_hold_days=365, fund_extra_slots=1, fund_trim_max=0.34)
    sim.fund = True
    held = [_ev(1, "AAA", rank=1, rs=80, er=0.11, mos=0.30), _ev(2, "BBB", rank=2, rs=78, er=0.12, mos=0.30)]
    for d in DAYS[:3]:
        _step(sim, d, held)
    assert set(sim.positions) == {1, 2}
    new = held + [_ev(3, "CCC", rank=3, rs=85, er=0.20, mos=0.45, q=88)]  # strong buy, clearly higher return
    _step(sim, DAYS[3], new)
    r = _step(sim, DAYS[4], new)
    trims = {s["cik"] for s in r.signals if s["type"] == "TRIM"}
    assert trims and trims <= {1, 2}
    assert any(s["cik"] == 3 and s["type"] == "STRONG_BUY" for s in r.signals)
    _step(sim, DAYS[5], new)
    assert set(sim.positions) == {1, 2, 3}                                # one extra slot for the strong buy


def test_no_funding_when_holdings_are_as_good():
    sim = _focused(333, n=2, swap_min_hold_days=365)
    sim.fund = True
    held = [_ev(1, "AAA", rank=1, rs=80, er=0.19, mos=0.30), _ev(2, "BBB", rank=2, rs=78, er=0.18, mos=0.30)]
    for d in DAYS[:3]:
        _step(sim, d, held)
    new = held + [_ev(3, "CCC", rank=3, rs=85, er=0.20, mos=0.45, q=88)]  # only 1-2 pts better: not worth selling
    _step(sim, DAYS[3], new)
    r = _step(sim, DAYS[4], new)
    assert not any(s["type"] in ("TRIM", "SWAP") for s in r.signals)
    assert r.badges[3][0] == "WATCH"


def test_policy_suffix_sets_fund_flag():
    assert pipeline.policy_label("reserve+fund").startswith("sliding reserve")
    assert "fund strong" in pipeline.policy_label("spread+fund")


def test_reserve_is_not_spent_at_ordinary_prices():
    sim = _focused(334, n=1, reserve_min_deploy=0.5, reserve_max=0.9, reserve_max_weeks=26, max_buy_weight=1.0)
    sim.policy = "reserve"
    ok = [_ev(1, "AAA", rank=1, mos=0.26, q=85)]
    for d in DAYS[:8]:
        _step(sim, d, ok)
    assert sim.reserve == pytest.approx(1 - (0.5 + 0.5 * 0.01 / 0.15), rel=1e-3)  # kept, waiting for a strong buy
    assert sim.cash >= sim.reserve - 1e-9


def test_stock_table_rows_carry_buffett_metrics():
    from oracle_edge.ui import data as d
    keys = {k for k, *_ in d.BUFFETT_METRICS}
    assert {"roe", "roic", "gm", "debt", "shares", "dollar", "pred"} <= keys
    assert d.stock_table(999999, "buy") == []  # unknown run: empty, no crash
