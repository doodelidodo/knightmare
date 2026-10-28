"""Uebungen jenseits der Taktikaufgaben, alle aus den eigenen Partien.

Gewinn verwerten (convert)
    Die Fokus-Karte zeigt es: ein grosser Teil der Niederlagen kommt aus
    Stellungen, die schon klar gewonnen waren. Hier wird genau so eine Stellung
    gegen Stockfish zu Ende gespielt. Ausgangspunkt ist die Stellung vor dem
    teuersten eigenen Zug, solange die Gewinnchance noch mindestens 70 % war.

Was droht? (threat)
    Der haeufigste Kipp-Zug ist ein uebersehener Gegnerzug. Gezeigt wird die
    Stellung vor dem eigenen Fehler; gesucht ist nicht der eigene Zug, sondern
    das, was der Gegner vorhat - "wenn er jetzt nochmal ziehen duerfte". Das
    ist genau die Frage, die man sich vor jedem Zug stellen soll.

Woodpecker
    Ein fester Satz eigener Aufgaben, in Runden wiederholt, jede Runde
    schneller. Gezaehlt wird die reine Loesezeit, die der Browser misst.

Alles zustandslos beim Ziehen: der Browser schickt die ganze Zugfolge mit,
der Server spielt nach und prueft. Gespeichert wird nur das Ergebnis.
"""

from __future__ import annotations

import io
import json
import logging
import threading
from datetime import date, datetime
from typing import Any, Optional

import chess
import chess.pgn
from sqlalchemy import select, update
from sqlalchemy.engine import Engine as DbEngine

from .analysis import win_percent
from .models import ChessGame, ChessMove, ChessPractice, ChessTactic, ChessWoodpecker, utc_now
from .tactics import Engine, MoveError, TacticSettings, legal_moves, mate_for, scope_where, upsert, win_for

log = logging.getLogger(__name__)

PRACTICE = ChessPractice.__table__
WOOD = ChessWoodpecker.__table__
TACTICS = ChessTactic.__table__
GAMES = ChessGame.__table__
MOVES = ChessMove.__table__

# Gewinn verwerten
CONVERT_MIN_WIN = 70.0     # so viel muss vor dem teuersten Zug noch da sein
CONVERT_PEAK = 75.0        # ... und irgendwann in der Partie mindestens so viel
CONVERT_SUCCESS = 97.0     # entschieden (Engine-Bewertung ab ~ +9 Bauern)
CONVERT_HOLD = 85.0        # nach CONVERT_MOVES eigenen Zuegen noch so viel = geschafft
CONVERT_FAIL = 60.0        # darunter: zu viel hergegeben - in jedem Fall
CONVERT_SLACK = 20.0       # ... und hoechstens so viel unter dem Start (gedeckelt bei 90)


def convert_floor(win_start: Optional[float]) -> float:
    """Wer von 97 % auf 65 % faellt, hat den Gewinn hergegeben, auch wenn er
    noch besser steht. Die Grenze haengt darum am Start: 20 Punkte darunter,
    aber nie unter 60 %."""
    return round(max(CONVERT_FAIL, min(win_start or 0.0, 90.0) - CONVERT_SLACK), 1)
CONVERT_MOVES = 15
CONVERT_SECONDS = 0.3
# Was droht?
THREAT_MIN_GAIN = 15.0     # so viel muss die Drohung dem Gegner bringen
THREAT_TOLERANCE = 5.0     # ein anderer Gegnerzug zaehlt, wenn er fast so stark ist
THREAT_ERRORS = ("missed_threat", "hanging_piece", "fork", "allowed_mate", "bad_trade")
# Woodpecker
WOOD_SET = 24
WOOD_MIN = 8

_prepare_lock = threading.Lock()


def practice_key(kind: str, game_uuid: str, ply: int) -> str:
    return f"{kind}:{game_uuid}#{ply}"


