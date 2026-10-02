"""Taktikaufgaben aus den eigenen Fehlern.

Knightmare sagt, *welche* Fehler man macht - hier wird daraus Training: fuer
jeden Fehler "was waere besser gewesen?", taeglich eine kleine Auswahl, mit
Wiederholung nach Leitner.

Knightmare hat fuer jeden Fehler schon den besten Zug gespeichert, aber nur
einen einzigen und aus einer schnellen Rechnung. Fuer eine Aufgabe reicht das
nicht:

1. Die Stellung wird aus dem PGN nachgespielt (Halbzug ply = Index ply-1).
2. Stockfish rechnet sie mit zwei Hauptvarianten nach. Eine Aufgabe taugt nur,
   wenn der beste Zug klar besser ist als der zweitbeste (sonst gibt es keine
   Loesung, sondern nur "irgendwas ausser dem Partiezug") und klar besser als
   der Partiezug (sonst war es gar kein richtiger Fehler).
3. Solange der Gegner danach nur eine sinnvolle Antwort hat und fuer uns
   wieder genau ein Zug stark ist, verlaengert sich die Loesung - wie bei den
   Lichess-Puzzles. Matt-Folgen laufen bis zum Matt.

Beim Loesen gilt ein anderer Zug auch, wenn er genauso stark ist (hoechstens
alt_tolerance Prozentpunkte schlechter, bei Matt: Matt bleibt erhalten).

Die API liefert nur Schluessel (reason, Fehlercodes) - Texte macht die
Oberflaeche, in beiden Sprachen.
"""

from __future__ import annotations

import io
import logging
import math
import os
import shutil
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional

import chess
import chess.engine
import chess.pgn
from sqlalchemy import func, inspect, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine as DbEngine

from .config import engine_popen_args
from .models import ChessGame, ChessMove, ChessTactic, ChessTacticDay, utc_now

log = logging.getLogger(__name__)

TACTICS = ChessTactic.__table__
DAYS = ChessTacticDay.__table__
GAMES = ChessGame.__table__
MOVES = ChessMove.__table__

# Leitner-Stufen: Tage bis zur naechsten Wiederholung. Stufe 6 = gelernt.
INTERVALS = {1: 1, 2: 3, 3: 7, 4: 14, 5: 30}
LEARNED = 6


@dataclass(frozen=True)
class TacticSettings:
    per_day: int = 10
    min_new: int = 3            # so viele neue mindestens, auch wenn viel faellig ist
    engine_seconds: float = 0.4
    min_cp_before: int = -300   # aussichtslose Stellungen sind keine gute Aufgabe
    min_drop: float = 8.0       # Loesung muss so viel besser sein als der Partiezug
    min_gap: float = 10.0       # ... und so viel besser als der zweitbeste Zug
    only_move_gap: float = 20.0 # Folgezuege nur, wenn sie wirklich erzwungen sind
    max_moves: int = 3          # eigene Zuege in einer Loesung (Matt: bis 5)
    alt_tolerance: float = 4.0
    max_per_game: int = 2       # nicht fuenf Aufgaben aus derselben Partie am Tag


def settings_from(app_settings) -> TacticSettings:
    return TacticSettings(
        per_day=app_settings.tactics_per_day,
        engine_seconds=app_settings.tactics_engine_seconds,
        min_cp_before=app_settings.tactics_min_cp,
    )


