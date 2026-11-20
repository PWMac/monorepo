"""Start the Oracle Edge web app (local only)."""

from __future__ import annotations

import logging
import os
import secrets as pysecrets
import socket

from nicegui import app, ui

from . import APP_NAME, __version__, db, jobs, paths, rules
from .ui import pages_admin, pages_main  # noqa: F401  (registers pages)

if (paths.APP_DIR / "README.html").exists():  # the user guide, opened from "Guide" in the menu
    app.add_static_file(local_file=paths.APP_DIR / "README.html", url_path="/guide")

PORT = int(os.environ.get("ORACLE_EDGE_PORT", "8765"))


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _start_watchdog() -> None:
    """If the app stops answering for 20+ s, write what every thread is doing to logs/hang.log."""
    import asyncio
    import faulthandler
    import threading
    import time
    from datetime import datetime

    loop = asyncio.get_running_loop()
    state = {"beat": time.monotonic(), "dumped": 0.0}

    def beat():
        state["beat"] = time.monotonic()

    def watch():
        while True:
            time.sleep(5)
            try:
                loop.call_soon_threadsafe(beat)
            except RuntimeError:  # loop closed: app is shutting down
                return
            stalled = time.monotonic() - state["beat"]
            if stalled > 20 and time.monotonic() - state["dumped"] > 120:
                state["dumped"] = time.monotonic()
                try:
                    with open(paths.LOG_DIR / "hang.log", "a", encoding="utf-8") as f:
                        f.write(f"\n==== {datetime.now():%Y-%m-%d %H:%M:%S} v{__version__}: app busy for "
                                f"{stalled:.0f}s. What each thread was doing:\n")
                        f.flush()
                        faulthandler.dump_traceback(file=f, all_threads=True)
                except Exception:
                    pass

    threading.Thread(target=watch, daemon=True, name="oe-watchdog").start()


@app.middleware("http")
async def _slow_requests(request, call_next):
    import time

    t0 = time.monotonic()
    resp = await call_next(request)
    dt = time.monotonic() - t0
    if dt > 2:
        logging.getLogger("oracle_edge").warning("slow request %.1fs: %s", dt, request.url.path)
    return resp


def startup() -> None:
    paths.ensure_dirs()
    _start_watchdog()
    db.get()
    rules.seed_profiles()
    if not db.cfg("storage_secret"):
        db.set_cfg("storage_secret", pysecrets.token_hex(16))
    # catch up automatically if the PC was off at the scheduled time
    try:
        from .tradecal import completed_session

        last = (db.cfg("last_daily_run") or {}).get("through")
        if db.cfg("initial_load_done") and (not last or last < completed_session()) and not jobs.running():
            if db.cfg("auto_catch_up", True):
                jobs.launch("daily", {})
    except Exception as e:  # never block the UI on this
        logging.getLogger("oracle_edge").warning("catch-up check failed: %s", e)


@app.get("/__oe/version")
def _version_endpoint():
    return {"version": __version__, "pid": os.getpid()}


@app.post("/__oe/shutdown")
def _shutdown_endpoint():
    app.shutdown()
    return {"ok": True}


def _running_version() -> str | None:
    import json as _json
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/__oe/version", timeout=3) as r:
            return _json.loads(r.read().decode()).get("version")
    except Exception:
        return None  # versions before 0.4.1 have no version endpoint


def _stop_running() -> None:
    import time
    import urllib.request

    try:
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{PORT}/__oe/shutdown", method="POST"),
                               timeout=3)
    except Exception:
        pass
    for _ in range(20):
        if _port_free(PORT):
            return
        time.sleep(0.5)
    if os.name == "nt":  # older copy without the shutdown endpoint: stop the Oracle Edge process on our port
        import subprocess

        ps = (f"Get-NetTCPConnection -LocalPort {PORT} -State Listen -ErrorAction SilentlyContinue | ForEach-Object "
              "{ $p = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue; "
              "if ($p -and $p.Path -like '*OracleEdge*') { Stop-Process -Id $p.Id -Force } }")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for _ in range(20):
            if _port_free(PORT):
                return
            time.sleep(0.5)


def main() -> None:
    import sys

    paths.ensure_dirs()
    if sys.stdout is None or sys.stderr is None:  # started with pythonw (no console window)
        f = open(paths.LOG_DIR / "app_console.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or f
        sys.stderr = sys.stderr or f
    logging.basicConfig(filename=paths.LOG_DIR / "oracle_edge.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    paths.ensure_dirs()
    db.get()
    if not _port_free(PORT):
        if _running_version() == __version__:
            # this version is already running: just open the browser to it
            import webbrowser

            webbrowser.open(f"http://127.0.0.1:{PORT}/")
            return
        # an older copy is still running after an update: stop it so the new code is served
        logging.getLogger("oracle_edge").info("stopping an older running copy (%s)", _running_version())
        _stop_running()
    app.on_startup(startup)
    ui.run(title=APP_NAME, host="127.0.0.1", port=PORT, show=os.environ.get("ORACLE_EDGE_NO_BROWSER") != "1",
           reload=False, storage_secret=db.cfg("storage_secret") or "oracle-edge", favicon="📈",
           show_welcome_message=False)


if __name__ in {"__main__", "__mp_main__"}:
    main()
