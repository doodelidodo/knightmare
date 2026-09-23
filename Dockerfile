FROM python:3.12-slim

LABEL org.opencontainers.image.title="Knightmare"
LABEL org.opencontainers.image.description="Find the patterns behind your chess mistakes."
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/doodelidodo/knightmare"

# Stockfish comes from Debian and lands in /usr/games, which is not on the
# default PATH - app/config.py looks there explicitly.
RUN apt-get update \
    && apt-get install -y --no-install-recommends stockfish \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# Messwerkzeuge - nicht Teil der App, aber im Container nuetzlich:
#   python -m tools.positional_probe
COPY tools ./tools

# Default: SQLite inside the container. Mount a volume on /app/data to keep
# your games across restarts.
ENV DATABASE_URL="sqlite:///./data/knightmare.db"
VOLUME ["/app/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
