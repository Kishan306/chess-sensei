"""Position fact extraction.

Everything here is computed with python-chess, so the coach can explain *verified* facts:
material, hanging pieces, tactics (forks, pins, skewers, discovered attacks, mate threats,
trapped pieces), threats that were ignored, king safety, pawn structure, development and
piece activity. Each tactic comes as {"type": ..., "text": ...} so the text can go straight
into the LLM prompt and the type can be used for tags and tests.
"""
from typing import Optional

import chess

VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 100}
ROOK_DIRS = [(0, 1), (0, -1), (1, 0), (-1, 0)]
BISHOP_DIRS = [(1, 1), (1, -1), (-1, 1), (-1, -1)]


# ---------- small helpers ----------

def color_name(color: chess.Color) -> str:
    return "white" if color == chess.WHITE else "black"


def describe_piece(board: chess.Board, square: int) -> str:
    p = board.piece_at(square)
    return f"{color_name(p.color)} {chess.piece_name(p.piece_type)} on {chess.square_name(square)}"


def value_at(board: chess.Board, square: int) -> int:
    p = board.piece_at(square)
    return VALUES[p.piece_type] if p else 0


def material(board: chess.Board) -> dict:
    totals = {}
    for color in (chess.WHITE, chess.BLACK):
        totals[color_name(color)] = sum(
            VALUES[pt] * len(board.pieces(pt, color))
            for pt in (chess.PAWN, chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN)
        )
    return totals


def balance(board: chess.Board, color: chess.Color) -> int:
    m = material(board)
    return m[color_name(color)] - m[color_name(not color)]


def format_line(fen: str, sans: list[str]) -> str:
    """['Nf6', 'Bg5'] from a Black-to-move position at move 12 -> '12... Nf6 13. Bg5'."""
    board = chess.Board(fen)
    out = []
    for i, san in enumerate(sans):
        if board.turn == chess.WHITE:
            out.append(f"{board.fullmove_number}. {san}")
        else:
            out.append(f"{board.fullmove_number}... {san}" if i == 0 else san)
        try:
            board.push_san(san)
        except ValueError:
            break
    return " ".join(out)


def effective_attackers(board: chess.Board, color: chess.Color, square: int) -> list[int]:
    """Pieces of `color` that can really capture on `square` (pinned pieces only along their pin)."""
    result = []
    for a in board.attackers(color, square):
        if board.is_pinned(color, a) and not (board.pin(color, a) & chess.BB_SQUARES[square]):
            continue
        if board.piece_type_at(a) == chess.KING and board.attackers(not color, square):
            continue  # king can't capture a defended piece
        result.append(a)
    return result


# ---------- hanging pieces ----------

def hanging_pieces(board: chess.Board, color: chess.Color) -> list[dict]:
    """Pieces of `color` that are attacked and either undefended or attacked by a cheaper piece."""
    found = []
    for square, piece in board.piece_map().items():
        if piece.color != color or piece.piece_type == chess.KING:
            continue
        attackers = effective_attackers(board, not color, square)
        if not attackers:
            continue
        cheapest = min(attackers, key=lambda a: value_at(board, a))
        if not board.attackers(color, square):
            reason = "undefended"
        elif value_at(board, cheapest) < VALUES[piece.piece_type]:
            reason = f"attacked by a cheaper piece ({describe_piece(board, cheapest)})"
        else:
            continue
        found.append({
            "square": chess.square_name(square),
            "piece": chess.piece_name(piece.piece_type),
            "value": VALUES[piece.piece_type],
            "reason": reason,
            "text": f"{describe_piece(board, square)} is {reason}",
        })
    return sorted(found, key=lambda h: -h["value"])


# ---------- tactics ----------

def _ray(square: int, df: int, dr: int):
    f, r = chess.square_file(square) + df, chess.square_rank(square) + dr
    while 0 <= f < 8 and 0 <= r < 8:
        yield chess.square(f, r)
        f, r = f + df, r + dr


