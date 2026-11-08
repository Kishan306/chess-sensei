"""Stockfish wrapper with an eval cache, plus the background job that analyses a whole game."""
import io
import logging
from typing import Optional

import chess
import chess.engine
import chess.pgn
from sqlmodel import Session, delete, select

from app.config import Settings, get_settings
from app.models import CoachNote, EvalCache, Game, GameSummary, Move
from app.services import classify, facts

log = logging.getLogger(__name__)


def _line_to_san(board: chess.Board, ucis: list[str]) -> list[str]:
    b = board.copy(stack=False)
    sans = []
    for u in ucis:
        mv = chess.Move.from_uci(u)
        if mv not in b.legal_moves:
            break
        sans.append(b.san(mv))
        b.push(mv)
    return sans


def terminal_eval(board: chess.Board) -> Optional[dict]:
    """Eval for a finished position (no engine needed). White POV."""
    if board.is_checkmate():
        # Side to move is mated. mate=0 with an explicit win %.
        return {"cp": None, "mate": 0, "win_pct": 0.0 if board.turn == chess.WHITE else 100.0}
    if board.is_stalemate() or board.is_insufficient_material():
        return {"cp": 0, "mate": None, "win_pct": 50.0}
    return None


class EngineAnalyzer:
    """One Stockfish process, reused for many positions. Use as a context manager."""

    def __init__(self, session: Session, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.session = session
        self.engine: Optional[chess.engine.SimpleEngine] = None

    def __enter__(self):
        self.engine = chess.engine.SimpleEngine.popen_uci(self.settings.stockfish_path)
        self.engine.configure({"Threads": self.settings.engine_threads, "Hash": self.settings.engine_hash_mb})
        return self

    def __exit__(self, *exc):
        if self.engine:
            self.engine.quit()
            self.engine = None

    def analyse(self, board: chess.Board) -> list[dict]:
        """Top lines for a position, best first. Each line: {uci, san, cp, mate} (White POV).

        Results are cached by position (EPD), so a position is never analysed twice.
        """
        key = board.epd()
        depth = self.settings.engine_depth
        cached = self.session.get(EvalCache, key)
        if cached and cached.depth >= depth and len(cached.multipv) > 0:
            return cached.multipv

        infos = self.engine.analyse(
            board,
            chess.engine.Limit(depth=depth, time=self.settings.engine_time),
            multipv=self.settings.engine_multipv,
        )
        lines = []
        for info in infos:
            pv = info.get("pv") or []
            if not pv:
                continue
            score = info["score"].white()
            ucis = [m.uci() for m in pv]
            lines.append({
                "uci": ucis,
                "san": _line_to_san(board, ucis),
                "cp": score.score(),
                "mate": score.mate(),
            })

        if cached:
            cached.depth, cached.multipv = depth, lines
        else:
            self.session.add(EvalCache(fen=key, depth=depth, multipv=lines))
        self.session.commit()
        return lines


def _position_eval(board: chess.Board, lines: list[dict]) -> dict:
    term = terminal_eval(board)
    if term:
        return term
    if not lines:
        return {"cp": 0, "mate": None, "win_pct": 50.0}
    top = lines[0]
    return {"cp": top["cp"], "mate": top["mate"], "win_pct": classify.win_pct(top["cp"], top["mate"])}


def build_moves(game_id: int, pgn: str, analyse_fn, settings: Settings, progress_fn=None) -> list[Move]:
    """Walk the game, evaluate each position with analyse_fn(board) -> lines, and classify moves.

    analyse_fn is injected so tests can run without Stockfish.
    """
    game = chess.pgn.read_game(io.StringIO(pgn))
    moves = list(game.mainline_moves())
    board = game.board()

    # Evaluate every position once: positions[0] = start, positions[i] = after ply i.
    positions: list[tuple[chess.Board, list[dict]]] = []
    total = len(moves) + 1
    for i in range(total):
        lines = [] if board.is_game_over() else analyse_fn(board)
        positions.append((board.copy(), lines))
        if progress_fn:
            progress_fn(int(100 * (i + 1) / total))
        if i < len(moves):
            board.push(moves[i])

    result: list[Move] = []
    sans_so_far: list[str] = []
    for ply, mv in enumerate(moves, start=1):
        before, lines_before = positions[ply - 1]
        after, lines_after = positions[ply]
        color = "white" if before.turn == chess.WHITE else "black"
        san = before.san(mv)
        sans_so_far.append(san)

        ev_before = _position_eval(before, lines_before)
        ev_after = _position_eval(after, lines_after)

        best_uci = lines_before[0]["uci"][0] if lines_before else None
        best_san = lines_before[0]["san"][0] if lines_before and lines_before[0]["san"] else None

        move_number = before.fullmove_number
        classification = classify.classify_move(
            classify.for_mover(ev_before["win_pct"], color),
            classify.for_mover(ev_after["win_pct"], color),
            is_best=(best_uci == mv.uci()),
            is_book=classify.is_book(sans_so_far),
            inaccuracy=settings.inaccuracy_threshold,
            mistake=settings.mistake_threshold,
            blunder=settings.blunder_threshold,
        )

        phase = classify.detect_phase(before, move_number)
        pv_best = lines_before[0]["san"] if lines_before else []
        pv_played = lines_after[0]["san"] if lines_after else []
        move_facts = (
            facts.extract_facts(before.fen(), mv.uci(), best_uci, pv_best, pv_played, phase)
            if classification in classify.FLAGGED else None
        )

        result.append(Move(
            game_id=game_id,
            ply=ply,
            move_number=move_number,
            color=color,
            san=san,
            uci=mv.uci(),
            fen_before=before.fen(),
            fen_after=after.fen(),
            eval_before_cp=ev_before["cp"],
            eval_after_cp=ev_after["cp"],
            mate_before=ev_before["mate"],
            mate_after=ev_after["mate"],
            win_pct_before=round(ev_before["win_pct"], 2),
            win_pct_after=round(ev_after["win_pct"], 2),
            best_move_san=best_san,
            best_move_uci=best_uci,
            pv_best=pv_best,
            pv_played=pv_played,
            multipv=lines_before,
            classification=classification,
            phase=phase,
            facts=move_facts,
        ))
    return result


def analyze_game(game_id: int) -> None:
    """Background task: run Stockfish over the whole game and store moves."""
    from app.db import engine as db_engine

    settings = get_settings()
    with Session(db_engine) as session:
        game = session.get(Game, game_id)
        if not game:
            return
        game.analysis_status, game.analysis_progress, game.analysis_error = "running", 0, None
        session.add(game)
        session.commit()

        def progress(pct: int) -> None:
            if pct != game.analysis_progress:
                game.analysis_progress = min(pct, 99)
                session.add(game)
                session.commit()

        try:
            with EngineAnalyzer(session, settings) as analyzer:
                moves = build_moves(game_id, game.pgn_raw, analyzer.analyse, settings, progress)
            clear_coaching(session, game_id)
            session.exec(delete(Move).where(Move.game_id == game_id))
            session.add_all(moves)
            game.coach_status, game.coach_progress, game.coach_error = "none", 0, None
            game.analysis_status, game.analysis_progress = "done", 100
        except Exception as exc:  # report any failure to the UI
            log.exception("Analysis failed for game %s", game_id)
            session.rollback()
            game = session.get(Game, game_id)
            game.analysis_status, game.analysis_error = "error", str(exc) or exc.__class__.__name__
        session.add(game)
        session.commit()


def moves_for_game(session: Session, game_id: int) -> list[Move]:
    return list(session.exec(select(Move).where(Move.game_id == game_id).order_by(Move.ply)))


def clear_coaching(session: Session, game_id: int) -> None:
    move_ids = select(Move.id).where(Move.game_id == game_id)
    session.exec(delete(CoachNote).where(CoachNote.move_id.in_(move_ids)))
    session.exec(delete(GameSummary).where(GameSummary.game_id == game_id))
