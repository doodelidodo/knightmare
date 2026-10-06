"""Selbsttest ohne Netz und ohne Datenbank.

Auf dem Server aufrufen mit:

    docker compose exec chess-analyzer python -m app.selftest

Geprueft wird:
  1. Hilfsfunktionen (Bewertungskurve, Zeitkontrolle, Spielphasen).
  2. Die Abbildung eines Chess.com-API-Eintrags auf das Datenmodell.
  3. Die Einordnung der Fehlerarten an konstruierten Stellungen.
  4. Stockfish laesst sich starten und analysiert eine ganze Partie korrekt.
  5. Taktikaufgaben: Wiederholung, Tagesauswahl, Zugpruefung, Vorbereitung
     mit Stockfish (in einer eigenen SQLite-Datei, nicht der echten DB).

Rueckgabecode 0 = alles gut, 1 = mindestens ein Test fehlgeschlagen.
"""

from __future__ import annotations

import sys
from typing import Optional

import chess

from .analysis import (
    CATEGORY_BLUNDER,
    CATEGORY_MISTAKE,
    CATEGORY_OK,
    ERROR_ALLOWED_MATE,
    ERROR_BAD_TRADE,
    ERROR_FORK,
    ERROR_HANGING_PIECE,
    ERROR_LABELS,
    ERROR_MISSED_MATE,
    ERROR_MISSED_THREAT,
    ERROR_MISSED_WIN,
    ERROR_POSITIONAL,
    MISSED_DISCOVERED,
    MISSED_FORK,
    MISSED_LABELS,
    MISSED_MATE,
    MISSED_MATERIAL,
    MISSED_PIN,
    MISSED_SKEWER,
    MISSED_TYPES,
    EngineAnalyzer,
    classify_error,
    categorize,
    classify_missed,
    mate_bucket,
    material_balance,
    parse_increment,
    phase_for,
    win_percent,
)
from .config import load_settings
from .lichess import QUERY_PARAMS
from .pipeline import (
    map_chesscom_game,
    map_lichess_game,
    opening_family_from_name,
    opening_name_from_url,
)

# Schulmatt: Schwarz spielt im 3. Zug Sf6?? und wird im 4. Zug matt gesetzt.
# Kurz genug fuer einen schnellen Test, aber mit einem eindeutigen Patzer.
SAMPLE_PGN = """[Event "Selbsttest"]
[Site "Chess.com"]
[Date "2026.01.01"]
[White "Gegner"]
[Black "Ich"]
[Result "1-0"]
[ECO "C23"]
[ECOUrl "https://www.chess.com/openings/Bishops-Opening-2...Nc6"]
[TimeControl "300+2"]
[Termination "Gegner won by checkmate"]

1. e4 {[%clk 0:05:00]} 1... e5 {[%clk 0:04:58]}
2. Bc4 {[%clk 0:04:55]} 2... Nc6 {[%clk 0:04:50]}
3. Qh5 {[%clk 0:04:52]} 3... Nf6 {[%clk 0:04:20]}
4. Qxf7# {[%clk 0:04:50]} 1-0
"""

SAMPLE_PAYLOAD = {
    "url": "https://www.chess.com/game/live/1234567890",
    "uuid": "selftest-0001",
    "pgn": SAMPLE_PGN,
    "time_control": "300+2",
    "end_time": 1767225600,
    "rated": True,
    "time_class": "rapid",
    "rules": "chess",
    "white": {"username": "Gegner", "rating": 1450, "result": "win"},
    "black": {"username": "Ich", "rating": 1430, "result": "checkmated"},
    "accuracies": {"white": 88.5, "black": 41.2},
}

LICHESS_PAYLOAD = {
    "id": "aBcD1234",
    "rated": True,
    "variant": "standard",
    "speed": "rapid",
    "perf": "rapid",
    "createdAt": 1767225000000,
    "lastMoveAt": 1767225600000,
    "status": "mate",
    "winner": "white",
    "players": {
        "white": {"user": {"name": "Gegner", "id": "gegner"}, "rating": 1500},
        "black": {"user": {"name": "aBetterDodo", "id": "abetterdodo"}, "rating": 1480,
                  "analysis": {"accuracy": 44.0}},
    },
    "opening": {"eco": "C23", "name": "Bishop's Opening: Boi Variation", "ply": 4},
    "clock": {"initial": 600, "increment": 5, "totalTime": 800},
    "pgn": SAMPLE_PGN,
}

PASS = "  OK  "
FAIL = " FEHL "


def _check(label: str, condition: bool, detail: str = "") -> bool:
    marker = PASS if condition else FAIL
    print(f"[{marker}] {label}{(' - ' + detail) if detail else ''}")
    return condition


