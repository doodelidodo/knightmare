"""Bringt mehr Rechenzeit fuer Stockfish etwas?

Messwerkzeug, kein Teil der App. Nimmt eine Stichprobe bereits analysierter
Partien, rechnet sie mit laengerer Zeit pro Stellung noch einmal durch - nur
im Speicher, an der Datenbank aendert sich nichts - und vergleicht Zug fuer
Zug mit dem, was gespeichert ist.

Auf tando im Container (dauert Minuten, laeuft im Vordergrund):
    curl -sL https://raw.githubusercontent.com/doodelidodo/knightmare/main/tools/engine_probe.py \\
      | docker compose exec -T chess python - --games 10 --movetime 0.6
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import random
import sys
import time

import chess

from app.analysis import EngineAnalyzer
from app.config import load_settings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--games", type=int, default=10)
    parser.add_argument("--movetime", type=float, default=0.6)
    parser.add_argument("--time-class", default="rapid")
    parser.add_argument("--seed", type=int, default=24)
    args = parser.parse_args()

    from sqlmodel import Session, select
    from app.db import engine
    from app.models import ChessGame, ChessMove

    base = load_settings()
    deep = dataclasses.replace(base, engine_movetime=args.movetime, engine_depth=0)

    with Session(engine) as session:
        statement = select(ChessGame).where(ChessGame.analyzed_at.is_not(None))
        if args.time_class:
            statement = statement.where(ChessGame.time_class == args.time_class)
        games = list(session.exec(statement).all())
        random.Random(args.seed).shuffle(games)
        games = games[: args.games]
        stored = {
            (m.game_id, m.ply): m
            for m in session.exec(
                select(ChessMove).where(ChessMove.game_id.in_([g.id for g in games]))
            ).all()
        }

    print(f"{len(games)} Partien, gespeichert mit {base.engine_movetime}s, "
          f"neu mit {deep.engine_movetime}s pro Stellung\n")

    same_category = 0
    compared = 0
    shifts: collections.Counter = collections.Counter()
    error_pairs = error_same = 0
    motif_pairs = motif_same = 0
    mate_changes: list[tuple] = []
    cp_diffs: list[int] = []
    started = time.time()

    with EngineAnalyzer(deep) as analyzer:
        for index, game in enumerate(games, start=1):
            color = chess.WHITE if game.color == "white" else chess.BLACK
            t0 = time.time()
            try:
                report = analyzer.analyse_game(game.pgn, color, game.time_control)
            except Exception as exc:  # noqa: BLE001
                print(f"  Partie {index}: nicht analysierbar ({exc})")
                continue
            for move in report.moves:
                old = stored.get((game.id, move.ply))
                if old is None:
                    continue
                compared += 1
                cp_diffs.append(abs(move.cp_loss - old.cp_loss))
                if move.category == old.category:
                    same_category += 1
                else:
                    shifts[(old.category, move.category)] += 1
                if move.category != "ok" and old.category != "ok":
                    error_pairs += 1
                    error_same += move.error_type == old.error_type
                if move.missed_motif or old.missed_motif:
                    motif_pairs += 1
                    motif_same += move.missed_motif == old.missed_motif
                if (move.mate_in or old.mate_in) and move.mate_in != old.mate_in:
                    mate_changes.append((game.url, move.ply, old.mate_in, move.mate_in))
            print(f"  Partie {index}/{len(games)} fertig ({time.time() - t0:.0f}s)")

    def pct(a: int, b: int) -> str:
        return f"{a / b * 100:.1f}%" if b else "–"

    print(f"\nDauer: {time.time() - started:.0f}s fuer {compared} eigene Zuege\n")
    print(f"Einstufung gleich geblieben:        {same_category}/{compared} = {pct(same_category, compared)}")
    print(f"Fehlerart gleich (wo beide Fehler): {error_same}/{error_pairs} = {pct(error_same, error_pairs)}")
    print(f"Verpasstes Motiv gleich:            {motif_same}/{motif_pairs} = {pct(motif_same, motif_pairs)}")
    if cp_diffs:
        cp_diffs.sort()
        median = cp_diffs[len(cp_diffs) // 2]
        p90 = cp_diffs[int(len(cp_diffs) * 0.9)]
        print(f"Abweichung Bewertungsverlust:       Median {median} cp, 90% unter {p90} cp")
    if shifts:
        print("\nWechsel der Einstufung (vorher -> nachher):")
        for (a, b), n in shifts.most_common():
            print(f"  {a:10s} -> {b:10s} {n:4d}")
    if mate_changes:
        print("\nMattlaenge anders:")
        for url, ply, a, b in mate_changes[:10]:
            print(f"  Halbzug {ply}: {a} -> {b}   {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
