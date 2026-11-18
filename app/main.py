import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.db import backfill_fingerprints, create_db_and_tables, reset_interrupted_analyses
from app.routes import analysis, coach, games

logging.basicConfig(level=logging.INFO)
STATIC = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_db_and_tables()
    reset_interrupted_analyses()
    backfill_fingerprints()
    yield


app = FastAPI(title="Chess Sensei", lifespan=lifespan)


@app.middleware("http")
async def same_origin_writes(request, call_next):
    """Other websites open in the browser must not trigger paid reviews or change data here.
    Browsers send Origin on cross-site POST/PUT/PATCH/DELETE; ours matches Host."""
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin and urlsplit(origin).netloc != request.headers.get("host"):
        return JSONResponse({"detail": "Cross-site request blocked."}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def revalidate_pages(request, call_next):
    """Pages and scripts are checked for changes on every load, so a rebuild or update shows up
    immediately instead of the browser reusing an old copy for hours."""
    response = await call_next(request)
    if not request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.include_router(games.router)
app.include_router(analysis.router)
app.include_router(coach.router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/profile", include_in_schema=False)
def profile_page():
    return FileResponse(STATIC / "profile.html")


@app.get("/puzzles", include_in_schema=False)
def puzzles_page():
    return FileResponse(STATIC / "puzzles.html")


@app.get("/review/{game_id}", include_in_schema=False)
def review(game_id: int):
    return FileResponse(STATIC / "review.html")
