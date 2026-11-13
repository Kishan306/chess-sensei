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
from app.services.tags import THEME_TAGS

log = logging.getLogger(__name__)

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


class MasterclassMistake(CoachExplanation):
    master_insight: str  # what a titled player sees here: candidates, plans, structure, prophylaxis


class MoveExplanation(BaseModel):
    """For moves that weren't mistakes (Masterclass mode explains every move)."""
    headline: str
    idea: str  # what the move does and the thinking behind it
    why_it_works: str  # or, for a merely decent move, what it gives up compared with the best
    opponent_options: str  # what the other side can do now, citing the line
    alternatives: list[Alternative]
    plan_ahead: str  # what to aim for over the next few moves
    master_insight: str
    principle: str


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
    usage: Optional[dict] = None


# ---------- prompts ----------

SYSTEM_PROMPT = """You are an experienced, encouraging chess coach reviewing a student's game. \
The student is rated around {rating}.

You receive VERIFIED data: engine lines from Stockfish and position facts computed by a chess \
library. Base every claim on that data.

Rules:
- Only mention moves and lines that appear in the provided data. Never invent variations and never \
state an evaluation that wasn't given. If the data doesn't show something, don't claim it.
- For plans and ideas beyond the given lines, use words ("bring the rook to the d-file", "push the \
f-pawn"), not move notation.
- Explain in human terms: plans, ideas, threats, piece safety and principles. Use numbers sparingly \
("you go from roughly equal to losing a piece", not "-2.31").
- Guess what the player was probably trying to do with the move, then explain why it doesn't work.
- Be specific and concrete: name the pieces and squares involved.
- Write moves in standard algebraic notation (e.g. Nf3, Bxf7+, O-O), with move numbers when citing a line.
- Talk to the student directly ("you"). If the move being reviewed was played by the opponent, \
explain the opponent's mistake and how the student could punish it.
- Be warm but honest; keep each field focused and readable (a few sentences, not an essay)."""


MASTERCLASS_PROMPT = """You are a titled chess player (International Master / Grandmaster strength) \
and an experienced coach, giving a deep, move-by-move masterclass on a student's game. The student \
is rated around {rating} and wants to understand every move: theirs and their opponent's.

You receive VERIFIED data: engine lines from Stockfish and position facts computed by a chess \
library. Base every concrete claim on that data.

Rules:
- Only mention moves and lines that appear in the provided data. Never invent variations and never \
state an evaluation that wasn't given. Your strength shows in the explanation, not in new analysis.
- For plans and ideas beyond the given lines, use words ("bring the rook to the d-file", "prepare \
the f-pawn break"), never move notation. Only moves that appear in the data may be written as moves.
- Think like a strong player: name the candidate moves from the data and why one is preferred, the \
plans for both sides, the pawn structure and what it means, piece placement, prophylaxis (what the \
move prevents), and the critical squares and files.
- Use numbers sparingly ("White keeps a small, stable edge", not "+0.37").
- Be specific: name pieces, squares and the move numbers of lines you cite.
- Talk to the student directly ("you"). For the opponent's moves, explain their idea and what you \
needed to watch for or how you could respond.
- Be honest about mistakes and generous about good moves. Keep each field readable: a few dense, \
insightful sentences rather than an essay."""


def _system(settings: Settings, mode: str = "standard") -> str:
    prompt = MASTERCLASS_PROMPT if mode == "masterclass" else SYSTEM_PROMPT
    return prompt.format(rating=settings.player_rating)


def settings_for(mode: str, settings: Settings) -> Settings:
    """Masterclass can use its own model and a higher effort."""
    if mode != "masterclass":
        return settings
    return settings.model_copy(update={
        "coach_model": settings.masterclass_model or settings.coach_model,
        "coach_effort": settings.masterclass_effort or settings.coach_effort,
    })


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


