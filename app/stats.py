"""Auswertungen ueber die analysierten Partien.

Absichtlich in Python statt in SQL aggregiert: die Datenmengen sind klein
(einige tausend Zeilen), dafuer verhaelt sich alles unter Postgres und SQLite
identisch und laesst sich ohne Datenbank testen.

Jede Auswertung nimmt `time_class` entgegen, damit sich alles auf eine
Partieart eingrenzen laesst - Rapid-Fehler unterscheiden sich deutlich von
Bullet-Fehlern, und beides zusammen zu mitteln verwischt genau das.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional, Sequence

from sqlmodel import Session, select

from .analysis import ERROR_LABELS
from .models import ChessGame, ChessMove

SCORE_BY_RESULT = {"win": 1.0, "draw": 0.5, "loss": 0.0}

# Restzeit-Klassen fuer die Zeitdruck-Auswertung (obere Grenze in Sekunden).
# Die Namen sind bewusst sprachneutrale Schluessel - beschriftet wird in der
# Oberflaeche, sonst liesse sich die Ausgabe nicht uebersetzen.
CLOCK_BUCKETS: tuple[tuple[str, float], ...] = (
    ("under_10s", 10.0),
    ("10_30s", 30.0),
    ("30_60s", 60.0),
    ("1_3min", 180.0),
    ("over_3min", float("inf")),
)

ERROR_CATEGORIES = ("blunder", "mistake", "inaccuracy")


def _round(value: Optional[float], digits: int = 1) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), digits)


def _mean(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _score_of(games: Iterable[ChessGame]) -> tuple[int, int, int, Optional[float]]:
    wins = draws = losses = 0
    for game in games:
        if game.result == "win":
            wins += 1
        elif game.result == "draw":
            draws += 1
        elif game.result == "loss":
            losses += 1
    total = wins + draws + losses
    percent = ((wins + 0.5 * draws) / total * 100.0) if total else None
    return wins, draws, losses, percent


def _analyzed(games: Iterable[ChessGame]) -> list[ChessGame]:
    return [game for game in games if game.analyzed_at is not None and game.acpl is not None]


def load_games(
    session: Session,
    days: Optional[int] = None,
    time_class: Optional[str] = None,
    color: Optional[str] = None,
    analyzed_only: bool = False,
) -> list[ChessGame]:
    statement = select(ChessGame)
    if days:
        since = datetime.utcnow() - timedelta(days=days)
        statement = statement.where(ChessGame.played_at >= since)
    if time_class:
        statement = statement.where(ChessGame.time_class == time_class)
    if color in {"white", "black"}:
        statement = statement.where(ChessGame.color == color)
    if analyzed_only:
        statement = statement.where(ChessGame.analyzed_at.is_not(None))  # type: ignore[union-attr]
    statement = statement.order_by(ChessGame.played_at.desc())  # type: ignore[union-attr]
    return list(session.exec(statement).all())


def load_moves(
    session: Session,
    days: Optional[int] = None,
    time_class: Optional[str] = None,
    errors_only: bool = False,
    with_clock: bool = False,
) -> list[ChessMove]:
    statement = select(ChessMove)
    if days:
        since = datetime.utcnow() - timedelta(days=days)
        statement = statement.where(ChessMove.played_at >= since)
    if time_class:
        statement = statement.where(ChessMove.time_class == time_class)
    if errors_only:
        statement = statement.where(ChessMove.category != "ok")
    if with_clock:
        statement = statement.where(ChessMove.clock_seconds.is_not(None))  # type: ignore[union-attr]
    return list(session.exec(statement).all())


def available_time_classes(session: Session) -> list[dict[str, Any]]:
    """Welche Partiearten liegen ueberhaupt vor - fuer die Filterleiste."""
    games = list(session.exec(select(ChessGame)).all())
    counter: Counter[str] = Counter(game.time_class for game in games)
    return [
        {"time_class": name, "games": count}
        for name, count in counter.most_common()
    ]


# --------------------------------------------------------------------------
# Uebersicht
# --------------------------------------------------------------------------
def overview(
    session: Session,
    days: Optional[int] = None,
    time_class: Optional[str] = None,
) -> dict[str, Any]:
    games = load_games(session, days=days, time_class=time_class)
    analyzed = _analyzed(games)
    wins, draws, losses, percent = _score_of(games)

    by_time_class: list[dict[str, Any]] = []
    grouped: dict[str, list[ChessGame]] = defaultdict(list)
    for game in games:
        grouped[game.time_class].append(game)

    for name, items in sorted(grouped.items(), key=lambda pair: -len(pair[1])):
        klass_analyzed = _analyzed(items)
        k_wins, k_draws, k_losses, k_percent = _score_of(items)
        latest_rating = next(
            (item.my_rating for item in items if item.my_rating is not None), None
        )
        by_time_class.append(
            {
                "time_class": name,
                "games": len(items),
                "wins": k_wins,
                "draws": k_draws,
                "losses": k_losses,
                "score_percent": _round(k_percent),
                "acpl": _round(_mean([item.acpl for item in klass_analyzed if item.acpl is not None])),
                "rating": latest_rating,
            }
        )

    total_blunders = sum(game.blunders for game in analyzed)
    total_mistakes = sum(game.mistakes for game in analyzed)
    total_inaccuracies = sum(game.inaccuracies for game in analyzed)

    pending = len([game for game in games if game.analyzed_at is None and game.analysis_error is None])
    failed = len([game for game in games if game.analysis_error is not None])

    return {
        "time_class": time_class or "all",
        "games": len(games),
        "analyzed": len(analyzed),
        "pending": pending,
        "failed": failed,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "score_percent": _round(percent),
        "acpl": _round(_mean([game.acpl for game in analyzed if game.acpl is not None])),
        "blunders_per_game": _round(total_blunders / len(analyzed), 2) if analyzed else None,
        "mistakes_per_game": _round(total_mistakes / len(analyzed), 2) if analyzed else None,
        "inaccuracies_per_game": _round(total_inaccuracies / len(analyzed), 2) if analyzed else None,
        "accuracy_chesscom": _round(
            _mean([game.accuracy_self for game in games if game.accuracy_self is not None])
        ),
        "rating": next((game.my_rating for game in games if game.my_rating is not None), None),
        "first_game_at": games[-1].played_at.isoformat() if games else None,
        "last_game_at": games[0].played_at.isoformat() if games else None,
        "by_time_class": by_time_class,
    }


# --------------------------------------------------------------------------
# Fehlerarten
# --------------------------------------------------------------------------
def error_types(
    session: Session,
    days: Optional[int] = None,
    time_class: Optional[str] = None,
    examples_per_type: int = 3,
) -> dict[str, Any]:
    """Welche Art von Fehler passiert wie oft - die Kategorie zum Ueben."""
    moves = load_moves(session, days=days, time_class=time_class, errors_only=True)
    if not moves:
        return {"time_class": time_class or "all", "errors_total": 0, "types": []}

    grouped: dict[str, list[ChessMove]] = defaultdict(list)
    for move in moves:
        grouped[move.error_type or "positional"].append(move)

    # Beispiele brauchen die Partie-URL - alle nötigen Partien auf einmal holen.
    example_moves: list[ChessMove] = []
    for items in grouped.values():
        example_moves.extend(
            sorted(items, key=lambda item: item.cp_loss, reverse=True)[:examples_per_type]
        )
    game_ids = {move.game_id for move in example_moves}
    games_by_id: dict[int, ChessGame] = {}
    if game_ids:
        found = session.exec(
            select(ChessGame).where(ChessGame.id.in_(game_ids))  # type: ignore[union-attr]
        ).all()
        games_by_id = {game.id: game for game in found if game.id is not None}

    rows: list[dict[str, Any]] = []
    for name, items in grouped.items():
        phases_counter: Counter[str] = Counter(item.phase for item in items)
        clocks = [item.clock_seconds for item in items if item.clock_seconds is not None]
        examples = sorted(items, key=lambda item: item.cp_loss, reverse=True)[:examples_per_type]

        rows.append(
            {
                "error_type": name,
                "label": ERROR_LABELS.get(name, name),
                "count": len(items),
                "share_percent": _round(len(items) / len(moves) * 100, 1),
                "avg_cp_loss": _round(_mean([item.cp_loss for item in items]), 0),
                "blunders": sum(1 for item in items if item.category == "blunder"),
                "mistakes": sum(1 for item in items if item.category == "mistake"),
                "inaccuracies": sum(1 for item in items if item.category == "inaccuracy"),
                "main_phase": phases_counter.most_common(1)[0][0] if phases_counter else None,
                "avg_clock_seconds": _round(_mean(clocks), 0) if clocks else None,
                "examples": [
                    {
                        "move_number": item.move_number,
                        "san": item.san,
                        "color": item.color,
                        "cp_loss": item.cp_loss,
                        "phase": item.phase,
                        "best_move_san": item.best_move_san,
                        "refutation_san": item.refutation_san,
                        "clock_seconds": item.clock_seconds,
                        "game_url": (
                            games_by_id[item.game_id].url
                            if item.game_id in games_by_id
                            else None
                        ),
                        "opponent": (
                            games_by_id[item.game_id].opponent
                            if item.game_id in games_by_id
                            else None
                        ),
                    }
                    for item in examples
                ],
            }
        )

    rows.sort(key=lambda row: -row["count"])
    return {
        "time_class": time_class or "all",
        "errors_total": len(moves),
        "types": rows,
    }


def _move_row(move: ChessMove, game: Optional[ChessGame]) -> dict[str, Any]:
    return {
        "id": move.id,
        "game_id": move.game_id,
        "ply": move.ply,
        "move_number": move.move_number,
        "san": move.san,
        "color": move.color,
        "phase": move.phase,
        "category": move.category,
        "error_type": move.error_type,
        "label": ERROR_LABELS.get(move.error_type or "", None),
        "cp_loss": move.cp_loss,
        "win_loss": move.win_loss,
        "best_move_san": move.best_move_san,
        "refutation_san": move.refutation_san,
        "clock_seconds": move.clock_seconds,
        "seconds_spent": move.seconds_spent,
        "played_at": move.played_at.isoformat(),
        "time_class": move.time_class,
        "game_url": game.url if game else None,
        "opponent": game.opponent if game else None,
        "opponent_rating": game.opponent_rating if game else None,
        "result": game.result if game else None,
        "opening": (game.opening_family or game.opening_name) if game else None,
    }


def error_moves(
    session: Session,
    error_type: Optional[str] = None,
    days: Optional[int] = None,
    time_class: Optional[str] = None,
    phase: Optional[str] = None,
    category: Optional[str] = None,
    sort: str = "cp_loss",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Alle Fehlerzuege einer Art - zum Durchgehen und Lernen, nicht als Stichprobe.

    Blaettert ueber limit/offset, weil eine haeufige Fehlerart schnell
    dreistellig wird.
    """
    conditions = [ChessMove.category != "ok"]
    if error_type:
        conditions.append(ChessMove.error_type == error_type)
    if days:
        since = datetime.utcnow() - timedelta(days=days)
        conditions.append(ChessMove.played_at >= since)
    if time_class:
        conditions.append(ChessMove.time_class == time_class)
    if phase:
        conditions.append(ChessMove.phase == phase)
    if category:
        conditions.append(ChessMove.category == category)

    # Gesamtzahl ueber die IDs statt ueber COUNT(*): bei einigen tausend
    # Fehlerzuegen kostet das nichts und verhaelt sich unter jedem Dialekt
    # gleich - dieselbe Ueberlegung wie beim Rest dieses Moduls.
    total = len(session.exec(select(ChessMove.id).where(*conditions)).all())

    statement = select(ChessMove).where(*conditions)
    if sort == "recent":
        statement = statement.order_by(
            ChessMove.played_at.desc(), ChessMove.ply  # type: ignore[union-attr]
        )
    else:
        statement = statement.order_by(
            ChessMove.cp_loss.desc(), ChessMove.played_at.desc()  # type: ignore[union-attr]
        )
    moves = list(session.exec(statement.offset(offset).limit(limit)).all())

    # Partie-Kontext (Gegner, Datum, Link) in einer einzigen Abfrage nachladen.
    games_by_id: dict[int, ChessGame] = {}
    game_ids = {move.game_id for move in moves}
    if game_ids:
        found = session.exec(
            select(ChessGame).where(ChessGame.id.in_(game_ids))  # type: ignore[union-attr]
        ).all()
        games_by_id = {game.id: game for game in found if game.id is not None}

    return {
        "error_type": error_type,
        "label": ERROR_LABELS.get(error_type or "", None),
        "time_class": time_class or "all",
        "sort": "recent" if sort == "recent" else "cp_loss",
        "total": total,
        "offset": offset,
        "limit": limit,
        "returned": len(moves),
        "moves": [_move_row(move, games_by_id.get(move.game_id)) for move in moves],
    }


