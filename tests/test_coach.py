"""Coach service tests with a fake LLM (no API calls)."""
import json

import chess
import pytest

from app.config import Settings
from app.models import Game, Move
from app.services import coach_service as C
from app.services.engine_service import build_moves


def _queen_blunder_game():
    pgn = '[White "Player123"]\n[Black "Opp"]\n\n1. e4 e5 2. Nf3 Qg5 3. Nxg5 *'
    best = {  # fake engine: equal everywhere, but after 2...Qg5 White is winning
        "rnb1kbnr/pppp1ppp/8/4p1q1/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq -": {"best": "f3g5", "cp": 900},
        "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq -": {"best": "b8c6", "cp": 30},
    }

    def analyse(board):
        spec = best.get(board.epd(), {})
        uci = spec.get("best") or sorted(m.uci() for m in board.legal_moves)[0]
        line = [uci]
        b = board.copy()
        b.push_uci(uci)
        sans = [board.san(chess.Move.from_uci(uci))]
        return [{"uci": line, "san": sans, "cp": spec.get("cp", 0), "mate": None}]

    game = Game(id=1, pgn_raw=pgn, white="Player123", black="Opp", my_color="black", result="*")
    moves = build_moves(1, pgn, analyse, Settings())
    for i, m in enumerate(moves, start=1):
        m.id = i
    return game, moves


GOOD = C.CoachExplanation(
    headline="Your queen came out too early and was simply lost.",
    likely_intention="You wanted to attack g2 with the queen.",
    why_it_fails="After 2... Qg5 the queen is undefended and the knight takes it: 3. Nxg5.",
    opponent_punishment="White plays 3. Nxg5 and wins your queen for nothing.",
    better_move="Nc6",
    why_better="2... Nc6 develops and defends e5.",
    alternatives=[],
    principle="Check every square your pieces land on for attackers.",
    theme_tags=["hanging_piece", "early_queen"],
)


class FakeLLM:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, system, messages, model):
        self.calls.append(messages)
        return self.outputs.pop(0)


def test_input_contains_verified_data():
    game, moves = _queen_blunder_game()
    qg5 = moves[3]
    assert qg5.classification == "blunder" and qg5.facts
    payload = C.build_move_input(game, qg5, moves[:3])
    assert payload["move_by"] == "student"
    assert payload["played_move"] == "2... Qg5"
    assert payload["engine_best_move"] == "2... Nc6"
    assert payload["opponent_best_reply_line"] == "3. Nxg5"
    assert payload["previous_moves"] == "1. e4 1... e5 2. Nf3"
    assert payload["facts"]["newly_hanging_after_move"][0]["piece"] == "queen"


def test_good_answer_passes_guard_without_retry():
    game, moves = _queen_blunder_game()
    llm = FakeLLM(GOOD)
    out = C.explain_move(game, moves[3], moves[:3], llm, Settings())
    assert len(llm.calls) == 1
    assert out["_guard"] == {"retried": False, "removed_moves": []}
    assert out["better_move"] == "Nc6"


def test_invented_move_triggers_retry_then_accepts_fix():
    game, moves = _queen_blunder_game()
    bad = GOOD.model_copy(update={"why_better": "2... Nc6 is good, and later Bb4 pins the knight."})
    llm = FakeLLM(bad, GOOD)
    out = C.explain_move(game, moves[3], moves[:3], llm, Settings())
    assert len(llm.calls) == 2
    assert "Bb4" in llm.calls[1][-1]["content"]  # correction names the invented move
    assert out["_guard"]["retried"] and not out["_guard"]["removed_moves"]


def test_invented_move_removed_if_retry_still_bad():
    game, moves = _queen_blunder_game()
    bad = GOOD.model_copy(update={
        "why_better": "2... Nc6 develops a piece. Then Bb4 pins the knight.",
        "alternatives": [C.Alternative(move="Qh4", comment="also bad")],
    })
    out = C.explain_move(game, moves[3], moves[:3], FakeLLM(bad, bad), Settings())
    assert out["_guard"]["removed_moves"] == ["Bb4", "Qh4"]
    assert out["why_better"] == "2... Nc6 develops a piece."
    assert out["alternatives"] == []


def test_squares_are_not_mistaken_for_moves():
    assert C.san_tokens("the knight on e5 and the pawn on d4") == set()
    assert C.san_tokens("after 13. e5 Nxe5 14. O-O-O+ and Qxh7#") == {"e5", "Nxe5", "O-O-O", "Qxh7"}


def test_summary_and_question():
    game, moves = _queen_blunder_game()
    summary_out = C.GameSummaryOut(
        turning_points=[C.TurningPoint(move="2... Qg5", description="The queen was lost.")],
        how_it_was_decided="Black lost the queen on move 2.",
        strength_shown="You played 1... e5 confidently.",
        main_weakness="Not checking if pieces are safe.",
        practice_next="Before every move, check what the opponent attacks.",
    )
    s = C.summarize_game(game, moves, {}, FakeLLM(summary_out), Settings())
    assert s["turning_points"][0]["move"] == "2... Qg5"

    calls = []

    def analyse(board):
        calls.append(board.fen())
        mv = sorted(board.legal_moves, key=lambda m: m.uci())[0]
        return [{"uci": [mv.uci()], "san": [board.san(mv)], "cp": 0, "mate": None}]

    answer = C.AskAnswer(answer="2... Nc6 was safer because it develops and keeps the queen home.")
    res = C.answer_question(game, moves[3], moves[:3], "why not Nc6? and what about Qh5?", analyse,
                            llm=FakeLLM(answer), settings=Settings())
    checked = {c["move"]: c for c in res["checked_moves"]}
    assert checked["Nc6"]["legal"] and "evaluation_after_it" in checked["Nc6"]
    assert checked["Nc6"]["as_played"] == "2... Nc6"
    assert checked["Qh5"]["legal"] is False  # not legal for either side here
    assert len(calls) == 1
