"""Exit rules (0.19): configurable trims, sells, switches and warnings."""

from __future__ import annotations

import pytest

from oracle_edge import db, exits, rules
from tests.test_engine import DAYS, _ev, _step
from tests.test_portfolio import _focused
from tests.test_ranked import MON1, MON2, TUE1, Snaps, _ranked


@pytest.fixture(scope="module", autouse=True)
def _db():
    db.get()
    rules.seed_profiles()


def _held(run_id, ev, **st):
    """A focused portfolio of one that has bought AAA (confirm, buy, fill)."""
    sim = _focused(run_id, n=1, **st)
    for d in DAYS[:3]:
        _step(sim, d, [ev])
    assert set(sim.positions) == {1}
    return sim


def test_old_settings_become_the_equivalent_rules():
    st = rules.load_yaml_profile()["settings"]
    rs = exits.rules_of(st)
    assert [r["when"] for r in rs] == ["fails_rules", "quality_below", "quality_drop", "q_hard", "q_soft",
                                       "near_limit", "price_over_value", "weight_above", "better_idea"]
    assert rs[1]["x"] == st["sell_quality"] and rs[6]["checks"] == st["nosebleed_days"]
    rk = exits.rules_of({**st, "buy_rule": "top_ranked", "ranked_sell_outside": 9})
    assert [(r["when"], r["x"]) for r in rk] == [("rank_below", 9), ("q_revenue", None)]
    assert exits.rules_of({**st, "exit_rules": []}) == []  # an empty list means: never sell on its own


def test_rule_lines_read_plainly():
    assert exits.describe_rule(exits.rule("quality_below", "sell", x=55)) == "Sell all when Quality falls below 55"
    line = exits.describe_rule(exits.rule("price_over_value", "trim", x=2, amount=0.25, confirm="checks", checks=3))
    assert line == "Trim 25% when the price is 2× the estimated value or more, after 3 checks in a row"
    assert exits.describe_rule(exits.rule("rank_below", x=8)) == "Sell all when it ranks below #8 by combined score"
    assert "every rule" not in exits.describe_rule(exits.rule("better_idea", "switch", x=15, y=0.04))


def test_custom_sell_on_quality_and_disabled_rules():
    sim = _held(601, _ev(1, "AAA", q=85), exit_rules=[exits.rule("quality_below", "sell", x=80, on=False)])
    r = _step(sim, DAYS[3], [_ev(1, "AAA", q=75)])
    assert not r.signals and r.badges[1][0] == "HOLD"  # the rule is off
    sim.st["exit_rules"] = [exits.rule("quality_below", "sell", x=80)]
    r = _step(sim, DAYS[4], [_ev(1, "AAA", q=75)])
    assert [(s["cik"], s["type"]) for s in r.signals] == [(1, "SELL")] and "Quality 75 < 80" in r.signals[0]["reason"]


def test_trim_on_value_after_checks_in_a_row():
    rule = exits.rule("price_over_value", "trim", x=1.5, amount=0.25, confirm="checks", checks=2)
    sim = _held(602, _ev(1, "AAA"), exit_rules=[rule])
    rich = _ev(1, "AAA", price=200.0, iv=100.0)
    r = _step(sim, DAYS[3], [rich])
    assert not r.signals                         # first check
    r = _step(sim, DAYS[4], [rich])
    assert [(s["type"], s["units"]) for s in r.signals] == [("TRIM", 0.25)]
    r = _step(sim, DAYS[5], [rich])
    assert not r.signals                         # the count starts again after a trim


