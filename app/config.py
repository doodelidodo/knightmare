"""Konfiguration des Chess-Analyzers - alles ueber Umgebungsvariablen steuerbar.

Bewusst ohne pydantic-settings, damit der Service die gleichen Abhaengigkeiten
nutzt wie der Rest der Plattform.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

# Uebliche Ablageorte des Stockfish-Binaries. Das Debian-Paket "stockfish"
# installiert nach /usr/games/stockfish - das ist NICHT im Standard-PATH von
# root-losen Containern, darum die explizite Liste.
ENGINE_CANDIDATES = (
    "/usr/games/stockfish",
    "/usr/bin/stockfish",
    "/usr/local/bin/stockfish",
)

DRAW_RESULTS = frozenset(
    {
        "agreed",
        "repetition",
        "stalemate",
        "insufficient",
        "50move",
        "timevsinsufficient",
    }
)


def _env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_str(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env_str(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return _env_str(name, "1" if default else "0").lower() in {"1", "true", "yes", "on", "ja"}


def _env_tuple(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _env_str(name, "")
    if not raw:
        return default
    parts = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    return parts or default


def detect_engine_path() -> str:
    configured = _env_str("CHESS_ENGINE_PATH")
    if configured:
        return configured
    found = shutil.which("stockfish")
    if found:
        return found
    for candidate in ENGINE_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    # Absichtlich kein Fehler hier: der Service soll auch ohne Engine starten
    # koennen (die API liefert dann weiterhin bereits analysierte Daten aus).
    return "stockfish"


@dataclass(frozen=True)
class Settings:
    database_url: str
    username: str
    user_agent: str

    # Abholen
    backfill_months: int
    incremental_archives: int
    time_classes: tuple[str, ...]
    rated_only: bool

    # Zeitplan
    auto_sync: bool
    sync_interval_hours: float
    startup_delay_seconds: float

    # Engine
    engine_path: str
    engine_movetime: float
    engine_depth: int
    engine_threads: int
    engine_hash_mb: int

    # Analyse
    max_games_per_run: int
    analysis_max_plies: int
    opening_plies: int
    skip_opening_plies: int
    inaccuracy_cp: int
    mistake_cp: int
    blunder_cp: int

    @property
    def configured(self) -> bool:
        return bool(self.username)


def load_settings() -> Settings:
    return Settings(
        database_url=_env_str(
            "DATABASE_URL", "sqlite:///./data/chess-analyzer.db"
        ),
        username=_env_str("CHESSCOM_USERNAME"),
        user_agent=_env_str(
            "CHESS_USER_AGENT",
            "my-platform-chess-analyzer/1.0 (self-hosted; "
            "+https://github.com/doodelidodo/my-platform)",
        ),
        backfill_months=max(1, _env_int("CHESS_BACKFILL_MONTHS", 6)),
        incremental_archives=max(1, _env_int("CHESS_INCREMENTAL_ARCHIVES", 2)),
        time_classes=_env_tuple(
            "CHESS_TIME_CLASSES", ("bullet", "blitz", "rapid", "daily")
        ),
        rated_only=_env_bool("CHESS_RATED_ONLY", True),
        auto_sync=_env_bool("CHESS_AUTO_SYNC", True),
        sync_interval_hours=max(0.25, _env_float("CHESS_SYNC_INTERVAL_HOURS", 6.0)),
        startup_delay_seconds=max(0.0, _env_float("CHESS_STARTUP_DELAY_SECONDS", 45.0)),
        engine_path=detect_engine_path(),
        engine_movetime=max(0.02, _env_float("CHESS_ENGINE_MOVETIME", 0.15)),
        engine_depth=max(0, _env_int("CHESS_ENGINE_DEPTH", 0)),
        engine_threads=max(1, _env_int("CHESS_ENGINE_THREADS", 2)),
        engine_hash_mb=max(16, _env_int("CHESS_ENGINE_HASH_MB", 256)),
        max_games_per_run=max(1, _env_int("CHESS_MAX_GAMES_PER_RUN", 40)),
        analysis_max_plies=max(10, _env_int("CHESS_ANALYSIS_MAX_PLIES", 240)),
        opening_plies=max(0, _env_int("CHESS_OPENING_PLIES", 20)),
        skip_opening_plies=max(0, _env_int("CHESS_SKIP_OPENING_PLIES", 0)),
        inaccuracy_cp=max(1, _env_int("CHESS_INACCURACY_CP", 50)),
        mistake_cp=max(1, _env_int("CHESS_MISTAKE_CP", 100)),
        blunder_cp=max(1, _env_int("CHESS_BLUNDER_CP", 300)),
    )