def engine_available(path: Optional[str]) -> bool:
    if not path:
        return False
    return os.path.isfile(path) or shutil.which(path) is not None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class Engine:
    """Ein eigener Stockfish-Prozess fuer die Aufgaben, per Lock geteilt.

    Getrennt vom Analyse-Prozess: beim Loesen soll ein Zug nicht warten, bis
    eine ganze Partie durchgerechnet ist. Ein Thread, wenig Hash - hier geht
    es um einzelne Stellungen.
    """

    def __init__(self, path: Optional[str], seconds: float) -> None:
        self.path = path if engine_available(path) else None
        self.seconds = seconds
        self._engine: Optional[chess.engine.SimpleEngine] = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self.path)

    def _open(self) -> chess.engine.SimpleEngine:
        if self._engine is None:
            if not self.path:
                raise RuntimeError("no engine")
            self._engine = chess.engine.SimpleEngine.popen_uci(self.path, **engine_popen_args())
            try:
                self._engine.configure({"Threads": 1, "Hash": 64})
            except chess.engine.EngineError:
                pass
        return self._engine

    def analyse(self, board: chess.Board, multipv: int = 1, seconds: Optional[float] = None) -> list[dict]:
        limit = chess.engine.Limit(time=seconds or self.seconds)
        with self._lock:
            for attempt in (1, 2):
                try:
                    result = self._open().analyse(board, limit, multipv=multipv)
                    return result if isinstance(result, list) else [result]
                except (chess.engine.EngineTerminatedError, chess.engine.EngineError, BrokenPipeError):
                    self._close_unlocked()
                    if attempt == 2:
                        raise
        return []

    def warm(self) -> None:
        """Prozess starten und das Netz laden, bevor der erste Zug kommt -
        sonst wartet genau der erste Fehlversuch nach einem Neustart spuerbar."""
        if self.available:
            try:
                self.analyse(chess.Board(), seconds=0.05)
            except Exception:  # noqa: BLE001 - dann eben beim ersten Zug
                log.warning("Stockfish fuer Aufgaben liess sich nicht vorwaermen", exc_info=True)

    def _close_unlocked(self) -> None:
        if self._engine is not None:
            try:
                self._engine.quit()
            except Exception:  # noqa: BLE001 - ein toter Prozess darf beim Aufraeumen nicht stoeren
                pass
            self._engine = None

    def close(self) -> None:
        with self._lock:
            self._close_unlocked()


def win_for(score: chess.engine.PovScore, color: chess.Color) -> float:
    """Gewinnchance in Prozent aus Sicht von color - gleiche Formel wie in
    analysis.win_percent, damit die Zahlen vergleichbar sind."""
    s = score.pov(color)
    if s.is_mate():
        mate = s.mate()
        return 100.0 if mate is not None and mate > 0 else 0.0
    cp = max(-1000, min(1000, s.score() or 0))
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def mate_for(score: chess.engine.PovScore, color: chess.Color) -> Optional[int]:
    s = score.pov(color)
    if s.is_mate() and (s.mate() or 0) > 0:
        return s.mate()
    return None


def _win_after(eng: Engine, board: chess.Board, us: chess.Color) -> tuple[float, Optional[int], list]:
    """Bewertung der Stellung board (Gegner am Zug) aus unserer Sicht."""
    if board.is_checkmate():
        return (100.0 if board.turn != us else 0.0), 0, []
    if board.is_game_over():
        return 50.0, None, []
    info = eng.analyse(board)[0]
    return win_for(info["score"], us), mate_for(info["score"], us), info.get("pv", [])


# ---------------------------------------------------------------------------
# Kandidaten
# ---------------------------------------------------------------------------
TACTICAL_ERRORS = {"fork", "hanging_piece", "missed_threat", "allowed_mate", "missed_win", "missed_mate", "bad_trade"}


def priority(category: str, mate_in: Optional[int], missed_motif: Optional[str], error_type: Optional[str],
             played_at: Optional[datetime], today: date) -> float:
    """Reihenfolge fuer neue Aufgaben: Patzer vor Fehlern, erkannte Motive und
    kurze Matts vor Stellungsfehlern, frische Partien leicht bevorzugt."""
    p = {"blunder": 3.0, "mistake": 2.0}.get(category or "", 1.0)
    if mate_in and mate_in > 0:
        p += 2.5 if mate_in <= 3 else 1.0
    if missed_motif:
        p += 2.0
    if error_type in TACTICAL_ERRORS:
        p += 1.0
    if played_at is not None:
        age = (today - played_at.date()).days
        p += max(0.0, 1.5 - age / 60)
    return round(p, 3)


