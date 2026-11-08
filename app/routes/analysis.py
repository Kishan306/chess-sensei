from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlmodel import Session

from app.db import get_session
from app.models import Game
from app.schemas import StatusOut
from app.services.engine_service import analyze_game

router = APIRouter(prefix="/api/games", tags=["analysis"])


@router.post("/{game_id}/analyze", response_model=StatusOut, status_code=202)
def start_analysis(game_id: int, background: BackgroundTasks, session: Session = Depends(get_session)):
    game = session.get(Game, game_id)
    if not game:
        raise HTTPException(404, "Game not found")
    if game.analysis_status == "running":
        return StatusOut(status=game.analysis_status, progress=game.analysis_progress)
    game.analysis_status, game.analysis_progress, game.analysis_error = "running", 0, None
    session.add(game)
    session.commit()
    background.add_task(analyze_game, game_id)
    return StatusOut(status="running", progress=0)


@router.get("/{game_id}/status", response_model=StatusOut)
def analysis_status(game_id: int, session: Session = Depends(get_session)):
    game = session.get(Game, game_id)
    if not game:
        raise HTTPException(404, "Game not found")
    return StatusOut(status=game.analysis_status, progress=game.analysis_progress, error=game.analysis_error)
