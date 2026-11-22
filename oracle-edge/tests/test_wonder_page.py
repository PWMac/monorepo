"""The Wonderful page (0.17): column histories and directions, filters, the map's encoding, CEO grades."""

from __future__ import annotations

import json

import pytest

from oracle_edge import ceo, db, rules, wonderful
from oracle_edge.ui import wonder_page as wp
from oracle_edge.ui.wonder import _aligned


@pytest.fixture(scope="module", autouse=True)
def _db():
    db.get()
    rules.seed_profiles()


def test_direction_compares_the_last_two_years_with_the_three_before():
    assert wonderful.direction([50, 50, 50, 56, 58], 4) == "up"
    assert wonderful.direction([60, 61, 59, 54, 53], 4) == "down"
    assert wonderful.direction([60, 62, 58, 61, 60], 4) == "flat"
    assert wonderful.direction([60, 70], 4) is None
    assert wonderful.summarize([("FY2020", 65.0), ("FY2021", 72.0), ("FY2022", 75.0), ("FY2023", 81.0)])["streak_70"] == 3


def test_histories_follow_each_column_and_end_on_todays_figures():
    p = rules.default_profile()
    pid, ver = p.snap
    cik = 990002
    with db.tx() as con:
        con.execute("DELETE FROM wonder WHERE cik=?", (cik,))
        con.execute("DELETE FROM snapshots WHERE cik=?", (cik,))
        for k, (cohort, as_of, score, gm, dp) in enumerate([("FY2020", "2020-12-31", 70, 0.40, 2.0),
                                                            ("FY2021", "2021-12-31", 71, 0.41, 2.1),
                                                            ("FY2022", "2022-12-31", 72, 0.40, 2.0),
                                                            ("FY2023", "2023-12-31", 74, 0.46, 1.0),
                                                            ("now", "2023-12-31", 75, 0.47, 0.9)]):
            con.execute("INSERT INTO wonder(cik,profile_id,cohort,as_of,filed,score,rank,n,pillars_json,feats_json) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (cik, pid, cohort, as_of, as_of, score, 1, 10, json.dumps({"moat": 80}),
                         json.dumps({"f": {"gm_last": gm, "debt_payback": dp, "roic_last": 0.2}, "pct": {}})))
    wonderful._HIST_CACHE.clear()
    h = wonderful.metric_histories(p)[cik]
    assert h["hist"]["gm_med"][-1] == ["FY2023", 0.47]  # today's figures stand for the latest year
    assert len(h["hist"]["gm_med"]) == 4
    assert h["tr"]["gm_med"] == "up"
    assert h["tr"]["debt_payback"] == "down"  # falling debt payback; the page colours it green (lower is better)
    al, y0 = _aligned({"a": [["FY2020", 1.0], ["FY2023", 2.0]], "b": [["FY2021", 3.0]]})
    assert y0 == 2020 and al["a"] == [1.0, None, None, 2.0] and al["b"] == [None, 3.0, None, None]
    with db.tx() as con:
        con.execute("DELETE FROM wonder WHERE cik=?", (cik,))
    wonderful._HIST_CACHE.clear()


def _row(**kw):
    r = {"industry": "Software", "tier": "Wonderful", "ceo_grade": "B", "passes": True, "trend_dir": "up",
         "score": 75.0, "quality": 80.0, "gm_med": 0.55, "mcap": 20e9, "debt_payback": 1.5, "ceo_score": 70.0}
    r.update(kw)
    return r


def test_filters_by_list_and_by_range_in_display_units():
    assert wp.passes(_row(), {"industry": ["Software", "Retail"], "gm_med": {"min": 50, "max": None}})
    assert not wp.passes(_row(), {"gm_med": {"min": 60}})  # 60% in the box = 0.60
    assert wp.passes(_row(), {"mcap": {"min": 10, "max": 50}})  # $B
    assert not wp.passes(_row(mcap=None), {"mcap": {"min": 10}})  # a missing value fails a number filter
    assert wp.passes(_row(), {"gm_med": {"min": None, "max": None}, "tier": []})  # empty filters do nothing
    assert not wp.passes(_row(ceo_grade=None), {"ceo_grade": ["A", "B"]})
    assert wp.passes(_row(ceo_grade=None), {"ceo_grade": ["none"]})
    assert not wp.passes(_row(passes=False), {"checklist": ["pass"]})
    assert wp.passes(_row(trend_dir=None), {"wtrend": ["new"]})
    assert wp.chip_text("gm_med", {"min": 40, "max": None}) == "Gross margin ≥ 40%"
    assert wp.chip_text("mcap", {"min": 10, "max": 50}) == "Market value ($B): 10–50 $B"
    assert wp.chip_text("industry", ["A", "B", "C", "D"], {}) == "Industry: A, B, C +1"