def tactic_key(game_uuid: str, ply: int) -> str:
    return f"{game_uuid}#{ply}"


def find_candidates(conn: Connection, settings: TacticSettings, today: date, limit: int) -> list[dict]:
    """Noch nicht vorbereitete Fehler, beste zuerst. Ohne PGN - das wird erst
    fuer die Ausgewaehlten geholt."""
    known = {row[0] for row in conn.execute(select(TACTICS.c.key))}
    m, g = MOVES.c, GAMES.c
    rows = conn.execute(
        select(m.ply, m.category, m.error_type, m.missed_motif, m.mate_in, m.played_at, m.game_id, g.uuid)
        .select_from(MOVES.join(GAMES, g.id == m.game_id))
        .where(
            (m.category.in_(("mistake", "blunder"))) | (m.mate_in > 0),
            m.best_move_san.is_not(None),
            m.cp_before >= settings.min_cp_before,
        )
        .order_by(m.played_at.desc())
        .limit(5000)
    ).all()
    out = []
    for r in rows:
        key = tactic_key(r.uuid, r.ply)
        if key in known:
            continue
        mate_in = r.mate_in if r.mate_in and r.mate_in > 0 else None
        out.append({
            "key": key, "game_id": r.game_id, "ply": r.ply, "category": r.category,
            "error_type": r.error_type, "missed_motif": r.missed_motif, "mate_in": mate_in,
            "priority": priority(r.category, mate_in, r.missed_motif, r.error_type, r.played_at, today),
        })
    out.sort(key=lambda c: -c["priority"])
    out = out[:limit]
    if out:
        ids = {c["game_id"] for c in out}
        games = {row.id: row for row in conn.execute(select(GAMES).where(g.id.in_(ids)))}
        for c in out:
            row = games.get(c["game_id"])
            c.update(
                game_uuid=row.uuid if row else None, url=row.url if row else "",
                played_at=row.played_at if row else None, platform=row.platform if row else None,
                time_class=row.time_class if row else None, color=row.color if row else "white",
                opponent=row.opponent if row else "", pgn=row.pgn if row else "",
            )
    return out


