"""Selbsttest ohne Netz und ohne Datenbank.

Auf dem Server aufrufen mit:

    docker compose exec chess-analyzer python -m app.selftest

Geprueft wird:
  1. Hilfsfunktionen (Bewertungskurve, Zeitkontrolle, Spielphasen).
  2. Die Abbildung eines Chess.com-API-Eintrags auf das Datenmodell.
  3. Die Einordnung der Fehlerarten an konstruierten Stellungen.
  4. Stockfish laesst sich starten und analysiert eine ganze Partie korrekt.

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
    except Exception as exc:  # noqa: BLE001
        _check("Stockfish gestartet", False, str(exc))
        return False

    ok = True
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

    print("\n" + "=" * 66)
    if (helpers_ok and mapping_ok and lichess_ok and classification_ok
            and missed_ok and analysis_ok):
        print("Alles in Ordnung.")
        return 0
    print("Mindestens ein Test ist fehlgeschlagen (siehe oben).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
