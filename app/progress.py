"""Fortschritt ueber Monate: wirkt das Ueben?

Pro Kalendermonat: Genauigkeit je Phase (dieselbe Formel wie im
Partiebericht), Patzer pro Partie, wie oft eine klar gewonnene Stellung auch
gewonnen wurde, und - fuer die Eroeffnung - wie oft eine bekannte Gewohnheit
noch gespielt wurde, wenn ihre Stellung aufs Brett kam.

Monate mit weniger als MIN_GAMES analysierten Partien bekommen keine Werte:
drei Partien sind kein Trend.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional

from sqlmodel import Session, select

from .analysis import win_percent
from .models import ChessGame, ChessMove
from .review import PHASES, move_accuracy, phase_accuracies

MIN_GAMES = 5
WON_POSITION = 75.0


def _mean(values: list[float]) -> Optional[float]:
    return round(sum(values) / len(values), 1) if values else None


def progress(
    session: Session,
    time_class: Optional[str] = None,
    platform: Optional[str] = None,
    months: int = 12,
) -> dict[str, Any]:
    from . import repertoire

    statement = select(ChessGame).where(ChessGame.analyzed_at.is_not(None))  # type: ignore[union-attr]
    if time_class:
        statement = statement.where(ChessGame.time_class == time_class)
    if platform:
        statement = statement.where(ChessGame.platform == platform)
    games = list(session.exec(statement).all())
    ids = [g.id for g in games]
    moves: dict[int, list[ChessMove]] = defaultdict(list)
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        for move in session.exec(select(ChessMove).where(ChessMove.game_id.in_(chunk))).all():  # type: ignore[attr-defined]
            moves[move.game_id].append(move)

    by_month: dict[str, list[ChessGame]] = defaultdict(list)
    for game in games:
        by_month[game.played_at.strftime("%Y-%m")].append(game)

    # Gewohnheiten: jede Begegnung mit einer Gewohnheits-Stellung, nach Monat.
    habit: dict[str, dict[str, int]] = defaultdict(lambda: {"repeated": 0, "avoided": 0})
    leaks = repertoire.leaks(session, time_class=time_class, platform=platform, limit=500)["leaks"]
    for leak in leaks:
        for item in leak["history"]:
            month = item["played_at"][:7]
            habit[month]["repeated" if item["same"] else "avoided"] += 1

    rows = []
    for month in sorted(by_month)[-months:]:
        items = by_month[month]
        row: dict[str, Any] = {"month": month, "games": len(items), "enough": len(items) >= MIN_GAMES}
        scored = [g for g in items if g.result in ("win", "draw", "loss")]
        row["score"] = _mean([{"win": 100.0, "draw": 50.0, "loss": 0.0}[g.result] for g in scored])
        phase_values: dict[str, list[float]] = {p: [] for p in PHASES}
        overall: list[float] = []
        won_positions = converted = 0
        blunders = 0
        for game in items:
            own = moves.get(game.id or -1, [])
            for phase, acc in phase_accuracies(own).items():
                if acc is not None:
                    phase_values[phase].append(acc)
            if own:
                overall.append(sum(move_accuracy(m.win_loss) for m in own) / len(own))
                if max(win_percent(m.cp_before) for m in own) >= WON_POSITION:
                    won_positions += 1
                    converted += 1 if game.result == "win" else 0
            blunders += game.blunders or 0
        row["accuracy"] = _mean(overall) if row["enough"] else None
        for phase in PHASES:
            # Eine Phase braucht selbst genug Partien - Endspiele sind seltener.
            vals = phase_values[phase]
            row[phase] = _mean(vals) if len(vals) >= MIN_GAMES else None
            row[phase + "_games"] = len(vals)
        row["blunders_per_game"] = round(blunders / len(items), 2) if row["enough"] else None
        row["won_positions"] = won_positions
        row["converted"] = converted
        row["conversion"] = round(converted / won_positions * 100, 1) if won_positions >= 3 else None
        row["habit_repeated"] = habit[month]["repeated"]
        row["habit_avoided"] = habit[month]["avoided"]
        rows.append(row)
    return {"time_class": time_class or "all", "min_games": MIN_GAMES, "months": rows,
            "habits_total": len(leaks)}