# --------------------------------------------------------------------------
# 1) Hilfsfunktionen
# --------------------------------------------------------------------------
def test_helpers() -> bool:
    ok = True
    ok &= _check("Inkrement aus '300+2'", parse_increment("300+2") == 2.0)
    ok &= _check("Inkrement aus '600'", parse_increment("600") == 0.0)
    ok &= _check("Inkrement aus Daily '1/86400'", parse_increment("1/86400") == 0.0)
    ok &= _check("Gewinnkurve bei 0 cp", abs(win_percent(0) - 50.0) < 0.01)
    ok &= _check(
        "Gewinnkurve bei +300 cp", win_percent(300) > 70.0, f"{win_percent(300):.1f}%"
    )

    board = chess.Board()
    ok &= _check("Grundstellung ist Eroeffnung", phase_for(board, 0, 20) == "opening")
    endgame = chess.Board("8/5k2/8/8/8/8/5K2/7R w - - 0 1")
    ok &= _check("Turmendspiel ist Endspiel", phase_for(endgame, 60, 20) == "endgame")

    ok &= _check(
        "Materialbilanz Grundstellung ist ausgeglichen",
        material_balance(chess.Board(), chess.WHITE) == 0,
    )
    ok &= _check(
        "Materialbilanz: ein Bauer mehr",
        material_balance(chess.Board("4k3/8/8/8/8/8/4P3/4K3 w - - 0 1"), chess.WHITE) == 1,
    )

    # Einstufung: derselbe Zug, zwei Massstaebe. 100 Centipawn bei +800
    # sind nach Centipawn ein "Fehler", nach Gewinnwahrscheinlichkeit nichts -
    # genau der Unterschied, der den Resttopf Stellungsfehler aufgeblaeht hat.
    class _Scale:
        def __init__(self, scale: str) -> None:
            self.error_scale = scale
            self.inaccuracy_cp, self.mistake_cp, self.blunder_cp = 50, 100, 300
            self.inaccuracy_win, self.mistake_win, self.blunder_win = 10.0, 20.0, 30.0

    drop_at_equality = win_percent(0) - win_percent(-100)      # ~9.2 Punkte
    drop_when_winning = win_percent(800) - win_percent(700)   # ~1.4 Punkte
    ok &= _check(
        "100 cp bei Gleichstand: nach Centipawn ein Fehler",
        categorize(_Scale("centipawn"), 100, drop_at_equality) == CATEGORY_MISTAKE,
    )
    ok &= _check(
        "100 cp bei +800: nach Centipawn immer noch ein Fehler",
        categorize(_Scale("centipawn"), 100, drop_when_winning) == CATEGORY_MISTAKE,
    )
    ok &= _check(
        "100 cp bei +800: nach Gewinnwahrscheinlichkeit kein Fehler",
        categorize(_Scale("winprob"), 100, drop_when_winning) == CATEGORY_OK,
        f"{drop_when_winning:.1f} Punkte",
    )
    ok &= _check(
        "Patzer bleibt Patzer: 35 Punkte Verlust",
        categorize(_Scale("winprob"), 400, 35.0) == CATEGORY_BLUNDER,
    )
    ok &= _check(
        "verpasstes Matt bei +900 zaehlt trotzdem als Fehler",
        categorize(_Scale("winprob"), 100, 1.0, missed_mate=True) == CATEGORY_MISTAKE,
    )
    ok &= _check(
        "verpasstes Matt in 4 bei +900 zaehlt noch als Fehler",
        categorize(_Scale("winprob"), 100, 1.0, missed_mate=True, mate_in=4) == CATEGORY_MISTAKE,
    )
    ok &= _check(
        "verpasstes Matt in 5 bei +900 zaehlt nicht mehr - kein Trainingsthema",
        categorize(_Scale("winprob"), 100, 1.0, missed_mate=True, mate_in=5) == CATEGORY_OK,
    )
    ok &= _check(
        "nachgeprueft ohne Matt (-1) hebt nicht an",
        categorize(_Scale("winprob"), 100, 1.0, missed_mate=True, mate_in=-1) == CATEGORY_OK,
    )
    ok &= _check(
        "Mattgruppen 1..4, 5+ und unklar",
        [mate_bucket(n) for n in (1, 2, 3, 4, 5, 12, None, -1)]
        == ["1", "2", "3", "4", "5+", "5+", "?", "?"],
    )
    ok &= _check(
        "verpasstes Matt macht aus einem Patzer keinen Fehler",
        categorize(_Scale("winprob"), 900, 45.0, missed_mate=True) == CATEGORY_BLUNDER,
    )
    ok &= _check(
        "unter 50 cp erreicht nie 10 Punkte - nach Umstellung kommen keine neuen Fehler hinzu",
        win_percent(0) - win_percent(-49) < 10.0,
        f"{win_percent(0) - win_percent(-49):.1f} Punkte bei 49 cp am steilsten Punkt",
    )

    family = opening_family_from_name("Scandinavian Defense Mieses Kotroc Variation")
    ok &= _check("Eroeffnungsfamilie (Chess.com-Schreibweise)",
                 family == "Scandinavian Defense", str(family))
    lichess_family = opening_family_from_name("Sicilian Defense: Najdorf Variation")
    ok &= _check("Eroeffnungsfamilie (Lichess-Schreibweise mit Doppelpunkt)",
                 lichess_family == "Sicilian Defense", str(lichess_family))

    name = opening_name_from_url(
        "https://www.chess.com/openings/Sicilian-Defense-Najdorf-Variation-6.Be3"
    )
    ok &= _check(
        "Eroeffnungsname aus URL",
        name == "Sicilian Defense Najdorf Variation",
        str(name),
    )
    return bool(ok)


# --------------------------------------------------------------------------
# 2) Abbildung der Chess.com-Daten
# --------------------------------------------------------------------------
def test_mapping() -> bool:
    game = map_chesscom_game(SAMPLE_PAYLOAD, "ich")
    if not _check("Partie liess sich abbilden", game is not None):
        return False
    assert game is not None

    ok = True
    ok &= _check("eigene Farbe erkannt", game.color == "black", game.color)
    ok &= _check("Ergebnis erkannt", game.result == "loss", game.result)
    ok &= _check("Gegner erkannt", game.opponent == "Gegner", game.opponent)
    ok &= _check("eigene Wertung", game.my_rating == 1430, str(game.my_rating))
    ok &= _check("Zeitkategorie", game.time_class == "rapid", game.time_class)
    ok &= _check("ECO-Code aus PGN", game.eco == "C23", str(game.eco))
    ok &= _check(
        "eigene Genauigkeit", game.accuracy_self == 41.2, str(game.accuracy_self)
    )
    ok &= _check("Plattform vermerkt", game.platform == "chesscom", game.platform)
    ok &= _check(
        "Schluessel traegt Plattform-Praefix",
        game.uuid.startswith("chesscom:"),
        game.uuid,
    )
    ok &= _check(
        "Partie eines anderen Spielers wird ignoriert",
        map_chesscom_game(SAMPLE_PAYLOAD, "jemand-anderes") is None,
    )
    return bool(ok)


def test_lichess_mapping() -> bool:
    # Diese drei Parameter entscheiden, ob ueberhaupt etwas Analysierbares
    # ankommt. "moves=false" liefert ein PGN aus reinen Kopfzeilen - die
    # Partien landen dann alle als "enthaelt keine Zuege" im Fehlertopf.
    ok = True
    ok &= _check("Lichess liefert die Zuege mit",
                 QUERY_PARAMS.get("moves") == "true",
                 str(QUERY_PARAMS.get("moves")))
    ok &= _check("PGN kommt im JSON",
                 QUERY_PARAMS.get("pgnInJson") == "true",
                 str(QUERY_PARAMS.get("pgnInJson")))
    ok &= _check("Uhrzeiten kommen mit",
                 QUERY_PARAMS.get("clocks") == "true",
                 str(QUERY_PARAMS.get("clocks")))

    game = map_lichess_game(LICHESS_PAYLOAD, "aBetterDodo")
    if not _check("Lichess-Partie liess sich abbilden", game is not None):
        return False
    assert game is not None

    ok &= _check("Plattform vermerkt", game.platform == "lichess", game.platform)
    ok &= _check("Schluessel mit Praefix", game.uuid == "lichess:aBcD1234", game.uuid)
    ok &= _check("eigene Farbe erkannt", game.color == "black", game.color)
    ok &= _check("Ergebnis aus dem Sieger abgeleitet", game.result == "loss", game.result)
    ok &= _check("Gegner erkannt", game.opponent == "Gegner", game.opponent)
    ok &= _check("eigene Wertung", game.my_rating == 1480, str(game.my_rating))
    ok &= _check("Zeitkategorie", game.time_class == "rapid", game.time_class)
    ok &= _check("Zeitkontrolle aus der Uhr", game.time_control == "600+5",
                 game.time_control)
    ok &= _check("ECO direkt uebernommen", game.eco == "C23", str(game.eco))
    ok &= _check("Eroeffnungsfamilie abgeleitet",
                 game.opening_family == "Bishop's Opening",
                 str(game.opening_family))
    ok &= _check("Genauigkeit aus der Lichess-Analyse",
                 game.accuracy_self == 44.0, str(game.accuracy_self))
    ok &= _check("Link zeigt die eigene Farbe",
                 game.url == "https://lichess.org/aBcD1234/black", game.url)
    ok &= _check("Zeitstempel in Millisekunden umgerechnet",
                 game.played_at.year == 2026, str(game.played_at))

    # Gross-/Kleinschreibung darf keine Rolle spielen.
    ok &= _check("Benutzername unabhaengig von Schreibweise",
                 map_lichess_game(LICHESS_PAYLOAD, "abetterdodo") is not None)
    ok &= _check("fremde Partie wird ignoriert",
                 map_lichess_game(LICHESS_PAYLOAD, "jemand-anderes") is None)

    variant = dict(LICHESS_PAYLOAD, variant="chess960")
    ok &= _check("Variante wird uebersprungen",
                 map_lichess_game(variant, "aBetterDodo") is None)

    aborted = dict(LICHESS_PAYLOAD, status="aborted")
    ok &= _check("abgebrochene Partie wird uebersprungen",
                 map_lichess_game(aborted, "aBetterDodo") is None)

    draw = dict(LICHESS_PAYLOAD)
    draw.pop("winner")
    drawn = map_lichess_game(draw, "aBetterDodo")
    ok &= _check("fehlender Sieger bedeutet Remis",
                 drawn is not None and drawn.result == "draw",
                 drawn.result if drawn else "-")
    return bool(ok)


