<p align="center">
  <img src="app/web/assets/logo.svg" width="72" alt="">
</p>

<h1 align="center">Knightmare</h1>

<p align="center">
  <em>Find the patterns behind your chess mistakes — then train them.</em><br>
  Chess.com and Lichess · runs on your own computer · Mac, Windows or Docker · English and German
</p>

---

Most analysis tools tell you **how often** you blunder. Knightmare tells you
**what kind of mistake** you keep making — and then shows you every single one
of them.

"Blunder rate 3.2 per game" is a number. *"You hung the piece you had just
moved 14 times this month — here are all 14 positions, with the move your
opponent could have punished you with"* is a training session.

## What makes it different

Knightmare groups every mistake by its **motif**, derived from the refutation:
the best reply Stockfish sees in the position right after your move. That reply
is what shows what was actually wrong.

| Category | Detected when | What to work on |
| --- | --- | --- |
| **Allowed mate** | A forced mate against you appears that wasn't there before. | King safety |
| **Missed a forced mate** | A forced mate was there before your move and gone after it. | Looking for mate when you are winning |
| **Walked into a fork** | The refuting piece then attacks two worthwhile targets: your king, something worth more than itself, or an undefended piece of at least minor-piece value. | Tactics |
| **Bad trade** | You captured, the opponent recaptures on the same square, and you come out behind. | Calculating exchanges |
| **Hung a piece** | The refutation captures the very piece you just moved. | Checking the destination square |
| **Missed a threat** | The refutation wins material somewhere else — something was already hanging. | Asking "what does that threaten?" |
| **Missed a win** | You were clearly winning (+200 cp or more) and ended up level, without losing material. | Converting won positions |
| **Positional slip** | Everything else: no material lost, position just worse. | Playing with a plan |

The split between *hung a piece* and *missed a threat* is the one that matters
most in practice: the first is a careless destination square, the second is not
reading the opponent's last move. Different habits, different training.

## The other half: what you left on the board

Every mistake has a second story. Knightmare also runs the engine's **best
move** — the one you did not play — through the same geometry, and tells you
which tactic was sitting there:

| Motif | Detected when |
| --- | --- |
| **Missed a mate** | A forced mate was available. |
| **Missed a fork** | The best move attacked two worthwhile targets at once. |
| **Missed a discovered attack** | The best move vacated a line, and a piece behind it hit something worth at least a minor piece. |
| **Missed a skewer** | Two enemy pieces on one line, the more valuable one in front. |
| **Missed a pin** | Two enemy pieces on one line, the more valuable one behind. |
| **Missed free material** | An undefended piece, or one worth more than the piece taking it. |

This costs no extra engine time — the best move comes back with the evaluation
either way.

A pin only counts with a queen or king behind it and at least a minor piece in
front. That threshold is measured, not guessed: against tens of thousands of
random positions the looser rule tagged nearly one move in nine, which would
have drowned every other motif.

Recorded only for mistakes and blunders, not inaccuracies: below 100
centipawns a "missed skewer" is usually an accident of geometry rather than
something to train. And deliberately nothing beyond the list above — zwischenzug,
deflection over several moves, zugzwang and anything positional cannot be
decided from one reply, and a wrong label is worse than none. You would train
the wrong thing.

## Then train it: your own mistakes as puzzles

Knowing *what* you get wrong is half of it. The **Training** tab turns your
mistakes into daily puzzles: the position from your own game, your move on the
clock, *what would have been better?*

- **Ten a day**, blunders and short mates first, at most two from the same
  game, and never the same position twice on one day (the same opening mistake
  in two games is one puzzle, not two).
- **Only real puzzles.** Every candidate is re-checked by Stockfish with two
  principal variations. It becomes a puzzle only if the best move is clearly
  better than your move *and* clearly better than the second-best move —
  otherwise there is no answer to find, just "anything but what you played".
  Solutions extend over several moves as long as each move is forced, like on
  Lichess.
- **Equal moves count.** Another move that wins just as much (or mates just as
  fast) solves it too. Wrong moves are refuted on the board.
- **Spaced repetition.** Solved at first try: back in 3 days, then 7, 14, 30,
  then learned. Missed or revealed: back tomorrow. Only the first attempt of
  the day counts.
