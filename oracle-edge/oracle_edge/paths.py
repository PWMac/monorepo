"""Filesystem locations. Everything lives inside the app folder (portable)."""

from __future__ import annotations

import os
from pathlib import Path

# App root = folder that contains Start.bat (one level above this package)
APP_DIR = Path(os.environ.get("ORACLE_EDGE_HOME", Path(__file__).resolve().parent.parent))
DATA_DIR = Path(os.environ.get("ORACLE_EDGE_DATA", APP_DIR / "data"))
CACHE_DIR = DATA_DIR / "cache"
LOG_DIR = Path(os.environ.get("ORACLE_EDGE_LOGS", APP_DIR / "logs"))
EXPORT_DIR = APP_DIR / "exports"
RULES_DIR = APP_DIR / "rules"
PROFILES_DIR = APP_DIR / "profiles"
DB_PATH = DATA_DIR / "oracle_edge.db"
# Daily backups: the folder chosen in Settings; until then this default (ideally a cloud-synced folder)
DEFAULT_BACKUP_DIR = os.environ.get("ORACLE_EDGE_BACKUP_DIR") or str(Path.home() / "Documents" / "Oracle Edge backups")


def ensure_dirs() -> None:
    for d in (DATA_DIR, CACHE_DIR, LOG_DIR, EXPORT_DIR, PROFILES_DIR):
        d.mkdir(parents=True, exist_ok=True)
