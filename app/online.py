"""Knightmare online - one server, many players.

Start with ``uvicorn app.online:app``. Anyone can type a Lichess or Chess.com
username (or upload a PGN file) and gets the usual Knightmare analysis of
their most recent games, without installing anything.

How it fits together:

* Every player gets an own small SQLite file under ``ONLINE_DATA_DIR``. The
  regular app (``app.main``) is reused unchanged: requests below
  ``/<platform>/<name>/`` are handed to it after switching database and
  settings for the duration of the request (``db.use_engine``,
  ``config.use_settings``). Because the page only uses relative paths, it
  works under any prefix.
* One worker thread works through a queue, one player at a time: fetch the
  newest games, keep only the newest ``ONLINE_GAMES``, analyse them.
* Limits keep a small server alive: games per player, queue length, new jobs
  per IP address and hour, and players not visited for ``ONLINE_KEEP_DAYS``
  are deleted. IP addresses are only held in memory for the rate limit.

Nothing here needs an account - only public games are fetched.
"""

from __future__ import annotations

import dataclasses
import html
import logging
import os
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

# Defaults for the online service. They must be in place before app.main is
# imported, because that module reads its settings at import time.
_DATA_DIR = Path(os.getenv("ONLINE_DATA_DIR", "./data/online")).resolve()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DATA_DIR / '_unused.db'}")
os.environ.setdefault("CHESS_TACTICS", "0")
os.environ.setdefault("CHESS_AUTO_SYNC", "0")

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from sqlalchemy import Column, Date, DateTime, Integer, MetaData, String, Table, create_engine, select, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from . import __version__, pipeline  # noqa: E402
from .config import (  # noqa: E402
    PLATFORM_CHESSCOM,
    PLATFORM_LICHESS,
    PLATFORM_PGN,
    USERNAME_PATTERN,
    Settings,
    load_settings,
    use_settings,
)
from .db import init_db, make_sqlite_engine, new_session, use_engine  # noqa: E402
from .models import ChessGame, utc_now  # noqa: E402

log = logging.getLogger("knightmare.online")

WEB_DIR = Path(__file__).parent / "web"
KINDS = (PLATFORM_LICHESS, PLATFORM_CHESSCOM, PLATFORM_PGN)
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{12,40}$")
PATH_PATTERN = re.compile(r"^/(lichess|chesscom|pgn)/([A-Za-z0-9_-]{2,40})(/.*)?$")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclasses.dataclass(frozen=True)
class OnlineConfig:
    data_dir: Path
    games: int
    keep_days: int
    refresh_hours: int
    queue_max: int
    per_ip_hour: int
    backfill_months: int
    seconds_per_game: int
    contact: str
    operator: str

    @classmethod
    def from_env(cls) -> "OnlineConfig":
        return cls(
            data_dir=Path(os.getenv("ONLINE_DATA_DIR", str(_DATA_DIR))).resolve(),
            games=max(5, min(200, _int("ONLINE_GAMES", 30))),
            keep_days=max(1, _int("ONLINE_KEEP_DAYS", 30)),
            refresh_hours=max(1, _int("ONLINE_REFRESH_HOURS", 6)),
            queue_max=max(1, _int("ONLINE_QUEUE_MAX", 100)),
            per_ip_hour=max(1, _int("ONLINE_PER_IP_HOUR", 6)),
            backfill_months=max(1, _int("ONLINE_BACKFILL_MONTHS", 12)),
            seconds_per_game=max(1, _int("ONLINE_SECONDS_PER_GAME", 12)),
            contact=os.getenv("ONLINE_CONTACT", "").strip(),
            operator=os.getenv("ONLINE_OPERATOR", "").strip(),
        )


