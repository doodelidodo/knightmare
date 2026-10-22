"""Das eigene Repertoire, aus den eigenen Partien gelesen.

Eroeffnungen sind der Teil des Spiels, der sich wiederholt: dieselbe Stellung
kommt zehnmal aufs Brett, und wer dort einmal falsch spielt, spielt meist
jedes Mal falsch. Genau das sucht dieses Modul.

Gearbeitet wird ueber *Stellungen*, nicht ueber Eroeffnungsnamen und nicht
ueber Zugfolgen: der Schluessel ist die FEN ohne Zugzaehler. Damit fallen
Zugumstellungen zusammen (1.Nf3 d5 2.d4 = 1.d4 d5 2.Nf3), und Chess.com- und
Lichess-Partien ohnehin, egal wie sie die Eroeffnung benennen.

Pro eigener Stellung liegt fuer jeden Zug in der Datenbank, was er gekostet
hat (`win_loss`, `category`) und - bei Fehlern - der beste Zug. Mehr braucht
es nicht: keine Engine, kein Netz, nur das PGN nachspielen.

Zwei Sichten:
  * explore(): ein Zugbaum wie der Lichess-Explorer, aber nur ueber die
    eigenen Partien, mit Ergebnis und eigener Genauigkeit an jedem Knoten.
  * leaks(): wiederkehrende Fehler - Stellungen, in denen derselbe
    schwache Zug mindestens zweimal gespielt wurde. Das sind die billigsten
    Punkte, die es im Schach gibt.
"""

from __future__ import annotations

import io
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Optional

import chess
import chess.pgn
from sqlmodel import Session, select

from .models import ChessGame, ChessMove, utc_now
from .review import move_accuracy

# So weit wird jede Partie nachgespielt (Halbzuege). Deckt sich mit der
# Eroeffnungsphase der Analyse (CHESS_OPENING_PLIES, Vorgabe 20) - weiter
# hinten wiederholen sich Stellungen kaum noch.
MAX_PLY = 20
START_KEY = " ".join(chess.STARTING_FEN.split()[:4])
SCORE = {"win": 1.0, "draw": 0.5, "loss": 0.0}


def position_key(board: chess.Board) -> str:
    """FEN ohne Halbzug- und Zugzaehler: gleiche Stellung, gleicher Schluessel.
    En-passant nur, wenn er wirklich moeglich ist (python-chess' Vorgabe)."""
    return " ".join(board.fen().split()[:4])


# --------------------------------------------------------------------------
# PGN -> Zugfolge, zwischengespeichert
# --------------------------------------------------------------------------
# Das Nachspielen ist der einzige teure Schritt (~1-2 ms pro Partie). Eine
# Partie aendert ihr PGN nie, also reicht ein Speicher pro Partie-ID mit der
# PGN-Laenge als Plausibilitaetspruefung. Schluessel ist die uuid, nicht die
# ID: online hat jeder Spieler seine eigene Datenbank, IDs wiederholen sich.
_cache: dict[str, tuple[int, list[tuple[str, str, str]]]] = {}
_CACHE_MAX = 20000
_cache_lock = threading.Lock()


def _sequence(game: ChessGame, max_ply: int = MAX_PLY) -> list[tuple[str, str, str]]:
    """[(Stellungsschluessel vor dem Zug, SAN, UCI), ...] fuer die ersten Halbzuege."""
    if game.uuid:
        with _cache_lock:
            hit = _cache.get(game.uuid)
        if hit and hit[0] == len(game.pgn or ""):
            return hit[1][:max_ply]
    out: list[tuple[str, str, str]] = []
    try:
        parsed = chess.pgn.read_game(io.StringIO(game.pgn or ""))
    except Exception:  # noqa: BLE001 - kaputtes PGN: Partie faellt einfach heraus
        parsed = None
    if parsed is not None:
        board = parsed.board()
        if board.fen() == chess.STARTING_FEN:  # Partien aus Stellungen (Chess960 ...) nicht
            for node in parsed.mainline():
                if len(out) >= MAX_PLY:
                    break
                move = node.move
                try:
                    san = board.san(move)
                except (ValueError, AssertionError):
                    break
                out.append((position_key(board), san, move.uci()))
                board.push(move)
    if game.uuid:
        with _cache_lock:
            if len(_cache) >= _CACHE_MAX:
                _cache.clear()
            _cache[game.uuid] = (len(game.pgn or ""), out)
    return out[:max_ply]


