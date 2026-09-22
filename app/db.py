from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import inspect, text
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


def _add_missing_columns() -> None:
    """Ergaenzt Spalten, die im Modell stehen, aber in der Tabelle fehlen.

    `SQLModel.metadata.create_all` legt nur neue Tabellen an und fasst
    bestehende nicht an. Ohne das hier wuerde eine bereits befuellte Datenbank
    nach einer Modell-Erweiterung bei jeder Abfrage scheitern. Bewusst klein
    gehalten: nur neue, nullbare Spalten und fehlende Indizes - alles andere
    waere ein Fall fuer ein richtiges Migrationswerkzeug.
    """
    inspector = inspect(engine)
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
            definition = CreateColumn(column).compile(dialect=engine.dialect)
            statement = f"ALTER TABLE {table.name} ADD COLUMN {definition}"
            try:
                with engine.begin() as connection:
                    connection.execute(text(statement))
                log.info("Spalte ergaenzt: %s.%s", table.name, column.name)
            except Exception as exc:  # noqa: BLE001 - Start darf daran nicht scheitern
                log.error("Spalte %s.%s liess sich nicht ergaenzen: %s",
                          table.name, column.name, exc)

        for index in table.indexes:
            try:
                with engine.begin() as connection:
                    index.create(bind=connection, checkfirst=True)
            except Exception as exc:  # noqa: BLE001
                log.debug("Index %s uebersprungen: %s", index.name, exc)


def init_db() -> None:
    # Die Modelle muessen importiert sein, damit sie in der Metadata stehen.
    from . import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    try:
        _add_missing_columns()
    except Exception as exc:  # noqa: BLE001
        log.error("Spaltenabgleich fehlgeschlagen: %s", exc)


def get_session():
    with Session(engine) as session:
        yield session


def new_session() -> Session:
    """Session fuer Hintergrundjobs, die nicht ueber FastAPI-Depends laufen."""
    return Session(engine)


__all__ = ["engine", "get_session", "new_session", "init_db", "DATABASE_URL"]
