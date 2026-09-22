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
from .analysis import ERROR_LABELS
from .config import load_settings
from .db import get_session, init_db
from .models import ChessGame, ChessSyncState
from .pipeline import current_status, reset_analysis, run_once

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
        "username": settings.username or None,
        "auto_sync": settings.auto_sync,
        "sync_interval_hours": settings.sync_interval_hours,
        "engine_path": settings.engine_path,
        "engine_movetime": settings.engine_movetime,
        "thresholds": {
            "inaccuracy": settings.inaccuracy_cp,
            "mistake": settings.mistake_cp,
            "blunder": settings.blunder_cp,
        },
        "error_labels": ERROR_LABELS,
        "games_total": total,
        "games_analyzed": analyzed,
        "games_pending": max(0, total - analyzed - failed),
        "games_failed": failed,
        "time_classes": stats.available_time_classes(session),
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
            detail="CHESSCOM_USERNAME ist nicht gesetzt.",
        )
    if current_status().get("running"):
        return {"started": False, "message": "Es laeuft bereits ein Durchgang."}

    background.add_task(run_once, settings, True, analyse_limit, full)
    return {
        "started": True,
        "message": "Durchgang gestartet - Fortschritt siehe Status.",
        "started_at": datetime.utcnow().isoformat(),
    }


@api.post("/reanalyze")
def reanalyze(
    background: BackgroundTasks,
    time_class: Optional[str] = TimeClassParam,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    """Alle (oder alle einer Partieart) noch einmal durchrechnen."""
    if current_status().get("running"):
        return {"started": False, "message": "Es laeuft bereits ein Durchgang."}

    selected = _clean_time_class(time_class)
    count = reset_analysis(session, time_class=selected)
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
def get_time_classes(session: Session = Depends(get_session)) -> dict[str, object]:
    return {"time_classes": stats.available_time_classes(session)}


@api.get("/overview")
def get_overview(
    days: Optional[int] = DaysParam,
    time_class: Optional[str] = TimeClassParam,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return stats.overview(session, days=days, time_class=_clean_time_class(time_class))


@api.get("/error-types")
def get_error_types(
    days: Optional[int] = DaysParam,
    time_class: Optional[str] = TimeClassParam,
    examples: int = Query(default=3, ge=0, le=10),
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return stats.error_types(
        session,
        days=days,
        time_class=_clean_time_class(time_class),
        examples_per_type=examples,
    )


@api.get("/errors")
def get_errors(
    error_type: Optional[str] = Query(default=None, max_length=20),
    days: Optional[int] = DaysParam,
    time_class: Optional[str] = TimeClassParam,
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
        phase=phase,
        category=category,
        sort=sort,
        limit=limit,
        offset=offset,
    )


@api.get("/openings")
def get_openings(
    color: Optional[str] = Query(default=None, pattern="^(white|black)$"),
    days: Optional[int] = DaysParam,
    time_class: Optional[str] = TimeClassParam,
    min_games: int = Query(default=3, ge=1, le=50),
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return stats.openings(
        session,
        color=color,
        days=days,
        time_class=_clean_time_class(time_class),
        min_games=min_games,
        limit=limit,
    )


@api.get("/phases")
def get_phases(
    days: Optional[int] = DaysParam,
    time_class: Optional[str] = TimeClassParam,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return stats.phases(session, days=days, time_class=_clean_time_class(time_class))


@api.get("/time-pressure")
def get_time_pressure(
    days: Optional[int] = DaysParam,
    time_class: Optional[str] = TimeClassParam,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return stats.time_pressure(
        session, days=days, time_class=_clean_time_class(time_class)
    )


@api.get("/report/weekly")
def get_weekly_report(
    time_class: Optional[str] = TimeClassParam,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return stats.weekly_report(session, time_class=_clean_time_class(time_class))


@api.get("/games")
def get_games(
    limit: int = Query(default=20, ge=1, le=200),
    time_class: Optional[str] = TimeClassParam,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return stats.recent_games(
        session, limit=limit, time_class=_clean_time_class(time_class)
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
    return FileResponse(WEB_DIR / "index.html")
