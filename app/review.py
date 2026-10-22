"""Bericht zu einer einzelnen Partie: Eroeffnung, Mittelspiel, Endspiel.

Gemessen wird mit der Genauigkeit nach der Lichess-Formel: jeder eigene Zug
bekommt aus dem Verlust an Gewinnwahrscheinlichkeit einen Wert zwischen 0
und 100, die Phase den Mittelwert. Das ist unabhaengig vom Massstab der
Fehlereinstufung (Centipawn oder Gewinnwahrscheinlichkeit) und braucht keine
Engine - `win_loss` liegt fuer jeden Zug in der Datenbank.

Die Note einer Phase ist bewusst *relativ*: verglichen mit den eigenen
Partien derselben Partieart. "Stark" heisst "besser als zwei Drittel deiner
Rapid-Eroeffnungen", nicht eine Zahl, die jemand fuer gut erklaert hat - eine
feste Schwelle waere geraten, und ein 1200er und ein 2000er braeuchten
verschiedene. Gibt es zu wenig Vergleichspartien, gibt es keine Note.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import timedelta
from typing import Any, Iterable, Optional, Sequence

from sqlmodel import Session, select

from .analysis import ERROR_LABELS, MISSED_LABELS, win_percent
from .models import ChessGame, ChessMove, utc_now

PHASES = ("opening", "middlegame", "endgame")

# Mindestens so viele eigene Zuege, damit eine Phase eine Genauigkeit bekommt.
# Zwei Endspielzuege vor der Aufgabe sagen nichts ueber das Endspiel.
MIN_PHASE_MOVES = 3
# Mindestens so viele Vergleichspartien fuer eine Note.
MIN_REFERENCE = 10
# Drittel: unteres = schwach, oberes = stark.
GRADE_LOW = 100.0 / 3
GRADE_HIGH = 200.0 / 3
# Ab wie vielen Prozentpunkten ein Gegnerzug als Geschenk zaehlt - dieselbe
# Schwelle wie eine eigene Ungenauigkeit nach Gewinnwahrscheinlichkeit.
OPPONENT_GIFT = 10.0


def move_accuracy(win_loss: Optional[float]) -> float:
    """Lichess: 103.1668 * exp(-0.04354 * Verlust) - 3.1669 (+1 Unschaerfe),
    begrenzt auf 0..100. 0 Verlust = 100, 10 Punkte = 65, 30 Punkte = 26."""
    loss = max(0.0, float(win_loss or 0.0))
    value = 103.1668 * math.exp(-0.04354 * loss) - 3.1669 + 1.0
    return max(0.0, min(100.0, value))


def _mean(values: Sequence[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _round(value: Optional[float], digits: int = 1) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def phase_accuracies(moves: Iterable[ChessMove]) -> dict[str, Optional[float]]:
    """Phase -> Genauigkeit (None, wenn zu wenige Zuege in der Phase)."""
    grouped: dict[str, list[float]] = defaultdict(list)
    for move in moves:
        grouped[move.phase].append(move_accuracy(move.win_loss))
    return {
        phase: (_mean(grouped[phase]) if len(grouped[phase]) >= MIN_PHASE_MOVES else None)
        for phase in PHASES
    }


def percentile(sorted_values: Sequence[float], value: float) -> float:
    """Anteil der Vergleichswerte unter `value`, Gleichstand halb gezaehlt."""
    if not sorted_values:
        return 50.0
    below = bisect_left(sorted_values, value)
    equal = bisect_right(sorted_values, value) - below
    return (below + 0.5 * equal) / len(sorted_values) * 100.0


def grade_for(pct: Optional[float]) -> Optional[str]:
    if pct is None:
        return None
    if pct >= GRADE_HIGH:
        return "strong"
    if pct <= GRADE_LOW:
        return "weak"
    return "average"


def _moves_by_game(session: Session, game_ids: Sequence[int]) -> dict[int, list[ChessMove]]:
    result: dict[int, list[ChessMove]] = defaultdict(list)
    ids = list(game_ids)
    # In Bloecken, damit die IN-Liste auch bei vielen Partien klein bleibt.
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        statement = select(ChessMove).where(ChessMove.game_id.in_(chunk))  # type: ignore[attr-defined]
        for move in session.exec(statement).all():
            result[move.game_id].append(move)
    for moves in result.values():
        moves.sort(key=lambda m: m.ply)
    return result


def reference(
    session: Session, time_class: Optional[str], platform: Optional[str] = None
) -> dict[str, list[float]]:
    """Phase -> sortierte Genauigkeiten aller analysierten Partien dieser
    Partieart. Der Massstab, an dem eine einzelne Partie gemessen wird."""
    statement = select(ChessMove)
    if time_class:
        statement = statement.where(ChessMove.time_class == time_class)
    if platform:
        statement = statement.where(ChessMove.platform == platform)
    by_game: dict[int, list[ChessMove]] = defaultdict(list)
    for move in session.exec(statement).all():
        by_game[move.game_id].append(move)
    values: dict[str, list[float]] = {phase: [] for phase in PHASES}
    for moves in by_game.values():
        for phase, acc in phase_accuracies(moves).items():
            if acc is not None:
                values[phase].append(acc)
    for phase in PHASES:
        values[phase].sort()
    return values


def _phase_block(phase: str, moves: list[ChessMove], ref: list[float]) -> dict[str, Any]:
    items = [m for m in moves if m.phase == phase]
    if not items:
        return {"phase": phase, "reached": False, "moves": 0}
    accs = [move_accuracy(m.win_loss) for m in items]
    acc = _mean(accs) if len(items) >= MIN_PHASE_MOVES else None
    pct = percentile(ref, acc) if (acc is not None and len(ref) >= MIN_REFERENCE) else None
    median = ref[len(ref) // 2] if len(ref) >= MIN_REFERENCE else None
    first, last = items[0], items[-1]
    return {
        "phase": phase,
        "reached": True,
        "moves": len(items),
        "from_move": first.move_number,
        "to_move": last.move_number,
        "accuracy": _round(acc),
        "percentile": _round(pct, 0),
        "grade": grade_for(pct),
        "your_median": _round(median),
        "reference_games": len(ref),
        "blunders": sum(1 for m in items if m.category == "blunder"),
        "mistakes": sum(1 for m in items if m.category == "mistake"),
        "inaccuracies": sum(1 for m in items if m.category == "inaccuracy"),
        # Gewinnchance beim Eintritt in die Phase und nach dem letzten eigenen
        # Zug darin - "aus der Eroeffnung mit 62 % herausgekommen".
        "win_start": _round(win_percent(first.cp_before)),
        "win_end": _round(win_percent(last.cp_after)),
        "cost": _round(sum(m.win_loss or 0.0 for m in items)),
    }


def _error_row(move: ChessMove) -> dict[str, Any]:
    return {
        "id": move.id,
        "ply": move.ply,
        "move_number": move.move_number,
        "san": move.san,
        "color": move.color,
        "phase": move.phase,
        "category": move.category,
        "error_type": move.error_type,
        "label": ERROR_LABELS.get(move.error_type or "", None),
        "missed_motif": move.missed_motif,
        "missed_label": MISSED_LABELS.get(move.missed_motif or "", None),
        "mate_in": move.mate_in if (move.mate_in or 0) > 0 else None,
        "cp_loss": move.cp_loss,
        "win_loss": move.win_loss,
        "win_before": _round(win_percent(move.cp_before)),
        "win_after": _round(win_percent(move.cp_after)),
        "best_move_san": move.best_move_san,
        "refutation_san": move.refutation_san,
        "clock_seconds": move.clock_seconds,
        "seconds_spent": move.seconds_spent,
    }


def _curve(moves: list[ChessMove]) -> list[dict[str, Any]]:
    """Gewinnchance aus eigener Sicht ueber die Partie: vor und nach jedem
    eigenen Zug. Gegnerzuege stecken im Sprung zwischen zwei eigenen."""
    points: list[dict[str, Any]] = []
    for move in moves:
        points.append({"ply": move.ply - 1, "win": _round(win_percent(move.cp_before)), "phase": move.phase})
        points.append(
            {
                "ply": move.ply,
                "win": _round(win_percent(move.cp_after)),
                "phase": move.phase,
                "category": move.category if move.category != "ok" else None,
                "san": move.san,
                "move_number": move.move_number,
            }
        )
    return points


def _opponent_gifts(moves: list[ChessMove]) -> list[dict[str, Any]]:
    """Gegnerfehler: die Gewinnchance springt zwischen zwei eigenen Zuegen um
    mindestens OPPONENT_GIFT nach oben. Dazu, ob der eigene Folgezug das
    Geschenk behalten hat."""
    gifts: list[dict[str, Any]] = []
    for prev, nxt in zip(moves, moves[1:]):
        if nxt.ply != prev.ply + 2:
            continue
        before = win_percent(prev.cp_after)
        after = win_percent(nxt.cp_before)
        gain = after - before
        if gain < OPPONENT_GIFT:
            continue
        gifts.append(
            {
                "ply": prev.ply + 1,
                "move_number": (prev.ply + 2) // 2,
                "gain": _round(gain),
                "win_after": _round(after),
                # Behalten, wenn der eigene Antwortzug weniger als die Haelfte
                # wieder hergegeben hat.
                "used": (nxt.win_loss or 0.0) < gain / 2,
                "reply_san": nxt.san,
                "reply_id": nxt.id,
                "reply_best": nxt.best_move_san,
            }
        )
    return gifts


def game_report(session: Session, game_id: int) -> dict[str, Any]:
    game = session.get(ChessGame, game_id)
    if game is None:
        return {"found": False}
    moves = sorted(
        session.exec(select(ChessMove).where(ChessMove.game_id == game_id)).all(),
        key=lambda m: m.ply,
    )
    ref = reference(session, game.time_class) if moves else {phase: [] for phase in PHASES}
    phases = [_phase_block(phase, moves, ref[phase]) for phase in PHASES]
    graded = [p for p in phases if p.get("accuracy") is not None]
    worst = min(graded, key=lambda p: (p["percentile"] if p["percentile"] is not None else p["accuracy"]), default=None)
    best = max(graded, key=lambda p: (p["percentile"] if p["percentile"] is not None else p["accuracy"]), default=None)
    errors = [_error_row(m) for m in moves if m.category != "ok"]
    turning = max(moves, key=lambda m: m.win_loss or 0.0, default=None)
    return {
        "found": True,
        "game": {
            "id": game.id,
            "url": game.url,
            "platform": game.platform,
            "played_at": game.played_at.isoformat(),
            "opponent": game.opponent,
            "opponent_rating": game.opponent_rating,
            "my_rating": game.my_rating,
            "color": game.color,
            "result": game.result,
            "termination": game.termination,
            "time_class": game.time_class,
            "time_control": game.time_control,
            "opening": game.opening_family or game.opening_name,
            "opening_name": game.opening_name,
            "eco": game.eco,
            "move_count": game.move_count,
            "analyzed": game.analyzed_at is not None,
            "analysis_error": game.analysis_error,
            "accuracy_platform": game.accuracy_self,
        },
        "accuracy": _round(_mean([move_accuracy(m.win_loss) for m in moves])),
        "phases": phases,
        "best_phase": best["phase"] if best and worst and best is not worst else None,
        "worst_phase": worst["phase"] if best and worst and best is not worst else None,
        "curve": _curve(moves),
        "errors": errors,
        "turning_point": _error_row(turning) if turning is not None and (turning.win_loss or 0) >= OPPONENT_GIFT else None,
        "opponent_gifts": _opponent_gifts(moves),
        "max_win": _round(max((win_percent(m.cp_before) for m in moves), default=0.0)),
    }


def game_list(
    session: Session,
    limit: int = 30,
    offset: int = 0,
    days: Optional[int] = None,
    time_class: Optional[str] = None,
    platform: Optional[str] = None,
) -> dict[str, Any]:
    """Partien mit Phasen-Noten fuer die Auswahl. Die Noten kommen aus
    denselben Vergleichswerten wie im Bericht, damit Liste und Bericht nie
    verschiedene Farben zeigen."""
    statement = select(ChessGame)
    if days:
        statement = statement.where(ChessGame.played_at >= utc_now() - timedelta(days=days))
    if time_class:
        statement = statement.where(ChessGame.time_class == time_class)
    if platform:
        statement = statement.where(ChessGame.platform == platform)
    all_games = list(
        session.exec(statement.order_by(ChessGame.played_at.desc())).all()  # type: ignore[union-attr]
    )
    page = all_games[offset:offset + limit]
    moves = _moves_by_game(session, [g.id for g in page if g.id is not None])
    refs: dict[Optional[str], dict[str, list[float]]] = {}
    rows = []
    for game in page:
        if game.time_class not in refs:
            refs[game.time_class] = reference(session, game.time_class)
        ref = refs[game.time_class]
        own = moves.get(game.id or -1, [])
        accs = phase_accuracies(own)
        phase_rows = []
        for phase in PHASES:
            acc = accs[phase]
            pct = percentile(ref[phase], acc) if acc is not None and len(ref[phase]) >= MIN_REFERENCE else None
            phase_rows.append({"phase": phase, "accuracy": _round(acc), "grade": grade_for(pct)})
        rows.append(
            {
                "id": game.id,
                "played_at": game.played_at.isoformat(),
                "url": game.url,
                "platform": game.platform,
                "time_class": game.time_class,
                "color": game.color,
                "result": game.result,
                "opponent": game.opponent,
                "opponent_rating": game.opponent_rating,
                "my_rating": game.my_rating,
                "opening": game.opening_family or game.opening_name,
                "analyzed": game.analyzed_at is not None,
                "analysis_error": game.analysis_error,
                "accuracy": _round(_mean([move_accuracy(m.win_loss) for m in own])),
                "blunders": game.blunders,
                "mistakes": game.mistakes,
                "inaccuracies": game.inaccuracies,
                "phases": phase_rows,
            }
        )
    return {"games": rows, "total": len(all_games), "offset": offset, "limit": limit}
