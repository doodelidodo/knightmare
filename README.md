<p align="center">
  <img src="app/web/assets/logo.svg" width="72" alt="">
</p>

<h1 align="center">Knightmare</h1>

<p align="center">
  <em>Find the patterns behind your chess mistakes.</em><br>
  Chess.com and Lichess · self-hosted · one container · English and German
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

On top of that: which openings cost you points, which phase you fall apart in,
and how your mistakes correlate with the clock — the last one is read from the
`[%clk]` comments both platforms write into the PGN.

## Quick start

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
GET  /api/phases  /api/time-pressure  /api/report/weekly
POST /api/sync    /api/reanalyze
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
a forced mate on the board, stalemate played instead. Exit code 0 means everything is fine. The same
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

## License

MIT — see [LICENSE](LICENSE). Do what you like with it.

## Support

If this found a pattern in your games that you hadn't noticed, a coffee is
appreciated: **[ko-fi.com/abetterdodo](https://ko-fi.com/abetterdodo)**

Not required, and the project stays free either way.