# --------------------------------------------------------------------------
# 3) Fehlerarten
# --------------------------------------------------------------------------
def _classify_case(
    fen: str,
    move_uci: str,
    reply_uci: Optional[str],
    cp_before: int,
    cp_after: int,
    mate_now: bool = False,
    mate_before: bool = False,
    mate_for_us_before: bool = False,
    mate_for_us_now: bool = False,
) -> str:
    """Baut eine Stellung auf, spielt Zug und Gegenzug, und ordnet den Fehler ein."""
    board_before = chess.Board(fen)
    move = chess.Move.from_uci(move_uci)
    if move not in board_before.legal_moves:
        raise ValueError(f"Zug {move_uci} ist in dieser Stellung nicht legal: {fen}")

    board_after = board_before.copy(stack=False)
    board_after.push(move)

    reply: Optional[chess.Move] = None
    if reply_uci:
        reply = chess.Move.from_uci(reply_uci)
        if reply not in board_after.legal_moves:
            raise ValueError(f"Gegenzug {reply_uci} ist nach {move_uci} nicht legal")

    return classify_error(
        board_before=board_before,
        move=move,
        board_after=board_after,
        reply=reply,
        cp_before=cp_before,
        cp_after=cp_after,
        mate_against_now=mate_now,
        mate_against_before=mate_before,
        mate_for_us_before=mate_for_us_before,
        mate_for_us_now=mate_for_us_now,
    )


def test_classification() -> bool:
    cases = [
        (
            "Figur eingestellt: Laeufer zieht auf ein vom Bauern gedecktes Feld",
            # Weiss: La3, Kd1. Schwarz: Bauer c5, Ke8. Weiss zieht Lb4?? cxb4
            ("4k3/8/8/2p5/8/B7/8/3K4 w - - 0 1", "a3b4", "c5b4", 200, -100),
            ERROR_HANGING_PIECE,
        ),
        (
            "Drohung uebersehen: Turm zieht weg, der Laeufer stand schon haengen",
            ("4k3/8/8/2p5/1B6/8/7R/3K4 w - - 0 1", "h2h3", "c5b4", 600, 300),
            ERROR_MISSED_THREAT,
        ),
        (
            "Gabel kassiert: Koenigszug erlaubt Springergabel auf Koenig und Turm",
            # Weiss: Ke1, Tf1. Schwarz: Sg4, Ke8. Nach Kd1?? folgt Se3+ (Gabel).
            ("4k3/8/8/8/6n1/8/8/4KR2 w - - 0 1", "e1d1", "g4e3", 100, -200),
            ERROR_FORK,
        ),
        (
            "Schlechter Abtausch: Laeufer schlaegt gedeckten Bauern",
            # Weiss: Lb4, Kd1. Schwarz: Bauern b6+c5, Ke8. Lxc5?? bxc5
            ("4k3/8/1p6/2p5/1B6/8/8/3K4 w - - 0 1", "b4c5", "b6c5", 150, -150),
            ERROR_BAD_TRADE,
        ),
        (
            "Gewinn verpasst: klarer Vorteil verspielt, ohne Material zu verlieren",
            (chess.STARTING_FEN, "e2e4", "e7e5", 400, 10),
            ERROR_MISSED_WIN,
        ),
        (
            "Stellungsfehler: kein Material weg, Stellung nur schlechter",
            (chess.STARTING_FEN, "e2e4", "e7e5", 30, -80),
            ERROR_POSITIONAL,
        ),
        (
            "Matt zugelassen schlaegt alles andere",
            ("4k3/8/8/2p5/8/B7/8/3K4 w - - 0 1", "a3b4", "c5b4", 200, -1000, True, False),
            ERROR_ALLOWED_MATE,
        ),
        (
            # Weiss: Ta1, Kg1. Schwarz: Kg8 ohne Luft. Ta8 waere matt, Ta7 nicht.
            "Matt verpasst, Stellung bleibt trotzdem gewonnen",
            ("6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1", "a1a7", "g8h8",
             1000, 400, False, False, True, False),
            ERROR_MISSED_MATE,
        ),
        (
            # Der wichtige Fall: kein Gegenzug, weil die Partie zu Ende ist -
            # patt gesetzt statt matt gesetzt. Fiel frueher in den Resttopf,
            # weil die Einordnung bei fehlendem Gegenzug sofort ausstieg.
            "Matt verpasst, auch ohne Gegenzug (patt statt matt)",
            ("6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1", "a1a7", None,
             1000, 0, False, False, True, False),
            ERROR_MISSED_MATE,
        ),
        (
            "weiter bestehendes Matt ist kein verpasstes",
            ("6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1", "a1a7", "g8h8",
             1000, 1000, False, False, True, True),
            ERROR_POSITIONAL,
        ),
    ]

    ok = True
    for label, arguments, expected in cases:
        try:
            result = _classify_case(*arguments)
        except Exception as exc:  # noqa: BLE001
            ok &= _check(label, False, f"Stellung ungueltig: {exc}")
            continue
        ok &= _check(
            label,
            result == expected,
            f"erwartet {ERROR_LABELS[expected]}, bekommen {ERROR_LABELS.get(result, result)}",
        )

    # Gegenprobe: ohne Widerlegung der Engine bleibt es beim Stellungsfehler.
    quiet = _classify_case(
        "4k3/8/8/8/8/8/8/3QK3 w - - 0 1", "d1d4", None, 50, -60
    )
    ok &= _check(
        "Ohne Gegenzug wird nichts hineininterpretiert",
        quiet == ERROR_POSITIONAL,
        quiet,
    )
    return bool(ok)


# --------------------------------------------------------------------------
# 5) Verpasste Taktik
#
# Jede Stellung ist von Hand gebaut und enthaelt genau ein Motiv. Die beiden
# letzten Faelle sind die wichtigeren: sie pruefen, dass NICHTS gemeldet wird,
# wo nichts ist. Eine Erkennung, die grosszuegig ist, fuellt die Liste mit
# Rauschen - und dann trainiert man das Falsche.
# --------------------------------------------------------------------------
def _missed_case(fen: str, move_uci: str, mate_for_us: bool = False) -> Optional[str]:
    board = chess.Board(fen)
    move = chess.Move.from_uci(move_uci)
    if move not in board.legal_moves:
        raise ValueError(f"Zug {move_uci} ist in dieser Stellung nicht legal: {fen}")
    return classify_missed(board, move, mate_for_us)


