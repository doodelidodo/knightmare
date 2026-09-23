"""Stockfish-Analyse einer einzelnen Partie samt Einordnung der Fehler.

Verfahren: Jede Stellung der Hauptvariante wird genau einmal bewertet. Der
Verlust eines eigenen Zuges ergibt sich dann aus der Differenz zwischen der
Bewertung vor dem Zug und der Bewertung der Folgestellung - beides aus der
eigenen Perspektive. Das braucht nur eine Analyse pro Stellung statt zwei
(einmal "bester Zug", einmal "gespielter Zug").

Die Engine liefert zu jeder Stellung nebenbei ihren besten Zug mit. Fuer die
Stellung NACH unserem Zug ist das die Widerlegung - also genau der Zug, der
zeigt, was an unserem Zug falsch war. Daraus wird die Fehlerart abgeleitet.

Bewertungen werden auf +/- 1000 Centipawn begrenzt: wer schon klar gewonnen
steht, "verliert" sonst rechnerisch hunderte Centipawn, ohne einen Fehler
gemacht zu haben.
"""

from __future__ import annotations

import io
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

import chess
import chess.engine
import chess.pgn

log = logging.getLogger(__name__)

EVAL_CLAMP = 1000
MATE_SCORE = 100000

# Koeffizient der Lichess-Gewinnwahrscheinlichkeitskurve.
WIN_CURVE_K = 0.00368208

PHASE_OPENING = "opening"
PHASE_MIDDLEGAME = "middlegame"
PHASE_ENDGAME = "endgame"
PHASES = (PHASE_OPENING, PHASE_MIDDLEGAME, PHASE_ENDGAME)

CATEGORY_OK = "ok"
CATEGORY_INACCURACY = "inaccuracy"
CATEGORY_MISTAKE = "mistake"
CATEGORY_BLUNDER = "blunder"

# --- Fehlerarten ----------------------------------------------------------
ERROR_ALLOWED_MATE = "allowed_mate"
ERROR_MISSED_MATE = "missed_mate"
ERROR_FORK = "fork"
ERROR_BAD_TRADE = "bad_trade"
ERROR_HANGING_PIECE = "hanging_piece"
ERROR_MISSED_THREAT = "missed_threat"
ERROR_MISSED_WIN = "missed_win"
ERROR_POSITIONAL = "positional"

ERROR_TYPES = (
    ERROR_ALLOWED_MATE,
    ERROR_MISSED_MATE,
    ERROR_FORK,
    ERROR_HANGING_PIECE,
    ERROR_MISSED_THREAT,
    ERROR_BAD_TRADE,
    ERROR_MISSED_WIN,
    ERROR_POSITIONAL,
)

# Kein Analyseergebnis, sondern ein Datenzustand: diese Zuege stammen aus einer
# Analyse von vor der Fehlerart-Erkennung. Bewusst NICHT in ERROR_TYPES - es ist
# keine Fehlerart, und sie als "Stellungsfehler" mitzuzaehlen wuerde die
# Verteilung verfaelschen und zu falschem Training fuehren.
ERROR_UNCLASSIFIED = "unclassified"

# Englisch als Vorgabe der API - die Oberflaeche uebersetzt ohnehin selbst,
# und fuer ein oeffentliches Projekt ist Englisch die groessere Zielgruppe.
ERROR_LABELS = {
    ERROR_ALLOWED_MATE: "Allowed mate",
    ERROR_MISSED_MATE: "Missed a forced mate",
    ERROR_FORK: "Walked into a fork",
    ERROR_HANGING_PIECE: "Hung a piece",
    ERROR_MISSED_THREAT: "Missed a threat",
    ERROR_BAD_TRADE: "Bad trade",
    ERROR_MISSED_WIN: "Missed a win",
    ERROR_POSITIONAL: "Positional slip",
    ERROR_UNCLASSIFIED: "Not classified yet",
}

# Materialwerte fuer die Fehlereinordnung (nicht fuer die Bewertung - die
# kommt von Stockfish).
MATERIAL_VALUES = {
    chess.PAWN: 1,
    chess.KNIGHT: 3,
    chess.BISHOP: 3,
    chess.ROOK: 5,
    chess.QUEEN: 9,
}

