"""The Gemini wrapper: token headroom, empty replies, and what counts as transient."""
from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors

import llm


def _response(text, finish_reason="STOP"):
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(finish_reason=finish_reason)],
    )


@pytest.fixture
def sent(monkeypatch):
    calls = {}

    def fake_generate_content(*, model, contents, config):
        calls.update(model=model, contents=contents, config=config)
        return calls.get("reply", _response("  hello  "))

    monkeypatch.setattr(llm.client.models, "generate_content", fake_generate_content)
    return calls


def test_reply_text_is_stripped(sent):
    assert llm.generate("q", model="m", max_tokens=100) == "hello"
    assert sent["model"] == "m"
    assert sent["contents"] == "q"


def test_thinking_gets_room_on_top_of_the_reply(sent):
    # Thinking tokens count against max_output_tokens on Gemini 3.x; a cap sized
    # for the reply alone would be spent thinking and come back empty.
    llm.generate("q", model="m", max_tokens=400)
    assert sent["config"].max_output_tokens > 400


def test_thinking_is_held_inside_the_allowance_on_2_5(sent):
    # Automatic thinking ate the reply's share too and cut a day's listing short.
    llm.generate("q", model="gemini-2.5-flash", max_tokens=400)
    config = sent["config"]
    assert config.thinking_config.thinking_budget == llm._THINKING_ALLOWANCE
    assert config.max_output_tokens - config.thinking_config.thinking_budget >= 400


def test_other_models_keep_their_default_thinking(sent):
    # 3.x takes a level, not a budget; sending a budget is a 400.
    llm.generate("q", model="gemini-3-flash", max_tokens=400)
    assert sent["config"].thinking_config is None


def test_system_and_json_mode_are_passed_through(sent):
    llm.generate("q", model="m", max_tokens=10, system="rules", json_output=True)
    assert sent["config"].system_instruction == "rules"
    assert sent["config"].response_mime_type == "application/json"


def test_plain_text_by_default(sent):
    llm.generate("q", model="m", max_tokens=10)
    assert sent["config"].response_mime_type is None
    assert sent["config"].system_instruction is None


@pytest.mark.parametrize("text", [None, "", "   "])
def test_an_empty_reply_raises_instead_of_returning_nothing(sent, text):
    sent["reply"] = _response(text, finish_reason="MAX_TOKENS")
    with pytest.raises(llm.EmptyReply, match="MAX_TOKENS"):
        llm.generate("q", model="m", max_tokens=10)


def test_a_reply_cut_at_the_cap_carries_the_callers_note(sent):
    sent["reply"] = _response("• Zone 3 — rebar", finish_reason="MAX_TOKENS")
    got = llm.generate("q", model="m", max_tokens=10, cut_off_note=" [cut]")
    assert got == "• Zone 3 — rebar [cut]"


def test_a_complete_reply_carries_no_note(sent):
    assert llm.generate("q", model="m", max_tokens=10, cut_off_note=" [cut]") == "hello"


def test_a_cut_reply_is_left_alone_without_a_note(sent):
    # The parsers read the reply as JSON; nothing may be appended for them.
    sent["reply"] = _response('{"type": "log"', finish_reason="MAX_TOKENS")
    assert llm.generate("q", model="m", max_tokens=10) == '{"type": "log"'


@pytest.mark.parametrize("exc, expected", [
    (errors.ServerError(503, {"error": {"message": "overloaded"}}), True),
    (errors.ServerError(500, {"error": {"message": "internal"}}), True),
    (errors.ClientError(429, {"error": {"message": "quota"}}), True),
    (errors.ClientError(400, {"error": {"message": "bad request"}}), False),
    (errors.ClientError(403, {"error": {"message": "key denied"}}), False),
    (errors.ClientError(404, {"error": {"message": "no such model"}}), False),
    (httpx.ConnectError("refused"), True),
    (ValueError("bad json"), False),
])
def test_is_transient(exc, expected):
    assert llm.is_transient(exc) is expected
