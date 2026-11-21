"""Buy Zone entries: the 30-day re-entry rule, the run-start flag and the S&P 500's same-year return."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from oracle_edge import db, pipeline, rules
from oracle_edge.signals import PortfolioSim
from oracle_edge.ui import data
from tests.test_engine import FakeBook, _ev


@pytest.fixture(scope="module", autouse=True)
def _db():
    db.get()
    rules.seed_profiles()


LONG = [(date(2025, 1, 2) + timedelta(days=i)).isoformat() for i in range(140)]


def _stint_sim(run_id):
    profile = rules.default_profile()
    db.get().execute("INSERT INTO runs(id,kind,name,profile_id,start_date) VALUES(?,?,?,?,?)",
                     (run_id, "backtest", "t", profile.id, LONG[0]))
    db.get().execute("INSERT INTO portfolios(run_id,name,kind,max_positions,position_cap,trim_to,last_nav)"
                     " VALUES(?,?,?,?,?,?,1.0)", (run_id, "m", "model", None, 0.40, 0.30))
    pid = db.scalar("SELECT id FROM portfolios WHERE run_id=?", (run_id,))
    pb = FakeBook({t: [(d, 100.0 + i) for i, d in enumerate(LONG)] for t in ("AAA", "BBB", "SPY")})
    return PortfolioSim(run_id, pid, profile, pb, {1: "AAA", 2: "BBB"})


def test_reentry_within_30_days_continues_the_entry():
    sim = _stint_sim(1901)
    a, b = _ev(1, "AAA", rank=3), _ev(2, "BBB", rank=1)
    plan = {i: [a, b] if i < 5 else [b] if i < 20 else [a, b] if i < 25 else [b] if i < 70 else [a, b]
            for i in range(len(LONG))}
    plan[0] = [a]  # BBB enters on day 2, after the run's first day
    for i, d in enumerate(LONG[:75]):
        sim.stints(d, plan[i])
    rows = db.rows("SELECT * FROM stints WHERE run_id=1901 AND cik=1 ORDER BY entered_on")
    assert [r["entered_on"] for r in rows] == [LONG[0], LONG[70]]  # out 15 days = same entry; out 45 = new one
    assert rows[0]["reentries"] == 1 and rows[0]["at_start"] == 1 and rows[1]["at_start"] == 0
    assert rows[0]["entry_rank"] == 3
    other = db.one("SELECT * FROM stints WHERE run_id=1901 AND cik=2")
    assert other["entered_on"] == LONG[1] and other["at_start"] == 0


def test_normalize_merges_old_style_rows():
    con = db.get()
    con.execute("INSERT INTO runs(id,kind,name,profile_id,start_date) VALUES(1902,'live','t',1,'2024-01-02')")
    rows = [(1902, 5, "2024-01-02", "2024-02-01", 10.0, 11.0, "closed", 2),
            (1902, 5, "2024-02-20", "2024-03-01", 12.0, 14.0, "closed", 9),   # 19 days later: merges
            (1902, 5, "2024-06-01", "2024-07-01", 9.0, 9.9, "closed", 20),    # 92 days later: new entry
            (1902, 6, "2024-03-05", None, 50.0, None, "open", 4)]
    con.executemany("INSERT INTO stints(run_id,cik,entered_on,left_on,entry_px,exit_px,status,entry_rank) "
                    "VALUES(?,?,?,?,?,?,?,?)", rows)
    assert pipeline.normalize_stints(1902) == 1
    got = db.rows("SELECT * FROM stints WHERE run_id=1902 AND cik=5 ORDER BY entered_on")
    assert len(got) == 2
    assert got[0]["left_on"] == "2024-03-01" and abs(got[0]["return_pct"] - 0.4) < 1e-9 and got[0]["reentries"] == 1
    assert got[0]["entry_rank"] == 2 and got[0]["at_start"] == 1 and got[1]["at_start"] == 0
    assert pipeline.normalize_stints(1902) == 0  # running it again changes nothing


def test_rank_table_compares_with_the_sp_over_the_same_days():
    con = db.get()
    con.execute("INSERT INTO runs(id,kind,name,profile_id,start_date) VALUES(1903,'live','t',1,'2024-01-02')")
    rows = [(1903, 1, "2024-01-02", 1, 0.50, 0.10, 1),    # run start: left out
            (1903, 2, "2024-03-01", 2, 0.20, 0.10, 0),
            (1903, 3, "2024-03-05", 4, 0.00, 0.05, 0),
            (1903, 4, "2024-03-09", 3, 0.30, 0.00, 0),
            (1903, 5, "2020-03-20", 20, 0.60, 0.70, 0)]   # crash entry: big raw return, behind the S&P
    con.executemany("INSERT INTO stints(run_id,cik,entered_on,entry_rank,fwd_1y,bench_fwd_1y,at_start,status) "
                    "VALUES(?,?,?,?,?,?,?, 'closed')", rows)
    rb = data.rank_buckets(1903)
    top, low, info = rb[0], rb[2], rb[-1]
    assert top["stints"] == 3 and info["skipped_start"] == 1
    assert abs(top["avg_excess_1y"] - (0.10 - 0.05 + 0.30) / 3) < 1e-9 and abs(top["med_excess_1y"] - 0.10) < 1e-9
    assert abs(top["beat"] - 2 / 3) < 1e-9
    assert low["avg_fwd_1y"] > top["avg_fwd_1y"] and low["avg_excess_1y"] < top["avg_excess_1y"]
