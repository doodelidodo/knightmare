from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateColumn
from sqlmodel import Session, SQLModel, create_engine

from .config import load_settings

log = logging.getLogger(__name__)

DATABASE_URL = load_settings().database_url

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

if DATABASE_URL.startswith("sqlite:///"):
    # Fuer den lokalen Betrieb ohne Postgres: Verzeichnis anlegen, sonst
    # scheitert SQLite beim ersten Schreiben.
    raw_path = DATABASE_URL.replace("sqlite:///", "", 1)
    if raw_path and raw_path != ":memory:":
        Path(raw_path).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args=connect_args,
    pool_pre_ping=not DATABASE_URL.startswith("sqlite"),
)


# --------------------------------------------------------------------------
# Mehrere Spieler auf einem Server (Online-Fassung, app/online.py)
# --------------------------------------------------------------------------
# Normalerweise gibt es genau eine Datenbank. Die Online-Fassung haelt pro
# Spieler eine eigene SQLite-Datei und schaltet fuer die Dauer einer Anfrage
# oder eines Analyse-Auftrags um. Alles, was ueber get_session/new_session/
# current_engine geht, landet dann automatisch in der richtigen Datei - der
# Rest des Codes muss davon nichts wissen.
_engine_override: ContextVar[Optional[Engine]] = ContextVar("knightmare_engine", default=None)


def current_engine() -> Engine:
    return _engine_override.get() or engine


@contextmanager
def use_engine(target: Engine) -> Iterator[Engine]:
    token = _engine_override.set(target)
    try:
        yield target
    finally:
        _engine_override.reset(token)


def make_sqlite_engine(path: Path) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(
        f"sqlite:///{path}", echo=False, connect_args={"check_same_thread": False}
    )


def _add_missing_columns(target: Engine) -> None:
    """Ergaenzt Spalten, die im Modell stehen, aber in der Tabelle fehlen.

    `SQLModel.metadata.create_all` legt nur neue Tabellen an und fasst
    bestehende nicht an. Ohne das hier wuerde eine bereits befuellte Datenbank
    nach einer Modell-Erweiterung bei jeder Abfrage scheitern. Bewusst klein
    gehalten: nur neue, nullbare Spalten und fehlende Indizes - alles andere
    waere ein Fall fuer ein richtiges Migrationswerkzeug.
    """
    inspector = inspect(target)
    existing_tables = set(inspector.get_table_names())

    for table in SQLModel.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue

        present = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            if not column.nullable and column.server_default is None:
                log.warning(
                    "Spalte %s.%s fehlt, ist aber NOT NULL ohne Default - "
                    "bitte von Hand ergaenzen.",
                    table.name,
                    column.name,
                )
                continue
            definition = CreateColumn(column).compile(dialect=target.dialect)
            statement = f"ALTER TABLE {table.name} ADD COLUMN {definition}"
            try:
                with target.begin() as connection:
                    connection.execute(text(statement))
                log.info("Spalte ergaenzt: %s.%s", table.name, column.name)
            except Exception as exc:  # noqa: BLE001 - Start darf daran nicht scheitern
                log.error("Spalte %s.%s liess sich nicht ergaenzen: %s",
                          table.name, column.name, exc)

        for index in table.indexes:
            try:
                with target.begin() as connection:
                    index.create(bind=connection, checkfirst=True)
            except Exception as exc:  # noqa: BLE001
                log.debug("Index %s uebersprungen: %s", index.name, exc)


def init_db(target: Optional[Engine] = None) -> None:
    # Die Modelle muessen importiert sein, damit sie in der Metadata stehen.
    from . import models  # noqa: F401

    target = target or current_engine()
    SQLModel.metadata.create_all(target)
    try:
        _add_missing_columns(target)
    except Exception as exc:  # noqa: BLE001
        log.error("Spaltenabgleich fehlgeschlagen: %s", exc)


def get_session():
    with Session(current_engine()) as session:
        yield session


def new_session() -> Session:
    """Session fuer Hintergrundjobs, die nicht ueber FastAPI-Depends laufen."""
    return Session(current_engine())


__all__ = [
    "engine", "current_engine", "use_engine", "make_sqlite_engine",
    "get_session", "new_session", "init_db", "DATABASE_URL",
]