def _board_before(pgn_text: str, ply: int) -> tuple[Optional[chess.Board], Optional[chess.Move], Optional[chess.Move]]:
    """Stellung vor Halbzug ply, der Partiezug dort und der Zug davor."""
    try:
        game = chess.pgn.read_game(io.StringIO(pgn_text or ""))
    except Exception:  # noqa: BLE001
        return None, None, None
    if game is None:
        return None, None, None
    moves = list(game.mainline_moves())
    if ply < 1 or ply > len(moves):
        return None, None, None
    board = game.board()
    for move in moves[: ply - 1]:
        board.push(move)
    return board, moves[ply - 1], (moves[ply - 2] if ply >= 2 else None)


# ---------------------------------------------------------------------------
# Kandidaten
# ---------------------------------------------------------------------------
def convert_candidates(conn, scope: Optional[TacticSettings] = None) -> list[dict]:
    """Pro verlorener oder remisierter Partie hoechstens eine Stellung: vor dem
    teuersten eigenen Zug, solange noch >= CONVERT_MIN_WIN da waren."""
    m, g = MOVES.c, GAMES.c
    rows = conn.execute(
        select(m.game_id, m.ply, m.cp_before, m.win_loss, m.san, m.best_move_san, g.uuid, g.result)
        .select_from(MOVES.join(GAMES, g.id == m.game_id))
        .where(g.result.in_(("loss", "draw")), scope_where(g.platform, g.time_class, scope, allow_any=False))
    ).all()
    by_game: dict[int, list] = {}
    for r in rows:
        by_game.setdefault(r.game_id, []).append(r)
    out = []
    for game_id, items in by_game.items():
        if max(win_percent(r.cp_before) for r in items) < CONVERT_PEAK:
            continue
        pool = [r for r in items if win_percent(r.cp_before) >= CONVERT_MIN_WIN and (r.win_loss or 0) > 0]
        if not pool:
            continue
        pick = max(pool, key=lambda r: r.win_loss or 0)
        out.append({"game_id": game_id, "ply": pick.ply, "uuid": pick.uuid, "result": pick.result,
                    "played_san": pick.san, "best_san": pick.best_move_san,
                    "win_start": round(win_percent(pick.cp_before), 1),
                    "priority": round(pick.win_loss or 0, 2)})
    return out


def threat_candidates(conn, scope: Optional[TacticSettings] = None) -> list[dict]:
    m, g = MOVES.c, GAMES.c
    rows = conn.execute(
        select(m.game_id, m.ply, m.cp_before, m.win_loss, m.san, m.best_move_san, m.refutation_san,
               m.error_type, g.uuid, g.result)
        .select_from(MOVES.join(GAMES, g.id == m.game_id))
        .where(m.error_type.in_(THREAT_ERRORS), m.category.in_(("mistake", "blunder")),
               m.refutation_san.is_not(None), scope_where(g.platform, g.time_class, scope, allow_any=False))
    ).all()
    return [{"game_id": r.game_id, "ply": r.ply, "uuid": r.uuid, "result": r.result,
             "played_san": r.san, "best_san": r.best_move_san, "refutation_san": r.refutation_san,
             "win_start": round(win_percent(r.cp_before), 1),
             # uebersehene Drohung zuerst - da ist die Frage "was droht?" exakt die richtige
             "priority": round((r.win_loss or 0) + (20 if r.error_type == "missed_threat" else 0), 2)}
            for r in rows]


def _base(kind: str, cand: dict, game) -> dict:
    return {
        "key": practice_key(kind, cand["uuid"], cand["ply"]), "kind": kind, "game_uuid": cand["uuid"],
        "ply": cand["ply"], "url": (game.url or "")[:300] if game else "", "played_at": game.played_at if game else None,
        "platform": game.platform if game else None, "time_class": game.time_class if game else None,
        "color": game.color if game else None, "opponent": (game.opponent or "")[:60] if game else "",
        "result": cand.get("result"), "played_san": cand.get("played_san"), "best_san": cand.get("best_san"),
        "win_start": cand.get("win_start"), "priority": cand.get("priority"), "attempts": 0, "solved": 0,
        "prepared_at": utc_now(),
    }


