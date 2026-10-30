"""Mein Weg: Fortschritt, den man selbst in der Hand hat.

Elo schwankt um 50 Punkte, ohne dass sich am Spiel etwas aendert - als
Wochenziel taugt sie nicht, und wer jede Partie als Pruefung erlebt, spielt
verkrampft. Diese Seite misst stattdessen den Prozess:

- Partien ohne Patzer, saubere Partien (ohne Fehler und Patzer),
- die laengste Serie eigener Zuege ohne Fehler,
- und welcher Punkt der Checkliste vor jedem Zug einen Fehler verhindert haette.

Die Checkliste (vier Fragen vor jedem Zug) ist bewusst kurz. Jede Fehlerart
aus der Analyse gehoert zu genau einer Frage - abgeleitet aus dem, was
`analysis.classify_error` tatsaechlich prueft:

  1 threat  Was will er?        Drohung uebersehen: der Gegner gewinnt Material,
                                das schon angegriffen war oder angegriffen wird.
  2 chance  Was kann ich?       Gewinn/Matt verpasst, oder eine Taktik stand bereit.
  3 safe    Ist es sicher?      Die gezogene Figur haengt, oder der Abtausch verliert.
  4 reply   Seine beste Antwort? Gabel oder Matt nach dem eigenen Zug.

Stellungsfehler ohne verpasstes Motiv gehoeren zu keiner Frage - dafuer ist
eine Checkliste nicht da, und das wird auch so gesagt.

Dazu der Wochenbrief: sonntags eine Push-Nachricht (ntfy) mit den Zahlen der
Woche und den zwei Partien, die sich fuer ein Video lohnen.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from sqlmodel import Session, select

from .config import Settings
from .models import ChessGame, ChessMove, ChessState, utc_now
from .review import move_accuracy

log = logging.getLogger(__name__)

CHECKLIST = ("threat", "chance", "safe", "reply")

POINT_FOR_ERROR = {
    "missed_threat": "threat",
    "missed_win": "chance",
    "missed_mate": "chance",
    "hanging_piece": "safe",
    "bad_trade": "safe",
    "fork": "reply",
    "allowed_mate": "reply",
}

SERIOUS = ("mistake", "blunder")
WEEKS = 8
BRIEF_STATE_KEY = "weekly_brief_sent"


def checklist_point(error_type: Optional[str], missed_motif: Optional[str] = None) -> Optional[str]:
    point = POINT_FOR_ERROR.get(error_type or "")
    if point:
        return point
    if missed_motif:
        return "chance"
    return None


def _zone(name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return timezone.utc


def _local(value: datetime, zone) -> datetime:
    return value.replace(tzinfo=timezone.utc).astimezone(zone)


def week_start(day: date) -> date:
    """Montag der Woche."""
    return day - timedelta(days=day.weekday())


def longest_clean_run(moves: list[ChessMove]) -> int:
    """Laengste Folge eigener Zuege ohne Fehler oder Patzer (Ungenauigkeiten
    unterbrechen nicht - die gehoeren zum Schach auf jedem Niveau)."""
    best = run = 0
    for move in sorted(moves, key=lambda m: m.ply):
        if move.category in SERIOUS:
            run = 0
        else:
            run += 1
            best = max(best, run)
    return best


def _game_facts(game: ChessGame, moves: list[ChessMove]) -> dict[str, Any]:
    serious = [m for m in moves if m.category in SERIOUS]
    points = Counter(p for p in (checklist_point(m.error_type, m.missed_motif) for m in serious) if p)
    accuracy = round(sum(move_accuracy(m.win_loss) for m in moves) / len(moves), 1) if moves else None
    worst = max(serious, key=lambda m: m.win_loss or 0.0) if serious else None
    return {
        "id": game.id,
        "played_at": game.played_at.isoformat(),
        "opponent": game.opponent,
        "result": game.result,
        "color": game.color,
        "url": game.url,
        "opening": game.opening_family or game.opening_name,
        "moves": len(moves),
        "blunders": sum(1 for m in moves if m.category == "blunder"),
        "mistakes": sum(1 for m in moves if m.category == "mistake"),
        "blunder_free": not any(m.category == "blunder" for m in moves),
        "clean": not serious,
        "longest_run": longest_clean_run(moves),
        "accuracy": accuracy,
        "points": dict(points),
        "preventable": sum(points.values()),
        "worst": {
            "move_id": worst.id, "san": worst.san, "move_number": worst.move_number,
            "category": worst.category, "error_type": worst.error_type, "loss": round(worst.win_loss or 0.0, 1),
            "point": checklist_point(worst.error_type, worst.missed_motif),
        } if worst else None,
    }


def _week_block(facts: list[dict[str, Any]]) -> dict[str, Any]:
    points = Counter()
    for f in facts:
        points.update(f["points"])
    games = len(facts)
    return {
        "games": games,
        "wins": sum(1 for f in facts if f["result"] == "win"),
        "blunder_free": sum(1 for f in facts if f["blunder_free"]),
        "clean": sum(1 for f in facts if f["clean"]),
        "blunder_free_pct": round(sum(1 for f in facts if f["blunder_free"]) / games * 100) if games else None,
        "longest_run": max((f["longest_run"] for f in facts), default=0),
        "errors": sum(f["blunders"] + f["mistakes"] for f in facts),
        "preventable": sum(points.values()),
        "points": {key: points.get(key, 0) for key in CHECKLIST},
        "accuracy": round(sum(f["accuracy"] for f in facts if f["accuracy"] is not None)
                          / max(1, sum(1 for f in facts if f["accuracy"] is not None)), 1)
        if any(f["accuracy"] is not None for f in facts) else None,
    }


def journey(
    session: Session,
    settings: Settings,
    time_class: Optional[str] = None,
    platform: Optional[str] = None,
    today: Optional[date] = None,
) -> dict[str, Any]:
    zone = _zone(settings.study_timezone)
    today = today or _local(utc_now(), zone).date()
    start = week_start(today) - timedelta(weeks=WEEKS - 1)
    since_utc = datetime.combine(start, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)

    statement = select(ChessGame).where(ChessGame.played_at >= since_utc, ChessGame.analyzed_at.is_not(None))
    if time_class:
        statement = statement.where(ChessGame.time_class == time_class)
    if platform:
        statement = statement.where(ChessGame.platform == platform)
    games = list(session.exec(statement).all())
    ids = [g.id for g in games]
    moves_by_game: dict[int, list[ChessMove]] = {}
    for chunk in range(0, len(ids), 400):
        part = ids[chunk:chunk + 400]
        for move in session.exec(select(ChessMove).where(ChessMove.game_id.in_(part))).all():
            moves_by_game.setdefault(move.game_id, []).append(move)

    by_week: dict[date, list[dict[str, Any]]] = {}
    for game in games:
        moves = moves_by_game.get(game.id, [])
        if not moves:
            continue
        week = week_start(_local(game.played_at, zone).date())
        by_week.setdefault(week, []).append(_game_facts(game, moves))

    this_monday = week_start(today)
    current_facts = sorted(by_week.get(this_monday, []), key=lambda f: f["played_at"])
    previous_facts = by_week.get(this_monday - timedelta(weeks=1), [])
    current = _week_block(current_facts)
    previous = _week_block(previous_facts)

    weeks = []
    for i in range(WEEKS):
        monday = start + timedelta(weeks=i)
        block = _week_block(by_week.get(monday, []))
        weeks.append({"start": monday.isoformat(), "games": block["games"],
                      "blunder_free_pct": block["blunder_free_pct"], "clean": block["clean"],
                      "preventable_per_game": round(block["preventable"] / block["games"], 2) if block["games"] else None})

    # Fokus: die Frage, die in den letzten vier Wochen am meisten verhindert haette.
    recent = Counter()
    for i in range(4):
        for f in by_week.get(this_monday - timedelta(weeks=i), []):
            recent.update(f["points"])
    focus = recent.most_common(1)[0][0] if recent else None

    # Ein Beispiel pro Frage: der juengste vermeidbare Fehler.
    examples: dict[str, dict[str, Any]] = {}
    all_facts = sorted((f for items in by_week.values() for f in items), key=lambda f: f["played_at"], reverse=True)
    for f in all_facts:
        moves = sorted(moves_by_game.get(f["id"], []), key=lambda m: -m.ply)
        for move in moves:
            if move.category not in SERIOUS:
                continue
            point = checklist_point(move.error_type, move.missed_motif)
            if point and point not in examples:
                examples[point] = {
                    "move_id": move.id, "game_id": f["id"], "san": move.san, "move_number": move.move_number,
                    "color": f["color"], "opponent": f["opponent"], "played_at": f["played_at"],
                    "category": move.category, "error_type": move.error_type,
                    "best_san": move.best_move_san, "refutation_san": move.refutation_san,
                }
        if len(examples) == len(CHECKLIST):
            break

    # Ziel der Woche: eine Partie ohne Patzer mehr als letzte Woche, mindestens zwei.
    goal_target = max(2, previous["blunder_free"] + 1)

    # Fuers Video: die lehrreichste (teuerster vermeidbarer Fehler) und die beste Partie.
    lesson = max((f for f in current_facts if f["worst"]), key=lambda f: f["worst"]["loss"], default=None)
    best = max((f for f in current_facts if f["accuracy"] is not None and f is not lesson),
               key=lambda f: (f["clean"], f["accuracy"]), default=None)

    return {
        "today": today.isoformat(),
        "week_start": this_monday.isoformat(),
        "checklist": list(CHECKLIST),
        "current": current,
        "previous": previous,
        "games": current_facts,
        "weeks": weeks,
        "focus": focus,
        "examples": examples,
        "goal": {"target": goal_target, "done": current["blunder_free"]},
        "lesson": lesson,
        "best": best,
        "highlights": highlights(current, previous),
    }


def highlights(current: dict[str, Any], previous: dict[str, Any]) -> list[dict[str, Any]]:
    """Was diese Woche gut lief - als Schluessel mit Zahlen, die Oberflaeche
    formuliert. Nur Dinge, die wirklich stimmen; lieber eine Zeile weniger."""
    out: list[dict[str, Any]] = []
    if current["blunder_free"]:
        out.append({"key": "blunder_free", "n": current["blunder_free"], "games": current["games"],
                    "more": current["blunder_free"] - previous["blunder_free"]})
    if current["clean"]:
        out.append({"key": "clean", "n": current["clean"]})
    if current["longest_run"] >= 15:
        out.append({"key": "run", "n": current["longest_run"],
                    "record": current["longest_run"] > previous["longest_run"] and previous["games"] > 0})
    if previous["games"] and current["games"] and current["preventable"] / current["games"] \
            < previous["preventable"] / previous["games"]:
        out.append({"key": "fewer", "now": round(current["preventable"] / current["games"], 1),
                    "before": round(previous["preventable"] / previous["games"], 1)})
    if current["games"] and not out:
        out.append({"key": "played", "n": current["games"]})
    return out


# --------------------------------------------------------------------------
# Wochenbrief per ntfy
# --------------------------------------------------------------------------
TEXT = {
    "de": {
        "points": {"threat": "Was will er?", "chance": "Was kann ich?", "safe": "Ist es sicher?",
                   "reply": "Seine beste Antwort?"},
        "title": lambda g, b: f"Schachwoche: {g} Partien, {b} ohne Patzer",
        "none": "Schachwoche: keine analysierte Partie",
        "blunder_free": lambda n, g, p: f"✓ {n} von {g} Partien ohne Patzer (Vorwoche {p})",
        "clean": lambda n: f"✓ {n} ganz {'saubere Partie' if n == 1 else 'saubere Partien'} (kein Fehler, kein Patzer)",
        "run": lambda n: f"✓ Längste saubere Serie: {n} Züge",
        "preventable": lambda n, label, k: f"Mit der Checkliste vermeidbar: {n} Fehler – vor allem „{label}“ ({k}×)",
        "focus": lambda label: f"Fokus nächste Woche: „{label}“",
        "lesson": lambda o, mv: f"Fürs Video, lehrreich: gegen {o} ({mv})",
        "best": lambda o, a: f"Fürs Video, gut gespielt: gegen {o} ({a} %)",
        "rest": "Diese Woche keine Partie – Pausen gehören dazu.",
    },
    "en": {
        "points": {"threat": "What does he want?", "chance": "What can I do?", "safe": "Is it safe?",
                   "reply": "His best answer?"},
        "title": lambda g, b: f"Chess week: {g} games, {b} without a blunder",
        "none": "Chess week: no analysed game",
        "blunder_free": lambda n, g, p: f"✓ {n} of {g} games without a blunder (week before: {p})",
        "clean": lambda n: f"✓ {n} completely clean {'game' if n == 1 else 'games'}",
        "run": lambda n: f"✓ Longest clean run: {n} moves",
        "preventable": lambda n, label, k: f"Preventable with the checklist: {n} — mostly “{label}” ({k}×)",
        "focus": lambda label: f"Focus next week: “{label}”",
        "lesson": lambda o, mv: f"For the video, instructive: vs {o} ({mv})",
        "best": lambda o, a: f"For the video, well played: vs {o} ({a}%)",
        "rest": "No game this week — breaks are part of it.",
    },
}


def brief_text(data: dict[str, Any], lang: str = "de") -> tuple[str, str]:
    """(Titel, Text) des Wochenbriefs."""
    t = TEXT.get(lang, TEXT["en"])
    cur, prev = data["current"], data["previous"]
    if not cur["games"]:
        return t["none"], t["rest"]
    lines = [t["blunder_free"](cur["blunder_free"], cur["games"], prev["blunder_free"])]
    if cur["clean"]:
        lines.append(t["clean"](cur["clean"]))
    if cur["longest_run"]:
        lines.append(t["run"](cur["longest_run"]))
    if cur["preventable"]:
        key, k = max(cur["points"].items(), key=lambda kv: kv[1])
        lines.append(t["preventable"](cur["preventable"], t["points"][key], k))
    if data.get("focus"):
        lines.append(t["focus"](t["points"][data["focus"]]))
    if data.get("lesson") and data["lesson"]["worst"]:
        w = data["lesson"]["worst"]
        sep = "." if data["lesson"]["color"] == "white" else "…"
        lines.append(t["lesson"](data["lesson"]["opponent"] or "?", f"{w['move_number']}{sep} {w['san']}"))
    if data.get("best") and data["best"]["accuracy"] is not None:
        lines.append(t["best"](data["best"]["opponent"] or "?", round(data["best"]["accuracy"])))
    return t["title"](cur["games"], cur["blunder_free"]), "\n".join(lines)


def _state_get(session: Session, key: str) -> Optional[str]:
    row = session.get(ChessState, key)
    return row.value if row else None


def _state_set(session: Session, key: str, value: str) -> None:
    row = session.get(ChessState, key) or ChessState(key=key)
    row.value = value
    row.updated_at = utc_now()
    session.add(row)
    session.commit()


def brief_due(settings: Settings, now_local: datetime, last_sent: Optional[str]) -> Optional[str]:
    """Woche (Montag als ISO-Datum), fuer die jetzt ein Brief faellig ist - oder None.
    Faellig ab Sonntag zur eingestellten Stunde, einmal pro Woche; verpasst
    (Server aus) wird bis Montagmittag nachgeholt, danach nicht mehr."""
    monday = week_start(now_local.date())
    if now_local.weekday() == 6 and now_local.hour >= settings.brief_hour:
        week = monday
    elif now_local.weekday() == 0 and now_local.hour < 12:
        week = monday - timedelta(weeks=1)
    else:
        return None
    key = week.isoformat()
    return None if last_sent == key else key


def send_brief(settings: Settings, title: str, body: str, client: Optional[httpx.Client] = None) -> bool:
    payload: dict[str, Any] = {"topic": settings.ntfy_topic, "title": title, "message": body, "tags": ["chess_pawn"]}
    if settings.brief_click_url:
        payload["click"] = settings.brief_click_url
    headers = {"Authorization": f"Bearer {settings.ntfy_token}"} if settings.ntfy_token else {}
    own = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        response = client.post(settings.ntfy_url.rstrip("/") + "/", content=json.dumps(payload).encode(),
                               headers=headers)
        if response.status_code >= 300:
            log.warning("Wochenbrief: ntfy antwortet %s", response.status_code)
            return False
        return True
    except httpx.HTTPError as exc:
        log.warning("Wochenbrief: ntfy nicht erreichbar: %s", exc)
        return False
    finally:
        if own:
            client.close()


def scope_of(settings: Settings) -> tuple[Optional[str], Optional[str]]:
    """Der Brief folgt dem Trainingsbereich, wenn er genau eine Partieart und
    eine Plattform nennt - sonst alle Partien."""
    tc = settings.training_time_classes[0] if len(settings.training_time_classes) == 1 else None
    pf = settings.training_platforms[0] if len(settings.training_platforms) == 1 else None
    return tc, pf


def maybe_send_brief(session: Session, settings: Settings, now: Optional[datetime] = None,
                     client: Optional[httpx.Client] = None) -> Optional[str]:
    """Schickt den Brief, wenn er faellig ist. Gibt die Woche zurueck oder None."""
    if not settings.brief_enabled:
        return None
    zone = _zone(settings.study_timezone)
    now_local = _local(now or utc_now(), zone)
    week = brief_due(settings, now_local, _state_get(session, BRIEF_STATE_KEY))
    if week is None:
        return None
    tc, pf = scope_of(settings)
    data = journey(session, settings, time_class=tc, platform=pf, today=date.fromisoformat(week) + timedelta(days=6))
    title, body = brief_text(data, settings.study_lang)
    if send_brief(settings, title, body, client):
        _state_set(session, BRIEF_STATE_KEY, week)
        log.info("Wochenbrief verschickt (%s)", week)
        return week
    return None


_brief_thread: Optional[threading.Thread] = None


def start_brief_loop(settings: Settings, stop: threading.Event) -> None:
    """Prueft alle zehn Minuten, ob der Brief faellig ist. Leichtgewichtig,
    eigener Thread - haengt nicht am Analyse-Durchlauf."""
    global _brief_thread
    if not settings.brief_enabled or (_brief_thread and _brief_thread.is_alive()):
        return

    def loop() -> None:
        from .db import new_session

        wait = 60.0  # erster Blick kurz nach dem Start, danach alle zehn Minuten
        while not stop.wait(wait):
            wait = 600.0
            try:
                with new_session() as session:
                    maybe_send_brief(session, settings)
            except Exception:  # noqa: BLE001 - der Brief darf nie etwas kippen
                log.exception("Wochenbrief fehlgeschlagen")

    _brief_thread = threading.Thread(target=loop, name="knightmare-brief", daemon=True)
    _brief_thread.start()
    log.info("Wochenbrief aktiv: sonntags ab %s Uhr an %s/%s", settings.brief_hour, settings.ntfy_url,
             settings.ntfy_topic)
