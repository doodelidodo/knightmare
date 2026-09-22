"""Ablauf: Partien abholen -> speichern -> analysieren.

Der Ablauf ist zweigeteilt, damit ein Abbruch der Analyse (Neustart, Fehler)
die bereits geholten Partien nicht verliert. Unanalysierte Partien werden beim
naechsten Lauf einfach weiterverarbeitet.
"""

from __future__ import annotations

import io
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import chess
import chess.pgn
from sqlalchemy import delete
from sqlmodel import Session, select

from .analysis import ANALYSIS_VERSION, AnalysisError, EngineAnalyzer
from .chesscom import ChessComClient, ChessComError
from .config import (
    DRAW_RESULTS,
    LICHESS_SPEEDS,
    PLATFORM_CHESSCOM,
    PLATFORM_LICHESS,
    Settings,
)
from .db import new_session
from .lichess import LichessClient, LichessError
from .models import ChessGame, ChessMove, ChessSyncState

log = logging.getLogger(__name__)

# Nur echtes Schach - keine Varianten wie Chess960, King of the Hill, Bughouse.
SUPPORTED_RULES = {"chess"}

_FAMILY_TERMINATORS = {
    "Defense",
    "Defence",
    "Opening",
    "Game",
    "Attack",
    "Gambit",
    "System",
    "Reversed",
}

# Ein Lauf zur Zeit - der Scheduler und ein manueller Klick im Dashboard
# duerfen sich nicht in die Quere kommen.
_run_lock = threading.Lock()

_status: dict[str, Any] = {
    "running": False,
    "phase": "idle",
    "started_at": None,
    "finished_at": None,
    "message": "",
    "fetched": 0,
    "analyzed": 0,
    "failed": 0,
}


def current_status() -> dict[str, Any]:
    return dict(_status)


def _set_status(**values: Any) -> None:
    _status.update(values)


# --------------------------------------------------------------------------
# Abbildung Chess.com -> eigenes Modell
# --------------------------------------------------------------------------
def opening_family_from_name(name: Optional[str]) -> Optional[str]:
    """Kuerzt einen Eroeffnungsnamen auf die Familie.

    Lichess liefert bereits 'Familie: Variante' - dort genuegt der Doppelpunkt
    und das Ergebnis ist exakt. Chess.com schreibt alles in einem Stueck
    ('Scandinavian Defense Mieses Kotroc Variation'), da hilft nur das
    Stichwort - und notfalls die ersten drei Woerter.
    """
    if not name:
        return None

    if ":" in name:
        family = name.split(":", 1)[0].strip()
        if family:
            return family

    words = name.split()
    for index, word in enumerate(words):
        if word.strip(",.:;") in _FAMILY_TERMINATORS:
            return " ".join(words[: index + 1])
    return " ".join(words[:3]) if words else None


def opening_name_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    if not slug:
        return None
    # Chess.com haengt die Zugfolge an: "...-Variation-3.Nf3-Nc6" -> abschneiden.
    slug = re.split(r"-\d+\.", slug)[0]
    name = slug.replace("-", " ").strip()
    return name or None


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _trim(value: Any, length: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text[:length] if text else None


def map_chesscom_game(payload: dict[str, Any], username: str) -> Optional[ChessGame]:
    """Baut aus einem Chess.com-Eintrag eine Zeile. None = Partie ueberspringen."""
    pgn_text = payload.get("pgn")
    if not pgn_text or not isinstance(pgn_text, str):
        return None

    white = payload.get("white") or {}
    black = payload.get("black") or {}
    white_name = str(white.get("username", "")).lower()
    black_name = str(black.get("username", "")).lower()

    if username == white_name:
        me, opponent, color = white, black, "white"
    elif username == black_name:
        me, opponent, color = black, white, "black"
    else:
        return None

    my_result = str(me.get("result", "")).lower()
    if my_result == "win":
        result = "win"
    elif my_result in DRAW_RESULTS:
        result = "draw"
    elif my_result:
        result = "loss"
    else:
        result = "unknown"

    end_time = _to_int(payload.get("end_time")) or 0
    played_at = datetime.fromtimestamp(end_time, tz=timezone.utc).replace(tzinfo=None)

    eco_code: Optional[str] = None
    eco_url: Optional[str] = None
    termination: Optional[str] = None
    try:
        headers = chess.pgn.read_headers(io.StringIO(pgn_text))
    except Exception:  # noqa: BLE001 - defektes PGN soll den Lauf nicht stoppen
        headers = None
    if headers is not None:
        eco_code = headers.get("ECO") or None
        eco_url = headers.get("ECOUrl") or None
        termination = headers.get("Termination") or None

    if not eco_url:
        raw_eco = payload.get("eco")
        if isinstance(raw_eco, str) and raw_eco.startswith("http"):
            eco_url = raw_eco
        elif isinstance(raw_eco, str) and raw_eco:
            eco_code = eco_code or raw_eco

    opening_name = opening_name_from_url(eco_url)
    opening_family = opening_family_from_name(opening_name)
    accuracies = payload.get("accuracies") or {}

    return ChessGame(
        platform=PLATFORM_CHESSCOM,
        uuid=f"chesscom:{payload.get('uuid') or payload.get('url') or ''}"[:64],
        url=_trim(payload.get("url"), 300) or "",
        played_at=played_at,
        time_class=_trim(payload.get("time_class"), 20) or "unknown",
        time_control=_trim(payload.get("time_control"), 30) or "",
        rated=bool(payload.get("rated", True)),
        color=color,
        result=result,
        termination=_trim(termination, 160),
        my_rating=_to_int(me.get("rating")),
        opponent=_trim(opponent.get("username"), 60) or "",
        opponent_rating=_to_int(opponent.get("rating")),
        eco=_trim(eco_code, 10),
        opening_name=_trim(opening_name, 200),
        opening_family=_trim(opening_family, 120),
        opening_url=_trim(eco_url, 300),
        accuracy_self=_to_float(accuracies.get(color)),
        accuracy_opponent=_to_float(
            accuracies.get("black" if color == "white" else "white")
        ),
        pgn=pgn_text,
    )


# Partien, die gar nicht erst gespielt wurden - kein PGN, nichts zu analysieren.
LICHESS_DEAD_STATUS = {"aborted", "noStart", "unknownFinish"}


def map_lichess_game(payload: dict[str, Any], username: str) -> Optional[ChessGame]:
    """Baut aus einem Lichess-Eintrag eine Zeile. None = Partie ueberspringen.

    Lichess liefert Eroeffnungsname und ECO-Code direkt mit - das Raten aus
    einer URL wie bei Chess.com entfaellt hier.
    """
    if str(payload.get("variant") or "standard") != "standard":
        return None
    if str(payload.get("status") or "") in LICHESS_DEAD_STATUS:
        return None

    pgn_text = payload.get("pgn")
    if not pgn_text or not isinstance(pgn_text, str):
        return None

    players = payload.get("players") or {}
    white = players.get("white") or {}
    black = players.get("black") or {}

    def _name(side: dict[str, Any]) -> str:
        user = side.get("user") or {}
        return str(user.get("name") or "").lower()

    wanted = (username or "").lower()
    if _name(white) == wanted:
        me, opponent, color = white, black, "white"
    elif _name(black) == wanted:
        me, opponent, color = black, white, "black"
    else:
        # Kann bei anonymen Gegnern oder Computer-Partien vorkommen.
        return None

    winner = payload.get("winner")
    if winner is None:
        result = "draw"
    elif str(winner) == color:
        result = "win"
    else:
        result = "loss"

    # Lichess rechnet in Millisekunden.
    last_move = _to_int(payload.get("lastMoveAt")) or _to_int(payload.get("createdAt")) or 0
    played_at = datetime.fromtimestamp(last_move / 1000, tz=timezone.utc).replace(
        tzinfo=None
    )

    speed = str(payload.get("speed") or "")
    time_class = LICHESS_SPEEDS.get(speed, speed.lower() or "unknown")

    clock = payload.get("clock") or {}
    initial = _to_int(clock.get("initial"))
    increment = _to_int(clock.get("increment"))
    if initial is not None and increment is not None:
        time_control = f"{initial}+{increment}" if increment else str(initial)
    else:
        time_control = ""

    opening = payload.get("opening") or {}
    opening_name = _trim(opening.get("name"), 200)
    opening_family = opening_family_from_name(opening_name)

    game_id = str(payload.get("id") or "")
    # Lichess zeigt die Partie aus Sicht der angehaengten Farbe an - praktisch,
    # wenn man den eigenen Fehler nachschauen will.
    url = f"https://lichess.org/{game_id}/{color}" if game_id else ""

    analysis_self = (me.get("analysis") or {}) if isinstance(me, dict) else {}
    analysis_opp = (opponent.get("analysis") or {}) if isinstance(opponent, dict) else {}

    return ChessGame(
        platform=PLATFORM_LICHESS,
        uuid=f"lichess:{game_id}"[:64],
        url=url[:300],
        played_at=played_at,
        time_class=time_class[:20],
        time_control=time_control[:30],
        rated=bool(payload.get("rated", True)),
        color=color,
        result=result,
        termination=_trim(payload.get("status"), 160),
        my_rating=_to_int(me.get("rating")),
        opponent=_trim((opponent.get("user") or {}).get("name"), 60) or "",
        opponent_rating=_to_int(opponent.get("rating")),
        eco=_trim(opening.get("eco"), 10),
        opening_name=opening_name,
        opening_family=_trim(opening_family, 120),
        opening_url=None,
        # Genauigkeit gibt es nur, wenn die Partie auf Lichess analysiert wurde.
        accuracy_self=_to_float(analysis_self.get("accuracy")),
        accuracy_opponent=_to_float(analysis_opp.get("accuracy")),
        pgn=pgn_text,
    )


def _archives_to_fetch(archives: list[str], settings: Settings, has_games: bool) -> list[str]:
    if not archives:
        return []
    if has_games:
        # Laufender Betrieb: aktueller und vorheriger Monat reichen, bereits
        # bekannte Partien werden ohnehin uebersprungen.
        return archives[-settings.incremental_archives :]

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=31 * settings.backfill_months)
    selected: list[str] = []
    for url in archives:
        match = re.search(r"/(\d{4})/(\d{2})/?$", url)
        if not match:
            continue
        year, month = int(match.group(1)), int(match.group(2))
        archive_date = datetime(year, month, 1, tzinfo=timezone.utc)
        if archive_date >= cutoff.replace(day=1):
            selected.append(url)
    return selected or archives[-settings.backfill_months :]


# --------------------------------------------------------------------------
# Schritt 1: abholen
# --------------------------------------------------------------------------
def _is_known(session: Session, uuid: str) -> bool:
    return (
        session.exec(select(ChessGame.id).where(ChessGame.uuid == uuid)).first()
        is not None
    )


def sync_chesscom(
    session: Session, settings: Settings, full: bool = False
) -> dict[str, Any]:
    username = settings.chesscom_username
    existing = session.exec(
        select(ChessGame.id)
        .where(ChessGame.platform == PLATFORM_CHESSCOM)
        .limit(1)
    ).first()
    has_games = existing is not None and not full

    with ChessComClient(username, settings.user_agent) as client:
        archives = client.archives()
        targets = _archives_to_fetch(archives, settings, has_games)

        added = 0
        skipped = 0
        last_archive: Optional[str] = None

        for archive_url in targets:
            last_archive = archive_url
            _set_status(
                phase=f"Chess.com {archive_url.rsplit('/', 2)[-2]}/"
                f"{archive_url.rsplit('/', 1)[-1]}"
            )
            for payload in client.games(archive_url):
                if str(payload.get("rules") or "chess").lower() not in SUPPORTED_RULES:
                    skipped += 1
                    continue
                time_class = str(payload.get("time_class") or "").lower()
                if settings.time_classes and time_class not in settings.time_classes:
                    skipped += 1
                    continue
                if settings.rated_only and not payload.get("rated", True):
                    skipped += 1
                    continue

                game = map_chesscom_game(payload, username.lower())
                if game is None or not game.uuid or _is_known(session, game.uuid):
                    skipped += 1
                    continue

                session.add(game)
                added += 1
            session.commit()

    return {
        "platform": PLATFORM_CHESSCOM,
        "added": added,
        "skipped": skipped,
        "archives": len(targets),
        "last_archive": last_archive,
    }


def sync_lichess(
    session: Session, settings: Settings, full: bool = False
) -> dict[str, Any]:
    """Holt die Lichess-Partien.

    Wie weit wir schon sind, steht in den Daten selbst: das juengste
    `played_at` dieser Plattform. Eine Stunde Ueberlappung, damit eine Partie
    an der Grenze nicht durchrutscht - Doppelte faengt die uuid-Pruefung ab.
    """
    username = settings.lichess_username
    since_ms: Optional[int] = None

    if not full:
        newest = session.exec(
            select(ChessGame.played_at)
            .where(ChessGame.platform == PLATFORM_LICHESS)
            .order_by(ChessGame.played_at.desc())  # type: ignore[union-attr]
            .limit(1)
        ).first()
        if newest is not None:
            overlap = newest - timedelta(hours=1)
            since_ms = int(overlap.replace(tzinfo=timezone.utc).timestamp() * 1000)

    if since_ms is None:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(
            days=31 * settings.backfill_months
        )
        since_ms = int(cutoff.timestamp() * 1000)

    added = 0
    skipped = 0
    seen = 0

    with LichessClient(
        username, settings.user_agent, token=settings.lichess_token
    ) as client:
        _set_status(phase="Lichess")
        for payload in client.games(since_ms=since_ms, rated_only=settings.rated_only):
            seen += 1
            if seen % 50 == 0:
                _set_status(phase=f"Lichess ({seen} gelesen)")

            game = map_lichess_game(payload, username)
            if game is None or not game.uuid:
                skipped += 1
                continue
            if settings.time_classes and game.time_class not in settings.time_classes:
                skipped += 1
                continue
            if _is_known(session, game.uuid):
                skipped += 1
                continue

            session.add(game)
            added += 1
            if added % 100 == 0:
                session.commit()
        session.commit()

    return {
        "platform": PLATFORM_LICHESS,
        "added": added,
        "skipped": skipped,
        "seen": seen,
    }


def sync_games(session: Session, settings: Settings, full: bool = False) -> dict[str, Any]:
    """Holt von allen konfigurierten Plattformen.

    Faellt eine Quelle aus, laufen die anderen trotzdem durch - ein
    Chess.com-Ausfall soll die Lichess-Partien nicht blockieren.
    """
    added = 0
    skipped = 0
    per_platform: list[dict[str, Any]] = []
    problems: list[str] = []
    last_archive: Optional[str] = None

    if settings.chesscom_username:
        try:
            result = sync_chesscom(session, settings, full=full)
            per_platform.append(result)
            added += result["added"]
            skipped += result["skipped"]
            last_archive = result.get("last_archive")
        except ChessComError as exc:
            session.rollback()
            problems.append(f"Chess.com: {exc}")
            log.error("Chess.com-Abgleich fehlgeschlagen: %s", exc)

    if settings.lichess_username:
        try:
            result = sync_lichess(session, settings, full=full)
            per_platform.append(result)
            added += result["added"]
            skipped += result["skipped"]
        except LichessError as exc:
            session.rollback()
            problems.append(f"Lichess: {exc}")
            log.error("Lichess-Abgleich fehlgeschlagen: %s", exc)

    if problems and not per_platform:
        # Keine einzige Quelle hat funktioniert - das ist ein echter Fehler.
        raise ChessComError("; ".join(problems))

    return {
        "added": added,
        "skipped": skipped,
        "platforms": per_platform,
        "problems": problems,
        "last_archive": last_archive,
    }


# --------------------------------------------------------------------------
# Schritt 2: analysieren
# --------------------------------------------------------------------------
def analyse_pending(
    session: Session, settings: Settings, limit: Optional[int] = None
) -> dict[str, Any]:
    maximum = limit if limit is not None else settings.max_games_per_run
    pending = session.exec(
        select(ChessGame)
        .where(ChessGame.analyzed_at.is_(None))  # type: ignore[union-attr]
        .where(ChessGame.analysis_error.is_(None))  # type: ignore[union-attr]
        .order_by(ChessGame.played_at.desc())  # type: ignore[union-attr]
        .limit(maximum)
    ).all()

    if not pending:
        return {"analyzed": 0, "failed": 0, "remaining": 0, "engine": None}

    analyzed = 0
    failed = 0
    engine_name: Optional[str] = None

    with EngineAnalyzer(settings) as analyzer:
        engine_name = analyzer.engine_id()
        for index, game in enumerate(pending, start=1):
            _set_status(phase=f"analysiere {index}/{len(pending)}")
            my_color = chess.WHITE if game.color == "white" else chess.BLACK
            try:
                report = analyzer.analyse_game(game.pgn, my_color, game.time_control)
            except AnalysisError as exc:
                game.analysis_error = str(exc)[:400]
                session.add(game)
                session.commit()
                failed += 1
                log.warning("Partie %s nicht analysierbar: %s", game.uuid, exc)
                continue
            except Exception as exc:  # noqa: BLE001 - eine kaputte Partie stoppt den Lauf nicht
                game.analysis_error = f"Unerwarteter Fehler: {exc}"[:400]
                session.add(game)
                session.commit()
                failed += 1
                log.exception("Unerwarteter Fehler bei Partie %s", game.uuid)
                continue

            # Alte Zuege entfernen, falls eine Partie erneut analysiert wird.
            for stale in session.exec(
                select(ChessMove).where(ChessMove.game_id == game.id)
            ).all():
                session.delete(stale)

            for move in report.moves:
                session.add(
                    ChessMove(
                        game_id=game.id,
                        ply=move.ply,
                        move_number=move.move_number,
                        san=move.san[:16],
                        phase=move.phase,
                        cp_before=move.cp_before,
                        cp_after=move.cp_after,
                        cp_loss=move.cp_loss,
                        win_loss=move.win_loss,
                        category=move.category,
                        error_type=_trim(move.error_type, 20),
                        missed_motif=_trim(move.missed_motif, 20),
                        best_move_san=_trim(move.best_move_san, 16),
                        refutation_san=_trim(move.refutation_san, 16),
                        clock_seconds=move.clock_seconds,
                        seconds_spent=move.seconds_spent,
                        played_at=game.played_at,
                        time_class=game.time_class,
                        color=game.color,
                        platform=game.platform,
                    )
                )

            game.acpl = report.acpl
            game.acpl_opening = report.acpl_by_phase.get("opening")
            game.acpl_middlegame = report.acpl_by_phase.get("middlegame")
            game.acpl_endgame = report.acpl_by_phase.get("endgame")
            game.inaccuracies = report.inaccuracies
            game.mistakes = report.mistakes
            game.blunders = report.blunders
            game.first_error_ply = report.first_error_ply
            game.move_count = report.move_count
            game.analyzed_at = datetime.utcnow()
            game.analysis_version = ANALYSIS_VERSION
            game.analysis_error = None
            session.add(game)
            session.commit()
            analyzed += 1

    remaining = len(
        session.exec(
            select(ChessGame.id)
            .where(ChessGame.analyzed_at.is_(None))  # type: ignore[union-attr]
            .where(ChessGame.analysis_error.is_(None))  # type: ignore[union-attr]
        ).all()
    )
    return {
        "analyzed": analyzed,
        "failed": failed,
        "remaining": remaining,
        "engine": engine_name,
    }


def reset_analysis(
    session: Session,
    time_class: Optional[str] = None,
    platform: Optional[str] = None,
) -> int:
    """Markiert Partien als unanalysiert und wirft ihre Zuege weg.

    Noetig nach geaenderten Schwellen oder erweiterter Analyse - die Partien
    selbst bleiben erhalten, es wird also nichts neu abgeholt.
    """
    statement = select(ChessGame)
    if time_class:
        statement = statement.where(ChessGame.time_class == time_class)
    if platform:
        statement = statement.where(ChessGame.platform == platform)
    games = list(session.exec(statement).all())
    game_ids = [game.id for game in games if game.id is not None]

    # In Haeppchen, damit die IN-Liste nicht ueberlaeuft.
    for start in range(0, len(game_ids), 500):
        chunk = game_ids[start : start + 500]
        session.execute(delete(ChessMove).where(ChessMove.game_id.in_(chunk)))

    for game in games:
        game.analyzed_at = None
        game.analysis_version = None
        game.analysis_error = None
        game.acpl = None
        game.acpl_opening = None
        game.acpl_middlegame = None
        game.acpl_endgame = None
        game.inaccuracies = 0
        game.mistakes = 0
        game.blunders = 0
        game.first_error_ply = None
        session.add(game)

    session.commit()
    log.info("%d Partien zur erneuten Analyse markiert", len(games))
    return len(games)


