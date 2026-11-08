"""LLM coach: builds prompts from verified engine data + facts, calls Claude with structured
output, and guards against invented moves.

Core rule: Stockfish and python-chess decide what's true; the LLM only explains facts it was given.
"""
import json
import logging
import re
from typing import Callable, Literal, Optional

import anthropic
import chess
import openai
from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings
from app.models import Game, Move
from app.services import classify, facts as facts_mod

log = logging.getLogger(__name__)

THEME_TAGS = (
    "hanging_piece", "missed_capture", "missed_fork", "allowed_fork", "missed_pin", "allowed_pin",
    "skewer", "discovered_attack", "back_rank", "missed_mate", "allowed_mate", "mate_threat",
    "trapped_piece", "ignored_threat", "king_safety", "pawn_structure", "passed_pawn",
    "development", "early_queen", "piece_activity", "bad_trade", "endgame_technique",
    "calculation", "overlooked_check", "missed_win", "opening_principles",
)
ThemeTag = Literal[THEME_TAGS]  # type: ignore[valid-type]

# Models that accept the server-side refusal fallback (`fallbacks: "default"`).
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


# ---------- structured outputs ----------

class Alternative(BaseModel):
    move: str
    comment: str


class CoachExplanation(BaseModel):
    headline: str
    likely_intention: str
    why_it_fails: str
    opponent_punishment: str
    better_move: str
    why_better: str
    alternatives: list[Alternative]
    principle: str
    theme_tags: list[ThemeTag]


class TurningPoint(BaseModel):
    move: str  # e.g. "14... Qe6"
    description: str


class GameSummaryOut(BaseModel):
    turning_points: list[TurningPoint]
    how_it_was_decided: str
    strength_shown: str
    main_weakness: str
    practice_next: str


class AskAnswer(BaseModel):
    answer: str


class CoachError(Exception):
    pass


# ---------- prompts ----------

SYSTEM_PROMPT = """You are an experienced, encouraging chess coach reviewing a student's game. \
The student is rated around {rating}.

You receive VERIFIED data: engine lines from Stockfish and position facts computed by a chess \
library. Base every claim on that data.

Rules:
- Only mention moves and lines that appear in the provided data. Never invent variations and never \
state an evaluation that wasn't given. If the data doesn't show something, don't claim it.
- Explain in human terms: plans, ideas, threats, piece safety and principles. Use numbers sparingly \
("you go from roughly equal to losing a piece", not "-2.31").
- Guess what the player was probably trying to do with the move, then explain why it doesn't work.
- Be specific and concrete: name the pieces and squares involved.
- Write moves in standard algebraic notation (e.g. Nf3, Bxf7+, O-O), with move numbers when citing a line.
- Talk to the student directly ("you"). If the move being reviewed was played by the opponent, \
explain the opponent's mistake and how the student could punish it.
- Be warm but honest; keep each field focused and readable (a few sentences, not an essay)."""


def _system(settings: Settings) -> str:
    return SYSTEM_PROMPT.format(rating=settings.player_rating)


def _describe_win(pct: float) -> str:
    if pct >= 90:
        return "winning"
    if pct >= 70:
        return "clearly better"
    if pct >= 55:
        return "slightly better"
    if pct > 45:
        return "about equal"
    if pct > 30:
        return "slightly worse"
    if pct > 10:
        return "clearly worse"
    return "losing"


def _line_eval_words(line: dict, mover_color: str) -> str:
    if line.get("mate") is not None:
        mate = line["mate"]
        mover_mates = (mate > 0) == (mover_color == "white")
        return f"forced mate in {abs(mate)} for {'the mover' if mover_mates else 'the opponent'}"
    pct = classify.for_mover(classify.win_pct(line.get("cp"), None), mover_color)
    return f"{_describe_win(pct)} for the mover ({round(pct)}% win chance)"


def ensure_facts(move: Move) -> dict:
    """Facts are stored at analysis time; compute them for games analysed before Phase 2."""
    if not move.facts:
        move.facts = facts_mod.extract_facts(
            move.fen_before, move.uci, move.best_move_uci, move.pv_best or [], move.pv_played or [], move.phase
        )
    return move.facts