def line_tactics(board: chess.Board, square: int) -> list[dict]:
    """Pins and skewers created by the slider on `square`."""
    piece = board.piece_at(square)
    if not piece or piece.piece_type not in (chess.BISHOP, chess.ROOK, chess.QUEEN):
        return []
    dirs = []
    if piece.piece_type in (chess.ROOK, chess.QUEEN):
        dirs += ROOK_DIRS
    if piece.piece_type in (chess.BISHOP, chess.QUEEN):
        dirs += BISHOP_DIRS

    tactics = []
    for df, dr in dirs:
        hits = []
        for s in _ray(square, df, dr):
            if board.piece_at(s):
                hits.append(s)
                if len(hits) == 2:
                    break
        if len(hits) < 2:
            continue
        front, back = hits
        if board.color_at(front) == piece.color or board.color_at(back) == piece.color:
            continue
        vf, vb = value_at(board, front), value_at(board, back)
        by = describe_piece(board, square)
        if vb == 100:
            tactics.append({"type": "pin", "text": f"{by} pins the {describe_piece(board, front)} to the king"})
        elif vf == 100 or (vf > vb and vb >= 3):
            tactics.append({"type": "skewer",
                            "text": f"{by} skewers the {describe_piece(board, front)} and the {describe_piece(board, back)} behind it"})
        elif vb > vf:
            tactics.append({"type": "pin",
                            "text": f"{by} pins the {describe_piece(board, front)} to the {describe_piece(board, back)}"})
    return tactics


def fork_targets(board: chess.Board, square: int) -> list[int]:
    """Enemy pieces attacked from `square` that are worth forking: the king, or a piece worth
    at least 3 that is more valuable than the attacker or undefended."""
    piece = board.piece_at(square)
    attacker_value = VALUES[piece.piece_type]
    targets = []
    for t in board.attacks(square):
        target = board.piece_at(t)
        if not target or target.color == piece.color:
            continue
        v = VALUES[target.piece_type]
        if v == 100 or (v >= 3 and (v > attacker_value or not board.attackers(target.color, t))):
            targets.append(t)
    return targets


def trapped_pieces(board: chess.Board, color: chess.Color) -> list[dict]:
    """Pieces of `color` (side to move) worth 3+ that are hanging and have no safe square."""
    if board.turn != color:
        return []
    hanging = {h["square"] for h in hanging_pieces(board, color)}
    result = []
    for h_sq in hanging:
        sq = chess.parse_square(h_sq)
        if value_at(board, sq) < 3:
            continue
        safe = False
        for mv in board.legal_moves:
            if mv.from_square != sq:
                continue
            b = board.copy(stack=False)
            b.push(mv)
            if not any(h["square"] == chess.square_name(mv.to_square) for h in hanging_pieces(b, color)):
                safe = True
                break
        if not safe:
            result.append({"type": "trapped_piece", "text": f"the {describe_piece(board, sq)} is trapped"})
    return result


def mate_threat(board: chess.Board, color: chess.Color) -> Optional[str]:
    """If `color` could mate in one given a free move, return that move in SAN."""
    if board.is_check():
        return None
    b = board.copy(stack=False)
    if b.turn != color:
        b.push(chess.Move.null())
    for mv in b.legal_moves:
        b.push(mv)
        mate = b.is_checkmate()
        b.pop()
        if mate:
            return b.san(mv)
    return None


