"""Daily database backup to a folder (best: a cloud-synced folder such as pCloud, OneDrive or Dropbox), with restore.

Uses SQLite's online backup API (consistent copy while the app is open), checks integrity,
zips it, writes to a temporary name on the target and renames when complete so the cloud
sync never uploads a half-written file.

Retention is the standard grandfather-father-son rotation. Each day's backup is stored once,
as oracle_edge_YYYY-MM-DD.db.zip, and pruning keeps:
  - the last 7 daily backups,
  - the newest backup of each of the last 4 weeks,
  - the newest backup of each of the last 12 months,
  - the newest backup of every year (yearly = 0 keeps them all; a number keeps that many years).
Everything else is deleted. Backups made by older versions (daily/, weekly/, monthly/ subfolders)
are still listed for restore and are pruned by the same rules.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import date, datetime
from pathlib import Path

from . import db, paths


def backup_dir() -> Path:
    return Path(db.cfg("backup_dir", paths.DEFAULT_BACKUP_DIR))


DEFAULT_RETENTION = {"daily": 7, "weekly": 4, "monthly": 12, "yearly": 0}


def retention() -> dict:
    r = db.cfg("backup_retention")
    if not r or "yearly" not in r:  # settings from before the GFS rotation: use the standard one
        return dict(DEFAULT_RETENTION)
    return {**DEFAULT_RETENTION, **r}


def run_backup(target: Path | None = None) -> str:
    target = Path(target or backup_dir())
    if not Path(target.anchor or ".").exists():
        raise RuntimeError(f"Backup folder not reachable: {target} (is the drive or cloud sync running?)")
    target.mkdir(parents=True, exist_ok=True)
    today = date.today()
    with tempfile.TemporaryDirectory() as td:
        snap = Path(td) / "oracle_edge.db"
        src = db.connect()
        dst = sqlite3.connect(snap)
        src.backup(dst)
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        dst.close()
        src.close()
        if ok != "ok":
            raise RuntimeError(f"Integrity check failed: {ok}")
        zpath = Path(td) / "backup.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.write(snap, "oracle_edge.db")
            prof = [dict(r) for r in db.rows("SELECT name, version, locked, active, body_json, notes FROM profiles")]
            z.writestr("profiles.json", json.dumps(prof, indent=1))
            z.writestr("settings.json", json.dumps({r["key"]: json.loads(r["value_json"]) for r in db.rows(
                "SELECT key, value_json FROM config")}, indent=1))
        name = f"oracle_edge_{today.isoformat()}.db.zip"
        tmp = target / (name + ".part")
        shutil.copyfile(zpath, tmp)
        tmp.replace(target / name)
        written = [str(target / name)]
    prune(target)
    db.set_cfg("last_backup", {"at": db.now(), "path": written[0]})
    db.log("backup", "run", True, written[0])
    return written[0]


def _backup_files(target: Path) -> list[Path]:
    """Every backup in the folder: the flat files plus the old daily/weekly/monthly subfolders."""
    if not target.exists():
        return []
    files = list(target.glob("oracle_edge_*.db.zip"))
    for kind in ("daily", "weekly", "monthly"):
        if (target / kind).exists():
            files += list((target / kind).glob("oracle_edge_*.db.zip"))
    return files


def _day(f: Path) -> date:
    """The day a backup was taken: from a dated name, otherwise from the file's modified time."""
    stem = f.name[len("oracle_edge_"):-len(".db.zip")]
    if len(stem) == 10 and stem[4] == "-" and stem[7] == "-":
        try:
            return date.fromisoformat(stem)
        except ValueError:
            pass
    return datetime.fromtimestamp(f.stat().st_mtime).date()


def keep_set(days: list[date], keep: dict) -> set[date]:
    """Which backup days the rotation keeps (newest backup per period, newest periods first)."""
    uniq = sorted(set(days), reverse=True)
    kept: set[date] = set(uniq[:max(int(keep.get("daily", 7)), 0)])
    for kind, key in (("weekly", lambda d: d.isocalendar()[:2]), ("monthly", lambda d: (d.year, d.month)),
                      ("yearly", lambda d: d.year)):
        n = int(keep.get(kind, 0))
        if kind != "yearly" and n <= 0:
            continue
        seen: dict = {}
        for d in uniq:  # newest first: the first one seen is the newest of its period
            seen.setdefault(key(d), d)
        newest = list(seen.values())
        kept.update(newest if (kind == "yearly" and n == 0) else newest[:n])
    return kept


def prune(target: Path) -> list[str]:
    files = _backup_files(target)
    if not files:
        return []
    by_day: dict[date, list[Path]] = {}
    for f in files:
        by_day.setdefault(_day(f), []).append(f)
    kept_days = keep_set(list(by_day), retention())
    removed = []
    for d, fs in by_day.items():
        fs.sort(key=lambda f: (f.parent != target, f.name))  # prefer the flat, dated file for a kept day
        for i, f in enumerate(fs):
            if d in kept_days and i == 0:
                continue
            try:
                f.unlink()
                removed.append(f.name)
            except OSError:
                pass
    for kind in ("daily", "weekly", "monthly"):  # remove old subfolders once empty
        try:
            (target / kind).rmdir()
        except OSError:
            pass
    return removed


def list_backups() -> list[dict]:
    out = []
    t = backup_dir()
    for f in sorted(_backup_files(t), key=lambda f: (_day(f), f.name), reverse=True):
        out.append({"kind": "backup" if f.parent == t else f.parent.name, "name": f.name, "path": str(f),
                    "day": _day(f).isoformat(), "size_mb": round(f.stat().st_size / 1e6, 1),
                    "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")})
    return out


def restore(zip_path: str) -> str:
    """Swap in a backup. The current DB is kept as a safety copy next to it."""
    paths.ensure_dirs()
    safety = paths.DATA_DIR / f"oracle_edge_before_restore_{datetime.now():%Y%m%d_%H%M%S}.db"
    if paths.DB_PATH.exists():
        src = db.connect()
        dst = sqlite3.connect(safety)
        src.backup(dst)
        dst.close()
        src.close()
    with zipfile.ZipFile(zip_path) as z, tempfile.TemporaryDirectory() as td:
        z.extract("oracle_edge.db", td)
        restored = sqlite3.connect(Path(td) / "oracle_edge.db")
        live = sqlite3.connect(paths.DB_PATH)
        restored.backup(live)
        live.close()
        restored.close()
    return str(safety)
