"""Daily database backup to a folder (best: a cloud-synced folder such as pCloud, OneDrive or Dropbox), with restore.

Uses SQLite's online backup API (consistent copy while the app is open), checks integrity,
zips it, writes to a temporary name on the target and renames when complete so the cloud
sync never uploads a half-written file. Retention: 14 daily, 8 weekly, 12 monthly.
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


def retention() -> dict:
    return db.cfg("backup_retention", {"daily": 14, "weekly": 8, "monthly": 12})


def run_backup(target: Path | None = None) -> str:
    target = Path(target or backup_dir())
    if not target.parent.exists() and not target.exists():
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
        written = []
        names = {
            "daily": f"oracle_edge_{today.isoformat()}.db.zip",
            "weekly": f"oracle_edge_{today.isocalendar()[0]}-W{today.isocalendar()[1]:02d}.db.zip",
            "monthly": f"oracle_edge_{today.strftime('%Y-%m')}.db.zip",
        }
        for kind, name in names.items():
            folder = target / kind
            folder.mkdir(exist_ok=True)
            tmp = folder / (name + ".part")
            shutil.copyfile(zpath, tmp)
            tmp.replace(folder / name)
            written.append(str(folder / name))
    prune(target)
    db.set_cfg("last_backup", {"at": db.now(), "path": written[0]})
    db.log("backup", "run", True, written[0])
    return written[0]


def prune(target: Path) -> None:
    keep = retention()
    for kind, n in keep.items():
        folder = target / kind
        if not folder.exists():
            continue
        files = sorted(folder.glob("oracle_edge_*.db.zip"))
        for f in files[:-n] if n > 0 else []:
            try:
                f.unlink()
            except OSError:
                pass


def list_backups() -> list[dict]:
    out = []
    t = backup_dir()
    for kind in ("daily", "weekly", "monthly"):
        for f in sorted((t / kind).glob("oracle_edge_*.db.zip"), reverse=True) if (t / kind).exists() else []:
            out.append({"kind": kind, "name": f.name, "path": str(f), "size_mb": round(f.stat().st_size / 1e6, 1),
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
