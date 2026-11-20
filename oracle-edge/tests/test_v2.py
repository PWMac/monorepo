"""Bank/insurer rulebooks, foreign valuation, quarterly early warnings, DB migration."""

from __future__ import annotations

import json
import sqlite3

import pytest

from oracle_edge import db, quarterly, rules
from oracle_edge.engine import _foreign_scale
from oracle_edge.metrics_fin import compute_bank, compute_insurer
from oracle_edge.valuation import value_financial
from tests.test_engine import DAYS, _ev, _sim, _step


@pytest.fixture(scope="module", autouse=True)
def _db():
    db.get()
    rules.seed_profiles()


# ------------------------------------------------------------------ quarterly TTM
def _state(entries_q, entries_a):
    st = quarterly._State()
    for item, s0, e0, v in entries_q:
        st.put_q(item, s0, e0, v)
    for item, fe, v in entries_a:
        st.put_a(item, fe, v)
    return st


def test_ttm_math():
    st = _state([("net_income", "2025-01-01", "2025-09-30", 50.0), ("net_income", "2024-01-01", "2024-09-30", 70.0)],
                [("net_income", "2024-12-31", 100.0)])
    assert quarterly._ttm(st, "net_income", "2025-09-30") == pytest.approx(80.0)  # 50 + 100 - 70


def test_quarterly_flags_hard_and_soft():
    p = rules.default_profile()
    st = _state([("net_income", "2025-01-01", "2025-06-30", -40.0), ("net_income", "2024-01-01", "2024-06-30", 30.0),
                 ("shares_diluted", "2025-04-01", "2025-06-30", 112.0),
                 ("shares_diluted", "2024-04-01", "2024-06-30", 100.0),
                 ("equity", "", "2025-06-30", 500.0)],
                [("net_income", "2024-12-31", 60.0), ("equity", "2024-06-30", 480.0)])
    flags, m = quarterly.check(st, "2025-06-30", "standard", p)
    ids = {f["id"]: f["hard"] for f in flags}
    assert m["ttm_net_income"] == pytest.approx(-10.0)
    assert ids["ttm_loss"] is True and ids["dilution"] is False


# ------------------------------------------------------------------ banks & insurers
def _stmt(n=10, **items):
    s = {"years": [f"{2016 + i}-12-31" for i in range(n)]}
    for k, v in items.items():
        s[k] = v if isinstance(v, list) else [v] * n
    from oracle_edge.sources.sec import concept_map

    for k in list(concept_map()) + ["shares_diluted"]:
        s.setdefault(k, [None] * n)
    return s


def test_bank_metrics():
    s = _stmt(net_income=120.0, total_assets=10_000.0, equity=1_000.0, noninterest_expense=300.0,
              net_interest_income=400.0, noninterest_income=150.0, loans=6_000.0, deposits=7_500.0,
              net_charge_offs=18.0, revenue=550.0)
    m = compute_bank(s)
    assert m["roa"].value == pytest.approx(0.012)
    assert m["efficiency"].value == pytest.approx(300 / 550)
    assert m["loans_to_deposits"].value == pytest.approx(0.8)
    assert m["nco_ratio"].value == pytest.approx(0.003)


def test_insurer_combined_ratio_and_fallback():
    s = _stmt(premiums_earned=1000.0, losses_incurred=620.0, dac_amort=170.0, other_uw_expense=120.0,
              equity=1000.0, loss_reserves=[1400.0 * 1.05 ** i for i in range(10)], unearned_premiums=450.0,
              shares_diluted=100.0)
    m = compute_insurer(s)
    assert m["combined_ok_years"].value == 1.0  # 91% every year
    assert m["float_growth"].value > 0.03
    s2 = _stmt(premiums_earned=1000.0, pretax_income=50.0, net_investment_income=100.0, equity=1000.0)
    m2 = compute_insurer(s2)
    assert m2["combined_ok_years"].value == 0.0 and "approximated" in m2["combined_ok_years"].detail  # (1000+100-50)/1000


def test_value_financial_pb():
    st = rules.load_yaml_profile()["settings"]
    vi = {"book_ps": 50.0, "roe": 0.15, "sust_g": 0.05, "eps3": 7.5, "eps_last": 7.5, "payout": 0.3,
          "eps_by_fy": {}, "eps_cagr": 0.05}
    v = value_financial(60.0, vi, st, 0.045, 0.25, {})
    # k = max(10% hurdle, 6.5%) = 10%; P/B = (0.15 - 0.05) / (0.10 - 0.05) = 2.0 -> 100
    assert v.iv_a == pytest.approx(100.0)
    assert v.iv == min(v.iv_a, v.iv_b)
    assert v.note.startswith("P/B justified 2.00")


# ------------------------------------------------------------------ foreign valuation
class FakeMacro:
    def fx(self, cur, d):
        return {"EUR": 1.10, "USD": 1.0}.get(cur)