# --------------------------------------------------------------------------
# Registry: who is there, in which state
# --------------------------------------------------------------------------
_meta = MetaData()
players = Table(
    "online_player",
    _meta,
    Column("slug", String(80), primary_key=True),  # "lichess/abetterdodo"
    Column("kind", String(12), nullable=False),
    Column("name", String(60), nullable=False),  # as typed (for display)
    Column("status", String(12), nullable=False),  # queued|running|ready|error
    Column("phase", String(60), nullable=True),
    Column("message", String(400), nullable=True),
    Column("games", Integer, nullable=False, default=0),
    Column("analyzed", Integer, nullable=False, default=0),
    Column("queued_at", DateTime, nullable=True),
    Column("finished_at", DateTime, nullable=True),
    Column("viewed_at", DateTime, nullable=True),
    Column("created_at", DateTime, nullable=False),
)

# Counters per day, nothing else: no IP, no name, no cookie. For the operator's
# dashboard (GET /api/stats). Metrics: visit (start page), new_lichess /
# new_chesscom / new_pgn (first analysis of a player), refresh, result (a
# result page opened), done / failed (finished jobs), games (games analysed).
daily = Table(
    "online_daily",
    _meta,
    Column("day", Date, primary_key=True),
    Column("metric", String(24), primary_key=True),
    Column("n", Integer, nullable=False, default=0),
)
STAT_METRICS = ("visit", "new_lichess", "new_chesscom", "new_pgn", "refresh", "result", "done", "failed", "games")


class Registry:
    def __init__(self, cfg: OnlineConfig) -> None:
        self.cfg = cfg
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = create_engine(
            f"sqlite:///{cfg.data_dir / 'registry.db'}",
            connect_args={"check_same_thread": False},
        )
        _meta.create_all(self.db)
        self._engines: "OrderedDict[str, Engine]" = OrderedDict()
        self._lock = threading.Lock()

    # -- player databases ---------------------------------------------------
    def db_path(self, slug: str) -> Path:
        kind, name = slug.split("/", 1)
        return self.cfg.data_dir / "players" / kind / f"{name.lower()}.db"

    def engine_for(self, slug: str) -> Engine:
        with self._lock:
            eng = self._engines.get(slug)
            if eng is not None:
                self._engines.move_to_end(slug)
                return eng
            eng = make_sqlite_engine(self.db_path(slug))
            init_db(eng)
            self._engines[slug] = eng
            while len(self._engines) > 64:
                _, old = self._engines.popitem(last=False)
                old.dispose()
            return eng

    def drop(self, slug: str) -> None:
        with self._lock:
            eng = self._engines.pop(slug, None)
        if eng is not None:
            eng.dispose()
        path = self.db_path(slug)
        for suffix in ("", "-journal", "-wal", "-shm"):
            try:
                Path(str(path) + suffix).unlink()
            except FileNotFoundError:
                pass
        with self.db.begin() as conn:
            conn.execute(players.delete().where(players.c.slug == slug))

    # -- rows ---------------------------------------------------------------
    def get(self, slug: str) -> Optional[dict[str, Any]]:
        with self.db.connect() as conn:
            row = conn.execute(select(players).where(players.c.slug == slug)).first()
        return dict(row._mapping) if row else None

    def update(self, slug: str, **values: Any) -> None:
        with self.db.begin() as conn:
            conn.execute(players.update().where(players.c.slug == slug).values(**values))

    def enqueue(self, slug: str, kind: str, name: str) -> None:
        now = utc_now()
        with self.db.begin() as conn:
            exists = conn.execute(select(players.c.slug).where(players.c.slug == slug)).first()
            if exists:
                conn.execute(players.update().where(players.c.slug == slug).values(
                    status="queued", phase=None, message=None, queued_at=now, viewed_at=now))
            else:
                conn.execute(players.insert().values(
                    slug=slug, kind=kind, name=name, status="queued", games=0, analyzed=0,
                    queued_at=now, viewed_at=now, created_at=now))

    def queued(self) -> list[str]:
        with self.db.connect() as conn:
            rows = conn.execute(
                select(players.c.slug).where(players.c.status == "queued")
                .order_by(players.c.queued_at)
            ).all()
        return [r[0] for r in rows]

    def position(self, slug: str) -> int:
        """0 = running or not queued, 1 = next, ..."""
        order = self.queued()
        return order.index(slug) + 1 if slug in order else 0

    def touch(self, slug: str) -> None:
        self.update(slug, viewed_at=utc_now())

    def reset_interrupted(self) -> None:
        with self.db.begin() as conn:
            conn.execute(players.update().where(players.c.status == "running")
                         .values(status="queued", phase=None))

    def count(self, metric: str, n: int = 1) -> None:
        """Adds n to today's counter. Local date (TZ of the container)."""
        if n <= 0:
            return
        try:
            with self.db.begin() as conn:
                conn.execute(text(
                    "INSERT INTO online_daily (day, metric, n) VALUES (:d, :m, :n) "
                    "ON CONFLICT(day, metric) DO UPDATE SET n = n + :n"),
                    {"d": datetime.now().date(), "m": metric, "n": n})
        except Exception as exc:  # noqa: BLE001 - counting must never break a request
            log.warning("Counter %s: %s", metric, exc)

    def stats(self, days: int) -> dict[str, Any]:
        start = datetime.now().date() - timedelta(days=days - 1)
        with self.db.connect() as conn:
            rows = conn.execute(select(daily).where(daily.c.day >= start)).all()
            total_players = conn.execute(text("SELECT count(*) FROM online_player")).scalar() or 0
            by_kind = dict(conn.execute(text(
                "SELECT kind, count(*) FROM online_player GROUP BY kind")).all())
        per_day: dict[str, dict[str, int]] = {}
        for r in rows:
            per_day.setdefault(r.day.isoformat(), {})[r.metric] = r.n
        out_days = []
        for i in range(days):
            d = (start + timedelta(days=i)).isoformat()
            out_days.append({"day": d, **{m: per_day.get(d, {}).get(m, 0) for m in STAT_METRICS}})
        totals = {m: sum(x[m] for x in out_days) for m in STAT_METRICS}
        return {"days": out_days, "totals": totals, "players": total_players, "players_by_kind": by_kind}

    def expired(self) -> list[str]:
        cutoff = utc_now() - timedelta(days=self.cfg.keep_days)
        with self.db.connect() as conn:
            rows = conn.execute(
                select(players.c.slug).where(players.c.viewed_at < cutoff)
                .where(players.c.status != "running")
            ).all()
        return [r[0] for r in rows]


