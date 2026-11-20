"""NYSE trading calendar helpers (holidays and early closes via exchange_calendars)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from . import db

ET = ZoneInfo("America/New_York")


@lru_cache(maxsize=1)
def _cal():
    try:
        import exchange_calendars as xcals

        return xcals.get_calendar("XNYS")
    except Exception:
        return None


def is_session(d: date) -> bool:
    cal = _cal()
    if cal is None:
        return d.weekday() < 5
    try:
        return bool(cal.is_session(d.isoformat()))
    except Exception:
        return d.weekday() < 5


def run_time_et() -> time:
    hh, mm = db.cfg("run_time_et", "18:00").split(":")
    return time(int(hh), int(mm))


def completed_session(now: datetime | None = None) -> str:
    """Latest session whose data should be final (today if past the run time in New York)."""
    now = (now or datetime.now(ET)).astimezone(ET)
    d = now.date()
    if now.time() < run_time_et():
        d -= timedelta(days=1)
    for _ in range(10):
        if is_session(d):
            return d.isoformat()
        d -= timedelta(days=1)
    return d.isoformat()


def sessions_between(a: str, b: str) -> list[str]:
    d, end, out = date.fromisoformat(a), date.fromisoformat(b), []
    while d <= end:
        if is_session(d):
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def local_run_time() -> str:
    """The scheduled run time shown in the user's local time zone (today's date)."""
    t = run_time_et()
    et_dt = datetime.combine(date.today(), t, ET)
    return et_dt.astimezone().strftime("%I:%M %p").lstrip("0")
