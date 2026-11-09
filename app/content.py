"""Fuers Video: die letzten 7 Tage als Rohstoff fuer YouTube und Reels (seit 1.19).

Knightmare kennt jede eigene Partie Zug fuer Zug mit Bewertung. Daraus
werden hier die *Momente* einer Woche, die eine Geschichte tragen - nicht
die lehrreichsten Fehler (dafuer gibt es das Training), sondern die mit
Dramatik:

  collapse     Absturz: aus einer klar guten Stellung in einem Zug weg
               ("von +7 auf -5 in einem Zug").
  missed_mate  Matt stand auf dem Brett und wurde nicht gespielt - das
               klassische Raetsel-Reel ("Matt in 3. Ich habe ... gespielt.").
  missed_tactic Gabel, Abzug, Spiess oder Fesselung verpasst (mit Fehler).
  punished     Der Gegner patzt, und du nutzt es sofort - der gute Moment.
  unpunished   Der Gegner patzt, und du siehst es nicht.
  comeback     Partie gewonnen oder remis, obwohl sie praktisch verloren war.
  clock        Auf Zeit verloren in guter Stellung, oder auf Zeit gewonnen in
               schlechter.

Alles hier kommt ohne Engine aus (Bewertungen liegen pro Zug in der
Datenbank). Gegnerzuege sind nicht gespeichert, ihr Effekt aber schon: der
Sprung zwischen der Bewertung nach dem eigenen Zug und vor dem naechsten
eigenen Zug ist genau das, was der Gegnerzug angerichtet hat.

Die Details eines Moments (Stellung, Stockfish-Varianten, Fakten, Export fuer
das Reel-Skript) rechnet `moment()` - auf Abruf, mit der Engine.
"""

from __future__ import annotations

import io
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Optional

import chess
import chess.pgn
from sqlmodel import Session, select

from .analysis import win_percent
from .journey import CHECKLIST, checklist_point
from .models import ChessGame, ChessMove, utc_now
from .review import move_accuracy

DAYS = 7
SERIOUS = ("mistake", "blunder")

COLLAPSE_FROM = 65.0       # so gut musste man stehen ...
COLLAPSE_LOSS = 30.0       # ... und so viel in einem Zug hergeben
GIFT = 20.0                # so viel muss ein Gegnerzug verschenken (Prozentpunkte Gewinnchance)
PUNISH_MAX_LOSS = 5.0      # eigener Zug danach: hoechstens so viel daneben = bestraft
UNPUNISH_MIN_LOSS = 20.0   # ... mindestens so viel = uebersehen
COMEBACK_LOW = 20.0        # so schlecht stand man irgendwann
CLOCK_GOOD = 60.0          # auf Zeit verloren, obwohl man so gut stand
TACTIC_MOTIFS = ("fork", "discovered", "skewer", "pin")
MAX_PER_GAME = 2
MAX_MOMENTS = 12


def _win(cp: Optional[int]) -> float:
    return round(win_percent(cp or 0), 1)


def _pawns(cp: Optional[int]) -> float:
    return round((cp or 0) / 100.0, 1)


def _game_info(game: ChessGame) -> dict[str, Any]:
    return {
        "id": game.id, "url": game.url, "platform": game.platform, "time_class": game.time_class,
        "played_at": game.played_at.isoformat(), "color": game.color, "result": game.result,
        "opponent": game.opponent, "opponent_rating": game.opponent_rating, "my_rating": game.my_rating,
        "opening": game.opening_family or game.opening_name, "termination": game.termination,
    }


def _on_time(game: ChessGame) -> bool:
    t = (game.termination or "").lower()
    return "time" in t or "zeit" in t or "flag" in t


def _moment(kind: str, score: float, game: ChessGame, move: Optional[ChessMove], **extra: Any) -> dict[str, Any]:
    out = {"kind": kind, "score": round(score, 1), "game": _game_info(game), "ply": None, "move_id": None}
    if move is not None:
        out.update({
            "ply": move.ply, "move_id": move.id, "move_number": move.move_number, "san": move.san,
            "category": move.category, "error_type": move.error_type, "missed_motif": move.missed_motif,
            "mate_in": move.mate_in, "best_san": move.best_move_san, "refutation_san": move.refutation_san,
            "win_before": _win(move.cp_before), "win_after": _win(move.cp_after),
            "eval_before": _pawns(move.cp_before), "eval_after": _pawns(move.cp_after),
            "clock": move.clock_seconds, "point": checklist_point(move.error_type, move.missed_motif)
            if move.category in SERIOUS else None,
        })
    out.update(extra)
    return out


