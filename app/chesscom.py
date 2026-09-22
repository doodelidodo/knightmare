"""Duenner Client fuer die oeffentliche Chess.com-API (keine Anmeldung noetig).

Wichtig laut Chess.com-Doku: serieller Zugriff ist unlimitiert, parallele
Requests koennen 429 ausloesen. Darum bewusst nacheinander mit kleiner Pause.
Ein aussagekraeftiger User-Agent wird ebenfalls ausdruecklich empfohlen -
Anfragen ohne werden in der Praxis regelmaessig mit 403 abgewiesen.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.chess.com/pub"


class ChessComError(RuntimeError):
    pass


class ChessComClient:
    def __init__(
        self,
        username: str,
        user_agent: str,
        timeout: float = 30.0,
        pause_seconds: float = 0.4,
    ) -> None:
        username = (username or "").strip().lower()
        if not username:
            raise ChessComError(
                "Kein Chess.com-Benutzername konfiguriert (CHESSCOM_USERNAME in der .env)."
            )
        self.username = username
        self.pause_seconds = pause_seconds
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            },
        )

    # -- Lebenszyklus ---------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ChessComClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- Interna --------------------------------------------------------
    def _get(self, url: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self._client.get(url)
            except httpx.HTTPError as exc:  # Netzwerkfehler, DNS, Timeout
                last_error = exc
                time.sleep(1.5 * (attempt + 1))
                continue

            if response.status_code == 429:
                wait = 2.0 * (attempt + 1)
                log.warning("Chess.com antwortet mit 429, warte %.1fs", wait)
                time.sleep(wait)
                continue
            if response.status_code == 404:
                raise ChessComError(
                    f"Chess.com kennt diese Adresse nicht (404): {url} - "
                    "stimmt der Benutzername?"
                )
            if response.status_code == 403:
                raise ChessComError(
                    "Chess.com hat die Anfrage abgelehnt (403). Das liegt fast immer "
                    "am User-Agent - CHESS_USER_AGENT in der .env auf etwas "
                    "Wiedererkennbares mit Kontaktmoeglichkeit setzen."
                )
            response.raise_for_status()

            if self.pause_seconds:
                time.sleep(self.pause_seconds)
            try:
                return response.json()
            except ValueError as exc:
                raise ChessComError(f"Unerwartete Antwort von {url}: kein JSON.") from exc

        if last_error is not None:
            raise ChessComError(
                f"Chess.com nicht erreichbar ({url}): {last_error}"
            ) from last_error
        raise ChessComError(f"Chess.com begrenzt die Anfragen (429) fuer {url}.")

    # -- Endpunkte ------------------------------------------------------
    def profile(self) -> dict[str, Any]:
        return self._get(f"{BASE_URL}/player/{self.username}")

    def stats(self) -> dict[str, Any]:
        return self._get(f"{BASE_URL}/player/{self.username}/stats")

    def archives(self) -> list[str]:
        """Liste der Monatsarchive, aeltestes zuerst."""
        data = self._get(f"{BASE_URL}/player/{self.username}/games/archives")
        archives = data.get("archives")
        if not isinstance(archives, list):
            return []
        return [str(item) for item in archives]

    def games(self, archive_url: str) -> list[dict[str, Any]]:
        data = self._get(archive_url)
        games = data.get("games")
        if not isinstance(games, list):
            return []
        return [game for game in games if isinstance(game, dict)]
