"""Build OracleEdge-app.zip (what Install-OracleEdge.bat installs) from this source folder.

Usage, from the oracle-edge folder:
    python tools/build_zip.py [output folder]

uv.exe is not stored in git (32 MB). Put it next to Start.bat first: download the Windows x64 build
from https://github.com/astral-sh/uv/releases (uv-x86_64-pc-windows-msvc.zip) and copy uv.exe out of it.
Then copy the zip and dist/Install-OracleEdge.bat into the same folder (any folder; a cloud-synced one is handy) and
double-click the installer.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCLUDE = ["README.html", "README.md", "LICENSE", "Start.bat", "start.sh", "Run-Daily.bat", ".python-version",
           ".gitignore", "pyproject.toml", "uv.lock", "uv.exe", "oracle_edge", "rules", "tests", "tools"]
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache"}


def build(out_dir: Path) -> Path:
    if not (ROOT / "uv.exe").exists():
        sys.exit("uv.exe is missing next to Start.bat. See the note at the top of tools/build_zip.py.")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "OracleEdge-app.zip"
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in INCLUDE:
            p = ROOT / name
            if not p.exists():
                continue
            files = [p] if p.is_file() else [f for f in p.rglob("*")
                                             if f.is_file() and not (SKIP_DIRS & set(f.relative_to(ROOT).parts))]
            for f in files:
                z.write(f, Path("OracleEdge") / f.relative_to(ROOT))
                n += 1
    print(f"Wrote {out} ({n} files, {out.stat().st_size / 1e6:.1f} MB)")
    return out


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist")