# Materialgewicht ohne Bauern und Koenig - Grundlage der Endspiel-Erkennung.
PHASE_PIECE_VALUES = {
    chess.QUEEN: 4,
    chess.ROOK: 2,
    chess.BISHOP: 1,
    chess.KNIGHT: 1,
}
ENDGAME_MATERIAL_THRESHOLD = 6

# Ab hier gilt ein Vorteil als "klarer Gewinn", der nicht verspielt werden darf.
MISSED_WIN_BEFORE_CP = 200
MISSED_WIN_AFTER_CP = 50

# Stand der Auswertung, mit dem eine Partie durchgerechnet wurde. Wird
# hochgezaehlt, sobald die Analyse etwas Neues erfasst, das alte Ergebnisse
# nicht haben. Ohne diesen Stempel liesse sich "hier war nichts zu holen"
# nicht von "das wurde nie geprueft" unterscheiden - und die Oberflaeche
# wuerde eine leere Liste zeigen, als waere sie ein Befund.
#   1 = Fehlerarten
#   2 = zusaetzlich verpasste Taktik
#   3 = "Matt verpasst" als eigene Fehlerart
ANALYSIS_VERSION = 3

# Ab welchem Stand eine Partie verpasste Motive traegt. Bewusst getrennt von
# ANALYSIS_VERSION: nicht jede spaetere Erweiterung entwertet diesen einen
# Abschnitt, und ein Hinweis "muss nachgerechnet werden", der nicht stimmt,
# ist schlimmer als keiner.
MISSED_SINCE_VERSION = 2

_TIME_CONTROL_RE = re.compile(r"^(\d+)(?:\+(\d+))?$")


class AnalysisError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------
def clamp_eval(centipawns: Optional[int]) -> int:
    if centipawns is None:
        return 0
    return max(-EVAL_CLAMP, min(EVAL_CLAMP, int(centipawns)))


def win_percent(centipawns: int) -> float:
    """Gewinnwahrscheinlichkeit in Prozent aus Sicht der bewerteten Seite."""
    value = clamp_eval(centipawns)
    return 50.0 + 50.0 * (2.0 / (1.0 + math.exp(-WIN_CURVE_K * value)) - 1.0)


def non_pawn_material(board: chess.Board) -> int:
    total = 0
    for piece_type, value in PHASE_PIECE_VALUES.items():
        white = len(board.pieces(piece_type, chess.WHITE))
        black = len(board.pieces(piece_type, chess.BLACK))
        total += value * (white + black)
    return total


def phase_for(board: chess.Board, ply: int, opening_plies: int) -> str:
    """Endspiel schlaegt Eroeffnung: ein frueher Damentausch ist kein Mittelspiel."""
    if non_pawn_material(board) <= ENDGAME_MATERIAL_THRESHOLD:
        return PHASE_ENDGAME
    if ply < opening_plies:
        return PHASE_OPENING
    return PHASE_MIDDLEGAME


def material_balance(board: chess.Board, color: chess.Color) -> int:
    """Materialvorsprung aus Sicht von 'color', in Bauerneinheiten."""
    total = 0
    for piece_type, value in MATERIAL_VALUES.items():
        mine = len(board.pieces(piece_type, color))
        theirs = len(board.pieces(piece_type, not color))
        total += value * (mine - theirs)
    return total


def parse_increment(time_control: str) -> float:
    """'600+5' -> 5.0, '300' -> 0.0, '1/86400' (Daily) -> 0.0."""
    value = (time_control or "").strip()
    if not value or "/" in value:
        return 0.0
    match = _TIME_CONTROL_RE.match(value)
    if not match:
        return 0.0
    return float(match.group(2) or 0)


