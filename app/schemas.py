from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel


class UploadError(BaseModel):
    game_index: int  # 1-based position in the uploaded text (0 = whole upload)
    message: str


class UploadResult(BaseModel):
    created: list[int]
    errors: list[UploadError]


class GameListItem(BaseModel):
    id: int
    white: str
    black: str
    white_elo: Optional[int]
    black_elo: Optional[int]
    result: str
    date: str
    opening_eco: str
    opening_name: str
    my_color: Optional[str]
    analysis_status: str
    analysis_progress: int
    created_at: datetime


class MoveOut(BaseModel):
    id: int
    ply: int
    move_number: int
    color: str
    san: str
    uci: str
    fen_before: str
    fen_after: str
    eval_before_cp: Optional[int]
    eval_after_cp: Optional[int]
    mate_before: Optional[int]
    mate_after: Optional[int]
    win_pct_before: Optional[float]
    win_pct_after: Optional[float]
    best_move_san: Optional[str]
    best_move_uci: Optional[str]
    pv_best: Optional[list]
    pv_played: Optional[list]
    multipv: Optional[list]
    classification: Optional[str]
    phase: Optional[str]
    tags: Optional[list] = None


class GameDetail(GameListItem):
    event: str
    time_control: str
    pgn_raw: str
    analysis_error: Optional[str]
    coach_status: str
    coach_progress: int
    coach_error: Optional[str]
    moves: list[MoveOut]
    notes: dict[int, dict] = {}
    summary: Optional[dict] = None


class GameUpdate(BaseModel):
    my_color: Optional[Literal["white", "black"]] = None


class StatusOut(BaseModel):
    status: str
    progress: int
    error: Optional[str] = None


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    answer: str
    checked_moves: list[dict]