def game_moments(game: ChessGame, moves: list[ChessMove]) -> list[dict[str, Any]]:
    """Alle Momente einer Partie, noch ungefiltert."""
    moves = sorted(moves, key=lambda m: m.ply)
    out: list[dict[str, Any]] = []
    for m in moves:
        wb = _win(m.cp_before)
        if m.mate_in and 0 < m.mate_in <= 5 and m.category != "ok":
            out.append(_moment("missed_mate", 45 + (10 if m.mate_in <= 3 else 0) + min(m.win_loss or 0, 30) / 3, game, m))
        elif m.missed_motif in TACTIC_MOTIFS and m.category in SERIOUS:
            out.append(_moment("missed_tactic", 20 + (m.win_loss or 0) * 0.6, game, m))
        if wb >= COLLAPSE_FROM and (m.win_loss or 0) >= COLLAPSE_LOSS:
            bonus = 15 if game.result != "win" else 0
            out.append(_moment("collapse", (m.win_loss or 0) + bonus + (wb - COLLAPSE_FROM) / 2, game, m))
    # Gegnerzuege: Sprung zwischen nach-eigenem-Zug und vor-naechstem-eigenem-Zug.
    for prev, nxt in zip(moves, moves[1:]):
        if nxt.ply != prev.ply + 2:
            continue
        gift = _win(nxt.cp_before) - _win(prev.cp_after)
        if gift < GIFT:
            continue
        loss = nxt.win_loss or 0
        common = {"gift": round(gift, 1), "win_before_gift": _win(prev.cp_after),
                  "eval_before_gift": _pawns(prev.cp_after)}
        if loss <= PUNISH_MAX_LOSS:
            out.append(_moment("punished", gift * 0.8, game, nxt, **common))
        elif loss >= UNPUNISH_MIN_LOSS:
            out.append(_moment("unpunished", gift * 0.7 + loss * 0.3, game, nxt, **common))
    if moves:
        low = min(moves, key=lambda m: m.cp_before)
        lowest = _win(low.cp_before)
        if game.result in ("win", "draw") and lowest <= COMEBACK_LOW:
            score = (COMEBACK_LOW - lowest) + (25 if game.result == "win" else 10)
            out.append(_moment("comeback", score, game, low, lowest=lowest))
        last = moves[-1]
        # Wer mit dem letzten Zug selbst mattsetzt, hat nicht auf Zeit verloren.
        if _on_time(game) and not (last.san or "").endswith("#"):
            final = _win(last.cp_after)
            if game.result == "loss" and final >= CLOCK_GOOD:
                out.append(_moment("clock", 20 + final - CLOCK_GOOD, game, last, final=final))
            elif game.result in ("win", "draw") and final <= 100 - CLOCK_GOOD:
                out.append(_moment("clock", 20 + (100 - CLOCK_GOOD) - final, game, last, final=final))
    return out


def pick(moments: list[dict[str, Any]], limit: int = MAX_MOMENTS) -> list[dict[str, Any]]:
    """Die besten, hoechstens MAX_PER_GAME pro Partie und nie zweimal derselbe
    Zug (ein Absturz mit verpasstem Matt ist *ein* Moment - der staerkere zaehlt)."""
    out: list[dict[str, Any]] = []
    per_game: Counter = Counter()
    seen: set[tuple] = set()
    for m in sorted(moments, key=lambda x: -x["score"]):
        key = (m["game"]["id"], m["ply"])
        if key in seen or per_game[m["game"]["id"]] >= MAX_PER_GAME:
            continue
        seen.add(key)
        per_game[m["game"]["id"]] += 1
        out.append(m)
        if len(out) >= limit:
            break
    return out