def describe_move(board: chess.Board, move: chess.Move) -> list[dict]:
    """What a move does tactically, from the position before it."""
    b = board.copy(stack=False)
    mover = b.turn
    san = b.san(move)
    tactics = []

    if b.is_capture(move):
        cap_sq = move.to_square
        if b.is_en_passant(move):
            cap_sq = chess.square(chess.square_file(move.to_square), chess.square_rank(move.from_square))
        tactics.append({"type": "capture", "text": f"{san} captures the {describe_piece(b, cap_sq)}"})
    if move.promotion:
        tactics.append({"type": "promotion", "text": f"{san} promotes to a {chess.piece_name(move.promotion)}"})

    # Sliders other than the moved piece, to spot discovered attacks
    before_attacks = {
        s: b.attacks(s) for s, p in b.piece_map().items()
        if p.color == mover and p.piece_type in (chess.BISHOP, chess.ROOK, chess.QUEEN) and s != move.from_square
    }

    b.push(move)

    if b.is_checkmate():
        tactics.append({"type": "checkmate", "text": f"{san} is checkmate"})
        return tactics
    if b.is_check():
        tactics.append({"type": "check", "text": f"{san} gives check"})

    targets = fork_targets(b, move.to_square)
    if len(targets) >= 2:
        names = " and the ".join(describe_piece(b, t) for t in targets)
        tactics.append({"type": "fork", "text": f"{san} forks the {names}"})
    elif len(targets) == 1 and value_at(b, targets[0]) != 100:
        tactics.append({"type": "attack", "text": f"{san} attacks the {describe_piece(b, targets[0])}"})

    tactics += line_tactics(b, move.to_square)

    for s, old in before_attacks.items():
        new = b.attacks(s) & ~old
        for t in new:
            target = b.piece_at(t)
            if target and target.color != mover and VALUES[target.piece_type] >= 3:
                kind = "discovered_check" if target.piece_type == chess.KING else "discovered_attack"
                verb = "gives a discovered check" if kind == "discovered_check" else "uncovers an attack"
                tactics.append({"type": kind,
                                "text": f"{san} {verb}: the {describe_piece(b, s)} now hits the {describe_piece(b, t)}"})

    threat = mate_threat(b, mover)
    if threat:
        tactics.append({"type": "mate_threat", "text": f"{san} threatens mate with {threat}"})

    tactics += trapped_pieces(b, not mover)
    return tactics


# ---------- threats before the move ----------

def opponent_threats(board: chess.Board) -> list[dict]:
    """What the opponent threatened before the side to move played (null-move trick)."""
    if board.is_check():
        return []
    me = board.turn
    b = board.copy(stack=False)
    b.push(chess.Move.null())
    my_hanging = {h["square"] for h in hanging_pieces(board, me)}
    threats = []
    for mv in b.legal_moves:
        tactic_types = {t["type"] for t in describe_move(b, mv)}
        wins_piece = b.is_capture(mv) and chess.square_name(mv.to_square) in my_hanging
        if "checkmate" in tactic_types or "fork" in tactic_types or wins_piece:
            kind = "checkmate" if "checkmate" in tactic_types else "fork" if "fork" in tactic_types else "wins_material"
            threats.append({"uci": mv.uci(), "san": b.san(mv), "type": kind})
    order = {"checkmate": 0, "wins_material": 1, "fork": 2}
    return sorted(threats, key=lambda t: order[t["type"]])[:5]


def threats_ignored(before: chess.Board, after: chess.Board) -> list[dict]:
    """Threats from before the move that the opponent can still carry out after it."""
    ignored = []
    for threat in opponent_threats(before):
        mv = chess.Move.from_uci(threat["uci"])
        if mv not in after.legal_moves:
            continue
        types = {t["type"] for t in describe_move(after, mv)}
        still = (
            threat["type"] == "checkmate" and "checkmate" in types
            or threat["type"] == "fork" and "fork" in types
            or threat["type"] == "wins_material"
            and chess.square_name(mv.to_square) in {h["square"] for h in hanging_pieces(after, before.turn)}
        )
        if still:
            ignored.append({**threat, "san": after.san(mv)})
    return ignored


# ---------- king safety, pawns, development, activity ----------