def test_the_map_shows_direction_by_shape_and_time_at_the_top_by_size():
    rows = [_row(ticker="AAA", name="A", quality=80.0, score=85.0, trend_dir="up", streak70=8, trend=6.0),
            _row(ticker="BBB", name="B", quality=60.0, score=50.0, trend_dir="down", streak70=0, trend=-7.0, passes=False)]
    o = wp.map_options(rows, {"AAA"}, 70)
    a = o["series"][0]["data"][0]
    b = next(d for s in o["series"] for d in s["data"] if d["name"] == "BBB")
    assert a["symbol"] == "triangle" and a["symbolRotate"] == 0 and a["symbolSize"] == 17
    assert b["symbol"] == "triangle" and b["symbolRotate"] == 180 and b["symbolSize"] == 6
    assert b["itemStyle"]["opacity"] < a["itemStyle"]["opacity"]  # outside the current view: faded
    assert any(z["type"] == "slider" for z in o["dataZoom"]) and "dataZoom" in o["toolbox"]["feature"]


def test_ceo_grades_are_saved_for_the_ranking_page():
    ceo.save_grade(990003, {"current": {"name": "Pat Example", "start": "2015-01-01"},
                            "career": {"grade": "A", "score": 86.0, "years": 9.0, "n": 2},
                            "record": {"rating": {"grade": "B", "score": 70.0}, "years": 9.0}})
    g = ceo.grades()[990003]
    assert g["grade"] == "A" and g["basis"] == "career" and g["name"] == "Pat Example"
    ceo.save_grade(990003, {"current": {"name": "New Person", "start": "2025-06-01"}, "record": {"years": 1.0}})
    g = ceo.grades()[990003]
    assert g["grade"] is None and "under two" in g["note"]
    with db.tx() as con:
        con.execute("DELETE FROM ceo_grades WHERE cik=990003")


def test_every_module_imports():
    """A syntax error in a page module must fail the tests, not the app (pages aren't otherwise loaded here)."""
    import importlib
    import pkgutil

    import oracle_edge
    import oracle_edge.ui

    for pkg in (oracle_edge, oracle_edge.ui):
        for m in pkgutil.iter_modules(pkg.__path__):
            if m.name not in ("app", "__main__"):
                importlib.import_module(f"{pkg.__name__}.{m.name}")


def test_value_filters_and_the_value_pass():
    from oracle_edge import values

    assert wp.passes(_row(v_buy=True, v_mos=0.30), {"v_buy": ["yes"], "v_mos": {"min": 25}})
    assert not wp.passes(_row(v_buy=None), {"v_buy": ["yes", "no"]})
    assert wp.passes(_row(v_buy=None), {"v_buy": ["none"]})
    n = values.compute_all()  # runs on whatever the test database holds
    assert n == len(values.all_values())


def test_stock_page_evaluates_untracked_companies_on_the_spot():
    from oracle_edge.engine import evaluate_one
    from oracle_edge.ui import valuecard

    assert evaluate_one(987654321, rules.default_profile()) is None  # unknown company: nothing, no error
    svg = valuecard.value_line_html(15.6, 19.5, 22.7, 26.2, 17.0, None)
    assert 'y="13"' in svg and 'y="30"' in svg  # today's price and buy-below labels never collide


def test_background_ceo_grading_saves_real_answers_only(monkeypatch):
    import time

    from oracle_edge.sources import company, wikidata

    monkeypatch.setattr(company, "get", lambda t, refresh=False: {"officers": []})
    monkeypatch.setattr(wikidata, "ceos", lambda cik, t: None)  # Wikidata unreachable
    monkeypatch.setattr(wikidata, "companies_led", lambda *a, **k: [])
    with db.tx() as con:
        con.execute("DELETE FROM ceo_grades WHERE cik IN (990010, 990011)")
    assert ceo.queue_grades([(990010, "ZZA")]) == 1
    for _ in range(50):
        if not ceo.grading_status()["running"]:
            break
        time.sleep(0.1)
    assert 990010 not in ceo.graded_since([990010])  # unreachable: nothing saved, tried again later
    monkeypatch.setattr(wikidata, "ceos", lambda cik, t: [])  # reachable, but no CEO on record
    ceo.queue_grades([(990011, "ZZB")])
    for _ in range(50):
        if not ceo.grading_status()["running"]:
            break
        time.sleep(0.1)
    g = ceo.graded_since([990011])[990011]
    assert g["grade"] is None and g["note"] == "CEO not found"
    with db.tx() as con:
        con.execute("DELETE FROM ceo_grades WHERE cik IN (990010, 990011)")


