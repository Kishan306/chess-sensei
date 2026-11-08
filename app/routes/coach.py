from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlmodel import Session, select

from app.config import get_settings
from app.db import get_session
from app.models import CoachNote, Game, Move
from app.schemas import AskRequest, AskResponse, StatusOut
from app.services import coach_service
from app.services.engine_service import EngineAnalyzer, moves_for_game

router = APIRouter(prefix="/api", tags=["coach"])


def _analysed_game(session: Session, game_id: int) -> Game:
    game = session.get(Game, game_id)
    if not game:
        raise HTTPException(404, "Game not found")
    if game.analysis_status != "done":
        raise HTTPException(409, "Analyse the game with the engine first.")
    return game


def _move_context(session: Session, move_id: int) -> tuple[Game, Move, list[Move]]:
    move = session.get(Move, move_id)
    if not move:
        raise HTTPException(404, "Move not found")
    game = _analysed_game(session, move.game_id)
    previous = [m for m in moves_for_game(session, game.id) if m.ply < move.ply]
    return game, move, previous


@router.post("/games/{game_id}/coach", response_model=StatusOut, status_code=202)
def coach_game(game_id: int, background: BackgroundTasks, refresh: bool = False,
               session: Session = Depends(get_session)):
    """Coach notes for all flagged moves + a game summary, generated in the background."""
    game = _analysed_game(session, game_id)
    if game.coach_status != "running":
        game.coach_status, game.coach_progress, game.coach_error = "running", 0, None
        session.add(game)
        session.commit()
        background.add_task(coach_service.coach_game, game_id, refresh)
    return StatusOut(status="running", progress=game.coach_progress)


@router.get("/games/{game_id}/coach/status", response_model=StatusOut)
def coach_status(game_id: int, session: Session = Depends(get_session)):
    game = session.get(Game, game_id)
    if not game:
        raise HTTPException(404, "Game not found")
    return StatusOut(status=game.coach_status, progress=game.coach_progress, error=game.coach_error)


@router.get("/moves/{move_id}/coach")
def move_coach(move_id: int, refresh: bool = False, session: Session = Depends(get_session)):
    """Coach note for one move; generated on demand (takes a few seconds) if missing."""
    game, move, previous = _move_context(session, move_id)
    existing = session.exec(select(CoachNote).where(CoachNote.move_id == move_id)).first()
    if existing and not refresh:
        return existing.explanation
    try:
        explanation = coach_service.explain_move(game, move, previous)
    except coach_service.CoachError as exc:
        raise HTTPException(502, str(exc))
    if existing:
        session.delete(existing)
    session.add(CoachNote(move_id=move_id, explanation=explanation, model=get_settings().coach_model))
    session.add(move)
    session.commit()
    return explanation


@router.post("/moves/{move_id}/ask", response_model=AskResponse)
def ask(move_id: int, body: AskRequest, session: Session = Depends(get_session)):
    """Follow-up question about a position. Moves mentioned in the question are engine-checked first."""
    question = body.question.strip()
    if not question:
        raise HTTPException(422, "Ask a question first.")
    if len(question) > 1000:
        raise HTTPException(422, "Please keep the question under 1000 characters.")
    game, move, previous = _move_context(session, move_id)
    note = session.exec(select(CoachNote).where(CoachNote.move_id == move_id)).first()
    try:
        with EngineAnalyzer(session) as analyzer:
            result = coach_service.answer_question(
                game, move, previous, question, analyzer.analyse, note.explanation if note else None
            )
    except coach_service.CoachError as exc:
        raise HTTPException(502, str(exc))
    except FileNotFoundError:
        raise HTTPException(503, "Stockfish not found. Check STOCKFISH_PATH in .env.")
    return AskResponse(answer=result["answer"], checked_moves=result["checked_moves"])