def prepare_convert(eng: Engine, cand: dict, game) -> dict:
    base = _base("convert", cand, game)
    board, _played, last = _board_before(game.pgn if game else "", cand["ply"])
    if board is None:
        return {**base, "status": "unsuitable", "reason": "pgn_unreadable"}
    us = board.turn
    info = eng.analyse(board, seconds=CONVERT_SECONDS * 2)[0]
    win = win_for(info["score"], us)
    mate = mate_for(info["score"], us)
    if mate is not None and mate <= 3:
        # Ein kurzes Matt ist eine Taktikaufgabe, keine Verwertung.
        return {**base, "status": "unsuitable", "reason": "short_mate"}
    if win < CONVERT_MIN_WIN:
        return {**base, "status": "unsuitable", "reason": f"not_winning ({win:.0f})"}
    return {**base, "status": "ready", "reason": None, "fen": board.fen(),
            "last_move": last.uci() if last else None, "win_start": round(win, 1)}


def prepare_threat(eng: Engine, cand: dict, game) -> dict:
    base = _base("threat", cand, game)
    board, _played, last = _board_before(game.pgn if game else "", cand["ply"])
    if board is None:
        return {**base, "status": "unsuitable", "reason": "pgn_unreadable"}
    if board.is_check():
        return {**base, "status": "unsuitable", "reason": "in_check"}
    us = board.turn
    them = not us
    now = eng.analyse(board)[0]
    win_them_now = 100.0 - win_for(now["score"], us)
    null = null_board(board)
    if not null.is_valid():
        return {**base, "status": "unsuitable", "reason": "null_invalid"}
    info = eng.analyse(null, multipv=1)[0]
    if not info.get("pv"):
        return {**base, "status": "unsuitable", "reason": "no_engine_result"}
    threat = info["pv"][0]
    win_them_threat = win_for(info["score"], them)
    gain = win_them_threat - win_them_now
    if gain < THREAT_MIN_GAIN:
        return {**base, "status": "unsuitable", "reason": f"no_threat ({gain:.0f})"}
    return {**base, "status": "ready", "reason": None, "fen": board.fen(),
            "last_move": last.uci() if last else None, "solution": threat.uci(),
            "solution_san": null.san(threat)}


def ensure(db: DbEngine, eng: Engine, kind: str, want: int = 12, max_tries: int = 30,
           scope: Optional[TacticSettings] = None) -> dict:
    """Bereitet bis zu `want` neue Stellungen vor (hoechstens max_tries
    Kandidaten gerechnet). Ohne Engine passiert nichts."""
    result = {"ready": 0, "unsuitable": 0}
    if not eng.available or not _prepare_lock.acquire(blocking=False):
        return result
    try:
        with db.connect() as conn:
            known = {r[0] for r in conn.execute(select(PRACTICE.c.key).where(PRACTICE.c.kind == kind))}
            ready = conn.execute(
                select(PRACTICE.c.key).where(PRACTICE.c.kind == kind, PRACTICE.c.status == "ready",
                                             PRACTICE.c.attempts == 0,
                                             scope_where(PRACTICE.c.platform, PRACTICE.c.time_class, scope))).all()
            cands = convert_candidates(conn, scope) if kind == "convert" else threat_candidates(conn, scope)
        if len(ready) >= want:
            return result
        cands = [c for c in cands if practice_key(kind, c["uuid"], c["ply"]) not in known]
        cands.sort(key=lambda c: -(c["priority"] or 0))
        need = want - len(ready)
        for cand in cands[:max_tries]:
            if result["ready"] >= need:
                break
            with db.connect() as conn:
                game = conn.execute(select(GAMES).where(GAMES.c.id == cand["game_id"])).first()
            try:
                row = (prepare_convert if kind == "convert" else prepare_threat)(eng, cand, game)
            except Exception as exc:  # noqa: BLE001
                log.warning("Uebung %s nicht vorbereitet: %s", cand, exc)
                row = {**_base(kind, cand, game), "status": "unsuitable", "reason": f"error: {exc}"[:120]}
            with db.begin() as conn:
                upsert(conn, PRACTICE, row, ["key"])
            result[row["status"] if row["status"] == "ready" else "unsuitable"] += 1
        return result
    finally:
        _prepare_lock.release()


