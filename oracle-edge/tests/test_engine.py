"""Core behaviour tests: parsing, point-in-time, scoring, valuation and the full signal ladder."""

from __future__ import annotations


import pytest

from oracle_edge import db, rules
from oracle_edge.engine import Eval, PriceBook
from oracle_edge.metrics import MetricResult
from oracle_edge.signals import PortfolioSim
from oracle_edge.sources import sec
from oracle_edge.valuation import value, value_score


@pytest.fixture(scope="module", autouse=True)
def _db():
    db.get()
    rules.seed_profiles()


# ------------------------------------------------------------------ parsing & point-in-time
def _doc():
    def e(end, val, filed, start=True):
        d = {"end": end, "val": val, "fy": int(filed[:4]), "fp": "FY", "form": "10-K", "filed": filed}
        if start:
            d["start"] = end[:4] + "-01-01"
        return d

    return {"facts": {"us-gaap": {
        # old tag for 2016-2017, new tag from 2018: must merge into one history
        "SalesRevenueNet": {"units": {"USD": [e("2016-12-31", 100, "2017-02-20"), e("2017-12-31", 110, "2018-02-20")]}},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            e("2018-12-31", 120, "2019-02-20"), e("2019-12-31", 130, "2020-02-20")]}},
        "NetIncomeLoss": {"units": {"USD": [
            e("2018-12-31", 20, "2019-02-20"),
            e("2018-12-31", 25, "2020-02-20"),  # restated a year later
            e("2019-12-31", 30, "2020-02-20"),
            {"start": "2019-07-01", "end": "2019-09-30", "val": 9, "fy": 2019, "fp": "Q3", "form": "10-Q",
             "filed": "2019-11-01"},  # quarterly: must be ignored
        ]}},
        "StockholdersEquity": {"units": {"USD": [e("2018-12-31", 200, "2019-02-20", False),
                                                 e("2019-12-31", 220, "2020-02-20", False),
                                                 e("2019-06-30", 999, "2019-08-01", False)]}},
    }}}


def test_parse_merges_tags_and_keeps_versions():
    p = sec.parse_companyfacts(_doc())
    rev = sorted((f[1], f[2]) for f in p["facts"] if f[0] == "revenue")
    assert rev == [("2016-12-31", 100), ("2017-12-31", 110), ("2018-12-31", 120), ("2019-12-31", 130)]
    ni = sorted((f[1], f[2], f[3]) for f in p["facts"] if f[0] == "net_income")
    assert ("2018-12-31", 20.0, "2019-02-20") in ni and ("2018-12-31", 25.0, "2020-02-20") in ni
    assert all(f[1] != "2019-09-30" for f in p["facts"])  # no quarterly
    eq_ends = {f[1] for f in p["facts"] if f[0] == "equity"}
    assert "2019-06-30" not in eq_ends  # instant not at a fiscal year end


def test_point_in_time_uses_value_known_then():
    from oracle_edge.metrics import build_statement

    facts = [("net_income", "2018-12-31", 20, "2019-02-20"), ("net_income", "2018-12-31", 25, "2020-02-20")]
    state = {}
    for item, fy, v, filed in sorted(facts, key=lambda x: x[3]):
        if filed <= "2019-12-31":
            state[(item, fy)] = v
    assert build_statement(state, 10)["net_income"] == [20]


def test_daily_index_parse():
    text = ("Form Type   Company Name                                                  CIK         Date Filed  File Name\n"
            "---------------------------------------------------------------------------------------------------------\n"
            "10-K        APPLE INC                                                     320193      20261031    edgar/data/320193/x.txt\n"
            "10-Q        SOMEBODY                                                      12345       20261031    edgar/data/12345/y.txt\n"
            "20-F        FOREIGN CO LTD                                                99999       20261031    edgar/data/99999/z.txt\n")
    got = sec.parse_daily_index(text)
    assert got == [("10-K", 320193, "2026-10-31"), ("10-Q", 12345, "2026-10-31"), ("20-F", 99999, "2026-10-31")]


