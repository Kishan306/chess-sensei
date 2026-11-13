from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel


class UploadError(BaseModel):
    game_index: int  # 1-based position in the uploaded text (0 = whole upload)
    message: str


class UploadDuplicate(BaseModel):
    game_index: int
    existing_id: int
    label: str  # e.g. "Alice vs Bob, 2026.11.01"


class UploadResult(BaseModel):
    created: list[int]
    errors: list[UploadError]
    duplicates: list[UploadDuplicate] = []


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
    coach_status: str = "none"
    coach_progress: int = 0
    coach_mode: Optional[str] = None
    coach_calls: int = 0
    coach_cost_usd: Optional[float] = 0.0
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


class SettingsOut(BaseModel):
    masterclass: bool


class SettingsUpdate(BaseModel):
    masterclass: bool


class CoachAllRequest(BaseModel):
    game_ids: Optional[list[int]] = None  # None = every game


class EstimateOut(BaseModel):
    mode: str
    games: int
    calls: int
    cost_usd: Optional[float] = None  # None when the model's price is unknown
    cost_basis: str = ""  # "your past reviews" | "a rough guess" | "unknown price"
