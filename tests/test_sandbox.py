"""The dashboard console must never reach the outside world. See sandbox.py."""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import dashboard
import database
import mailer
import sandbox
import whatsapp_client
from config import settings

SITE = "120363021760406818@g.us"
MASTER = "120363411944982031@g.us"


@pytest.fixture
def in_console():
    box = []
    t1 = sandbox.outbox.set(box)
    t2 = sandbox.state_group.set("dashboard:7")
    yield box
    sandbox.state_group.reset(t2)
    sandbox.outbox.reset(t1)


@pytest.fixture
def no_bridge(monkeypatch):
    async def refuse(*a, **k):
        pytest.fail("reached the WhatsApp bridge")
    monkeypatch.setattr(whatsapp_client, "_bridge_post", refuse)


def test_messages_and_files_are_captured(in_console, no_bridge):
    asyncio.run(whatsapp_client.send_message(SITE, "hello"))
    asyncio.run(whatsapp_client.send_document(SITE, b"PK..", "a.xlsx", "cap"))
    assert in_console == [
        {"type": "text", "text": "hello"},
        {"type": "file", "filename": "a.xlsx", "data": b"PK..", "caption": "cap"},
    ]


def test_outside_the_console_messages_go_to_the_bridge(monkeypatch):
    sent = []

    async def fake(path, payload, timeout=30):
        sent.append(path)
    monkeypatch.setattr(whatsapp_client, "_bridge_post", fake)
    asyncio.run(whatsapp_client.send_message(SITE, "hello"))
    assert sent == ["/send"]


def test_email_is_captured_not_sent(in_console, monkeypatch):
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(mailer.smtplib, "SMTP", lambda *a, **k: pytest.fail("sent an email"))
    mailer.send_email(["a@b.sg"], "Subj", "body", "<p>body</p>")
    assert in_console == [{"type": "email", "to": ["a@b.sg"], "subject": "Subj",
                           "text": "body", "html": "<p>body</p>", "attachments": []}]


def test_captured_in_threads_too(in_console, no_bridge):
    # Handlers hand blocking work to asyncio.to_thread; the context must follow.
    async def run():
        await asyncio.to_thread(sandbox.capture, {"type": "text", "text": "from a thread"})
    asyncio.run(run())
    assert in_console == [{"type": "text", "text": "from a thread"}]


def test_state_goes_under_the_testers_key(in_console, monkeypatch):
    calls = []
    monkeypatch.setattr(database, "_execute", lambda q, p=(): calls.append(p))
    monkeypatch.setattr(database, "_fetch", lambda q, p=(): calls.append(p) or [])
    database.save_reorder_session(SITE, __import__("datetime").date(2026, 11, 3), [])
    database.get_reorder_session(SITE)
    database.clear_reorder_session(SITE)
    assert [p[0] for p in calls] == ["dashboard:7"] * 3


def test_location_order_falls_back_to_the_groups(in_console, monkeypatch):
    def fetch(q, p=()):
        return [{"location_name": "U3"}] if p == (SITE,) else []
    monkeypatch.setattr(database, "_fetch", fetch)
    assert database.get_location_order(SITE) == ["U3"]


def test_testers_own_order_wins(in_console, monkeypatch):
    def fetch(q, p=()):
        return [{"location_name": "Mine"}] if p == ("dashboard:7",) else [{"location_name": "U3"}]
    monkeypatch.setattr(database, "_fetch", fetch)
    assert database.get_location_order(SITE) == ["Mine"]


def test_outside_the_console_state_is_the_groups(monkeypatch):
    calls = []
    monkeypatch.setattr(database, "_fetch", lambda q, p=(): calls.append(p) or [])
    database.get_reorder_session(SITE)
    assert calls == [(SITE,)]


def test_nothing_is_queued_as_a_post(in_console, monkeypatch):
    monkeypatch.setattr(database, "_execute", lambda *a: pytest.fail("queued a post"))
    with pytest.raises(RuntimeError):
        database.enqueue_message(SITE, "Tester", "", "U3, slab, casting")


def test_catch_up_is_skipped(in_console, monkeypatch):
    import ingest_batch

    async def refuse(**k):
        pytest.fail("ran a batch")
    monkeypatch.setattr(ingest_batch, "run_batch", refuse)
    asyncio.run(ingest_batch.catch_up(SITE))


# ── The endpoint ──────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "DASHBOARD_SITE_GROUP_ID", SITE)
    monkeypatch.setattr(settings, "MASTER_GROUP_IDS", MASTER)
    monkeypatch.setattr(dashboard, "_console_limiter", dashboard.RateLimiter(100, 600, 1000))
    app = FastAPI()
    app.include_router(dashboard.router)
    app.dependency_overrides[dashboard.current_user] = lambda: {
        "id": 7, "email": "t@lta.gov.sg", "name": "Tester", "is_admin": False,
        "must_change_password": False}
    return TestClient(app)


@pytest.mark.parametrize("text", ["U3, slab, casting in progress", "/dwall CN281A 26m", "hello"])
def test_plain_posts_are_refused(client, monkeypatch, text):
    import message_handler
    monkeypatch.setattr(message_handler, "handle_message",
                        lambda *a, **k: pytest.fail("reached the handler"))
    r = client.post("/api/console", json={"text": text})
    assert r.status_code == 400


def test_a_command_runs_and_its_replies_come_back(client, monkeypatch, no_bridge):
    import message_handler
    seen = {}

    async def fake(group_id, sender_name, sender_number, text, **k):
        seen.update(group=group_id, state=sandbox.state_group.get(), text=text)
        await whatsapp_client.send_message(group_id, "👀 *Preview*")
        await whatsapp_client.send_document(group_id, b"PK", "r.xlsx", "")

    monkeypatch.setattr(message_handler, "handle_message", fake)
    r = client.post("/api/console", json={"text": "/daily"})
    assert r.status_code == 200
    assert seen == {"group": SITE, "state": "dashboard:7", "text": "/daily"}
    replies = r.json()["replies"]
    assert replies[0] == {"type": "text", "text": "👀 *Preview*"}
    assert replies[1]["type"] == "file" and replies[1]["data"] == "UEs="
    # The context is gone once the request is.
    assert not sandbox.active() and sandbox.state_group.get() is None


def test_tunnel_scope_acts_as_the_master_group(client, monkeypatch, no_bridge):
    import message_handler
    seen = {}

    async def fake(group_id, *a, **k):
        seen["group"] = group_id
    monkeypatch.setattr(message_handler, "handle_message", fake)
    r = client.post("/api/console", json={"text": "how far is each drive", "scope": "tunnel"})
    assert r.status_code == 200 and seen["group"] == MASTER
