"""Datenmodell des Chess-Analyzers.

Die Tabellen liegen in derselben Postgres wie der Rest der Plattform, sind aber
mit dem Praefix "chess_" klar abgegrenzt. Angelegt werden sie beim Start dieses
Services (SQLModel.metadata.create_all) - die integration-platform-api kennt
diese Modelle nicht und fasst sie auch nicht an.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class ChessGame(SQLModel, table=True):
    __tablename__ = "chess_games"

    id: Optional[int] = Field(default=None, primary_key=True)

    # --- Stammdaten der Plattform ---------------------------------------
    # server_default sorgt dafuer, dass bestehende Zeilen beim Ergaenzen der
    # Spalte automatisch als Chess.com markiert werden - alles, was es vor der
    # Lichess-Unterstuetzung gab, kam von dort.
    platform: str = Field(
        default="chesscom",
        index=True,
        max_length=12,
        sa_column_kwargs={"server_default": "chesscom"},
    )
    # Plattform-Praefix im Schluessel, damit sich IDs nie ueberschneiden
    # koennen ("lichess:abcd1234").
    uuid: str = Field(index=True, unique=True, max_length=64)
    url: str = Field(default="", max_length=300)
    played_at: datetime = Field(index=True)
    time_class: str = Field(default="unknown", index=True, max_length=20)
    time_control: str = Field(default="", max_length=30)
    rated: bool = Field(default=True)

    color: str = Field(default="white", index=True, max_length=5)
    result: str = Field(default="unknown", index=True, max_length=10)  # win/loss/draw
    termination: Optional[str] = Field(default=None, max_length=160)
    my_rating: Optional[int] = Field(default=None)
    opponent: str = Field(default="", max_length=60)
    opponent_rating: Optional[int] = Field(default=None)

    eco: Optional[str] = Field(default=None, index=True, max_length=10)
    opening_name: Optional[str] = Field(default=None, max_length=200)
    opening_family: Optional[str] = Field(default=None, index=True, max_length=120)
    opening_url: Optional[str] = Field(default=None, max_length=300)

    # Chess.com liefert seine eigene Genauigkeit nur, wenn der Spieler die
    # Analyse angefordert hat - darum optional.
    accuracy_self: Optional[float] = Field(default=None)
    accuracy_opponent: Optional[float] = Field(default=None)

    move_count: Optional[int] = Field(default=None)
    pgn: str = Field(default="")

    # --- Ergebnisse der eigenen Stockfish-Analyse -----------------------
    analyzed_at: Optional[datetime] = Field(default=None, index=True)
    # Mit welchem Stand der Auswertung. Leer heisst: vor der Einfuehrung
    # dieses Feldes analysiert, also aelter als alles Gezaehlte.
    analysis_version: Optional[int] = Field(default=None, index=True)
    analysis_error: Optional[str] = Field(default=None, max_length=400)
    acpl: Optional[float] = Field(default=None)
    acpl_opening: Optional[float] = Field(default=None)
    acpl_middlegame: Optional[float] = Field(default=None)
    acpl_endgame: Optional[float] = Field(default=None)
    inaccuracies: int = Field(default=0)
    mistakes: int = Field(default=0)
    blunders: int = Field(default=0)
    first_error_ply: Optional[int] = Field(default=None)

    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class ChessMove(SQLModel, table=True):
    """Ein eigener Zug. Gegnerzuege werden bewusst nicht gespeichert."""

    __tablename__ = "chess_moves"

    id: Optional[int] = Field(default=None, primary_key=True)
    game_id: int = Field(index=True, foreign_key="chess_games.id")

    ply: int = Field(default=0)
    move_number: int = Field(default=0)
    san: str = Field(default="", max_length=16)
    phase: str = Field(default="middlegame", index=True, max_length=12)

    cp_before: int = Field(default=0)
    cp_after: int = Field(default=0)
    cp_loss: int = Field(default=0, index=True)
    win_loss: float = Field(default=0.0)
    category: str = Field(default="ok", index=True, max_length=12)
    # Art des Fehlers (Figur eingestellt, Gabel kassiert, ...) - nur gesetzt,
    # wenn category != "ok". Siehe analysis.ERROR_TYPES.
    error_type: Optional[str] = Field(default=None, index=True, max_length=20)
    # Zweite Achse: welches Motiv im besten Zug steckte, den wir nicht
    # gespielt haben. Nullable, damit die Spalte in bestehende Datenbanken
    # nachgezogen werden kann - alte Zuege bleiben schlicht leer.
    missed_motif: Optional[str] = Field(default=None, index=True, max_length=20)
    best_move_san: Optional[str] = Field(default=None, max_length=16)
    # Der Zug, mit dem der Gegner unseren Fehler bestraft haette.
    refutation_san: Optional[str] = Field(default=None, max_length=16)

    # Restzeit nach dem Zug bzw. fuer diesen Zug verbrauchte Zeit (aus den
    # [%clk]-Kommentaren im PGN von Chess.com).
    clock_seconds: Optional[float] = Field(default=None)
    seconds_spent: Optional[float] = Field(default=None)

    # Denormalisiert, damit Auswertungen ueber Zuege ohne Join auf die Partie
    # auskommen.
    played_at: datetime = Field(index=True)
    time_class: str = Field(default="unknown", index=True, max_length=20)
    color: str = Field(default="white", max_length=5)
    platform: str = Field(
        default="chesscom",
        index=True,
        max_length=12,
        sa_column_kwargs={"server_default": "chesscom"},
    )


class ChessSyncState(SQLModel, table=True):
    """Einzelne Zeile (id=1) mit dem Zustand des letzten Laufs.

    Fuer Lichess braucht es hier bewusst keinen Zeiger: wie weit wir sind,
    steht ohnehin in den Partien selbst (juengstes `played_at` dieser
    Plattform) - eine Zustandsvariable weniger, die falsch stehen kann.
    """

    __tablename__ = "chess_sync_state"

    id: Optional[int] = Field(default=1, primary_key=True)
    username: str = Field(default="", max_length=60)
    last_sync_at: Optional[datetime] = Field(default=None)
    last_sync_status: str = Field(default="idle", max_length=20)
    last_sync_message: str = Field(default="", max_length=600)
    last_archive: Optional[str] = Field(default=None, max_length=200)
    games_fetched_total: int = Field(default=0)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
