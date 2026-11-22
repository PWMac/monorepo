"""Today page: score movers."""

import json
from datetime import date, timedelta

from oracle_edge import db, movers, rules


def test_movers_compare_this_years_score_with_last_years():
    rules.seed_profiles()
    p = rules.default_profile()
    pid = p.snap[0]
    today = date.today()
    d = lambda k: (today - timedelta(days=k)).isoformat()  # noqa: E731
    pil = lambda m, g, s, a: json.dumps({"moat": m, "growth": g, "strength": s, "allocation": a})  # noqa: E731
    cases = [(9601, "ZMUP", 3, 60, 72, pil(50, 40, 50, 50), pil(50, 60, 50, 50)),   # up 12, growth
             (9602, "ZMDN", 2, 70, 55, pil(70, 50, 60, 50), pil(50, 50, 60, 50)),   # down 15, moat
             (9603, "ZMOLD", 20, 40, 80, pil(1, 1, 1, 1), pil(9, 9, 9, 9))]         # filed weeks ago: not this week
    for cik, t, ago, s0, s1, p0, p1 in cases:
        db.get().execute("INSERT OR REPLACE INTO securities(cik, ticker, name, active) VALUES(?,?,?,1)", (cik, t, t))
        asof = d(ago + 40)
        db.get().execute("INSERT OR REPLACE INTO wonder(cik,profile_id,cohort,as_of,filed,score,rank,n,pillars_json,"
                         "feats_json) VALUES(?,?,?,?,?,?,?,?,?,?)", (cik, pid, "now", asof, d(ago), s1, 1, 10, p1, "{}"))
        db.get().execute("INSERT OR REPLACE INTO wonder(cik,profile_id,cohort,as_of,filed,score,rank,n,pillars_json,"
                         "feats_json) VALUES(?,?,?,?,?,?,?,?,?,?)", (cik, pid, "FY_new", asof, d(ago), s1, 1, 10, p1, "{}"))
        db.get().execute("INSERT OR REPLACE INTO wonder(cik,profile_id,cohort,as_of,filed,score,rank,n,pillars_json,"
                         "feats_json) VALUES(?,?,?,?,?,?,?,?,?,?)", (cik, pid, "FY_old", d(ago + 400), d(ago + 360), s0,
                                                                      1, 10, p0, "{}"))
    m = movers.movers(p, days=7, end=today.isoformat())
    assert [x["ticker"] for x in m["up"]] == ["ZMUP"] and m["up"][0]["change"] == 12
    assert m["up"][0]["why"] == "growth +20"
    assert [x["ticker"] for x in m["down"]] == ["ZMDN"] and m["down"][0]["why"] == "moat -20"
    m = movers.movers(p, days=30, end=today.isoformat())
    assert "ZMOLD" in [x["ticker"] for x in m["up"]]