- Hint (motif or mistake type), show the solution, the move you played with
  the winning chance before and after, and a link to the game.

Puzzles are prepared after each run from the mistakes found, about a second of
engine time per candidate, in a separate low-resource Stockfish process.

## See it on the board

Every mistake in every list has a **Show board** link. It opens a small board
right under the line — from your side, with the opponent's last move
highlighted — and three arrows: **red** your move, **green** the better one,
**orange dashed** the reply that would have punished you. The position is
rebuilt from the stored PGN on the fly, so there is nothing extra to store and
nothing that can go stale. The link to the game on Chess.com or Lichess stays
for when you want the whole game.

## And where to start: the focus card

A dozen statistics still leave you guessing which one matters. The card at the
top of the analysis tab picks **at most three points**, ranked by how many
games they actually cost you:

- **Convert won positions** — in how many of your losses you were clearly
  winning at some point (75%+ win chance), and what gave it away.
- **See your opponent's reply** — for every loss, the move where it tipped (your
  biggest drop from a position you were still in). If that was a missed threat,
  a hung piece, a fork or an allowed mate, it counts here.
- **Mind the clock** — only shown when a real share of those moves came with
  under 30 seconds left.
- **One opening** — only with 20+ games, a clear gap (|z| ≥ 2.5), and compared
  with your score *on the same platform*, so an opening you mostly play on your
  weaker site doesn't light up for that reason alone.

Every point has to hold in the older *and* the newer half of your games, or it
is marked "not certain yet". It compares you with yourself only — there is no
"for your rating" here, because there is no honest reference data for that.

The points come from a measurement, not from intuition: `tools/focus_probe.py`
prints all candidate angles for your own database, and only the ones that were
clear and stable on real games made it into the card.

On top of that: which openings cost you points, which phase you fall apart in,
and how your mistakes correlate with the clock — the last one is read from the
`[%clk]` comments both platforms write into the PGN.

## Quick start

### Desktop app (Mac and Windows)

