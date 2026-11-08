"""OpenAI-compatible provider (OpenAI, Gemini, Groq, OpenRouter, Ollama, LM Studio) with a fake server."""
from types import SimpleNamespace

import httpx2
import openai
import pytest

from app.config import Settings
from app.services import coach_service as C


def _reply(content, finish="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)])


def _bad_request():
    req = httpx2.Request("POST", "http://local/v1/chat/completions")
    return openai.BadRequestError("response_format not supported", response=httpx2.Response(400, request=req), body=None)


def _coach(fake_create):
    coach = C.OpenAICompatibleCoach(Settings(coach_provider="openai", coach_model="m",
                                             coach_base_url="http://localhost:11434/v1"))
    coach.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    return coach


ANSWER = '{"answer": "Nc6 develops a piece."}'


def test_falls_back_to_simpler_format_and_strips_think_and_fences():
    calls = []

    def create(**kwargs):
        calls.append(kwargs.get("response_format", {}).get("type"))
        if kwargs.get("response_format", {}).get("type") == "json_schema":
            raise _bad_request()
        return _reply(f"<think>let me think</think>\n```json\n{ANSWER}\n```")

    coach = _coach(create)
    out = coach("system", [{"role": "user", "content": "q"}], C.AskAnswer)
    assert out.answer == "Nc6 develops a piece."
    assert calls == ["json_schema", "json_object"]
    # The working mode is remembered for the next call
    coach("system", [{"role": "user", "content": "q"}], C.AskAnswer)
    assert calls[-1] == "json_object"


def test_schema_goes_in_the_system_prompt():
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        return _reply(ANSWER)

    _coach(create)("Be a coach.", [{"role": "user", "content": "q"}], C.AskAnswer)
    assert seen["messages"][0]["role"] == "system"
    assert '"answer"' in seen["messages"][0]["content"]
    assert seen["model"] == "m"


def test_invalid_json_is_retried_once():
    replies = iter([_reply("Sure! The answer is that Nc6 is good."), _reply(ANSWER)])
    out = _coach(lambda **kw: next(replies))("s", [{"role": "user", "content": "q"}], C.AskAnswer)
    assert out.answer.startswith("Nc6")


def test_invalid_json_twice_raises_friendly_error():
    with pytest.raises(C.CoachError, match="valid JSON"):
        _coach(lambda **kw: _reply("not json"))("s", [{"role": "user", "content": "q"}], C.AskAnswer)


def test_cut_off_answer():
    with pytest.raises(C.CoachError, match="cut off"):
        _coach(lambda **kw: _reply('{"answer": "Nc', finish="length"))("s", [{"role": "user", "content": "q"}], C.AskAnswer)


def test_connection_error_mentions_base_url():
    def create(**kw):
        raise openai.APIConnectionError(request=httpx2.Request("POST", "http://localhost:11434/v1"))

    with pytest.raises(C.CoachError, match="localhost:11434"):
        _coach(create)("s", [{"role": "user", "content": "q"}], C.AskAnswer)


def test_make_llm_picks_provider():
    assert isinstance(C.make_llm(Settings(coach_provider="anthropic", anthropic_api_key="x")), C.AnthropicCoach)
    assert isinstance(C.make_llm(Settings(coach_provider="openai")), C.OpenAICompatibleCoach)
    with pytest.raises(C.CoachError):
        C.make_llm(Settings(coach_provider="nope"))