# ------------------------------------------------------------------ scoring
def test_level_score_anchors():
    rule = {"direction": "higher", "floor": 0.2, "target": 0.4, "excellent": 0.6}
    assert rules.level_score(0.2, rule) == 0
    assert rules.level_score(0.4, rule) == 70
    assert rules.level_score(0.6, rule) == 100
    assert 0 < rules.level_score(0.39, rule) < 70  # partial credit, not a cliff
    low = {"direction": "lower", "floor": 8, "target": 4, "excellent": 0}
    assert rules.level_score(4, low) == 70 and rules.level_score(0, low) == 100 and rules.level_score(9, low) == 0
    rng = {"direction": "range", "floor": 0, "target": 0.15, "excellent": 0.30, "ceiling": 0.45}
    assert rules.level_score(0.21, rng) == 100 and rules.level_score(0.0, rng) == 0


def test_roe_gate_requires_min_year():
    rule = next(r for r in rules.load_yaml_profile()["rules"] if r["id"] == "r19")
    m = MetricResult(0.20, [0.25, 0.08, 0.27], ["a", "b", "c"])
    m.min_year = 0.08
    assert rules.score_rule(rule, m).passed is False


# ------------------------------------------------------------------ valuation
def _vi():
    return {"oe_ps": 5.0, "eps3": 5.0, "eps_last": 5.2, "eps_cagr": 0.08, "roe": 0.25, "payout": 0.3, "sust_g": 0.175,
            "shares": 1e8, "ebitda_g": 0.07, "eps_by_fy": {}, "fy_end": "2025-12-31"}


def test_valuation_cheaper_means_better():
    st = rules.load_yaml_profile()["settings"]
    a = value(60, _vi(), st, 0.045, 0.25, {})
    b = value(90, _vi(), st, 0.045, 0.25, {})
    assert a.er > b.er and a.mos > b.mos
    assert a.iv == b.iv and a.iv == min(a.iv_a, a.iv_b)
    assert a.buy_below <= a.iv * 0.75 + 1e-9
    assert value_score(0.10, {"target": 0.10}) == pytest.approx(70)


def test_discount_rate_is_treasury_plus_addon_when_low():
    from oracle_edge.valuation import discount_rate

    st = rules.load_yaml_profile()["settings"]
    assert discount_rate(0.045, st) == pytest.approx(0.065)
    assert discount_rate(0.055, st) == pytest.approx(0.055)


# ------------------------------------------------------------------ signal ladder
class FakeBook(PriceBook):
    def __init__(self, series: dict[str, list[tuple[str, float]]]):
        super().__init__()
        for t, pts in series.items():
            self.d[t] = ([d for d, _ in pts], [p for _, p in pts], [p for _, p in pts], [p for _, p in pts],
                         [1e7] * len(pts))


DAYS = [f"2026-01-{d:02d}" for d in range(5, 31)]


def _ev(cik, t, lst="buy", q=85.0, rank=1, rs=80.0, mos=0.35, er=0.14, price=100.0, iv=150.0, gates=True,
        failed=None, filed="2025-02-20", pe=15.0, pe_med=15.0):
    return Eval(cik=cik, ticker=t, list=lst, quality=q, value=75.0, rank_score=rs, confidence=1.0, price=price, iv=iv,
                mos=mos, er=er, buy_below=iv * 0.75, strong_buy_below=iv * 0.6, expensive_above=iv * 1.5, pe_now=pe,
                pe_median=pe_med, gates_passed=gates, failed=failed or [], near=None, filed=filed, rank=rank)