# --------------------------------------------------------------------------
# Sammeln
# --------------------------------------------------------------------------
@dataclass
class Play:
    """Ein Zug in einer Stellung, in einer bestimmten Partie."""

    game: ChessGame
    ply: int
    san: str
    uci: str
    own: bool
    move: Optional[ChessMove]  # nur bei eigenen Zuegen, wenn analysiert


@dataclass
class Node:
    key: str
    plays: list[Play] = field(default_factory=list)
    paths: Counter = field(default_factory=Counter)
    own_to_move: bool = False


def _load(
    session: Session,
    color: str,
    days: Optional[int],
    time_class: Optional[str],
    platform: Optional[str],
) -> dict[str, Node]:
    statement = select(ChessGame).where(ChessGame.color == color)
    if days:
        statement = statement.where(ChessGame.played_at >= utc_now() - timedelta(days=days))
    if time_class:
        statement = statement.where(ChessGame.time_class == time_class)
    if platform:
        statement = statement.where(ChessGame.platform == platform)
    games = [g for g in session.exec(statement).all() if g.pgn]
    games.sort(key=lambda g: g.played_at)

    moves: dict[tuple[int, int], ChessMove] = {}
    ids = [g.id for g in games if g.id is not None]
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        statement_m = (
            select(ChessMove)
            .where(ChessMove.game_id.in_(chunk))  # type: ignore[attr-defined]
            .where(ChessMove.ply <= MAX_PLY)
        )
        for move in session.exec(statement_m).all():
            moves[(move.game_id, move.ply)] = move

    own_turn = chess.WHITE if color == "white" else chess.BLACK
    nodes: dict[str, Node] = {}
    for game in games:
        path: list[str] = []
        for index, (key, san, uci) in enumerate(_sequence(game)):
            ply = index + 1
            # Wer am Zug ist, steht im Schluessel (zweites Feld).
            to_move_white = key.split()[1] == "w"
            own = to_move_white == (own_turn == chess.WHITE)
            node = nodes.get(key)
            if node is None:
                node = nodes[key] = Node(key=key, own_to_move=own)
            node.paths[" ".join(path)] += 1
            node.plays.append(
                Play(game=game, ply=ply, san=san, uci=uci, own=own,
                     move=moves.get((game.id, ply)) if own else None)
            )
            path.append(san)
    return nodes


def format_path(path: str) -> str:
    """"e4 e5 Nf3" -> "1.e4 e5 2.Nf3"."""
    out = []
    for index, san in enumerate(path.split()):
        out.append(f"{index // 2 + 1}.{san}" if index % 2 == 0 else san)
    return " ".join(out)


def _path_of(node: Node) -> str:
    return node.paths.most_common(1)[0][0] if node.paths else ""


def _fen(key: str) -> str:
    return key + " 0 1"


def _score(games: list[ChessGame]) -> Optional[float]:
    scored = [SCORE[g.result] for g in games if g.result in SCORE]
    return round(sum(scored) / len(scored) * 100, 1) if scored else None


def _uci_of(key: str, san: Optional[str]) -> Optional[str]:
    if not san:
        return None
    try:
        return chess.Board(_fen(key)).parse_san(san).uci()
    except ValueError:
        return None


def _trail(path: str) -> list[dict[str, str]]:
    """Fuer die Brotkrumen im Explorer: jeder Zug des Weges mit dem
    Schluessel der Stellung *nach* dem Zug."""
    board = chess.Board()
    out = []
    for san in path.split():
        try:
            board.push_san(san)
        except ValueError:
            break
        out.append({"san": san, "key": position_key(board)})
    return out


def _legal(key: str) -> list[str]:
    return [m.uci() for m in chess.Board(_fen(key)).legal_moves]


def _last_move_uci(path: str) -> Optional[str]:
    """Der Zug, der in die Stellung gefuehrt hat - fuer die gelbe Markierung."""
    sans = path.split()
    if not sans:
        return None
    board = chess.Board()
    try:
        for san in sans[:-1]:
            board.push_san(san)
        return board.parse_san(sans[-1]).uci()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Explorer
