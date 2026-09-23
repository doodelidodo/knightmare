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
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select

from . import __version__, stats
from .analysis import ERROR_LABELS, MISSED_LABELS
from .config import load_settings
from .db import get_session, init_db, new_session
from .models import ChessGame, ChessSyncState, utc_now
from .pipeline import current_status, recategorize, reset_analysis, run_once

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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    # Massstab und Schwellen wirken sofort, ohne Neuberechnung: die Werte pro
    # Zug liegen vor, nur die Einstufung wird nachgezogen. Sekunden, kein
    # Engine-Lauf. Ein Fehler hier darf den Start nicht verhindern.
    try:
        with new_session() as session:
            result = recategorize(session, settings)
        log.info("Einstufung geprueft: %s", result)
    except Exception as exc:  # noqa: BLE001
        log.error("Umstufung beim Start fehlgeschlagen: %s", exc)
    global _scheduler_thread
    if settings.auto_sync and settings.configured:
        _scheduler_thread = threading.Thread(
            target=_scheduler_loop, name="knightmare-scheduler", daemon=True
        )
        _scheduler_thread.start()
        log.info(
            "Scheduler gestartet (alle %.1f h, erster Lauf in %.0f s)",
            settings.sync_interval_hours,
            settings.startup_delay_seconds,
        )
    elif not settings.configured:
        log.warning(
            "CHESSCOM_USERNAME ist nicht gesetzt - es werden keine Partien geholt."
        )
    yield
    _stop_scheduler.set()


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
    if request.url.path.startswith("/api/"):
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

    return {
        "version": __version__,
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
        "run": current_status(),
        "last_sync": {
            "at": state.last_sync_at.isoformat() if state and state.last_sync_at else None,
            "status": state.last_sync_status if state else "nie gelaufen",
            "message": state.last_sync_message if state else "",
        },
    }


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


@api.get("/rating")
def get_rating(
    time_class: str = Query(default="rapid", max_length=20),
    limit: int = Query(default=120, ge=2, le=1000),
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return stats.rating_history(session, time_class=time_class, limit=limit)


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