def listing(db: DbEngine, kind: str, limit: int = 40, scope: Optional[TacticSettings] = None) -> dict:
    p = PRACTICE.c
    in_scope = scope_where(p.platform, p.time_class, scope)
    with db.connect() as conn:
        rows = conn.execute(
            select(PRACTICE).where(p.kind == kind, p.status == "ready", in_scope)
            # Offene zuerst, dann die zuletzt verfehlten, gemeisterte zuletzt.
            .order_by((p.solved > 0).asc(), p.attempts.asc(), p.priority.desc())
            .limit(limit)
        ).all()
        total = conn.execute(select(p.key).where(p.kind == kind, p.status == "ready", in_scope)).all()
        mastered = conn.execute(select(p.key).where(p.kind == kind, p.status == "ready", p.solved > 0,
                                                    in_scope)).all()
    return {"kind": kind, "items": [payload(r) for r in rows], "total": len(total), "mastered": len(mastered)}


def payload(row) -> dict:
    board = chess.Board(row.fen)
    return {
        "key": row.key, "kind": row.kind, "fen": row.fen, "orientation": row.color or "white",
        "to_move": "white" if board.turn else "black", "move_number": board.fullmove_number,
        "last_move": row.last_move, "legal": legal_moves(board),
        # Bei "Was droht?" zieht man die Figuren des Gegners
        "legal_them": _legal_them(board) if row.kind == "threat" else None,
        "win_start": row.win_start, "fail_below": convert_floor(row.win_start) if row.kind == "convert" else None,
        "attempts": row.attempts or 0, "solved": row.solved or 0,
        "last_result": row.last_result,
        "game": {"opponent": row.opponent, "played_at": row.played_at.date().isoformat() if row.played_at else None,
                 "platform": row.platform, "time_class": row.time_class, "url": row.url, "result": row.result},
    }


def null_board(board: chess.Board) -> chess.Board:
    """Dieselbe Stellung, aber der Gegner ist am Zug. Als frisches Brett aus
    der FEN - ein Nullzug im Verlauf ginge als "0000" an die Engine."""
    b = board.copy(stack=False)
    b.push(chess.Move.null())
    return chess.Board(b.fen())


def _legal_them(board: chess.Board) -> list[str]:
    return [m.uci() for m in null_board(board).legal_moves]


def _row(db: DbEngine, key: str, kind: str):
    with db.connect() as conn:
        row = conn.execute(select(PRACTICE).where(PRACTICE.c.key == key, PRACTICE.c.kind == kind)).first()
    if row is None or row.status != "ready":
        raise MoveError("unknown_task")
    return row


def record(db: DbEngine, key: str, solved: bool, today: Optional[date] = None) -> None:
    with db.begin() as conn:
        row = conn.execute(select(PRACTICE).where(PRACTICE.c.key == key)).first()
        if row is None:
            return
        conn.execute(update(PRACTICE).where(PRACTICE.c.key == key).values(
            attempts=(row.attempts or 0) + 1, solved=(row.solved or 0) + (1 if solved else 0),
            last_result="solved" if solved else "failed", last_seen=today or date.today(), updated_at=utc_now()))