# --------------------------------------------------------------------------
def explore(
    session: Session,
    color: str = "white",
    key: Optional[str] = None,
    days: Optional[int] = None,
    time_class: Optional[str] = None,
    platform: Optional[str] = None,
) -> dict[str, Any]:
    color = color if color in {"white", "black"} else "white"
    nodes = _load(session, color, days, time_class, platform)
    key = key or START_KEY
    node = nodes.get(key)
    path = _path_of(node) if node else ""
    children: dict[str, list[Play]] = defaultdict(list)
    for play in node.plays if node else []:
        children[play.san].append(play)

    rows = []
    for san, plays in children.items():
        games = [p.game for p in plays]
        board = chess.Board(_fen(key))
        try:
            board.push_san(san)
            next_key = position_key(board)
        except ValueError:
            next_key = None
        row: dict[str, Any] = {
            "san": san,
            "uci": plays[0].uci,
            "games": len(plays),
            "score": _score(games),
            "wins": sum(1 for g in games if g.result == "win"),
            "draws": sum(1 for g in games if g.result == "draw"),
            "losses": sum(1 for g in games if g.result == "loss"),
            "next_key": next_key if next_key in nodes else None,
            "last_played": max(g.played_at for g in games).isoformat(),
        }
        analysed = [p.move for p in plays if p.move is not None]
        if node and node.own_to_move and analysed:
            errors = [m for m in analysed if m.category != "ok"]
            best = Counter(m.best_move_san for m in errors if m.best_move_san)
            row.update(
                accuracy=round(sum(move_accuracy(m.win_loss) for m in analysed) / len(analysed), 1),
                avg_loss=round(sum(m.win_loss or 0.0 for m in analysed) / len(analysed), 1),
                errors=len(errors),
                worst=max((m.category for m in errors),
                          key=lambda c: ("inaccuracy", "mistake", "blunder").index(c)
                          if c in ("inaccuracy", "mistake", "blunder") else -1,
                          default=None),
                better=best.most_common(1)[0][0] if best else None,
            )
        rows.append(row)
    rows.sort(key=lambda r: (-r["games"], r["san"]))

    total_games = len({p.game.id for p in node.plays}) if node else 0
    return {
        "color": color,
        "key": key,
        "fen": _fen(key),
        "path": path,
        "path_text": format_path(path),
        "trail": _trail(path),
        "last_move": _last_move_uci(path),
        "own_to_move": bool(node.own_to_move) if node else (color == "white") == (key.split()[1] == "w"),
        "games": total_games,
        "score": _score([p.game for p in node.plays]) if node else None,
        "moves": rows,
        "start": key == START_KEY,
    }


# --------------------------------------------------------------------------
# Wiederkehrende Fehler
# --------------------------------------------------------------------------
# Ein Zug gilt als "wiederkehrender Fehler", wenn er in derselben Stellung
# mindestens MIN_REPEAT-mal gespielt wurde und mindestens MIN_REPEAT-mal als
# Fehler (Ungenauigkeit oder schlimmer) eingestuft ist. Zweimal ist bewusst
# niedrig: in der Eroeffnung ist dieselbe Stellung mit demselben Zug kein
# Zufall, und beim zweiten Mal ist es schon eine Gewohnheit.
MIN_REPEAT = 2
SEVERITY = {"inaccuracy": 1, "mistake": 2, "blunder": 3}


def leaks(
    session: Session,
    color: Optional[str] = None,
    days: Optional[int] = None,
    time_class: Optional[str] = None,
    platform: Optional[str] = None,
    limit: int = 30,
) -> dict[str, Any]:
    colors = [color] if color in {"white", "black"} else ["white", "black"]
    rows: list[dict[str, Any]] = []
    once = 0
    for side in colors:
        nodes = _load(session, side, days, time_class, platform)
        for node in nodes.values():
            if not node.own_to_move:
                continue
            by_move: dict[str, list[Play]] = defaultdict(list)
            for play in node.plays:
                by_move[play.san].append(play)
            for san, plays in by_move.items():
                rated = [p for p in plays if p.move is not None]
                errors = [p for p in rated if p.move.category != "ok"]  # type: ignore[union-attr]
                if not errors:
                    continue
                if len(errors) < MIN_REPEAT:
                    once += 1
                    continue
                rows.append(_leak_row(node, side, san, plays, errors, by_move))
    rows.sort(key=lambda r: (-r["cost"], -r["times"]))
    return {
        "color": color or "all",
        "leaks": rows[:limit],
        "total": len(rows),
        "single_errors": once,
        "min_repeat": MIN_REPEAT,
        "max_ply": MAX_PLY,
    }