# --------------------------------------------------------------------------
# Eroeffnungen
# --------------------------------------------------------------------------
def openings(
    session: Session,
    color: Optional[str] = None,
    days: Optional[int] = None,
    time_class: Optional[str] = None,
    min_games: int = 3,
    limit: int = 20,
) -> dict[str, Any]:
    games = load_games(session, days=days, time_class=time_class, color=color)
    grouped: dict[str, list[ChessGame]] = defaultdict(list)
    for game in games:
        key = game.opening_family or game.opening_name or game.eco or "unbekannt"
        grouped[key].append(game)

    rows: list[dict[str, Any]] = []
    for name, items in grouped.items():
        if len(items) < min_games:
            continue
        analyzed = _analyzed(items)
        wins, draws, losses, percent = _score_of(items)
        rows.append(
            {
                "opening": name,
                "eco": next((item.eco for item in items if item.eco), None),
                "games": len(items),
                "wins": wins,
                "draws": draws,
                "losses": losses,
                "score_percent": _round(percent),
                "acpl": _round(_mean([item.acpl for item in analyzed if item.acpl is not None])),
                "acpl_opening": _round(
                    _mean([item.acpl_opening for item in analyzed if item.acpl_opening is not None])
                ),
                "blunders_per_game": _round(
                    sum(item.blunders for item in analyzed) / len(analyzed), 2
                )
                if analyzed
                else None,
                "example_url": next((item.url for item in items if item.url), None),
            }
        )

    # Schlechtestes Ergebnis zuerst - das ist die interessante Richtung.
    rows.sort(
        key=lambda row: (
            row["score_percent"] if row["score_percent"] is not None else 50.0,
            -row["games"],
        )
    )
    return {
        "color": color or "all",
        "time_class": time_class or "all",
        "min_games": min_games,
        "openings": rows[:limit],
        "total_groups": len(rows),
    }