# --------------------------------------------------------------------------
# Settings per player
# --------------------------------------------------------------------------
def player_settings(base: Settings, cfg: OnlineConfig, kind: str, name: str) -> Settings:
    return dataclasses.replace(
        base,
        chesscom_username=name if kind == PLATFORM_CHESSCOM else "",
        lichess_username=name if kind == PLATFORM_LICHESS else "",
        backfill_months=cfg.backfill_months,
        incremental_archives=1,
        fetch_max_games=cfg.games,
        max_games_per_run=cfg.games,
        auto_sync=False,
        tactics_enabled=False,
        online_mode=True,
    )


# --------------------------------------------------------------------------
# Worker: one job at a time
# --------------------------------------------------------------------------
_ANALYSING = re.compile(r"analysiere (\d+)/(\d+)")


class Worker:
    def __init__(self, registry: Registry, base: Settings) -> None:
        self.reg = registry
        self.base = base
        self.current: Optional[str] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_cleanup = 0.0

    def start(self) -> None:
        self.reg.reset_interrupted()
        self._thread = threading.Thread(target=self._loop, name="online-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def nudge(self) -> None:
        self._wake.set()

    def progress(self) -> Optional[tuple[int, int]]:
        match = _ANALYSING.search(str(pipeline.current_status().get("phase") or ""))
        return (int(match.group(1)), int(match.group(2))) if match else None

    def _loop(self) -> None:
        while not self._stop.is_set():
            if time.monotonic() - self._last_cleanup > 3600:
                self._last_cleanup = time.monotonic()
                self.cleanup()
            order = self.reg.queued()
            if not order:
                self._wake.wait(5)
                self._wake.clear()
                continue
            self.run(order[0])

    def cleanup(self) -> int:
        removed = 0
        for slug in self.reg.expired():
            try:
                self.reg.drop(slug)
                removed += 1
            except Exception as exc:  # noqa: BLE001
                log.error("Could not remove %s: %s", slug, exc)
        if removed:
            log.info("Removed %d players not visited for %d days", removed, self.reg.cfg.keep_days)
        return removed

    def run(self, slug: str) -> None:
        row = self.reg.get(slug)
        if row is None:
            return
        kind, name = row["kind"], row["name"]
        cfg = self.reg.cfg
        self.current = slug
        self.reg.update(slug, status="running", phase="fetch", message=None)
        settings = player_settings(self.base, cfg, kind, name)
        problems: list[str] = []
        try:
            with use_engine(self.reg.engine_for(slug)), use_settings(settings):
                session = new_session()
                try:
                    if kind in (PLATFORM_LICHESS, PLATFORM_CHESSCOM):
                        fetched = pipeline.sync_games(session, settings)
                        problems = list(fetched.get("problems") or [])
                    pipeline.keep_newest(session, cfg.games)
                    self.reg.update(slug, phase="analyse")
                    result = pipeline.analyse_pending(session, settings, limit=cfg.games)
                    self.reg.count("games", int(result.get("analyzed") or 0))
                    total = len(session.exec(select(ChessGame.id)).all())
                    done = len(session.exec(
                        select(ChessGame.id).where(ChessGame.analyzed_at.is_not(None))  # type: ignore[union-attr]
                    ).all())
                finally:
                    session.close()
            if total == 0:
                message = "; ".join(problems) or "no_games"
                self.reg.update(slug, status="error", phase=None, message=message[:400],
                                games=0, analyzed=0, finished_at=utc_now())
                self.reg.count("failed")
            else:
                self.reg.update(slug, status="ready", phase=None,
                                message=("; ".join(problems)[:400] or None),
                                games=total, analyzed=done, finished_at=utc_now())
                self.reg.count("done")
            log.info("%s: %d games, %d analysed", slug, total, done if total else 0)
        except Exception as exc:  # noqa: BLE001 - one player must not stop the queue
            log.exception("Job %s failed", slug)
            self.reg.update(slug, status="error", phase=None, message=str(exc)[:400],
                            finished_at=utc_now())
            self.reg.count("failed")
        finally:
            self.current = None


# --------------------------------------------------------------------------
# Rate limit (memory only - IP addresses are never written anywhere)
# --------------------------------------------------------------------------
class RateLimit:
    def __init__(self, per_hour: int) -> None:
        self.per_hour = per_hour
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] > 3600:
                hits.popleft()
            if len(hits) >= self.per_hour:
                return False
            hits.append(now)
            if len(self._hits) > 10000:  # keep memory bounded
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]
            return True


