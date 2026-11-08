from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Game(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    pgn_raw: str
    white: str = ""
    black: str = ""
    white_elo: Optional[int] = None
    black_elo: Optional[int] = None
    result: str = "*"
    date: str = ""
    event: str = ""
    time_control: str = ""
    opening_eco: str = ""
    opening_name: str = ""
    my_color: Optional[str] = None  # "white" | "black" | None (unknown)
    created_at: datetime = Field(default_factory=_now)
    analysis_status: str = "pending"  # pending | running | done | error
    analysis_progress: int = 0
    analysis_error: Optional[str] = None
    coach_status: str = "none"  # none | running | done | error
    coach_progress: int = 0
    coach_error: Optional[str] = None


class Move(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    game_id: int = Field(foreign_key="game.id", index=True)
    ply: int  # 1-based half-move index
    move_number: int
    color: str  # "white" | "black"
    san: str
    uci: str
    fen_before: str
    fen_after: str
    # Evals are stored from White's point of view
    eval_before_cp: Optional[int] = None
    eval_after_cp: Optional[int] = None
    mate_before: Optional[int] = None
    mate_after: Optional[int] = None
    win_pct_before: Optional[float] = None
    win_pct_after: Optional[float] = None
    best_move_san: Optional[str] = None
    best_move_uci: Optional[str] = None
    pv_best: Optional[list] = Field(default=None, sa_column=Column(JSON))
    pv_played: Optional[list] = Field(default=None, sa_column=Column(JSON))
    multipv: Optional[list] = Field(default=None, sa_column=Column(JSON))
    classification: Optional[str] = None
    phase: Optional[str] = None
    facts: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    tags: Optional[list] = Field(default=None, sa_column=Column(JSON))  # weakness tags from facts


class EvalCache(SQLModel, table=True):
    fen: str = Field(primary_key=True)  # EPD (FEN without move clocks)
    depth: int  # depth the search actually reached
    time_limit: Optional[float] = None  # seconds the search was allowed
    multipv: list = Field(sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)


class CoachNote(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    move_id: int = Field(foreign_key="move.id", index=True)
    explanation: dict = Field(sa_column=Column(JSON))  # structured, see coach_service.CoachExplanation
    model: str
    created_at: datetime = Field(default_factory=_now)


class GameSummary(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    game_id: int = Field(foreign_key="game.id", index=True)
    summary: dict = Field(sa_column=Column(JSON))
    model: str = ""
    created_at: datetime = Field(default_factory=_now)
