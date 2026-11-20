"""Job watchdog (heartbeat, live-process check, revival), FRED give-up, resuming a stopped first-time load."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pandas as pd

from oracle_edge import db, jobs, pipeline
from oracle_edge.sources import fred


def _job(kind="initial_load", status="running", minutes_ago=30, pid=None, progress=0.5):
    hb = (datetime.now() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    return db.get().execute(
        "INSERT INTO jobs(kind,args,status,started_at,heartbeat,progress,pid) VALUES(?,?,?,?,?,?,?)",
        (kind, "{}", status, hb, hb, progress, pid)).lastrowid


def test_stale_job_with_dead_process_is_failed():
    db.get().execute("DELETE FROM jobs")
    jid = _job(pid=999_999_9)
    assert jobs.running() is None
    assert db.one("SELECT status, error FROM jobs WHERE id=?", (jid,))["error"] == jobs.STALE


def test_stale_job_with_live_process_keeps_running(monkeypatch):
    db.get().execute("DELETE FROM jobs")
    jid = _job(pid=12345)
    monkeypatch.setattr(jobs, "pid_alive", lambda pid: True)
    r = jobs.running()
    assert r and r["id"] == jid
    assert db.one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "running"


def test_pid_alive_for_this_process():
    assert jobs.pid_alive(os.getpid())
    assert not jobs.pid_alive(None)


def test_wrongly_failed_job_revives_on_next_update():
    db.get().execute("DELETE FROM jobs")
    jid = _job(status="failed")
    db.get().execute("UPDATE jobs SET error=? WHERE id=?", (jobs.STALE, jid))
    jobs.Job(jid).update(0.6, "Scoring")
    r = db.one("SELECT status, error, step FROM jobs WHERE id=?", (jid,))
    assert r["status"] == "running" and r["error"] is None and r["step"] == "Scoring"
    # a job that failed for a real reason stays failed
    db.get().execute("UPDATE jobs SET status='failed', error='boom' WHERE id=?", (jid,))
    jobs.Job(jid).update(0.7)
    assert db.one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "failed"


def test_fred_gives_up_and_falls_back_for_vix(monkeypatch):
    calls = []

    def down(series_id, start="1990-01-01"):
        calls.append(series_id)
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(fred, "fetch_series", down)
    monkeypatch.setattr(fred, "vix_from_yahoo",
                        lambda start="1990-01-01": pd.DataFrame({"date": ["2026-11-19"], "value": [24.5]}))
    seen = []
    out = fred.refresh_all(seen.append)
    assert len(calls) == fred.GIVE_UP_AFTER  # stops asking a server that isn't answering
    assert out["VIXCLS"] == 1
    assert db.scalar("SELECT value FROM macro WHERE series='VIXCLS' AND date='2026-11-19'") == 24.5
    assert seen and seen[-1] == 1.0


def test_resumable_initial_load():
    db.get().execute("DELETE FROM jobs")
    jid = _job(status="failed", progress=0.50)
    assert pipeline.resumable_initial_load()["id"] == jid
    db.get().execute("UPDATE jobs SET progress=0.2 WHERE id=?", (jid,))
    assert pipeline.resumable_initial_load() is None  # stopped before the SEC read finished: start over
    db.get().execute("UPDATE jobs SET progress=0.6 WHERE id=?", (jid,))
    _job(kind="daily", status="done")
    db.get().execute("UPDATE jobs SET status='done' WHERE kind='daily'")
    assert pipeline.resumable_initial_load() is None  # a later successful run replaced it
