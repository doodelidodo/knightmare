"""Was steckt im Resttopf "Stellungsfehler"?

Messwerkzeug, kein Teil der App. Spielt jeden als Stellungsfehler
eingeordneten Zug aus der Datenbank nach und prueft Kandidaten, die sich
exakt aus dem Brett entscheiden lassen. Erst wenn ein Kandidat hier einen
nennenswerten, sauberen Anteil erklaert, wird er zur Kategorie.

Auf tando im Container:
    docker compose exec -T chess python -m tools.positional_probe
    docker compose exec -T chess python -m tools.positional_probe --time-class rapid --days 90
"""

from __future__ import annotations

import argparse
import collections
import io
import sys
from datetime import datetime, timedelta
from typing import Optional

import chess
import chess.pgn

from app.analysis import MATERIAL_VALUES, win_percent

# --------------------------------------------------------------------------
# Kandidaten. Alle nehmen: Brett vor dem Zug, Zug, Brett nach dem Zug,
# Gegenzug (kann None sein). Rueckgabe: True, wenn das Muster vorliegt.
# --------------------------------------------------------------------------
KING_ZONE_RADIUS = 1


def _value(board: chess.Board, square: int) -> int:
    piece = board.piece_at(square)
    if piece is None:
        return 0
    return 100 if piece.piece_type == chess.KING else MATERIAL_VALUES.get(piece.piece_type, 0)


def _cheapest_attacker(board: chess.Board, color: chess.Color, square: int) -> int:
    """Wert des billigsten Angreifers von 'color' auf 'square', 0 wenn keiner."""
    best = 0
    for attacker in board.attackers(color, square):
        value = _value(board, attacker)
        if best == 0 or value < best:
            best = value
    return best


def en_prise_pieces(board: chess.Board, color: chess.Color) -> set[int]:
    """Eigene Figuren (ab Leichtfigur), die gerade verloren gehen wuerden:
    angegriffen und ungedeckt, oder von etwas Billigerem angegriffen."""
    result: set[int] = set()
    for piece_type in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN):
        for square in board.pieces(piece_type, color):
            attacker = _cheapest_attacker(board, not color, square)
            if attacker == 0:
                continue
            defended = board.is_attacked_by(color, square)
            if not defended or attacker < _value(board, square):
                result.add(square)
    return result


def left_piece_en_prise(before: chess.Board, move: chess.Move,
                        after: chess.Board, final: Optional[chess.Board]) -> bool:
    """Nach dem Gegenzug haengt eine eigene Figur, die vorher nicht hing."""
    if final is None:
        return False
    us = before.turn
    return bool(en_prise_pieces(final, us) - en_prise_pieces(before, us))


def piece_trapped(before: chess.Board, move: chess.Move,
                  after: chess.Board, final: Optional[chess.Board]) -> bool:
    """Eine eigene Figur (ab Leichtfigur) ist angegriffen und hat kein
    sicheres Feld mehr - jedes Zielfeld ist ungedeckt angegriffen oder
    von etwas Billigerem."""
    if final is None:
        return False
    us = before.turn
    board = final.copy(stack=False)
    board.turn = us  # wir sind dran, wenn die Figur fliehen muesste
    for piece_type in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN):
        for square in board.pieces(piece_type, us):
            if _cheapest_attacker(board, not us, square) == 0:
                continue
            value = _value(board, square)
            safe = False
            for escape in board.legal_moves:
                if escape.from_square != square:
                    continue
                probe = board.copy(stack=False)
                probe.push(escape)
                attacker = _cheapest_attacker(probe, not us, escape.to_square)
                captured = _value(board, escape.to_square)
                if attacker == 0 or attacker >= value or captured >= value:
                    safe = True
                    break
            if not safe:
                # Vorher schon gefangen? Dann ist es nicht dieser Zug.
                return square not in _trapped_before(before, us)
    return False


