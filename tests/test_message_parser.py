"""Message classification / JSON-extraction logic.

The Gemini call is replaced with a stub, so these tests exercise the real
response handling (code-fence stripping + JSON parsing) without a network call.
"""
import pytest

import message_parser as mp


@pytest.fixture
def fake_model(monkeypatch):
    """Make llm.generate return a canned string."""
    def _install(reply_text: str):
        monkeypatch.setattr(mp.llm, "generate", lambda *a, **k: reply_text)
    return _install


def test_parses_plain_json_log(fake_model):
    fake_model('{"type": "log", "data": {"main_location": "Zone 3", '
                '"sub_location": "GL A-B", "description": "Honeycomb works", '
                '"manpower": "Worker - 1"}}')
    result = mp.classify_and_parse("Zone 3 honeycomb works, 1 worker")
    assert result["type"] == "log"
    assert result["data"]["main_location"] == "Zone 3"


def test_strips_markdown_code_fences(fake_model):
    # Models often wrap JSON in ```json ... ``` — that must be tolerated.
    fake_model('```json\n{"type": "ignore"}\n```')
    result = mp.classify_and_parse("good morning team")
    assert result == {"type": "ignore"}


def test_parses_query_type(fake_model):
    fake_model('{"type": "query", "query": "When was Panel 39 cast?"}')
    result = mp.classify_and_parse("when was panel 39 cast?")
    assert result["type"] == "query"
    assert result["query"] == "When was Panel 39 cast?"


def test_parses_dwall_entry(fake_model):
    fake_model('{"type": "dwall", "data": {"panel_number": "CN284A", '
                '"entry_number": "Ent-2"}}')
    result = mp.classify_and_parse("Ent-2 - Panel No. CN284A")
    assert result["type"] == "dwall"
    assert result["data"]["panel_number"] == "CN284A"


def test_the_prompt_rules_out_safety_notices(monkeypatch):
    # Gemini filed three safety circulars as logs in the first comparison run.
    seen = {}

    def fake_generate(prompt, **kwargs):
        seen["prompt"] = prompt
        return '{"type": "ignore"}'

    monkeypatch.setattr(mp.llm, "generate", fake_generate)
    mp.classify_and_parse("Please conduct a safety time-out")
    assert "safety notices" in seen["prompt"]
    assert '"U3 GL10-14 (South side)" -> main_location "U3"' in seen["prompt"]
