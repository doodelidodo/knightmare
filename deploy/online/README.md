# Knightmare online

A public Knightmare for many players: anyone enters a Lichess or Chess.com
username (or uploads a PGN file) and gets the analysis of their newest games.
Same image as the self-hosted version, started as `app.online:app`.

```
cp .env.example .env     # DOMAIN, ONLINE_OPERATOR, ONLINE_CONTACT
docker compose up -d
```

Point the DNS A record of `DOMAIN` at the server first - Caddy fetches the
certificate on the first request.

## How it works

- Every player gets an own SQLite file in the `knightmare_data` volume.
  Pages live under `/lichess/<name>/`, `/chesscom/<name>/` and
  `/pgn/<random>/` (uploads are only reachable with the link).
- One worker analyses one player at a time: fetch the newest games, keep
  `ONLINE_GAMES` of them, run them through Stockfish. About 12 seconds per
  game on two shared vCPUs.
- Visiting again after `ONLINE_REFRESH_HOURS` fetches new games.
- Limits: `ONLINE_PER_IP_HOUR` new analyses per IP address (held in memory
  only), `ONLINE_QUEUE_MAX` queued players, 2 MB per PGN upload.
- Players not visited for `ONLINE_KEEP_DAYS` are deleted, checked hourly.
- Read-only: no re-analysis, no training mode (that stays in the desktop app
  and the self-hosted version).

## Update

```
docker compose pull && docker compose up -d
```

## Look inside

```
docker compose logs -f knightmare
curl -s https://$DOMAIN/health
docker compose exec knightmare python -m app.selftest
```
