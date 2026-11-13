from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlmodel import Session, col, select

from app.config import get_settings
from app.db import get_session
from app.models import CoachNote, Game, Move
from app.schemas import (AskRequest, AskResponse, CoachAllRequest, EstimateOut, SettingsOut,
                         SettingsUpdate, StatusOut)
from app.services import app_settings, classify, coach_service
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


def _notes(session: Session, moves: list[Move]) -> dict[int, CoachNote]:
    return {n.move_id: n for n in session.exec(select(CoachNote).where(CoachNote.move_id.in_([m.id for m in moves])))}


# ---------- settings (top-bar toggle) ----------

@router.get("/settings", response_model=SettingsOut)
def read_settings(session: Session = Depends(get_session)):
    return SettingsOut(masterclass=app_settings.get_bool(session, app_settings.MASTERCLASS))


@router.put("/settings", response_model=SettingsOut)
def update_settings(body: SettingsUpdate, session: Session = Depends(get_session)):
    app_settings.set_bool(session, app_settings.MASTERCLASS, body.masterclass)
    return SettingsOut(masterclass=body.masterclass)


# ---------- reviews ----------

@router.post("/games/{game_id}/coach", response_model=StatusOut, status_code=202)
def coach_game(game_id: int, background: BackgroundTasks, refresh: bool = False,
               session: Session = Depends(get_session)):
    """Coach notes + game summary, generated in the background.

    Standard mode explains the mistakes; Masterclass mode explains every move.
    """
    game = _analysed_game(session, game_id)
    mode = app_settings.coach_mode(session)
    if game.coach_status not in ("running", "queued", "stopping"):
        game.coach_status, game.coach_progress, game.coach_error = "running", 0, None
        session.add(game)
        session.commit()
        background.add_task(coach_service.coach_game, game_id, refresh, None, mode)
    return StatusOut(status="running", progress=game.coach_progress)


@router.post("/coach/all", response_model=EstimateOut, status_code=202)
def coach_all(body: CoachAllRequest, background: BackgroundTasks, session: Session = Depends(get_session)):
    """Review several games one after another (analysing them first if needed)."""
    mode = app_settings.coach_mode(session)
    estimate = _estimate(session, body.game_ids, mode)
    games = _games(session, body.game_ids)
    queued = [g.id for g in games if g.coach_status not in ("running", "queued", "stopping")]
    for g in games:
        if g.id in queued:
            g.coach_status, g.coach_progress, g.coach_error = "queued", 0, None
            session.add(g)
    session.commit()
    if queued:
        background.add_task(coach_service.coach_all, queued, mode)
    return estimate


@router.get("/coach/estimate", response_model=EstimateOut)
def estimate(game_id: int | None = None, session: Session = Depends(get_session)):
    """How many LLM calls a review would make, so the UI can ask before spending."""
    return _estimate(session, [game_id] if game_id else None, app_settings.coach_mode(session))


def _games(session: Session, game_ids: list[int] | None) -> list[Game]:
    query = select(Game).order_by(Game.created_at)
    if game_ids:
        query = query.where(Game.id.in_(game_ids))
    return list(session.exec(query))


def _estimate(session: Session, game_ids: list[int] | None, mode: str) -> EstimateOut:
    settings = get_settings()
    games = _games(session, game_ids)
    calls = 0
    for g in games:
        moves = moves_for_game(session, g.id)
        notes = _notes(session, moves)
        calls += coach_service.estimate_calls(g, moves, notes, mode, settings)
    model = coach_service.settings_for(mode, settings).coach_model
    history = [n.explanation.get("_usage") for n in session.exec(
        select(CoachNote).where(CoachNote.mode == mode, CoachNote.model == model)
        .order_by(col(CoachNote.id).desc()).limit(200))]
    cost, basis = coach_service.estimate_cost(calls, mode, history, settings)
    return EstimateOut(mode=mode, games=len(games), calls=calls, cost_usd=cost, cost_basis=basis)


# ---------- stopping ----------

def _stop(session: Session, game: Game) -> None:
    if game.coach_status == "queued":
        game.coach_status, game.coach_error = "stopped", "Stopped before it started."
    elif game.coach_status == "running":
        game.coach_status = "stopping"  # the running job sees this and stops after the calls in flight
    session.add(game)


@router.post("/games/{game_id}/coach/stop", response_model=StatusOut)
def stop_game(game_id: int, session: Session = Depends(get_session)):
    """Stop this game's review. Explanations already made are kept."""
    game = session.get(Game, game_id)
    if not game:
        raise HTTPException(404, "Game not found")
    _stop(session, game)
    session.commit()
    return StatusOut(status=game.coach_status, progress=game.coach_progress, error=game.coach_error)


@router.post("/coach/stop")
def stop_all(session: Session = Depends(get_session)):
    """Stop every running or queued review."""
    games = list(session.exec(select(Game).where(col(Game.coach_status).in_(["queued", "running"]))))
    for g in games:
        _stop(session, g)
    session.commit()
    return {"stopped": len(games)}


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
    mode = app_settings.coach_mode(session)
    existing = session.exec(select(CoachNote).where(CoachNote.move_id == move_id)).first()
    if existing and not refresh and coach_service.note_is_current(existing, mode):
        return existing.explanation
    if mode != "masterclass" and move.classification not in classify.FLAGGED:
        raise HTTPException(400, "Turn on Masterclass mode to get explanations of good moves.")
    explanation = None
    if mode == "masterclass" and not refresh:
        explanation = coach_service.trivial_note(move, previous[-1] if previous else None)
    if explanation is None:
        try:
            explanation = coach_service.explain_move(game, move, previous, mode=mode)
        except coach_service.CoachError as exc:
            coach_service.add_usage(game, exc.usage)
            session.add(game)
            session.commit()
            raise HTTPException(502, str(exc))
    coach_service.add_usage(game, explanation.get("_usage"))
    session.add(game)
    if existing:
        session.delete(existing)
        session.flush()
    model = coach_service.settings_for(mode, get_settings()).coach_model
    session.add(CoachNote(move_id=move_id, explanation=explanation, model=model, mode=mode))
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
                game, move, previous, question, analyzer.analyse, note.explanation if note else None,
                mode=app_settings.coach_mode(session),
            )
    except coach_service.CoachError as exc:
        coach_service.add_usage(game, exc.usage)
        session.add(game)
        session.commit()
        raise HTTPException(502, str(exc))
    except FileNotFoundError:
        raise HTTPException(503, "Stockfish not found. Check STOCKFISH_PATH in .env.")
    coach_service.add_usage(game, result.get("_usage"))
    session.add(game)
    session.commit()
    return AskResponse(answer=result["answer"], checked_moves=result["checked_moves"])