def _trapped_before(board: chess.Board, us: chess.Color) -> set[int]:
    result: set[int] = set()
    probe_board = board.copy(stack=False)
    probe_board.turn = us
    for piece_type in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN):
        for square in probe_board.pieces(piece_type, us):
            if _cheapest_attacker(probe_board, not us, square) == 0:
                continue
            value = _value(probe_board, square)
            safe = False
            for escape in probe_board.legal_moves:
                if escape.from_square != square:
                    continue
                probe = probe_board.copy(stack=False)
                probe.push(escape)
                attacker = _cheapest_attacker(probe, not us, escape.to_square)
                if attacker == 0 or attacker >= value or _value(probe_board, escape.to_square) >= value:
                    safe = True
                    break
            if not safe:
                result.add(square)
    return result


def king_zone(board: chess.Board, color: chess.Color) -> chess.SquareSet:
    king = board.king(color)
    if king is None:
        return chess.SquareSet()
    zone = chess.SquareSet(chess.BB_KING_ATTACKS[king])
    zone.add(king)
    return zone


def king_zone_pressure(board: chess.Board, color: chess.Color) -> int:
    """Wie viele Felder rund um den eigenen Koenig greift der Gegner an."""
    return sum(1 for square in king_zone(board, color) if board.is_attacked_by(not color, square))


def king_exposed(before: chess.Board, move: chess.Move,
                 after: chess.Board, final: Optional[chess.Board]) -> bool:
    """Der Zug hat die Koenigszone spuerbar geoeffnet: mindestens zwei Felder
    mehr unter Beschuss als vorher."""
    if final is None:
        return False
    us = before.turn
    return king_zone_pressure(final, us) - king_zone_pressure(before, us) >= 2


def pawn_defects(board: chess.Board, color: chess.Color) -> int:
    """Doppel- und Isolani-Bauern zaehlen."""
    files = [0] * 8
    for square in board.pieces(chess.PAWN, color):
        files[chess.square_file(square)] += 1
    doubled = sum(count - 1 for count in files if count > 1)
    isolated = 0
    for file_index, count in enumerate(files):
        if count == 0:
            continue
        left = files[file_index - 1] if file_index > 0 else 0
        right = files[file_index + 1] if file_index < 7 else 0
        if left == 0 and right == 0:
            isolated += count
    return doubled + isolated


def pawn_structure_damaged(before: chess.Board, move: chess.Move,
                           after: chess.Board, final: Optional[chess.Board]) -> bool:
    """Der eigene Zug hat einen neuen Doppel- oder Isolani-Bauern erzeugt."""
    us = before.turn
    return pawn_defects(after, us) > pawn_defects(before, us)


def moved_piece_back(before: chess.Board, move: chess.Move,
                     after: chess.Board, final: Optional[chess.Board]) -> bool:
    """Rueckzug ohne Not: eine Figur (kein Bauer, kein Koenig) zieht Richtung
    eigene Grundreihe, ohne dass sie angegriffen war."""
    piece = before.piece_at(move.from_square)
    if piece is None or piece.piece_type in (chess.PAWN, chess.KING):
        return False
    if before.is_attacked_by(not before.turn, move.from_square):
        return False
    if before.is_capture(move):
        return False
    forward = 1 if before.turn == chess.WHITE else -1
    return (chess.square_rank(move.to_square) - chess.square_rank(move.from_square)) * forward < 0


CANDIDATES = [
    ("figur_haengt_danach", left_piece_en_prise),
    ("figur_gefangen", piece_trapped),
    ("koenig_geoeffnet", king_exposed),
    ("bauernstruktur", pawn_structure_damaged),
    ("rueckzug_ohne_not", moved_piece_back),
]


