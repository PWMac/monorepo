"""Background jobs: run in a separate process, report progress through the `jobs` table."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timedelta

from . import db, paths


class Job:
    def __init__(self, job_id: int):
        self.id = job_id
        self._last = 0.0

    def update(self, progress: float | None = None, step: str | None = None, message: str | None = None):
        sets, vals = ["heartbeat=?"], [db.now()]
        if progress is not None:
            sets.append("progress=?")
            vals.append(round(max(0.0, min(1.0, progress)), 4))
        if step is not None:
            sets.append("step=?")
            vals.append(step)
        if message is not None:
            sets.append("message=?")
            vals.append(message)
        db.get().execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", (*vals, self.id))

    def sub(self, lo: float, hi: float, step: str):
        """Progress callback mapped into [lo, hi] of the whole job."""
        self.update(lo, step)

        def cb(frac: float):
            self.update(lo + (hi - lo) * frac)

        return cb


def running() -> dict | None:
    r = db.one("SELECT * FROM jobs WHERE status='running' ORDER BY id DESC LIMIT 1")
    if not r:
        return None
    hb = datetime.fromisoformat(r["heartbeat"] or r["started_at"])
    if datetime.now() - hb > timedelta(minutes=15):
        db.get().execute("UPDATE jobs SET status='failed', error='stopped responding' WHERE id=?", (r["id"],))
        return None
    return dict(r)


def create(kind: str, args: dict | None = None) -> int:
    cur = db.get().execute(
        "INSERT INTO jobs(kind,args,status,started_at,heartbeat,progress) VALUES(?,?, 'queued', ?, ?, 0)",
        (kind, json.dumps(args or {}), db.now(), db.now()),
    )
    return cur.lastrowid


def launch(kind: str, args: dict | None = None) -> int:
    """Start a job in a background process (used by the UI)."""
    if running():
        raise RuntimeError("Another job is already running. Wait for it to finish.")
    jid = create(kind, args)
    log = open(paths.LOG_DIR / f"job_{jid}_{kind}.log", "a", encoding="utf-8")
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    p = subprocess.Popen([sys.executable, "-m", "oracle_edge.cli", "job", str(jid)], cwd=str(paths.APP_DIR),
                         stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
    db.get().execute("UPDATE jobs SET pid=? WHERE id=?", (p.pid, jid))
    return jid


def execute(job_id: int) -> None:
    """Called inside the worker process."""
    from . import pipeline

    r = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    args = json.loads(r["args"] or "{}")
    db.get().execute("UPDATE jobs SET status='running', heartbeat=?, started_at=? WHERE id=?",
                     (db.now(), db.now(), job_id))
    job = Job(job_id)
    try:
        fn = {"initial_load": pipeline.initial_load, "daily": pipeline.daily_run, "backtest": pipeline.run_backtest,
              "rebuild": pipeline.rebuild_profile, "backup": pipeline.backup_job,
              "recalc": pipeline.recalc_backtest, "sweep": pipeline.run_sweep,
              "recalc_all": pipeline.recalc_all}[r["kind"]]
        msg = fn(job, **args)
        db.get().execute("UPDATE jobs SET status='done', progress=1, finished_at=?, message=? WHERE id=?",
                         (db.now(), msg or "Done", job_id))
    except Exception as e:
        db.get().execute("UPDATE jobs SET status='failed', finished_at=?, error=? WHERE id=?",
                         (db.now(), f"{e}\n{traceback.format_exc()[-3000:]}", job_id))
        raise