# --------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------
cfg = OnlineConfig.from_env()
registry = Registry(cfg)
worker = Worker(registry, load_settings())
limiter = RateLimit(cfg.per_ip_hour)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if os.getenv("ONLINE_WORKER", "1") != "0":
        worker.start()
    yield
    worker.stop()


site = FastAPI(title="Knightmare online", version=__version__, lifespan=lifespan,
               docs_url=None, redoc_url=None, openapi_url=None)


def _slug(kind: str, name: str) -> str:
    return f"{kind}/{name.lower()}"


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def job_state(slug: str) -> dict[str, Any]:
    row = registry.get(slug)
    if row is None:
        return {"slug": slug, "status": "unknown"}
    state: dict[str, Any] = {
        "slug": slug,
        "url": f"/{slug}/",
        "name": row["name"],
        "kind": row["kind"],
        "status": row["status"],
        "phase": row["phase"],
        "message": row["message"],
        "games": row["games"],
        "analyzed": row["analyzed"],
        "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
    }
    spg = cfg.seconds_per_game
    if row["status"] == "queued":
        pos = registry.position(slug)
        running = 1 if worker.current else 0
        state["position"] = pos
        state["eta_seconds"] = (pos - 1 + running) * cfg.games * spg + cfg.games * spg
    elif row["status"] == "running":
        prog = worker.progress() if row["phase"] == "analyse" else None
        if prog:
            state["progress"] = {"done": prog[0] - 1, "total": prog[1]}
            state["eta_seconds"] = (prog[1] - prog[0] + 1) * spg
        else:
            state["eta_seconds"] = cfg.games * spg
    return state