def test_foreign_scale_fx_and_adr():
    vi = {"oe_ps": 5.0, "eps3": 4.0, "eps_last": 4.0, "bvps": 20.0, "shares": 1_000_000.0,
          "eps_by_fy": {"2024-12-31": 4.0}}
    out, why = _foreign_scale(vi, {"currency": "EUR", "adr_ratio": 2.0}, "2026-01-05", FakeMacro(), None)
    assert why == "" and out["eps_last"] == pytest.approx(8.8) and out["shares"] == 500_000
    assert out["eps_by_fy"]["2024-12-31"] == pytest.approx(8.8)
    none, why = _foreign_scale(vi, {"currency": "EUR", "adr_ratio": None}, "2026-01-05", FakeMacro(), None)
    assert none is None and "ADR" in why
    none, why = _foreign_scale(vi, {"currency": "XYZ", "adr_ratio": 1.0}, "2026-01-05", FakeMacro(), None)
    assert none is None and "exchange rate" in why


# ------------------------------------------------------------------ early warnings in the signal ladder
HARD = [{"id": "ttm_loss", "name": "Trailing-12-month loss", "hard": True, "detail": "TTM net income -5M"}]
SOFT = [{"id": "dilution", "name": "Sudden dilution", "hard": False, "detail": "diluted shares up 12%"}]


def test_hard_quarterly_flag_review_then_sell_on_next_filing():
    sim, _ = _sim(201, {})
    for d in DAYS[:3]:
        _step(sim, d, [_ev(1, "AAA", mos=0.3)])
    r = _step(sim, DAYS[3], [_with(_ev(1, "AAA", lst="pricey"), HARD, "2026-01-04")])
    assert r.badges[1][0] == "REVIEW" and "becomes SELL" in r.badges[1][1]
    r = _step(sim, DAYS[4], [_with(_ev(1, "AAA", lst="pricey"), HARD, "2026-01-04")])
    assert r.badges[1][0] == "REVIEW" and not r.signals  # same 10-Q: still a review
    r = _step(sim, DAYS[5], [_with(_ev(1, "AAA", lst="pricey"), HARD, "2026-01-08")])
    assert r.signals and r.signals[0]["type"] == "SELL" and "confirmed" in r.signals[0]["reason"]


def test_soft_flag_never_sells():
    sim, _ = _sim(202, {})
    for d in DAYS[:3]:
        _step(sim, d, [_ev(1, "AAA", mos=0.3)])
    for i, d in enumerate(DAYS[3:8]):
        r = _step(sim, d, [_with(_ev(1, "AAA", lst="pricey"), SOFT, f"2026-01-{10 + i:02d}")])
        assert r.badges[1][0] == "REVIEW" and not any(s["type"] == "SELL" for s in r.signals)


def test_near_gate_is_a_watch_item():
    sim, _ = _sim(203, {})
    for d in DAYS[:3]:
        _step(sim, d, [_ev(1, "AAA", mos=0.3)])
    e = _ev(1, "AAA", lst="pricey")
    e.gate_margin, e.gate_margin_rule = 0.05, {"id": "r10", "name": "Debt payback", "value": 3.8, "target": 4.0,
                                               "fmt": "years"}
    r = _step(sim, DAYS[3], [e])
    assert r.badges[1][0] == "REVIEW" and "Debt payback close to its limit (3.8 yrs vs 4.0 yrs)" in r.badges[1][1]


def test_hard_flag_blocks_new_buy():
    sim, _ = _sim(204, {})
    for d in DAYS[:3]:
        r = _step(sim, d, [_with(_ev(1, "AAA", mos=0.3), HARD, "2026-01-02")])
    assert not r.signals and r.badges[1][0] == "WATCH" and "10-Q" in r.badges[1][1]
    assert 1 not in sim.positions


def _with(e, flags, qfiled):
    e.qflags, e.qfiled = flags, qfiled
    return e


# ------------------------------------------------------------------ upgrades from v1
def test_migration_from_v1_and_profile_defaults(tmp_path):
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.executescript(db.MIGRATIONS[0])
    con.execute("INSERT INTO meta(key,value) VALUES('schema_version','1')")
    old = rules.load_yaml_profile()
    old.pop("bank_rules")
    old.pop("insurer_rules")
    old["settings"].pop("quarterly_warnings")
    con.execute("INSERT INTO profiles(name,version,locked,active,body_json) VALUES('Old',1,1,1,?)", (json.dumps(old),))
    con.commit()
    con.close()
    c2 = db.connect(p)
    db.migrate(c2)
    cols = {r[1] for r in c2.execute("PRAGMA table_info(securities)").fetchall()}
    assert {"adr_ratio", "adr_source"} <= cols
    assert c2.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(len(db.MIGRATIONS))
    body = rules._with_defaults(json.loads(c2.execute("SELECT body_json FROM profiles").fetchone()[0]))
    assert body["bank_rules"] and body["insurer_rules"] and body["settings"]["quarterly_warnings"] is True