def king_safety(board: chess.Board, color: chess.Color) -> dict:
    king = board.king(color)
    kf, kr = chess.square_file(king), chess.square_rank(king)
    back_rank = 0 if color == chess.WHITE else 7
    forward = 1 if color == chess.WHITE else -1
    castled = kr == back_rank and kf in (0, 1, 2, 6, 7)
    files = [f for f in (kf - 1, kf, kf + 1) if 0 <= f < 8]

    missing_shield, open_files, half_open = [], [], []
    for f in files:
        own = [s for s in board.pieces(chess.PAWN, color) if chess.square_file(s) == f]
        enemy = [s for s in board.pieces(chess.PAWN, not color) if chess.square_file(s) == f]
        shield = [s for s in own if 1 <= (chess.square_rank(s) - kr) * forward <= 2]
        if not shield:
            missing_shield.append(chess.FILE_NAMES[f])
        if not own and not enemy:
            open_files.append(chess.FILE_NAMES[f])
        elif not own:
            half_open.append(chess.FILE_NAMES[f])

    zone = chess.BB_KING_ATTACKS[king] | chess.BB_SQUARES[king]
    attackers = {a for s in chess.SquareSet(zone) for a in board.attackers(not color, s)
                 if board.piece_type_at(a) != chess.KING}
    return {
        "king_square": chess.square_name(king),
        "castled": castled,
        "can_still_castle": board.has_castling_rights(color),
        "missing_pawn_shield_files": missing_shield,
        "open_files_near_king": open_files,
        "half_open_files_near_king": half_open,
        "enemy_pieces_attacking_king_zone": len(attackers),
        "in_check": board.is_check() and board.turn == color,
    }


def pawn_structure(board: chess.Board, color: chess.Color) -> dict:
    pawns = board.pieces(chess.PAWN, color)
    enemy = board.pieces(chess.PAWN, not color)
    forward = 1 if color == chess.WHITE else -1
    by_file = {f: [s for s in pawns if chess.square_file(s) == f] for f in range(8)}
    result = {"isolated": [], "doubled": [], "backward": [], "passed": []}
    for s in pawns:
        f, r = chess.square_file(s), chess.square_rank(s)
        neighbours = [p for nf in (f - 1, f + 1) if 0 <= nf < 8 for p in by_file[nf]]
        name = chess.square_name(s)
        if not neighbours:
            result["isolated"].append(name)
        if len(by_file[f]) > 1:
            result["doubled"].append(name)
        ahead = lambda rank: (rank - r) * forward > 0  # noqa: E731
        if not any(chess.square_file(e) in (f - 1, f, f + 1) and ahead(chess.square_rank(e)) for e in enemy):
            result["passed"].append(name)
        if neighbours and all(ahead(chess.square_rank(p)) for p in neighbours):
            front = s + 8 * forward
            if 0 <= front < 64 and board.attackers(not color, front) & enemy:
                result["backward"].append(name)
    return {k: sorted(v) for k, v in result.items()}


def pawn_structure_changes(before: chess.Board, after: chess.Board, move: chess.Move) -> dict:
    """Pawn weaknesses/strengths created or removed by the move, for both sides."""
    moved_pawn = before.piece_type_at(move.from_square) == chess.PAWN
    changes = {}
    for color in (chess.WHITE, chess.BLACK):
        old = pawn_structure(before, color)
        new = pawn_structure(after, color)
        side = {}
        for kind in old:
            o = set(old[kind])
            if moved_pawn and before.turn == color and chess.square_name(move.from_square) in o:
                # The same pawn just moved: compare it on its new square
                o = (o - {chess.square_name(move.from_square)}) | {chess.square_name(move.to_square)}
            created, removed = sorted(set(new[kind]) - o), sorted(o - set(new[kind]))
            if created or removed:
                side[kind] = {"created": created, "removed": removed}
        if side:
            changes[color_name(color)] = side
    return changes


def development(board: chess.Board, color: chess.Color) -> dict:
    homes = {chess.WHITE: [chess.B1, chess.G1, chess.C1, chess.F1], chess.BLACK: [chess.B8, chess.G8, chess.C8, chess.F8]}
    undeveloped = [
        chess.square_name(s) for s in homes[color]
        if (p := board.piece_at(s)) and p.color == color and p.piece_type in (chess.KNIGHT, chess.BISHOP)
    ]
    king = board.king(color)
    return {
        "undeveloped_minor_pieces": undeveloped,
        "king_in_centre": chess.square_file(king) in (3, 4) and not king_safety(board, color)["castled"],
    }