# --------------------------------------------------------------------------
# Fehlerarten
# --------------------------------------------------------------------------
def is_fork(board_after_reply: chess.Board, reply: chess.Move, victim_color: chess.Color) -> bool:
    """Greift die gerade gezogene Figur mindestens zwei lohnende Ziele an?

    Lohnend heisst: der eigene Koenig (also Schach), eine wertvollere Figur als
    der Angreifer selbst, oder eine ungedeckte Figur ab Leichtfigurwert. Damit
    zaehlt der Angriff auf zwei gedeckte Bauern korrekt nicht als Gabel.
    """
    attacker = board_after_reply.piece_at(reply.to_square)
    if attacker is None or attacker.color == victim_color:
        return False
    attacker_value = MATERIAL_VALUES.get(attacker.piece_type, 0)

    targets = 0
    for square in board_after_reply.attacks(reply.to_square):
        target = board_after_reply.piece_at(square)
        if target is None or target.color != victim_color:
            continue
        if target.piece_type == chess.KING:
            targets += 1
            continue
        value = MATERIAL_VALUES.get(target.piece_type, 0)
        if value > attacker_value:
            targets += 1
        elif value >= 3 and not board_after_reply.is_attacked_by(victim_color, square):
            targets += 1
    return targets >= 2


# --------------------------------------------------------------------------
# Verpasste Taktik
#
# Zweite Achse der Auswertung. Die Fehlerarten oben sagen, worin man
# hineingelaufen ist; hier geht es um das Gegenteil: was auf dem Brett stand
# und nicht gespielt wurde. Grundlage ist der beste Zug, den Stockfish in der
# Stellung VOR unserem Zug gesehen hat - der liegt ohnehin vor, die Erkennung
# kostet also keine zusaetzliche Rechenzeit.
#
# Alles hier ist reine Geometrie auf dem Brett. Was sich damit nicht sauber
# entscheiden laesst - Zwischenzug, Ablenkung ueber mehrere Zuege, Zugzwang -
# ist bewusst nicht dabei. Ein falsches Etikett waere schlimmer als keins:
# man wuerde das Falsche trainieren.
# --------------------------------------------------------------------------
MISSED_MATE = "mate"
MISSED_FORK = "fork"
MISSED_DISCOVERED = "discovered"
MISSED_SKEWER = "skewer"
MISSED_PIN = "pin"
MISSED_MATERIAL = "material"

MISSED_TYPES = (
    MISSED_MATE,
    MISSED_FORK,
    MISSED_DISCOVERED,
    MISSED_SKEWER,
    MISSED_PIN,
    MISSED_MATERIAL,
)

MISSED_LABELS = {
    MISSED_MATE: "Missed a mate",
    MISSED_FORK: "Missed a fork",
    MISSED_DISCOVERED: "Missed a discovered attack",
    MISSED_SKEWER: "Missed a skewer",
    MISSED_PIN: "Missed a pin",
    MISSED_MATERIAL: "Missed free material",
}

SLIDERS = (chess.BISHOP, chess.ROOK, chess.QUEEN)

# Ab welchem Wert ein Ziel die Erkennung wert ist. Ein Angriff auf einen Bauern
# ist kein Motiv, das man trainieren muesste.
WORTHWHILE_TARGET = 3

# Was hinter einer Fesselung stehen muss, damit sie eine ist: Dame oder
# Koenig. Ein Turm dahinter laesst der vorderen Figur meist noch Luft.
PIN_ANCHOR_VALUE = 9


def _value_at(board: chess.Board, square: int) -> int:
    piece = board.piece_at(square)
    if piece is None:
        return 0
    if piece.piece_type == chess.KING:
        # Der Koenig hat keinen Tauschwert, steht aber ueber allem.
        return 100
    return MATERIAL_VALUES.get(piece.piece_type, 0)