# ---------------------------------------------------------------------------
# Gewinn verwerten: ein Zug, eine Antwort
# ---------------------------------------------------------------------------
def convert_move(db: DbEngine, eng: Engine, key: str, moves: list[str]) -> dict:
    """moves: alle Zuege seit der Ausgangsstellung, abwechselnd eigener Zug
    und Antwort der Engine, der letzte ist der neue eigene Zug."""
    row = _row(db, key, "convert")
    if not moves or len(moves) % 2 == 0:
        raise MoveError("expected_own_move")
    board = chess.Board(row.fen)
    us = board.turn
    for uci in moves:
        try:
            move = chess.Move.from_uci(uci)
        except ValueError as exc:
            raise MoveError("unknown_move") from exc
        if move not in board.legal_moves:
            raise MoveError("illegal_move")
        board.push(move)
    before_last = board.copy()
    before_last.pop()
    own_moves = (len(moves) + 1) // 2

    def finish(status: str, **extra: Any) -> dict:
        res = {"status": status, "fen": board.fen(), "own_moves": own_moves, **extra}
        if status in ("success", "fail"):
            record(db, key, status == "success")
            if status == "fail":
                best = eng.analyse(before_last, seconds=CONVERT_SECONDS)[0]
                pv = best.get("pv") or []
                if pv and pv[0].uci() != moves[-1]:
                    res["better_san"] = before_last.san(pv[0])
                    res["better_uci"] = pv[0].uci()
        return res

    if board.is_checkmate():
        return finish("success", reason="mate", win=100.0)
    if board.is_game_over(claim_draw=True):
        return finish("fail", reason="draw", win=50.0)
    info = eng.analyse(board, seconds=CONVERT_SECONDS)[0]
    pv = info.get("pv") or []
    if not pv:
        return finish("fail", reason="no_reply", win=win_for(info["score"], us))
    reply = pv[0]
    reply_san = board.san(reply)
    board.push(reply)
    if board.is_checkmate():
        return finish("fail", reason="mated", win=0.0, reply=reply.uci(), reply_san=reply_san)
    if board.is_game_over(claim_draw=True):
        return finish("fail", reason="draw", win=50.0, reply=reply.uci(), reply_san=reply_san)
    after = eng.analyse(board, seconds=CONVERT_SECONDS)[0]
    win = round(win_for(after["score"], us), 1)
    mate = mate_for(after["score"], us)
    common = {"win": win, "reply": reply.uci(), "reply_san": reply_san, "legal": legal_moves(board)}
    if win < convert_floor(row.win_start):
        return finish("fail", reason="gave_back", **common)
    if (mate is not None and mate <= 3) or (win >= CONVERT_SUCCESS and own_moves >= 3):
        return finish("success", reason="decisive", **common)
    if own_moves >= CONVERT_MOVES:
        return finish("success" if win >= CONVERT_HOLD else "fail", reason="limit", **common)
    return {"status": "continue", "fen": board.fen(), "own_moves": own_moves, **common}


def convert_giveup(db: DbEngine, key: str) -> dict:
    _row(db, key, "convert")
    record(db, key, False)
    return {"status": "fail", "reason": "gave_up"}


# ---------------------------------------------------------------------------
# Was droht?
# ---------------------------------------------------------------------------
def threat_check(db: DbEngine, eng: Engine, key: str, uci: str, reveal: bool = False) -> dict:
    row = _row(db, key, "threat")
    board = chess.Board(row.fen)
    null = null_board(board)
    them = null.turn
    explain = {"solution": row.solution, "solution_san": row.solution_san, "played_san": row.played_san,
               "best_san": row.best_san, "url": row.url}
    if reveal:
        record(db, key, False)
        return {"result": "revealed", **explain}
    try:
        move = chess.Move.from_uci(uci)
    except ValueError as exc:
        raise MoveError("unknown_move") from exc
    if move not in null.legal_moves:
        raise MoveError("illegal_move")
    san = null.san(move)
    ok = move.uci() == row.solution
    if not ok and eng.available:
        best = eng.analyse(null)[0]
        win_best = win_for(best["score"], them)
        after = null.copy()
        after.push(move)
        if after.is_checkmate():
            ok = True
        else:
            info = eng.analyse(after)[0]
            ok = win_for(info["score"], them) >= win_best - THREAT_TOLERANCE
    record(db, key, ok)
    return {"result": "good" if ok else "bad", "san": san, **explain}


# ---------------------------------------------------------------------------
# Woodpecker
# ---------------------------------------------------------------------------
def _current_round(conn):
    return conn.execute(select(WOOD).order_by(WOOD.c.cycle.desc(), WOOD.c.round.desc()).limit(1)).first()


