# Slim Debian base: small, and its apt repo ships Stockfish at /usr/games/stockfish.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends stockfish \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Run as a normal user; the database lives in /data (a volume, so it survives rebuilds).
RUN useradd --uid 1000 --create-home app && mkdir /data && chown app:app /data
USER app

ENV STOCKFISH_PATH=/usr/games/stockfish \
    DATABASE_URL=sqlite:////data/chess_coach.db
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