_LINE_START = re.compile(r"^\d+\.")
_ANY_SAN = re.compile(r"^(O-O-O|O-O|[KQRBN][a-h]?[1-8]?x?[a-h][1-8]|[a-h]x[a-h][1-8](?:=[QRBN])?|[a-h][1-8](?:=[QRBN])?)[+#]?$")


def line_moves(payload) -> set[str]:
    """Every move inside the move lines of the data ("17... Qh5 18. Nh3 e4"), including
    pawn moves that have no move number in front of them."""
    found: set[str] = set()

    def walk(value):
        if isinstance(value, str) and _LINE_START.match(value):
            for token in value.split():
                m = _ANY_SAN.match(token)
                if m:
                    found.add(m.group(1))
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(payload)
    return found


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

# (system, messages, output_model, on_usage=callback) -> parsed output.
# on_usage(model, input_tokens, output_tokens) is called once per API request.
LLMFn = Callable[[str, list[dict], type[BaseModel]], BaseModel]


class AnthropicCoach:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        # With no key in .env the SDK falls back to other credential sources (e.g. `ant auth login`).
        self.client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key or None)

    def __call__(self, system: str, messages: list[dict], output_model: type[BaseModel],
                 on_usage=None) -> BaseModel:
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

        if on_usage and response.usage:
            u = response.usage
            on_usage(getattr(response, "model", None) or self.settings.coach_model,
                     (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0)
                     + (getattr(u, "cache_creation_input_tokens", 0) or 0),
                     u.output_tokens or 0)
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

    def _request(self, messages: list[dict], output_model: type[BaseModel], on_usage=None) -> str:
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
            if on_usage and getattr(response, "usage", None):
                on_usage(self.settings.coach_model, response.usage.prompt_tokens or 0,
                         response.usage.completion_tokens or 0)
            choice = response.choices[0] if response.choices else None
            if not choice or not choice.message or not choice.message.content:
                raise CoachError("The model returned an empty answer.")
            if choice.finish_reason == "length":
                raise CoachError("The model's answer was cut off. Try a model with a larger output limit.")
            return choice.message.content

    def __call__(self, system: str, messages: list[dict], output_model: type[BaseModel],
                 on_usage=None) -> BaseModel:
        schema = json.dumps(output_model.model_json_schema())
        system = (
            f"{system}\n\nReply with ONLY a JSON object (no markdown, no extra text) that matches "
            f"this JSON schema:\n{schema}"
        )
        convo = [{"role": "system", "content": system}, *messages]
        try:
            for attempt in range(2):
                raw = self._request(convo, output_model, on_usage)
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


