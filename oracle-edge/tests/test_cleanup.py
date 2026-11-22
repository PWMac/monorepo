"""Database cleanup (0.19): tests keep their last day; the live history keeps every day."""

from oracle_edge import db, pipeline


class _J:
    def update(self, *a, **k):
        pass


def _run(rid, kind):
    con = db.get()
    con.execute("INSERT INTO runs(id,kind,name,start_date) VALUES(?,?,?,?)", (rid, kind, kind, "2026-01-01"))
    for d in ("2026-01-02", "2026-01-09", "2026-01-16"):
        con.execute("INSERT INTO evaluations(run_id,date,cik,list) VALUES(?,?,?,?)", (rid, d, 1, "buy"))
        con.execute("INSERT INTO badges(run_id,portfolio_id,date,cik,badge,reason) VALUES(?,?,?,?,?,?)",
                    (rid, 1, d, 1, "HOLD", ""))


def test_cleanup_keeps_live_history_and_each_tests_last_day():
    _run(9001, "live")
    _run(9002, "backtest")
    db.get().execute("INSERT INTO evaluations(run_id,date,cik,list) VALUES(9999,'2026-01-02',1,'buy')")  # deleted run
    assert pipeline.db_size()["removable_rows"] >= 3
    msg = pipeline.db_cleanup_job(_J())
    assert "rows removed" in msg
    assert db.scalar("SELECT COUNT(*) FROM evaluations WHERE run_id=9001") == 3
    assert [r["date"] for r in db.rows("SELECT date FROM evaluations WHERE run_id=9002")] == ["2026-01-16"]
    assert db.scalar("SELECT COUNT(*) FROM badges WHERE run_id=9002") == 1
    assert db.scalar("SELECT COUNT(*) FROM evaluations WHERE run_id=9999") == 0