def week(session: Session, time_class: Optional[str] = None, platform: Optional[str] = None,
         offset: int = 0, now: Optional[datetime] = None, days: int = DAYS) -> dict[str, Any]:
    """Die letzten `days` Tage (offset: so viele Zeitraeume zurueck)."""
    now = now or utc_now()
    end = now - timedelta(days=days * offset)
    start = end - timedelta(days=days)
    statement = select(ChessGame).where(ChessGame.played_at >= start, ChessGame.played_at < end,
                                        ChessGame.analyzed_at.is_not(None))
    if time_class:
        statement = statement.where(ChessGame.time_class == time_class)
    if platform:
        statement = statement.where(ChessGame.platform == platform)
    games = sorted(session.exec(statement).all(), key=lambda g: g.played_at)
    ids = [g.id for g in games]
    by_game: dict[int, list[ChessMove]] = {}
    for chunk in range(0, len(ids), 400):
        for mv in session.exec(select(ChessMove).where(ChessMove.game_id.in_(ids[chunk:chunk + 400]))).all():
            by_game.setdefault(mv.game_id, []).append(mv)

    moments: list[dict[str, Any]] = []
    rows = []
    errors: Counter = Counter()
    points: Counter = Counter()
    gifts = punished = 0
    for g in games:
        mv = by_game.get(g.id, [])
        if not mv:
            continue
        found = game_moments(g, mv)
        moments.extend(found)
        gifts += sum(1 for f in found if f["kind"] in ("punished", "unpunished"))
        punished += sum(1 for f in found if f["kind"] == "punished")
        serious = [m for m in mv if m.category in SERIOUS]
        for m in serious:
            errors[m.error_type or "positional"] += 1
            p = checklist_point(m.error_type, m.missed_motif)
            if p:
                points[p] += 1
        acc = round(sum(move_accuracy(m.win_loss) for m in mv) / len(mv), 1)
        swing_max = max(_win(m.cp_before) for m in mv)
        swing_min = min(_win(m.cp_before) for m in mv)
        rows.append({**_game_info(g), "accuracy": acc, "moves": len(mv),
                     "blunders": sum(1 for m in mv if m.category == "blunder"),
                     "mistakes": sum(1 for m in mv if m.category == "mistake"),
                     "best_win": swing_max, "worst_win": swing_min,
                     "moments": len(found)})

    rated = [g for g in games if g.my_rating]
    results = Counter(g.result for g in games)
    accs = [r["accuracy"] for r in rows]
    best = max(rows, key=lambda r: (r["blunders"] == 0, r["accuracy"]), default=None)
    wild = max(rows, key=lambda r: r["best_win"] - r["worst_win"], default=None)
    return {
        "start": start.isoformat(), "end": end.isoformat(), "days": days, "offset": offset,
        "summary": {
            "games": len(rows), "wins": results.get("win", 0), "draws": results.get("draw", 0),
            "losses": results.get("loss", 0),
            "rating_start": rated[0].my_rating if rated else None, "rating_end": rated[-1].my_rating if rated else None,
            "accuracy": round(sum(accs) / len(accs), 1) if accs else None,
            "blunder_free": sum(1 for r in rows if r["blunders"] == 0),
            "blunders": sum(r["blunders"] for r in rows), "mistakes": sum(r["mistakes"] for r in rows),
            "top_error": errors.most_common(1)[0][0] if errors else None,
            "points": {p: points.get(p, 0) for p in CHECKLIST},
            "gifts": gifts, "punished": punished,
        },
        "best_game": best, "wildest_game": wild if wild and wild["best_win"] - wild["worst_win"] >= 50 else None,
        "games": rows,
        "moments": pick(moments),
        "moment_count": len(moments),
    }


# ---------------------------------------------------------------------------
# Ein Moment im Detail: Stellung, Varianten, Export
# ---------------------------------------------------------------------------
def _board_at(pgn: str, ply: int) -> tuple[Optional[chess.Board], Optional[chess.Move], Optional[chess.Move]]:
    try:
        game = chess.pgn.read_game(io.StringIO(pgn or ""))
    except Exception:  # noqa: BLE001
        return None, None, None
    if game is None:
        return None, None, None
    moves = list(game.mainline_moves())
    if ply < 1 or ply > len(moves):
        return None, None, None
    board = game.board()
    for mv in moves[: ply - 1]:
        board.push(mv)
    return board, moves[ply - 1], (moves[ply - 2] if ply >= 2 else None)