class UsageMeter:
    """Adds up the tokens and dollars of the LLM calls behind one explanation."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.calls = self.input_tokens = self.output_tokens = 0
        self.cost: Optional[float] = 0.0

    def __call__(self, model: str, input_tokens: int, output_tokens: int) -> None:
        from app.services.pricing import cost_usd

        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        c = cost_usd(model, input_tokens, output_tokens, self.settings)
        self.cost = None if c is None or self.cost is None else self.cost + c

    def as_dict(self) -> dict:
        return {"calls": self.calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cost_usd": round(self.cost, 6) if self.cost is not None else None}


# Fields where an unverified move matters enough to pay for a second call.
# (An unverified `better_move` is simply replaced with the engine's best move.)
_KEY_FIELDS = ("headline",)


def _needs_retry(output: BaseModel, bad: set[str]) -> bool:
    data = output.model_dump()
    if any(san_tokens(str(data.get(f, ""))) & bad for f in _KEY_FIELDS):
        return True
    before = len(output.model_dump_json())
    after = len(scrub(output, bad).model_dump_json())
    return after < before * 0.75  # cutting the sentences would lose more than a quarter of the answer


def _guarded_call(llm: LLMFn, system: str, user_payload: dict, intro: str,
                  output_model: type[BaseModel], settings: Optional[Settings] = None) -> tuple[BaseModel, dict]:
    """Call the LLM and check every move it mentions.

    Sentences with unverified moves are removed. Only when that would damage the answer
    (a key field, or a big part of the text) is the model asked once to rewrite it.
    """
    meter = UsageMeter(settings or get_settings())
    payload_text = json.dumps(user_payload, indent=1, ensure_ascii=False)
    allowed = san_tokens(payload_text) | line_moves(user_payload)
    messages = [{"role": "user", "content": f"{intro}\n\n<data>\n{payload_text}\n</data>"}]
    guard = {"retried": False, "removed_moves": []}
    try:
        output = llm(system, messages, output_model, on_usage=meter)
        bad = unknown_moves(output, allowed)
        if bad and _needs_retry(output, bad):
            guard["retried"] = True
            log.info("Coach mentioned unverified moves %s in a key part; retrying", sorted(bad))
            messages += [
                {"role": "assistant", "content": output.model_dump_json()},
                {"role": "user", "content": (
                    "Your answer mentions moves that are not in the data: "
                    f"{', '.join(sorted(bad))}. Rewrite the whole answer using only moves that appear in the data; "
                    "describe other ideas in words."
                )},
            ]
            output = llm(system, messages, output_model, on_usage=meter)
            bad = unknown_moves(output, allowed)
        if bad:
            log.info("Removing sentences with unverified moves %s", sorted(bad))
            output = scrub(output, bad)
            guard["removed_moves"] = sorted(bad)
    except CoachError as exc:
        exc.usage = meter.as_dict()  # calls that failed may still have been billed
        raise
    guard["usage"] = meter.as_dict()
    return output, guard


# ---------- public API ----------

def explain_move(game: Game, move: Move, previous: list[Move], llm: Optional[LLMFn] = None,
                 settings: Optional[Settings] = None, mode: str = "standard") -> dict:
    settings = settings_for(mode, settings or get_settings())
    llm = llm or make_llm(settings)
    payload = build_move_input(game, move, previous)
    mistake = move.classification in classify.FLAGGED

    if mistake:
        intro = (
            f"Review this {move.classification.replace('_', ' ')} and explain it like a coach. "
            "Fill every field. `better_move` is the engine's best move in SAN. "
            "`alternatives` comments on the other engine lines (may be empty). "
            "Pick 1-3 `theme_tags` that fit the mistake."
        )
        if mode == "masterclass":
            intro += " `master_insight` is what a titled player sees in this position that the student missed."
        model = MasterclassMistake if mode == "masterclass" else CoachExplanation
    else:
        intro = (
            f"Explain this move (rated '{move.classification}') in depth, as part of a move-by-move masterclass. "
            "Fill every field. `why_it_works`: why it is good, or, if it is only decent, what it gives up "
            "compared with the engine's best move. `opponent_options`: the other side's best continuation from "
            "the data. `alternatives` comments on the other engine lines (may be empty). "
            "`master_insight` is the deeper point a titled player would make."
        )
        model = MoveExplanation

    output, guard = _guarded_call(llm, _system(settings, mode), payload, intro, model, settings)
    result = output.model_dump()
    result["_usage"] = guard.pop("usage")
    result["kind"] = "mistake" if mistake else "move"
    if mistake:
        if move.best_move_san and san_tokens(result["better_move"]) - san_tokens(json.dumps(payload)):
            result["better_move"] = move.best_move_san
        if not result["better_move"] and move.best_move_san:
            result["better_move"] = move.best_move_san
    result["_guard"] = guard
    result["_mode"] = mode
    return result


def summarize_game(game: Game, moves: list[Move], notes: dict[int, dict], llm: Optional[LLMFn] = None,
                   settings: Optional[Settings] = None, mode: str = "standard") -> dict:
    settings = settings_for(mode, settings or get_settings())
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
    if mode == "masterclass":
        intro += (" This is a masterclass review: explain the strategic story of the game (plans, structures, "
                  "the moments where the balance shifted) the way a titled player would.")
    output, guard = _guarded_call(llm, _system(settings, mode), payload, intro, GameSummaryOut, settings)
    usage = guard.pop("usage")
    return {**output.model_dump(), "_guard": guard, "_mode": mode, "_usage": usage}


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
                    settings: Optional[Settings] = None, mode: str = "standard") -> dict:
    settings = settings_for(mode, settings or get_settings())
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
    output, guard = _guarded_call(llm, _system(settings, mode), payload, intro, AskAnswer, settings)
    usage = guard.pop("usage")
    return {"answer": output.answer, "checked_moves": checked, "_guard": guard, "_usage": usage}


# ---------- background job: coach a whole game ----------

def add_usage(game: Game, usage: Optional[dict]) -> None:
    """Add one explanation's tokens and cost to the game's running total."""
    if not usage:
        return
    game.coach_calls = (game.coach_calls or 0) + usage["calls"]
    game.coach_input_tokens = (game.coach_input_tokens or 0) + usage["input_tokens"]
    game.coach_output_tokens = (game.coach_output_tokens or 0) + usage["output_tokens"]
    if usage["cost_usd"] is None or game.coach_cost_usd is None:
        game.coach_cost_usd = None if usage["calls"] else game.coach_cost_usd
    else:
        game.coach_cost_usd = round(game.coach_cost_usd + usage["cost_usd"], 6)


# ---------- trivial moves (Masterclass explains them without an LLM call) ----------

def trivial_note(move: Move, previous: Optional[Move]) -> Optional[dict]:
    """A short, free note for moves that need no deep explanation, or None."""
    if move.classification in classify.FLAGGED:
        return None
    board = chess.Board(move.fen_before)
    label = facts_mod.format_line(move.fen_before, [move.san])
    text = None
    if move.classification == "book":
        headline, text = "Book move", (f"{label} is standard opening theory: a well-known, sound move in this "
                                       "position. The interesting decisions come later.")
    elif board.legal_moves.count() == 1:
        headline, text = "The only legal move", f"{label} was forced: it was the only legal move."
    elif previous is not None and board.is_capture(chess.Move.from_uci(move.uci)):
        prev_to = previous.uci[2:4]
        if (move.uci[2:4] == prev_to and chess.Board(previous.fen_before).is_capture(chess.Move.from_uci(previous.uci))
                and move.classification in ("best", "excellent")):
            choice = "the engine's top choice" if move.classification == "best" else "one of the engine's top choices"
            headline, text = "Recapture", f"{label} takes back on {prev_to}, restoring the material balance. It was {choice}."
    if text is None:
        return None
    return {"kind": "brief", "headline": headline, "text": text, "_mode": "masterclass",
            "_usage": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}}


def coach_targets(game: Game, moves: list[Move], mode: str, settings: Settings) -> list[Move]:
    """Moves to explain: every move in Masterclass, otherwise the (student's) mistakes."""
    if mode == "masterclass":
        return list(moves)
    targets = [m for m in moves if m.classification in classify.FLAGGED]
    if settings.coach_only_my_moves and game.my_color:
        targets = [m for m in targets if m.color == game.my_color]
    return targets


def note_is_current(note, mode: str) -> bool:
    """A Masterclass review replaces standard notes; a standard review keeps any note."""
    return mode != "masterclass" or note.mode == "masterclass"


def _label(m: Move) -> str:
    return f"{m.move_number}{'.' if m.color == 'white' else '...'} {m.san}"


def coach_game(game_id: int, refresh: bool = False, llm: Optional[LLMFn] = None, mode: str = "standard") -> None:
    """Generate coach notes for the target moves (in parallel), then the game summary.

    Stops early (keeping what's done) when the game's status is set to "stopping".
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from sqlmodel import Session, delete, select

    from app.db import engine as db_engine
    from app.models import CoachNote, GameSummary

    settings = get_settings()
    run_settings = settings_for(mode, settings)
    with Session(db_engine) as session:
        game = session.get(Game, game_id)
        if not game or game.coach_status in ("stopping", "stopped"):
            return
        moves = list(session.exec(select(Move).where(Move.game_id == game_id).order_by(Move.ply)))
        targets = coach_targets(game, moves, mode, settings)
        existing = {
            n.move_id: n for n in session.exec(select(CoachNote).where(CoachNote.move_id.in_([m.id for m in moves])))
        }

        game.coach_status, game.coach_progress, game.coach_error = "running", 0, None
        session.add(game)
        session.commit()

        def save_note(m: Move, explanation: dict, model: str) -> None:
            if m.id in existing:
                session.delete(existing.pop(m.id))
                session.flush()
            session.add(CoachNote(move_id=m.id, explanation=explanation, model=model, mode=mode))
            notes[m.id] = explanation

        try:
            notes: dict[int, dict] = {mid: n.explanation for mid, n in existing.items()}
            todo = [m for m in targets if refresh or m.id not in existing or not note_is_current(existing[m.id], mode)]

            # Trivial moves get a free short note instead of an LLM call
            if mode == "masterclass":
                by_ply = {m.ply: m for m in moves}
                for m in list(todo):
                    brief = trivial_note(m, by_ply.get(m.ply - 1))
                    if brief:
                        save_note(m, brief, "none")
                        todo.remove(m)
            for m in todo:
                ensure_facts(m)  # computed here, so worker threads only read the moves
                session.add(m)
            done = len(targets) - len(todo)
            game.coach_progress = int(90 * done / max(len(targets), 1))
            session.add(game)
            session.commit()

            # Worker threads get plain copies: session objects get expired/reloaded on every
            # commit, which isn't safe to do from several threads.
            def detached(obj):
                # getattr loads expired fields; model_dump alone would skip them
                return type(obj).model_validate({f: getattr(obj, f) for f in type(obj).model_fields})

            game_copy = detached(game)
            move_copies = [detached(m) for m in moves]
            by_id = {c.id: c for c in move_copies}

            llm = llm or make_llm(run_settings) if todo else llm
            failures, successes, stopped = [], 0, False
            pool = ThreadPoolExecutor(max_workers=max(1, settings.coach_concurrency))
            try:
                futures = {
                    pool.submit(explain_move, game_copy, by_id[m.id], move_copies[: m.ply - 1], llm, settings, mode): m
                    for m in todo
                }
                for future in as_completed(futures):
                    if future.cancelled():
                        continue
                    m = futures[future]
                    try:
                        explanation = future.result()
                    except CoachError as exc:
                        add_usage(game, exc.usage)
                        failures.append(f"{_label(m)}: {exc}")
                        if len(failures) >= 3 and successes == 0:
                            # Nothing has worked yet: most likely a config problem (key, model, server)
                            for f in futures:
                                f.cancel()
                            raise CoachError(str(exc)) from exc
                    else:
                        save_note(m, explanation, run_settings.coach_model)
                        add_usage(game, explanation.get("_usage"))
                        successes += 1
                        done += 1
                    game.coach_progress = int(90 * done / max(len(targets), 1))
                    session.add(game)
                    session.commit()
                    # Stop requested from the UI? Cancel the calls that haven't started yet.
                    session.refresh(game)
                    if game.coach_status == "stopping" and not stopped:
                        stopped = True
                        # Cancel each waiting call. (pool.shutdown(cancel_futures=True) would drop
                        # them without telling as_completed, which then waits forever.)
                        for f in futures:
                            f.cancel()
            finally:
                pool.shutdown(wait=True)

            session.refresh(game)
            if stopped or game.coach_status == "stopping":
                game.coach_status = "stopped"
                game.coach_error = f"Stopped: {done} of {len(targets)} moves explained. Start again to continue."
            elif todo and not successes:
                raise CoachError(failures[0].split(": ", 1)[-1])
            else:
                summary = summarize_game(game, moves, notes, llm or make_llm(run_settings), settings, mode)
                add_usage(game, summary.get("_usage"))
                session.exec(delete(GameSummary).where(GameSummary.game_id == game_id))
                session.add(GameSummary(game_id=game_id, summary=summary, model=run_settings.coach_model))
                game.coach_status, game.coach_progress, game.coach_mode = "done", 100, mode
                game.coach_error = (f"{len(failures)} move(s) couldn't be explained: " + "; ".join(failures[:5])) if failures else None
        except Exception as exc:
            log.exception("Coaching failed for game %s", game_id)
            usage = getattr(exc, "usage", None)
            session.rollback()
            game = session.get(Game, game_id)
            game.coach_status = "error"
            game.coach_error = str(exc) or exc.__class__.__name__
            add_usage(game, usage)
        session.add(game)
        session.commit()


def coach_all(game_ids: list[int], mode: str, llm: Optional[LLMFn] = None) -> None:
    """Background job for several games: analyse each one if needed, then review it.
    Games stopped while waiting are skipped."""
    from sqlmodel import Session

    from app.db import engine as db_engine
    from app.services.engine_service import analyze_game

    def still_queued(game_id: int) -> bool:
        with Session(db_engine) as session:
            game = session.get(Game, game_id)
            return bool(game) and game.coach_status == "queued"

    for game_id in game_ids:
        if not still_queued(game_id):
            continue
        with Session(db_engine) as session:
            needs_analysis = session.get(Game, game_id).analysis_status != "done"
        if needs_analysis:
            analyze_game(game_id)
            with Session(db_engine) as session:
                game = session.get(Game, game_id)
                if game.analysis_status != "done":
                    game.coach_status, game.coach_error = "error", "Engine analysis failed; see the game's status."
                    session.add(game)
                    session.commit()
                    continue
        if still_queued(game_id):
            coach_game(game_id, llm=llm, mode=mode)


def estimate_calls(game: Game, moves: list[Move], notes: dict, mode: str, settings: Settings) -> int:
    """LLM calls a review would make (one per move still to explain, plus the summary).
    Trivial moves in Masterclass are free."""
    if not moves:  # not analysed yet: count moves from the PGN
        import io

        import chess.pgn
        g = chess.pgn.read_game(io.StringIO(game.pgn_raw))
        n = len(list(g.mainline_moves())) if g else 0
        return (int(n * 0.8) if mode == "masterclass" else max(1, n // 8)) + 1
    by_ply = {m.ply: m for m in moves}
    calls = 0
    for m in coach_targets(game, moves, mode, settings):
        if m.id in notes and note_is_current(notes[m.id], mode):
            continue
        if mode == "masterclass" and trivial_note(m, by_ply.get(m.ply - 1)):
            continue
        calls += 1
    return calls + 1


# Rough tokens per call, used for a dollar estimate before any history exists
_GUESS_TOKENS = {"standard": (6000, 1500), "masterclass": (7000, 2500)}


def estimate_cost(calls: int, mode: str, history: list[dict], settings: Settings) -> tuple[Optional[float], str]:
    """Dollar estimate: average cost per call of past notes in this mode, or a rough guess."""
    from app.services.pricing import cost_usd

    paid = [u for u in history if u and u.get("calls") and u.get("cost_usd") is not None]
    if paid:
        per_call = sum(u["cost_usd"] for u in paid) / sum(u["calls"] for u in paid)
        return round(per_call * calls, 2), "your past reviews"
    model = settings_for(mode, settings).coach_model
    tokens_in, tokens_out = _GUESS_TOKENS[mode]
    per_call = cost_usd(model, tokens_in, tokens_out, settings)
    return (round(per_call * calls, 2), "a rough guess") if per_call is not None else (None, "unknown price")
