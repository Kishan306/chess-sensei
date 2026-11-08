import chess

from app.services.facts import extract_facts
from app.services.tags import TACTICAL, merge_tags, tags_from_facts


def tags_for(fen, played, best, pv_best, pv_played, phase="middlegame", cls="blunder", move_number=20):
    f = extract_facts(fen, played, best, pv_best, pv_played, phase)
    return tags_from_facts(f, cls, phase, move_number)


def test_hanging_queen_and_early_queen():
    b = chess.Board()
    for san in ["e4", "e5", "Nf3"]:
        b.push_san(san)
    t = tags_for(b.fen(), "d8g5", "b8c6", ["Nc6"], ["Nxg5"], phase="opening", move_number=2)
    assert "hanging_piece" in t and "early_queen" in t


def test_allowed_mate_fools_mate():
    b = chess.Board()
    for san in ["f3", "e5"]:
        b.push_san(san)
    t = tags_for(b.fen(), "g2g4", "b1c3", ["Nc3"], ["Qh4#"], phase="opening", move_number=2)
    assert "allowed_mate" in t


def test_allowed_fork():
    # Black plays ...h6 and lets Nc7+ fork the king and rook
    fen = "r3k3/7p/8/1N6/8/8/8/4K3 b - - 0 1"
    t = tags_for(fen, "h7h6", "e8d7", ["Kd7"], ["Nc7+"])
    assert "allowed_fork" in t


def test_missed_back_rank_mate():
    fen = "6k1/5ppp/8/8/8/8/5PPP/3R2K1 w - - 0 1"
    t = tags_for(fen, "h2h3", "d1d8", ["Rd8#"], [], cls="missed_win")
    assert "missed_mate" in t and "missed_win" in t


def test_ignored_back_rank_threat():
    fen = "1n4k1/5ppp/8/8/8/8/5PPP/3R2K1 b - - 0 1"
    t = tags_for(fen, "b8a6", "h7h6", ["h6"], ["Rd8#"])
    assert {"ignored_threat", "allowed_mate", "back_rank"} <= set(t)


def test_missed_fork():
    fen = "r3k3/7p/8/1N6/8/8/8/4K3 w - - 0 1"
    t = tags_for(fen, "e1d2", "b5c7", ["Nc7+"], [], cls="mistake")
    assert "missed_fork" in t


def test_positional_tags_only_without_tactics():
    # Quiet endgame move with nothing tactical going on
    fen = "8/5k2/8/8/8/8/5K2/8 w - - 0 50"
    t = tags_for(fen, "f2e3", "f2f3", ["Kf3"], [], phase="endgame", cls="inaccuracy", move_number=50)
    assert t == ["endgame_technique"]
    assert not set(t) & TACTICAL


def test_no_facts_no_tags():
    assert tags_from_facts(None, "blunder", "middlegame", 10) == []


def test_merge_keeps_fact_tags_first_without_duplicates():
    assert merge_tags(["hanging_piece"], ["hanging_piece", "calculation"]) == ["hanging_piece", "calculation"]
    assert merge_tags(None, ["calculation"]) == ["calculation"]
