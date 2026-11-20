"""Background jobs: run in a separate process, report progress through the `jobs` table."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import traceback
from datetime import datetime, timedelta

from . import db, paths


class Job:
    def __init__(self, job_id: int):
        self.id = job_id
        self._last = 0.0

    def update(self, progress: float | None = None, step: str | None = None, message: str | None = None):
        # a job wrongly marked "stopped responding" that is in fact still working comes back to life
        sets, vals = ["heartbeat=?", f"status=CASE WHEN status='failed' AND error={STALE_SQL} THEN 'running' ELSE status END",
                      f"error=CASE WHEN status='failed' AND error={STALE_SQL} THEN NULL ELSE error END"], [db.now()]
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


STALE = "stopped responding"
STALE_SQL = "'stopped responding'"
HEARTBEAT_SECONDS = 30
STALE_AFTER = timedelta(minutes=10)


def pid_alive(pid: int | None) -> bool:
    """True if a process with this id is still running. Never sends a signal (on Windows os.kill would end it)."""
    if not pid:
        return False
    if os.name == "nt":
        import ctypes

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def running() -> dict | None:
    r = db.one("SELECT * FROM jobs WHERE status='running' ORDER BY id DESC LIMIT 1")
    if not r:
        return None
    hb = datetime.fromisoformat(r["heartbeat"] or r["started_at"])
    if datetime.now() - hb > STALE_AFTER:
        # The worker beats every 30 s from its own thread, so silence means the process is gone or frozen.
        # Only give up on it when the process has really ended.
        if pid_alive(r["pid"]) and r["pid"] != os.getpid():
            return dict(r)
        db.get().execute("UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?",
                         (STALE, db.now(), r["id"]))
        return None
    return dict(r)


class _Heartbeat(threading.Thread):
    """Keeps the job's heartbeat fresh while long steps (downloads, slow servers) give no progress."""

    def __init__(self, job_id: int):
        super().__init__(daemon=True, name=f"job-{job_id}-heartbeat")
        self.job_id = job_id
        self.stop = threading.Event()

    def run(self):
        while not self.stop.wait(HEARTBEAT_SECONDS):
            try:
                Job(self.job_id).update()
            except Exception:  # a busy database just means we try again next beat
                pass


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
    beat = _Heartbeat(job_id)
    beat.start()
    try:
        fn = {"initial_load": pipeline.initial_load, "daily": pipeline.daily_run, "backtest": pipeline.run_backtest,
              "rebuild": pipeline.rebuild_profile, "backup": pipeline.backup_job,
              "recalc": pipeline.recalc_backtest, "sweep": pipeline.run_sweep,
              "recalc_all": pipeline.recalc_all, "experiments": pipeline.run_experiments}[r["kind"]]
        msg = fn(job, **args)
        db.get().execute("UPDATE jobs SET status='done', progress=1, finished_at=?, message=? WHERE id=?",
                         (db.now(), msg or "Done", job_id))
    except Exception as e:
        db.get().execute("UPDATE jobs SET status='failed', finished_at=?, error=? WHERE id=?",
                         (db.now(), f"{e}\n{traceback.format_exc()[-3000:]}", job_id))
        raise
    finally:
        beat.stop.set()