def test_gain_trim_and_trailing_stop_use_prices():
    up = {d: (100.0 if d < DAYS[4] else 160.0 if d < DAYS[7] else 90.0) for d in DAYS}
    sim = _focused(603, n=1, prices={"AAA": up}, exit_rules=[
        exits.rule("trail_stop", "sell", x=0.35), exits.rule("gain_above", "trim", x=0.5, amount=0.5)])
    ev = _ev(1, "AAA")
    for d in DAYS[:3]:
        _step(sim, d, [ev])
    r = _step(sim, DAYS[4], [ev])                # +60% from cost
    assert [(s["type"], s["units"]) for s in r.signals] == [("TRIM", 0.5)]
    for d in DAYS[5:7]:
        _step(sim, d, [ev])
    r = _step(sim, DAYS[7], [ev])                # 160 -> 90: 44% below the high
    assert [s["type"] for s in r.signals] == ["SELL"] and "below its high" in r.signals[0]["reason"]


def test_warn_only_puts_it_under_review_without_trading():
    sim = _held(604, _ev(1, "AAA"), exit_rules=[exits.rule("exp_return_below", "warn", x=0.10)])
    r = _step(sim, DAYS[3], [_ev(1, "AAA", er=0.06)])
    assert not r.signals and r.badges[1][0] == "REVIEW" and "6.0%" in r.badges[1][1]


def test_filing_confirmed_sell_waits_for_a_newer_filing():
    sim = _held(605, _ev(1, "AAA"), exit_rules=[exits.rule("quality_drop", "sell", x=10, confirm="filing")])
    r = _step(sim, DAYS[3], [_ev(1, "AAA", q=70)])
    assert r.badges[1][0] == "REVIEW" and not r.signals
    r = _step(sim, DAYS[4], [_ev(1, "AAA", q=70)])
    assert r.badges[1][0] == "REVIEW" and not r.signals      # same filing: still waiting
    r = _step(sim, DAYS[5], [_ev(1, "AAA", q=70, filed="2026-02-20")])
    assert [s["type"] for s in r.signals] == ["SELL"] and "confirmed" in r.signals[0]["reason"]


def test_no_switch_rule_means_no_swaps():
    sim = _held(606, _ev(1, "AAA", rs=70, er=0.11), swap_min_hold_days=0, exit_rules=[])
    better = [_ev(1, "AAA", rank=2, rs=70, er=0.11), _ev(4, "DDD", rank=1, rs=95, er=0.25)]
    _step(sim, DAYS[3], better)
    r = _step(sim, DAYS[4], better)
    assert not any(s["type"] == "SWAP" for s in r.signals)
    sim.st["exit_rules"] = [exits.rule("better_idea", "switch", x=10, y=0.05)]
    r = _step(sim, DAYS[5], better)
    assert any(s["type"] == "SWAP" for s in r.signals)


def test_top_ranked_uses_wonderfulness_rules_and_skips_what_it_would_sell():
    scores = {1: (90, 90), 2: (88, 88), 3: (80, 80)}
    sim = _ranked(607, scores, exit_rules=[exits.rule("wonder_drop", "sell", x=5),
                                           exits.rule("combined_below", "sell", x=85)])
    r = _step(sim, MON1, [])
    assert {c for c, t in {(s["cik"], s["type"]) for s in r.signals}} == {1, 2}  # #3 (80) would be sold at once
    _step(sim, TUE1, [])
    old = sim.snaps.wonder_asof
    sim.snaps.wonder_asof = lambda c, d: (84 if d >= MON2 else 90) if c == 1 else old(c, d)  # point in time
    sim.snaps.scores[1] = (84, 92)   # Wonderfulness down 6 since bought (combined 88 still fine)
    r = _step(sim, MON2, [])
    sells = [s for s in r.signals if s["type"] == "SELL"]
    assert [s["cik"] for s in sells] == [1] and "Wonderfulness down 6" in sells[0]["reason"]


def test_presets():
    st = rules.load_yaml_profile()["settings"]
    assert [r["when"] for r in exits.preset("ranked", st)] == ["rank_below", "q_revenue"]
    assert exits.preset("classic", st)[0]["when"] == "fails_rules"
    with pytest.raises(KeyError):
        exits.TRIGGERS["nope"]
    assert isinstance(Snaps({}).ciks(), list)