def find_line_motif(
    board_after: chess.Board, move: chess.Move, victim_color: chess.Color
) -> Optional[str]:
    """Fesselung oder Spiess entlang eines Strahls der gerade gezogenen Figur.

    Beide Motive sehen geometrisch gleich aus: zwei gegnerische Figuren
    hintereinander auf einer Linie der angreifenden Figur. Welches von beiden
    es ist, entscheidet die Reihenfolge der Werte.

      vorne weniger wert als hinten -> Fesselung (die vordere kann nicht weg)
      vorne mehr wert als hinten    -> Spiess (die vordere muss weg, die
                                       hintere faellt)

    Der Koenig zaehlt dabei als das Wertvollste ueberhaupt, eine absolute
    Fesselung ist also der Normalfall der ersten Zeile.
    """
    attacker = board_after.piece_at(move.to_square)
    if attacker is None or attacker.piece_type not in SLIDERS:
        return None
    if attacker.color == victim_color:
        return None
    attacker_value = MATERIAL_VALUES.get(attacker.piece_type, 0)

    for target in board_after.attacks(move.to_square):
        front = board_after.piece_at(target)
        if front is None or front.color != victim_color:
            continue

        # Hinter dem ersten Ziel in derselben Richtung weitersuchen.
        behind = _first_piece_behind(board_after, move.to_square, target)
        if behind is None:
            continue
        back = board_after.piece_at(behind)
        if back is None or back.color != victim_color:
            continue

        front_value = _value_at(board_after, target)
        back_value = _value_at(board_after, behind)

        # Fesselung: hinten steht das Wertvollere, vorne kommt nicht weg.
        #
        # Die beiden Schranken sind gemessen, nicht geraten: mit "hinten
        # mindestens Leichtfigur" trug fast jeder neunte zufaellige Zug das
        # Etikett - diese eine Kategorie haette die Liste geflutet. Eine
        # Fesselung ist dann ein Thema, wenn hinten Dame oder Koenig steht
        # (nur dann ist vorn wirklich nichts zu machen) und vorn mehr als ein
        # Bauer. Damit liegt sie bei 0,5 %, in derselben Groessenordnung wie
        # Gabel und Spiess.
        if (
            back_value > front_value
            and back_value >= PIN_ANCHOR_VALUE
            and front_value >= WORTHWHILE_TARGET
        ):
            return MISSED_PIN
        # Spiess: vorne das Wertvollere, und es lohnt sich nur, wenn die
        # angreifende Figur selbst weniger wert ist.
        if front_value > back_value and front_value > attacker_value:
            return MISSED_SKEWER
    return None


def _first_piece_behind(
    board: chess.Board, origin: int, target: int
) -> Optional[int]:
    """Erstes besetztes Feld hinter 'target', vom 'origin' aus gesehen.

    Schrittweise in dieselbe Richtung weiter. Dass wir dabei auf der Linie
    bleiben, ergibt sich aus dem Schritt selbst - ein Strahlenabgleich ist
    dafuer nicht noetig. Nur Ziele auf einer geraden oder diagonalen Linie
    kommen hier an, weil die angreifende Figur eine Langschrittfigur ist.
    """
    file_step = chess.square_file(target) - chess.square_file(origin)
    rank_step = chess.square_rank(target) - chess.square_rank(origin)

    # Nicht ausgerichtet: weder gerade noch exakt diagonal. Kann nicht
    # vorkommen, solange der Angreifer ein Laeufer, Turm oder eine Dame ist -
    # aber die Annahme steht besser im Code als im Kopf.
    if file_step and rank_step and abs(file_step) != abs(rank_step):
        return None

    file_step = (file_step > 0) - (file_step < 0)
    rank_step = (rank_step > 0) - (rank_step < 0)
    if not file_step and not rank_step:
        return None

    file_index = chess.square_file(target) + file_step
    rank_index = chess.square_rank(target) + rank_step
    while 0 <= file_index <= 7 and 0 <= rank_index <= 7:
        square = chess.square(file_index, rank_index)
        if board.piece_at(square) is not None:
            return square
        file_index += file_step
        rank_index += rank_step
    return None


def is_discovered_attack(
    board_before: chess.Board, board_after: chess.Board, move: chess.Move
) -> bool:
    """Hat der Zug eine Linie geraeumt, hinter der eine eigene Figur stand?

    Gesucht wird eine eigene Langschrittfigur, die nach dem Zug etwas
    Lohnendes angreift, das sie vorher nicht angegriffen hat - und zwar durch
    genau das Feld, das der Zug verlassen hat.
    """
    us = board_before.turn
    for square in board_after.pieces(chess.BISHOP, us) | board_after.pieces(
        chess.ROOK, us
    ) | board_after.pieces(chess.QUEEN, us):
        if square == move.to_square:
            continue  # die Figur, die gerade gezogen hat, deckt sich selbst auf nicht
        gained = board_after.attacks(square) - board_before.attacks(square)
        for target in gained:
            piece = board_after.piece_at(target)
            if piece is None or piece.color == us:
                continue
            if _value_at(board_after, target) < WORTHWHILE_TARGET:
                continue
            # chess.between() liefert je nach Fassung von python-chess ein
            # SquareSet oder ein rohes Bitboard (eine Zahl). SquareSet()
            # nimmt beides entgegen - so bleibt der Test gueltig.
            if move.from_square in chess.SquareSet(chess.between(square, target)):
                return True
    return False


