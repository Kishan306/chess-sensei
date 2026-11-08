"""Win-percentage maths, move classification, opening book and game phase."""
import math
from functools import lru_cache
from pathlib import Path
from typing import Optional

import chess

BOOK_FILE = Path(__file__).resolve().parent.parent / "data" / "book.txt"

PIECE_VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}

FLAGGED = ("inaccuracy", "mistake", "blunder", "missed_win")


# ---------- win % ----------

def win_pct_from_cp(cp: int) -> float:
    """Lichess formula. cp is from the point of view of the side we want the win % for."""
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def win_pct(cp: Optional[int], mate: Optional[int]) -> float:
    """Win % for White, given a White-POV score. A forced mate counts as 100% / 0%."""
    if mate is not None:
        return 100.0 if mate > 0 else 0.0
    return win_pct_from_cp(cp or 0)


def for_mover(white_win_pct: float, color: str) -> float:
    return white_win_pct if color == "white" else 100.0 - white_win_pct


# ---------- classification ----------

def classify_move(
    win_before: float,
    win_after: float,
    *,
    is_best: bool,
    is_book: bool = False,
    inaccuracy: float = 10,
    mistake: float = 20,
    blunder: float = 30,
) -> str:
    """Classify a move from the mover's win % before (i.e. with best play) and after it.

    Both numbers must already be from the mover's point of view.
    """
    if is_book:
        return "book"
    if is_best:
        return "best"

    drop = win_before - win_after

    # Moves that don't change the outcome: the game was already won or already lost.
    if (win_before >= 95 and win_after >= 95) or (win_before <= 5 and win_after <= 5):
        return "excellent" if drop < 2 else "good"

    if win_before >= 80 and win_after < 60:
        return "missed_win"
    if drop >= blunder:
        return "blunder"
    if drop >= mistake:
        return "mistake"
    if drop >= inaccuracy:
        return "inaccuracy"
    if drop < 2:
        return "excellent"
    return "good"


# ---------- opening book ----------

@lru_cache
def _book_prefixes() -> frozenset[tuple[str, ...]]:
    prefixes: set[tuple[str, ...]] = set()
    for raw in BOOK_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        sans = tuple(line.split())
        for i in range(1, len(sans) + 1):
            prefixes.add(sans[:i])
    return frozenset(prefixes)


def is_book(sans_up_to_and_including_move: list[str]) -> bool:
    return tuple(sans_up_to_and_including_move) in _book_prefixes()


# ---------- phase ----------

def non_pawn_material(board: chess.Board, color: chess.Color) -> int:
    return sum(
        PIECE_VALUES[pt] * len(board.pieces(pt, color))
        for pt in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN)
    )


_MINOR_HOME = {
    chess.WHITE: {chess.KNIGHT: [chess.B1, chess.G1], chess.BISHOP: [chess.C1, chess.F1]},
    chess.BLACK: {chess.KNIGHT: [chess.B8, chess.G8], chess.BISHOP: [chess.C8, chess.F8]},
}


def undeveloped_minors(board: chess.Board, color: chess.Color) -> int:
    count = 0
    for piece_type, squares in _MINOR_HOME[color].items():
        for sq in squares:
            p = board.piece_at(sq)
            if p and p.piece_type == piece_type and p.color == color:
                count += 1
    return count


def detect_phase(board: chess.Board, move_number: int) -> str:
    white_np = non_pawn_material(board, chess.WHITE)
    black_np = non_pawn_material(board, chess.BLACK)
    queens_off = not board.pieces(chess.QUEEN, chess.WHITE) and not board.pieces(chess.QUEEN, chess.BLACK)

    if (white_np <= 13 and black_np <= 13) or (queens_off and white_np + black_np <= 26):
        return "endgame"

    undeveloped = undeveloped_minors(board, chess.WHITE) + undeveloped_minors(board, chess.BLACK)
    # Opening lasts until move 12 at most, and ends earlier once most minor pieces are out.
    if move_number <= 12 and undeveloped > 2:
        return "opening"
    return "middlegame"
