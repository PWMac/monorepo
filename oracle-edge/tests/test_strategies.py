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
    cid = rules.app_checklist_id()
    for p in rules.list_profiles():  # only the strategies on that Oracle checklist are re-scored
        on = rules.checklist_id(p.settings) == cid
        assert p.version == before[p.id] + (1 if on else 0), p.name
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


def test_hide_and_delete_strategies():
    prim = rules.primary_id()
    with pytest.raises(ValueError):
        rules.set_hidden(prim, True)
    with pytest.raises(ValueError):
        rules.delete_profile(prim)
    locked = next(p for p in rules.list_profiles() if p.locked and p.id != prim)
    with pytest.raises(ValueError):
        rules.delete_profile(locked.id)          # built-ins are hidden instead
    rules.set_hidden(locked.id, True)
    assert locked.id not in [p.id for p in rules.list_profiles(include_hidden=False)]
    rules.set_hidden(locked.id, False)
    # a copy that owns the shared scores: deleting it hands them to a strategy scored the same way
    cid = rules.save_as(prim, "temp copy", json.loads(json.dumps(rules.get_profile(prim).body)))
    c = rules.get_profile(cid)
    con = db.get()
    con.execute("INSERT INTO snapshots(cik,profile_id,profile_version,filed,quality) VALUES(1,?,?,?,80)",
                (cid, c.version, "2026-01-01"))
    con.execute("INSERT INTO runs(kind,name,profile_id,start_date) VALUES('backtest','t',?,'2026-01-01')", (cid,))
    res = rules.delete_profile(cid)
    assert res["runs"] == 1 and not db.scalar("SELECT COUNT(*) FROM runs WHERE profile_id=?", (cid,))
    heir_rows = db.scalar("SELECT COUNT(*) FROM snapshots WHERE profile_id!=? AND filed='2026-01-01' AND cik=1",
                          (cid,))
    assert heir_rows == 1                                  # kept under another strategy, not lost
    with pytest.raises(KeyError):
        rules.get_profile(cid)


def test_house_view_restore_defaults():
    b = json.loads(json.dumps(rules.default_profile().body))
    b["rules"][0]["target"] = float(b["rules"][0]["target"]) * 1.3
    rules.set_house_view(b)
    d = rules.house_defaults()
    assert rules.house_changed(d)
    rules.set_house_view(d)
    assert not rules.house_changed(d)
    assert rules.default_profile().rules[0]["target"] == pytest.approx(d["rules"][0]["target"])


def test_oracle_checklists():
    cks = rules.checklists()
    assert len(cks) == 1                                   # one checklist: record length is the strategy's setting
    app = rules.app_checklist_id()
    assert rules.checklist_id(rules.default_profile().settings) == app
    relaxed = next(p for p in rules.list_profiles() if p.name == "Relaxed backfill (5y)")
    assert rules.scoring_standard(relaxed.settings) == "short" and rules.checklist_id(relaxed.settings) == app
    assert relaxed.settings["lookback_years"] == 5 and relaxed.snap != rules.default_profile().snap
    # a copy with a different rule scores differently, so a strategy moved onto it gets its own scores
    nid = rules.copy_checklist(app, "Lenient debt")
    with pytest.raises(ValueError):
        rules.copy_checklist(app, "Lenient debt")
    b = json.loads(json.dumps(rules.house_view(nid)))
    b["rules"][0]["target"] = float(b["rules"][0]["target"]) * 0.5
    rules.set_house_view(b, nid)
    cid = rules.save_as(rules.primary_id(), "on lenient", None)
    body = json.loads(json.dumps(rules.get_profile(cid).body))
    body["settings"]["checklist_id"] = nid
    rules.update_profile(cid, body)
    p = rules.get_profile(cid)
    assert rules.checklist_id(p.settings) == nid and p.rules[0]["target"] == pytest.approx(b["rules"][0]["target"])
    assert rules.scoring_hash(p.body) != rules.scoring_hash(rules.default_profile().body)
    with pytest.raises(ValueError):
        rules.delete_checklist(nid)                                  # still in use
    rules.delete_profile(cid)
    rules.rename_checklist(nid, "Lenient debt v2")
    rules.delete_checklist(nid)
    assert nid not in rules.checklists()


def test_identical_five_year_checklist_from_early_builds_is_folded_in():
    cks = rules.checklists()
    app = min(cks)
    dup = max(cks) + 1
    cks[dup] = {**json.loads(json.dumps(cks[app])), "name": "Oracle checklist · 5-year record", "record": "short"}
    rules._save_checklists(cks)
    pid = rules.save_as(rules.primary_id(), "five-year copy", None)
    b = json.loads(db.one("SELECT body_json FROM profiles WHERE id=?", (pid,))["body_json"])
    b["settings"]["checklist_id"] = dup
    db.get().execute("UPDATE profiles SET body_json=? WHERE id=?", (json.dumps(b), pid))
    rules.ensure_house_view()
    assert dup not in rules.checklists()
    p = rules.get_profile(pid)
    assert rules.checklist_id(p.settings) == app and rules.scoring_standard(p.settings) == "short"
    rules.delete_profile(pid)