def test_missed() -> bool:
    ok = True

    # Matt: Turm auf die Grundreihe, der Koenig hat keine Luft.
    ok &= _check(
        "Matt erkannt (Grundreihe)",
        _missed_case("6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1", "a1a8") == MISSED_MATE,
        str(_missed_case("6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1", "a1a8")),
    )

    # Gabel: Sc7+ trifft Koenig und Turm zugleich.
    ok &= _check(
        "Gabel erkannt (Koenig und Turm)",
        _missed_case("r3k3/8/8/1N6/8/8/8/4K3 w - - 0 1", "b5c7") == MISSED_FORK,
        str(_missed_case("r3k3/8/8/1N6/8/8/8/4K3 w - - 0 1", "b5c7")),
    )

    # Fesselung: Turm auf e1, der Springer auf e7 kann nicht weg.
    ok &= _check(
        "Fesselung erkannt (Springer an den Koenig)",
        _missed_case("4k3/4n3/8/8/8/8/8/R5K1 w - - 0 1", "a1e1") == MISSED_PIN,
        str(_missed_case("4k3/4n3/8/8/8/8/8/R5K1 w - - 0 1", "a1e1")),
    )

    # Spiess: Ta8+ zwingt den Koenig weg, dahinter faellt der Turm.
    ok &= _check(
        "Spiess erkannt (Koenig vorn, Turm dahinter)",
        _missed_case("4k2r/8/8/8/8/8/8/R5K1 w - - 0 1", "a1a8") == MISSED_SKEWER,
        str(_missed_case("4k2r/8/8/8/8/8/8/R5K1 w - - 0 1", "a1a8")),
    )

    # Abzug: der Springer raeumt die Diagonale b2-g7, der Laeufer trifft die Dame.
    ok &= _check(
        "Abzugsangriff erkannt (Springer raeumt die Diagonale)",
        _missed_case("6k1/6q1/8/8/3N4/8/1B6/7K w - - 0 1", "d4b5") == MISSED_DISCOVERED,
        str(_missed_case("6k1/6q1/8/8/3N4/8/1B6/7K w - - 0 1", "d4b5")),
    )

    # Freies Material: der Turm auf d5 ist von nichts gedeckt.
    ok &= _check(
        "Ungedecktes Material erkannt",
        _missed_case("4k3/8/8/3r4/8/8/8/3RK3 w - - 0 1", "d1d5") == MISSED_MATERIAL,
        str(_missed_case("4k3/8/8/3r4/8/8/8/3RK3 w - - 0 1", "d1d5")),
    )

    # Gegenprobe 1: derselbe Schlagzug, aber der Turm ist gedeckt. Gleicher
    # Wert, gedeckt - das ist ein Abtausch und kein verpasstes Motiv.
    ok &= _check(
        "gedeckter Gleichwert ist kein Motiv",
        _missed_case("3rk3/8/8/3r4/8/8/3R4/4K3 w - - 0 1", "d2d5") is None,
        str(_missed_case("3rk3/8/8/3r4/8/8/3R4/4K3 w - - 0 1", "d2d5")),
    )

    # Gegenprobe 2: ein stiller Bauernzug, der gar nichts anstellt.
    ok &= _check(
        "stiller Zug ist kein Motiv",
        _missed_case("4k3/8/8/8/8/8/4P3/4K3 w - - 0 1", "e2e3") is None,
        str(_missed_case("4k3/8/8/8/8/8/4P3/4K3 w - - 0 1", "e2e3")),
    )

    # Klassiker aus der Eroeffnung: Laeufer fesselt den Springer an die Dame.
    ok &= _check(
        "Fesselung an die Dame erkannt",
        _missed_case("3qk3/8/5n2/8/8/8/8/2B1K3 w - - 0 1", "c1g5") == MISSED_PIN,
        str(_missed_case("3qk3/8/5n2/8/8/8/8/2B1K3 w - - 0 1", "c1g5")),
    )

    # Gegenprobe 3: ein Bauer vor einem Turm ist keine Fesselung, die man
    # ueben muesste. Mit der frueheren, grosszuegigeren Schranke war sie eine -
    # und trug damit fast jeder neunte zufaellige Zug dieses Etikett.
    ok &= _check(
        "Bauer vor Turm ist keine Fesselung",
        _missed_case("4k3/8/4r3/3p4/8/1B6/8/7K w - - 0 1", "b3c4") is None,
        str(_missed_case("4k3/8/4r3/3p4/8/1B6/8/7K w - - 0 1", "b3c4")),
    )

    # Ohne besten Zug darf nichts gemeldet werden.
    ok &= _check(
        "fehlender bester Zug liefert nichts",
        classify_missed(chess.Board(), None, False) is None,
    )

    # Jede Kategorie braucht eine Beschriftung, sonst steht in der Oberflaeche
    # der rohe Schluessel.
    ok &= _check(
        "alle Motive haben eine Beschriftung",
        all(name in MISSED_LABELS for name in MISSED_TYPES),
        ", ".join(sorted(set(MISSED_TYPES) - set(MISSED_LABELS))),
    )
    return bool(ok)


