import chess

from app.services import facts as F


def types(tactics):
    return {t["type"] for t in tactics}


def test_knight_fork_on_king_and_rook():
    board = chess.Board("r3k3/8/8/3N4/8/8/8/4K3 w - - 0 1")
    t = F.describe_move(board, chess.Move.from_uci("d5c7"))
    assert {"check", "fork"} <= types(t)
    fork = next(x for x in t if x["type"] == "fork")["text"]
    assert "e8" in fork and "a8" in fork


def test_bishop_pin_to_king():
    board = chess.Board("4k3/8/2n5/8/8/8/8/4KB2 w - - 0 1")
    t = F.describe_move(board, chess.Move.from_uci("f1b5"))
    assert "pin" in types(t)
    assert "c6" in next(x for x in t if x["type"] == "pin")["text"]


def test_bishop_skewer_king_and_queen():
    board = chess.Board("8/8/8/1q6/2k3B1/8/8/7K w - - 0 1")
    t = F.describe_move(board, chess.Move.from_uci("g4e2"))
    assert {"check", "skewer"} <= types(t)


def test_discovered_attack_with_check():
    board = chess.Board("6k1/4q3/8/8/4N3/8/8/4R1K1 w - - 0 1")
    t = F.describe_move(board, chess.Move.from_uci("e4f6"))
    assert {"check", "discovered_attack"} <= types(t)


def test_hanging_queen_after_blunder():
    board = chess.Board()
    for san in ["e4", "e5", "Nf3", "Qg5"]:
        board.push_san(san)
    hanging = F.hanging_pieces(board, chess.BLACK)
    assert hanging[0]["square"] == "g5" and hanging[0]["piece"] == "queen"
    assert hanging[0]["reason"] == "undefended"


def test_defended_piece_attacked_by_cheaper_piece():
    # Black knight on d5, defended by e6 pawn, attacked by the c4 pawn
    board = chess.Board("4k3/8/4p3/3n4/2P5/8/8/4K3 b - - 0 1")
    hanging = F.hanging_pieces(board, chess.BLACK)
    assert hanging and "cheaper" in hanging[0]["reason"]
    # The defended e6 pawn isn't hanging
    assert all(h["square"] != "e6" for h in hanging)


def test_back_rank_mate():
    board = chess.Board("6k1/5ppp/8/8/8/8/5PPP/3R2K1 w - - 0 1")
    assert "checkmate" in types(F.describe_move(board, chess.Move.from_uci("d1d8")))


def test_back_rank_threat_ignored_or_parried():
    board = chess.Board("1n4k1/5ppp/8/8/8/8/5PPP/3R2K1 b - - 0 1")
    threats = F.opponent_threats(board)
    assert threats[0]["type"] == "checkmate" and threats[0]["san"] == "Rd8#"

    def after(uci):
        b = board.copy()
        b.push_uci(uci)
        return b

    assert F.threats_ignored(board, after("b8a6"))  # Na6 ignores the threat
    assert not F.threats_ignored(board, after("b8c6"))  # Nc6 covers d8
    assert not F.threats_ignored(board, after("h7h6"))  # h6 makes luft


def test_mate_threat_detected():
    # The knight leaves the d-file, so the rook now threatens Rd8# on the back rank
    board = chess.Board("6k1/5ppp/8/8/3N4/8/5PPP/3R2K1 w - - 0 1")
    t = F.describe_move(board, chess.Move.from_uci("d4b5"))
    threat = next(x for x in t if x["type"] == "mate_threat")
    assert "Rd8#" in threat["text"]


def test_trapped_bishop():
    board = chess.Board("r3k3/B1p5/1p6/8/8/8/8/4K3 w - - 0 1")
    assert F.trapped_pieces(board, chess.WHITE)


def test_pawn_structure():
    board = chess.Board("4k3/8/8/8/8/2P5/2P2P1P/4K3 w - - 0 1")
    s = F.pawn_structure(board, chess.WHITE)
    assert s["doubled"] == ["c2", "c3"]
    assert set(s["isolated"]) == {"c2", "c3", "f2", "h2"}
    assert set(s["passed"]) == {"c2", "c3", "f2", "h2"}


def test_king_safety_open_files():
    board = chess.Board("4k3/8/8/8/8/8/5P1P/6K1 w - - 0 1")
    ks = F.king_safety(board, chess.WHITE)
    assert ks["castled"] is True
    assert ks["missing_pawn_shield_files"] == ["g"]
    assert ks["open_files_near_king"] == ["g"]


def test_format_line_with_move_numbers():
    fen = chess.Board().fen()
    assert F.format_line(fen, ["e4", "e5", "Nf3"]) == "1. e4 e5 2. Nf3"
    b = chess.Board()
    b.push_san("e4")
    assert F.format_line(b.fen(), ["e5", "Nf3"]) == "1... e5 2. Nf3"


def test_extract_facts_for_queen_blunder():
    b = chess.Board()
    for san in ["e4", "e5", "Nf3"]:
        b.push_san(san)
    facts = F.extract_facts(b.fen(), "d8g5", "b8c6", ["Nc6", "Bb5"], ["Nxg5"])
    assert facts["move"] == "2... Qg5"
    assert facts["newly_hanging_after_move"][0]["piece"] == "queen"
    assert facts["refutation_line"] == "3. Nxg5"
    assert facts["refutation_outcome_for_me"]["material_change"] == -9
    assert "capture" in types(facts["opponent_best_reply"]["tactics"])
    assert facts["best_move"]["move"] == "2... Nc6"