def woodpecker_state(db: DbEngine, scope: Optional[TacticSettings] = None) -> dict:
    from .tactics import puzzle_payload

    with db.connect() as conn:
        cur = _current_round(conn)
        available = conn.execute(select(TACTICS.c.key).where(
            TACTICS.c.status == "ready", scope_where(TACTICS.c.platform, TACTICS.c.time_class, scope))).all()
        if cur is None:
            return {"active": False, "available": len(available), "min": WOOD_MIN, "set_size": WOOD_SET,
                    "rounds": []}
        rounds = conn.execute(select(WOOD).where(WOOD.c.cycle == cur.cycle).order_by(WOOD.c.round)).all()
        keys = cur.keys.split()
        rows = {r.key: r for r in conn.execute(select(TACTICS).where(TACTICS.c.key.in_(keys)))}
    results = json.loads(cur.results or "{}")
    items = [{"key": k, "result": results.get(k), "puzzle": puzzle_payload(rows[k])} for k in keys if k in rows]
    return {
        "active": True, "cycle": cur.cycle, "round": cur.round, "finished": cur.finished_at is not None,
        "seconds": round(cur.seconds or 0, 1), "items": items, "available": len(available),
        "min": WOOD_MIN, "set_size": WOOD_SET,
        "rounds": [{"round": r.round, "seconds": round(r.seconds or 0, 1),
                    "solved": sum(1 for v in json.loads(r.results or "{}").values() if v == "solved"),
                    "done": len(json.loads(r.results or "{}")), "total": len(r.keys.split()),
                    "finished": r.finished_at is not None} for r in rounds],
    }


def woodpecker_start(db: DbEngine, new_cycle: bool, scope: Optional[TacticSettings] = None) -> dict:
    """Neuer Satz (new_cycle) oder naechste Runde mit demselben Satz."""
    t = TACTICS.c
    with db.begin() as conn:
        cur = _current_round(conn)
        if new_cycle or cur is None:
            # Bekannte zuerst (schon einmal gesehen), dann nach Wichtigkeit -
            # Woodpecker trainiert Wiedererkennen, nicht Erstbegegnung.
            keys = [r.key for r in conn.execute(
                select(t.key).where(t.status == "ready", scope_where(t.platform, t.time_class, scope))
                .order_by((t.attempts > 0).desc(), t.priority.desc(), t.key).limit(WOOD_SET))]
            if len(keys) < WOOD_MIN:
                raise MoveError("not_enough_tasks")
            cycle, rnd = ((cur.cycle + 1) if cur else 1), 1
        else:
            keys = cur.keys.split()
            cycle, rnd = cur.cycle, cur.round + 1
        conn.execute(WOOD.insert().values(cycle=cycle, round=rnd, keys=" ".join(keys), results="{}",
                                          seconds=0.0, started_at=utc_now(), finished_at=None))
    return woodpecker_state(db, scope)


def woodpecker_record(db: DbEngine, key: str, solved: bool, seconds: float) -> dict:
    with db.begin() as conn:
        cur = _current_round(conn)
        if cur is None or key not in cur.keys.split():
            raise MoveError("unknown_task")
        results = json.loads(cur.results or "{}")
        counted = key not in results
        if counted:
            results[key] = "solved" if solved else "failed"
        finished = len(results) >= len(cur.keys.split())
        conn.execute(update(WOOD).where(WOOD.c.cycle == cur.cycle, WOOD.c.round == cur.round).values(
            results=json.dumps(results), seconds=(cur.seconds or 0) + max(0.0, min(float(seconds or 0), 900.0)),
            finished_at=utc_now() if finished and cur.finished_at is None else cur.finished_at))
    return {"counted": counted, "round_finished": finished}


def woodpecker_row(db: DbEngine, key: str):
    with db.connect() as conn:
        cur = _current_round(conn)
        if cur is None or key not in cur.keys.split():
            raise MoveError("unknown_task")
        row = conn.execute(select(TACTICS).where(TACTICS.c.key == key)).first()
    if row is None:
        raise MoveError("unknown_task")
    return row
