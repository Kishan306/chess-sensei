"""Engine pipeline tests.

The first group uses a fake analyser so it runs anywhere. The second group runs real
Stockfish and is skipped when STOCKFISH_PATH doesn't point at a working engine.
"""
import os
import shutil
import time

import chess
import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.config import Settings, get_settings
from app.models import EvalCache
from app.services.engine_service import EngineAnalyzer, build_moves


def _fake_analyser(scores: dict[str, dict]):
    """scores: EPD → {"best": uci, "cp": int|None, "mate": int|None}. Default: equal, first legal move."""
    calls = []

    def analyse(board: chess.Board):
        calls.append(board.epd())
        spec = scores.get(board.epd(), {})
        best = spec.get("best") or sorted(m.uci() for m in board.legal_moves)[0]
        mv = chess.Move.from_uci(best)
        return [{"uci": [best], "san": [board.san(mv)], "cp": spec.get("cp", 0), "mate": spec.get("mate")}]

    analyse.calls = calls
    return analyse


def _epd_after(sans: list[str]) -> str:
    b = chess.Board()
    for s in sans:
        b.push_san(s)
    return b.epd()


def test_build_moves_flags_fools_mate_blunder(fixture_text):
    pgn = '[White "a"]\n[Black "b"]\n\n1. f3 e5 2. g4 Qh4# 0-1'
    scores = {
        _epd_after(["f3", "e5", "g4"]): {"best": "d8h4", "mate": -1},  # Black mates in 1
    }
    analyse = _fake_analyser(scores)
    moves = build_moves(1, pgn, analyse, Settings())

    assert [m.san for m in moves] == ["f3", "e5", "g4", "Qh4#"]
    g4, qh4 = moves[2], moves[3]
    assert g4.color == "white" and g4.classification == "blunder"
    assert g4.pv_played == ["Qh4#"]
    assert qh4.classification == "best"
    assert qh4.win_pct_after == 0.0 and qh4.mate_after == 0
    # Final (mated) position is not sent to the engine
    assert len(analyse.calls) == 4


def test_build_moves_marks_book_moves():
    pgn = '[White "a"]\n[Black "b"]\n\n1. e4 e5 2. Nf3 Nc6 *'
    moves = build_moves(1, pgn, _fake_analyser({}), Settings())
    assert all(m.classification == "book" for m in moves)
    assert moves[0].phase == "opening"


def test_progress_reaches_100():
    pgn = '[White "a"]\n[Black "b"]\n\n1. e4 e5 2. Nf3 Nc6 *'
    seen = []
    build_moves(1, pgn, _fake_analyser({}), Settings(), seen.append)
    assert seen[-1] == 100 and seen == sorted(seen)


# ---------- real Stockfish ----------

def _stockfish_path():
    path = os.environ.get("STOCKFISH_PATH") or get_settings().stockfish_path
    return path if path and (os.path.isfile(path) or shutil.which(path)) else None


needs_engine = pytest.mark.skipif(_stockfish_path() is None, reason="Stockfish not available")


@pytest.fixture
def analyzer():
    db = create_engine("sqlite://")
    SQLModel.metadata.create_all(db)
    settings = Settings(stockfish_path=_stockfish_path(), engine_depth=14, engine_time=0.3)
    with Session(db) as session, EngineAnalyzer(session, settings) as a:
        yield a, settings, session


@needs_engine
def test_hanging_queen_is_a_blunder(analyzer, fixture_text):
    a, settings, _ = analyzer
    moves = build_moves(1, fixture_text("queen_blunder.pgn"), a.analyse, settings)
    qg5 = next(m for m in moves if m.san == "Qg5")
    assert qg5.classification == "blunder"
    assert next(m for m in moves if m.san == "Nxg5").classification == "best"


@needs_engine
def test_scholars_mate_defence_blunder(analyzer, fixture_text):
    a, settings, _ = analyzer
    moves = build_moves(1, fixture_text("scholars_mate.pgn"), a.analyse, settings)
    nf6 = next(m for m in moves if m.san == "Nf6")
    assert nf6.classification == "blunder"
    assert nf6.pv_played[0] == "Qxf7#"
    assert next(m for m in moves if m.san == "Qxf7#").classification == "best"


@needs_engine
def test_eval_cache_is_reused(analyzer):
    a, _, session = analyzer
    board = chess.Board()
    first = a.analyse(board)
    assert session.get(EvalCache, board.epd()) is not None
    real_engine, a.engine = a.engine, None  # any engine call would now crash
    try:
        assert a.analyse(board) == first
    finally:
        a.engine = real_engine


@needs_engine
def test_forty_move_game_is_fast_enough(fixture_text):
    db = create_engine("sqlite://")
    SQLModel.metadata.create_all(db)
    settings = Settings(stockfish_path=_stockfish_path())  # real depth/time settings
    start = time.monotonic()
    with Session(db) as session, EngineAnalyzer(session, settings) as a:
        moves = build_moves(1, fixture_text("forty_moves.pgn"), a.analyse, settings)
    assert len(moves) == 80
    assert time.monotonic() - start < 120