| | Download |
| --- | --- |
| **macOS** (Apple Silicon, macOS 11+) | [Knightmare-macOS.dmg](https://github.com/doodelidodo/knightmare/releases/latest/download/Knightmare-macOS.dmg) |
| **Windows** (10/11, 64-bit) | [Knightmare-Windows-Setup.exe](https://github.com/doodelidodo/knightmare/releases/latest/download/Knightmare-Windows-Setup.exe) |

Install, start, and your browser opens. Enter your Chess.com and/or Lichess
username — that's the whole setup. Stockfish is included; your games and the
analysis stay on your computer. A small window shows that Knightmare is
running; closing it stops it.

The apps are not signed with a paid developer certificate, so the system
warns on the first start:

- **macOS:** drag Knightmare into *Applications*, open it once (it will be
  blocked), then *System Settings → Privacy & Security → Open Anyway*.
- **Windows:** *More info → Run anyway* in the SmartScreen dialog.

Your data lives in `~/Library/Application Support/Knightmare` (macOS) or
`%LOCALAPPDATA%\Knightmare` (Windows) and survives updates and uninstalls.
Intel Macs and Linux: use Docker below.

### Docker

```bash
docker run -d --name knightmare -p 8000:8000 \
  -e CHESSCOM_USERNAME=yourname \
  -e LICHESS_USERNAME=yourname \
  -v knightmare_data:/app/data \
  ghcr.io/doodelidodo/knightmare:latest
```

Either one on its own is fine. With both, you can look at them together or one at a time.

Open <http://localhost:8000>, hit **Fetch & analyse**, and let it work.

Or with compose:

```bash
git clone https://github.com/doodelidodo/knightmare
cd knightmare
cp .env.example .env     # put your username(s) in
docker compose up -d
```

No database to set up — SQLite lives in the mounted volume. Point
`DATABASE_URL` at Postgres if you'd rather.

**The first run takes a while.** A blitz game needs roughly 12 seconds of
engine time, and each run analyses at most 40 games by default, so a few
hundred games catch up over several runs. The scheduler keeps going every six
hours; the status line shows what's left.

## How it works

1. **Fetch** — via the public APIs of Chess.com and Lichess. Neither needs an
   account or a key. Chess.com is read from monthly archives (first run: the
   last `CHESS_BACKFILL_MONTHS` months, later runs the two most recent);
   Lichess streams as NDJSON from the newest game already stored. If one
   platform is down, the other still runs.
2. **Analyse** — every position of the game is evaluated exactly once. The loss
   of a move is the difference between the evaluation before it and the
   evaluation of the resulting position, both from your side. That halves the
   engine time compared to the obvious approach of evaluating "best move" and
   "played move" separately.
3. **Classify** — the engine hands back its best move for free, so grouping the
   mistake by motif costs no extra computation.

Evaluations are clamped to ±1000 centipawns. Without that, every move in a long
since won position looks like a catastrophe.

Phases: endgame once non-pawn, non-king material drops to 6 points or less
(queen 4, rook 2, bishop/knight 1 each); otherwise opening through ply 20, then
middlegame. Endgame beats opening, so an early queen trade isn't a middlegame.

## Configuration

Everything is an environment variable. All you need is a username for at
least one of the two platforms.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CHESSCOM_USERNAME` | – | Your Chess.com username. |
| `LICHESS_USERNAME` | – | Your Lichess username. At least one of the two is required. |
| `LICHESS_TOKEN` | – | Optional. Raises the Lichess rate limit; anonymous access works fine. |
| `CHESS_USER_AGENT` | `knightmare/1.1 (self-hosted)` | Chess.com answers 403 without a usable user agent. |
| `CHESS_BACKFILL_MONTHS` | `6` | How far back on the first run. |
| `CHESS_SYNC_INTERVAL_HOURS` | `6` | How often to look for new games. |
| `CHESS_MAX_GAMES_PER_RUN` | `40` | Cap per run, so the machine isn't busy for hours. |
| `CHESS_ENGINE_MOVETIME` | `0.15` | Seconds per position. More is more accurate and slower. |
| `CHESS_ENGINE_DEPTH` | `0` | Fixed depth instead of time (0 = off). |
| `CHESS_ENGINE_THREADS` / `CHESS_ENGINE_HASH_MB` | `2` / `256` | Stockfish resources. |
| `CHESS_TIME_CLASSES` | all of them | `ultrabullet,bullet,blitz,rapid,classical,daily`. Lichess correspondence is mapped to `daily`. |
| `CHESS_RATED_ONLY` | `1` | Rated games only. |
| `CHESS_ERROR_SCALE` | `winprob` | How a move is graded: `winprob` = win probability lost, `centipawn` = fixed evaluation loss. See below. |
| `CHESS_INACCURACY_WIN` / `CHESS_MISTAKE_WIN` / `CHESS_BLUNDER_WIN` | `10` / `20` / `30` | Thresholds in win-probability points (`winprob` scale). |
| `CHESS_INACCURACY_CP` / `CHESS_MISTAKE_CP` / `CHESS_BLUNDER_CP` | `50` / `100` / `300` | Thresholds in centipawns (`centipawn` scale). |
| `CHESS_TACTICS` | `1` | Daily puzzles from your mistakes (`0` = off). |
| `CHESS_TACTICS_PER_DAY` | `10` | Puzzles per day. |
| `CHESS_TACTICS_SECONDS` | `0.4` | Engine time per position when preparing and checking puzzles. |
| `CHESS_TACTICS_MIN_CP` | `-300` | Skip positions that were already lost before the mistake. |
| `DATABASE_URL` | SQLite in `/app/data` | Postgres works too. |

Changed the scale or a threshold? Just restart. Every move already stores both
its centipawn loss and its win-probability loss, so regrading is a pass over the
database — seconds, no engine.

### Why win probability is the default

A fixed centipawn scale cannot tell a real mistake from noise. Losing 100
centipawns at equality swings the game; losing 100 at +800 changes nothing. On a
sample of 587 "positional slips" graded by centipawns, 73 % cost less than ten
points of win probability and a quarter happened in positions that were already
decided. They were not mistakes worth a category — they were the scale.

One deliberate exception: a **missed forced mate up to mate in 4 always counts
as at least a mistake**, whatever the scale says. Missing mate at +900 costs almost no win
probability, but it is exactly the pattern you want to see. Without the
exception, half of them vanished from the list. Longer mates are graded like
any other move — missing mate in 9 at +900 is not a training topic.

Missed mates are broken down by length — **mate in 1, 2, 3, 4 and 5+** — and the
list can be filtered by it, so a beginner can start with every mate in one they
walked past. Each of those positions gets a second, deeper engine look (one
second instead of 0.15), so the length is the shortest mate, not a detour.
Mates up to three moves are found reliably; four and longer are a lower bound.

The win-probability curve is the one Lichess uses (`k = 0.00368208`), and so are
the default thresholds: 10, 20 and 30 points. Set `CHESS_ERROR_SCALE=centipawn`
if you want the old behaviour.

## API

The UI is just a client. Everything is available under `/api`, with
interactive docs at `/api/docs`.

```
GET  /api/error-types?time_class=rapid       # how often each motif happens
GET  /api/errors?error_type=hanging_piece&sort=cp_loss&limit=50
GET  /api/missed                             # tactics that were there and weren't played
GET  /api/missed/moves?motif=fork&sort=recent
GET  /api/openings?color=black&min_games=3
GET  /api/focus                              # where to focus: at most three points
GET  /api/moves/{id}/board                   # position + arrows for one mistake
GET  /api/phases  /api/time-pressure  /api/report/weekly
POST /api/sync    /api/reanalyze
GET  /api/tactics/today?day=2026-10-01       # today's puzzles (solutions stay on the server)
POST /api/tactics/move   /api/tactics/reveal
```

Every endpoint takes `time_class`, `platform` and `days`, so you can look at
rapid on Lichess alone if that is what you are working on.

## Verify it works

```bash
docker exec knightmare python -m app.selftest
```

Checks that Stockfish starts, analyses a complete game, reads the clock
comments, and — the interesting part — classifies every motif correctly
against purpose-built positions: the seven mistake types, and the six missed
tactics — plus three positions where the answer must be *nothing*, which are
the ones that keep the detection honest. Among them the case that started it:
a forced mate on the board, stalemate played instead. It also prepares a
real puzzle with Stockfish and walks it through the daily selection and the
move check, in a throwaway SQLite file, and builds the focus card from a set
of constructed games. Exit code 0 means everything is fine. The same
test runs on every push in CI.

## Honest limitations

- **The classification is a heuristic, not a verdict.** It gets the common
  cases right and will misread tangled tactics. Good enough for "what should I
  work on", not for judging a single position.
- **0.15 s per position is not a grandmaster opinion.** Fine for patterns over
  hundreds of games; raise `CHESS_ENGINE_MOVETIME` if you want more.
- **Openings are grouped into families.** Lichess names them as
  "Family: Variation", which splits exactly; Chess.com writes them in one
  piece, so there a keyword heuristic decides where the family ends.
- **Variants are skipped** (Chess960, King of the Hill, Bughouse), and daily
  games stay out of the time-pressure view because their clocks run in days.

## Not affiliated with Chess.com or Lichess

Knightmare uses the public
[Chess.com Published-Data API](https://support.chess.com/en/articles/9650547-published-data-api)
and the [Lichess API](https://lichess.org/api), and contains none of their
designs, piece sets, sounds or move classification glyphs. Both names are
trademarks of their respective owners.

## Building the desktop app yourself

```bash
pip install -r requirements.txt -r desktop/requirements.txt
python desktop/fetch_stockfish.py      # official Stockfish binaries
python desktop/build.py                # add --dmg on macOS
```

Or run it straight from source with `python -m app.desktop`. The CI workflow
`.github/workflows/desktop.yml` builds both apps on every `v*` tag, runs the
self-test inside the finished bundle and attaches them to the release.

## License

MIT — see [LICENSE](LICENSE). Do what you like with it.

The desktop apps bundle the unmodified official [Stockfish](https://stockfishchess.org)
binaries, which are licensed under the GPLv3. Their licence text and a link to
the source come with the app (`stockfish/Copying.txt`, `stockfish/SOURCE.txt`).

## Support

If this found a pattern in your games that you hadn't noticed, a coffee is
appreciated: **[ko-fi.com/abetterdodo](https://ko-fi.com/abetterdodo)**

Not required, and the project stays free either way.