# ---------------------------------------------------------------------------
# Vorbereiten
# ---------------------------------------------------------------------------
def prepare(eng: Engine, cand: dict, settings: TacticSettings) -> dict:
    """Macht aus einem Fehler eine Aufgabe - oder begruendet, warum nicht.

    Liefert die Spalten fuer chess_tactic. Unbrauchbare werden mit Grund
    gespeichert, damit sie nicht bei jedem Lauf neu gerechnet werden.
    """
    base = {
        "key": cand["key"], "priority": cand.get("priority"), "game_uuid": cand.get("game_uuid"),
        "ply": cand["ply"], "url": (cand.get("url") or "")[:300], "played_at": cand.get("played_at"),
        "platform": cand.get("platform"), "time_class": cand.get("time_class"), "color": cand.get("color"),
        "opponent": (cand.get("opponent") or "")[:60], "category": cand.get("category"),
        "error_type": cand.get("error_type"), "missed_motif": cand.get("missed_motif"),
        "mate_in": cand.get("mate_in"), "box": 0, "attempts": 0, "solved": 0,
        "prepared_at": utc_now(),
    }

    def bad(reason: str) -> dict:
        return {**base, "status": "unsuitable", "reason": reason[:120]}

    try:
        game = chess.pgn.read_game(io.StringIO(cand.get("pgn") or ""))
    except Exception:  # noqa: BLE001
        game = None
    if game is None:
        return bad("pgn_unreadable")
    moves = list(game.mainline_moves())
    idx = cand["ply"] - 1
    if idx < 0 or idx >= len(moves):
        return bad("ply_missing")
    board = game.board()
    for mv in moves[:idx]:
        board.push(mv)
    us = chess.WHITE if cand.get("color") == "white" else chess.BLACK
    if board.turn != us:
        return bad("wrong_side")
    played = moves[idx]

    root = eng.analyse(board, multipv=2)
    if not root or not root[0].get("pv"):
        return bad("no_engine_result")
    best = root[0]["pv"][0]
    if best == played:
        return bad("played_best")
    win_best = win_for(root[0]["score"], us)
    root_mate = mate_for(root[0]["score"], us)
    second = win_for(root[1]["score"], us) if len(root) > 1 and root[1].get("pv") else 0.0
    gap = win_best - second

    after_played = board.copy()
    after_played.push(played)
    win_played, _, punish_pv = _win_after(eng, after_played, us)
    if win_best - win_played < settings.min_drop:
        return bad(f"small_drop ({win_best - win_played:.1f})")
    if root_mate is None and gap < settings.min_gap:
        return bad(f"ambiguous (gap {gap:.1f})")

    line = [best]
    b = board.copy()
    b.push(best)
    limit = min(5, max(settings.max_moves, root_mate or 0)) if root_mate else settings.max_moves
    while len(line) // 2 + 1 < limit and not b.is_game_over():
        rep = eng.analyse(b)
        if not rep or not rep[0].get("pv"):
            break
        reply = rep[0]["pv"][0]
        b2 = b.copy()
        b2.push(reply)
        if b2.is_game_over():
            break
        infos = eng.analyse(b2, multipv=2)
        if not infos or not infos[0].get("pv"):
            break
        w1 = win_for(infos[0]["score"], us)
        w2 = win_for(infos[1]["score"], us) if len(infos) > 1 and infos[1].get("pv") else 0.0
        forcing = mate_for(infos[0]["score"], us) is not None or (w1 - w2) >= settings.only_move_gap
        if not forcing:
            break
        nxt = infos[0]["pv"][0]
        line += [reply, nxt]
        b = b2
        b.push(nxt)

    punish_san = ""
    if punish_pv:
        try:
            punish_san = after_played.variation_san(punish_pv[:2])
        except (ValueError, AssertionError):
            punish_san = ""
    return {
        **base,
        "status": "ready",
        "reason": None,
        "fen": board.fen(),
        "last_move": moves[idx - 1].uci() if idx >= 1 else None,
        "played_uci": played.uci(),
        "played_san": board.san(played),
        "solution": " ".join(m.uci() for m in line),
        "solution_san": board.variation_san(line),
        "punish_san": punish_san[:40],
        "win_best": round(win_best, 1),
        "win_played": round(win_played, 1),
        "gap": round(gap, 1),
        "mate_in": root_mate or cand.get("mate_in"),
    }


def upsert(conn: Connection, table, values: dict, keys: list[str]) -> None:
    insert = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
    values = {**values, "updated_at": utc_now()}
    stmt = insert(table).values(**values)
    changes = {k: stmt.excluded[k] for k in values if k not in keys}
    conn.execute(stmt.on_conflict_do_update(index_elements=keys, set_=changes))


def ensure_pool(engine: DbEngine, eng: Engine, settings: TacticSettings, today: date,
                target_new: int, max_tries: int) -> dict:
    """Bereitet Aufgaben vor, bis target_new neue bereitliegen oder max_tries
    Kandidaten durchgerechnet sind. Jede Aufgabe wird sofort gespeichert - ein
    Abbruch verliert nichts. exhausted: es gibt keine weiteren Kandidaten."""
    result = {"prepared": 0, "ready": 0, "unsuitable": 0, "exhausted": False}
    if not eng.available:
        result["exhausted"] = True
        return result
    with engine.connect() as conn:
        have = conn.execute(
            select(func.count()).select_from(TACTICS)
            .where(TACTICS.c.status == "ready", TACTICS.c.box == 0)
        ).scalar_one()
    need = target_new - have
    if need <= 0:
        return result
    with engine.connect() as conn:
        # Einer mehr als noetig: so ist klar, ob danach noch etwas kommt.
        cands = find_candidates(conn, settings, today, max_tries + 1)
    processed = 0
    for cand in cands[:max_tries]:
        if result["ready"] >= need:
            break
        try:
            row = prepare(eng, cand, settings)
        except Exception as exc:  # noqa: BLE001 - eine kaputte Stellung stoppt den Lauf nicht
            log.warning("Aufgabe %s nicht vorbereitet: %s", cand["key"], exc)
            row = {"key": cand["key"], "status": "unsuitable", "reason": f"error: {exc}"[:120],
                   "ply": cand["ply"], "box": 0, "attempts": 0, "solved": 0, "prepared_at": utc_now()}
        with engine.begin() as conn:
            upsert(conn, TACTICS, row, ["key"])
        processed += 1
        result["ready" if row["status"] == "ready" else "unsuitable"] += 1
    result["prepared"] = processed
    result["exhausted"] = processed >= len(cands)
    if processed:
        log.info("Taktik: %s neue Aufgaben, %s unbrauchbar", result["ready"], result["unsuitable"])
    return result


