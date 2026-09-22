"""Client fuer die Lichess-Partie-API.

Anders als bei Chess.com gibt es keine Monatsarchive, sondern einen einzigen
Endpunkt, der die Partien als NDJSON-Strom liefert - eine Zeile JSON pro
Partie. Das passt gut zum inkrementellen Nachladen: `since` als Zeitstempel
genuegt, um nur das Neue zu holen.

Anmeldung ist nicht noetig. Mit einem Token (LICHESS_TOKEN) steigt die
Ratenbegrenzung, ohne reicht es fuer den Hausgebrauch ebenfalls.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterator, Optional

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://lichess.org/api"

# Nur diese Geschwindigkeiten holen - Varianten interessieren uns nicht.
DEFAULT_PERF_TYPES = "ultraBullet,bullet,blitz,rapid,classical,correspondence"


# Feste Abfrageparameter. Absichtlich hier oben und nicht in der Methode
# versteckt, damit der Selbsttest sie pruefen kann - "moves" war schon einmal
# falsch gesetzt und hat jede Lichess-Partie unanalysierbar gemacht.
QUERY_PARAMS: dict[str, str] = {
    "perfType": DEFAULT_PERF_TYPES,
    "pgnInJson": "true",   # PGN mitliefern, das ist unsere Analysequelle
    "clocks": "true",      # [%clk]-Kommentare ins PGN - fuer die Zeitdruck-Auswertung
    "opening": "true",     # Eroeffnungsname und ECO ohne eigenes Raten
    "tags": "true",
    # NICHT abschalten: "moves" steuert, ob die Zuege ueberhaupt mitkommen -
    # auch die im PGN. Mit moves=false liefert Lichess ein PGN aus reinen
    # Kopfzeilen, und jede Analyse scheitert mit "Partie enthaelt keine Zuege".
    "moves": "true",
    "evals": "false",      # wir rechnen selbst
    "sort": "dateDesc",
    "finished": "true",
}


class LichessError(RuntimeError):
    pass


class LichessClient:
    def __init__(
        self,
        username: str,
        user_agent: str,
        token: str = "",
        timeout: float = 120.0,
    ) -> None:
        username = (username or "").strip()
        if not username:
            raise LichessError(
                "Kein Lichess-Benutzername konfiguriert (LICHESS_USERNAME)."
            )
        self.username = username
        headers = {
            "User-Agent": user_agent,
            "Accept": "application/x-ndjson",
        }
        if token:
            headers["Authorization"] = f"Bearer {token.strip()}"
        # Grosszuegiger Timeout: der Export ist ein Stream, der bei vielen
        # Partien durchaus eine Weile laeuft.
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout, read=timeout),
            follow_redirects=True,
            headers=headers,
        )

    # -- Lebenszyklus ---------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LichessClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- Endpunkte ------------------------------------------------------
    def profile(self) -> dict[str, Any]:
        response = self._client.get(
            f"{BASE_URL}/user/{self.username}", headers={"Accept": "application/json"}
        )
        if response.status_code == 404:
            raise LichessError(f"Lichess kennt den Benutzer '{self.username}' nicht.")
        response.raise_for_status()
        return response.json()

    def games(
        self,
        since_ms: Optional[int] = None,
        max_games: Optional[int] = None,
        rated_only: bool = True,
    ) -> Iterator[dict[str, Any]]:
        """Liefert Partien als Strom, neueste zuerst.

        Bewusst ein Generator: bei mehreren tausend Partien soll nicht erst
        alles im Speicher landen.
        """
        params: dict[str, Any] = dict(QUERY_PARAMS)
        if rated_only:
            params["rated"] = "true"
        if since_ms:
            params["since"] = str(int(since_ms))
        if max_games:
            params["max"] = str(int(max_games))

        url = f"{BASE_URL}/games/user/{self.username}"
        attempt = 0
        while True:
            try:
                with self._client.stream("GET", url, params=params) as response:
                    if response.status_code == 429:
                        attempt += 1
                        if attempt > 3:
                            raise LichessError(
                                "Lichess begrenzt die Anfragen (429). Spaeter erneut "
                                "versuchen oder LICHESS_TOKEN setzen."
                            )
                        wait = 60.0
                        log.warning("Lichess antwortet mit 429, warte %.0fs", wait)
                        time.sleep(wait)
                        continue
                    if response.status_code == 404:
                        raise LichessError(
                            f"Lichess kennt den Benutzer '{self.username}' nicht."
                        )
                    response.raise_for_status()

                    for line in response.iter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            payload = json.loads(line)
                        except ValueError:
                            log.warning("Unlesbare Zeile im Lichess-Strom uebersprungen")
                            continue
                        if isinstance(payload, dict):
                            yield payload
                    return
            except httpx.HTTPError as exc:
                attempt += 1
                if attempt > 3:
                    raise LichessError(f"Lichess nicht erreichbar: {exc}") from exc
                time.sleep(2.0 * attempt)
