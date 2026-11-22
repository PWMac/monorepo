"""Strategies and the house view (0.18)."""

import json

import pytest

from oracle_edge import db, engine, rules


@pytest.fixture(autouse=True)
def _seed():
    rules.seed_profiles()


def test_one_house_view_scores_every_strategy_the_same():
    hv = rules.house_view()
    assert hv and hv["rules"]
    profs = rules.list_profiles()
    std = [p for p in profs if rules.scoring_standard(p.settings) == "standard"]
    short = [p for p in profs if rules.scoring_standard(p.settings) == "short"]
    assert std and short  # Relaxed backfill (5y) is the short-record one
    assert len({rules.scoring_hash(p.body) for p in std}) == 1  # same rules, same record length: same scores
    assert all(p.settings["lookback_years"] == 5 and p.settings["min_years"] == 5 for p in short)
    # a strategy's own stored rules no longer matter: the house view's are applied when it loads
    p = next(x for x in std if not x.locked) if any(not x.locked for x in std) else None
    if p:
        b = json.loads(json.dumps(p.body))
        b["rules"][0]["target"] = 99.0
        rules.update_profile(p.id, b)
        assert rules.get_profile(p.id).rules[0]["target"] == hv["rules"][0]["target"]


def test_changing_the_house_view_rescores_everyone_once():
    before = {p.id: p.version for p in rules.list_profiles()}
    b = json.loads(json.dumps(rules.default_profile().body))
    assert not rules.house_changed(b)
    b["rules"][0]["target"] = float(b["rules"][0]["target"]) * 1.1
    assert rules.house_changed(b)
    v = rules.set_house_view(b)
    assert v == rules.house_view()["version"]
    assert all(p.version == before[p.id] + 1 for p in rules.list_profiles())
    assert rules.default_profile().rules[0]["target"] == pytest.approx(b["rules"][0]["target"])


def test_primary_strategy_comes_first_and_must_be_standard():
    pid = rules.primary_id()
    assert pid and rules.default_profile().id == pid
    assert rules.active_profiles()[0].id == pid
    short = next(p for p in rules.list_profiles() if rules.scoring_standard(p.settings) == "short")
    with pytest.raises(ValueError):
        rules.set_primary(short.id)
    other = next(p for p in rules.list_profiles() if p.id != pid and rules.scoring_standard(p.settings) == "standard")
    rules.set_primary(other.id)
    assert rules.default_profile().id == other.id and rules.get_profile(other.id).active
    assert rules.active_profiles()[0].id == other.id
    rules.set_primary(pid)


class _Snaps:
    def __init__(self, wonder, ind):
        self.w, self.ind = wonder, ind

    def wonder_asof(self, cik, d):
        return self.w.get(cik)

    def industry(self, cik):
        return self.ind.get(cik, "")


def test_universe_filters():
    snaps = _Snaps({1: 80.0, 2: 60.0}, {1: "Semiconductors", 2: "Banks", 3: "Retail"})
    st = {"universe_wonder_min": 70}
    assert engine.universe_ok(st, snaps, 1, "2026-01-01")[0]
    assert not engine.universe_ok(st, snaps, 2, "2026-01-01")[0]
    assert not engine.universe_ok(st, snaps, 3, "2026-01-01")[0]  # unknown Wonderfulness: left out
    assert engine.universe_ok({}, snaps, 3, "2026-01-01")[0]
    assert engine.universe_ok({"universe_industries": ["Semiconductors"]}, snaps, 1, "x")[0]
    assert not engine.universe_ok({"universe_industries": ["Semiconductors"]}, snaps, 3, "x")[0]
    assert not engine.universe_ok({"universe_exclude_industries": ["Banks"]}, snaps, 2, "x")[0]


def test_wonderfulness_is_point_in_time():
    pid = rules.default_profile().snap[0]
    c = 97001
    for cohort, filed, score in (("FY2023", "2023-03-01", 60.0), ("FY2024", "2024-03-01", 72.0),
                                 ("now", "2024-03-01", 74.0)):
        db.get().execute("INSERT OR REPLACE INTO wonder(cik,profile_id,cohort,as_of,filed,score,rank,n,pillars_json,"
                         "feats_json) VALUES(?,?,?,?,?,?,?,?,?,?)", (c, pid, cohort, filed, filed, score, 1, 1, "{}", "{}"))
    wb = engine.wonder_book()
    assert wb[c][0] == ["2023-03-01", "2024-03-01"] and wb[c][1] == [60.0, 74.0]


def test_screener_filters_become_a_universe():
    from oracle_edge.ui import wonder_page as wp

    upd, ignored = wp.strategy_from_filters({"score": {"min": 70}, "quality": {"min": 75, "max": None},
                                             "mcap": {"min": 10}, "industry": ["Semiconductors"],
                                             "ceo_grade": ["A", "B"], "v_mos": {"min": 25}}, 2e9)
    assert upd == {"universe_wonder_min": 70.0, "quality_min": 75.0, "min_market_cap": 10e9,
                   "universe_industries": ["Semiconductors"]}
    assert set(ignored) == {"CEO grade", "Margin of safety"}