def test_officer_change_8ks_are_found_by_item_502():
    from oracle_edge.sources import sec

    doc = {"filings": {"recent": {"form": ["8-K", "8-K", "10-Q", "8-K/A", "8-K"],
                                  "filingDate": ["2026-11-18", "2026-11-10", "2026-11-05", "2026-10-01", "2025-01-01"],
                                  "items": ["5.02,9.01", "8.01", "", "5.02", "5.02"]}}}
    assert sec.parse_officer_changes(doc, "2026-09-01") == ["2026-11-18", "2026-10-01"]
    idx = ("8-K              ACME CORP                       12345   20261118  edgar/data/12345/x.txt\n"
           "10-K             ACME CORP                       12345   20261118  edgar/data/12345/y.txt\n")
    assert [f for f, _, _ in sec.parse_daily_index(idx)] == ["10-K"]  # the default still means annual + quarterly
    assert {f for f, _, _ in sec.parse_daily_index(idx, sec.EIGHTK_FORMS | sec.ANNUAL_FORMS)} == {"8-K", "10-K"}


def test_daily_ceo_check_finds_a_new_ceo_and_records_the_change(monkeypatch):
    from oracle_edge.sources import company, sec, wikidata

    cik = 990020
    with db.tx() as con:
        con.execute("DELETE FROM ceo_grades WHERE cik=?", (cik,))
        con.execute("DELETE FROM ceo_changes WHERE cik=?", (cik,))
        con.execute("DELETE FROM market_alerts WHERE kind='ceo_change'")
        con.execute("INSERT OR REPLACE INTO securities(cik, ticker, name) VALUES(?,?,?)", (cik, "ZZCEO", "Zz Corp"))
    old = [{"qid": "Q1", "name": "Ann Oldham", "start": "2012-01-01", "end": None}]
    monkeypatch.setattr(wikidata, "ceos", lambda c, t: old)  # Wikidata still lists the old CEO
    monkeypatch.setattr(wikidata, "companies_led", lambda *a, **k: [])
    monkeypatch.setattr(company, "get", lambda t, refresh=False: {"officers": [{"name": "Ann Oldham", "title": "CEO"}]})
    ceo.assess(cik, "ZZCEO")
    assert ceo.grades()[cik]["name"] == "Ann Oldham"
    # the company files an 8-K (item 5.02); Yahoo already shows the new CEO
    monkeypatch.setattr(company, "get", lambda t, refresh=False: {"officers": [{"name": "Bob Newman",
                                                                                "title": "Chief Executive Officer"}]})
    monkeypatch.setattr(sec, "officer_changes", lambda c, since: ["2026-11-18"])
    out = ceo.check_changes({cik: "2026-11-18", 424242: "2026-11-18"}, set())
    assert out["checked_8k"] == 1 and out["officer_changes"] == 1  # companies we don't follow aren't looked up
    g = ceo.grades()[cik]
    assert g["name"] == "Bob Newman" and g["start"] == "2026-11-18"
    ch = ceo.recent_changes(3650)[cik]
    assert ch["old_name"] == "Ann Oldham" and ch["new_name"] == "Bob Newman" and "8-K" in ch["source"]
    assert db.scalar("SELECT COUNT(*) FROM market_alerts WHERE kind='ceo_change' AND message LIKE 'ZZCEO%'") == 1
    ceo.check_changes({cik: "2026-11-18"}, set())  # seen again: no duplicate change
    assert db.scalar("SELECT COUNT(*) FROM ceo_changes WHERE cik=?", (cik,)) == 1
    from oracle_edge import emailer

    assert "CEO changes" in emailer.market_html(db.rows("SELECT * FROM market_alerts WHERE kind='ceo_change'"))
    with db.tx() as con:
        con.execute("DELETE FROM ceo_grades WHERE cik=?", (cik,))
        con.execute("DELETE FROM ceo_changes WHERE cik=?", (cik,))
        con.execute("DELETE FROM market_alerts WHERE kind='ceo_change'")
        con.execute("DELETE FROM securities WHERE cik=?", (cik,))


def test_ceo_start_date_is_read_from_a_biography_sentence():
    from oracle_edge.sources import sec

    f = sec.ceo_start_in_text
    assert f("Timothy D. Cook has served as the Company's Chief Executive Officer since August 2011.", "Tim Cook")[0] \
        == "2011-08-01"
    assert f("Satya Nadella was appointed Chief Executive Officer in February 2014 and Chairman in 2021.",
             "Satya Nadella")[0] == "2014-02-01"
    assert f("Anirudh Devgan has served as our President since 2017 and as Chief Executive Officer since December "
             "2021.", "Anirudh Devgan")[0] == "2021-12-01"
    assert f("Mr. Brown served as interim CEO in 2015 and has been CFO since 2016.", "Al Brown") is None
    assert f("Ms. Lee has served as Chief Executive Officer since 2012.", "Bob Jones") is None  # someone else
    html = "<p>Mr.&nbsp;Jones has served as CEO since <b>January 1, 2019</b>.</p>"
    assert f(sec._text(html), "Bob Jones")[0] == "2019-01-01"