def build_move_input(game: Game, move: Move, previous: list[Move]) -> dict:
    """Everything the coach may use for one move (§7.2 of the plan)."""
    mover = move.color
    wb = classify.for_mover(move.win_pct_before, mover)
    wa = classify.for_mover(move.win_pct_after, mover)
    after = chess.Board(move.fen_after)
    lines = []
    for line in (move.multipv or [])[1:]:
        lines.append({
            "line": facts_mod.format_line(move.fen_before, line["san"][:8]),
            "evaluation": _line_eval_words(line, mover),
        })
    return {
        "game": {
            "opening": " ".join(x for x in (game.opening_eco, game.opening_name) if x) or "unknown",
            "student_colour": game.my_color or "unknown",
            "white": f"{game.white} ({game.white_elo or '?'})",
            "black": f"{game.black} ({game.black_elo or '?'})",
            "time_control": game.time_control or "unknown",
            "result": game.result,
        },
        "move_by": "student" if game.my_color == mover else ("opponent" if game.my_color else mover),
        "move_number": move.move_number,
        "side_to_move": mover,
        "fen": move.fen_before,
        "previous_moves": " ".join(
            facts_mod.format_line(p.fen_before, [p.san]) for p in previous[-3:]
        ),
        "played_move": facts_mod.format_line(move.fen_before, [move.san]),
        "classification": move.classification,
        "phase": move.phase,
        "position_before": f"{_describe_win(wb)} for the mover ({round(wb)}% win chance)",
        "position_after": f"{_describe_win(wa)} for the mover ({round(wa)}% win chance)",
        "engine_best_move": facts_mod.format_line(move.fen_before, [move.best_move_san]) if move.best_move_san else None,
        "engine_best_line": facts_mod.format_line(move.fen_before, move.pv_best or []),
        "opponent_best_reply_line": facts_mod.format_line(after.fen(), move.pv_played or []),
        "other_engine_lines": lines,
        "facts": ensure_facts(move),
    }


# ---------- hallucination guard ----------

_SAN = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(O-O-O|O-O|[KQRBN][a-h]?[1-8]?x?[a-h][1-8]|[a-h]x[a-h][1-8](?:=[QRBN])?|[a-h][1-8]=[QRBN])"
    r"[+#]?(?![A-Za-z0-9])"
)
# Bare pawn moves are only recognisable after a move number ("13. e5", "13...e5"),
# otherwise they look like square names ("the knight on e5").
_NUMBERED_PAWN = re.compile(r"\d+\.(?:\.\.)?\s*([a-h][1-8])(?![A-Za-z0-9=])")


def san_tokens(text: str) -> set[str]:
    return set(_SAN.findall(text)) | set(_NUMBERED_PAWN.findall(text))


def unknown_moves(output: BaseModel, allowed: set[str]) -> set[str]:
    return san_tokens(output.model_dump_json()) - allowed


def _strip_sentences(text: str, bad: set[str]) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept = [s for s in sentences if not (san_tokens(s) & bad)]
    return " ".join(kept)


def scrub(output: BaseModel, bad: set[str]) -> BaseModel:
    """Remove sentences/entries that still mention unverified moves."""
    data = output.model_dump()

    def clean(value):
        if isinstance(value, str):
            return _strip_sentences(value, bad)
        if isinstance(value, list):
            # Drop list entries (e.g. alternatives) whose "move" field is unverified, then clean the rest
            kept = [v for v in value if not (isinstance(v, dict) and san_tokens(str(v.get("move", ""))) & bad)]
            return [clean(v) for v in kept]
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        return value

    return type(output).model_validate(clean(data))


# ---------- LLM client ----------

# (system, messages, output_model) -> parsed output
LLMFn = Callable[[str, list[dict], type[BaseModel]], BaseModel]


