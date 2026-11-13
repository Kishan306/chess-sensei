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
from app.services import classify, facts, tags

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

    def analyse(self, board: chess.Board, deep: bool = False) -> list[dict]:
        """Top lines for a position, best first. Each line: {uci, san, cp, mate} (White POV).

        deep=True uses the slower ENGINE_DEEP_* limits (used to re-check mistakes).
        Results are cached by position; a cached result is reused when it was searched with
        at least the same budget (depth actually reached, or time allowed).
        """
        s = self.settings
        depth, time_limit = (s.engine_deep_depth, s.engine_deep_time) if deep else (s.engine_depth, s.engine_time)
        key = board.epd()
        cached = self.session.get(EvalCache, key)
        if cached and cached.multipv and (cached.depth >= depth or (cached.time_limit or 0) >= time_limit):
            return cached.multipv

        infos = self.engine.analyse(
            board, chess.engine.Limit(depth=depth, time=time_limit), multipv=s.engine_multipv
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
        reached = infos[0].get("depth", 0) if infos else 0

        if cached:
            cached.depth, cached.time_limit, cached.multipv = reached, time_limit, lines
        else:
            self.session.add(EvalCache(fen=key, depth=reached, time_limit=time_limit, multipv=lines))
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


def _judge(positions, moves, settings: Settings) -> list[dict]:
    """Evaluate and classify every move from the current position results (cheap, no engine)."""
    judged = []
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
        win_before = classify.for_mover(ev_before["win_pct"], color)
        win_after = classify.for_mover(ev_after["win_pct"], color)
        book = classify.is_book(sans_so_far)
        judged.append({
            "color": color, "san": san, "ev_before": ev_before, "ev_after": ev_after, "best_uci": best_uci,
            "drop": win_before - win_after, "book": book,
            "classification": classify.classify_move(
                win_before, win_after, is_best=(best_uci == mv.uci()), is_book=book,
                inaccuracy=settings.inaccuracy_threshold, mistake=settings.mistake_threshold,
                blunder=settings.blunder_threshold,
            ),
        })
    return judged


def _needs_deep_check(j: dict, settings: Settings) -> bool:
    """Mistakes, plus near-misses that a deeper search might turn into mistakes."""
    if j["book"]:
        return False
    return j["classification"] in classify.FLAGGED or j["drop"] >= 0.6 * settings.inaccuracy_threshold


def build_moves(game_id: int, pgn: str, analyse_fn, settings: Settings, progress_fn=None,
                deep_fn=None) -> list[Move]:
    """Walk the game, evaluate each position with analyse_fn(board) -> lines, and classify moves.

    If deep_fn is given, the positions around every mistake (and near-miss) are searched again
    with it and the moves re-classified, so the labels don't depend on a too-short search.
    The engine functions are injected so tests can run without Stockfish.
    """
    game = chess.pgn.read_game(io.StringIO(pgn))
    moves = list(game.mainline_moves())
    board = game.board()
    quick_share = 70 if deep_fn else 100

    # Quick pass: positions[0] = start, positions[i] = after ply i.
    positions: list[tuple[chess.Board, list[dict]]] = []
    total = len(moves) + 1
    for i in range(total):
        lines = [] if board.is_game_over() else analyse_fn(board)
        positions.append((board.copy(), lines))
        if progress_fn:
            progress_fn(int(quick_share * (i + 1) / total))
        if i < len(moves):
            board.push(moves[i])

    judged = _judge(positions, moves, settings)

    # Deep pass on the positions before and after each candidate move.
    if deep_fn:
        targets = sorted({
            idx for ply, j in enumerate(judged, start=1) if _needs_deep_check(j, settings)
            for idx in (ply - 1, ply) if not positions[idx][0].is_game_over()
        })
        for n, idx in enumerate(targets, start=1):
            pos_board = positions[idx][0]
            positions[idx] = (pos_board, deep_fn(pos_board))
            if progress_fn:
                progress_fn(quick_share + int((100 - quick_share) * n / len(targets)))
        if targets:
            judged = _judge(positions, moves, settings)

    result: list[Move] = []
    for ply, (mv, j) in enumerate(zip(moves, judged), start=1):
        before, lines_before = positions[ply - 1]
        after, lines_after = positions[ply]
        ev_before, ev_after, classification = j["ev_before"], j["ev_after"], j["classification"]
        best_uci = j["best_uci"]
        best_san = lines_before[0]["san"][0] if lines_before and lines_before[0]["san"] else None
        move_number = before.fullmove_number
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
            color=j["color"],
            san=j["san"],
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
            tags=tags.tags_from_facts(move_facts, classification, phase, move_number) if move_facts else None,
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
                deep = (lambda b: analyzer.analyse(b, deep=True)) if settings.engine_deep_time > 0 else None
                moves = build_moves(game_id, game.pgn_raw, analyzer.analyse, settings, progress, deep)
            clear_coaching(session, game_id)
            session.exec(delete(Move).where(Move.game_id == game_id))
            session.add_all(moves)
            if game.coach_status != "queued":  # a queued review still runs after this analysis
                game.coach_status, game.coach_progress, game.coach_error = "none", 0, None
            game.coach_mode = None
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


def ensure_tags(session: Session, moves: list[Move]) -> None:
    """Fill in facts and tags for mistakes in games analysed before tags existed."""
    changed = False
    for m in moves:
        if m.classification not in classify.FLAGGED or m.tags is not None:
            continue
        if not m.facts:
            m.facts = facts.extract_facts(
                m.fen_before, m.uci, m.best_move_uci, m.pv_best or [], m.pv_played or [], m.phase
            )
        m.tags = tags.tags_from_facts(m.facts, m.classification, m.phase, m.move_number)
        session.add(m)
        changed = True
    if changed:
        session.commit()


def clear_coaching(session: Session, game_id: int) -> None:
    move_ids = select(Move.id).where(Move.game_id == game_id)
    session.exec(delete(CoachNote).where(CoachNote.move_id.in_(move_ids)))
    session.exec(delete(GameSummary).where(GameSummary.game_id == game_id))
