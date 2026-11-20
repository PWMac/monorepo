"""Windows Task Scheduler registration for the daily after-close run.

The task runs Run-Daily.bat on weekdays at the local time equal to the configured New York time
during daylight-saving months (the earliest it could be); Run-Daily then waits until the actual
New York run time, so it stays correct all year, including places without daylight saving time.
"""

from __future__ import annotations

import os
import subprocess
from datetime import date, datetime

from . import db, paths
from .tradecal import ET, run_time_et

TASK_NAME = "Oracle Edge Daily"


def earliest_local_time() -> str:
    t = run_time_et()
    cands = []
    for month in (1, 7):  # winter and summer
        d = date(date.today().year, month, 15)
        cands.append(datetime.combine(d, t, ET).astimezone().time())
    return min(cands).strftime("%H:%M")


def register() -> str:
    if os.name != "nt":
        raise RuntimeError("Scheduling is only available on Windows.")
    bat = paths.APP_DIR / "Run-Daily.bat"
    vbs = paths.APP_DIR / "tools" / "run_hidden.vbs"
    st = earliest_local_time()
    cmd = ["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI",
           "/ST", st, "/TR", f'wscript.exe "{vbs}" "{bat}"', "/RL", "LIMITED"]
    r = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW)
    if r.returncode != 0:
        db.set_cfg("scheduler", {"registered": False, "error": (r.stderr or r.stdout).strip(), "at": db.now()})
        raise RuntimeError("Windows refused to create the task: " + (r.stderr or r.stdout).strip())
    if not exists():
        db.set_cfg("scheduler", {"registered": False, "error": "created but not found afterwards", "at": db.now()})
        raise RuntimeError("Windows reported success, but the task can't be found. Try again, or create it in "
                           "Task Scheduler by hand (see the Guide, section 16).")
    db.set_cfg("scheduler", {"registered": True, "local_start": st, "path": str(bat), "at": db.now()})
    return (f"Task '{TASK_NAME}' created: weekdays at {st} your time (it waits until "
            f"{run_time_et().strftime('%H:%M')} New York). Find it in Task Scheduler → Task Scheduler Library.")


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def exists() -> bool:
    if os.name != "nt":
        return False
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True, text=True,
                       creationflags=_NO_WINDOW)
    return r.returncode == 0


def next_run() -> str | None:
    if os.name != "nt":
        return None
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"], capture_output=True, text=True,
                       creationflags=_NO_WINDOW)
    for line in r.stdout.splitlines():
        if line.lower().startswith("next run time"):
            return line.split(":", 1)[1].strip()
    return None


def open_task_scheduler() -> None:
    if os.name == "nt":
        os.startfile("taskschd.msc")  # noqa: S606


def unregister() -> None:
    if os.name == "nt":
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], capture_output=True, creationflags=_NO_WINDOW)
    db.set_cfg("scheduler", {"registered": False})


def status() -> dict:
    """What Windows actually has, not just what the app last tried."""
    s = dict(db.cfg("scheduler", {}) or {})
    s["exists"] = exists()
    s["next_run"] = next_run() if s["exists"] else None
    s["moved"] = bool(s["exists"] and s.get("path") and s["path"] != str(paths.APP_DIR / "Run-Daily.bat"))
    return s