def mobility(board: chess.Board, square: int) -> int:
    piece = board.piece_at(square)
    own = board.occupied_co[piece.color]
    return len(chess.SquareSet(board.attacks(square) & ~own))


# ---------- lines ----------

def line_outcome(fen: str, sans: list[str], color: chess.Color) -> dict:
    """Material change for `color` along a line, and whether it ends in mate."""
    board = chess.Board(fen)
    start = balance(board, color)
    for san in sans:
        try:
            board.push_san(san)
        except ValueError:
            break
    return {
        "material_change": balance(board, color) - start,
        "ends_in_checkmate": board.is_checkmate(),
        "mated_side": color_name(board.turn) if board.is_checkmate() else None,
    }


# ---------- main entry ----------

def extract_facts(
    fen_before: str,
    played_uci: str,
    best_uci: Optional[str],
    pv_best: list[str],
    pv_played: list[str],
    phase: Optional[str] = None,
) -> dict:
    """All facts for one move. pv_best starts from fen_before, pv_played from the position after."""
    before = chess.Board(fen_before)
    me = before.turn
    played = chess.Move.from_uci(played_uci)
    played_san = before.san(played)
    after = before.copy(stack=False)
    after.push(played)

    hanging_before = hanging_pieces(before, me)
    hanging_after = hanging_pieces(after, me)
    before_squares = {h["square"] for h in hanging_before}
    moved_to = chess.square_name(played.to_square)

    facts = {
        "mover": color_name(me),
        "move": format_line(fen_before, [played_san]),
        "material_before": material(before),
        "material_after": material(after),
        "played_move_tactics": describe_move(before, played),
        "my_hanging_pieces_after_move": hanging_after,
        "newly_hanging_after_move": [h for h in hanging_after if h["square"] not in before_squares or h["square"] == moved_to],
        "opponent_threats_before_move": [
            {"move": t["san"], "type": t["type"]} for t in opponent_threats(before)
        ],
        "threats_ignored": [{"move": t["san"], "type": t["type"]} for t in threats_ignored(before, after)],
        "king_safety_after_move": king_safety(after, me),
        "opponent_king_safety": king_safety(after, not me),
        "pawn_structure_changes": pawn_structure_changes(before, after, played),
        "moved_piece_activity": {
            "piece": chess.piece_name(before.piece_type_at(played.from_square)),
            "squares_controlled_before": mobility(before, played.from_square),
            "squares_controlled_after": mobility(after, played.to_square),
            "on_the_rim": after.piece_type_at(played.to_square) == chess.KNIGHT
            and chess.square_file(played.to_square) in (0, 7),
        },
    }

    if pv_played:
        try:
            reply = after.parse_san(pv_played[0])
            facts["opponent_best_reply"] = {
                "move": format_line(after.fen(), [pv_played[0]]),
                "tactics": describe_move(after, reply),
            }
        except ValueError:
            pass
        facts["refutation_line"] = format_line(after.fen(), pv_played)
        facts["refutation_outcome_for_me"] = line_outcome(after.fen(), pv_played, me)

    if best_uci and best_uci != played_uci:
        best = chess.Move.from_uci(best_uci)
        facts["best_move"] = {
            "move": format_line(fen_before, [before.san(best)]),
            "tactics": describe_move(before, best),
            "line": format_line(fen_before, pv_best),
            "outcome_for_me": line_outcome(fen_before, pv_best, me),
        }

    if phase == "opening":
        dev = development(after, me)
        dev["early_queen_move"] = before.piece_type_at(played.from_square) == chess.QUEEN and before.fullmove_number <= 8
        facts["development"] = dev

    return facts