# ---------------------------------------------------------------------------
# Tagesauswahl und Wiederholung
# ---------------------------------------------------------------------------
def schedule(box: int, solved: bool, today: date) -> tuple[int, Optional[date]]:
    """Neue Stufe und naechster Termin. Eine neue Aufgabe, die auf Anhieb
    sitzt, springt auf Stufe 2 - der Fehler aus der Partie zeigt aber, dass sie
    einmal nicht sass, darum kommt sie trotzdem wieder."""
    if solved:
        nb = 2 if box == 0 else box + 1
        if nb >= LEARNED:
            return LEARNED, None
    else:
        nb = 1
    return nb, today + timedelta(days=INTERVALS[nb])


def day_rows(conn: Connection, day: date) -> list:
    return conn.execute(select(DAYS).where(DAYS.c.day == day).order_by(DAYS.c.pos)).all()


def position_key(fen: Optional[str]) -> str:
    """Stellung ohne Zugzaehler: Figuren, Zugrecht, Rochade, en passant."""
    return " ".join((fen or "").split()[:4])


def build_day(conn: Connection, day: date, settings: TacticSettings) -> list:
    """Stellt die Aufgaben des Tages zusammen und schreibt sie fest. Leere
    Auswahl wird nicht gespeichert - dann wird spaeter neu versucht."""
    rows = day_rows(conn, day)
    if rows:
        return rows
    t = TACTICS.c
    # Gleiche Stellung aus zwei Partien (derselbe Eroeffnungsfehler zweimal)
    # ergibt zwei Aufgaben mit verschiedenem Schluessel - am selben Tag soll sie
    # trotzdem nur einmal kommen. Die zweite wartet bis zu einem anderen Tag.
    seen: set[str] = set()
    due: list[str] = []
    for r in conn.execute(
        select(t.key, t.fen).where(t.status == "ready", t.box.between(1, LEARNED - 1), t.due <= day)
        .order_by(t.due, t.box, t.key)
    ):
        pk = position_key(r.fen)
        if pk and pk in seen:
            continue
        if pk:
            seen.add(pk)
        due.append(r.key)
    # Stellungen, die schon in Wiederholung oder gelernt sind, nicht nochmal als neu.
    in_rotation = {position_key(r.fen) for r in conn.execute(
        select(t.fen).where(t.status == "ready", t.box >= 1)
    )} - {""}
    fresh = conn.execute(
        select(t.key, t.game_uuid, t.fen).where(t.status == "ready", t.box == 0)
        .order_by(t.priority.desc(), t.played_at.desc(), t.key)
    ).all()
    per_game: dict[str, int] = {}
    new_keys: list[str] = []
    want_new = max(settings.min_new, settings.per_day - len(due))
    for r in fresh:
        if len(new_keys) >= want_new:
            break
        g = r.game_uuid or r.key
        if per_game.get(g, 0) >= settings.max_per_game:
            continue
        pk = position_key(r.fen)
        if pk and (pk in seen or pk in in_rotation):
            continue
        if pk:
            seen.add(pk)
        per_game[g] = per_game.get(g, 0) + 1
        new_keys.append(r.key)
    reviews = due[: max(0, settings.per_day - len(new_keys))]
    # Wiederholungen und neue abwechselnd, damit nicht erst zehn Mal dasselbe Gefuehl kommt.
    keys: list[str] = []
    for i in range(max(len(reviews), len(new_keys))):
        if i < len(reviews):
            keys.append(reviews[i])
        if i < len(new_keys):
            keys.append(new_keys[i])
    keys = keys[: settings.per_day]
    for pos, key in enumerate(keys):
        conn.execute(DAYS.insert().values(day=day, pos=pos, key=key, result=None, updated_at=utc_now()))
    return day_rows(conn, day)


