"""Command line entry points.

  python -m oracle_edge.cli job <id>        run a queued background job (used by the UI)
  python -m oracle_edge.cli daily [--wait]  daily after-close run (used by the scheduled task)
  python -m oracle_edge.cli backup          back up the database now
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime

from . import db, jobs, paths
from .tradecal import ET, run_time_et


def _logging():
    paths.ensure_dirs()
    logging.basicConfig(filename=paths.LOG_DIR / "oracle_edge.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")


def main(argv=None) -> int:
    _logging()
    if sys.stdout is None or sys.stderr is None:
        f = open(paths.LOG_DIR / "cli_console.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or f
        sys.stderr = sys.stderr or f
    ap = argparse.ArgumentParser(prog="oracle_edge")
    sub = ap.add_subparsers(dest="cmd", required=True)
    j = sub.add_parser("job")
    j.add_argument("job_id", type=int)
    d = sub.add_parser("daily")
    d.add_argument("--wait", action="store_true", help="wait until the configured New York run time")
    d.add_argument("--force", action="store_true")
    sub.add_parser("backup")
    a = ap.parse_args(argv)
    db.get()
    if a.cmd == "job":
        jobs.execute(a.job_id)
    elif a.cmd == "daily":
        if a.wait:
            while datetime.now(ET).time() < run_time_et():
                time.sleep(60)
        if jobs.running():
            print("Another job is running; skipping.")
            return 0
        jid = jobs.create("daily", {"force": a.force})
        try:
            jobs.execute(jid)
        except Exception as e:
            try:
                from . import emailer

                emailer.send_failure(f"The daily run failed: {e}")
            except Exception:
                pass
            return 1
    elif a.cmd == "backup":
        from . import backup

        print(backup.run_backup())
    return 0


if __name__ == "__main__":
    sys.exit(main())
