"""Desktop-Fassung: Knightmare als App fuer Mac und Windows.

Dieselbe App wie im Container, nur anders gestartet:

* Daten liegen im ueblichen Ordner des Systems
  (macOS: ~/Library/Application Support/Knightmare,
  Windows: %LOCALAPPDATA%\\Knightmare), als SQLite-Datei.
* Stockfish liegt in der App bei. Unter Windows zwei Fassungen (AVX2 und
  eine fuer aeltere Prozessoren); die erste, die antwortet, gewinnt.
* Die Benutzernamen traegt man im Browser ein (`/api/setup`). Sie landen in
  `settings.env` im Datenordner.
* Der Server hoert nur auf 127.0.0.1. Ein kleines Fenster zeigt, dass er
  laeuft, und beendet ihn. Ohne tkinter (oder mit --headless) laeuft er ohne
  Fenster bis Strg+C.
* Ein zweiter Start oeffnet nur den Browser auf der laufenden Instanz -
  zwei Prozesse auf derselben SQLite-Datei waeren keine gute Idee.

Aufruf aus dem Quellcode: `python -m app.desktop` (Optionen: --headless,
--no-browser, --selftest). Gebaut wird mit `desktop/build.py`.
"""

from __future__ import annotations

import argparse
import json
import locale
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Optional

APP_NAME = "Knightmare"
PREFERRED_PORT = 8765

log = logging.getLogger("knightmare.desktop")


# --------------------------------------------------------------------------
# Orte
# --------------------------------------------------------------------------
def data_dir() -> Path:
    override = os.environ.get("KNIGHTMARE_DATA_DIR", "").strip()
    if override:
        base = Path(override)
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    elif os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        base = Path(root) / APP_NAME
    else:
        root = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        base = Path(root) / "knightmare"
    base.mkdir(parents=True, exist_ok=True)
    return base


def bundle_dir() -> Path:
    """Wurzel der gebauten App bzw. des Repos beim Start aus dem Quellcode."""
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        return Path(frozen)
    return Path(__file__).resolve().parent.parent


def engine_candidates() -> list[Path]:
    folder = bundle_dir() / "stockfish"
    if os.name == "nt":
        names = ["stockfish-avx2.exe", "stockfish-sse41.exe", "stockfish.exe"]
    else:
        names = ["stockfish"]
    return [folder / name for name in names if (folder / name).is_file()]