# --------------------------------------------------------------------------
# Spielphasen
# --------------------------------------------------------------------------
def phases(
    session: Session,
    days: Optional[int] = None,
    time_class: Optional[str] = None,
) -> dict[str, Any]:
    moves = load_moves(session, days=days, time_class=time_class)

    grouped: dict[str, list[ChessMove]] = defaultdict(list)
    for move in moves:
        grouped[move.phase].append(move)

    rows: list[dict[str, Any]] = []
    for phase in ("opening", "middlegame", "endgame"):
        items = grouped.get(phase, [])
        if not items:
            rows.append(
                {
                    "phase": phase,
                    "moves": 0,
                    "acpl": None,
                    "blunders": 0,
                    "mistakes": 0,
                    "inaccuracies": 0,
                    "blunders_per_100": None,
                    "errors_per_100": None,
                }
            )
            continue
        blunders = sum(1 for item in items if item.category == "blunder")
        mistakes = sum(1 for item in items if item.category == "mistake")
        inaccuracies = sum(1 for item in items if item.category == "inaccuracy")
        rows.append(
            {
                "phase": phase,
                "moves": len(items),
                "acpl": _round(_mean([item.cp_loss for item in items])),
                "blunders": blunders,
                "mistakes": mistakes,
                "inaccuracies": inaccuracies,
                "blunders_per_100": _round(blunders / len(items) * 100, 2),
                "errors_per_100": _round((blunders + mistakes) / len(items) * 100, 2),
            }
        )
    return {"time_class": time_class or "all", "phases": rows, "moves_total": len(moves)}