def record(conn: Connection, day: date, key: str, solved: bool) -> bool:
    """Erstes Ergebnis einer Aufgabe am Tag zaehlt, spaetere Versuche nicht.
    Gibt zurueck, ob gezaehlt wurde."""
    res = conn.execute(
        update(DAYS)
        .where(DAYS.c.day == day, DAYS.c.key == key, DAYS.c.result.is_(None))
        .values(result="solved" if solved else "failed", updated_at=utc_now())
    )
    if res.rowcount != 1:
        return False
    row = conn.execute(select(TACTICS).where(TACTICS.c.key == key)).first()
    if row is None:
        return True
    box, due = schedule(row.box or 0, solved, day)
    conn.execute(
        update(TACTICS).where(TACTICS.c.key == key).values(
            box=box, due=due, attempts=(row.attempts or 0) + 1, solved=(row.solved or 0) + (1 if solved else 0),
            last_seen=day, last_result="solved" if solved else "failed", updated_at=utc_now(),
        )
    )
    return True


def stats(conn: Connection, today: date) -> dict:
    t = TACTICS.c
    counts: dict[str, Any] = {"new": 0, "learning": 0, "learned": 0, "due": 0, "unsuitable": 0}
    for r in conn.execute(select(t.status, t.box, t.due)):
        if r.status != "ready":
            counts["unsuitable"] += 1
        elif (r.box or 0) == 0:
            counts["new"] += 1
        elif r.box >= LEARNED:
            counts["learned"] += 1
        else:
            counts["learning"] += 1
            if r.due and r.due <= today:
                counts["due"] += 1
    d = DAYS.c
    since = today - timedelta(days=29)
    results = [r.result for r in conn.execute(select(d.result).where(d.day >= since, d.result.is_not(None)))]
    counts["last30_done"] = len(results)
    counts["last30_solved"] = sum(1 for r in results if r == "solved")
    # Serie: Tage in Folge mit komplett erledigter Auswahl
    by_day: dict[date, list] = {}
    for r in conn.execute(select(d.day, d.result).where(d.day >= today - timedelta(days=400))):
        by_day.setdefault(r.day, []).append(r.result)
    day = today if by_day.get(today) and all(by_day[today]) else today - timedelta(days=1)
    streak = 0
    while by_day.get(day) and all(by_day[day]):
        streak += 1
        day -= timedelta(days=1)
    counts["streak"] = streak
    return counts


