import chess
import pytest

from app.services import classify
from app.services.classify import classify_move, detect_phase, is_book, win_pct, win_pct_from_cp


def test_win_pct_formula():
    assert win_pct_from_cp(0) == pytest.approx(50)
    assert win_pct_from_cp(100) == pytest.approx(59.1, abs=0.1)
    assert win_pct_from_cp(-300) == pytest.approx(24.9, abs=0.1)
    assert win_pct_from_cp(1000) > 97


def test_mate_counts_as_full_win_or_loss():
    assert win_pct(None, 3) == 100
    assert win_pct(None, -1) == 0


def test_for_mover_flips_for_black():
    assert classify.for_mover(70, "white") == 70
    assert classify.for_mover(70, "black") == 30


@pytest.mark.parametrize("before,after,expected", [
    (50, 49, "excellent"),
    (50, 45, "good"),
    (50, 40, "inaccuracy"),
    (50, 30, "mistake"),
    (50, 15, "blunder"),
    (60, 0, "blunder"),
])
def test_thresholds(before, after, expected):
    assert classify_move(before, after, is_best=False) == expected


def test_best_and_book_win_over_drop():
    assert classify_move(50, 10, is_best=True) == "best"
    assert classify_move(50, 10, is_best=False, is_book=True) == "book"


def test_missed_win():
    assert classify_move(85, 55, is_best=False) == "missed_win"
    # Still winning afterwards → not a missed win, just a drop
    assert classify_move(90, 65, is_best=False) == "mistake"


def test_no_flag_when_already_decided():
    assert classify_move(99.5, 96, is_best=False) in ("excellent", "good")
    assert classify_move(4, 0.5, is_best=False) in ("excellent", "good")


def test_custom_thresholds():
    assert classify_move(50, 42, is_best=False, inaccuracy=5) == "inaccuracy"


def test_book_lookup():
    assert is_book(["e4", "e5", "Nf3", "Nc6", "Bb5"])
    assert not is_book(["e4", "e5", "Ke2"])
    assert not is_book(["f3"])


def test_phase_detection():
    assert detect_phase(chess.Board(), 1) == "opening"
    # Developed pieces by move 9 → middlegame
    dev = chess.Board("r2q1rk1/ppp1bppp/2np1n2/4p3/2B1P1b1/2NP1N2/PPP2PPP/R1BQ1RK1 w - - 0 9")
    assert detect_phase(dev, 9) == "middlegame"
    assert detect_phase(dev, 14) == "middlegame"
    # Rook + bishop each → endgame
    end = chess.Board("4k3/pp3ppp/2b5/8/8/5B2/PP3PPP/3R2K1 w - - 0 30")
    assert detect_phase(end, 30) == "endgame"
