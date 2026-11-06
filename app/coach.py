"""Der Coach: eine gefuehrte Tages-Session aus den eigenen Fehlern (seit 1.18).

Vorher gab es vier Modi nebeneinander (Tagesaufgaben, Was droht?, Gewinn
verwerten, Woodpecker). Jetzt gibt es *einen* Ablauf pro Tag:

  1. Lektion    - die Checklisten-Frage, die in den letzten vier Wochen am
                  meisten verhindert haette, mit den eigenen Zahlen und einem
                  Beispiel aus einer eigenen Partie.
  2. Aufgaben   - die Tagesaufgaben (Leitner wie bisher), aber jede in zwei
                  Schritten: erst "was antwortet er auf deinen Zug?" (die
                  Widerlegung finden - genau das, was man in der Partie nicht
                  gesehen hat), dann "was spielst du stattdessen?". Danach
                  eine Erklaerung, die aus der Brettgeometrie abgeleitet ist:
                  was die Widerlegung tut, was der bessere Zug tut, und zu
                  welcher Frage der Checkliste der Fehler gehoert.
  3. Verwerten  - eine gewonnene Stellung aus einer verlorenen Partie gegen
                  Stockfish zu Ende spielen.
  4. Abschluss  - Zahlen des Tages und die Serie.

"Was droht?" in der alten Form ist weg: dort galt der beste Gegnerzug in der
Nullzug-Stellung als Drohung, und der hatte mit dem eigenen Fehler oft nichts
zu tun. Schritt 1 fragt jetzt nach der Widerlegung des *tatsaechlich
gespielten* Zugs - die kommt aus der Analyse und ist der Grund, warum der Zug
ein Fehler war.

Die Erklaerungen sind reine Geometrie (kein Sprachmodell): Schach, Schlagen
mit Gewinn, Gabel mit ihren Zielen, Fesselung/Spiess, Abzug, neu haengende
Figuren, Mattdrohung. Die Oberflaeche macht daraus Saetze in beiden Sprachen.
Die API liefert nur Schluessel und Felder.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, Optional

import chess
from sqlalchemy import select, update
from sqlalchemy.engine import Engine as DbEngine

from . import analysis, practice
from .analysis import MATERIAL_VALUES
from .journey import CHECKLIST, checklist_point
from .models import ChessCoachDay, ChessGame, ChessMove, ChessTactic, ChessTacticDay, utc_now
from .tactics import (Engine, MoveError, TacticSettings, legal_moves, mate_for, puzzle_payload,
                      scope_where, win_for)

log = logging.getLogger(__name__)

COACH = ChessCoachDay.__table__
TACTICS = ChessTactic.__table__
DAYS = ChessTacticDay.__table__
GAMES = ChessGame.__table__
MOVES = ChessMove.__table__

# Bei diesen Fehlerarten ist die Widerlegung die Pointe: der Gegner tut etwas
# mit dem Zug. Bei "Gewinn verpasst" oder "Matt verpasst" gibt es keine
# Widerlegung zu finden - da geht es direkt um den besseren eigenen Zug.
REFUTE_ERRORS = ("hanging_piece", "fork", "allowed_mate", "missed_threat", "bad_trade")
REFUTE_TOLERANCE = 4.0      # ein anderer Gegnerzug zaehlt, wenn er fast so stark ist
LESSON_DAYS = 28
SERIOUS = ("mistake", "blunder")
PIECE_VALUE = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 100}
WORTHWHILE = analysis.WORTHWHILE_TARGET


# ---------------------------------------------------------------------------
# Geometrie: was tut ein Zug?
# ---------------------------------------------------------------------------
def _sym(board: chess.Board, square: int) -> str:
    piece = board.piece_at(square)
    return piece.symbol().lower() if piece else ""


def _en_prise(board: chess.Board, victim: chess.Color) -> dict[int, int]:
    """Figuren von `victim`, die der Gegner mit Gewinn nehmen koennte:
    ungedeckt angegriffen, oder von einer billigeren Figur angegriffen.
    Liefert Feld -> billigster Angreifer (Feld)."""
    out: dict[int, int] = {}
    for square, piece in board.piece_map().items():
        if piece.color != victim or piece.piece_type == chess.KING:
            continue
        attackers = board.attackers(not victim, square)
        if not attackers:
            continue
        cheapest = min(attackers, key=lambda a: PIECE_VALUE[board.piece_type_at(a)])
        if not board.attackers(victim, square) or PIECE_VALUE[board.piece_type_at(cheapest)] < PIECE_VALUE[piece.piece_type]:
            out[square] = cheapest
    return out


def _fork_targets(after: chess.Board, move: chess.Move, victim: chess.Color) -> list[int]:
    """Dieselbe Regel wie analysis.is_fork, aber mit den Zielen."""
    attacker = after.piece_at(move.to_square)
    if attacker is None or attacker.color == victim:
        return []
    value = MATERIAL_VALUES.get(attacker.piece_type, 0)
    targets = []
    for sq in after.attacks(move.to_square):
        t = after.piece_at(sq)
        if t is None or t.color != victim:
            continue
        if t.piece_type == chess.KING:
            targets.append(sq)
        elif MATERIAL_VALUES.get(t.piece_type, 0) > value:
            targets.append(sq)
        elif MATERIAL_VALUES.get(t.piece_type, 0) >= WORTHWHILE and not after.is_attacked_by(victim, sq):
            targets.append(sq)
    return targets if len(targets) >= 2 else []


def _line_motif(after: chess.Board, move: chess.Move, victim: chess.Color) -> Optional[dict]:
    attacker = after.piece_at(move.to_square)
    if attacker is None or attacker.piece_type not in analysis.SLIDERS or attacker.color == victim:
        return None
    for target in after.attacks(move.to_square):
        front = after.piece_at(target)
        if front is None or front.color != victim:
            continue
        behind = analysis._first_piece_behind(after, move.to_square, target)
        if behind is None:
            continue
        back = after.piece_at(behind)
        if back is None or back.color != victim:
            continue
        fv, bv = analysis._value_at(after, target), analysis._value_at(after, behind)
        if bv > fv and bv >= analysis.PIN_ANCHOR_VALUE and fv >= WORTHWHILE:
            return {"kind": "pin", "front": chess.square_name(target), "front_piece": _sym(after, target),
                    "back": chess.square_name(behind), "back_piece": _sym(after, behind)}
        if fv > bv and fv > MATERIAL_VALUES.get(attacker.piece_type, 0):
            return {"kind": "skewer", "front": chess.square_name(target), "front_piece": _sym(after, target),
                    "back": chess.square_name(behind), "back_piece": _sym(after, behind)}
    return None


def _discovered(before: chess.Board, after: chess.Board, move: chess.Move) -> Optional[dict]:
    us = before.turn
    for sq in after.pieces(chess.BISHOP, us) | after.pieces(chess.ROOK, us) | after.pieces(chess.QUEEN, us):
        if sq == move.to_square:
            continue
        for target in after.attacks(sq) - before.attacks(sq):
            p = after.piece_at(target)
            if p is None or p.color == us or analysis._value_at(after, target) < WORTHWHILE:
                continue
            if move.from_square in chess.SquareSet(chess.between(sq, target)):
                return {"kind": "discovered", "by": chess.square_name(sq), "by_piece": _sym(after, sq),
                        "target": chess.square_name(target), "target_piece": _sym(after, target)}
    return None


def _threatens_mate(after: chess.Board) -> Optional[str]:
    """Haette der Ziehende gleich noch einen Zug, setzte er Matt? Liefert den
    Mattzug (uci)."""
    b = chess.Board(after.fen())
    try:
        b.push(chess.Move.null())
    except (ValueError, AssertionError):
        return None
    b = chess.Board(b.fen())
    if not b.is_valid():
        return None
    for reply in b.legal_moves:
        b.push(reply)
        mate = b.is_checkmate()
        b.pop()
        if mate:
            return reply.uci()
    return None


MOTIF_KINDS = ("fork", "pin", "skewer", "discovered", "attacks")
LINE_MAX_PLIES = 10


def _claimed_squares(fact: dict) -> set[int]:
    """Wo das Motiv Material holen will: Gabel - eines der Ziele, Spiess - die
    hintere Figur, Fesselung - die gefesselte, Abzug/Angriff - das Ziel."""
    names: list[str] = []
    if fact["kind"] == "fork":
        names = [t["square"] for t in fact.get("targets", []) if t.get("piece") != "k"]
    elif fact["kind"] == "skewer":
        names = [fact["back"]]
    elif fact["kind"] == "pin":
        names = [fact["front"]]
    elif fact["kind"] in ("discovered", "attacks"):
        names = [fact["target"]]
    return {chess.parse_square(n) for n in names}


def _line_captures(before: chess.Board, line: list[chess.Move]) -> set[int]:
    """Felder, auf denen die Seite am Zug (before.turn) in der Variante schlaegt."""
    mover = before.turn
    b = before.copy(stack=False)
    out: set[int] = set()
    for mv in line:
        if mv not in b.legal_moves:
            break
        if b.turn == mover and b.is_capture(mv):
            out.add(mv.to_square)
        b.push(mv)
    return out


def describe_move(before: chess.Board, move: chess.Move, mate_in: Optional[int] = None,
                  line: Optional[list[chess.Move]] = None) -> list[dict]:
    """Was tut dieser Zug? Eine Liste von Fakten mit Feldern, aus der die
    Oberflaeche Saetze baut. Reihenfolge: das Schlagendste zuerst.

    Sicher aus dem Brett: Matt, Schach, Schlagen, Umwandlung, Mattdrohung.
    Motive (Gabel, Spiess, Fesselung, Abzug, Angriff) dagegen nur, wenn die
    Stockfish-Variante `line` (beginnt mit `move`) das behauptete Material
    auch wirklich schlaegt. Geometrie allein reicht nicht: "Rxb8 spiesst
    Dame c8 und Turm e8" ist geometrisch richtig und schachlich falsch,
    weil die Dame einfach zuruecknimmt. Ohne Variante: keine Motive."""
    if move not in before.legal_moves:
        return []
    facts = _describe_raw(before, move, mate_in)
    taken = _line_captures(before, line) if line else set()
    kept = []
    for f in facts:
        if f["kind"] in MOTIF_KINDS and not (_claimed_squares(f) & taken):
            continue
        kept.append(f)
    if not kept:
        piece = _sym(before, move.from_square)
        kept.append({"kind": "quiet", "piece": piece, "to": chess.square_name(move.to_square)})
    return kept


def _describe_raw(before: chess.Board, move: chess.Move, mate_in: Optional[int] = None) -> list[dict]:
    mover = before.turn
    victim = not mover
    after = before.copy(stack=False)
    after.push(move)
    facts: list[dict] = []
    piece = _sym(before, move.from_square)
    to = chess.square_name(move.to_square)

    if after.is_checkmate():
        return [{"kind": "mate", "piece": piece, "to": to}]
    if mate_in and mate_in > 0:
        facts.append({"kind": "mate_in", "n": mate_in})
    if after.is_check():
        facts.append({"kind": "check", "piece": piece, "to": to})
    if before.is_capture(move):
        victim_sq = move.to_square
        if before.is_en_passant(move):
            victim_sq = move.to_square + (-8 if mover == chess.WHITE else 8)
        taken = _sym(before, victim_sq)
        defended = before.is_attacked_by(victim, move.to_square)
        free = (not defended and taken != "p") or PIECE_VALUE.get(before.piece_type_at(victim_sq) or 0, 0) > PIECE_VALUE.get(before.piece_type_at(move.from_square) or 0, 0)
        facts.append({"kind": "capture", "piece": piece, "taken": taken, "square": chess.square_name(victim_sq),
                      "free": bool(free), "defended": bool(defended)})
    if move.promotion:
        facts.append({"kind": "promotion", "to": to})
    forks = _fork_targets(after, move, victim)
    if forks:
        facts.append({"kind": "fork", "piece": piece, "to": to,
                      "targets": [{"square": chess.square_name(s), "piece": _sym(after, s)} for s in forks]})
    line = _line_motif(after, move, victim)
    if line:
        facts.append({**line, "piece": piece, "to": to})
    disc = _discovered(before, after, move)
    if disc:
        facts.append(disc)
    # Neu haengende Figuren des Gegners (ab Leichtfigur) - ohne die, die
    # Gabel, Abzug oder Fesselung schon nennen.
    hanging_before = set(_en_prise(before, victim))
    hanging_after = _en_prise(after, victim)
    named = set(forks)
    if disc:
        named.add(chess.parse_square(disc["target"]))
    if line:
        named.add(chess.parse_square(line["front"]))
        named.add(chess.parse_square(line["back"]))
    new_hanging = [(s, a) for s, a in hanging_after.items() if s not in hanging_before and s not in named
                   and s != move.to_square and PIECE_VALUE[after.piece_type_at(s)] >= WORTHWHILE]
    for s, attacker in sorted(new_hanging, key=lambda x: -PIECE_VALUE[after.piece_type_at(x[0])])[:2]:
        facts.append({"kind": "attacks", "target": chess.square_name(s), "target_piece": _sym(after, s),
                      "by": chess.square_name(attacker), "by_piece": _sym(after, attacker),
                      "undefended": not after.is_attacked_by(victim, s)})
    if not after.is_check():
        mate_move = _threatens_mate(after)
        if mate_move:
            facts.append({"kind": "mate_threat", "move": mate_move})
    if not facts:
        facts.append({"kind": "quiet", "piece": piece, "to": to})
    return facts


def _material(board: chess.Board, color: chess.Color) -> int:
    return sum(MATERIAL_VALUES.get(p.piece_type, 0) for p in board.piece_map().values()
               if p.color == color and p.piece_type != chess.KING)


def engine_line(eng: Engine, board: chess.Board, first: chess.Move) -> list[chess.Move]:
    """Die Stockfish-Hauptvariante nach `first`, mit `first` vorne. Ohne
    Engine nur der Zug selbst."""
    line = [first]
    if not eng.available or first not in board.legal_moves:
        return line
    b = board.copy(stack=False)
    b.push(first)
    if b.is_game_over():
        return line
    try:
        info = eng.analyse(b)[0]
    except Exception:  # noqa: BLE001 - dann eben ohne Variante
        log.warning("Variante nicht gerechnet", exc_info=True)
        return line
    return line + list(info.get("pv") or [])[: LINE_MAX_PLIES - 1]


def line_fact(board: chess.Board, line: list[chess.Move], us: chess.Color) -> Optional[dict]:
    """Die Variante bis zum Ende der Schlagfolge, mit der Materialbilanz aus
    Sicht von `us` (in Bauerneinheiten). Gezeigt wird der erste Zug und dann
    alle direkt folgenden Schlag-, Schach- und Umwandlungszuege; der erste
    ruhige Zug beendet sie - sonst stuende die Bilanz mitten im Abtausch."""
    b = board.copy(stack=False)
    shown: list[chess.Move] = []
    for mv in line:
        if mv not in b.legal_moves:
            break
        forcing = b.is_capture(mv) or mv.promotion is not None or b.gives_check(mv)
        if shown and not forcing:
            break
        b.push(mv)
        shown.append(mv)
        if b.is_game_over():
            break
    if len(shown) < 2:
        return None
    start = _material(board, us) - _material(board, not us)
    end = _material(b, us) - _material(b, not us)
    return {"kind": "line", "san": board.variation_san(shown), "uci": [m.uci() for m in shown],
            "material": end - start, "mate": b.is_checkmate()}


def why_played_is_bad(before: chess.Board, played: chess.Move, refutation: Optional[chess.Move]) -> list[dict]:
    """Was der eigene Zug angerichtet hat - aus Sicht nach der Widerlegung:
    welche eigenen Figuren danach haengen, ob die gezogene Figur selbst
    das Opfer ist."""
    if played not in before.legal_moves:
        return []
    us = before.turn
    after = before.copy(stack=False)
    after.push(played)
    out: list[dict] = []
    if refutation is not None and refutation in after.legal_moves:
        if after.is_capture(refutation) and refutation.to_square == played.to_square:
            out.append({"kind": "moved_into_capture", "piece": _sym(before, played.from_square),
                        "to": chess.square_name(played.to_square)})
        # Was danach noch haengt (ab Leichtfigur) - aber nicht, was die
        # Widerlegung selbst schon benennt (Gabel, Angriff), sonst steht es doppelt.
        named = set()
        for f in _describe_raw(after, refutation):
            for key in ("target", "front", "back"):
                if f.get(key):
                    named.add(chess.parse_square(f[key]))
            for t in f.get("targets", []):
                named.add(chess.parse_square(t["square"]))
        b2 = after.copy(stack=False)
        b2.push(refutation)
        hanging = {s: a for s, a in _en_prise(b2, us).items()
                   if PIECE_VALUE[b2.piece_type_at(s)] >= WORTHWHILE and s not in named}
        for s, a in sorted(hanging.items(), key=lambda x: -PIECE_VALUE[b2.piece_type_at(x[0])])[:2]:
            out.append({"kind": "left_hanging", "piece": _sym(b2, s), "square": chess.square_name(s),
                        "by": chess.square_name(a), "by_piece": _sym(b2, a)})
    # Haengte die Figur schon vor dem Zug (Drohung uebersehen)? Mit
    # Angreifern und Verteidigern, damit man es auf dem Brett nachzaehlen kann.
    before_hanging = _en_prise(before, us)
    if refutation is not None and refutation.to_square in before_hanging and refutation.to_square != played.to_square:
        sq = refutation.to_square
        out.append({"kind": "was_already_attacked", "piece": _sym(before, sq), "square": chess.square_name(sq),
                    "attackers": [{"square": chess.square_name(a), "piece": _sym(before, a)}
                                  for a in sorted(before.attackers(not us, sq))],
                    "defenders": [{"square": chess.square_name(d), "piece": _sym(before, d)}
                                  for d in sorted(before.attackers(us, sq))]})
    return out


# ---------------------------------------------------------------------------
# Aufgabe: Schritt 1 (Widerlegung) und Erklaerung
# ---------------------------------------------------------------------------
def _boards(row) -> tuple[Optional[chess.Board], Optional[chess.Move], Optional[chess.Board]]:
    try:
        before = chess.Board(row.fen)
        played = chess.Move.from_uci(row.played_uci or "")
    except (ValueError, TypeError):
        return None, None, None
    if played not in before.legal_moves:
        return before, None, None
    after = before.copy(stack=False)
    after.push(played)
    return before, played, after


def ensure_punish(db: DbEngine, eng: Engine, row) -> Optional[str]:
    """Widerlegung (uci) - aus der Spalte, sonst einmal mit der Engine
    nachgerechnet und gespeichert. Aufgaben von vor 1.18 haben sie nicht."""
    if row.punish_uci:
        return row.punish_uci
    _before, _played, after = _boards(row)
    if after is None or after.is_game_over():
        return None
    uci = None
    if row.punish_san:
        # "12... Nxe5 13. Bxe5" - der erste Zug reicht.
        for token in (row.punish_san or "").replace(".", " ").split():
            if token[0].isdigit():
                continue
            try:
                uci = after.parse_san(token).uci()
            except ValueError:
                uci = None
            break
    if uci is None and eng.available:
        info = eng.analyse(after)[0]
        pv = info.get("pv") or []
        uci = pv[0].uci() if pv else None
    if uci:
        with db.begin() as conn:
            conn.execute(update(TACTICS).where(TACTICS.c.key == row.key).values(punish_uci=uci))
    return uci


def has_refute_step(row) -> bool:
    return (row.error_type in REFUTE_ERRORS) and bool(row.played_uci)


def exercise_payload(db: DbEngine, eng: Engine, row) -> dict:
    """Die Aufgabe, wie der Browser sie braucht: das Puzzle wie bisher, dazu
    Schritt 1 (Stellung nach dem Partiezug, Zuege des Gegners)."""
    pz = puzzle_payload(row)
    point = checklist_point(row.error_type, row.missed_motif)
    out = {**pz, "point": point, "played_san": row.played_san, "played_uci": row.played_uci,
           "category": row.category, "error_type": row.error_type, "missed_motif": row.missed_motif,
           "refute": None}
    if has_refute_step(row):
        before, played, after = _boards(row)
        punish = ensure_punish(db, eng, row) if after is not None else None
        if after is not None and punish and not after.is_game_over():
            out["refute"] = {"fen": after.fen(), "legal": legal_moves(after),
                             "to_move": "white" if after.turn else "black"}
    return out


def explain(db: DbEngine, eng: Engine, row) -> dict:
    """Erklaerung nach der Aufgabe - alles Geometrie, kein Engine-Aufruf noetig
    (ausser einmalig fuer eine fehlende Widerlegung)."""
    before, played, after = _boards(row)
    punish_uci = ensure_punish(db, eng, row) if after is not None else None
    sol = (row.solution or "").split()
    best = None
    try:
        best = chess.Move.from_uci(sol[0]) if sol else None
    except ValueError:
        best = None
    refutation = None
    try:
        refutation = chess.Move.from_uci(punish_uci) if punish_uci else None
    except ValueError:
        refutation = None
    out = {
        "point": checklist_point(row.error_type, row.missed_motif),
        "error_type": row.error_type, "missed_motif": row.missed_motif, "category": row.category,
        "open": bool(row.open),
        "played": {"uci": row.played_uci, "san": row.played_san},
        "refutation": {"uci": punish_uci, "san": None},
        "best": {"uci": sol[0] if sol else None, "san": None, "line_san": row.solution_san,
                 "line": sol, "mate_in": row.mate_in},
        "win_played": row.win_played, "win_best": row.win_best,
        "played_facts": [], "refutation_facts": [], "best_facts": [],
        "url": row.url,
    }
    if before is None:
        return out
    us = before.turn
    out["refutation_line"] = None
    out["best_line"] = None
    if after is not None and refutation is not None and refutation in after.legal_moves:
        out["refutation"]["san"] = after.san(refutation)
        rline = engine_line(eng, after, refutation)
        out["refutation_facts"] = describe_move(after, refutation, line=rline)
        out["refutation_line"] = line_fact(after, rline, us) if len(rline) > 1 else None
    if played is not None:
        out["played_facts"] = why_played_is_bad(before, played, refutation)
    if best is not None and best in before.legal_moves:
        out["best"]["san"] = before.san(best)
        # Die gespeicherte Loesung ist schon Stockfish; danach weiter mit der Engine.
        stored = [chess.Move.from_uci(u) for u in sol]
        b = before.copy(stack=False)
        for mv in stored:
            if mv not in b.legal_moves:
                break
            b.push(mv)
        bline = stored
        if eng.available and not b.is_game_over() and len(stored) < LINE_MAX_PLIES:
            try:
                bline = stored + list(eng.analyse(b)[0].get("pv") or [])[: LINE_MAX_PLIES - len(stored)]
            except Exception:  # noqa: BLE001
                bline = stored
        out["best_facts"] = describe_move(before, best, row.mate_in if row.mate_in and 0 < row.mate_in <= 5 else None,
                                          line=bline)
        out["best_line"] = line_fact(before, bline, us) if len(bline) > 1 else None
    out["engine"] = eng.available
    return out


def check_refutation(eng: Engine, row, uci: str) -> dict:
    """Schritt 1: hat man die Widerlegung gefunden? Der gespeicherte Zug
    zaehlt; ein anderer auch, wenn er fuer den Gegner fast genauso stark ist."""
    before, played, after = _boards(row)
    if after is None or not row.punish_uci:
        raise MoveError("no_refute_step")
    try:
        move = chess.Move.from_uci(uci)
    except ValueError as exc:
        raise MoveError("unknown_move") from exc
    if move not in after.legal_moves:
        raise MoveError("illegal_move")
    them = after.turn
    san = after.san(move)
    ok = move.uci() == row.punish_uci
    alternative = False
    if not ok and eng.available:
        b2 = after.copy(stack=False)
        b2.push(move)
        if b2.is_checkmate():
            ok = True
        else:
            best = eng.analyse(after)[0]
            win_best = win_for(best["score"], them)
            mate_best = mate_for(best["score"], them)
            info = eng.analyse(b2)[0]
            win_u = win_for(info["score"], them)
            mate_u = mate_for(info["score"], them)
            if mate_best is not None:
                ok = mate_u is not None and mate_u <= mate_best
            else:
                ok = win_u >= win_best - REFUTE_TOLERANCE
        alternative = ok
    res: dict = {"result": "good" if ok else "bad", "san": san, "alternative": alternative}
    if ok:
        # Die Loesung geht erst nach dem Finden (oder Aufdecken) an den Browser.
        punish = chess.Move.from_uci(row.punish_uci)
        res["punish_uci"] = row.punish_uci
        res["punish_san"] = after.san(punish) if punish in after.legal_moves else None
        line = engine_line(eng, after, move)
        res["facts"] = describe_move(after, move, line=line)
        res["line"] = line_fact(after, line, them) if len(line) > 1 else None
    return res


def reveal_refutation(row, eng: Optional[Engine] = None) -> dict:
    _before, _played, after = _boards(row)
    if after is None or not row.punish_uci:
        raise MoveError("no_refute_step")
    punish = chess.Move.from_uci(row.punish_uci)
    if punish not in after.legal_moves:
        return {"result": "revealed", "punish_uci": row.punish_uci, "punish_san": None, "facts": [], "line": None}
    line = engine_line(eng, after, punish) if eng is not None else [punish]
    return {"result": "revealed", "punish_uci": row.punish_uci, "punish_san": after.san(punish),
            "facts": describe_move(after, punish, line=line),
            "line": line_fact(after, line, after.turn) if len(line) > 1 else None}


# ---------------------------------------------------------------------------
# Lektion: die eigenen Zahlen zur Fokus-Frage
# ---------------------------------------------------------------------------
def lesson_data(db: DbEngine, scope: Optional[TacticSettings], today: date) -> dict:
    """Welche Checklisten-Frage haette in den letzten 28 Tagen am meisten
    verhindert - mit Zahlen pro Frage und Fehlerart, und bis zu drei
    Beispielen (Zug-IDs fuer das Brett) zur Fokus-Frage."""
    m, g = MOVES.c, GAMES.c
    since = datetime.combine(today - timedelta(days=LESSON_DAYS), datetime.min.time())
    with db.connect() as conn:
        rows = conn.execute(
            select(m.id, m.ply, m.move_number, m.san, m.best_move_san, m.refutation_san, m.category, m.error_type,
                   m.missed_motif, m.win_loss, g.id.label("game_id"), g.opponent, g.played_at, g.color, g.url, g.result)
            .select_from(MOVES.join(GAMES, g.id == m.game_id))
            .where(g.played_at >= since, g.analyzed_at.is_not(None), m.category.in_(SERIOUS),
                   scope_where(g.platform, g.time_class, scope, allow_any=False))
            .order_by(g.played_at.desc(), m.ply.desc())
        ).all()
        games = conn.execute(
            select(g.id).where(g.played_at >= since, g.analyzed_at.is_not(None),
                               scope_where(g.platform, g.time_class, scope, allow_any=False))).all()
    points: Counter = Counter()
    errors: Counter = Counter()
    by_point_errors: dict[str, Counter] = {p: Counter() for p in CHECKLIST}
    lost_after: Counter = Counter()
    for r in rows:
        p = checklist_point(r.error_type, r.missed_motif)
        errors[r.error_type or "positional"] += 1
        if not p:
            continue
        points[p] += 1
        by_point_errors[p][r.error_type or "positional"] += 1
        if r.result == "loss":
            lost_after[p] += 1
    focus = points.most_common(1)[0][0] if points else None
    examples = []
    if focus:
        seen_games: set[int] = set()
        for r in rows:
            if checklist_point(r.error_type, r.missed_motif) != focus or r.game_id in seen_games:
                continue
            seen_games.add(r.game_id)
            examples.append({"move_id": r.id, "san": r.san, "move_number": r.move_number, "color": r.color,
                             "opponent": r.opponent, "played_at": r.played_at.date().isoformat() if r.played_at else None,
                             "category": r.category, "error_type": r.error_type, "best_san": r.best_move_san,
                             "refutation_san": r.refutation_san, "win_loss": round(r.win_loss or 0, 1), "url": r.url})
            if len(examples) >= 3:
                break
    return {
        "days": LESSON_DAYS, "games": len(games), "serious": len(rows),
        "points": {p: points.get(p, 0) for p in CHECKLIST},
        "errors": dict(errors),
        "focus": focus,
        "focus_errors": dict(by_point_errors[focus]) if focus else {},
        "focus_lost_games": lost_after.get(focus, 0) if focus else 0,
        "examples": examples,
    }


# ---------------------------------------------------------------------------
# Die Session des Tages
# ---------------------------------------------------------------------------
def _coach_row(conn, day: date):
    return conn.execute(select(COACH).where(COACH.c.day == day)).first()


def _refutes(row) -> dict[str, str]:
    try:
        return json.loads(row.refutes or "{}") if row else {}
    except ValueError:
        return {}


def session(db: DbEngine, eng: Engine, scope: Optional[TacticSettings], day: date,
            tactics_payload: dict) -> dict:
    """Setzt die Session aus Tagesaufgaben, Lektion und Verwertung zusammen.
    Beim ersten Aufruf des Tages wird die Fokus-Frage festgeschrieben."""
    lesson = lesson_data(db, scope, day)
    with db.begin() as conn:
        row = _coach_row(conn, day)
        if row is None:
            conn.execute(COACH.insert().values(day=day, focus=lesson["focus"], lesson_seen=False, refutes="{}",
                                               updated_at=utc_now()))
            row = _coach_row(conn, day)
    focus = row.focus or lesson["focus"]
    refutes = _refutes(row)
    items = []
    keys = [it["key"] for it in tactics_payload.get("items", [])]
    rows_by_key = {}
    if keys:
        with db.connect() as conn:
            rows_by_key = {r.key: r for r in conn.execute(select(TACTICS).where(TACTICS.c.key.in_(keys)))}
    for it in tactics_payload.get("items", []):
        trow = rows_by_key.get(it["key"])
        if trow is None:
            continue
        ex = exercise_payload(db, eng, trow)
        items.append({"key": it["key"], "pos": it["pos"], "result": it["result"],
                      "refute_result": refutes.get(it["key"]), "exercise": ex,
                      "explain": None})
    # Fokus-Aufgaben zuerst, sonst die Reihenfolge des Tages.
    items.sort(key=lambda i: (0 if (focus and i["exercise"]["point"] == focus) else 1, i["pos"]))

    convert = _convert_item(db, eng, scope, row)
    done_ex = sum(1 for i in items if i["result"])
    refute_total = sum(1 for i in items if i["exercise"]["refute"] or i["refute_result"])
    refute_good = sum(1 for i in items if i["refute_result"] == "good")
    return {
        "day": day.isoformat(), "focus": focus, "lesson_seen": bool(row.lesson_seen), "lesson": lesson,
        "items": items, "convert": convert,
        "progress": {"exercises": done_ex, "exercises_total": len(items),
                     "solved": sum(1 for i in items if i["result"] == "solved"),
                     "refute_good": refute_good, "refute_total": refute_total,
                     "convert_result": row.convert_result},
        "finished": row.finished_at is not None,
        "stats": tactics_payload.get("stats"),
    }


def _convert_item(db: DbEngine, eng: Engine, scope: Optional[TacticSettings], row) -> Optional[dict]:
    """Die eine Verwertungs-Stellung des Tages: beim ersten Mal die naechste
    offene aus dem Vorrat, danach immer dieselbe."""
    p = practice.PRACTICE.c
    key = row.convert_key
    with db.connect() as conn:
        item = conn.execute(select(practice.PRACTICE).where(p.key == key)).first() if key else None
    if item is None:
        if eng.available:
            try:
                practice.ensure(db, eng, "convert", want=4, max_tries=8, scope=scope)
            except Exception:  # noqa: BLE001
                log.exception("Gewinn verwerten: Vorbereitung fehlgeschlagen")
        listing = practice.listing(db, "convert", limit=1, scope=scope)
        if not listing["items"]:
            return None
        key = listing["items"][0]["key"]
        with db.begin() as conn:
            conn.execute(update(COACH).where(COACH.c.day == row.day).values(convert_key=key, updated_at=utc_now()))
        with db.connect() as conn:
            item = conn.execute(select(practice.PRACTICE).where(p.key == key)).first()
        if item is None:
            return None
    out = practice.payload(item)
    out["result"] = row.convert_result
    return out


def mark_lesson(db: DbEngine, day: date) -> None:
    with db.begin() as conn:
        conn.execute(update(COACH).where(COACH.c.day == day).values(lesson_seen=True, updated_at=utc_now()))


def record_refute(db: DbEngine, day: date, key: str, result: str) -> bool:
    """Nur der erste Versuch zaehlt. Liefert, ob er gezaehlt wurde."""
    with db.begin() as conn:
        row = _coach_row(conn, day)
        if row is None:
            return False
        refutes = _refutes(row)
        if key in refutes:
            return False
        refutes[key] = result
        conn.execute(update(COACH).where(COACH.c.day == day).values(refutes=json.dumps(refutes), updated_at=utc_now()))
    return True


def record_convert(db: DbEngine, day: date, key: str, result: str) -> None:
    with db.begin() as conn:
        row = _coach_row(conn, day)
        if row is None or row.convert_key != key or row.convert_result:
            return
        conn.execute(update(COACH).where(COACH.c.day == day).values(convert_result=result, updated_at=utc_now()))


def finish(db: DbEngine, day: date) -> None:
    with db.begin() as conn:
        row = _coach_row(conn, day)
        if row is not None and row.finished_at is None:
            conn.execute(update(COACH).where(COACH.c.day == day).values(finished_at=utc_now(), updated_at=utc_now()))


def history(db: DbEngine, today: date, days: int = 14) -> list[dict]:
    """Die letzten Tage: erledigt oder nicht - fuer die Serie auf der
    Abschluss-Karte."""
    d = DAYS.c
    since = today - timedelta(days=days - 1)
    with db.connect() as conn:
        by_day: dict[date, list] = {}
        for r in conn.execute(select(d.day, d.result).where(d.day >= since)):
            by_day.setdefault(r.day, []).append(r.result)
    out = []
    for i in range(days):
        day = since + timedelta(days=i)
        results = by_day.get(day)
        out.append({"day": day.isoformat(), "done": bool(results) and all(results),
                    "solved": sum(1 for r in (results or []) if r == "solved"), "total": len(results or [])})
    return out