# --------------------------------------------------------------------------
# 6) Vollstaendige Analyse mit Stockfish
# --------------------------------------------------------------------------
def test_analysis() -> bool:
    settings = load_settings()
    print(
        f"\nEngine: {settings.engine_path} "
        f"(Rechenzeit {settings.engine_movetime}s pro Stellung)"
    )

    try:
        with EngineAnalyzer(settings) as analyzer:
            print(f"Engine gestartet: {analyzer.engine_id()}\n")
            report = analyzer.analyse_game(SAMPLE_PGN, chess.BLACK, "300+2")
            # Grundreihe: Ta8 matt. Und ein Matt in 2 ohne Matt in 1 - Kb6,
            # dann Th8 matt; Th8+ sofort laesst Ka7 entkommen.
            mate_one = analyzer.mate_distance(
                chess.Board("6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1"), chess.WHITE)
            mate_two = analyzer.mate_distance(
                chess.Board("k7/8/2K5/8/8/8/8/7R w - - 0 1"), chess.WHITE)
            no_mate = analyzer.mate_distance(chess.Board(), chess.WHITE)
    except Exception as exc:  # noqa: BLE001
        _check("Stockfish gestartet", False, str(exc))
        return False

    ok = True
    ok &= _check("Mattlaenge: Matt in 1 erkannt", mate_one == 1, str(mate_one))
    ok &= _check("Mattlaenge: Matt in 2, nicht in 1", mate_two == 2, str(mate_two))
    ok &= _check("Mattlaenge: Grundstellung hat keins", no_mate is None, str(no_mate))
    ok &= _check("Zuege gefunden", report.move_count == 7, str(report.move_count))
    ok &= _check(
        "eigene Zuege bewertet", len(report.moves) == 3, f"{len(report.moves)} Zuege"
    )
    ok &= _check("Patzer erkannt", report.blunders >= 1, f"{report.blunders} Patzer")

    blunder = next(
        (move for move in report.moves if move.category == CATEGORY_BLUNDER), None
    )
    ok &= _check(
        "Patzer ist der Springerzug im 3. Zug",
        blunder is not None and blunder.san == "Nf6",
        blunder.san if blunder else "keiner",
    )
    if blunder is not None:
        ok &= _check(
            "besserer Zug wurde mitgeliefert",
            bool(blunder.best_move_san),
            str(blunder.best_move_san),
        )
        ok &= _check(
            "Fehlerart ist 'Matt zugelassen'",
            blunder.error_type == ERROR_ALLOWED_MATE,
            str(blunder.error_type),
        )
        ok &= _check(
            "Widerlegung wurde erkannt",
            blunder.refutation_san == "Qxf7#",
            str(blunder.refutation_san),
        )

    ok &= _check(
        "fehlerfreie Zuege bekommen keine Fehlerart",
        all(move.error_type is None for move in report.moves if move.category == "ok"),
    )

    with_clock = [move for move in report.moves if move.clock_seconds is not None]
    ok &= _check(
        "Restzeiten gelesen",
        len(with_clock) == 3,
        f"{len(with_clock)} von {len(report.moves)}",
    )

    nf6 = next((move for move in report.moves if move.san == "Nf6"), None)
    if nf6 is not None:
        # 4:50 -> 4:20 sind 30 Sekunden, plus 2 Sekunden Inkrement.
        ok &= _check(
            "Zugzeit berechnet",
            nf6.seconds_spent is not None and abs(nf6.seconds_spent - 32.0) < 0.6,
            f"{nf6.seconds_spent}s",
        )

    ok &= _check(
        "Durchschnittsverlust berechnet", report.acpl is not None, f"{report.acpl} cp"
    )

    print("\nAnalysierte eigene Zuege:")
    for move in report.moves:
        extra = ""
        if move.error_type:
            extra = f"  [{ERROR_LABELS.get(move.error_type, move.error_type)}]"
        print(
            f"  {move.move_number:>2}. {move.san:<6} "
            f"{move.phase:<11} Verlust {move.cp_loss:>5} cp  "
            f"({move.category}{', besser: ' + move.best_move_san if move.best_move_san else ''})"
            f"{extra}"
        )
    return bool(ok)


# --------------------------------------------------------------------------
# 7) Taktikaufgaben
# --------------------------------------------------------------------------
SCHOLAR_PGN = """[Event "Test"]
[White "ich"]
[Black "gegner"]
[Result "0-1"]

1. e4 e5 2. Bc4 Nc6 3. Qh5 Nf6 4. d3 Nxh5 0-1
"""


