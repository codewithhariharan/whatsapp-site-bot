"""Message classification / JSON-extraction logic.

The Claude API call is replaced with a stub, so these tests exercise the real
response handling (code-fence stripping + JSON parsing) without a network call.
"""
from types import SimpleNamespace

import pytest

import message_parser as mp


def _stub_response(text: str):
    """Mimic the shape of an anthropic Messages response: .content[0].text."""
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


@pytest.fixture
def fake_claude(monkeypatch):
    """Make message_parser.client.messages.create return a canned string."""
    def _install(reply_text: str):
        monkeypatch.setattr(
            mp.client.messages, "create",
            lambda *a, **k: _stub_response(reply_text),
        )
    return _install


def test_parses_plain_json_log(fake_claude):
    fake_claude('{"type": "log", "data": {"main_location": "Zone 3", '
                '"sub_location": "GL A-B", "description": "Honeycomb works", '
                '"manpower": "Worker - 1"}}')
    result = mp.classify_and_parse("Zone 3 honeycomb works, 1 worker")
    assert result["type"] == "log"
    assert result["data"]["main_location"] == "Zone 3"


def test_strips_markdown_code_fences(fake_claude):
    # Models often wrap JSON in ```json ... ``` — that must be tolerated.
    fake_claude('```json\n{"type": "ignore"}\n```')
    result = mp.classify_and_parse("good morning team")
    assert result == {"type": "ignore"}


def test_parses_query_type(fake_claude):
    fake_claude('{"type": "query", "query": "When was Panel 39 cast?"}')
    result = mp.classify_and_parse("when was panel 39 cast?")
    assert result["type"] == "query"
    assert result["query"] == "When was Panel 39 cast?"


def test_parses_dwall_entry(fake_claude):
    fake_claude('{"type": "dwall", "data": {"panel_number": "CN284A", '
                '"entry_number": "Ent-2"}}')
    result = mp.classify_and_parse("Ent-2 - Panel No. CN284A")
    assert result["type"] == "dwall"
    assert result["data"]["panel_number"] == "CN284A"