def _evd(**detail):
    e = _ev(1, "AAA", price=300.0, iv=100.0)
    e.detail = detail
    return e


def test_fair_value_trim_unless_sales_are_growing():
    rule = exits.rule("fair_over", "trim", x=2.0, amount=0.33, unless="sales_growth_at_least", ux=0.05)
    sim = _held(608, _ev(1, "AAA"), exit_rules=[rule])
    r = _step(sim, DAYS[3], [_evd(fair_iv=120.0, sales_g=0.12)])
    assert not r.signals                                  # 2.5× fair value, but sales grow 12%: keep it
    r = _step(sim, DAYS[4], [_evd(fair_iv=120.0, sales_g=0.01)])
    assert [(s["type"], s["units"]) for s in r.signals] == [("TRIM", 0.33)] and "fair value" in r.signals[0]["reason"]
    assert "unless sales are growing 5% or more" in exits.describe_rule(rule)


def test_expected_return_and_growth_gap_triggers():
    sim = _held(609, _ev(1, "AAA"), exit_rules=[exits.rule("growth_gap_above", "warn", x=0.15),
                                                exits.rule("fair_return_below", "sell", x=0.04)])
    r = _step(sim, DAYS[3], [_evd(fair_iv=150.0, fair_er=0.07, implied_g=0.30, delivered_g=0.08)])
    assert r.badges[1][0] == "REVIEW" and "30% growth" in r.badges[1][1]
    r = _step(sim, DAYS[4], [_evd(fair_iv=150.0, fair_er=0.02, implied_g=0.10, delivered_g=0.08)])
    assert [s["type"] for s in r.signals] == ["SELL"]


def test_data_preset_reads_plainly():
    lines = [exits.describe_rule(r) for r in exits.preset("data", {})]
    assert any("3× its fair value" in x for x in lines) and any("below 4%" in x for x in lines)
    assert exits.needs_fair({"exit_rules": exits.preset("data", {})}) and not exits.needs_fair({})


def test_lagging_the_market_unless_growing():
    falling = {d: (100.0 if d < DAYS[3] else 80.0) for d in DAYS}
    rule = exits.rule("lagging_market", "sell", x=0.15, y=0.23, unless="sales_growth_at_least", ux=0.05)  # ~1 week
    sim = _focused(610, n=1, prices={"AAA": falling}, exit_rules=[rule])
    ev = _ev(1, "AAA")
    for d in DAYS[:3]:
        _step(sim, d, [ev])
    growing = _ev(1, "AAA")
    growing.detail = {"sales_g": 0.12}
    r = _step(sim, DAYS[7], [growing])
    assert not r.signals                                   # 20% behind the S&P 500, but sales grow 12%: keep it
    stalled = _ev(1, "AAA")
    stalled.detail = {"sales_g": 0.01}
    r = _step(sim, DAYS[8], [stalled])
    assert [s["type"] for s in r.signals] == ["SELL"] and "behind the S&P 500" in r.signals[0]["reason"]
    six = exits.rule("lagging_market", "sell", x=0.15, y=6, unless="sales_growth_at_least", ux=0.05)
    assert exits.describe_rule(six) == ("Sell all when it is 15% or more behind the S&P 500 over 6 months, "
                                        "unless sales are growing 5% or more")


def test_no_holdings_limit():
    from oracle_edge import db
    from oracle_edge.signals import ensure_portfolio

    p = rules.default_profile()
    db.get().execute("INSERT INTO runs(id,kind,name,profile_id,start_date) VALUES(?,?,?,?,?)",
                     (611, "backtest", "t", p.id, DAYS[0]))
    pid = ensure_portfolio(611, p, mode="focused", overrides={"max_holdings": 0})
    assert db.scalar("SELECT max_positions FROM portfolios WHERE id=?", (pid,)) is None
    assert "No limit on holdings" in rules.describe({"settings": {**p.settings, "max_holdings": 0}})