# --------------------------------------------------------------------------
# Zeitdruck
# --------------------------------------------------------------------------
def time_pressure(
    session: Session,
    days: Optional[int] = None,
    time_class: Optional[str] = None,
) -> dict[str, Any]:
    moves = load_moves(session, days=days, time_class=time_class, with_clock=True)
    # Daily-Partien haben Restzeiten in Tagen - das verzerrt die Klassen.
    moves = [move for move in moves if move.time_class != "daily"]

    buckets: dict[str, list[ChessMove]] = {name: [] for name, _ in CLOCK_BUCKETS}
    for move in moves:
        remaining = move.clock_seconds
        if remaining is None:
            continue
        for name, upper in CLOCK_BUCKETS:
            if remaining < upper:
                buckets[name].append(move)
                break

    rows: list[dict[str, Any]] = []
    for name, _ in CLOCK_BUCKETS:
        items = buckets[name]
        blunders = sum(1 for item in items if item.category == "blunder")
        errors = sum(1 for item in items if item.category in {"blunder", "mistake"})
        rows.append(
            {
                "bucket": name,
                "moves": len(items),
                "acpl": _round(_mean([item.cp_loss for item in items])) if items else None,
                "blunders": blunders,
                "blunders_per_100": _round(blunders / len(items) * 100, 2) if items else None,
                "errors_per_100": _round(errors / len(items) * 100, 2) if items else None,
            }
        )

    # Lange Bedenkzeit fuer einen Zug ist oft ein Zeichen fuer eine kritische
    # Stellung - und nicht selten geht genau dort etwas schief.
    long_thinks = [
        move for move in moves if move.seconds_spent is not None and move.seconds_spent >= 20
    ]
    long_think_stats = None
    if long_thinks:
        blunders = sum(1 for move in long_thinks if move.category == "blunder")
        long_think_stats = {
            "moves": len(long_thinks),
            "acpl": _round(_mean([move.cp_loss for move in long_thinks])),
            "blunders_per_100": _round(blunders / len(long_thinks) * 100, 2),
        }

    return {
        "time_class": time_class or "all",
        "buckets": rows,
        "moves_with_clock": len(moves),
        "long_thinks": long_think_stats,
    }