def moment(session: Session, move_id: int, eng) -> dict[str, Any]:
    """Details zu einem Moment: Stellung vor dem eigenen Zug, der gespielte
    Zug, der beste (Stockfish), die Widerlegung, und der Gegnerzug davor.
    Varianten mit Materialbilanz aus `coach`. Dazu der Export fuer ein
    Reel-Skript (FEN, Zuege in SAN und UCI, Link)."""
    from . import coach

    mv = session.get(ChessMove, move_id)
    if mv is None:
        return {"found": False}
    game = session.get(ChessGame, mv.game_id)
    board, played, last = _board_at(game.pgn if game else "", mv.ply)
    if board is None or played is None:
        return {"found": False}
    us = board.turn
    out: dict[str, Any] = {
        "found": True, "move_id": move_id, "fen": board.fen(), "orientation": game.color,
        "to_move": "white" if us else "black", "move_number": board.fullmove_number,
        "last": {"uci": last.uci(), "san": None} if last else None,
        "played": {"uci": played.uci(), "san": board.san(played)},
        "best": None, "refutation": None, "engine": bool(eng is not None and eng.available),
        "eval_before": _pawns(mv.cp_before), "eval_after": _pawns(mv.cp_after),
        "win_before": _win(mv.cp_before), "win_after": _win(mv.cp_after),
        "url": game.url, "pgn": game.pgn,
    }
    if last is not None and board.move_stack:
        prev = board.copy()
        prev.pop()
        out["last"]["san"] = prev.san(last)
    # Bester Zug: Engine, sonst aus der Analyse.
    best_move = None
    if eng is not None and eng.available:
        try:
            info = eng.analyse(board)[0]
            pv = list(info.get("pv") or [])
            if pv:
                best_move = pv[0]
        except Exception:  # noqa: BLE001
            best_move = None
    if best_move is None and mv.best_move_san:
        try:
            best_move = board.parse_san(mv.best_move_san)
        except ValueError:
            best_move = None
    if best_move is not None:
        line = coach.engine_line(eng, board, best_move) if eng is not None else [best_move]
        out["best"] = {"uci": best_move.uci(), "san": board.san(best_move), "same": best_move == played,
                       "line_uci": [m.uci() for m in line], "line_san": board.variation_san(line),
                       "line": coach.line_fact(board, line, us) if len(line) > 1 else None,
                       "facts": coach.describe_move(board, best_move, line=line if len(line) > 1 else None)}
    after = board.copy(stack=False)
    after.push(played)
    out["played"]["facts"] = coach.describe_move(board, played)
    if not after.is_game_over():
        ref = None
        if eng is not None and eng.available:
            try:
                pv = list(eng.analyse(after)[0].get("pv") or [])
                ref = pv[0] if pv else None
            except Exception:  # noqa: BLE001
                ref = None
        if ref is None and mv.refutation_san:
            try:
                ref = after.parse_san(mv.refutation_san)
            except ValueError:
                ref = None
        if ref is not None and (mv.category in SERIOUS):
            line = coach.engine_line(eng, after, ref) if eng is not None else [ref]
            out["refutation"] = {"uci": ref.uci(), "san": after.san(ref), "line_uci": [m.uci() for m in line],
                                 "line_san": after.variation_san(line),
                                 "line": coach.line_fact(after, line, us) if len(line) > 1 else None,
                                 "facts": coach.describe_move(after, ref, line=line if len(line) > 1 else None)}
    out["export"] = {
        "fen": out["fen"], "orientation": out["orientation"], "played": out["played"]["san"],
        "played_uci": out["played"]["uci"],
        "line": out["best"]["line_san"] if out["best"] else None,
        "line_uci": out["best"]["line_uci"] if out["best"] else None,
        "refutation": out["refutation"]["line_san"] if out["refutation"] else None,
        "refutation_uci": out["refutation"]["line_uci"] if out["refutation"] else None,
        "last_move": out["last"]["uci"] if out["last"] else None,
        "eval_before": out["eval_before"], "eval_after": out["eval_after"],
        "ref": game.url,
    }
    return out
