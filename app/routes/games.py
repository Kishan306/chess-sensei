from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from sqlmodel import Session, delete, select

from app.config import get_settings
from app.db import get_session
from app.models import CoachNote, Game, GameSummary, Move
from app.schemas import GameDetail, GameListItem, GameUpdate, UploadError, UploadResult
from app.services import pgn_service
from app.services.engine_service import analyze_game, clear_coaching, moves_for_game

router = APIRouter(prefix="/api/games", tags=["games"])


@router.post("", response_model=UploadResult)
async def upload_games(
    background: BackgroundTasks,
    pgn: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    analyze: bool = Form(False),
    session: Session = Depends(get_session),
):
    """Upload PGN as pasted text (form field `pgn`) and/or a `.pgn` file.

    With analyze=true the new games are analysed one after another in the background.
    """
    parts = []
    if pgn and pgn.strip():
        parts.append(pgn)
    if file is not None and file.filename:
        raw = await file.read()
        parts.append(raw.decode("utf-8", errors="replace"))
    text = "\n\n".join(parts)

    parsed, errors = pgn_service.parse_pgn_text(text)
    usernames = get_settings().usernames
    created = []
    for pg in parsed:
        game = Game(**pgn_service.game_fields(pg, usernames))
        if analyze:
            game.analysis_status = "running"  # queued; progress stays 0 until its turn
        session.add(game)
        session.commit()
        created.append(game.id)

    if analyze and created:
        background.add_task(_analyze_all, list(created))

    return UploadResult(
        created=created,
        errors=[UploadError(game_index=e.index, message=e.message) for e in errors],
    )


def _analyze_all(game_ids: list[int]) -> None:
    for game_id in game_ids:
        analyze_game(game_id)


@router.get("", response_model=list[GameListItem])
def list_games(
    result: Optional[str] = None,
    opening: Optional[str] = None,
    color: Optional[str] = None,
    session: Session = Depends(get_session),
):
    query = select(Game).order_by(Game.created_at.desc())
    if result:
        query = query.where(Game.result == result)
    if opening:
        like = f"%{opening}%"
        query = query.where((Game.opening_name.ilike(like)) | (Game.opening_eco.ilike(like)))
    if color:
        query = query.where(Game.my_color == color)
    return session.exec(query).all()


def _get_game(session: Session, game_id: int) -> Game:
    game = session.get(Game, game_id)
    if not game:
        raise HTTPException(404, "Game not found")
    return game


@router.get("/{game_id}", response_model=GameDetail)
def get_game(game_id: int, session: Session = Depends(get_session)):
    game = _get_game(session, game_id)
    moves = moves_for_game(session, game_id)
    notes = session.exec(select(CoachNote).where(CoachNote.move_id.in_([m.id for m in moves]))).all()
    summary = session.exec(select(GameSummary).where(GameSummary.game_id == game_id)).first()
    return GameDetail(
        **game.model_dump(),
        moves=[m.model_dump() for m in moves],
        notes={n.move_id: n.explanation for n in notes},
        summary=summary.summary if summary else None,
    )


@router.patch("/{game_id}", response_model=GameListItem)
def update_game(game_id: int, body: GameUpdate, session: Session = Depends(get_session)):
    """Lets the user pick their colour when it couldn't be detected."""
    game = _get_game(session, game_id)
    game.my_color = body.my_color
    session.add(game)
    session.commit()
    session.refresh(game)
    return game


@router.delete("/{game_id}", status_code=204)
def delete_game(game_id: int, session: Session = Depends(get_session)):
    game = _get_game(session, game_id)
    clear_coaching(session, game_id)
    session.exec(delete(Move).where(Move.game_id == game_id))
    session.delete(game)
    session.commit()
