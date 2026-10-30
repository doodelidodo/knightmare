"""Knightmare - findet die Muster hinter deinen Schachfehlern.

Der Service liefert beides aus: die Oberflaeche unter `/` und die API unter
`/api`. Damit laeuft er ohne Reverse Proxy, ohne Build-Schritt und ohne
zweiten Container - `docker run` genuegt.

Die Oberflaeche spricht die API ueber *relative* Pfade an (`api/overview`,
nicht `/api/overview`). Dadurch funktioniert sie unveraendert auch dann, wenn
jemand sie hinter einem Praefix wie `/chess/` einhaengt.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import chess
from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select as sa_select
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from . import __version__, focus, journey, practice, progress, repertoire, review, stats, tactics
from .analysis import ERROR_LABELS, MISSED_LABELS
from .config import (
    SETUP_KEYS,
    USERNAME_PATTERN,
    current_settings,
    load_settings,
    settings_file,
    write_settings_file,
)
from .db import engine as db_engine
from .db import get_session, init_db, new_session
from .models import ChessGame, ChessSyncState, utc_now
from .pipeline import (
    current_status,
    merge_duplicate_games,
    recategorize,
    reset_analysis,
    run_once,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("knightmare")

WEB_DIR = Path(__file__).parent / "web"

settings = load_settings()
_stop_scheduler = threading.Event()
_scheduler_thread: Optional[threading.Thread] = None

# Gemeinsame Filterparameter
TimeClassParam = Query(
    default=None,
    max_length=20,
    description="bullet, blitz, rapid oder daily - leer = alle",
)
DaysParam = Query(default=None, ge=1, le=3650)
PlatformParam = Query(
    default=None,
    pattern="^(chesscom|lichess|all|alle)?$",
    description="chesscom oder lichess - leer = beide",
)


def _clean_platform(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = value.strip().lower()
    return None if value in {"", "all", "alle"} else value


def _clean_time_class(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = value.strip().lower()
    return None if value in {"", "all", "alle"} else value


def _scheduler_loop() -> None:
    """Wartet erst kurz (DB/Netz kommen nach dem Start nicht sofort),
    laeuft dann in festem Abstand weiter."""
    if _stop_scheduler.wait(settings.startup_delay_seconds):
        return
    interval = settings.sync_interval_hours * 3600
    while not _stop_scheduler.is_set():
        try:
            log.info("Geplanter Durchlauf startet")
            result = run_once(settings)
            log.info("Geplanter Durchlauf beendet: %s", result.get("message", result))
        except Exception:  # noqa: BLE001 - der Scheduler darf nie sterben
            log.exception("Geplanter Durchlauf abgebrochen")
        if _stop_scheduler.wait(interval):
            return


def _start_scheduler() -> bool:
    """Startet den Zeitplan, falls er noch nicht laeuft."""
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return False
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop, name="knightmare-scheduler", daemon=True
    )
    _scheduler_thread.start()
    log.info(
        "Scheduler gestartet (alle %.1f h, erster Lauf in %.0f s)",
        settings.sync_interval_hours,
        settings.startup_delay_seconds,
    )
    return True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    # Massstab und Schwellen wirken sofort, ohne Neuberechnung: die Werte pro
    # Zug liegen vor, nur die Einstufung wird nachgezogen. Sekunden, kein
    # Engine-Lauf. Ein Fehler hier darf den Start nicht verhindern.
    # Doppelte Partien (alte Chess.com-Schluessel ohne Praefix) zusammenfuehren,
    # bevor irgendetwas gezaehlt wird.
    try:
        with new_session() as session:
            merged = merge_duplicate_games(session)
        if any(merged.values()):
            log.info("Doppelte Partien bereinigt: %s", merged)
    except Exception as exc:  # noqa: BLE001
        log.error("Bereinigung doppelter Partien fehlgeschlagen: %s", exc)
    try:
        with new_session() as session:
            result = recategorize(session, settings)
        log.info("Einstufung geprueft: %s", result)
    except Exception as exc:  # noqa: BLE001
        log.error("Umstufung beim Start fehlgeschlagen: %s", exc)
    # Aufgaben: Lernstaende aus dem frueheren Trainer einmalig uebernehmen,
    # Engine im Hintergrund vorwaermen.
    try:
        tactics.import_legacy(db_engine)
    except Exception as exc:  # noqa: BLE001
        log.error("Uebernahme der Trainer-Aufgaben fehlgeschlagen: %s", exc)
    trainer = tactics.get_trainer()
    if trainer.enabled and trainer.engine.available:
        threading.Thread(target=trainer.engine.warm, name="tactics-warm", daemon=True).start()
    if settings.auto_sync and settings.configured:
        _start_scheduler()
    elif not settings.configured:
        log.warning(
            "CHESSCOM_USERNAME ist nicht gesetzt - es werden keine Partien geholt."
        )
    try:
        journey.start_brief_loop(settings, _stop_scheduler)
    except Exception as exc:  # noqa: BLE001
        log.error("Wochenbrief nicht gestartet: %s", exc)
    yield
    _stop_scheduler.set()
    tactics.get_trainer().engine.close()


app = FastAPI(
    title="Knightmare",
    version=__version__,
    description="Findet die Muster hinter deinen Schachfehlern.",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)


@app.middleware("http")
async def no_store_for_api(request, call_next):
    """API-Antworten duerfen nie aus dem Browser-Cache kommen.

    Sie sind Momentaufnahmen - Stand der Analyse, gefundene Partien, offene
    Zuege. Ohne Cache-Control raet der Browser eine Haltbarkeit und liefert
    eine Stunde alte Zahlen aus, ohne zu fragen. Das hat hier bereits zu
    einem verschwundenen Plattform-Filter und einer falsch gelesenen
    Versionsnummer gefuehrt.
    """
    response = await call_next(request)
    # Hinter einem Praefix (Online-Fassung, /lichess/<name>/) steht der in
    # root_path - verglichen wird der Pfad innerhalb dieser App.
    path, root = request.url.path, request.scope.get("root_path") or ""
    if root and path.startswith(root):
        path = path[len(root):]
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api = APIRouter(prefix="/api")


# --------------------------------------------------------------------------
# Betrieb
# --------------------------------------------------------------------------
@api.get("/health")
def health() -> dict[str, object]:
    return {"status": "ok", "service": "knightmare", "version": __version__}


@api.get("/status")
def status(session: Session = Depends(get_session)) -> dict[str, object]:
    total = len(session.exec(select(ChessGame.id)).all())
    analyzed = len(
        session.exec(
            select(ChessGame.id).where(ChessGame.analyzed_at.is_not(None))  # type: ignore[union-attr]
        ).all()
    )
    failed = len(
        session.exec(
            select(ChessGame.id).where(ChessGame.analysis_error.is_not(None))  # type: ignore[union-attr]
        ).all()
    )
    state = session.get(ChessSyncState, 1)
    # Pro Anfrage: die Online-Fassung setzt hier die Einstellungen des Spielers.
    settings = current_settings()

    return {
        "version": __version__,
        "online": settings.online_mode,
        "configured": settings.configured,
        "chesscom_username": settings.chesscom_username or None,
        "lichess_username": settings.lichess_username or None,
        "username": ", ".join(
            filter(None, [settings.chesscom_username, settings.lichess_username])
        )
        or None,
        "auto_sync": settings.auto_sync,
        "sync_interval_hours": settings.sync_interval_hours,
        "engine_path": settings.engine_path,
        "engine_movetime": settings.engine_movetime,
        "error_scale": settings.error_scale,
        "thresholds": (
            {
                "inaccuracy": settings.inaccuracy_win,
                "mistake": settings.mistake_win,
                "blunder": settings.blunder_win,
                "unit": "win%",
            }
            if settings.error_scale == "winprob"
            else {
                "inaccuracy": settings.inaccuracy_cp,
                "mistake": settings.mistake_cp,
                "blunder": settings.blunder_cp,
                "unit": "cp",
            }
        ),
        "error_labels": ERROR_LABELS,
        "missed_labels": MISSED_LABELS,
        "games_total": total,
        "games_analyzed": analyzed,
        "games_pending": max(0, total - analyzed - failed),
        "games_failed": failed,
        "time_classes": stats.available_time_classes(session),
        "platforms": stats.available_platforms(session),
        "configured_platforms": list(settings.platforms),
        "setup_available": settings_file() is not None,
        # Online laeuft der Zeitplan nicht pro Spieler - der Zustand des
        # Abholens gehoert dort dem Auftrag (app/online.py), nicht diesem Zaehler.
        "run": None if settings.online_mode else current_status(),
        "last_sync": {
            "at": state.last_sync_at.isoformat() if state and state.last_sync_at else None,
            "status": state.last_sync_status if state else "nie gelaufen",
            "message": state.last_sync_message if state else "",
        },
    }


# --------------------------------------------------------------------------
# Einrichtung im Browser (nur Desktop-Fassung)
# --------------------------------------------------------------------------
class SetupBody(BaseModel):
    chesscom_username: str = Field(default="", max_length=40)
    lichess_username: str = Field(default="", max_length=40)


def _account_exists(platform: str, username: str) -> Optional[bool]:
    """True/False, wenn die Plattform es sagt; None, wenn sie nicht antwortet.

    Ein Tippfehler im Namen fiele sonst erst Minuten spaeter als leere
    Auswertung auf. Ist man offline, wird trotzdem gespeichert.
    """
    import httpx

    url = (
        f"https://lichess.org/api/user/{username}"
        if platform == "lichess"
        else f"https://api.chess.com/pub/player/{username.lower()}"
    )
    try:
        response = httpx.get(
            url, headers={"User-Agent": settings.user_agent}, timeout=8.0
        )
    except httpx.HTTPError:
        return None
    if response.status_code == 404:
        return False
    if response.status_code == 200:
        # Lichess antwortet bei gesperrten Konten mit 200 und "disabled".
        try:
            return not response.json().get("disabled", False)
        except ValueError:
            return True
    return None


@api.get("/setup")
def get_setup() -> dict[str, object]:
    return {
        "available": settings_file() is not None,
        "chesscom_username": settings.chesscom_username,
        "lichess_username": settings.lichess_username,
    }


@api.post("/setup")
def post_setup(body: SetupBody, background: BackgroundTasks) -> dict[str, object]:
    global settings
    path = settings_file()
    if path is None:
        raise HTTPException(status_code=404, detail="setup_unavailable")

    names = {
        "chesscom": body.chesscom_username.strip(),
        "lichess": body.lichess_username.strip(),
    }
    if not any(names.values()):
        raise HTTPException(status_code=400, detail="none_given")
    for platform, name in names.items():
        if name and not USERNAME_PATTERN.match(name):
            raise HTTPException(status_code=400, detail=f"{platform}_invalid")
    unchecked = []
    for platform, name in names.items():
        if not name:
            continue
        exists = _account_exists(platform, name)
        if exists is False:
            raise HTTPException(status_code=400, detail=f"{platform}_unknown")
        if exists is None:
            unchecked.append(platform)

    values = dict(zip(SETUP_KEYS, (names["chesscom"], names["lichess"])))
    write_settings_file(path, values)
    os.environ.update(values)
    settings = load_settings()
    log.info("Einrichtung gespeichert: %s", ", ".join(settings.platforms))

    if settings.auto_sync:
        started = _start_scheduler()
        # Laeuft der Zeitplan schon (Namen geaendert), sofort einmal holen
        # statt bis zum naechsten Takt zu warten.
        if not started and not current_status().get("running"):
            background.add_task(run_once, settings, True, None, False)
    return {"saved": True, "platforms": list(settings.platforms), "unchecked": unchecked}


@api.post("/sync")
def sync(
    background: BackgroundTasks,
    full: bool = Query(default=False, description="Komplette Historie neu einlesen"),
    analyse_limit: Optional[int] = Query(default=None, ge=1, le=500),
) -> dict[str, object]:
    if not settings.configured:
        raise HTTPException(
            status_code=400,
            detail="Weder CHESSCOM_USERNAME noch LICHESS_USERNAME ist gesetzt.",
        )
    if current_status().get("running"):
        return {"started": False, "message": "Es laeuft bereits ein Durchgang."}

    background.add_task(run_once, settings, True, analyse_limit, full)
    return {
        "started": True,
        "message": "Durchgang gestartet - Fortschritt siehe Status.",
        "started_at": utc_now().isoformat(),
    }


@api.post("/reanalyze")
def reanalyze(
    background: BackgroundTasks,
    time_class: Optional[str] = TimeClassParam,
    platform: Optional[str] = PlatformParam,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    """Alle (oder alle einer Partieart) noch einmal durchrechnen."""
    if current_status().get("running"):
        return {"started": False, "message": "Es laeuft bereits ein Durchgang."}

    count = reset_analysis(
        session,
        time_class=_clean_time_class(time_class),
        platform=_clean_platform(platform),
    )
    background.add_task(run_once, settings, False, None, False)
    return {
        "started": True,
        "reset": count,
        "message": f"{count} Partien werden neu analysiert.",
    }


# --------------------------------------------------------------------------
# Auswertungen
# --------------------------------------------------------------------------
@api.get("/time-classes")
def get_time_classes(
    platform: Optional[str] = PlatformParam,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return {
        "time_classes": stats.available_time_classes(
            session, platform=_clean_platform(platform)
        ),
        "platforms": stats.available_platforms(session),
    }


@api.get("/overview")
def get_overview(
    days: Optional[int] = DaysParam,
    time_class: Optional[str] = TimeClassParam,
    platform: Optional[str] = PlatformParam,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return stats.overview(
        session,
        days=days,
        time_class=_clean_time_class(time_class),
        platform=_clean_platform(platform),
    )


@api.get("/error-types")
def get_error_types(
    days: Optional[int] = DaysParam,
    time_class: Optional[str] = TimeClassParam,
    platform: Optional[str] = PlatformParam,
    examples: int = Query(default=3, ge=0, le=10),
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return stats.error_types(
        session,
        days=days,
        time_class=_clean_time_class(time_class),
        platform=_clean_platform(platform),
        examples_per_type=examples,
    )


@api.get("/errors")
def get_errors(
    error_type: Optional[str] = Query(default=None, max_length=20),
    days: Optional[int] = DaysParam,
    time_class: Optional[str] = TimeClassParam,
    platform: Optional[str] = PlatformParam,
    phase: Optional[str] = Query(
        default=None, pattern="^(opening|middlegame|endgame)$"
    ),
    category: Optional[str] = Query(
        default=None, pattern="^(blunder|mistake|inaccuracy)$"
    ),
    sort: str = Query(default="cp_loss", pattern="^(cp_loss|recent)$"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    mate_in: Optional[str] = Query(
        default=None, pattern=r"^([1-9]|[1-9]\+|\?)$",
        description="Nur verpasste Matts dieser Laenge: 1, 2, 3, 4, 5+ oder ? (unklar)",
    ),
    session: Session = Depends(get_session),
) -> dict[str, object]:
    """Alle Fehlerzuege einer Art, zum Durchgehen - nicht nur eine Stichprobe."""
    return stats.error_moves(
        session,
        error_type=error_type or None,
        days=days,
        time_class=_clean_time_class(time_class),
        platform=_clean_platform(platform),
        phase=phase,
        category=category,
        sort=sort,
        limit=limit,
        offset=offset,
        mate_in=mate_in,
    )


@api.get("/missed")
def get_missed_motifs(
    days: Optional[int] = DaysParam,
    time_class: Optional[str] = TimeClassParam,
    platform: Optional[str] = PlatformParam,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    """Welche Taktik lag bereit und wurde nicht gespielt."""
    return stats.missed_motifs(
        session,
        days=days,
        time_class=_clean_time_class(time_class),
        platform=_clean_platform(platform),
    )


@api.get("/missed/moves")
def get_missed_moves(
    motif: Optional[str] = Query(default=None, max_length=20),
    days: Optional[int] = DaysParam,
    time_class: Optional[str] = TimeClassParam,
    platform: Optional[str] = PlatformParam,
    phase: Optional[str] = Query(default=None, max_length=20),
    sort: str = Query(default="cp_loss", pattern="^(cp_loss|recent)$"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    mate_in: Optional[str] = Query(
        default=None, pattern=r"^([1-9]|[1-9]\+|\?)$",
        description="Nur verpasste Matts dieser Laenge: 1, 2, 3, 4, 5+ oder ? (unklar)",
    ),
    session: Session = Depends(get_session),
) -> dict[str, object]:
    """Jede einzelne Stelle - bewusst ohne Obergrenze im Sinne von Stichprobe."""
    return stats.missed_moves(
        session,
        motif=(motif or None),
        days=days,
        time_class=_clean_time_class(time_class),
        platform=_clean_platform(platform),
        phase=(phase or None),
        sort=sort,
        limit=limit,
        offset=offset,
        mate_in=mate_in,
    )


@api.get("/openings")
def get_openings(
    color: Optional[str] = Query(default=None, pattern="^(white|black)$"),
    days: Optional[int] = DaysParam,
    time_class: Optional[str] = TimeClassParam,
    platform: Optional[str] = PlatformParam,
    min_games: int = Query(default=3, ge=1, le=50),
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return stats.openings(
        session,
        color=color,
        days=days,
        time_class=_clean_time_class(time_class),
        platform=_clean_platform(platform),
        min_games=min_games,
        limit=limit,
    )


@api.get("/focus")
def get_focus(
    days: Optional[int] = DaysParam,
    time_class: Optional[str] = TimeClassParam,
    platform: Optional[str] = PlatformParam,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return focus.focus(
        session,
        days=days,
        time_class=_clean_time_class(time_class),
        platform=_clean_platform(platform),
    )


@api.get("/phases")
def get_phases(
    days: Optional[int] = DaysParam,
    time_class: Optional[str] = TimeClassParam,
    platform: Optional[str] = PlatformParam,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return stats.phases(
        session,
        days=days,
        time_class=_clean_time_class(time_class),
        platform=_clean_platform(platform),
    )


@api.get("/time-pressure")
def get_time_pressure(
    days: Optional[int] = DaysParam,
    time_class: Optional[str] = TimeClassParam,
    platform: Optional[str] = PlatformParam,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return stats.time_pressure(
        session,
        days=days,
        time_class=_clean_time_class(time_class),
        platform=_clean_platform(platform),
    )


@api.get("/report/weekly")
def get_weekly_report(
    time_class: Optional[str] = TimeClassParam,
    platform: Optional[str] = PlatformParam,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return stats.weekly_report(
        session,
        time_class=_clean_time_class(time_class),
        platform=_clean_platform(platform),
    )


@api.get("/games")
def get_games(
    limit: int = Query(default=20, ge=1, le=200),
    time_class: Optional[str] = TimeClassParam,
    platform: Optional[str] = PlatformParam,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return stats.recent_games(
        session,
        limit=limit,
        time_class=_clean_time_class(time_class),
        platform=_clean_platform(platform),
    )


@api.get("/games/{game_id}/errors")
def get_game_errors(
    game_id: int, session: Session = Depends(get_session)
) -> dict[str, object]:
    result = stats.game_errors(session, game_id)
    if not result.get("found"):
        raise HTTPException(status_code=404, detail="Partie nicht gefunden.")
    return result


@api.get("/moves/{move_id}/board")
def get_move_board(move_id: int, session: Session = Depends(get_session)) -> dict[str, object]:
    result = stats.move_board(session, move_id)
    if not result.get("found"):
        raise HTTPException(status_code=404, detail="move_not_found")
    return result


# --------------------------------------------------------------------------
# Partien im Einzelnen: Phasen-Noten und alle Fehler einer Partie
# --------------------------------------------------------------------------
@api.get("/review/games")
def get_review_games(
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100000),
    days: Optional[int] = DaysParam,
    time_class: Optional[str] = TimeClassParam,
    platform: Optional[str] = PlatformParam,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return review.game_list(
        session, limit=limit, offset=offset, days=days,
        time_class=_clean_time_class(time_class), platform=_clean_platform(platform),
    )


@api.get("/review/games/{game_id}")
def get_review_game(game_id: int, session: Session = Depends(get_session)) -> dict[str, object]:
    result = review.game_report(session, game_id)
    if not result.get("found"):
        raise HTTPException(status_code=404, detail="game_not_found")
    return result


# --------------------------------------------------------------------------
# Repertoire: Zugbaum der eigenen Partien und wiederkehrende Fehler
# --------------------------------------------------------------------------
ColorParam = Query(default=None, pattern="^(white|black|all)?$")


@api.get("/repertoire/tree")
def get_repertoire_tree(
    color: Optional[str] = ColorParam,
    key: Optional[str] = Query(default=None, max_length=100),
    days: Optional[int] = DaysParam,
    time_class: Optional[str] = TimeClassParam,
    platform: Optional[str] = PlatformParam,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return repertoire.explore(
        session, color=color or "white", key=key, days=days,
        time_class=_clean_time_class(time_class), platform=_clean_platform(platform),
    )


@api.get("/repertoire/leaks")
def get_repertoire_leaks(
    color: Optional[str] = ColorParam,
    days: Optional[int] = DaysParam,
    time_class: Optional[str] = TimeClassParam,
    platform: Optional[str] = PlatformParam,
    limit: int = Query(default=30, ge=1, le=200),
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return repertoire.leaks(
        session, color=None if color in (None, "all") else color, days=days,
        time_class=_clean_time_class(time_class), platform=_clean_platform(platform), limit=limit,
    )


@api.get("/journey")
def get_journey(
    time_class: Optional[str] = TimeClassParam,
    platform: Optional[str] = PlatformParam,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    """Mein Weg: Prozess statt Elo - Partien ohne Patzer, saubere Serien und
    welche Frage der Checkliste welchen Fehler verhindert haette."""
    data = journey.journey(session, current_settings(), time_class=_clean_time_class(time_class),
                           platform=_clean_platform(platform))
    data["brief"] = {"enabled": current_settings().brief_enabled, "hour": current_settings().brief_hour}
    data["daily_streak"] = None
    try:
        if not current_settings().online_mode:
            with db_engine.connect() as conn:
                data["daily_streak"] = tactics.stats(conn, date.today(), tactics.get_trainer().settings)["streak"]
    except Exception:  # noqa: BLE001 - Zusatz
        pass
    return data


@api.post("/journey/brief")
def post_journey_brief(session: Session = Depends(get_session)) -> dict[str, object]:
    """Wochenbrief sofort schicken (zum Ausprobieren). Zaehlt nicht als der
    regulaere Sonntagsbrief."""
    cfg = current_settings()
    if not cfg.brief_enabled:
        raise HTTPException(status_code=400, detail="brief_disabled")
    tc, pf = journey.scope_of(cfg)
    title, body = journey.brief_text(journey.journey(session, cfg, time_class=tc, platform=pf), cfg.study_lang)
    return {"sent": journey.send_brief(cfg, title, body), "title": title, "body": body}


@api.get("/repertoire/mistakes")
def get_repertoire_mistakes(
    color: Optional[str] = ColorParam,
    days: Optional[int] = DaysParam,
    time_class: Optional[str] = TimeClassParam,
    platform: Optional[str] = PlatformParam,
    inaccuracies: bool = Query(default=False),
    limit: int = Query(default=300, ge=1, le=1000),
    session: Session = Depends(get_session),
) -> dict[str, object]:
    """Jeder eigene Fehler in der Eroeffnung, mit Uebersicht pro Eroeffnung."""
    return repertoire.opening_mistakes(
        session, color=None if color in (None, "all") else color, days=days,
        time_class=_clean_time_class(time_class), platform=_clean_platform(platform),
        include_inaccuracies=inaccuracies, limit=limit,
    )


class DrillBody(BaseModel):
    fen: str = Field(max_length=100)
    uci: str = Field(min_length=4, max_length=5)


@api.post("/repertoire/check")
def repertoire_check(body: DrillBody) -> dict[str, object]:
    """Eroeffnungs-Drill: ist der Zug in dieser Stellung gut genug? Nutzt die
    Engine des Trainings (eigener Prozess, kurze Rechenzeit)."""
    trainer = tactics.get_trainer()
    if not trainer.engine.available:
        raise HTTPException(status_code=503, detail="no_engine")
    try:
        board = chess.Board(body.fen)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="bad_fen") from exc
    if not board.is_valid():
        raise HTTPException(status_code=400, detail="bad_fen")
    res = repertoire.check_drill(trainer.engine, body.fen, body.uci,
                                 tolerance=trainer.settings.alt_tolerance)
    if res["result"] == "illegal":
        raise HTTPException(status_code=400, detail="illegal_move")
    return res


@api.get("/rating")
def get_rating(
    time_class: str = Query(default="rapid", max_length=20),
    limit: int = Query(default=120, ge=2, le=1000),
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return stats.rating_history(session, time_class=time_class, limit=limit)


# --------------------------------------------------------------------------
# Taktikaufgaben
# --------------------------------------------------------------------------
def _tactic_day(value: Optional[date]) -> date:
    """Der Tag kommt vom Browser - dort ist es 00:30, auf dem Server
    vielleicht noch gestern. Mehr als einen Tag Abstand gibt es nicht."""
    today = date.today()
    if value is None:
        return today
    if abs((value - today).days) > 1:
        raise HTTPException(status_code=400, detail="day_out_of_range")
    return value


def _tactics_payload(day: date, build: bool) -> dict[str, object]:
    trainer = tactics.get_trainer()
    T = tactics.TACTICS
    if build:
        try:
            with db_engine.begin() as conn:
                tactics.build_day(conn, day, trainer.settings)
        except IntegrityError:
            # Handy und PC gleichzeitig am Morgen: der andere war schneller,
            # seine Auswahl gilt.
            pass
    with db_engine.begin() as conn:
        rows = tactics.day_rows(conn, day)
        keys = [r.key for r in rows]
        by_key = {r.key: r for r in conn.execute(sa_select(T).where(T.c.key.in_(keys)))} if keys else {}
        items = []
        for r in rows:
            row = by_key.get(r.key)
            if row is None or row.status != "ready":
                continue
            item = {"pos": r.pos, "key": r.key, "result": r.result, "puzzle": tactics.puzzle_payload(row)}
            if r.result:
                item["explain"] = tactics.explanation(row)
            items.append(item)
        st = tactics.stats(conn, day, trainer.settings)
    return {"day": day.isoformat(), "items": items, "stats": st}


def _tactics_status() -> dict[str, object]:
    trainer = tactics.get_trainer()
    problem = None
    if not trainer.enabled:
        problem = "disabled"
    elif not trainer.engine.available:
        problem = "no_engine"
    elif trainer.status["last_error"]:
        problem = "failed"
    return {
        "enabled": trainer.enabled,
        "engine": trainer.engine.available,
        "preparing": trainer.status["preparing"],
        "per_day": trainer.settings.per_day,
        "problem": problem,
        "error": trainer.status["last_error"],
    }


@api.get("/tactics/today")
def tactics_today(day: Optional[date] = Query(default=None)) -> dict[str, object]:
    """Die Aufgaben des Tages. Beim ersten Aufruf wird die Auswahl
    festgeschrieben; reicht der Vorrat nicht, startet die Vorbereitung und
    die Seite fragt nach (preparing)."""
    d = _tactic_day(day)
    if current_settings().online_mode:
        # Das Training haengt an einer einzigen Datenbank und einem eigenen
        # Engine-Prozess - in der Online-Fassung (viele Spieler) gibt es es nicht.
        return {"day": d.isoformat(), "items": [], "stats": None, "enabled": False,
                "engine": False, "preparing": False, "per_day": 0,
                "problem": "disabled", "error": None}
    trainer = tactics.get_trainer()
    data = _tactics_payload(d, False)
    if not data["items"] and trainer.enabled:
        if trainer.pool_ready(d) or trainer.status["exhausted"] or not trainer.engine.available:
            data = _tactics_payload(d, True)
        if (not data["items"] and trainer.engine.available
                and not trainer.status["preparing"] and not trainer.status["exhausted"]):
            trainer.fill_in_background(d)
    return {**data, **_tactics_status()}


class TacticMoveBody(BaseModel):
    day: date
    key: str = Field(max_length=90)
    moves: list[str] = Field(min_length=1, max_length=20)


class TacticRevealBody(BaseModel):
    day: date
    key: str = Field(max_length=90)


def _tactic_for_day(conn, day: date, key: str):
    D, T = tactics.DAYS, tactics.TACTICS
    if conn.execute(sa_select(D).where(D.c.day == day, D.c.key == key)).first() is None:
        raise HTTPException(status_code=404, detail="not_in_day")
    row = conn.execute(sa_select(T).where(T.c.key == key)).first()
    if row is None or row.status != "ready":
        raise HTTPException(status_code=404, detail="not_found")
    return row


def _day_complete(conn, day: date) -> bool:
    rows = tactics.day_rows(conn, day)
    return bool(rows) and all(r.result is not None for r in rows)


def _finish(day: date, key: str, solved: bool) -> dict[str, object]:
    T = tactics.TACTICS
    with db_engine.begin() as conn:
        counted = tactics.record(conn, day, key, solved)
        complete = _day_complete(conn, day)
        row = conn.execute(sa_select(T).where(T.c.key == key)).first()
        return {"counted": counted, "day_complete": complete, "explain": tactics.explanation(row),
                "box": row.box, "due": row.due.isoformat() if row.due else None}


@api.post("/tactics/move")
def tactics_move(body: TacticMoveBody) -> dict[str, object]:
    """Prueft einen Zug. Zustandslos: der Browser schickt die ganze Folge
    seit der Ausgangsstellung. Die Loesung geht erst nach dem Loesen oder
    Aufdecken an den Browser."""
    trainer = tactics.get_trainer()
    day = _tactic_day(body.day)
    with db_engine.connect() as conn:
        row = _tactic_for_day(conn, day, body.key)
    try:
        res = tactics.check_move(trainer.engine, row, body.moves, trainer.settings)
    except tactics.MoveError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
    if res["result"] == "solved":
        res.update(_finish(day, body.key, True))
    elif res["result"] == "wrong":
        # Der erste Fehlversuch zaehlt fuer die Wiederholung; nochmal
        # probieren darf man trotzdem.
        with db_engine.begin() as conn:
            res["counted"] = tactics.record(conn, day, body.key, False)
            res["day_complete"] = _day_complete(conn, day)
    return res


@api.post("/tactics/reveal")
def tactics_reveal(body: TacticRevealBody) -> dict[str, object]:
    day = _tactic_day(body.day)
    with db_engine.connect() as conn:
        _tactic_for_day(conn, day, body.key)
    return _finish(day, body.key, False)


# --------------------------------------------------------------------------
# Fortschritt ueber Monate
# --------------------------------------------------------------------------
@api.get("/progress")
def get_progress(
    time_class: Optional[str] = TimeClassParam,
    platform: Optional[str] = PlatformParam,
    months: int = Query(default=12, ge=2, le=60),
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return progress.progress(session, time_class=_clean_time_class(time_class),
                             platform=_clean_platform(platform), months=months)


# --------------------------------------------------------------------------
# Uebungen: Gewinn verwerten, Was droht?, Woodpecker
# --------------------------------------------------------------------------
def _practice_engine():
    trainer = tactics.get_trainer()
    if not trainer.engine.available:
        raise HTTPException(status_code=503, detail="no_engine")
    return trainer.engine


@api.get("/practice/{kind}")
def get_practice(kind: str) -> dict[str, object]:
    if kind not in ("convert", "threat"):
        raise HTTPException(status_code=404, detail="unknown_kind")
    if current_settings().online_mode:
        return {"kind": kind, "items": [], "total": 0, "mastered": 0, "online": True}
    eng = tactics.get_trainer().engine
    scope = tactics.get_trainer().settings
    prepared = practice.ensure(db_engine, eng, kind, want=8, max_tries=12, scope=scope) if eng.available else None
    out = practice.listing(db_engine, kind, scope=scope)
    out["engine"] = eng.available
    out["prepared"] = prepared
    return out


class ConvertBody(BaseModel):
    key: str = Field(max_length=100)
    moves: list[str] = Field(default_factory=list, max_length=200)


@api.post("/practice/convert/move")
def practice_convert_move(body: ConvertBody) -> dict[str, object]:
    eng = _practice_engine()
    try:
        return practice.convert_move(db_engine, eng, body.key, body.moves)
    except tactics.MoveError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc


class KeyBody(BaseModel):
    key: str = Field(max_length=100)


@api.post("/practice/convert/giveup")
def practice_convert_giveup(body: KeyBody) -> dict[str, object]:
    try:
        return practice.convert_giveup(db_engine, body.key)
    except tactics.MoveError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc


class ThreatBody(BaseModel):
    key: str = Field(max_length=100)
    uci: str = Field(default="", max_length=5)
    reveal: bool = False


@api.post("/practice/threat/check")
def practice_threat_check(body: ThreatBody) -> dict[str, object]:
    eng = tactics.get_trainer().engine
    try:
        return practice.threat_check(db_engine, eng, body.key, body.uci, reveal=body.reveal)
    except tactics.MoveError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc


@api.get("/woodpecker")
def get_woodpecker() -> dict[str, object]:
    return practice.woodpecker_state(db_engine, tactics.get_trainer().settings)


class WoodStartBody(BaseModel):
    new_cycle: bool = False


@api.post("/woodpecker/start")
def woodpecker_start(body: WoodStartBody) -> dict[str, object]:
    try:
        return practice.woodpecker_start(db_engine, body.new_cycle, tactics.get_trainer().settings)
    except tactics.MoveError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc


class WoodMoveBody(BaseModel):
    key: str = Field(max_length=90)
    moves: list[str] = Field(default_factory=list, max_length=20)
    seconds: float = Field(default=0.0, ge=0, le=3600)
    reveal: bool = False


@api.post("/woodpecker/move")
def woodpecker_move(body: WoodMoveBody) -> dict[str, object]:
    """Wie /tactics/move, aber ohne Tagesauswahl und ohne Leitner: Woodpecker
    misst Tempo, nicht Wiederholung. Gezaehlt wird der erste Ausgang."""
    trainer = tactics.get_trainer()
    try:
        row = practice.woodpecker_row(db_engine, body.key)
        if body.reveal:
            res: dict[str, object] = {"result": "revealed", **tactics.explanation(row)}
            res.update(practice.woodpecker_record(db_engine, body.key, False, body.seconds))
            return res
        res = tactics.check_move(trainer.engine, row, body.moves, trainer.settings)
    except tactics.MoveError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
    if res["result"] in ("solved", "wrong"):
        res.update(practice.woodpecker_record(db_engine, body.key, res["result"] == "solved", body.seconds))
        if res["result"] == "solved":
            res["explain"] = tactics.explanation(row)
    return res



app.include_router(api)


# --------------------------------------------------------------------------
# Oberflaeche
# --------------------------------------------------------------------------
if (WEB_DIR / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")


@app.get("/health", include_in_schema=False)
def health_root() -> dict[str, object]:
    """Zweiter Gesundheitspfad fuer Docker und Reverse Proxies."""
    return health()


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Die Seite selbst darf der Browser nicht auf Verdacht behalten.

    FileResponse schickt nur ETag und Last-Modified. Ohne Cache-Control
    *raet* der Browser eine Haltbarkeit (ueblich: ein Zehntel des Alters der
    Datei) und liefert die Seite bis dahin aus dem Speicher, ohne zu fragen.
    Nach einem Update sieht man dann die alte Oberflaeche mit frischen Zahlen
    darin - ein Zustand, der ratlos macht.

    "no-cache" heisst nicht "nicht speichern", sondern "vor dem Benutzen
    nachfragen": der ETag bleibt, unveraendert kostet es nur ein 304.
    """
    return FileResponse(
        WEB_DIR / "index.html",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )
