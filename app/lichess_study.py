"""Eigene Partien automatisch als Kapitel in Lichess-Studien ablegen.

Wozu: Knightmare findet die Fehler, aber eigene Varianten und Kommentare legt
man besser dort an, wo es dafuer ein richtiges Werkzeug gibt - in einer
Lichess-Studie, auch unterwegs im Browser. Das Abtippen erledigt dieses Modul:
nach jedem Durchlauf landen neue Partien (Vorgabe: Chess.com, Rapid) in einer
Studie pro Monat, mit Knightmares Fehlern schon als Kommentar und Symbol am Zug.

- Aus, solange LICHESS_STUDY_TOKEN fehlt. Der Token braucht study:read und
  study:write. In der Online-Fassung immer aus.
- Studien heissen nach CHESS_STUDY_NAME ("Games {month}" -> "Games 2026-10").
  Gibt es keine mit dem Namen, legt Knightmare eine private an. Ist eine voll
  (64 Kapitel), geht es in "Games 2026-10 (2)" weiter.
- Exportiert wird nur, was schon analysiert ist - sonst fehlten die Kommentare.
- Was schon drin ist, steht in chess_study_chapters. Ein Fehlschlag bei
  Lichess (Netz, 429) laesst die Partie offen, sie kommt beim naechsten Lauf.
"""

from __future__ import annotations

import io
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import chess
import chess.pgn
import httpx
from sqlalchemy import func
from sqlmodel import Session, select

from .analysis import (
    CATEGORY_BLUNDER,
    CATEGORY_INACCURACY,
    CATEGORY_MISTAKE,
    CATEGORY_OK,
    ERROR_LABELS,
    MISSED_LABELS,
)
from .config import Settings
from .models import ChessGame, ChessMove, ChessStudyChapter, utc_now

log = logging.getLogger(__name__)

BASE_URL = "https://lichess.org"
MAX_CHAPTERS = 64
# Bis "(9)" - mehr als 500 Partien einer Sorte im Monat waeren ohnehin seltsam.
MAX_OVERFLOW = 9
PAUSE_SECONDS = 1.0

# Lichess-Symbole (NAG): ?! = 6, ? = 2, ?? = 4
NAGS = {
    CATEGORY_INACCURACY: chess.pgn.NAG_DUBIOUS_MOVE,
    CATEGORY_MISTAKE: chess.pgn.NAG_MISTAKE,
    CATEGORY_BLUNDER: chess.pgn.NAG_BLUNDER,
}

TEXT = {
    "en": {
        "category": {
            CATEGORY_INACCURACY: "Inaccuracy",
            CATEGORY_MISTAKE: "Mistake",
            CATEGORY_BLUNDER: "Blunder",
        },
        "best": "Best",
        "reply": "Reply",
        "vs": "vs",
        "result": {"win": "win", "loss": "loss", "draw": "draw"},
        "date": "%Y-%m-%d",
        "error": dict(ERROR_LABELS),
        "missed": dict(MISSED_LABELS),
    },
    "de": {
        "category": {
            CATEGORY_INACCURACY: "Ungenauigkeit",
            CATEGORY_MISTAKE: "Fehler",
            CATEGORY_BLUNDER: "Patzer",
        },
        "best": "Besser",
        "reply": "Antwort",
        "vs": "gegen",
        "result": {"win": "Sieg", "loss": "Niederlage", "draw": "Remis"},
        "date": "%d.%m.%Y",
        "error": {
            "allowed_mate": "Matt zugelassen",
            "missed_mate": "Matt verpasst",
            "fork": "Gabel kassiert",
            "hanging_piece": "Figur eingestellt",
            "missed_threat": "Drohung übersehen",
            "bad_trade": "Schlechter Abtausch",
            "missed_win": "Gewinn verpasst",
            "positional": "Stellungsfehler",
        },
        "missed": {
            "mate": "Matt übersehen",
            "fork": "Gabel übersehen",
            "discovered": "Abzugsangriff übersehen",
            "skewer": "Spiess übersehen",
            "pin": "Fesselung übersehen",
            "material": "Freies Material übersehen",
        },
    },
}


class StudyError(RuntimeError):
    """Lichess nicht erreichbar oder Anfrage abgelehnt - Lauf abbrechen."""


class StudyFull(StudyError):
    """Die Studie hat schon 64 Kapitel."""


class GameRejected(StudyError):
    """Lichess lehnt genau diese Partie ab - nicht noch einmal schicken."""


# --------------------------------------------------------------------------
# Reine Hilfsfunktionen (ohne Netz, im Selbsttest geprueft)
# --------------------------------------------------------------------------
def _text(lang: str) -> dict[str, Any]:
    return TEXT.get(lang, TEXT["en"])


def _zone(name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - unbekannte Zone oder kein tzdata
        return timezone.utc


def local_time(played_at: datetime, zone_name: str) -> datetime:
    """played_at liegt als UTC ohne Zeitzone in der Datenbank."""
    return played_at.replace(tzinfo=timezone.utc).astimezone(_zone(zone_name))


def month_key(played_at: datetime, zone_name: str) -> str:
    return local_time(played_at, zone_name).strftime("%Y-%m")


def study_names(template: str, month: str) -> list[str]:
    """Moegliche Namen fuer einen Monat, in der Reihenfolge, in der sie
    gefuellt werden: "Games 2026-10", "Games 2026-10 (2)", ..."""
    if "{month}" not in template:
        template = template.strip() + " {month}"
    try:
        base = template.format(month=month)
    except (KeyError, IndexError, ValueError):
        base = f"{template} {month}"
    base = base.strip()[:90] or month
    return [base] + [f"{base} ({n})" for n in range(2, MAX_OVERFLOW + 1)]


def parse_month(value: str) -> Optional[datetime]:
    """'2026-10' -> erster Tag des Monats 00:00 (naiv). Unlesbar -> None."""
    try:
        return datetime.strptime(value.strip()[:7], "%Y-%m")
    except (ValueError, AttributeError):
        return None


def move_comment(move: ChessMove, lang: str) -> str:
    texts = _text(lang)
    parts: list[str] = []
    label = texts["category"].get(move.category, "")
    kind = texts["error"].get(move.error_type or "", "")
    head = f"{label}: {kind}" if label and kind else (label or kind)
    if head:
        parts.append(head)
    missed = texts["missed"].get(move.missed_motif or "", "")
    if missed and missed != kind:
        parts.append(missed)
    if move.best_move_san:
        parts.append(f"{texts['best']} {move.best_move_san}")
    if move.refutation_san:
        parts.append(f"{texts['reply']} {move.refutation_san}")
    return "Knightmare – " + ". ".join(parts) + "." if parts else ""


def annotated_pgn(pgn_text: str, moves: list[ChessMove], lang: str) -> str:
    """PGN mit Knightmares Einstufung als Symbol und Kommentar am eigenen Zug.

    Die Uhrzeit-Kommentare von Chess.com ([%clk ...]) bleiben erhalten -
    Lichess zeigt daraus die Restzeit an. Laesst sich das PGN nicht lesen,
    geht es unveraendert hinaus."""
    try:
        game = chess.pgn.read_game(io.StringIO(pgn_text or ""))
    except Exception:  # noqa: BLE001
        return pgn_text
    if game is None:
        return pgn_text

    by_ply = {
        move.ply: move
        for move in moves
        if move.category and move.category != CATEGORY_OK
    }
    if by_ply:
        for ply, node in enumerate(game.mainline(), start=1):
            move = by_ply.get(ply)
            if move is None:
                continue
            nag = NAGS.get(move.category)
            if nag:
                node.nags.add(nag)
            comment = move_comment(move, lang)
            if comment:
                node.comment = f"{node.comment} {comment}".strip() if node.comment else comment
        game.headers["Annotator"] = "Knightmare"

    exporter = chess.pgn.StringExporter(headers=True, variations=True, comments=True)
    return game.accept(exporter)


def chapter_name(game: ChessGame, lang: str, zone_name: str) -> str:
    texts = _text(lang)
    when = local_time(game.played_at, zone_name).strftime(texts["date"])
    result = texts["result"].get(game.result, game.result)
    opponent = game.opponent or "?"
    return f"{when} {texts['vs']} {opponent} · {result}"[:100]


def export_floor(session: Session, settings: Settings) -> datetime:
    """Ab wann exportiert wird (UTC, naiv)."""
    configured = parse_month(settings.study_from) if settings.study_from else None
    if configured is not None:
        start = configured
    else:
        first = session.exec(
            select(func.min(ChessGame.played_at))
            .join(ChessStudyChapter, ChessStudyChapter.game_id == ChessGame.id)
        ).first()
        reference = first if first else utc_now()
        local = local_time(reference, settings.study_timezone)
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        start = start.replace(tzinfo=_zone(settings.study_timezone))
        return start.astimezone(timezone.utc).replace(tzinfo=None)
    start = start.replace(tzinfo=_zone(settings.study_timezone))
    return start.astimezone(timezone.utc).replace(tzinfo=None)


def pending_games(session: Session, settings: Settings) -> list[ChessGame]:
    exported = select(ChessStudyChapter.game_id)
    statement = (
        select(ChessGame)
        .where(ChessGame.platform.in_(settings.study_platforms))
        .where(ChessGame.time_class.in_(settings.study_time_classes))
        .where(ChessGame.played_at >= export_floor(session, settings))
        .where(ChessGame.analyzed_at.is_not(None))
        .where(ChessGame.id.not_in(exported))
        .order_by(ChessGame.played_at)
        .limit(settings.study_max_per_run)
    )
    return list(session.exec(statement).all())


# --------------------------------------------------------------------------
# Lichess
# --------------------------------------------------------------------------
class LichessStudyClient:
    def __init__(self, token: str, user_agent: str, timeout: float = 30.0) -> None:
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}", "User-Agent": user_agent},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LichessStudyClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise StudyError(f"Lichess nicht erreichbar: {exc}") from exc
        if response.status_code == 401:
            raise StudyError("Lichess lehnt den Studien-Token ab (401) - LICHESS_STUDY_TOKEN prüfen.")
        if response.status_code == 403:
            raise StudyError("Dem Studien-Token fehlt eine Berechtigung (study:read, study:write).")
        if response.status_code == 429:
            raise StudyError("Lichess begrenzt gerade die Anfragen (429) - weiter beim nächsten Lauf.")
        return response

    def username(self) -> str:
        response = self._request("GET", "/api/account")
        if response.status_code != 200:
            raise StudyError(f"Lichess-Konto nicht lesbar ({response.status_code}).")
        return str(response.json().get("username") or "")

    def studies(self, username: str) -> dict[str, str]:
        """Name -> ID aller eigenen Studien (auch der privaten)."""
        response = self._request(
            "GET", f"/api/study/by/{username}", headers={"Accept": "application/x-ndjson"}
        )
        if response.status_code != 200:
            raise StudyError(f"Studien nicht lesbar ({response.status_code}).")
        found: dict[str, str] = {}
        for line in response.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            name, study_id = str(item.get("name") or "").strip(), str(item.get("id") or "")
            if name and study_id and name not in found:
                found[name] = study_id
        return found

    def create_study(self, name: str) -> str:
        response = self._request(
            "POST",
            "/api/study",
            data={
                "name": name,
                "visibility": "private",
                "computer": "everyone",
                "explorer": "everyone",
                "cloneable": "owner",
                "shareable": "owner",
                "chat": "member",
            },
        )
        if response.status_code != 200:
            raise StudyError(f"Studie „{name}“ liess sich nicht anlegen ({response.status_code}: {response.text[:120]}).")
        study_id = str(response.json().get("id") or "")
        if not study_id:
            raise StudyError(f"Lichess hat für „{name}“ keine ID zurückgegeben.")
        return study_id

    def import_chapter(self, study_id: str, pgn: str, name: str, orientation: str) -> str:
        response = self._request(
            "POST",
            f"/api/study/{study_id}/import-pgn",
            data={
                "pgn": pgn,
                "name": name,
                "orientation": orientation,
                # Sonst ersetzt Lichess den Namen durch "Weiss - Schwarz".
                "isDefaultName": "false",
                # Ersetzt das leere "Chapter 1" einer frisch angelegten Studie.
                # Ist das erste Kapitel nicht leer, passiert nichts.
                "initial": "true",
                "sticky": "false",
            },
        )
        text = response.text[:200]
        if response.status_code == 404:
            raise StudyError(f"Studie {study_id} gibt es nicht mehr.")
        if "too many chapters" in text.lower():
            raise StudyFull(study_id)
        if response.status_code == 400:
            raise GameRejected(text)
        if response.status_code != 200:
            raise StudyError(f"Import abgelehnt ({response.status_code}: {text}).")
        data = response.json()
        chapters = data.get("chapters") or []
        error = data.get("error")
        if not chapters:
            if error and "too many" in str(error).lower():
                raise StudyFull(study_id)
            raise GameRejected(str(error or "kein Kapitel angelegt"))
        return str(chapters[0].get("id") or "")


# --------------------------------------------------------------------------
# Ablauf
# --------------------------------------------------------------------------
def export_games(
    session: Session,
    settings: Settings,
    client: Optional[LichessStudyClient] = None,
    pause_seconds: float = PAUSE_SECONDS,
) -> Optional[dict[str, Any]]:
    """Exportiert ausstehende Partien. None, wenn ausgeschaltet."""
    if not settings.study_enabled:
        return None
    games = pending_games(session, settings)
    result: dict[str, Any] = {"exported": 0, "created": [], "rejected": 0, "problem": None}
    if not games:
        return result

    own_client = client is None
    client = client or LichessStudyClient(settings.study_token, settings.user_agent)
    try:
        studies = client.studies(client.username())
        full: set[str] = set()
        for index, game in enumerate(games):
            if index and pause_seconds:
                time.sleep(pause_seconds)
            moves = list(session.exec(select(ChessMove).where(ChessMove.game_id == game.id)).all())
            pgn = annotated_pgn(game.pgn, moves, settings.study_lang)
            name = chapter_name(game, settings.study_lang, settings.study_timezone)
            month = month_key(game.played_at, settings.study_timezone)
            orientation = "black" if game.color == "black" else "white"

            record: Optional[ChessStudyChapter] = None
            for study_name in study_names(settings.study_name, month):
                study_id = studies.get(study_name)
                if study_id in full:
                    continue
                if not study_id:
                    study_id = client.create_study(study_name)
                    studies[study_name] = study_id
                    result["created"].append(study_name)
                    log.info("Lichess-Studie angelegt: %s (%s)", study_name, study_id)
                try:
                    chapter_id = client.import_chapter(study_id, pgn, name, orientation)
                except StudyFull:
                    full.add(study_id)
                    continue
                except GameRejected as exc:
                    record = ChessStudyChapter(
                        game_id=game.id, study_id=study_id, study_name=study_name,
                        error=str(exc)[:300],
                    )
                    result["rejected"] += 1
                    log.warning("Lichess lehnt Partie %s ab: %s", game.uuid, exc)
                    break
                record = ChessStudyChapter(
                    game_id=game.id, study_id=study_id, study_name=study_name, chapter_id=chapter_id,
                )
                result["exported"] += 1
                break
            if record is None:
                raise StudyError(f"Alle Studien für {month} sind voll.")
            session.add(record)
            session.commit()
    except StudyError as exc:
        result["problem"] = str(exc)
        log.error("Export in Lichess-Studien abgebrochen: %s", exc)
    finally:
        if own_client:
            client.close()
    return result


def summary_text(result: Optional[dict[str, Any]]) -> str:
    """Kurzer Zusatz fuer die Laufmeldung, leer wenn nichts zu sagen ist."""
    if not result:
        return ""
    parts = []
    if result.get("exported"):
        parts.append(f"{result['exported']} in Lichess-Studie")
    if result.get("created"):
        parts.append("neue Studie " + ", ".join(result["created"]))
    if result.get("rejected"):
        parts.append(f"{result['rejected']} von Lichess abgelehnt")
    if result.get("problem"):
        parts.append(f"Studien-Export: {result['problem']}")
    return " · ".join(parts)