class AnthropicCoach:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        # With no key in .env the SDK falls back to other credential sources (e.g. `ant auth login`).
        self.client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key or None)

    def __call__(self, system: str, messages: list[dict], output_model: type[BaseModel]) -> BaseModel:
        kwargs = dict(
            model=self.settings.coach_model,
            max_tokens=16000,
            system=system,
            messages=messages,
            output_format=output_model,
            output_config={"effort": self.settings.coach_effort},
        )
        try:
            if self.settings.coach_fallbacks and self.settings.coach_model in FALLBACK_MODELS:
                response = self.client.beta.messages.parse(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
                )
            else:
                response = self.client.messages.parse(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise CoachError("The Anthropic API key is missing or invalid. Set ANTHROPIC_API_KEY in .env.") from exc
        except anthropic.NotFoundError as exc:
            raise CoachError(f"Model '{self.settings.coach_model}' not found. Check COACH_MODEL in .env.") from exc
        except anthropic.RateLimitError as exc:
            raise CoachError("Rate limited by the Anthropic API. Try again in a minute.") from exc
        except anthropic.APIStatusError as exc:
            raise CoachError(f"Anthropic API error ({exc.status_code}): {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise CoachError("Couldn't reach the Anthropic API. Check your internet connection.") from exc

        if response.stop_reason == "refusal":
            raise CoachError("The model declined to answer this request.")
        if response.stop_reason == "max_tokens" or response.parsed_output is None:
            raise CoachError("The model's answer was cut off or couldn't be parsed.")
        return response.parsed_output


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def _extract_json(text: str) -> str:
    """Pull the JSON object out of a reply that may include <think> blocks or ``` fences."""
    text = _THINK.sub("", text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start != -1 and end > start else text


class OpenAICompatibleCoach:
    """Any server that speaks the OpenAI Chat Completions API: OpenAI, Google Gemini, Groq,
    OpenRouter, or a free local model through Ollama / LM Studio."""

    # Tried in order; the first one the server accepts is remembered.
    _FORMAT_MODES = ("json_schema", "json_object", "none")

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.client = openai.OpenAI(
            api_key=self.settings.coach_api_key or "not-needed",  # local servers ignore the key
            base_url=self.settings.coach_base_url or None,
            timeout=300,  # local models can be slow
            max_retries=2,
        )
        self.mode_index = 0

    def _request(self, messages: list[dict], output_model: type[BaseModel]) -> str:
        schema = output_model.model_json_schema()
        while True:
            mode = self._FORMAT_MODES[self.mode_index]
            kwargs = {"model": self.settings.coach_model, "messages": messages}
            if mode == "json_schema":
                kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": output_model.__name__, "schema": schema, "strict": False},
                }
            elif mode == "json_object":
                kwargs["response_format"] = {"type": "json_object"}
            try:
                response = self.client.chat.completions.create(**kwargs)
            except (openai.BadRequestError, openai.UnprocessableEntityError) as exc:
                # Server doesn't support this response format: try a simpler one
                if self.mode_index < len(self._FORMAT_MODES) - 1:
                    log.info("Provider rejected response_format=%s (%s); trying a simpler mode", mode, exc)
                    self.mode_index += 1
                    continue
                raise CoachError(f"The LLM server rejected the request: {exc}") from exc
            choice = response.choices[0] if response.choices else None
            if not choice or not choice.message or not choice.message.content:
                raise CoachError("The model returned an empty answer.")
            if choice.finish_reason == "length":
                raise CoachError("The model's answer was cut off. Try a model with a larger output limit.")
            return choice.message.content

    def __call__(self, system: str, messages: list[dict], output_model: type[BaseModel]) -> BaseModel:
        schema = json.dumps(output_model.model_json_schema())
        system = (
            f"{system}\n\nReply with ONLY a JSON object (no markdown, no extra text) that matches "
            f"this JSON schema:\n{schema}"
        )
        convo = [{"role": "system", "content": system}, *messages]
        try:
            for attempt in range(2):
                raw = self._request(convo, output_model)
                try:
                    return output_model.model_validate_json(_extract_json(raw))
                except (ValidationError, ValueError) as exc:
                    if attempt == 1:
                        raise CoachError("The model's answer wasn't valid JSON for the expected format. "
                                         "Try a larger or more capable model.") from exc
                    convo += [
                        {"role": "assistant", "content": raw},
                        {"role": "user", "content": f"That reply did not match the schema ({str(exc)[:500]}). "
                                                    "Reply again with ONLY the corrected JSON object."},
                    ]
        except openai.AuthenticationError as exc:
            raise CoachError("The LLM API key is missing or invalid. Set COACH_API_KEY in .env.") from exc
        except openai.NotFoundError as exc:
            raise CoachError(f"Model '{self.settings.coach_model}' not found on the LLM server. "
                             "Check COACH_MODEL (for Ollama, run `ollama pull <model>` first).") from exc
        except openai.RateLimitError as exc:
            raise CoachError("Rate limited by the LLM provider. Try again in a minute.") from exc
        except openai.APIConnectionError as exc:
            raise CoachError(f"Couldn't reach the LLM server at {self.settings.coach_base_url or 'the default URL'}. "
                             "Is it running? Check COACH_BASE_URL.") from exc
        except openai.APIStatusError as exc:
            raise CoachError(f"LLM API error ({exc.status_code}): {exc.message}") from exc
        raise CoachError("The model didn't return an answer.")


def make_llm(settings: Optional[Settings] = None) -> LLMFn:
    """Pick the LLM client from COACH_PROVIDER."""
    settings = settings or get_settings()
    provider = settings.coach_provider.lower()
    if provider == "anthropic":
        return AnthropicCoach(settings)
    if provider in ("openai", "openai_compatible", "ollama"):
        return OpenAICompatibleCoach(settings)
    raise CoachError(f"Unknown COACH_PROVIDER '{settings.coach_provider}'. Use 'anthropic' or 'openai'.")


def _guarded_call(llm: LLMFn, system: str, user_payload: dict, intro: str,
                  output_model: type[BaseModel]) -> tuple[BaseModel, dict]:
    """Call the LLM, check every move it mentions, retry once, then scrub what's left."""
    payload_text = json.dumps(user_payload, indent=1, ensure_ascii=False)
    allowed = san_tokens(payload_text)
    messages = [{"role": "user", "content": f"{intro}\n\n<data>\n{payload_text}\n</data>"}]

    output = llm(system, messages, output_model)
    bad = unknown_moves(output, allowed)
    guard = {"retried": False, "removed_moves": []}
    if bad:
        guard["retried"] = True
        log.info("Coach mentioned unverified moves %s; retrying", sorted(bad))
        messages += [
            {"role": "assistant", "content": output.model_dump_json()},
            {"role": "user", "content": (
                "Your answer mentions moves that are not in the data: "
                f"{', '.join(sorted(bad))}. Rewrite the whole answer using only moves that appear in the data."
            )},
        ]
        output = llm(system, messages, output_model)
        bad = unknown_moves(output, allowed)
        if bad:
            log.warning("Coach still mentioned unverified moves %s; removing them", sorted(bad))
            output = scrub(output, bad)
            guard["removed_moves"] = sorted(bad)
    return output, guard


# ---------- public API ----------

def explain_move(game: Game, move: Move, previous: list[Move], llm: Optional[LLMFn] = None,
                 settings: Optional[Settings] = None) -> dict:
    settings = settings or get_settings()
    llm = llm or make_llm(settings)
    payload = build_move_input(game, move, previous)
    intro = (
        f"Review this {move.classification.replace('_', ' ')} and explain it like a coach. "
        "Fill every field. `better_move` is the engine's best move in SAN. "
        "`alternatives` comments on the other engine lines (may be empty). "
        "Pick 1-3 `theme_tags` that fit the mistake."
    )
    output, guard = _guarded_call(llm, _system(settings), payload, intro, CoachExplanation)
    result = output.model_dump()
    if move.best_move_san and san_tokens(result["better_move"]) - san_tokens(json.dumps(payload)):
        result["better_move"] = move.best_move_san
    if not result["better_move"] and move.best_move_san:
        result["better_move"] = move.best_move_san
    result["_guard"] = guard
    return result


def summarize_game(game: Game, moves: list[Move], notes: dict[int, dict], llm: Optional[LLMFn] = None,
                   settings: Optional[Settings] = None) -> dict:
    settings = settings or get_settings()
    llm = llm or make_llm(settings)
    flagged = []
    for m in moves:
        if m.classification not in classify.FLAGGED:
            continue
        entry = {
            "move": facts_mod.format_line(m.fen_before, [m.san]),
            "by": "student" if m.color == game.my_color else "opponent",
            "classification": m.classification,
            "engine_best_move": m.best_move_san,
            "win_chance_for_mover": f"{round(classify.for_mover(m.win_pct_before, m.color))}% -> "
                                    f"{round(classify.for_mover(m.win_pct_after, m.color))}%",
        }
        note = notes.get(m.id)
        if note:
            entry["coach_note"] = {k: note[k] for k in ("headline", "principle", "theme_tags") if k in note}
        flagged.append(entry)

    payload = {
        "game": build_move_input(game, moves[0], [])["game"] if moves else {},
        "student_colour": game.my_color or "unknown",
        "eval_graph_white_win_pct_after_each_move": [
            f"{facts_mod.format_line(m.fen_before, [m.san])}: {round(m.win_pct_after)}" for m in moves
        ],
        "flagged_moves": flagged,
    }
    intro = (
        "Write a short game review for the student. Give the 2-3 turning points (use moves from the data), "
        "how the game was won or lost, one strength the student showed, their main weakness in this game, "
        "and ONE concrete thing to practise next."
    )
    output, guard = _guarded_call(llm, _system(settings), payload, intro, GameSummaryOut)
    return {**output.model_dump(), "_guard": guard}


_QUESTION_MOVE = re.compile(r"(?<![A-Za-z0-9])(O-O-O|O-O|[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?)[+#]?(?![A-Za-z0-9])")


def check_question_moves(move: Move, question: str, analyse_fn) -> list[dict]:
    """Engine-check every move the user mentions, as an alternative to the played move or as a reply."""
    checked = []
    seen = set()
    for token in _QUESTION_MOVE.findall(question):
        if token in seen:
            continue
        seen.add(token)
        result = {"move": token, "legal": False}
        for label, fen in (("instead of the played move", move.fen_before), ("as a reply after the played move", move.fen_after)):
            board = chess.Board(fen)
            try:
                mv = board.parse_san(token)
            except ValueError:
                continue
            mover = "white" if board.turn == chess.WHITE else "black"
            san = board.san(mv)
            tactics = facts_mod.describe_move(board, mv)
            board.push(mv)
            entry = {"when": label, "as_played": facts_mod.format_line(fen, [san]), "tactics": tactics}
            if board.is_game_over():
                entry["result"] = "checkmate" if board.is_checkmate() else "draw"
            else:
                lines = analyse_fn(board)
                if lines:
                    top = lines[0]
                    entry["engine_line_after_it"] = facts_mod.format_line(board.fen(), top["san"][:8])
                    entry["evaluation_after_it"] = _line_eval_words(top, mover)
            result = {"move": token, "legal": True, **entry}
            break
        checked.append(result)
    return checked


def answer_question(game: Game, move: Move, previous: list[Move], question: str, analyse_fn,
                    note: Optional[dict] = None, llm: Optional[LLMFn] = None,
                    settings: Optional[Settings] = None) -> dict:
    settings = settings or get_settings()
    llm = llm or make_llm(settings)
    checked = check_question_moves(move, question, analyse_fn)
    payload = build_move_input(game, move, previous)
    payload["engine_checked_moves_from_question"] = checked
    if note:
        payload["your_earlier_explanation"] = {k: v for k, v in note.items() if not k.startswith("_")}
    intro = (
        "The student asks a follow-up question about this position. Answer it like a coach in a short "
        "paragraph or two, using only the data. Moves they mention have been checked by the engine in "
        "`engine_checked_moves_from_question`; if one is not legal, say so.\n\n"
        f"Question: {question}"
    )
    output, guard = _guarded_call(llm, _system(settings), payload, intro, AskAnswer)
    return {"answer": output.answer, "checked_moves": checked, "_guard": guard}


# ---------- background job: coach a whole game ----------

def coach_game(game_id: int, refresh: bool = False, llm: Optional[LLMFn] = None) -> None:
    """Generate coach notes for every flagged move, then the game summary."""
    from sqlmodel import Session, delete, select

    from app.db import engine as db_engine
    from app.models import CoachNote, GameSummary

    settings = get_settings()
    with Session(db_engine) as session:
        game = session.get(Game, game_id)
        if not game:
            return
        moves = list(session.exec(select(Move).where(Move.game_id == game_id).order_by(Move.ply)))
        targets = [m for m in moves if m.classification in classify.FLAGGED]
        if settings.coach_only_my_moves and game.my_color:
            targets = [m for m in targets if m.color == game.my_color]

        game.coach_status, game.coach_progress, game.coach_error = "running", 0, None
        session.add(game)
        session.commit()

        try:
            llm = llm or make_llm(settings)
            notes: dict[int, dict] = {}
            failures = []
            for i, m in enumerate(targets):
                existing = session.exec(select(CoachNote).where(CoachNote.move_id == m.id)).first()
                if existing and not refresh:
                    notes[m.id] = existing.explanation
                else:
                    try:
                        explanation = explain_move(game, m, moves[: m.ply - 1], llm, settings)
                    except CoachError as exc:
                        if not notes and i == 0:
                            raise  # first call failed: likely config (key/model); stop early
                        failures.append(f"{m.move_number}{'.' if m.color == 'white' else '...'} {m.san}: {exc}")
                        continue
                    if existing:
                        session.delete(existing)
                    session.add(CoachNote(move_id=m.id, explanation=explanation, model=settings.coach_model))
                    session.add(m)  # facts may have been computed lazily
                    notes[m.id] = explanation
                game.coach_progress = int(90 * (i + 1) / max(len(targets), 1))
                session.add(game)
                session.commit()

            summary = summarize_game(game, moves, notes, llm, settings)
            session.exec(delete(GameSummary).where(GameSummary.game_id == game_id))
            session.add(GameSummary(game_id=game_id, summary=summary, model=settings.coach_model))
            game.coach_status, game.coach_progress = "done", 100
            game.coach_error = ("Some moves failed: " + "; ".join(failures)) if failures else None
        except Exception as exc:
            log.exception("Coaching failed for game %s", game_id)
            session.rollback()
            game = session.get(Game, game_id)
            game.coach_status = "error"
            game.coach_error = str(exc) or exc.__class__.__name__
        session.add(game)
        session.commit()
