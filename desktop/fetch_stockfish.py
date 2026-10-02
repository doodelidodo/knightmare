"""Holt die offiziellen Stockfish-Binaries fuer den Desktop-Build.

    python desktop/fetch_stockfish.py            # fuer das laufende System
    python desktop/fetch_stockfish.py --target windows

Legt sie unter `stockfish/` im Repo ab (nicht eingecheckt), zusammen mit der
Lizenz (GPLv3) und den Autoren - beides muss mit der App ausgeliefert werden.
"""

from __future__ import annotations

import argparse
import io
import platform
import shutil
import stat
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "sf_17.1"
BASE = f"https://github.com/official-stockfish/Stockfish/releases/download/{VERSION}/"

# Ziel -> [(Archiv, Name in der App)]
ASSETS = {
    "windows": [
        ("stockfish-windows-x86-64-avx2.zip", "stockfish-avx2.exe"),
        ("stockfish-windows-x86-64-sse41-popcnt.zip", "stockfish-sse41.exe"),
    ],
    "macos-arm64": [("stockfish-macos-m1-apple-silicon.tar", "stockfish")],
    "macos-x86_64": [("stockfish-macos-x86-64-avx2.tar", "stockfish")],
    "linux": [("stockfish-ubuntu-x86-64-avx2.tar", "stockfish")],
}
KEEP = ("Copying.txt", "AUTHORS")

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "stockfish"


def current_target() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos-arm64" if platform.machine() == "arm64" else "macos-x86_64"
    return "linux"


def members(blob: bytes, name: str) -> dict[str, bytes]:
    """Alle Dateien direkt unter stockfish/ (ohne src/, wiki/ ...)."""
    files: dict[str, bytes] = {}
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            for info in archive.infolist():
                parts = info.filename.split("/")
                if len(parts) == 2 and parts[1]:
                    files[parts[1]] = archive.read(info)
    else:
        with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
            for info in archive.getmembers():
                parts = info.name.split("/")
                if info.isfile() and len(parts) == 2:
                    files[parts[1]] = archive.extractfile(info).read()  # type: ignore[union-attr]
    return files


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=sorted(ASSETS), default=current_target())
    args = parser.parse_args()

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    for asset, target_name in ASSETS[args.target]:
        print(f"Lade {asset} ...", flush=True)
        with urllib.request.urlopen(BASE + asset, timeout=300) as response:
            blob = response.read()
        files = members(blob, asset)
        binary = next(
            (data for fname, data in files.items() if fname.startswith("stockfish-")), None
        )
        if binary is None:
            print(f"Kein Binary in {asset}", file=sys.stderr)
            return 1
        path = OUT / target_name
        path.write_bytes(binary)
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        for keep in KEEP:
            if keep in files and not (OUT / keep).exists():
                (OUT / keep).write_bytes(files[keep])

    (OUT / "SOURCE.txt").write_text(
        "Stockfish is free software under the GNU General Public License v3 (see Copying.txt).\n"
        "Knightmare ships the unmodified official binaries and talks to them over UCI.\n"
        f"Version: {VERSION}\n"
        "Source code: https://github.com/official-stockfish/Stockfish/releases/tag/"
        f"{VERSION}\n",
        encoding="utf-8",
    )
    print("Fertig:", ", ".join(sorted(p.name for p in OUT.iterdir())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