def engine_works(path: Path) -> bool:
    """Startet die Engine kurz und wartet auf `uciok`.

    Faengt den Fall ab, dass ein Prozessor AVX2 nicht kann: die Fassung
    stuerzt dann sofort mit "illegal instruction" ab.
    """
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        result = subprocess.run(
            [str(path)],
            input="uci\nquit\n",
            capture_output=True,
            text=True,
            timeout=15,
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Engine %s startet nicht: %s", path.name, exc)
        return False
    if "uciok" in result.stdout:
        return True
    log.warning("Engine %s antwortet nicht (Code %s)", path.name, result.returncode)
    return False


def pick_engine() -> Optional[str]:
    for candidate in engine_candidates():
        if os.name != "nt":
            try:
                candidate.chmod(candidate.stat().st_mode | 0o111)
            except OSError:
                pass
        if engine_works(candidate):
            log.info("Stockfish: %s", candidate)
            return str(candidate)
    return None


# --------------------------------------------------------------------------
# Laufende Instanz und Port
# --------------------------------------------------------------------------
def _health(port: int, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=timeout
        ) as response:
            return json.load(response).get("service") == "knightmare"
    except Exception:  # noqa: BLE001 - jede Art von "nein" ist hier ein Nein
        return False


def running_port(folder: Path) -> Optional[int]:
    try:
        port = int((folder / "port").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return port if _health(port) else None


def free_port() -> int:
    for port in (PREFERRED_PORT, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return sock.getsockname()[1]
    raise RuntimeError("Kein freier Port gefunden")


# --------------------------------------------------------------------------
# Protokoll
# --------------------------------------------------------------------------
def setup_logging(folder: Path) -> None:
    # Eine Fenster-App unter Windows hat weder stdout noch stderr. Alles, was
    # dorthin schreibt (uvicorn, print im Selbsttest), wuerde sonst abbrechen.
    log_path = folder / "knightmare.log"
    if sys.stdout is None or sys.stderr is None:
        stream = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
        handlers: list[logging.Handler] = [logging.StreamHandler(stream)]
    else:
        handlers = [
            logging.StreamHandler(sys.stderr),
            logging.handlers.RotatingFileHandler(
                log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
            ),
        ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=handlers,
        force=True,
    )


# --------------------------------------------------------------------------
# Umgebung fuer die App
# --------------------------------------------------------------------------
def prepare_environment(folder: Path) -> None:
    """Muss vor dem Import von app.main laufen - db.py liest beim Import."""
    from .config import SETTINGS_FILE_ENV, apply_settings_file

    settings_path = folder / "settings.env"
    os.environ[SETTINGS_FILE_ENV] = str(settings_path)
    apply_settings_file(settings_path)

    db_path = (folder / "knightmare.db").as_posix()
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{db_path}")
    # Auf dem eigenen Rechner will man nach dem Einrichten nicht 45 Sekunden
    # auf den ersten Durchgang warten.
    os.environ.setdefault("CHESS_STARTUP_DELAY_SECONDS", "5")
    from . import __version__

    os.environ.setdefault(
        "CHESS_USER_AGENT",
        f"knightmare-desktop/{__version__} (+https://github.com/doodelidodo/knightmare)",
    )
    if not os.environ.get("CHESS_ENGINE_PATH"):
        engine = pick_engine()
        if engine:
            os.environ["CHESS_ENGINE_PATH"] = engine
        else:
            log.warning("Keine mitgelieferte Engine gefunden - suche im System.")


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------
class Server:
    def __init__(self, port: int) -> None:
        import uvicorn

        from .main import app

        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_config=None,
            # Feste, reine Python-Teile: nichts, was PyInstaller ueber einen
            # Modulnamen in einem String erst suchen muesste.
            loop="asyncio",
            http="h11",
            ws="none",
            lifespan="on",
        )
        self.port = port
        self.url = f"http://127.0.0.1:{port}/"
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="uvicorn", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def wait_ready(self, seconds: float = 60.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not self._thread.is_alive():
                return False
            if _health(self.port, timeout=1.0):
                return True
            time.sleep(0.25)
        return False

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


# --------------------------------------------------------------------------
# Fenster
# --------------------------------------------------------------------------
TEXTS = {
    "en": {
        "running": "Knightmare is running",
        "hint": "It works in your browser. Closing this window stops it.",
        "open": "Open in browser",
        "folder": "Data folder",
        "quit": "Quit",
        "failed": "Knightmare could not start. Details are in the log in the data folder.",
    },
    "de": {
        "running": "Knightmare läuft",
        "hint": "Es arbeitet im Browser. Dieses Fenster schliessen beendet es.",
        "open": "Im Browser öffnen",
        "folder": "Datenordner",
        "quit": "Beenden",
        "failed": "Knightmare konnte nicht starten. Details stehen im Protokoll im Datenordner.",
    },
}


def ui_language() -> str:
    candidates = [os.environ.get("LANG", "")]
    try:
        candidates.append(locale.getlocale()[0] or "")
    except ValueError:
        pass
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["defaults", "read", "-g", "AppleLanguages"],
                capture_output=True, text=True, timeout=3,
            ).stdout
            candidates.insert(0, out.replace("(", "").strip().strip('"'))
        except (OSError, subprocess.SubprocessError):
            pass
    if os.name == "nt":
        try:
            import ctypes

            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()  # type: ignore[attr-defined]
            candidates.insert(0, "de" if lang_id & 0x3FF == 0x07 else "en")
        except Exception:  # noqa: BLE001
            pass
    for value in candidates:
        if value.lower().startswith("de"):
            return "de"
        if value:
            return "en"
    return "en"


def open_folder(folder: Path) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        elif os.name == "nt":
            os.startfile(str(folder))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except OSError as exc:
        log.warning("Ordner liess sich nicht oeffnen: %s", exc)


def run_window(server: Server, folder: Path) -> bool:
    """Zeigt das Statusfenster. False, wenn es kein tkinter gibt."""
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        return False
    try:
        root = tk.Tk()
    except tk.TclError:  # kein Bildschirm
        return False

    text = TEXTS[ui_language()]
    root.title(APP_NAME)
    root.resizable(False, False)
    icon = bundle_dir() / "desktop" / "icons" / "icon.png"
    if icon.is_file():
        try:
            root.iconphoto(True, tk.PhotoImage(file=str(icon)))
        except tk.TclError:
            pass

    frame = ttk.Frame(root, padding=18)
    frame.grid()
    ttk.Label(frame, text=text["running"], font=("TkDefaultFont", 14, "bold")).grid(
        column=0, row=0, columnspan=3, sticky="w"
    )
    ttk.Label(frame, text=server.url, foreground="#5b6ef5").grid(
        column=0, row=1, columnspan=3, sticky="w", pady=(4, 2)
    )
    ttk.Label(frame, text=text["hint"], wraplength=340).grid(
        column=0, row=2, columnspan=3, sticky="w", pady=(0, 14)
    )

    def quit_app() -> None:
        root.destroy()

    ttk.Button(frame, text=text["open"], command=lambda: webbrowser.open(server.url)).grid(
        column=0, row=3, padx=(0, 6)
    )
    ttk.Button(frame, text=text["folder"], command=lambda: open_folder(folder)).grid(
        column=1, row=3, padx=(0, 6)
    )
    ttk.Button(frame, text=text["quit"], command=quit_app).grid(column=2, row=3)

    def watch() -> None:
        if not server.alive:
            root.destroy()
            return
        root.after(1000, watch)

    root.protocol("WM_DELETE_WINDOW", quit_app)
    root.after(1000, watch)
    root.lift()
    root.mainloop()
    return True


def show_error(message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, message)
        root.destroy()
    except Exception:  # noqa: BLE001
        print(message, file=sys.stderr)


# --------------------------------------------------------------------------
# Einstieg
# --------------------------------------------------------------------------
def run_selftest(folder: Path) -> int:
    prepare_environment(folder)
    from . import selftest

    return selftest.main()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="knightmare")
    parser.add_argument("--headless", action="store_true", help="ohne Fenster")
    parser.add_argument("--no-browser", action="store_true", help="Browser nicht oeffnen")
    parser.add_argument("--selftest", action="store_true", help="Selbsttest ausfuehren")
    # macOS reicht beim Start per Finder manchmal "-psn_..." herein.
    args, _unknown = parser.parse_known_args(argv)

    folder = data_dir()
    setup_logging(folder)

    if args.selftest:
        return run_selftest(folder)

    existing = running_port(folder)
    if existing:
        log.info("Knightmare laeuft schon auf Port %s - oeffne nur den Browser.", existing)
        if not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{existing}/")
        return 0

    prepare_environment(folder)
    port = free_port()
    server = Server(port)
    server.start()
    if not server.wait_ready():
        log.error("Server kam nicht hoch.")
        if not args.headless:
            show_error(TEXTS[ui_language()]["failed"] + f"\n\n{folder}")
        server.stop()
        return 1

    (folder / "port").write_text(str(port), encoding="utf-8")
    log.info("Knightmare laeuft: %s (Daten: %s)", server.url, folder)
    if not args.no_browser:
        webbrowser.open(server.url)

    try:
        if args.headless or not run_window(server, folder):
            while server.alive:
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            (folder / "port").unlink()
        except OSError:
            pass
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