class AnalyseBody(BaseModel):
    platform: str = Field(max_length=12)
    username: str = Field(max_length=40)


@site.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "knightmare-online", "version": __version__,
            "queued": len(registry.queued()), "running": worker.current}


@site.get("/api/stats")
def stats(days: int = 14) -> JSONResponse:
    """Daily totals for the operator's dashboard. Only counts, no personal
    data - readable from another origin (the dashboard runs elsewhere)."""
    data = registry.stats(max(1, min(90, days)))
    data.update(version=__version__, queued=len(registry.queued()), running=worker.current is not None,
                games_per_player=cfg.games)
    return JSONResponse(data, headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})


@site.get("/api/config")
def config() -> dict[str, Any]:
    return {"games": cfg.games, "keep_days": cfg.keep_days, "version": __version__,
            "seconds_per_game": cfg.seconds_per_game}


@site.post("/api/analyse")
def analyse(body: AnalyseBody, request: Request) -> dict[str, Any]:
    kind = body.platform.strip().lower()
    name = body.username.strip()
    if kind not in (PLATFORM_LICHESS, PLATFORM_CHESSCOM):
        raise HTTPException(400, "bad_platform")
    if not USERNAME_PATTERN.match(name):
        raise HTTPException(400, "bad_username")
    slug = _slug(kind, name)
    row = registry.get(slug)
    if row and row["status"] in ("queued", "running"):
        return job_state(slug)
    if row and row["status"] == "ready" and row["finished_at"] and \
            utc_now() - row["finished_at"] < timedelta(hours=cfg.refresh_hours):
        registry.touch(slug)
        return job_state(slug)
    if row and row["analyzed"]:
        # Results exist, only older than ONLINE_REFRESH_HOURS: show them right
        # away and fetch new games in the background - if there is room.
        registry.touch(slug)
        if len(registry.queued()) < cfg.queue_max and limiter.allow(_client_ip(request)):
            registry.enqueue(slug, kind, row["name"])
            registry.count("refresh")
            worker.nudge()
        return job_state(slug)
    if len(registry.queued()) >= cfg.queue_max:
        raise HTTPException(503, "queue_full")
    if not limiter.allow(_client_ip(request)):
        raise HTTPException(429, "too_many")
    if row is None:
        from .main import _account_exists  # the same check the desktop setup uses

        if _account_exists(kind, name) is False:
            raise HTTPException(404, "not_found")
    registry.enqueue(slug, kind, row["name"] if row else name)
    registry.count(f"new_{kind}" if row is None else "refresh")
    worker.nudge()
    return job_state(slug)


@site.get("/api/job/{kind}/{name}")
def job(kind: str, name: str) -> dict[str, Any]:
    if kind not in KINDS or not re.match(r"^[A-Za-z0-9_-]{2,40}$", name):
        raise HTTPException(404, "unknown")
    state = job_state(_slug(kind, name) if kind != PLATFORM_PGN else f"pgn/{name}")
    if state["status"] == "unknown":
        raise HTTPException(404, "unknown")
    return state


@site.post("/api/upload")
async def upload(request: Request, file: UploadFile = File(...), name: str = Form("")) -> dict[str, Any]:
    raw = await file.read(pipeline.PGN_MAX_BYTES + 1)
    if len(raw) > pipeline.PGN_MAX_BYTES:
        raise HTTPException(413, "too_big")
    text = raw.decode("utf-8", errors="replace")
    if "[" not in text:
        raise HTTPException(400, "not_pgn")
    name = name.strip()[:60]
    if not name:
        found = pipeline.pgn_players(text)
        if not found:
            raise HTTPException(400, "not_pgn")
        return {"need_name": True, "players": list(found)[:12]}
    if len(registry.queued()) >= cfg.queue_max:
        raise HTTPException(503, "queue_full")
    if not limiter.allow(_client_ip(request)):
        raise HTTPException(429, "too_many")
    token = secrets.token_urlsafe(12).replace("-", "x").replace("_", "y")
    slug = f"{PLATFORM_PGN}/{token}"
    with use_engine(registry.engine_for(slug)):
        session = new_session()
        try:
            result = pipeline.ingest_pgn(session, text, name, max_games=cfg.games)
        finally:
            session.close()
    if not result["added"]:
        registry.drop(slug)
        raise HTTPException(400, "no_games_for_name")
    registry.enqueue(slug, PLATFORM_PGN, name)
    registry.count("new_pgn")
    worker.nudge()
    return job_state(slug)


