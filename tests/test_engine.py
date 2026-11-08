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


# ---------- deep re-check of mistakes ----------

def _scored(scores: dict[str, int], default=0):
    """Fake analyser: White-POV cp by EPD, best move = first legal move. Records calls."""
    calls = []

    def analyse(board):
        calls.append(board.epd())
        mv = sorted(board.legal_moves, key=lambda m: m.uci())[0]
        return [{"uci": [mv.uci()], "san": [board.san(mv)], "cp": scores.get(board.epd(), default), "mate": None}]

    analyse.calls = calls
    return analyse


PGN = '[White "a"]\n[Black "b"]\n\n1. h3 a6 2. h4 a5 *'


def test_deep_pass_clears_a_false_blunder():
    after_h4 = _epd_after(["h3", "a6", "h4"])
    quick = _scored({after_h4: -600})  # shallow search thinks 2. h4 loses
    deep = _scored({})  # deeper search: it's equal
    moves = build_moves(1, PGN, quick, Settings(), deep_fn=deep)
    h4 = moves[2]
    assert h4.classification not in ("blunder", "mistake", "inaccuracy")
    assert h4.eval_after_cp == 0
    # The bad reading made both 2. h4 and 2... a5 look wrong, so only the positions around
    # those two moves are re-searched, not the whole game.
    assert set(deep.calls) == {_epd_after(["h3", "a6"]), after_h4, _epd_after(["h3", "a6", "h4", "a5"])}
    assert moves[3].classification not in ("blunder", "mistake", "inaccuracy")


def test_deep_pass_catches_a_hidden_mistake():
    after_h4 = _epd_after(["h3", "a6", "h4"])
    quick = _scored({after_h4: -80})  # near-miss: ~7% drop, not flagged yet
    deep = _scored({after_h4: -250})  # deeper search: it's a real mistake
    moves = build_moves(1, PGN, quick, Settings(), deep_fn=deep)
    assert moves[2].classification in ("mistake", "blunder")
    assert moves[2].tags is not None  # facts/tags computed from the deep result


def test_no_deep_pass_when_nothing_suspicious():
    deep = _scored({})
    build_moves(1, PGN, _scored({}), Settings(), deep_fn=deep)
    assert deep.calls == []


class _FakeEngine:
    def __init__(self, depth):
        self.depth = depth
        self.calls = 0

    def analyse(self, board, limit, multipv):
        self.calls += 1
        mv = next(iter(board.legal_moves))
        return [{"pv": [mv], "score": chess.engine.PovScore(chess.engine.Cp(20), chess.WHITE), "depth": self.depth}]


def test_cache_stores_reached_depth_and_reuses_by_budget():
    import chess.engine  # noqa: F401

    db = create_engine("sqlite://")
    SQLModel.metadata.create_all(db)
    with Session(db) as session:
        a = EngineAnalyzer(session, Settings(engine_depth=18, engine_time=0.5, engine_deep_depth=24, engine_deep_time=3))
        a.engine = _FakeEngine(depth=12)  # quick search times out at depth 12
        board = chess.Board()

        a.analyse(board)
        row = session.get(EvalCache, board.epd())
        assert (row.depth, row.time_limit) == (12, 0.5)

        a.analyse(board)  # same budget: reused even though depth 18 wasn't reached
        assert a.engine.calls == 1

        a.engine.depth = 24
        a.analyse(board, deep=True)  # bigger budget: searched again
        assert a.engine.calls == 2 and session.get(EvalCache, board.epd()).depth == 24

        a.analyse(board)  # deep result is good enough for a quick request too
        assert a.engine.calls == 2
        a.engine = None