def test_missing_start_date_comes_from_filings_and_coverage_says_why(monkeypatch):
    from oracle_edge.sources import company, sec, wikidata

    cik = 990040
    with db.tx() as con:
        con.execute("DELETE FROM ceo_grades WHERE cik IN (990040, 990041)")
        con.execute("DELETE FROM ceo_cache WHERE key LIKE 'secstart:99004%'")
    monkeypatch.setattr(wikidata, "ceos", lambda c, t: [{"qid": "Q9", "name": "Dana Ruiz", "start": None, "end": None}])
    monkeypatch.setattr(wikidata, "companies_led", lambda *a, **k: [])
    monkeypatch.setattr(company, "get", lambda t, refresh=False: {"officers": []})
    calls = []
    monkeypatch.setattr(sec, "ceo_start_from_filings", lambda c, n: calls.append(c) or (
        {"start": "2015-03-01", "form": "DEF 14A", "filed": "2026-03-20",
         "sentence": "Ms. Ruiz has served as CEO since March 2015."} if c == cik else None))
    res = ceo.assess(cik, "ZZS")
    assert res["current"]["start"] == "2015-03-01" and "DEF 14A" in res["current"]["source"]
    ceo.assess(cik, "ZZS")
    assert calls.count(cik) == 1  # cached
    res2 = ceo.assess(990041, "ZZT")  # nothing in the filings either
    assert not res2["current"].get("start")
    cov = ceo.coverage([cik, 990041, 990042])
    assert cov["no_start"] == 1 and cov["not_tried"] == 1 and cov["graded"] + cov["other"] + cov["new"] == 1
    assert "no start date" in ceo.coverage_text(cov)
    with db.tx() as con:
        con.execute("DELETE FROM ceo_grades WHERE cik IN (990040, 990041)")
        con.execute("DELETE FROM ceo_cache WHERE key LIKE 'secstart:99004%'")


def test_the_table_follows_the_map_zoom_and_legend():
    rows = [{"ticker": "A", "quality": 85.0, "score": 80.0, "passes": True, "v_mos": 0.4, "v_buy": True},
            {"ticker": "B", "quality": 60.0, "score": 75.0, "passes": False, "v_mos": 0.1, "v_buy": False},
            {"ticker": "C", "quality": 40.0, "score": 30.0, "passes": False, "v_mos": -0.6, "v_buy": False},
            {"ticker": "D", "quality": None, "score": 50.0, "passes": False},  # not on the map
            {"ticker": "E", "quality": 72.0, "score": 66.0, "passes": True, "v_mos": -0.2, "v_buy": False}]
    assert [wp.band(r) for r in rows] == ["buy", "below", "over", "none", "near"]
    assert wp.norm_zoom([20, 100], [0, 100]) is None  # the whole map
    z = wp.norm_zoom([55.2, 100], [60, 100])
    assert z == {"x": [55.2, 100.0], "y": [60.0, 100.0]}
    assert [r["ticker"] for r in wp.map_view(rows, None, set())] == ["A", "B", "C", "D", "E"]
    assert [r["ticker"] for r in wp.map_view(rows, z, set())] == ["A", "B", "E"]
    assert [r["ticker"] for r in wp.map_view(rows, z, {"buy"})] == ["B", "E"]
    assert [r["ticker"] for r in wp.map_view(rows, None, {"over", "near"})] == ["A", "B"]
    lab = wp.legend_counts(rows, rows, z)
    assert lab["Buy zone"] == "Buy zone (1)" and lab["Over 25% above value"] == "Over 25% above value (0 of 1)"
    for r in rows:
        r.update(name=r["ticker"], streak70=0)
    opt = wp.map_options(rows, {"A", "B"}, 70, z, {"over"}, lab)
    assert opt["dataZoom"][0]["startValue"] == 55.2 and opt["dataZoom"][1]["startValue"] == 60.0
    assert opt["legend"]["selected"]["Over 25% above value"] is False and "(0 of 1)" in opt["legend"][":formatter"]
    assert opt["legend"]["data"] == list(wp.BAND_NAME.values())  # best first
    by = {x["name"]: x for x in opt["series"]}
    assert by["Buy zone"]["itemStyle"]["color"] == wp.BAND_COL["buy"] and [d["name"] for d in by["Buy zone"]["data"]] == ["A"]
    assert opt["series"][-2]["name"] == "Buy zone"  # drawn on top
    assert "markLine" in by["lines"] and "lines" not in opt["legend"]["data"]  # quadrant lines never hidden
