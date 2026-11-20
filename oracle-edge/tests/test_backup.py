"""Backup rotation (grandfather-father-son): what is kept and what is pruned."""

from __future__ import annotations

from datetime import date, timedelta

from oracle_edge import backup, db

STD = {"daily": 7, "weekly": 4, "monthly": 12, "yearly": 0}


def _days(start: date, n: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def test_keep_set_two_years_of_daily_backups():
    days = _days(date(2024, 1, 1), 731)  # 2024-01-01 .. 2025-12-31
    kept = backup.keep_set(days, STD)
    last = days[-1]
    assert set(days[-7:]) <= kept                                   # last 7 days
    assert date(2024, 12, 31) in kept and date(2025, 12, 31) in kept  # newest of each year
    assert date(2025, 1, 31) in kept                                 # newest of a month within 12 months
    assert date(2024, 6, 30) not in kept                             # month older than 12 months, not a year-end
    assert date(2024, 1, 1) not in kept
    assert all(d <= last for d in kept)
    # a year of rotation stays small: ~7 daily + 4 weekly + 12 monthly + 1 per year, with overlaps
    assert len(kept) <= 7 + 4 + 12 + 2


def test_yearly_limit_and_weekly_off():
    days = _days(date(2021, 1, 1), 5 * 365)
    kept = backup.keep_set(days, {"daily": 3, "weekly": 0, "monthly": 0, "yearly": 2})
    years = {d.year for d in kept if d not in days[-3:]}
    assert len(years) <= 2


def test_prune_removes_unkept_and_old_subfolders(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "retention", lambda: {"daily": 2, "weekly": 0, "monthly": 0, "yearly": 0})
    for d in _days(date(2026, 11, 10), 5):
        (tmp_path / f"oracle_edge_{d.isoformat()}.db.zip").write_bytes(b"x")
    # old layout from earlier versions: same day stored three ways
    for kind, name in (("daily", "oracle_edge_2026-11-14.db.zip"), ("weekly", "oracle_edge_2026-W46.db.zip"),
                       ("monthly", "oracle_edge_2026-11.db.zip")):
        (tmp_path / kind).mkdir()
        (tmp_path / kind / name).write_bytes(b"x")
        import os
        import time

        t = time.mktime((2026, 11, 14, 12, 0, 0, 0, 0, -1))  # written on Nov 14, like the dated copy
        os.utime(tmp_path / kind / name, (t, t))
    backup.prune(tmp_path)
    left = sorted(p.name for p in tmp_path.rglob("*.zip"))
    # last 2 days, plus the newest of 2026 (yearly 0 = keep all years) which is also the newest day
    assert "oracle_edge_2026-11-14.db.zip" in left and "oracle_edge_2026-11-13.db.zip" in left
    assert "oracle_edge_2026-11-10.db.zip" not in left
    assert not (tmp_path / "daily").exists()                      # duplicate of a kept day removed, folder gone


def test_retention_upgrades_old_settings():
    db.get()
    db.set_cfg("backup_retention", {"daily": 14, "weekly": 8, "monthly": 12})
    assert backup.retention() == STD
    db.set_cfg("backup_retention", {"daily": 10, "weekly": 4, "monthly": 6, "yearly": 3})
    assert backup.retention()["yearly"] == 3