# --------------------------------------------------------------------------
# Wochenreport
# --------------------------------------------------------------------------
def weekly_report(session: Session, time_class: Optional[str] = None) -> dict[str, Any]:
    now = datetime.utcnow()
    this_week_start = now - timedelta(days=7)
    last_week_start = now - timedelta(days=14)

    all_games = load_games(session, days=14, time_class=time_class)
    this_week = [game for game in all_games if game.played_at >= this_week_start]
    last_week = [
        game for game in all_games if last_week_start <= game.played_at < this_week_start
    ]

    def summarise(games: list[ChessGame]) -> dict[str, Any]:
        analyzed = _analyzed(games)
        wins, draws, losses, percent = _score_of(games)
        return {
            "games": len(games),
            "wins": wins,
            "draws": draws,
            "losses": losses,
            "score_percent": _round(percent),
            "acpl": _round(_mean([game.acpl for game in analyzed if game.acpl is not None])),
            "blunders_per_game": _round(
                sum(game.blunders for game in analyzed) / len(analyzed), 2
            )
            if analyzed
            else None,
        }

    current = summarise(this_week)
    previous = summarise(last_week)

    # Auffaelligste Eroeffnung der Woche: mindestens zwei Partien, schlechtestes Ergebnis.
    grouped: dict[str, list[ChessGame]] = defaultdict(list)
    for game in this_week:
        key = game.opening_family or game.opening_name or game.eco or "unbekannt"
        grouped[key].append(game)
    worst_opening = None
    candidates = [(name, items) for name, items in grouped.items() if len(items) >= 2]
    if candidates:

        def _rank(pair: tuple[str, list[ChessGame]]) -> tuple[float, int]:
            # Achtung: nicht "or 50.0" - eine Ausbeute von 0% ist ein echter
            # Wert und genau der interessante Fall.
            percent = _score_of(pair[1])[3]
            return (percent if percent is not None else 50.0, -len(pair[1]))

        name, items = min(candidates, key=_rank)
        wins, draws, losses, percent = _score_of(items)
        worst_opening = {
            "opening": name,
            "games": len(items),
            "score_percent": _round(percent),
            "wins": wins,
            "draws": draws,
            "losses": losses,
        }

    # Haeufigste Fehlerart der Woche und groesster Patzer.
    top_error = None
    worst_move = None
    game_ids = [game.id for game in this_week if game.id is not None]
    if game_ids:
        week_moves = list(
            session.exec(
                select(ChessMove)
                .where(ChessMove.game_id.in_(game_ids))  # type: ignore[union-attr]
                .where(ChessMove.category != "ok")
            ).all()
        )
        if week_moves:
            counter: Counter[str] = Counter(
                move.error_type or "positional" for move in week_moves
            )
            name, count = counter.most_common(1)[0]
            top_error = {
                "error_type": name,
                "label": ERROR_LABELS.get(name, name),
                "count": count,
                "share_percent": _round(count / len(week_moves) * 100, 1),
            }

            move = max(week_moves, key=lambda item: item.cp_loss)
            game = session.get(ChessGame, move.game_id)
            worst_move = {
                "san": move.san,
                "move_number": move.move_number,
                "cp_loss": move.cp_loss,
                "best_move_san": move.best_move_san,
                "refutation_san": move.refutation_san,
                "error_type": move.error_type,
                "error_label": ERROR_LABELS.get(move.error_type or "", None),
                "phase": move.phase,
                "clock_seconds": move.clock_seconds,
                "game_url": game.url if game else None,
                "opponent": game.opponent if game else None,
            }

    def delta(key: str) -> Optional[float]:
        left, right = current.get(key), previous.get(key)
        if left is None or right is None:
            return None
        return _round(left - right, 2)

    return {
        "time_class": time_class or "all",
        "period_start": this_week_start.isoformat(),
        "current": current,
        "previous": previous,
        "delta": {
            "score_percent": delta("score_percent"),
            "acpl": delta("acpl"),
            "blunders_per_game": delta("blunders_per_game"),
            "games": delta("games"),
        },
        "worst_opening": worst_opening,
        "top_error": top_error,
        "worst_move": worst_move,
    }


