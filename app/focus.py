"""Fokus: woran sich das Training am meisten lohnt.

Hervorgegangen aus tools/focus_probe.py. Gemessen am 6.10. an 331 eigenen
Rapid-Partien; uebernommen wurde nur, was dort deutlich und in beiden
Zeithaelften gleich ausfiel:

  conversion  Niederlagen aus klar gewonnener Stellung (42 % aller Niederlagen)
  threats     Kipp-Zug war ein uebersehener Gegnerzug (57 % der Kipp-Zuege) -
              als GRUPPE; die einzelnen Fehlerarten darin tauschten zwischen
              den Haelften die Plaetze, die Summe nicht
  opening     nur bei klarem Befund, gegen den Schnitt derselben Plattform
  pressure    Kipp-Zuege unter Zeitdruck - bei Dominic nicht vorhanden (96 %
              mit mehr als 3 Minuten), fuer andere Spieler aber plausibel;
              erscheint nur, wenn es ausschlaegt

Bewusst nicht: verpasste Motive (zu kleine Zahlen) und Stellungsfehler als
Empfehlung - "spiel positionell besser" ist keine Massnahme. Ihr Anteil an
den Kosten steht nur als Einordnung dabei.

Verglichen wird nur mit sich selbst, nie mit anderen Spielern - dafuer
fehlen Referenzdaten. Jeder Punkt traegt "stable": erfuellt er die Schwelle
in der aelteren UND der neueren Haelfte der Partien?

Alle Schluessel sprachneutral, beschriftet wird in der Oberflaeche.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Iterable, Optional

from sqlmodel import Session, select

from .analysis import win_percent
from .models import ChessGame, ChessMove
from .stats import group_openings, load_games

# Wann ueberhaupt etwas gesagt wird.
MIN_GAMES = 30
MIN_LOSSES = 10

# Kipp-Zug: groesster eigener Verlust aus einer Stellung, in der man noch
# im Spiel war (>= 40 %), und mindestens ein Fehler (10 Punkte).
KIPP_FROM_WIN = 40.0
KIPP_MIN_LOSS = 10.0
# "Klar gewonnen": ab 75 % Gewinnchance, etwa drei Bauern.
WINNING_FROM = 75.0

THREAT_GROUP = ("missed_threat", "hanging_piece", "fork", "allowed_mate")
PRESSURE_SECONDS = 30.0

# Schwellen je Punkt: Anteil und Mindestanzahl.
CONVERSION_MIN_SHARE = 25.0   # % aller Niederlagen
THREATS_MIN_SHARE = 40.0      # % der Kipp-Zuege
PRESSURE_MIN_SHARE = 25.0     # % der Kipp-Zuege mit Uhr
MIN_COUNT = 5

# Eroeffnungen: viele werden gleichzeitig geprueft, eine schlaegt bei
# |z| >= 2 schon zufaellig aus. Darum strenger, mehr Partien, und in beiden
# Haelften in dieselbe Richtung.
OPENING_MIN_GAMES = 20
OPENING_Z = 2.5
OPENING_HALF_Z = 1.0

MAX_ITEMS = 3
EXAMPLES = 5

SCORE = {"win": 1.0, "draw": 0.5, "loss": 0.0}


# --------------------------------------------------------------------------
# Bausteine (rein, ohne Datenbank - so auch im Selbsttest pruefbar)
# --------------------------------------------------------------------------
def _wb(move: ChessMove) -> float:
    return win_percent(move.cp_before)


def tipping_move(moves: list[ChessMove]) -> Optional[ChessMove]:
    """Groesster eigener Verlust ab >= KIPP_FROM_WIN, sofern ein Fehler."""
    candidates = [m for m in moves if _wb(m) >= KIPP_FROM_WIN]
    if not candidates:
        return None
    worst = max(candidates, key=lambda m: m.win_loss)
    return worst if worst.win_loss >= KIPP_MIN_LOSS else None


def slip_from_winning(moves: list[ChessMove]) -> Optional[ChessMove]:
    """Groesster eigener Verlust ab >= WINNING_FROM (auch unter 10 Punkten,
    damit ein Beispiel gezeigt werden kann). None, wenn nie gewonnen stand."""
    winning = [m for m in moves if _wb(m) >= WINNING_FROM]
    if not winning:
        return None
    return max(winning, key=lambda m: m.win_loss)


def _pct(part: int, whole: int) -> Optional[float]:
    return round(part / whole * 100, 1) if whole else None


def _example(move: ChessMove, game: ChessGame) -> dict[str, Any]:
    return {
        "game_url": game.url or None,
        "played_at": game.played_at.isoformat(),
        "platform": game.platform,
        "opponent": game.opponent,
        "result": game.result,
        "move_number": move.move_number,
        "san": move.san,
        "best_move_san": move.best_move_san,
        "error_type": move.error_type,
        "win_before": round(_wb(move)),
        "win_after": round(win_percent(move.cp_after)),
    }


def _examples(pairs: list[tuple[ChessMove, ChessGame]]) -> list[dict[str, Any]]:
    pairs = sorted(pairs, key=lambda pair: pair[1].played_at, reverse=True)
    return [_example(move, game) for move, game in pairs[:EXAMPLES]]


def _halves(games: list[ChessGame]) -> tuple[list[ChessGame], list[ChessGame]]:
    ordered = sorted(games, key=lambda g: g.played_at)
    half = len(ordered) // 2
    return ordered[:half], ordered[half:]


# --------------------------------------------------------------------------
# Die einzelnen Punkte
# --------------------------------------------------------------------------
def conversion_stats(games: list[ChessGame], moves: dict[int, list[ChessMove]]) -> dict[str, Any]:
    losses = [g for g in games if g.result == "loss"]
    reached = 0
    converted = 0
    thrown: list[tuple[ChessMove, ChessGame]] = []
    culprits: Counter[str] = Counter()
    for game in games:
        slip = slip_from_winning(moves.get(game.id, []))
        if slip is None:
            continue
        reached += 1
        if game.result == "win":
            converted += 1
        elif game.result == "loss":
            thrown.append((slip, game))
            if slip.win_loss >= KIPP_MIN_LOSS:
                culprits[slip.error_type or "unclassified"] += 1
            else:
                culprits["no_single_error"] += 1
    return {
        "count": len(thrown),
        "base": len(losses),
        "share": _pct(len(thrown), len(losses)),
        "reached": reached,
        "converted": converted,
        "culprits": [{"key": k, "count": v} for k, v in culprits.most_common()],
        "pairs": thrown,
    }


def tipping_stats(games: list[ChessGame], moves: dict[int, list[ChessMove]]) -> dict[str, Any]:
    tipped: list[tuple[ChessMove, ChessGame]] = []
    for game in games:
        if game.result != "loss":
            continue
        move = tipping_move(moves.get(game.id, []))
        if move is not None:
            tipped.append((move, game))

    threat_pairs = [(m, g) for m, g in tipped if m.error_type in THREAT_GROUP]
    by_type = Counter(m.error_type for m, _ in threat_pairs)

    clocked = [(m, g) for m, g in tipped if m.clock_seconds is not None]
    pressure_pairs = [(m, g) for m, g in clocked if m.clock_seconds < PRESSURE_SECONDS]
    return {
        "threats": {
            "count": len(threat_pairs),
            "base": len(tipped),
            "share": _pct(len(threat_pairs), len(tipped)),
            "by_type": [{"key": k, "count": by_type[k]} for k in THREAT_GROUP if by_type[k]],
            "pairs": threat_pairs,
        },
        "pressure": {
            "count": len(pressure_pairs),
            "base": len(clocked),
            "share": _pct(len(pressure_pairs), len(clocked)),
            "pairs": pressure_pairs,
        },
    }


def _platform_baselines(games: list[ChessGame]) -> dict[str, float]:
    totals: dict[str, list[float]] = {}
    for game in games:
        totals.setdefault(game.platform, []).append(SCORE.get(game.result, 0.5))
    return {p: sum(v) / len(v) for p, v in totals.items()}


def _opening_z(items: list[ChessGame], base: dict[str, float]) -> tuple[float, float, float]:
    """(Ausbeute, Erwartung, z). Erwartung = Schnitt der jeweiligen Plattform,
    je Partie - eine Eroeffnung, die man fast nur auf der schwaecheren
    Plattform spielt, soll nicht deshalb auffallen."""
    n = len(items)
    score = sum(SCORE.get(g.result, 0.5) for g in items) / n
    expected = sum(base.get(g.platform, 0.5) for g in items) / n
    p0 = min(max(expected, 0.05), 0.95)
    z = (score - p0) / math.sqrt(p0 * (1 - p0) / n)
    return score, expected, z


def opening_findings(games: list[ChessGame], moves: dict[int, list[ChessMove]]) -> dict[str, Any]:
    base = _platform_baselines(games)
    older, newer = _halves(games)
    older_ids = {g.id for g in older}
    weak: list[dict[str, Any]] = []
    strong: list[dict[str, Any]] = []
    for color in ("white", "black"):
        of_color = [g for g in games if g.color == color]
        for label, items in group_openings(of_color).values():
            if len(items) < OPENING_MIN_GAMES:
                continue
            score, expected, z = _opening_z(items, base)
            if abs(z) < OPENING_Z:
                continue
            first = [g for g in items if g.id in older_ids]
            second = [g for g in items if g.id not in older_ids]
            half_z = [(_opening_z(part, base)[2] if part else 0.0) for part in (first, second)]
            stable = all((hz <= -OPENING_HALF_Z) if z < 0 else (hz >= OPENING_HALF_Z) for hz in half_z)
            exits = []
            for g in items:
                opening_moves = [m for m in moves.get(g.id, []) if m.phase == "opening"]
                if opening_moves:
                    exits.append(win_percent(opening_moves[-1].cp_after))
            row = {
                "opening": label,
                "color": color,
                "games": len(items),
                "score": round(score * 100, 1),
                "expected": round(expected * 100, 1),
                "z": round(z, 1),
                "older_z": round(half_z[0], 1),
                "newer_z": round(half_z[1], 1),
                "stable": stable,
                "exit_win": round(sum(exits) / len(exits)) if exits else None,
                "impact": round((expected - score) * len(items), 1),
            }
            (weak if z < 0 else strong).append(row)
    weak.sort(key=lambda r: -r["impact"])
    strong.sort(key=lambda r: r["impact"])
    return {"weak": weak, "strong": strong}


def positional_share(games: list[ChessGame], moves: dict[int, list[ChessMove]]) -> Optional[float]:
    total = 0.0
    positional = 0.0
    for game in games:
        for move in moves.get(game.id, []):
            if move.category == "ok":
                continue
            total += move.win_loss
            if move.error_type == "positional":
                positional += move.win_loss
    return round(positional / total * 100, 1) if total else None


# --------------------------------------------------------------------------
# Zusammensetzen
# --------------------------------------------------------------------------
def _passes(share: Optional[float], count: int, min_share: float, min_count: int) -> bool:
    return share is not None and share >= min_share and count >= min_count


def build(games: list[ChessGame], moves: dict[int, list[ChessMove]]) -> dict[str, Any]:
    """Fokus aus bereits geladenen Partien und Zuegen (Zuege je Partie, nach Halbzug)."""
    games = [g for g in games if moves.get(g.id)]
    losses = sum(1 for g in games if g.result == "loss")
    result: dict[str, Any] = {
        "games": len(games),
        "losses": losses,
        "min_games": MIN_GAMES,
        "min_losses": MIN_LOSSES,
        "enough": len(games) >= MIN_GAMES and losses >= MIN_LOSSES,
        "items": [],
        "strength": None,
        "positional_share": None,
    }
    if not result["enough"]:
        return result

    older, newer = _halves(games)
    half_min = max(1, MIN_COUNT // 2)
    items: list[dict[str, Any]] = []

    conv = conversion_stats(games, moves)
    if _passes(conv["share"], conv["count"], CONVERSION_MIN_SHARE, MIN_COUNT):
        halves = [conversion_stats(part, moves) for part in (older, newer)]
        items.append({
            "key": "conversion",
            "count": conv["count"], "base": conv["base"], "share": conv["share"],
            "reached": conv["reached"], "converted": conv["converted"],
            "breakdown": conv["culprits"],
            "older": halves[0]["share"], "newer": halves[1]["share"],
            "stable": all(_passes(h["share"], h["count"], CONVERSION_MIN_SHARE, half_min) for h in halves),
            "impact": conv["count"],
            "examples": _examples(conv["pairs"]),
        })

    tip = tipping_stats(games, moves)
    tip_halves = [tipping_stats(part, moves) for part in (older, newer)]
    for key, min_share in (("threats", THREATS_MIN_SHARE), ("pressure", PRESSURE_MIN_SHARE)):
        data = tip[key]
        if not _passes(data["share"], data["count"], min_share, MIN_COUNT):
            continue
        halves = [h[key] for h in tip_halves]
        items.append({
            "key": key,
            "count": data["count"], "base": data["base"], "share": data["share"],
            "breakdown": data.get("by_type", []),
            "older": halves[0]["share"], "newer": halves[1]["share"],
            "stable": all(_passes(h["share"], h["count"], min_share, half_min) for h in halves),
            "impact": data["count"],
            "examples": _examples(data["pairs"]),
        })

    openings = opening_findings(games, moves)
    if openings["weak"]:
        row = openings["weak"][0]
        items.append({"key": "opening", **row})
    if openings["strong"]:
        result["strength"] = openings["strong"][0]

    # Stabile zuerst, dann nach Wirkung (Partien bzw. verlorene Punkte).
    items.sort(key=lambda item: (not item["stable"], -item["impact"]))
    result["items"] = items[:MAX_ITEMS]
    result["positional_share"] = positional_share(games, moves)
    return result


def _group_moves(moves: Iterable[ChessMove]) -> dict[int, list[ChessMove]]:
    grouped: dict[int, list[ChessMove]] = {}
    for move in moves:
        grouped.setdefault(move.game_id, []).append(move)
    for items in grouped.values():
        items.sort(key=lambda m: m.ply)
    return grouped


def focus(
    session: Session,
    days: Optional[int] = None,
    time_class: Optional[str] = None,
    platform: Optional[str] = None,
) -> dict[str, Any]:
    games = [
        g for g in load_games(session, days=days, time_class=time_class, platform=platform,
                              analyzed_only=True)
        if g.analysis_error is None
    ]
    ids = [g.id for g in games if g.id is not None]
    moves: list[ChessMove] = []
    for start in range(0, len(ids), 500):  # IN-Liste in Portionen
        chunk = ids[start:start + 500]
        moves.extend(session.exec(
            select(ChessMove).where(ChessMove.game_id.in_(chunk))  # type: ignore[union-attr]
        ).all())
    result = build(games, _group_moves(moves))
    result["time_class"] = time_class or "all"
    result["platform"] = platform or "all"
    return result