# --------------------------------------------------------------------------
# Nachspielen
# --------------------------------------------------------------------------
def replay(pgn_text: str, ply: int):
    """Brett vor Halbzug 'ply' (1-basiert) und der Zug selbst."""
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return None, None
    board = game.board()
    for index, node in enumerate(game.mainline(), start=1):
        if index == ply:
            return board, node.move
        board.push(node.move)
    return None, None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--time-class", default=None)
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--error-type", default="positional")
    parser.add_argument("--limit", type=int, default=5000)
    args = parser.parse_args()

    from sqlmodel import Session, select
    from app.db import engine
    from app.models import ChessGame, ChessMove, utc_now

    conditions = [ChessMove.error_type == args.error_type]
    if args.time_class:
        conditions.append(ChessMove.time_class == args.time_class)
    if args.days:
        conditions.append(ChessMove.played_at >= utc_now() - timedelta(days=args.days))

    with Session(engine) as session:
        moves = list(session.exec(select(ChessMove).where(*conditions).limit(args.limit)).all())
        game_ids = {m.game_id for m in moves}
        games = {g.id: g for g in session.exec(select(ChessGame).where(ChessGame.id.in_(game_ids))).all()}

    print(f"{len(moves)} Zuege der Art '{args.error_type}'\n")

    by_candidate: dict[str, collections.Counter] = {name: collections.Counter() for name, _ in CANDIDATES}
    none_at_all = collections.Counter()
    decided = collections.Counter()   # |cp_before| >= 500: Stellung war schon entschieden
    win_loss_bins = collections.Counter()
    overlap = collections.Counter()
    failed = 0

    for m in moves:
        game = games.get(m.game_id)
        if game is None:
            failed += 1
            continue
        before, move = replay(game.pgn, m.ply)
        if before is None or move is None:
            failed += 1
            continue
        after = before.copy(stack=False)
        after.push(move)
        final: Optional[chess.Board] = None
        if m.refutation_san:
            try:
                reply = after.parse_san(m.refutation_san)
                final = after.copy(stack=False)
                final.push(reply)
            except (ValueError, AssertionError):
                final = None

        hits = []
        for name, fn in CANDIDATES:
            try:
                if fn(before, move, after, final):
                    hits.append(name)
                    by_candidate[name][m.category] += 1
            except Exception as exc:  # noqa: BLE001 - messen, nicht abbrechen
                by_candidate[name]["FEHLER: " + type(exc).__name__] += 1
        if not hits:
            none_at_all[m.category] += 1
        overlap[len(hits)] += 1

        if abs(m.cp_before) >= 500:
            decided[m.category] += 1
        drop = win_percent(m.cp_before) - win_percent(m.cp_after)
        bucket = "<5" if drop < 5 else "5-10" if drop < 10 else "10-20" if drop < 20 else ">=20"
        win_loss_bins[bucket] += 1

    total = len(moves) - failed
    print(f"nachgespielt: {total}, nicht nachspielbar: {failed}\n")
    print("Kandidat                 gesamt   Patzer  Fehler  Ungenau.")
    for name, _ in CANDIDATES:
        c = by_candidate[name]
        n = sum(v for k, v in c.items() if not k.startswith("FEHLER"))
        errs = {k: v for k, v in c.items() if k.startswith("FEHLER")}
        print(f"  {name:22s} {n:6d} {n/total*100:5.1f}%  {c['blunder']:6d} {c['mistake']:7d} {c['inaccuracy']:8d}"
              + (f"   {errs}" if errs else ""))
    n = sum(none_at_all.values())
    print(f"  {'– keiner –':22s} {n:6d} {n/total*100:5.1f}%  {none_at_all['blunder']:6d} {none_at_all['mistake']:7d} {none_at_all['inaccuracy']:8d}")
    print("\nTreffer pro Zug:", dict(sorted(overlap.items())))

    d = sum(decided.values())
    print(f"\nStellung schon entschieden (|Bewertung vorher| >= 500 cp): {d} = {d/total*100:.1f}%"
          f"  (Patzer {decided['blunder']}, Fehler {decided['mistake']}, Ungenau. {decided['inaccuracy']})")
    print("Verlust an Gewinnwahrscheinlichkeit:",
          "  ".join(f"{k}: {v} ({v/total*100:.0f}%)" for k, v in sorted(win_loss_bins.items(), key=lambda kv: ["<5","5-10","10-20",">=20"].index(kv[0]))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
