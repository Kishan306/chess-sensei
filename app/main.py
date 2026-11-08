import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.db import create_db_and_tables, reset_interrupted_analyses
from app.routes import analysis, coach, games

logging.basicConfig(level=logging.INFO)
STATIC = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_db_and_tables()
    reset_interrupted_analyses()
    yield


app = FastAPI(title="Chess Sensei", lifespan=lifespan)
app.include_router(games.router)
app.include_router(analysis.router)
app.include_router(coach.router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/review/{game_id}", include_in_schema=False)
def review(game_id: int):
    return FileResponse(STATIC / "review.html")
