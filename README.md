<p align="center">
  <img src="app/web/assets/logo.svg" width="72" alt="">
</p>

<h1 align="center">Knightmare</h1>

<p align="center">
  <em>Find the patterns behind your chess mistakes.</em><br>
  Self-hosted · one container · English and German
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
| **Walked into a fork** | The refuting piece then attacks two worthwhile targets: your king, something worth more than itself, or an undefended piece of at least minor-piece value. | Tactics |
| **Bad trade** | You captured, the opponent recaptures on the same square, and you come out behind. | Calculating exchanges |
| **Hung a piece** | The refutation captures the very piece you just moved. | Checking the destination square |
| **Missed a threat** | The refutation wins material somewhere else — something was already hanging. | Asking "what does that threaten?" |
| **Missed a win** | You were clearly winning (+200 cp or more) and ended up level, without losing material. | Converting won positions |
| **Positional slip** | Everything else: no material lost, position just worse. | Playing with a plan |

The split between *hung a piece* and *missed a threat* is the one that matters
most in practice: the first is a careless destination square, the second is not
reading the opponent's last move. Different habits, different training.

On top of that: which openings cost you points, which phase you fall apart in,
and how your mistakes correlate with the clock — the last one is read from the
`[%clk]` comments Chess.com writes into the PGN.

## Quick start

```bash
docker run -d --name knightmare -p 8000:8000 \
  -e CHESSCOM_USERNAME=yourname \
  -v knightmare_data:/app/data \
  ghcr.io/doodelidodo/knightmare:latest
```

Open <http://localhost:8000>, hit **Fetch & analyse**, and let it work.

Or with compose:

```bash
git clone https://github.com/doodelidodo/knightmare
cd knightmare
cp .env.example .env     # put your username in
docker compose up -d
```

No database to set up — SQLite lives in the mounted volume. Point
`DATABASE_URL` at Postgres if you'd rather.

**The first run takes a while.** A blitz game needs roughly 12 seconds of
engine time, and each run analyses at most 40 games by default, so a few
hundred games catch up over several runs. The scheduler keeps going every six
hours; the status line shows what's left.

## How it works

1. **Fetch** — via the public Chess.com API, no account or key needed. First
   run pulls the last `CHESS_BACKFILL_MONTHS` months, later runs only the two
   most recent monthly archives.
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

Everything is an environment variable. Only `CHESSCOM_USERNAME` is required.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CHESSCOM_USERNAME` | – | Your Chess.com username. |
| `CHESS_USER_AGENT` | `knightmare/1.0 (self-hosted)` | Chess.com answers 403 without a usable user agent. |
| `CHESS_BACKFILL_MONTHS` | `6` | How far back on the first run. |
| `CHESS_SYNC_INTERVAL_HOURS` | `6` | How often to look for new games. |
| `CHESS_MAX_GAMES_PER_RUN` | `40` | Cap per run, so the machine isn't busy for hours. |
| `CHESS_ENGINE_MOVETIME` | `0.15` | Seconds per position. More is more accurate and slower. |
| `CHESS_ENGINE_DEPTH` | `0` | Fixed depth instead of time (0 = off). |
| `CHESS_ENGINE_THREADS` / `CHESS_ENGINE_HASH_MB` | `2` / `256` | Stockfish resources. |
| `CHESS_TIME_CLASSES` | `bullet,blitz,rapid,daily` | Which game types to fetch. |
| `CHESS_RATED_ONLY` | `1` | Rated games only. |
| `CHESS_INACCURACY_CP` / `CHESS_MISTAKE_CP` / `CHESS_BLUNDER_CP` | `50` / `100` / `300` | Mistake thresholds. |
| `DATABASE_URL` | SQLite in `/app/data` | Postgres works too. |

Changed the thresholds? Hit **Re-analyse** — nothing is re-fetched, only
re-evaluated.

## API

The UI is just a client. Everything is available under `/api`, with
interactive docs at `/api/docs`.

```
GET  /api/error-types?time_class=rapid       # how often each motif happens
GET  /api/errors?error_type=hanging_piece&sort=cp_loss&limit=50
GET  /api/openings?color=black&min_games=3
GET  /api/phases  /api/time-pressure  /api/report/weekly
POST /api/sync    /api/reanalyze
```

Every endpoint takes `time_class` and `days`, so you can look at rapid alone.

## Verify it works

```bash
docker exec knightmare python -m app.selftest
```

Checks that Stockfish starts, analyses a complete game, reads the clock
comments, and — the interesting part — classifies all seven motifs correctly
against purpose-built positions. Exit code 0 means everything is fine. The same
test runs on every push in CI.

## Honest limitations

- **The classification is a heuristic, not a verdict.** It gets the common
  cases right and will misread tangled tactics. Good enough for "what should I
  work on", not for judging a single position.
- **0.15 s per position is not a grandmaster opinion.** Fine for patterns over
  hundreds of games; raise `CHESS_ENGINE_MOVETIME` if you want more.
- **Chess.com only.** Lichess has an open API too, so support is possible —
  contributions welcome.
- **Openings are grouped by the name Chess.com supplies** in the PGN, collapsed
  to the family: "Sicilian Defense" rather than "Sicilian Defense Najdorf
  Variation 6.Be3".
- **Variants are skipped** (Chess960, King of the Hill, Bughouse), and daily
  games stay out of the time-pressure view because their clocks run in days.

## Not affiliated with Chess.com

Knightmare uses the public
[Published-Data API](https://support.chess.com/en/articles/9650547-published-data-api)
and contains none of Chess.com's designs, piece sets, sounds or move
classification glyphs. Chess.com is a trademark of its owners.

## License

MIT — see [LICENSE](LICENSE). Do what you like with it.

## Support

If this found a pattern in your games that you hadn't noticed, a coffee is
appreciated: **[ko-fi.com/abetterdodo](https://ko-fi.com/abetterdodo)**

Not required, and the project stays free either way.