def _sim(run_id, prices, max_positions=None):
    profile = rules.default_profile()
    db.get().execute("INSERT INTO runs(id,kind,name,profile_id,start_date) VALUES(?,?,?,?,?)",
                     (run_id, "backtest", "t", profile.id, DAYS[0]))
    db.get().execute("INSERT INTO portfolios(run_id,name,kind,max_positions,position_cap,trim_to,last_nav)"
                     " VALUES(?,?,?,?,?,?,1.0)", (run_id, "m", "model", max_positions, 0.40, 0.30))
    pid = db.scalar("SELECT id FROM portfolios WHERE run_id=?", (run_id,))
    tk = {1: "AAA", 2: "BBB", 3: "CCC"}
    pb = FakeBook({t: [(d, prices.get(t, 100.0)) for d in DAYS] for t in ["AAA", "BBB", "CCC", "SPY"]})
    return PortfolioSim(run_id, pid, profile, pb, tk), pb


def _step(sim, d, evals, mos_gate=0.25):
    fi, fo = sim.fill_pending(d)
    sim.mark(d, fi, fo)
    res = sim.decide(d, evals, True, mos_gate)
    sim.stints(d, evals)
    sim.persist(d, res)
    return res


def test_buy_needs_two_closes_then_fills_next_open():
    sim, _ = _sim(101, {})
    r1 = _step(sim, DAYS[0], [_ev(1, "AAA")])
    assert r1.badges[1][0] == "WATCH" and not r1.signals
    r2 = _step(sim, DAYS[1], [_ev(1, "AAA")])
    assert r2.signals[0]["type"] == "BUY"  # MOS 35% < 25% gate + 15% -> plain BUY
    _step(sim, DAYS[2], [_ev(1, "AAA")])
    assert 1 in sim.positions


def test_strong_vs_plain_buy():
    sim, _ = _sim(102, {})
    _step(sim, DAYS[0], [_ev(1, "AAA", mos=0.30), _ev(2, "BBB", mos=0.45, rank=2)])
    r = _step(sim, DAYS[1], [_ev(1, "AAA", mos=0.30), _ev(2, "BBB", mos=0.45, rank=2)])
    types = {s["cik"]: s["type"] for s in r.signals}
    assert types == {1: "BUY", 2: "STRONG_BUY"}


def test_sell_when_gate_fails_but_not_on_price_drop():
    sim, _ = _sim(103, {})
    for d in DAYS[:3]:
        _step(sim, d, [_ev(1, "AAA", mos=0.3)])
    assert 1 in sim.positions
    r = _step(sim, DAYS[3], [_ev(1, "AAA", lst="pricey", price=60, mos=0.1)])  # price fell, business fine
    assert r.badges[1][0] in ("HOLD", "ADD") and not any(s["type"] == "SELL" for s in r.signals)
    r = _step(sim, DAYS[4], [_ev(1, "AAA", lst="out", gates=False,
                                 failed=[{"id": "r10", "name": "Debt payback", "value": 4.6}])])
    assert r.signals[0]["type"] == "SELL" and "Debt payback" in r.signals[0]["reason"]
    _step(sim, DAYS[5], [])
    assert 1 not in sim.positions
    ep = db.one("SELECT * FROM episodes WHERE run_id=103")
    assert ep["status"] == "closed" and ep["exit_reason"].startswith("thesis broken")


def test_review_then_sell_on_next_filing():
    sim, _ = _sim(104, {})
    for d in DAYS[:3]:
        _step(sim, d, [_ev(1, "AAA", q=85, mos=0.3)])
    r = _step(sim, DAYS[3], [_ev(1, "AAA", q=72, lst="pricey", filed="2025-02-20")])
    assert r.badges[1][0] == "REVIEW" and not r.signals
    r = _step(sim, DAYS[4], [_ev(1, "AAA", q=71, lst="pricey", filed="2025-02-20")])
    assert r.badges[1][0] == "REVIEW"  # same filing: still just a review
    r = _step(sim, DAYS[5], [_ev(1, "AAA", q=70, lst="pricey", filed="2026-02-20")])
    assert r.signals and r.signals[0]["type"] == "SELL" and "confirmed" in r.signals[0]["reason"]