def _temp_db():
    """Eigene SQLite-Datei - der Selbsttest fasst die echte Datenbank nicht an."""
    import tempfile

    from sqlmodel import SQLModel, create_engine

    from . import models  # noqa: F401 - Tabellen in die Metadata

    path = tempfile.mkdtemp()
    engine = create_engine(f"sqlite:///{path}/selftest.db", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def test_tactics() -> bool:
    from datetime import date, datetime, timedelta
    from types import SimpleNamespace

    import chess.engine
    from sqlalchemy import select, text

    from . import tactics as tx

    ok = True
    d = date(2026, 9, 28)
    ok &= _check("Leitner: neu + gelöst -> Stufe 2 in 3 Tagen", tx.schedule(0, True, d) == (2, d + timedelta(days=3)))
    ok &= _check("Leitner: Stufe 3 + gelöst -> 4 in 14 Tagen", tx.schedule(3, True, d) == (4, d + timedelta(days=14)))
    ok &= _check("Leitner: Stufe 5 + gelöst -> gelernt", tx.schedule(5, True, d) == (tx.LEARNED, None))
    ok &= _check("Leitner: falsch -> Stufe 1, morgen", tx.schedule(4, False, d) == (1, d + timedelta(days=1)))
    even = chess.engine.PovScore(chess.engine.Cp(0), chess.WHITE)
    ok &= _check("Gewinnchance: 0 cp = 50 %", abs(tx.win_for(even, chess.WHITE) - 50) < 0.01)
    ok &= _check("Gewinnchance: gleiche Kurve wie die Analyse",
                 abs(tx.win_for(chess.engine.PovScore(chess.engine.Cp(150), chess.WHITE), chess.WHITE) - win_percent(150)) < 0.01)
    mate = chess.engine.PovScore(chess.engine.Mate(2), chess.WHITE)
    ok &= _check("Matt aus beiden Sichten", tx.win_for(mate, chess.BLACK) == 0.0 and tx.mate_for(mate, chess.WHITE) == 2)
    ok &= _check("Priorität: kurzes Matt vor Stellungsfehler",
                 tx.priority("blunder", 1, "mate", "missed_mate", None, d) > tx.priority("mistake", None, None, "positional", None, d))
    row = SimpleNamespace(url="https://lichess.org/abcd1234", platform="lichess", ply=31)
    ok &= _check("Lichess-Link zeigt die Stellung vor dem Zug", tx.game_link(row) == "https://lichess.org/abcd1234#30")
    ok &= _check("Stellungsschlüssel ohne Zugzähler",
                 tx.position_key("8/8/8/8/8/8/K7/k7 w - - 3 40") == tx.position_key("8/8/8/8/8/8/K7/k7 w - - 0 1"))

    # Zugpruefung ohne Engine: zwei Matts in 1 - beide zaehlen.
    no_engine = tx.Engine(None, 0.1)
    two = SimpleNamespace(fen="6k1/5ppp/8/8/8/8/5PPP/R2Q2K1 w - - 0 1", solution="a1a8")
    s = tx.TacticSettings()
    ok &= _check("Lösungszug gelöst", tx.check_move(no_engine, two, ["a1a8"], s)["result"] == "solved")
    ok &= _check("anderes Matt zählt auch", tx.check_move(no_engine, two, ["d1d8"], s)["result"] == "solved")
    ok &= _check("ohne Engine: anderer Zug falsch", tx.check_move(no_engine, two, ["h2h3"], s)["result"] == "wrong")
    for bad, code in ((["a1a9"], "unknown_move"), (["a1b3"], "illegal_move"), (["a1a8", "g8h7"], "expected_own_move")):
        try:
            tx.check_move(no_engine, two, bad, s)
            ok &= _check(f"{code} abgelehnt", False)
        except tx.MoveError as exc:
            ok &= _check(f"{code} abgelehnt", exc.code == code, exc.code)

    # Tagesauswahl: hoechstens zwei neue je Partie, Wiederholungen dabei,
    # festgeschrieben.
    T, D = tx.TACTICS, tx.DAYS
    eng = _temp_db()

    def fen_n(i: int) -> str:
        # Die Auswahl vergleicht nur den Stellungsschluessel - lauter
        # verschiedene genuegen, gueltig muessen sie hier nicht sein.
        return f"stellung-{i} w - - 0 1"

    base = {"status": "ready", "attempts": 0, "solved": 0}
    with eng.begin() as conn:
        n = 0
        for i in range(6):
            conn.execute(T.insert().values(key=f"g1#{i}", priority=10 - i, game_uuid="g1", box=0, fen=fen_n(n), **base)); n += 1
        for i in range(6):
            conn.execute(T.insert().values(key=f"g{i + 2}#1", priority=1, game_uuid=f"g{i + 2}", box=0, fen=fen_n(n), **base)); n += 1
        for i in range(3):
            conn.execute(T.insert().values(key=f"r{i}#1", priority=0, game_uuid=f"r{i}", box=2, fen=fen_n(n),
                                           due=d - timedelta(days=i), **{**base, "attempts": 1, "solved": 1})); n += 1
        conn.execute(T.insert().values(key="later#1", game_uuid="later", box=3, due=d + timedelta(days=2), fen=fen_n(n), **base)); n += 1
        conn.execute(T.insert().values(key="bad#1", status="unsuitable", game_uuid="bad", box=0, attempts=0, solved=0))
        keys = [r.key for r in tx.build_day(conn, d, tx.TacticSettings(per_day=10))]
        again = [r.key for r in tx.build_day(conn, d, tx.TacticSettings(per_day=10))]
    ok &= _check("zehn Aufgaben", len(keys) == 10, str(len(keys)))
    ok &= _check("höchstens zwei aus derselben Partie", sum(k.startswith("g1#") for k in keys) == 2)
    ok &= _check("fällige Wiederholungen dabei, spätere nicht",
                 {"r0#1", "r1#1", "r2#1"} <= set(keys) and "later#1" not in keys)
    ok &= _check("Wiederholung und neu abwechselnd, älteste zuerst",
                 keys[0].startswith("r2#") and not keys[1].startswith("r"))
    ok &= _check("unbrauchbare nie, Auswahl bleibt fest", "bad#1" not in keys and keys == again)
    with eng.begin() as conn:
        ok &= _check("erstes Ergebnis des Tages zählt", tx.record(conn, d, keys[0], False) is True)
        ok &= _check("zweites nicht", tx.record(conn, d, keys[0], True) is False)
        r0 = conn.execute(select(T).where(T.c.key == keys[0])).first()
        st = tx.stats(conn, d)
    ok &= _check("falsch -> zurück auf Stufe 1", r0.box == 1 and r0.due == d + timedelta(days=1) and r0.attempts == 2)
    # Neue bleiben "neu", bis sie beantwortet sind; r2 ist nach dem Fehlversuch erst morgen wieder faellig.
    ok &= _check("Statistik zählt Vorrat",
                 (st["new"], st["learning"], st["due"], st["unsuitable"]) == (12, 4, 2, 1), str(st))

    # Gleiche Stellung aus mehreren Partien: am selben Tag nur einmal.
    fen_a = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"
    fen_b = "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
    fen_c = "rnbqkb1r/pppppppp/5n2/8/3P4/8/PPP1PPPP/RNBQKBNR w KQkq - 1 2"
    eng2 = _temp_db()
    with eng2.begin() as conn:
        for i in range(4):
            conn.execute(T.insert().values(key=f"a{i}#5", priority=5, game_uuid=f"a{i}", box=0,
                                           fen=fen_a.replace(" 2 3", f" {i} {i + 3}"), **base))
        for i in range(2):
            conn.execute(T.insert().values(key=f"b{i}#5", priority=5, game_uuid=f"b{i}", box=1, due=d, fen=fen_b, **base))
        conn.execute(T.insert().values(key="bnew#5", priority=9, game_uuid="bnew", box=0, fen=fen_b, **base))
        conn.execute(T.insert().values(key="clearn#5", priority=0, game_uuid="clearn", box=3,
                                       due=d + timedelta(days=5), fen=fen_c, **base))
        conn.execute(T.insert().values(key="cnew#5", priority=9, game_uuid="cnew", box=0, fen=fen_c, **base))
        for i in range(5):
            conn.execute(T.insert().values(key=f"x{i}#5", priority=1, game_uuid=f"x{i}", box=0, fen=fen_n(i + 1), **base))
        keys = [r.key for r in tx.build_day(conn, d, tx.TacticSettings(per_day=10))]
    ok &= _check("gleiche Stellung aus vier Partien: nur einmal am Tag", sum(k.startswith("a") for k in keys) == 1, str(keys))
    ok &= _check("doppelte Wiederholung und Zwilling: nur einmal", sum(k.startswith("b") for k in keys) == 1)
    ok &= _check("Stellung schon in Wiederholung -> nicht nochmal neu", "cnew#5" not in keys)
    ok &= _check("Rest füllt auf", len(keys) == len(set(keys)) == 7, str(len(keys)))

    # Uebernahme aus dem frueheren Trainer (gleiche Spalten, gleicher Schluessel).
    eng3 = _temp_db()
    with eng3.begin() as conn:
        conn.execute(text("CREATE TABLE trainer_tactic (key VARCHAR(90) PRIMARY KEY, status VARCHAR(12), fen VARCHAR(100), "
                          "solution TEXT, box INTEGER, due DATE, attempts INTEGER, solved INTEGER, extra_col INTEGER)"))
        conn.execute(text("INSERT INTO trainer_tactic VALUES ('lichess:x#7', 'ready', :f, 'a1a8', 3, '2026-10-05', 4, 3, 1)"),
                     {"f": two.fen})
        conn.execute(text("CREATE TABLE trainer_tactic_day (day DATE, pos INTEGER, key VARCHAR(90), result VARCHAR(10), updated_at TIMESTAMP)"))
        conn.execute(text("INSERT INTO trainer_tactic_day VALUES ('2026-09-29', 0, 'lichess:x#7', 'solved', NULL)"))
    first = tx.import_legacy(eng3)
    second = tx.import_legacy(eng3)
    with eng3.connect() as conn:
        moved = conn.execute(select(T).where(T.c.key == "lichess:x#7")).first()
        days = conn.execute(select(D)).all()
    ok &= _check("Trainer-Aufgaben übernommen, samt Lernstand",
                 first == 1 and moved is not None and moved.box == 3 and moved.attempts == 4 and str(moved.due) == "2026-10-05")
    ok &= _check("Tagesauswahl übernommen, zweiter Start ändert nichts", len(days) == 1 and second == 0)
    ok &= _check("ohne Trainer-Tabellen passiert nichts", tx.import_legacy(eng) == 0)

    # Mit Stockfish: echte Vorbereitung. Schaefermatt verpasst -> Qxf7#.
    settings = load_settings()
    sf = tx.Engine(settings.engine_path, 0.2)
    if not sf.available:
        print("  (Stockfish nicht gefunden - Vorbereitung übersprungen)")
        return bool(ok)
    try:
        cand = {"key": "lichess:test0001#7", "ply": 7, "color": "white", "pgn": SCHOLAR_PGN, "category": "blunder",
                "mate_in": 1, "game_uuid": "lichess:test0001"}
        row = tx.prepare(sf, cand, tx.TacticSettings())
        ok &= _check("Aufgabe vorbereitet", row["status"] == "ready", row.get("reason") or "")
        ok &= _check("Lösung Qxf7#", row.get("solution") == "h5f7" and row.get("solution_san") == "4. Qxf7#",
                     str(row.get("solution_san")))
        ok &= _check("Partiezug und Gegnerzug davor", row.get("played_san") == "d3" and row.get("last_move") == "g8f6")
        ok &= _check("Partiezug kostet die Dame", (row.get("win_played") or 100) < 20, str(row.get("win_played")))
        same = tx.prepare(sf, {**cand, "pgn": SCHOLAR_PGN.replace("4. d3 Nxh5", "4. Qxf7#")}, tx.TacticSettings())
        ok &= _check("bester Zug gespielt -> keine Aufgabe", same["status"] == "unsuitable" and same["reason"] == "played_best",
                     str(same.get("reason")))
        # Ganzer Weg: Partie + Zug in der Datenbank -> Vorrat -> Tagesauswahl -> loesen.
        with eng.begin() as conn:
            conn.execute(T.delete())
            conn.execute(D.delete())
            game_id = conn.execute(tx.GAMES.insert().values(
                platform="lichess", uuid="lichess:test0001", url="https://lichess.org/test0001",
                played_at=datetime(2026, 9, 20, 18, 0), time_class="rapid", time_control="600+0", rated=True,
                color="white", result="loss", opponent="gegner", pgn=SCHOLAR_PGN, inaccuracies=0, mistakes=0,
                blunders=1, created_at=datetime(2026, 9, 20, 18, 0),
            )).inserted_primary_key[0]
            conn.execute(tx.MOVES.insert().values(
                game_id=game_id, ply=7, move_number=4, san="d3", phase="opening", cp_before=1000, cp_after=-900,
                cp_loss=1900, win_loss=90.0, category="blunder", error_type="missed_mate", missed_motif="mate",
                mate_in=1, best_move_san="Qxf7#", played_at=datetime(2026, 9, 20, 18, 0), time_class="rapid",
                color="white", platform="lichess",
            ))
        pool = tx.ensure_pool(eng, sf, tx.TacticSettings(), d, 5, 5)
        ok &= _check("Vorrat aus der Datenbank", pool["ready"] == 1 and pool["exhausted"], str(pool))
        with eng.begin() as conn:
            day = tx.build_day(conn, d, tx.TacticSettings())
            trow = conn.execute(select(T).where(T.c.key == "lichess:test0001#7")).first()
        ok &= _check("in der Tagesauswahl", [r.key for r in day] == ["lichess:test0001#7"])
        res = tx.check_move(sf, trow, ["h5f7"], tx.TacticSettings())
        ok &= _check("Qxf7# löst die Aufgabe", res["result"] == "solved")
        res = tx.check_move(sf, trow, ["d2d3"], tx.TacticSettings())
        # Welche Widerlegung kommt, haengt von Engine-Version und Rechenzeit ab:
        # Stockfish 16 (apt in der CI) nennt mal Nxh5, mal Bb4+ - beide gewinnen
        # die Dame. Geprueft wird darum nur, dass eine Widerlegung gezeigt wird.
        ok &= _check("Partiezug wird widerlegt", res["result"] == "wrong" and bool(res.get("punish_san")),
                     str(res.get("punish_san")))
    finally:
        sf.close()
    return bool(ok)


def test_focus() -> bool:
    """Fokus-Karte und Zusammenfuehrung der Eroeffnungsnamen - ohne Engine,
    mit gebauten Partien. Zahlen absichtlich so gewaehlt, dass jeder Punkt
    eindeutig ueber oder unter seiner Schwelle liegt."""
    from datetime import datetime, timedelta

    from . import focus as fx
    from .models import ChessGame, ChessMove
    from .stats import group_openings

    ok = True

    # --- Eroeffnungsnamen: Chess.com-URL-Schreibweise und Lichess zusammen
    def g(name: str, i: int = 0) -> ChessGame:
        return ChessGame(id=i, uuid=f"t:{name}:{i}", played_at=datetime(2026, 1, 1), opening_family=name)
    groups = group_openings([g("Caro-Kann Defense", 1), g("Caro Kann Defense", 2),
                             g("Bishops Opening", 3), g("Bishop's Opening", 4), g("Vienna Game", 5)])
    labels = sorted(label for label, _ in groups.values())
    ok &= _check("Caro-Kann aus beiden Schreibweisen eine Gruppe",
                 len(groups) == 3 and "Caro-Kann Defense" in labels, str(labels))
    ok &= _check("Anzeige nimmt die Schreibweise mit Apostroph", "Bishop's Opening" in labels, str(labels))

    # --- Fokus aus gebauten Partien
    start = datetime(2026, 1, 1)
    games: list[ChessGame] = []
    moves: dict[int, list[ChessMove]] = {}

    def add(i: int, result: str, plan: list[tuple[int, int, Optional[str], Optional[float]]],
            platform: str = "lichess", color: str = "white", opening: str = "Vienna Game") -> None:
        game = ChessGame(id=i, uuid=f"f:{i}", platform=platform, played_at=start + timedelta(hours=i),
                         color=color, result=result, opening_family=opening, analyzed_at=start)
        games.append(game)
        rows = []
        for n, (before, after, etype, clock) in enumerate(plan, start=1):
            loss = max(0.0, win_percent(before) - win_percent(after))
            rows.append(ChessMove(game_id=i, ply=2 * n - 1, move_number=n, san="e4", phase="middlegame",
                                  cp_before=before, cp_after=after, cp_loss=max(0, before - after),
                                  win_loss=round(loss, 2), category="blunder" if loss >= 30 else "ok",
                                  error_type=etype if loss >= 30 else None, clock_seconds=clock,
                                  played_at=game.played_at, platform=platform))
        moves[i] = rows

    # Abwechselnd ueber den Zeitraum verteilt, damit beide Haelften gleich aussehen:
    # 20 Siege, 10 Niederlagen aus Gewinnstellung (Figur eingestellt),
    # 10 Niederlagen aus Ausgleich (Drohung uebersehen), alle mit viel Zeit.
    i = 0
    for k in range(10):
        add(i, "win", [(30, 20, None, 300.0), (400, 600, None, 300.0)]); i += 1
        add(i, "win", [(30, 20, None, 300.0), (400, 600, None, 300.0)]); i += 1
        add(i, "loss", [(30, 400, None, 300.0), (400, -500, "hanging_piece", 300.0)]); i += 1
        add(i, "loss", [(30, 20, None, 300.0), (20, -500, "missed_threat", 300.0)]); i += 1

    result = fx.build(games, moves)
    keys = [item["key"] for item in result["items"]]
    by_key = {item["key"]: item for item in result["items"]}
    ok &= _check("genug Daten erkannt", result["enough"], f"{result['games']} Partien")
    ok &= _check("Gegnerzug uebersehen: alle 20 Kipp-Zuege",
                 by_key.get("threats", {}).get("count") == 20 and by_key["threats"]["share"] == 100.0,
                 str(by_key.get("threats", {}).get("share")))
    ok &= _check("Gewinnstellung: 10 von 20 Niederlagen",
                 by_key.get("conversion", {}).get("count") == 10 and by_key["conversion"]["share"] == 50.0,
                 str(by_key.get("conversion", {}).get("share")))
    ok &= _check("beide stabil ueber die Haelften",
                 all(by_key[k]["stable"] for k in ("threats", "conversion") if k in by_key))
    ok &= _check("Zeitdruck erscheint nicht, wenn er nicht ausschlaegt", "pressure" not in keys, str(keys))
    ok &= _check("Beispiele: hoechstens fuenf, neueste zuerst",
                 len(by_key["threats"]["examples"]) == 5
                 and by_key["threats"]["examples"][0]["played_at"] >= by_key["threats"]["examples"][-1]["played_at"])

    few = fx.build(games[:12], moves)
    ok &= _check("zu wenige Partien: keine Empfehlung", not few["enough"] and not few["items"])

    # Eroeffnung gegen den Schnitt DERSELBEN Plattform: Chess.com 30 Partien mit
    # 50 % bei Plattformschnitt 50 % ist unauffaellig, auch wenn der
    # Gesamtschnitt (mit schwacher Lichess-Bilanz) niedriger liegt.
    base_games: list[ChessGame] = []
    for k in range(30):
        base_games.append(ChessGame(id=1000 + k, uuid=f"c:{k}", platform="chesscom", color="black",
                                    played_at=start, result="win" if k % 2 else "loss",
                                    opening_family="Caro Kann Defense"))
    for k in range(30):
        base_games.append(ChessGame(id=2000 + k, uuid=f"l:{k}", platform="lichess", color="white",
                                    played_at=start, result="loss" if k % 3 else "win",
                                    opening_family="Queen's Pawn Game"))
    base = fx._platform_baselines(base_games)
    _, expected, z = fx._opening_z(base_games[:30], base)
    ok &= _check("Eroeffnung: Erwartung aus der eigenen Plattform", abs(expected - 0.5) < 1e-9 and abs(z) < 0.01,
                 f"Erwartung {expected:.2f}, z {z:.2f}")
    return bool(ok)


def test_duplicates() -> bool:
    """Doppelte Partien: alter Chess.com-Schluessel ohne Praefix neben dem
    neuen. Am 6.10. auf tando 74 Stueck - jede zaehlte in jeder Auswertung
    doppelt."""
    from datetime import date, datetime

    from sqlmodel import Session, select

    from .models import ChessGame, ChessMove, ChessTactic, ChessTacticDay
    from .pipeline import _is_known, merge_duplicate_games

    eng = _temp_db()
    ok = True
    url = "https://www.chess.com/game/daily/1032231630"
    with Session(eng) as s:
        old = ChessGame(platform="chesscom", uuid="3d7874b0", url=url, played_at=datetime(2026, 9, 20),
                        analyzed_at=datetime(2026, 9, 21), analysis_version=3)
        new = ChessGame(platform="chesscom", uuid="chesscom:3d7874b0", url=url, played_at=datetime(2026, 9, 20),
                        analyzed_at=datetime(2026, 9, 23), analysis_version=4)
        solo = ChessGame(platform="chesscom", uuid="aaaa1111", url="https://www.chess.com/game/live/1",
                         played_at=datetime(2026, 9, 1), analyzed_at=datetime(2026, 9, 2), analysis_version=4)
        s.add_all([old, new, solo]); s.commit()
        for g in (old, new):
            s.add(ChessMove(game_id=g.id, ply=7, played_at=g.played_at))
        # Lernstand haengt am alten Schluessel, eine leere Kopie am neuen.
        s.add(ChessTactic(key="3d7874b0#7", status="ready", game_uuid="3d7874b0", attempts=3, box=3))
        s.add(ChessTactic(key="chesscom:3d7874b0#7", status="ready", game_uuid="chesscom:3d7874b0"))
        s.add(ChessTactic(key="aaaa1111#9", status="ready", game_uuid="aaaa1111", attempts=1, box=2))
        s.add(ChessTacticDay(day=date(2026, 10, 1), pos=0, key="3d7874b0#7"))
        s.commit()
        new_id = new.id

        first = merge_duplicate_games(s)
        second = merge_duplicate_games(s)
        games = list(s.exec(select(ChessGame)).all())
        uuids = sorted(g.uuid for g in games)
        moves = list(s.exec(select(ChessMove)).all())
        tactics = {t.key: t for t in s.exec(select(ChessTactic)).all()}
        day = s.exec(select(ChessTacticDay)).first()
        ok &= _check("eine Kopie entfernt, ein alter Schluessel umbenannt",
                     first["removed"] == 1 and first["renamed"] == 1, str(first))
        ok &= _check("zweiter Lauf aendert nichts", not any(second.values()), str(second))
        ok &= _check("die weiter analysierte Kopie bleibt", [g.id for g in games if g.url == url] == [new_id])
        ok &= _check("alle Schluessel mit Praefix", uuids == ["chesscom:3d7874b0", "chesscom:aaaa1111"], str(uuids))
        ok &= _check("Zuege der entfernten Kopie weg", len(moves) == 1 and moves[0].game_id == new_id)
        ok &= _check("Lernstand bleibt erhalten (3 Versuche)",
                     tactics.get("chesscom:3d7874b0#7") is not None
                     and tactics["chesscom:3d7874b0#7"].attempts == 3 and "3d7874b0#7" not in tactics,
                     str(sorted(tactics)))
        ok &= _check("Aufgabe ohne Zwilling zieht mit um", "chesscom:aaaa1111#9" in tactics)
        ok &= _check("Tagesauswahl zeigt auf den neuen Schluessel", day is not None and day.key == "chesscom:3d7874b0#7",
                     str(day.key if day else None))
        ok &= _check("Import erkennt die Partie an der URL",
                     _is_known(s, "chesscom:anders", url) and not _is_known(s, "chesscom:anders", "https://x"))
    return bool(ok)


def main() -> int:
    print("=" * 66)
    print("Chess-Analyzer Selbsttest")
    print("=" * 66)

    print("\n1) Hilfsfunktionen")
    helpers_ok = test_helpers()

    print("\n2) Abbildung der Chess.com-Daten")
    mapping_ok = test_mapping()

    print("\n3) Abbildung der Lichess-Daten")
    lichess_ok = test_lichess_mapping()

    print("\n4) Einordnung der Fehlerarten")
    classification_ok = test_classification()

    print("\n5) Verpasste Taktik")
    missed_ok = test_missed()

    print("\n6) Stockfish-Analyse einer ganzen Partie")
    analysis_ok = test_analysis()

    print("\n7) Taktikaufgaben aus eigenen Fehlern")
    tactics_ok = test_tactics()

    print("\n8) Fokus: woran sich das Training lohnt")
    focus_ok = test_focus()

    print("\n9) Doppelte Partien zusammenfuehren")
    dup_ok = test_duplicates()

    print("\n" + "=" * 66)
    if (helpers_ok and mapping_ok and lichess_ok and classification_ok
            and missed_ok and analysis_ok and tactics_ok and focus_ok and dup_ok):
        print("Alles in Ordnung.")
        return 0
    print("Mindestens ein Test ist fehlgeschlagen (siehe oben).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