def _leak_row(
    node: Node,
    side: str,
    san: str,
    plays: list[Play],
    errors: list[Play],
    by_move: dict[str, list[Play]],
) -> dict[str, Any]:
    path = _path_of(node)
    error_moves = [p.move for p in errors if p.move is not None]
    best_counter = Counter(m.best_move_san for m in error_moves if m.best_move_san)
    best_san = best_counter.most_common(1)[0][0] if best_counter else None
    worst = max(error_moves, key=lambda m: SEVERITY.get(m.category, 0))
    # Was sonst in dieser Stellung gespielt wurde - und ob es besser lief.
    others = []
    for other_san, other_plays in by_move.items():
        if other_san == san:
            continue
        rated = [p.move for p in other_plays if p.move is not None]
        others.append(
            {
                "san": other_san,
                "times": len(other_plays),
                "ok": bool(rated) and all(m.category == "ok" for m in rated),
            }
        )
    others.sort(key=lambda o: -o["times"])
    # Verlauf: jedes Mal, wenn die Stellung auf dem Brett stand, in
    # zeitlicher Reihenfolge - hat sich die Gewohnheit schon geaendert?
    history = []
    for play in sorted(node.plays, key=lambda p: p.game.played_at):
        history.append(
            {
                "played_at": play.game.played_at.isoformat(),
                "san": play.san,
                "same": play.san == san,
                "ok": play.move is not None and play.move.category == "ok",
                "result": play.game.result,
                "game_id": play.game.id,
                "url": play.game.url,
            }
        )
    since_last = 0
    for item in reversed(history):
        if item["same"]:
            break
        since_last += 1
    games = [p.game for p in plays]
    return {
        "id": f"{side}:{node.key}:{san}",
        "color": side,
        "key": node.key,
        "fen": _fen(node.key),
        "path": path,
        "path_text": format_path(path),
        "move_number": len(path.split()) // 2 + 1,
        "last_move": _last_move_uci(path),
        "san": san,
        "uci": plays[0].uci,
        "times": len(plays),
        "times_error": len(errors),
        "reached": len(node.plays),
        "category": worst.category,
        "avg_loss": round(sum(m.win_loss or 0.0 for m in error_moves) / len(error_moves), 1),
        "cost": round(sum(m.win_loss or 0.0 for m in error_moves), 1),
        "best_san": best_san,
        "best_uci": _uci_of(node.key, best_san),
        "legal": _legal(node.key),
        "score": _score(games),
        "others": others[:4],
        "history": history,
        "since_last": since_last,
        "last_played": max(g.played_at for g in games).isoformat(),
        "example_id": error_moves[-1].id,
    }


# --------------------------------------------------------------------------
# Ueben: ist dieser Zug in dieser Stellung gut genug?
# --------------------------------------------------------------------------
def check_drill(eng, fen: str, uci: str, tolerance: float = 4.0, seconds: float = 0.5) -> dict[str, Any]:
    """Wie beim Taktiktraining: ein Zug zaehlt, wenn er hoechstens `tolerance`
    Prozentpunkte schlechter ist als der beste. In der Eroeffnung gibt es oft
    mehrere gute Zuege - die sollen nicht als falsch gelten."""
    from .tactics import _win_after, mate_for, win_for

    board = chess.Board(fen)
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        return {"result": "illegal"}
    if move not in board.legal_moves:
        return {"result": "illegal"}
    us = board.turn
    san = board.san(move)
    info = eng.analyse(board, multipv=1, seconds=seconds)[0]
    best = info["pv"][0] if info.get("pv") else None
    win_best = win_for(info["score"], us)
    mate_best = mate_for(info["score"], us)
    after = board.copy()
    after.push(move)
    win_played, mate_played, pv = _win_after(eng, after, us)
    if move == best:
        ok = True
    elif mate_best is not None:
        ok = mate_played is not None and mate_played <= mate_best
    else:
        ok = win_played >= win_best - tolerance
    reply = pv[0] if pv else None
    return {
        "result": "good" if ok else "bad",
        "san": san,
        "best_san": board.san(best) if best else None,
        "best_uci": best.uci() if best else None,
        "win_best": round(win_best, 1),
        "win_played": round(win_played, 1),
        "reply_san": after.san(reply) if reply else None,
        "reply_uci": reply.uci() if reply else None,
    }