# --------------------------------------------------------------------------
# Gesamtlauf
# --------------------------------------------------------------------------
def _store_state(
    session: Session, settings: Settings, status: str, message: str, last_archive: Optional[str], fetched: int
) -> None:
    state = session.get(ChessSyncState, 1)
    if state is None:
        state = ChessSyncState(id=1)
    names = []
    if settings.chesscom_username:
        names.append(f"chess.com/{settings.chesscom_username}")
    if settings.lichess_username:
        names.append(f"lichess/{settings.lichess_username}")
    state.username = ", ".join(names)[:60]
    state.last_sync_at = datetime.utcnow()
    state.last_sync_status = status[:20]
    state.last_sync_message = message[:600]
    if last_archive:
        state.last_archive = last_archive[:200]
    state.games_fetched_total = (state.games_fetched_total or 0) + fetched
    state.updated_at = datetime.utcnow()
    session.add(state)
    session.commit()


def run_once(
    settings: Settings,
    do_sync: bool = True,
    analyse_limit: Optional[int] = None,
    full: bool = False,
) -> dict[str, Any]:
    """Ein kompletter Durchlauf. Gibt eine Zusammenfassung zurueck."""
    if not settings.configured:
        return {
            "ok": False,
            "message": "Weder CHESSCOM_USERNAME noch LICHESS_USERNAME ist gesetzt.",
        }

    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "message": "Es laeuft bereits ein Durchgang."}

    _set_status(
        running=True,
        phase="start",
        started_at=datetime.utcnow().isoformat(),
        finished_at=None,
        message="",
        fetched=0,
        analyzed=0,
        failed=0,
    )

    summary: dict[str, Any] = {"ok": True}
    session = new_session()
    try:
        fetch_result: dict[str, Any] = {"added": 0, "skipped": 0, "last_archive": None}
        if do_sync:
            _set_status(phase="hole Partien")
            fetch_result = sync_games(session, settings, full=full)
            _set_status(fetched=fetch_result["added"])

        _set_status(phase="analysiere")
        analysis_result = analyse_pending(session, settings, limit=analyse_limit)
        _set_status(
            analyzed=analysis_result["analyzed"], failed=analysis_result["failed"]
        )

        message = (
            f"{fetch_result['added']} neue Partien, "
            f"{analysis_result['analyzed']} analysiert, "
            # "fehlerhaft" waere in einer Schach-App doppeldeutig - gemeint ist
            # nicht lesbar, nicht etwa ein schlecht gespielter Zug.
            f"{analysis_result['failed']} nicht lesbar, "
            f"{analysis_result['remaining']} offen"
        )
        # Ausgefallene Quellen gehoeren in die Meldung - sonst wundert man
        # sich, warum von einer Plattform nichts ankommt.
        for problem in fetch_result.get("problems") or []:
            message += f" · {problem}"
        summary.update(fetch=fetch_result, analysis=analysis_result, message=message)
        _store_state(
            session, settings, "ok", message, fetch_result.get("last_archive"), fetch_result["added"]
        )
        _set_status(message=message)
    except ChessComError as exc:
        summary = {"ok": False, "message": str(exc)}
        _store_state(session, settings, "error", str(exc), None, 0)
        _set_status(message=str(exc))
        log.error("Abholen fehlgeschlagen: %s", exc)
    except AnalysisError as exc:
        summary = {"ok": False, "message": str(exc)}
        _store_state(session, settings, "error", str(exc), None, 0)
        _set_status(message=str(exc))
        log.error("Analyse fehlgeschlagen: %s", exc)
    except Exception as exc:  # noqa: BLE001
        summary = {"ok": False, "message": f"Unerwarteter Fehler: {exc}"}
        _store_state(session, settings, "error", str(exc), None, 0)
        _set_status(message=str(exc))
        log.exception("Durchlauf abgebrochen")
    finally:
        session.close()
        _set_status(
            running=False, phase="idle", finished_at=datetime.utcnow().isoformat()
        )
        _run_lock.release()

    return summary
