"""Woran sollte ich arbeiten? - Messung vor einer Empfehlung.

Messwerkzeug, kein Teil der App (Vorlage: positional_probe.py). Bevor
Knightmare eine "Fokus"-Karte bekommt, soll diese Auswertung an echten Daten
zeigen, welche Blickwinkel ueberhaupt deutliche und stabile Unterschiede
liefern. Nur was hier traegt, wird spaeter Empfehlung.

Braucht keine Engine und kein Nachspielen - nur die gespeicherten Zuege.

Sieben Abschnitte:
  1  Kosten je Fehlerart       verlorene Gewinnprozente pro Partie, nicht Anzahl
  2  Kipp-Zug                  welcher Fehler hat verlorene Partien entschieden?
  3  Gewinnstellung verwertet  wie oft wird aus "klar besser" kein Sieg - und woran
  4  Zeitdruck                 Kosten pro Zug nach Restzeit
  5  Spielphase                Kosten pro Zug nach Phase
  6  Verpasste Motive          was stand auf dem Brett (Achse 2)
  7  Eroeffnungen              Ergebnis und Stand nach der Eroeffnung, mit Signifikanz
  +  Stabilitaet               Rangfolge aeltere vs. neuere Haelfte der Partien

Auf tando im Container:
    docker compose exec -T chess python -m tools.focus_probe
    docker compose exec -T chess python -m tools.focus_probe --time-class rapid --platform lichess

Ohne neues Image:
    curl -sL https://raw.githubusercontent.com/doodelidodo/knightmare/main/tools/focus_probe.py \\
      | docker compose exec -T chess python - --time-class rapid
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import Counter, defaultdict
from datetime import timedelta
from typing import Any, Iterable, Optional

from app.analysis import win_percent

# --------------------------------------------------------------------------
# Messparameter. Bewusst vorlaeufig - genau die sollen sich hier bewaehren.
# --------------------------------------------------------------------------
ERROR_CATEGORIES = ("inaccuracy", "mistake", "blunder")
SERIOUS = ("mistake", "blunder")

# Kipp-Zug: groesster eigener Verlust aus einer Stellung, in der man noch
# im Spiel war. Darunter zaehlt ein Zug nicht als "entscheidend".
KIPP_FROM_WIN = 40.0
KIPP_MIN_LOSS = 10.0
# Gewinnstellung: ab hier gilt man als "klar besser".
WINNING_FROM = 75.0
# Eroeffnungen: Mindestzahl Partien, und ab welchem |z| ein Unterschied
# als auffaellig gilt.
OPENING_MIN_GAMES = 8
Z_NOTABLE = 2.0

CLOCK_BUCKETS = (
    ("under_10s", 10.0),
    ("10_30s", 30.0),
    ("30_60s", 60.0),
    ("1_3min", 180.0),
    ("over_3min", float("inf")),
)

SCORE = {"win": 1.0, "draw": 0.5, "loss": 0.0}


# --------------------------------------------------------------------------
# Hilfen
# --------------------------------------------------------------------------
def win_before(move) -> float:
    return win_percent(move.cp_before)


def win_after(move) -> float:
    return win_percent(move.cp_after)


def clock_bucket(seconds: Optional[float]) -> Optional[str]:
    if seconds is None:
        return None
    for name, upper in CLOCK_BUCKETS:
        if seconds < upper:
            return name
    return CLOCK_BUCKETS[-1][0]


def is_timeout(game) -> bool:
    text = (game.termination or "").lower()
    return "time" in text and "abandon" not in text


def error_key(move) -> str:
    return move.error_type or "unclassified"


def pct(part: float, whole: float) -> str:
    return f"{part / whole * 100:5.1f}%" if whole else "   – "


def group_moves(moves: Iterable) -> dict[int, list]:
    grouped: dict[int, list] = defaultdict(list)
    for move in moves:
        grouped[move.game_id].append(move)
    for items in grouped.values():
        items.sort(key=lambda m: m.ply)
    return grouped


# --------------------------------------------------------------------------
# 1  Kosten je Fehlerart
# --------------------------------------------------------------------------
def cost_by_error_type(games: list, moves_by_game: dict[int, list]) -> dict[str, Any]:
    count: Counter = Counter()
    serious: Counter = Counter()
    cost: Counter = Counter()
    for game in games:
        for move in moves_by_game.get(game.id, []):
            if move.category not in ERROR_CATEGORIES:
                continue
            key = error_key(move)
            count[key] += 1
            cost[key] += move.win_loss
            if move.category in SERIOUS:
                serious[key] += 1
    total_cost = sum(cost.values())
    rows = [
        {
            "key": key,
            "count": count[key],
            "serious": serious[key],
            "cost": cost[key],
            "cost_per_game": cost[key] / len(games) if games else 0.0,
            "share": cost[key] / total_cost if total_cost else 0.0,
        }
        for key in count
    ]
    rows.sort(key=lambda r: -r["cost"])
    return {"rows": rows, "total_cost": total_cost, "games": len(games)}


# --------------------------------------------------------------------------
# 2  Kipp-Zug in verlorenen Partien
# --------------------------------------------------------------------------
def tipping_move(moves: list):
    """Groesster eigener Verlust aus einer Stellung >= KIPP_FROM_WIN, oder None."""
    candidates = [m for m in moves if win_before(m) >= KIPP_FROM_WIN]
    if not candidates:
        return None
    worst = max(candidates, key=lambda m: m.win_loss)
    return worst if worst.win_loss >= KIPP_MIN_LOSS else None


def tipping_moves(games: list, moves_by_game: dict[int, list]) -> dict[str, Any]:
    losses = [g for g in games if g.result == "loss"]
    by_type: Counter = Counter()
    by_phase: Counter = Counter()
    by_clock: Counter = Counter()
    never_in_game = 0
    no_single_error = 0
    timeouts = 0
    loss_sizes: list[float] = []
    for game in losses:
        moves = moves_by_game.get(game.id, [])
        if is_timeout(game):
            timeouts += 1
        if not any(win_before(m) >= KIPP_FROM_WIN for m in moves):
            never_in_game += 1
            continue
        move = tipping_move(moves)
        if move is None:
            no_single_error += 1
            continue
        by_type[error_key(move)] += 1
        by_phase[move.phase] += 1
        bucket = clock_bucket(move.clock_seconds)
        if bucket:
            by_clock[bucket] += 1
        loss_sizes.append(move.win_loss)
    return {
        "losses": len(losses),
        "by_type": by_type,
        "by_phase": by_phase,
        "by_clock": by_clock,
        "never_in_game": never_in_game,
        "no_single_error": no_single_error,
        "timeouts": timeouts,
        "median_loss": sorted(loss_sizes)[len(loss_sizes) // 2] if loss_sizes else None,
    }


# --------------------------------------------------------------------------
# 3  Gewinnstellung verwertet?
# --------------------------------------------------------------------------
def conversion(games: list, moves_by_game: dict[int, list]) -> dict[str, Any]:
    reached = 0
    outcome: Counter = Counter()
    culprit: Counter = Counter()
    timeouts = 0
    for game in games:
        moves = moves_by_game.get(game.id, [])
        winning = [m for m in moves if win_before(m) >= WINNING_FROM]
        if not winning:
            continue
        reached += 1
        outcome[game.result] += 1
        if game.result == "win":
            continue
        if is_timeout(game):
            timeouts += 1
        worst = max(winning, key=lambda m: m.win_loss)
        if worst.win_loss >= KIPP_MIN_LOSS:
            culprit[error_key(worst)] += 1
        else:
            culprit["(kein einzelner Fehler)"] += 1
    return {"reached": reached, "outcome": outcome, "culprit": culprit, "timeouts": timeouts}


# --------------------------------------------------------------------------
# 4/5  Kosten pro Zug nach Restzeit bzw. Phase
# --------------------------------------------------------------------------
def cost_by(moves: Iterable, key_fn) -> dict[str, Any]:
    n: Counter = Counter()
    serious: Counter = Counter()
    cost: Counter = Counter()
    for move in moves:
        key = key_fn(move)
        if key is None:
            continue
        n[key] += 1
        if move.category in ERROR_CATEGORIES:
            cost[key] += move.win_loss
        if move.category in SERIOUS:
            serious[key] += 1
    total = sum(cost.values())
    return {"n": n, "serious": serious, "cost": cost, "total_cost": total}


# --------------------------------------------------------------------------
# 6  Verpasste Motive
# --------------------------------------------------------------------------
def missed_motifs(games: list, moves_by_game: dict[int, list]) -> dict[str, Any]:
    count: Counter = Counter()
    cost: Counter = Counter()
    for game in games:
        for move in moves_by_game.get(game.id, []):
            if not move.missed_motif or move.category not in SERIOUS:
                continue
            count[move.missed_motif] += 1
            cost[move.missed_motif] += move.win_loss
    return {"count": count, "cost": cost, "games": len(games)}


# --------------------------------------------------------------------------
# 7  Eroeffnungen
# --------------------------------------------------------------------------
def opening_exit_win(moves: list) -> Optional[float]:
    """Gewinnchance nach dem letzten eigenen Eroeffnungszug."""
    opening = [m for m in moves if m.phase == "opening"]
    return win_after(opening[-1]) if opening else None


def openings(games: list, moves_by_game: dict[int, list]) -> dict[str, Any]:
    if not games:
        return {"rows": [], "overall": None}
    overall = sum(SCORE.get(g.result, 0.5) for g in games) / len(games)
    grouped: dict[tuple[str, str], list] = defaultdict(list)
    for game in games:
        name = game.opening_family or game.opening_name or game.eco or "?"
        grouped[(game.color, name)].append(game)
    rows = []
    for (color, name), items in grouped.items():
        if len(items) < OPENING_MIN_GAMES:
            continue
        score = sum(SCORE.get(g.result, 0.5) for g in items) / len(items)
        p0 = min(max(overall, 0.05), 0.95)
        z = (score - p0) / math.sqrt(p0 * (1 - p0) / len(items))
        exits = [w for w in (opening_exit_win(moves_by_game.get(g.id, [])) for g in items) if w is not None]
        rows.append({
            "color": color,
            "name": name,
            "games": len(items),
            "score": score,
            "z": z,
            "exit_win": sum(exits) / len(exits) if exits else None,
        })
    rows.sort(key=lambda r: r["z"])
    return {"rows": rows, "overall": overall}


# --------------------------------------------------------------------------
# Stabilitaet: aeltere vs. neuere Haelfte
# --------------------------------------------------------------------------
def top_keys(counter: Counter, n: int = 3) -> list[str]:
    return [key for key, _ in counter.most_common(n)]


def stability(games: list, moves_by_game: dict[int, list]) -> dict[str, Any]:
    ordered = sorted(games, key=lambda g: g.played_at)
    half = len(ordered) // 2
    result = {}
    for label, part in (("aeltere Haelfte", ordered[:half]), ("neuere Haelfte", ordered[half:])):
        costs = cost_by_error_type(part, moves_by_game)
        cost_counter = Counter({r["key"]: r["cost"] for r in costs["rows"]})
        kipp = tipping_moves(part, moves_by_game)["by_type"]
        result[label] = {
            "games": len(part),
            "cost_top": top_keys(cost_counter),
            "cost_per_game": {r["key"]: r["cost_per_game"] for r in costs["rows"]},
            "kipp_top": top_keys(kipp),
        }
    return result


# --------------------------------------------------------------------------
# Ausgabe
# --------------------------------------------------------------------------
def report(games: list, moves: list, out=print) -> None:
    moves_by_game = group_moves(moves)
    games = [g for g in games if moves_by_game.get(g.id)]
    if not games:
        out("Keine analysierten Partien im gewaehlten Ausschnitt.")
        return
    results = Counter(g.result for g in games)
    out(f"{len(games)} analysierte Partien  (Siege {results['win']}, Remis {results['draw']}, "
        f"Niederlagen {results['loss']})  ·  {len(moves)} eigene Zuege\n")

    # 1
    data = cost_by_error_type(games, moves_by_game)
    out("1  KOSTEN JE FEHLERART  (verlorene Gewinnprozente, nur Zuege mit Einstufung)")
    out(f"   gesamt {data['total_cost']:.0f} Punkte = {data['total_cost'] / len(games):.1f} pro Partie\n")
    out("   Fehlerart              Anzahl  davon F/P   pro Partie  Anteil Kosten")
    for r in data["rows"]:
        out(f"   {r['key']:22s} {r['count']:6d} {r['serious']:10d} {r['cost_per_game']:11.1f}   {r['share'] * 100:6.1f}%")
    out("")

    # 2
    data = tipping_moves(games, moves_by_game)
    n = data["losses"]
    tipped = sum(data["by_type"].values())
    out(f"2  KIPP-ZUG IN {n} NIEDERLAGEN  (groesster eigener Verlust ab >= {KIPP_FROM_WIN:.0f}% "
        f"Gewinnchance, mind. {KIPP_MIN_LOSS:.0f} Punkte)")
    out(f"   nie im Spiel (nie >= {KIPP_FROM_WIN:.0f}%): {data['never_in_game']}  ·  "
        f"kein einzelner Fehler: {data['no_single_error']}  ·  auf Zeit verloren: "
        f"{data['timeouts']} von {n}")
    if data["median_loss"] is not None:
        out(f"   Median-Verlust des Kipp-Zugs: {data['median_loss']:.0f} Punkte")
    out("   Fehlerart              Partien  Anteil")
    for key, value in data["by_type"].most_common():
        out(f"   {key:22s} {value:7d}  {pct(value, tipped)}")
    out("   Phase:   " + "  ".join(f"{k} {v} ({pct(v, tipped).strip()})" for k, v in data["by_phase"].most_common()))
    if data["by_clock"]:
        clocked = sum(data["by_clock"].values())
        out("   Restzeit: " + "  ".join(
            f"{name} {data['by_clock'][name]} ({pct(data['by_clock'][name], clocked).strip()})"
            for name, _ in CLOCK_BUCKETS if data["by_clock"][name]))
    out("")

    # 3
    data = conversion(games, moves_by_game)
    reached = data["reached"]
    out(f"3  GEWINNSTELLUNG VERWERTET?  (mind. ein eigener Zug ab >= {WINNING_FROM:.0f}%)")
    out(f"   erreicht in {reached} Partien = {pct(reached, len(games)).strip()} aller Partien")
    if reached:
        o = data["outcome"]
        out(f"   davon gewonnen {o['win']} ({pct(o['win'], reached).strip()}), remis {o['draw']}, "
            f"verloren {o['loss']}  ·  auf Zeit: {data['timeouts']}")
        lost = reached - o["win"]
        if lost:
            out("   Woran nicht verwertet (groesster Verlust ab Gewinnstellung):")
            for key, value in data["culprit"].most_common():
                out(f"     {key:22s} {value:5d}  {pct(value, lost)}")
    out("")

    # 4
    clocked = [m for m in moves if m.clock_seconds is not None]
    out(f"4  ZEITDRUCK  ({len(clocked)} Zuege mit Uhr)")
    if clocked:
        data = cost_by(clocked, lambda m: clock_bucket(m.clock_seconds))
        out("   Restzeit     Zuege   F/P pro 100   Kosten pro 100 Zuege   Anteil Kosten")
        for name, _ in CLOCK_BUCKETS:
            k = data["n"][name]
            if not k:
                continue
            out(f"   {name:10s} {k:7d} {data['serious'][name] / k * 100:12.1f} {data['cost'][name] / k * 100:20.0f}   "
                f"{pct(data['cost'][name], data['total_cost'])}")
    out("")

    # 5
    data = cost_by(moves, lambda m: m.phase)
    out("5  SPIELPHASE")
    out("   Phase        Zuege   F/P pro 100   Kosten pro 100 Zuege   Anteil Kosten")
    for name in ("opening", "middlegame", "endgame"):
        k = data["n"][name]
        if not k:
            continue
        out(f"   {name:10s} {k:7d} {data['serious'][name] / k * 100:12.1f} {data['cost'][name] / k * 100:20.0f}   "
            f"{pct(data['cost'][name], data['total_cost'])}")
    out("")

    # 6
    data = missed_motifs(games, moves_by_game)
    out("6  VERPASSTE MOTIVE  (nur Fehler/Patzer, Achse 2)")
    for key, value in data["count"].most_common():
        out(f"   {key:22s} {value:5d}   pro 100 Partien {value / len(games) * 100:5.1f}   "
            f"Kosten pro Partie {data['cost'][key] / len(games):5.1f}")
    if not data["count"]:
        out("   (keine)")
    out("")

    # 7
    data = openings(games, moves_by_game)
    out(f"7  EROEFFNUNGEN  (ab {OPENING_MIN_GAMES} Partien je Farbe; Gesamtschnitt "
        f"{data['overall'] * 100:.0f}%; * = |z| >= {Z_NOTABLE:.0f})")
    out("   Farbe  Partien  Punkte   z      Chance nach Eroeffnung   Eroeffnung")
    for r in data["rows"]:
        mark = "*" if abs(r["z"]) >= Z_NOTABLE else " "
        exit_win = f"{r['exit_win']:.0f}%" if r["exit_win"] is not None else "–"
        out(f"   {r['color']:5s} {r['games']:8d} {r['score'] * 100:6.0f}% {r['z']:+5.1f}{mark}   {exit_win:>8s}"
            f"                 {r['name'][:50]}")
    if not data["rows"]:
        out("   (keine Eroeffnung mit genug Partien)")
    out("")

    # Stabilitaet
    data = stability(games, moves_by_game)
    out("+  STABILITAET  (dieselbe Rangfolge in beiden Haelften = verlaessliche Empfehlung)")
    for label, part in data.items():
        out(f"   {label} ({part['games']} Partien)")
        out("     teuerste Fehlerarten: " + ", ".join(
            f"{k} {part['cost_per_game'][k]:.1f}" for k in part["cost_top"]))
        out("     haeufigster Kipp-Zug: " + (", ".join(part["kipp_top"]) or "–"))


# --------------------------------------------------------------------------
# Laden
# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--time-class", default=None)
    parser.add_argument("--platform", default=None)
    parser.add_argument("--days", type=int, default=None)
    args = parser.parse_args()

    from sqlmodel import Session, select
    from app.db import engine
    from app.models import ChessGame, ChessMove, utc_now

    conditions = [ChessGame.analyzed_at.is_not(None), ChessGame.analysis_error.is_(None)]
    if args.time_class:
        conditions.append(ChessGame.time_class == args.time_class)
    if args.platform:
        conditions.append(ChessGame.platform == args.platform)
    if args.days:
        conditions.append(ChessGame.played_at >= utc_now() - timedelta(days=args.days))

    with Session(engine) as session:
        games = list(session.exec(select(ChessGame).where(*conditions)).all())
        ids = [g.id for g in games]
        moves: list = []
        for start in range(0, len(ids), 500):  # IN-Liste in Portionen
            chunk = ids[start:start + 500]
            moves.extend(session.exec(select(ChessMove).where(ChessMove.game_id.in_(chunk))).all())

    filters = ", ".join(f"{k}={v}" for k, v in vars(args).items() if v) or "alle Partien"
    print(f"Fokus-Messung · {filters}\n")
    report(games, moves)
    return 0


if __name__ == "__main__":
    sys.exit(main())
