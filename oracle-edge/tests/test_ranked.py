"""Top-ranked buying (0.18.1): weekly money into the best combined scores, sells on rank and shrinking sales."""

from __future__ import annotations

import json

import pytest

from oracle_edge import db, rules
from oracle_edge.signals import PortfolioSim, ensure_portfolio
from tests.test_engine import DAYS, FakeBook, _step

MON1, TUE1, MON2, MON3 = "2026-01-05", "2026-01-06", "2026-01-12", "2026-01-19"


@pytest.fixture(scope="module", autouse=True)
def _db():
    db.get()
    rules.seed_profiles()


class Snaps:
    """cik -> (Wonderfulness, Quality); flags: cik -> 10-Q flag ids."""

    def __init__(self, scores, flags=None):
        self.scores, self.flags = scores, flags if flags is not None else {}

    def ciks(self):
        return list(self.scores)

    def asof(self, cik, d):
        return {"quality": self.scores[cik][1], "filed": "2025-02-20"} if cik in self.scores else None

    def wonder_asof(self, cik, d):
        return self.scores[cik][0] if cik in self.scores else None

    def quarterly(self, cik, d, filed):
        f = self.flags.get(cik)
        if not f:
            return None
        return {"flags_json": json.dumps([{"id": x, "name": "Revenue shrinking", "detail": "sales -20%"} for x in f])}


TK = {1: "AAA", 2: "BBB", 3: "CCC", 4: "DDD", 5: "EEE"}


def _ranked(run_id, scores, flags=None, n=2, k=3, **over):
    profile = rules.default_profile()
    db.get().execute("INSERT INTO runs(id,kind,name,profile_id,start_date) VALUES(?,?,?,?,?)",
                     (run_id, "backtest", "t", profile.id, DAYS[0]))
    db.get().execute("INSERT INTO portfolios(run_id,name,kind,max_positions,position_cap,trim_to,sizing,cash,"
                     "last_value,last_nav,mode,contribution,weekly,contributed) "
                     "VALUES(?,?,?,7,0.4,0.3,'equal',0,0,1.0,'focused',0,200,0)", (run_id, "m", "model"))
    pid = db.scalar("SELECT id FROM portfolios WHERE run_id=?", (run_id,))
    pb = FakeBook({t: [(d, 100.0) for d in DAYS] for t in list(TK.values()) + ["SPY"]})
    sim = PortfolioSim(run_id, pid, profile, pb, TK)
    sim.st = dict(sim.st, buy_rule="top_ranked", ranked_buy_count=n, ranked_sell_outside=k,
                  ranked_sell_on_revenue=True, **over)
    sim.snaps = Snaps(scores, flags)
    return sim


def _types(r):
    return {(s["cik"], s["type"]) for s in r.signals}


def test_zero_start_buys_the_top_ranked_once_a_week():
    sim = _ranked(501, {1: (90, 90), 2: (88, 88), 3: (80, 80), 4: (70, 70)})
    r = _step(sim, MON1, [])  # started with $0: the first week's $200 arrives now
    assert _types(r) == {(1, "BUY"), (2, "BUY")}
    assert all(s["units"] == pytest.approx(100.0) for s in r.signals)
    r = _step(sim, TUE1, [])  # fills at this open; no second decision in the same week
    assert not r.signals and set(sim.positions) == {1, 2} and sim.cash == pytest.approx(0.0, abs=1e-6)
    assert r.badges[1][0] == "HOLD"
    r = _step(sim, MON2, [])  # next week's money goes to the same two
    assert _types(r) == {(1, "ADD"), (2, "ADD")}


def test_sells_outside_the_top_k_and_reinvests_in_the_top():
    scores = {1: (90, 90), 2: (88, 88), 3: (80, 80), 4: (75, 75), 5: (60, 60)}
    sim = _ranked(502, scores)
    _step(sim, MON1, [])
    _step(sim, TUE1, [])
    scores[1] = (79, 79)  # now #3: still inside the top 3, held but not bought
    r = _step(sim, MON2, [])
    assert (1, "SELL") not in _types(r) and _types(r) == {(2, "ADD"), (3, "BUY")}
    _step(sim, "2026-01-13", [])
    scores[1] = (50, 50)  # now last: sold, and the sale's money goes to this week's top 2
    r = _step(sim, MON3, [])
    sells = [s for s in r.signals if s["type"] == "SELL"]
    assert [s["cik"] for s in sells] == [1] and "outside the top 3" in sells[0]["reason"]
    buys = [s for s in r.signals if s["type"] in ("BUY", "ADD")]
    assert {s["cik"] for s in buys} == {2, 3}
    assert sum(s["units"] for s in buys) == pytest.approx(200.0 + 100.0)  # new money + the sold $100


def test_shrinking_sales_sell_and_block_buying():
    scores = {1: (90, 90), 2: (88, 88), 3: (80, 80)}
    flags = {}
    sim = _ranked(503, scores, flags)
    _step(sim, MON1, [])
    _step(sim, TUE1, [])
    flags[1] = ["rev_decline"]
    r = _step(sim, MON2, [])
    assert (1, "SELL") in _types(r) and "Revenue shrinking" in r.badges[1][1]
    assert {c for c, t in _types(r) if t in ("BUY", "ADD")} == {2, 3}  # the #1 company is skipped while flagged


def test_new_money_cap_skips_a_holding_that_is_too_big():
    sim = _ranked(504, {1: (90, 90), 2: (88, 88), 3: (80, 80)}, ranked_max_weight=0.4)
    _step(sim, MON1, [])
    _step(sim, TUE1, [])
    r = _step(sim, MON2, [])  # both holdings are 50% of the portfolio: the money goes to #3 instead
    assert {c for c, t in _types(r) if t in ("BUY", "ADD")} == {3}


def test_zero_starting_money_is_kept():
    p = rules.default_profile()
    db.get().execute("INSERT INTO runs(id,kind,name,profile_id,start_date) VALUES(?,?,?,?,?)",
                     (505, "backtest", "t", p.id, DAYS[0]))
    pid = ensure_portfolio(505, p, mode="focused", overrides={"initial_capital": 0, "weekly_contribution": 200})
    row = db.one("SELECT cash, weekly FROM portfolios WHERE id=?", (pid,))
    assert row["cash"] == 0 and row["weekly"] == 200


def test_seeded_strategy_describes_itself():
    p = rules.get_profile(db.scalar("SELECT id FROM profiles WHERE name=?", (rules.RANKED_NAME,)))
    assert p.settings["buy_rule"] == "top_ranked" and p.settings["initial_capital"] == 0
    # same scoring as the primary strategy: shares its scores, nothing to re-score
    assert rules.scoring_hash(p.body) == rules.scoring_hash(rules.default_profile().body)
    text = rules.describe(p.body)
    assert "4 best-scored" in text and "below #8" in text and "$200 a week" in text