def wins_loose_material(board_before: chess.Board, move: chess.Move) -> bool:
    """Schlaegt der Zug etwas, das ungedeckt oder mehr wert als der Schlaegende ist?"""
    if not board_before.is_capture(move):
        return False
    if board_before.is_en_passant(move):
        return False  # ein Bauer gegen einen Bauern ist kein verpasstes Motiv
    victim_value = _value_at(board_before, move.to_square)
    attacker_value = _value_at(board_before, move.from_square)
    if victim_value == 0:
        return False
    defended = board_before.is_attacked_by(
        not board_before.turn, move.to_square
    )
    return (not defended and victim_value >= WORTHWHILE_TARGET) or (
        victim_value > attacker_value
    )


def classify_missed(
    board_before: chess.Board, best: Optional[chess.Move], mate_for_us: bool
) -> Optional[str]:
    """Welches Motiv steckt in dem besten Zug, den wir nicht gespielt haben?

    Reihenfolge nach Durchschlagskraft: Matt vor Gabel vor Abzug vor Spiess
    vor Fesselung vor blossem Materialgewinn. Gibt None zurueck, wenn der
    beste Zug kein benennbares taktisches Motiv traegt - das ist der
    Normalfall und ausdruecklich kein Mangel.
    """
    if best is None:
        return None
    if best not in board_before.legal_moves:
        return None

    board_after = board_before.copy(stack=False)
    board_after.push(best)
    them = not board_before.turn

    if board_after.is_checkmate() or mate_for_us:
        return MISSED_MATE
    if is_fork(board_after, best, them):
        return MISSED_FORK
    if is_discovered_attack(board_before, board_after, best):
        return MISSED_DISCOVERED

    line = find_line_motif(board_after, best, them)
    if line is not None:
        return line

    if wins_loose_material(board_before, best):
        return MISSED_MATERIAL
    return None


def classify_error(
    board_before: chess.Board,
    move: chess.Move,
    board_after: chess.Board,
    reply: Optional[chess.Move],
    cp_before: int,
    cp_after: int,
    mate_against_now: bool,
    mate_against_before: bool,
    mate_for_us_before: bool = False,
    mate_for_us_now: bool = False,
) -> str:
    """Ordnet einen fehlerhaften Zug einer Fehlerart zu.

    Heuristik, kein Urteil: Grundlage ist der beste Gegenzug, den Stockfish in
    der Stellung nach unserem Zug sieht. Die Reihenfolge der Pruefungen ist
    bewusst - Matt schlaegt alles, danach Material, zuletzt Stellung.
    """
    us = board_before.turn

    # Matt zugelassen - aber nur, wenn vorher noch keins gegen uns stand.
    if mate_against_now and not mate_against_before:
        return ERROR_ALLOWED_MATE

    # Matt verpasst: vorher sah die Engine ein erzwungenes Matt fuer uns,
    # nachher nicht mehr.
    #
    # Muss vor dem Ausstieg bei fehlendem Gegenzug stehen. Sonst landet
    # ausgerechnet der schlimmste Fall im Resttopf: Matt in eins auf dem
    # Brett, stattdessen patt gesetzt - dann gibt es keinen Gegenzug mehr,
    # und die Einordnung stieg hier bisher mit "Stellungsfehler" aus.
    #
    # Und vor der Materialpruefung, weil "du hattest Matt" der lehrreichere
    # Satz ist als "du hast einen Bauern verloren".
    if mate_for_us_before and not mate_for_us_now:
        return ERROR_MISSED_MATE

    if reply is None:
        return ERROR_POSITIONAL

    board_final = board_after.copy(stack=False)
    try:
        board_final.push(reply)
    except (ValueError, AssertionError):
        return ERROR_POSITIONAL

    material_lost = material_balance(board_before, us) - material_balance(board_final, us)
    was_capture = board_before.is_capture(move)
    reply_is_capture = board_after.is_capture(reply)

    # Gabel zuerst: sie erklaert den Materialverlust besser als "eingestellt".
    if is_fork(board_final, reply, us):
        return ERROR_FORK

    # Eigener Schlagzug, der zurueckgeschlagen wird und Material kostet.
    if was_capture and reply_is_capture and reply.to_square == move.to_square and material_lost >= 1:
        return ERROR_BAD_TRADE

    if reply_is_capture and material_lost >= 1:
        # Wurde die Figur geschlagen, die wir gerade gezogen haben?
        if reply.to_square == move.to_square:
            return ERROR_HANGING_PIECE
        return ERROR_MISSED_THREAT

    if material_lost >= 1:
        # Materialverlust ohne direkten Schlagzug - der Gegenzug droht etwas,
        # das wir nicht mehr parieren koennen.
        return ERROR_MISSED_THREAT

    if cp_before >= MISSED_WIN_BEFORE_CP and cp_after < MISSED_WIN_AFTER_CP:
        return ERROR_MISSED_WIN

    return ERROR_POSITIONAL