# --------------------------------------------------------------------------
# Einzelne Partien
# --------------------------------------------------------------------------
def recent_games(
    session: Session, limit: int = 20, time_class: Optional[str] = None
) -> dict[str, Any]:
    statement = select(ChessGame)
    if time_class:
        statement = statement.where(ChessGame.time_class == time_class)
    statement = statement.order_by(ChessGame.played_at.desc()).limit(limit)  # type: ignore[union-attr]
    games = list(session.exec(statement).all())
    return {
        "time_class": time_class or "all",
        "games": [
            {
                "id": game.id,
                "played_at": game.played_at.isoformat(),
                "url": game.url,
                "time_class": game.time_class,
                "color": game.color,
                "result": game.result,
                "opponent": game.opponent,
                "opponent_rating": game.opponent_rating,
                "my_rating": game.my_rating,
                "opening": game.opening_family or game.opening_name,
                "acpl": game.acpl,
                "blunders": game.blunders,
                "mistakes": game.mistakes,
                "inaccuracies": game.inaccuracies,
                "analyzed": game.analyzed_at is not None,
                "analysis_error": game.analysis_error,
            }
            for game in games
        ],
    }


def game_errors(session: Session, game_id: int, limit: int = 40) -> dict[str, Any]:
    game = session.get(ChessGame, game_id)
    if game is None:
        return {"found": False}
    statement = (
        select(ChessMove)
        .where(ChessMove.game_id == game_id)
        .where(ChessMove.category != "ok")
        .order_by(ChessMove.ply)  # type: ignore[arg-type]
        .limit(limit)
    )
    moves = list(session.exec(statement).all())
    return {
        "found": True,
        "game": {
            "id": game.id,
            "url": game.url,
            "played_at": game.played_at.isoformat(),
            "opponent": game.opponent,
            "color": game.color,
            "result": game.result,
            "time_class": game.time_class,
            "opening": game.opening_name,
            "acpl": game.acpl,
        },
        "moves": [
            {
                "ply": move.ply,
                "move_number": move.move_number,
                "san": move.san,
                "phase": move.phase,
                "category": move.category,
                "error_type": move.error_type,
                "error_label": ERROR_LABELS.get(move.error_type or "", None),
                "cp_loss": move.cp_loss,
                "win_loss": move.win_loss,
                "best_move_san": move.best_move_san,
                "refutation_san": move.refutation_san,
                "clock_seconds": move.clock_seconds,
                "seconds_spent": move.seconds_spent,
            }
            for move in moves
        ],
    }


def rating_history(
    session: Session, time_class: str = "rapid", limit: int = 120
) -> dict[str, Any]:
    statement = (
        select(ChessGame)
        .where(ChessGame.time_class == time_class)
        .where(ChessGame.my_rating.is_not(None))  # type: ignore[union-attr]
        .order_by(ChessGame.played_at.desc())  # type: ignore[union-attr]
        .limit(limit)
    )
    games = list(session.exec(statement).all())
    games.reverse()
    return {
        "time_class": time_class,
        "points": [
            {"played_at": game.played_at.isoformat(), "rating": game.my_rating}
            for game in games
        ],
    }