def test_nosebleed_trim_after_ten_closes():
    sim, _ = _sim(105, {})
    for d in DAYS[:3]:
        _step(sim, d, [_ev(1, "AAA", mos=0.3)])
    trims = []
    for d in DAYS[3:15]:
        r = _step(sim, d, [_ev(1, "AAA", lst="pricey", price=320, iv=150, pe=45, pe_med=18)])
        trims += [s for s in r.signals if s["type"] == "TRIM"]
    assert len(trims) == 1 and abs(trims[0]["units"] - 0.33) < 1e-9


def test_oversized_trim():
    sim, pb = _sim(106, {})
    evs = [_ev(1, "AAA", mos=0.3), _ev(2, "BBB", mos=0.3, rank=2), _ev(3, "CCC", mos=0.3, rank=3)]
    for d in DAYS[:3]:
        _step(sim, d, evs)
    assert len(sim.positions) == 3
    a = pb.d["AAA"]  # AAA goes up 5x while the others are flat -> ~71% of the portfolio
    for i in range(4, len(a[0])):
        a[2][i] = a[3][i] = a[1][i] = 500.0
    r = _step(sim, DAYS[4], [_ev(1, "AAA", lst="pricey", price=500, iv=600), _ev(2, "BBB", lst="pricey"),
                             _ev(3, "CCC", lst="pricey")])
    t = [s for s in r.signals if s["type"] == "TRIM"]
    assert t and t[0]["cik"] == 1 and "oversized" in t[0]["reason"]


def test_single_holding_never_oversized():
    sim, _ = _sim(110, {})
    for d in DAYS[:4]:
        r = _step(sim, d, [_ev(1, "AAA", mos=0.3)])
    assert not any(s["type"] == "TRIM" for s in r.signals)


def test_swap_only_with_holdings_limit():
    sim, _ = _sim(107, {}, max_positions=1)
    for d in DAYS[:3]:
        _step(sim, d, [_ev(1, "AAA", mos=0.3, rs=60, er=0.10)])
    weak = _ev(1, "AAA", lst="pricey", rs=60, er=0.10)
    _step(sim, DAYS[3], [weak, _ev(2, "BBB", rs=85, er=0.16, mos=0.3)])
    r = _step(sim, DAYS[4], [weak, _ev(2, "BBB", rs=85, er=0.16, mos=0.3)])
    types = sorted(s["type"] for s in r.signals)
    assert types == ["BUY", "SWAP"]


def test_variable_holdings_no_swap_just_note():
    sim, _ = _sim(108, {})
    for d in DAYS[:3]:
        _step(sim, d, [_ev(1, "AAA", mos=0.3, rs=60, er=0.10)])
    weak = _ev(1, "AAA", lst="pricey", rs=60, er=0.10)
    _step(sim, DAYS[3], [weak, _ev(2, "BBB", rs=85, er=0.16, mos=0.3)])
    r = _step(sim, DAYS[4], [weak, _ev(2, "BBB", rs=85, er=0.16, mos=0.3)])
    assert [s["type"] for s in r.signals] == ["BUY"]
    assert "upgrade available" in r.badges[1][1]


def test_add_when_cheaper_with_cooldown():
    sim, pb = _sim(109, {})
    for d in DAYS[:3]:
        _step(sim, d, [_ev(1, "AAA", mos=0.3)])
    a = pb.d["AAA"]
    for i in range(3, len(a[0])):
        a[1][i] = a[2][i] = a[3][i] = 85.0
    r = _step(sim, DAYS[3], [_ev(1, "AAA", mos=0.45, price=85)])
    assert [s["type"] for s in r.signals] == ["ADD"]
    r = _step(sim, DAYS[4], [_ev(1, "AAA", mos=0.45, price=85)])
    assert not r.signals  # cooldown
