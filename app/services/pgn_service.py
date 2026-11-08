"""Parse and validate PGN text, split multi-game files, detect the user's colour."""
import io
import re
from dataclasses import dataclass, field
from typing import Optional

import chess.pgn


@dataclass
class ParsedGame:
    index: int  # 1-based position in the uploaded text
    pgn: str
    headers: dict
    sans: list[str] = field(default_factory=list)


@dataclass
class PgnError:
    index: int
    message: str


def _int_or_none(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _describe_error(game: chess.pgn.Game, err: Exception) -> str:
    """Turn a python-chess parse error into 'move 12 (Black): illegal san ...'."""
    played = list(game.mainline_moves())
    ply = len(played) + 1  # the move after the last one that parsed
    move_no = (ply + 1) // 2
    side = "White" if ply % 2 == 1 else "Black"
    text = str(err)
    m = re.match(r"(illegal|invalid|ambiguous) san: '([^']+)'", text)
    if m:
        text = f"{m.group(1)} move '{m.group(2)}'"
    return f"move {move_no} ({side}): {text}"


def parse_pgn_text(text: str) -> tuple[list[ParsedGame], list[PgnError]]:
    """Split text into games. Returns (valid games, errors for rejected games)."""
    games: list[ParsedGame] = []
    errors: list[PgnError] = []
    if not text or not text.strip():
        return games, [PgnError(0, "No PGN text provided.")]

    stream = io.StringIO(text)
    index = 0
    while True:
        game = chess.pgn.read_game(stream)
        if game is None:
            break
        index += 1
        if game.errors:
            errors.append(PgnError(index, _describe_error(game, game.errors[0])))
            continue
        moves = list(game.mainline_moves())
        if not moves:
            errors.append(PgnError(index, "game has no moves."))
            continue
        if game.board().fen() != chess.STARTING_FEN:
            errors.append(PgnError(index, "games from a custom start position are not supported yet."))
            continue

        board = game.board()
        sans = []
        for mv in moves:
            sans.append(board.san(mv))
            board.push(mv)

        exporter = chess.pgn.StringExporter(headers=True, variations=False, comments=False)
        games.append(ParsedGame(index=index, pgn=game.accept(exporter), headers=dict(game.headers), sans=sans))

    if index == 0:
        errors.append(PgnError(0, "No games found in the PGN text."))
    return games, errors


def detect_my_color(headers: dict, usernames: list[str]) -> Optional[str]:
    names = {u.lower() for u in usernames}
    white = headers.get("White", "").strip().lower()
    black = headers.get("Black", "").strip().lower()
    if white in names and black not in names:
        return "white"
    if black in names and white not in names:
        return "black"
    return None


def game_fields(parsed: ParsedGame, usernames: list[str]) -> dict:
    """Map PGN headers to Game model fields."""
    h = parsed.headers
    opening = h.get("Opening", "")
    if not opening and h.get("ECOUrl"):  # chess.com puts the opening in a URL
        opening = re.sub(r"[-]", " ", h["ECOUrl"].rsplit("/", 1)[-1])
    date = h.get("UTCDate") or h.get("Date", "")
    if date.replace("?", "").replace(".", "") == "":
        date = ""  # PGN placeholder for "unknown"
    return {
        "pgn_raw": parsed.pgn,
        "white": h.get("White", ""),
        "black": h.get("Black", ""),
        "white_elo": _int_or_none(h.get("WhiteElo")),
        "black_elo": _int_or_none(h.get("BlackElo")),
        "result": h.get("Result", "*"),
        "date": date,
        "event": h.get("Event", ""),
        "time_control": h.get("TimeControl", ""),
        "opening_eco": h.get("ECO", ""),
        "opening_name": opening,
        "my_color": detect_my_color(h, usernames),
    }
