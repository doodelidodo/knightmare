"""Baut die Desktop-App mit PyInstaller.

    python desktop/fetch_stockfish.py
    python desktop/build.py

Ergebnis in dist/:
  macOS:   Knightmare.app  (und mit --dmg zusaetzlich Knightmare-macOS.dmg)
  Windows: Knightmare\\Knightmare.exe  (Installer baut desktop/installer.iss)
  Linux:   knightmare/knightmare  (nur zum Ausprobieren, nicht verteilt)

Bewusst "onedir" statt einer einzelnen Datei: startet schneller (nichts wird
bei jedem Start entpackt) und loest seltener Virenscanner aus.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT / "desktop"
SEP = os.pathsep  # ";" unter Windows, ":" sonst - so will es --add-data


def version() -> str:
    for line in (ROOT / "app" / "__init__.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"')
    return "0.0.0"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dmg", action="store_true", help="macOS: zusaetzlich ein DMG bauen")
    args = parser.parse_args()

    stockfish = ROOT / "stockfish"
    if not any(stockfish.glob("stockfish*")):
        print("Erst desktop/fetch_stockfish.py ausfuehren.", file=sys.stderr)
        return 1

    name = "Knightmare" if sys.platform in ("darwin", "win32") else "knightmare"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir",
        "--name", name,
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        "--paths", str(ROOT),
        "--add-data", f"{ROOT / 'app' / 'web'}{SEP}app/web",
        "--add-data", f"{stockfish}{SEP}stockfish",
        "--add-data", f"{DESKTOP / 'icons' / 'icon.png'}{SEP}desktop/icons",
        "--add-data", f"{ROOT / 'LICENSE'}{SEP}.",
        # Die App laedt diese Module ueber Namen in Strings bzw. erst zur
        # Laufzeit - PyInstaller findet sie sonst nicht.
        "--collect-submodules", "uvicorn",
        "--hidden-import", "app.main",
        "--hidden-import", "app.selftest",
        # Postgres gibt es nur im Container.
        "--exclude-module", "psycopg",
        "--exclude-module", "psycopg_binary",
        # Aus uvicorn[standard] - die Desktop-App nutzt asyncio und h11.
        "--exclude-module", "uvloop",
        "--exclude-module", "httptools",
        "--exclude-module", "watchfiles",
        "--exclude-module", "websockets",
        "--exclude-module", "tkinter.test",
    ]
    if sys.platform == "darwin":
        cmd += [
            "--windowed",
            "--icon", str(DESKTOP / "icons" / "icon.icns"),
            "--osx-bundle-identifier", "ch.abetterdodo.knightmare",
        ]
    elif sys.platform == "win32":
        cmd += ["--windowed", "--icon", str(DESKTOP / "icons" / "icon.ico")]
    cmd.append(str(DESKTOP / "launch.py"))

    print(" ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)

    if sys.platform == "darwin":
        app = ROOT / "dist" / "Knightmare.app"
        plist = app / "Contents" / "Info.plist"
        # Version und Mindestsystem ins Info.plist; ohne Version zeigt der
        # Finder "0.0.0".
        for key, value in (
            ("CFBundleShortVersionString", version()),
            ("CFBundleVersion", version()),
            ("LSMinimumSystemVersion", "11.0"),
            ("NSHighResolutionCapable", "true"),
        ):
            kind = "-bool" if value == "true" else "-string"
            subprocess.run(["defaults", "write", str(plist), key, kind, value], check=True)
        subprocess.run(["plutil", "-convert", "xml1", str(plist)], check=True)
        # Nach dem Aendern des Info.plist neu (ad hoc) signieren - Apple
        # Silicon startet sonst gar nichts Unsigniertes.
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
        if args.dmg:
            staging = ROOT / "build" / "dmg"
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            shutil.copytree(app, staging / "Knightmare.app", symlinks=True)
            os.symlink("/Applications", staging / "Applications")
            dmg = ROOT / "dist" / "Knightmare-macOS.dmg"
            dmg.unlink(missing_ok=True)
            subprocess.run(
                ["hdiutil", "create", "-volname", "Knightmare", "-srcfolder", str(staging),
                 "-ov", "-format", "UDZO", str(dmg)],
                check=True,
            )
            print("DMG:", dmg)
    print("Fertig:", ROOT / "dist")
    return 0


if __name__ == "__main__":
    sys.exit(main())