# --------------------------------------------------------------------------
# Ergebnisstrukturen
# --------------------------------------------------------------------------
@dataclass
class Evaluation:
    """Bewertung einer Stellung, immer aus Sicht von Weiss."""

    centipawns: int
    mate: Optional[int] = None
    best_move: Optional[chess.Move] = None
    best_san: Optional[str] = None


@dataclass
class MoveReport:
    ply: int
    move_number: int
    san: str
    phase: str
    cp_before: int
    cp_after: int
    cp_loss: int
    win_loss: float
    category: str
    error_type: Optional[str] = None
    missed_motif: Optional[str] = None
    best_move_san: Optional[str] = None
    refutation_san: Optional[str] = None
    clock_seconds: Optional[float] = None
    seconds_spent: Optional[float] = None


@dataclass
class GameReport:
    moves: list[MoveReport] = field(default_factory=list)
    move_count: int = 0
    acpl: Optional[float] = None
    acpl_by_phase: dict[str, Optional[float]] = field(default_factory=dict)
    inaccuracies: int = 0
    mistakes: int = 0
    blunders: int = 0
    first_error_ply: Optional[int] = None


def _mean(values: Iterable[float]) -> Optional[float]:
    items = list(values)
    if not items:
        return None
    return round(sum(items) / len(items), 1)


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------
class EngineAnalyzer:
    """Haelt eine Stockfish-Instanz offen und analysiert Partien nacheinander.

    Immer als Context-Manager benutzen, sonst bleibt ein Engine-Prozess zurueck.
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        self._engine: Optional[chess.engine.SimpleEngine] = None

    # -- Lebenszyklus ---------------------------------------------------
    def open(self) -> chess.engine.SimpleEngine:
        if self._engine is None:
            try:
                self._engine = chess.engine.SimpleEngine.popen_uci(
                    self.settings.engine_path
                )
            except FileNotFoundError as exc:
                raise AnalysisError(
                    f"Stockfish nicht gefunden unter '{self.settings.engine_path}'. "
                    "CHESS_ENGINE_PATH setzen oder das Paket im Image installieren."
                ) from exc
            except chess.engine.EngineError as exc:
                raise AnalysisError(f"Stockfish liess sich nicht starten: {exc}") from exc

            options: dict[str, object] = {}
            available = self._engine.options
            if "Threads" in available:
                options["Threads"] = self.settings.engine_threads
            if "Hash" in available:
                options["Hash"] = self.settings.engine_hash_mb
            if options:
                try:
                    self._engine.configure(options)
                except chess.engine.EngineError as exc:
                    log.warning("Engine-Optionen abgelehnt (%s) - laufe mit Standard.", exc)
        return self._engine

    def close(self) -> None:
        if self._engine is not None:
            try:
                self._engine.quit()
            except Exception:  # noqa: BLE001 - beim Aufraeumen egal
                pass
            finally:
                self._engine = None

    def __enter__(self) -> "EngineAnalyzer":
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def engine_id(self) -> str:
        engine = self._engine
        if engine is None:
            return "nicht gestartet"
        return str(engine.id.get("name", "unbekannt"))

    # -- Interna --------------------------------------------------------
    def _limit(self) -> chess.engine.Limit:
        if self.settings.engine_depth > 0:
            return chess.engine.Limit(depth=self.settings.engine_depth)
        return chess.engine.Limit(time=self.settings.engine_movetime)

    def _categorize(self, cp_loss: int) -> str:
        if cp_loss >= self.settings.blunder_cp:
            return CATEGORY_BLUNDER
        if cp_loss >= self.settings.mistake_cp:
            return CATEGORY_MISTAKE
        if cp_loss >= self.settings.inaccuracy_cp:
            return CATEGORY_INACCURACY
        return CATEGORY_OK

    def evaluate(self, board: chess.Board) -> Evaluation:
        """Bewertung aus Sicht von Weiss plus bester Zug in dieser Stellung."""
        # Beendete Stellungen darf man der Engine nicht vorlegen - sie hat dort
        # keinen legalen Zug und python-chess wirft einen EngineError.
        outcome = board.outcome(claim_draw=False)
        if outcome is not None:
            if outcome.winner is None:
                return Evaluation(centipawns=0)
            # mate wird wie bei Stockfish aus Sicht von Weiss vorzeichenbehaftet
            # gefuehrt: positiv = Weiss setzt matt. Nicht 0 verwenden, sonst
            # laesst sich "Matt fuer uns" nicht von "Matt gegen uns" trennen.
            if outcome.winner == chess.WHITE:
                return Evaluation(centipawns=EVAL_CLAMP, mate=1)
            return Evaluation(centipawns=-EVAL_CLAMP, mate=-1)

        engine = self.open()
        info = engine.analyse(board, self._limit())

        score = info.get("score")
        if score is None:
            raise AnalysisError("Engine lieferte keine Bewertung zurueck.")
        white_score = score.white()
        centipawns = clamp_eval(white_score.score(mate_score=MATE_SCORE))
        mate = white_score.mate()

        best_move: Optional[chess.Move] = None
        best_san: Optional[str] = None
        principal_variation = info.get("pv") or []
        if principal_variation:
            best_move = principal_variation[0]
            try:
                best_san = board.san(best_move)
            except (ValueError, AssertionError):
                best_san = None
                best_move = None
        return Evaluation(
            centipawns=centipawns, mate=mate, best_move=best_move, best_san=best_san
        )

    # -- Oeffentlich ----------------------------------------------------
    def analyse_game(
        self,
        pgn_text: str,
        my_color: chess.Color,
        time_control: str = "",
    ) -> GameReport:
        game = chess.pgn.read_game(io.StringIO(pgn_text or ""))
        if game is None:
            raise AnalysisError("PGN liess sich nicht lesen.")

        nodes = list(game.mainline())
        if not nodes:
            raise AnalysisError("Partie enthaelt keine Zuege.")

        settings = self.settings
        max_plies = min(len(nodes), settings.analysis_max_plies)
        increment = parse_increment(time_control)

        # Stellung vor jedem Zug einsammeln (positions[i] = vor Halbzug i),
        # plus die Schlussstellung an Position len(moves).
        board = game.board()
        positions: list[chess.Board] = [board.copy(stack=False)]
        moves: list[chess.Move] = []
        clocks: list[Optional[float]] = []
        for node in nodes:
            move = node.move
            if move is None:
                break
            moves.append(move)
            try:
                clocks.append(node.clock())
            except Exception:  # noqa: BLE001 - kaputte Kommentare sind kein Grund abzubrechen
                clocks.append(None)
            board.push(move)
            positions.append(board.copy(stack=False))

        if not moves:
            raise AnalysisError("Partie enthaelt keine gueltigen Zuege.")

        evaluations: dict[int, Evaluation] = {}

        def evaluation(index: int) -> Evaluation:
            if index not in evaluations:
                evaluations[index] = self.evaluate(positions[index])
            return evaluations[index]

        sign = 1 if my_color == chess.WHITE else -1
        report = GameReport(move_count=len(moves))
        losses: list[int] = []
        losses_by_phase: dict[str, list[int]] = {phase: [] for phase in PHASES}
        previous_clock: dict[bool, Optional[float]] = {
            chess.WHITE: None,
            chess.BLACK: None,
        }

        def mate_against(evaluation_result: Evaluation) -> bool:
            """Droht Matt gegen uns? mate ist aus Sicht von Weiss vorzeichenbehaftet."""
            if evaluation_result.mate is None:
                return False
            return (evaluation_result.mate * sign) <= 0

        def mate_for_us(evaluation_result: Evaluation) -> bool:
            if evaluation_result.mate is None:
                return False
            return (evaluation_result.mate * sign) > 0

        for index in range(len(moves)):
            position_before = positions[index]
            mover = position_before.turn
            clock_after = clocks[index] if index < len(clocks) else None

            spent: Optional[float] = None
            earlier = previous_clock[mover]
            if earlier is not None and clock_after is not None:
                spent = round(max(0.0, earlier - clock_after + increment), 1)
            if clock_after is not None:
                previous_clock[mover] = clock_after

            if mover != my_color:
                continue
            if index >= max_plies:
                continue
            if index < settings.skip_opening_plies:
                continue

            before = evaluation(index)
            after = evaluation(index + 1)

            cp_before = sign * before.centipawns
            cp_after = sign * after.centipawns
            cp_loss = max(0, cp_before - cp_after)
            win_loss = round(
                max(0.0, win_percent(cp_before) - win_percent(cp_after)), 2
            )
            category = self._categorize(cp_loss)
            phase = phase_for(position_before, index, settings.opening_plies)

            try:
                san = position_before.san(moves[index])
            except (ValueError, AssertionError):
                san = moves[index].uci()

            error_type: Optional[str] = None
            missed_motif: Optional[str] = None
            refutation_san: Optional[str] = None

            # Verpasste Taktik nur bei Fehlern und Patzern, nicht bei
            # Ungenauigkeiten: unter 100 Centipawn ist ein "verpasster Spiess"
            # meist Zufall der Geometrie und kein Trainingsthema. Lieber
            # weniger Treffer als eine Liste, der man nicht trauen kann.
            if category in (CATEGORY_MISTAKE, CATEGORY_BLUNDER):
                missed_motif = classify_missed(
                    position_before, before.best_move, mate_for_us(before)
                )

            if category != CATEGORY_OK:
                error_type = classify_error(
                    board_before=position_before,
                    move=moves[index],
                    board_after=positions[index + 1],
                    reply=after.best_move,
                    cp_before=cp_before,
                    cp_after=cp_after,
                    mate_against_now=mate_against(after),
                    mate_against_before=mate_against(before),
                    mate_for_us_before=mate_for_us(before),
                    mate_for_us_now=mate_for_us(after),
                )
                refutation_san = after.best_san

            report.moves.append(
                MoveReport(
                    ply=index + 1,
                    move_number=index // 2 + 1,
                    san=san,
                    phase=phase,
                    cp_before=cp_before,
                    cp_after=cp_after,
                    cp_loss=cp_loss,
                    win_loss=win_loss,
                    category=category,
                    error_type=error_type,
                    missed_motif=missed_motif,
                    # Den besten Zug nur dort merken, wo er interessant ist.
                    best_move_san=before.best_san if category != CATEGORY_OK else None,
                    refutation_san=refutation_san,
                    clock_seconds=clock_after,
                    seconds_spent=spent,
                )
            )

            losses.append(cp_loss)
            losses_by_phase[phase].append(cp_loss)

            if category == CATEGORY_BLUNDER:
                report.blunders += 1
            elif category == CATEGORY_MISTAKE:
                report.mistakes += 1
            elif category == CATEGORY_INACCURACY:
                report.inaccuracies += 1

            if category != CATEGORY_OK and report.first_error_ply is None:
                report.first_error_ply = index + 1

        report.acpl = _mean(losses)
        report.acpl_by_phase = {
            phase: _mean(values) for phase, values in losses_by_phase.items()
        }
        return report
