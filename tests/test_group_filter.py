"""The bot serves one group only.

The Baileys bridge filters on the group name first; this is the Python half of
that gate, which stops anything the bridge lets through from reaching the
database. Background tasks are captured rather than run, so an accepted message
is observable without touching Supabase or Claude.
"""
import pytest
from fastapi.testclient import TestClient

import main


SERVED_GROUP = "CR106 LTA PJT (Site Work)"
SECRET = "test-bridge-secret"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main.settings, "BRIDGE_SHARED_SECRET", SECRET)
    monkeypatch.setattr(main.settings, "ALLOWED_GROUP_NAME", SERVED_GROUP)
    return TestClient(main.app)


@pytest.fixture
def handled(monkeypatch):
    """Record the messages that make it through to the handler."""
    calls = []

    async def _fake(group_id, sender_name, sender_number, text, group_name=None):
        calls.append((group_id, group_name, text))

    monkeypatch.setattr(main, "handle_message", _fake)
    return calls


def _post(client, **overrides):
    body = {
        "group_id": "1234@g.us",
        "group_name": SERVED_GROUP,
        "sender_name": "Engineer",
        "sender_number": "6591234567",
        "text": "Rebar fixing at Shaft B",
    }
    body.update(overrides)
    return client.post(
        "/baileys/incoming", json=body, headers={"X-Bridge-Secret": SECRET}
    )


def test_served_group_is_handled(client, handled):
    assert _post(client).json() == {"status": "ok"}
    assert handled == [("1234@g.us", SERVED_GROUP, "Rebar fixing at Shaft B")]


def test_other_group_is_dropped(client, handled):
    resp = _post(client, group_name="Some Other Site Group", group_id="9999@g.us")
    assert resp.json() == {"status": "ignored"}
    assert handled == []


def test_group_name_match_ignores_case_and_padding(client, handled):
    # WhatsApp subjects come back with the user's own capitalisation and can
    # pick up stray whitespace; neither should take the bot offline.
    assert _post(client, group_name="  cr106 lta pjt (site work)  ").json() == {
        "status": "ok"
    }
    assert len(handled) == 1


def test_missing_group_name_is_dropped(client, handled):
    # An older bridge that doesn't send group_name can't be trusted to have
    # filtered, so its messages are refused rather than logged blind.
    assert _post(client, group_name=None).json() == {"status": "ignored"}
    assert handled == []


def test_blank_setting_serves_every_group(client, handled, monkeypatch):
    monkeypatch.setattr(main.settings, "ALLOWED_GROUP_NAME", "")
    assert _post(client, group_name="Anything At All").json() == {"status": "ok"}
    assert len(handled) == 1


def test_wrong_secret_is_rejected_before_the_group_check(client, handled):
    resp = client.post(
        "/baileys/incoming",
        json={"group_id": "1234@g.us", "group_name": SERVED_GROUP, "text": "hi"},
        headers={"X-Bridge-Secret": "wrong"},
    )
    assert resp.status_code == 403
    assert handled == []