# ---------------------------------------------------------------------------
# Loesen
# ---------------------------------------------------------------------------
class MoveError(ValueError):
    """code: expected_own_move | sequence_mismatch | unknown_move | illegal_move"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def legal_moves(board: chess.Board) -> list[str]:
    return [m.uci() for m in board.legal_moves]


def game_link(row) -> str:
    url = row.url or ""
    if url and row.platform == "lichess" and row.ply:
        # Lichess zeigt mit #n die Stellung nach Halbzug n - wir wollen die davor.
        return f"{url}#{max(0, row.ply - 1)}"
    return url


def puzzle_payload(row) -> dict:
    board = chess.Board(row.fen)
    return {
        "key": row.key,
        "fen": row.fen,
        "orientation": row.color or ("white" if board.turn else "black"),
        "to_move": "white" if board.turn else "black",
        "move_number": board.fullmove_number,
        "last_move": row.last_move,
        "legal": legal_moves(board),
        "review": (row.box or 0) > 0,
        "box": row.box or 0,
        "hint": {"missed_motif": row.missed_motif, "error_type": row.error_type,
                 "mate_in": row.mate_in if row.mate_in and 0 < row.mate_in <= 5 else None},
        "game": {"opponent": row.opponent, "played_at": row.played_at.date().isoformat() if row.played_at else None,
                 "platform": row.platform, "time_class": row.time_class, "url": game_link(row)},
        "moves_to_find": (len((row.solution or "").split()) + 1) // 2,
    }


def explanation(row) -> dict:
    return {
        "played_san": row.played_san,
        "played_uci": row.played_uci,
        "punish_san": row.punish_san,
        "solution": (row.solution or "").split(),
        "solution_san": row.solution_san,
        "win_best": row.win_best,
        "win_played": row.win_played,
    }


def check_move(eng: Engine, row, moves: list[str], settings: TacticSettings) -> dict:
    """moves: alle Zuege seit der Ausgangsstellung, der letzte ist der neue
    eigene Zug. Zustandslos - der Client schickt die ganze Folge."""
    sol = (row.solution or "").split()
    if not moves or len(moves) % 2 == 0:
        raise MoveError("expected_own_move")
    *prefix, last = moves
    if prefix != sol[: len(prefix)]:
        raise MoveError("sequence_mismatch")
    board = chess.Board(row.fen)
    us = board.turn
    for u in prefix:
        board.push_uci(u)
    try:
        move = chess.Move.from_uci(last)
    except ValueError as exc:
        raise MoveError("unknown_move") from exc
    if move not in board.legal_moves:
        raise MoveError("illegal_move")
    idx = len(prefix)
    san = board.san(move)
    expected = chess.Move.from_uci(sol[idx]) if idx < len(sol) else None
    main_san = board.san(expected) if expected else None
    after = board.copy()
    after.push(move)

    if move == expected or after.is_checkmate():
        if after.is_checkmate() or idx + 1 >= len(sol):
            return {"result": "solved", "san": san, "fen": after.fen()}
        reply = chess.Move.from_uci(sol[idx + 1])
        reply_san = after.san(reply)
        after.push(reply)
        return {"result": "correct", "san": san, "reply": reply.uci(), "reply_san": reply_san,
                "fen": after.fen(), "legal": legal_moves(after)}

    if eng.available:
        best_info = eng.analyse(board)[0]
        win_b = win_for(best_info["score"], us)
        mate_b = mate_for(best_info["score"], us)
        win_u, mate_u, pv = _win_after(eng, after, us)
        if mate_b is not None:
            ok = mate_u is not None and mate_u <= mate_b
        else:
            ok = win_u >= win_b - settings.alt_tolerance
        if ok:
            return {"result": "solved", "alternative": True, "san": san, "main_san": main_san, "fen": after.fen()}
        punish = pv[0] if pv else None
        return {"result": "wrong", "san": san, "fen": after.fen(),
                "punish": punish.uci() if punish else None,
                "punish_san": after.san(punish) if punish else None}
    return {"result": "wrong", "san": san, "fen": after.fen(), "punish": None, "punish_san": None}


# ---------------------------------------------------------------------------
# Uebernahme aus dem frueheren, eigenstaendigen Trainer
# ---------------------------------------------------------------------------
LEGACY_TACTICS = "trainer_tactic"
LEGACY_DAYS = "trainer_tactic_day"


def import_legacy(engine: DbEngine) -> int:
    """Uebernimmt Aufgaben und Lernstaende aus den Tabellen des frueheren
    Trainers (gleiche Spalten, gleicher Schluessel), falls sie in derselben
    Datenbank liegen - einmalig, nur solange chess_tactic noch leer ist.
    Wer den Trainer nie hatte, merkt davon nichts."""
    insp = inspect(engine)
    if not insp.has_table(LEGACY_TACTICS):
        return 0
    with engine.connect() as conn:
        if conn.execute(select(func.count()).select_from(TACTICS)).scalar_one():
            return 0
    legacy_cols = {c["name"] for c in insp.get_columns(LEGACY_TACTICS)}
    cols = [c.name for c in TACTICS.columns if c.name in legacy_cols]
    col_list = ", ".join(cols)
    with engine.begin() as conn:
        conn.execute(text(f"INSERT INTO {TACTICS.name} ({col_list}) SELECT {col_list} FROM {LEGACY_TACTICS}"))
        n = conn.execute(select(func.count()).select_from(TACTICS)).scalar_one()
        if insp.has_table(LEGACY_DAYS):
            conn.execute(text(
                f"INSERT INTO {DAYS.name} (day, pos, key, result, updated_at) "
                f"SELECT day, pos, key, result, updated_at FROM {LEGACY_DAYS}"
            ))
    log.info("Taktik: %d Aufgaben samt Lernstand aus %s uebernommen", n, LEGACY_TACTICS)
    return n


# ---------------------------------------------------------------------------
# Dienst: Engine, Vorrat, Zustand - einmal pro Prozess
# ---------------------------------------------------------------------------
class Trainer:
    """Haelt die Aufgaben-Engine und den Stand der Vorbereitung.

    Vorbereitet wird an zwei Stellen: nach jedem Durchlauf (neue Partien,
    neue Fehler) und bei Bedarf, wenn die Seite Aufgaben will und der Vorrat
    nicht reicht - beim allerersten Mal. Hoechstens ein Lauf gleichzeitig.
    """

    def __init__(self, db_engine: DbEngine, app_settings) -> None:
        self.db = db_engine
        self.settings = settings_from(app_settings)
        self.enabled = bool(app_settings.tactics_enabled)
        self.engine = Engine(app_settings.engine_path, self.settings.engine_seconds)
        self._fill_lock = threading.Lock()
        self.status: dict[str, Any] = {"preparing": False, "exhausted": False, "last_error": None}

    @property
    def target_new(self) -> int:
        return self.settings.per_day * 3

    def fill(self, target_new: Optional[int] = None, max_tries: Optional[int] = None,
             today: Optional[date] = None) -> Optional[dict]:
        if not self.enabled or not self.engine.available:
            return None
        if not self._fill_lock.acquire(blocking=False):
            return None
        self.status.update(preparing=True, last_error=None)
        try:
            result = ensure_pool(
                self.db, self.engine, self.settings, today or date.today(),
                target_new or self.target_new, max_tries or self.settings.per_day * 12,
            )
            self.status["exhausted"] = result["exhausted"]
            return result
        except Exception as exc:  # noqa: BLE001
            log.exception("Taktik-Vorrat fehlgeschlagen")
            self.status.update(last_error=str(exc)[:300], exhausted=True)
            return None
        finally:
            self.status["preparing"] = False
            self._fill_lock.release()

    def fill_in_background(self, today: Optional[date] = None) -> None:
        """Startet eine Vorbereitung, falls keine laeuft. Das Flag wird sofort
        gesetzt, damit die Seite schon in derselben Antwort "wird vorbereitet"
        zeigt und nachfragt."""
        if not self.enabled or not self.engine.available or self._fill_lock.locked():
            return
        self.status["preparing"] = True
        threading.Thread(
            target=self.fill, kwargs={"max_tries": self.settings.per_day * 6, "today": today},
            name="knightmare-tactics", daemon=True,
        ).start()

    def pool_ready(self, day: date) -> bool:
        with self.db.connect() as conn:
            st = stats(conn, day)
        return st["new"] + st["due"] >= self.settings.per_day


_trainer: Optional[Trainer] = None
_trainer_lock = threading.Lock()


def get_trainer() -> Trainer:
    global _trainer
    with _trainer_lock:
        if _trainer is None:
            from .config import load_settings
            from .db import engine as db_engine

            _trainer = Trainer(db_engine, load_settings())
        return _trainer