def _page(name: str) -> FileResponse:
    return FileResponse(WEB_DIR / name, media_type="text/html",
                        headers={"Cache-Control": "no-cache, must-revalidate"})


@site.get("/", include_in_schema=False)
def landing(request: Request) -> FileResponse:
    if request.method == "GET":
        registry.count("visit")
    return _page("online.html")


@site.get("/privacy", include_in_schema=False)
def privacy() -> HTMLResponse:
    template = (WEB_DIR / "privacy.html").read_text(encoding="utf-8")
    operator = html.escape(cfg.operator or "the operator of this site")
    contact = html.escape(cfg.contact)
    contact_html = f'<a href="mailto:{contact}">{contact}</a>' if "@" in contact else (contact or "–")
    page = (template.replace("{{OPERATOR}}", operator)
            .replace("{{CONTACT}}", contact_html)
            .replace("{{KEEP_DAYS}}", str(cfg.keep_days))
            .replace("{{GAMES}}", str(cfg.games)))
    return HTMLResponse(page, headers={"Cache-Control": "no-cache"})


@site.get("/assets/{name}", include_in_schema=False)
def assets(name: str) -> FileResponse:
    path = (WEB_DIR / "assets" / name).resolve()
    if path.parent != (WEB_DIR / "assets").resolve() or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "max-age=86400"})


# --------------------------------------------------------------------------
# /<platform>/<name>/... -> the regular Knightmare app, switched to that player
# --------------------------------------------------------------------------
_main_app = None
_base_settings = load_settings()


def _regular_app():
    global _main_app
    if _main_app is None:
        from .main import app as regular

        _main_app = regular
    return _main_app


class Dispatcher:
    """ASGI entry point: player pages go to the regular app, the rest to `site`."""

    def __init__(self, fallback) -> None:
        self.fallback = fallback

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.fallback(scope, receive, send)
            return
        match = PATH_PATTERN.match(scope.get("path") or "")
        if not match or match.group(2) in ("api",):
            await self.fallback(scope, receive, send)
            return
        kind, name, rest = match.group(1), match.group(2), match.group(3)
        if kind == PLATFORM_PGN:
            if not TOKEN_PATTERN.match(name):
                await JSONResponse({"detail": "unknown"}, 404)(scope, receive, send)
                return
            slug = f"pgn/{name}"
        else:
            slug = _slug(kind, name)
        prefix = f"/{kind}/{name}"
        row = registry.get(slug)

        if rest is None:
            await RedirectResponse(prefix + "/", 308)(scope, receive, send)
            return
        if row is None or (row["status"] != "ready" and not row["analyzed"]):
            # Not there (yet): the start page fetches or shows the progress.
            await RedirectResponse(f"/?p={kind}/{name}", 302)(scope, receive, send)
            return
        if scope["method"] not in ("GET", "HEAD"):
            await JSONResponse({"detail": "read_only"}, 403)(scope, receive, send)
            return

        if rest == "/":
            if scope["method"] == "GET":
                registry.count("result")
            if not row["viewed_at"] or utc_now() - row["viewed_at"] > timedelta(hours=1):
                registry.touch(slug)

        settings = player_settings(_base_settings, cfg, row["kind"], row["name"])
        if row["kind"] == PLATFORM_PGN:
            settings = dataclasses.replace(settings, chesscom_username="", lichess_username="")
        # ASGI: path stays the full path, root_path names the prefix - the
        # router of the regular app matches on what comes after it.
        inner = dict(scope)
        inner["root_path"] = scope.get("root_path", "") + prefix
        with use_engine(registry.engine_for(slug)), use_settings(settings):
            await _regular_app()(inner, receive, send)


# Lifespan and everything that is not a player page goes to `site`.
app = Dispatcher(site)
