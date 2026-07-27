"""Command routing in message_handler.

Routing is prefix-based, so commands sharing a prefix have to be ordered
carefully — "/excel2" also starts with "/excel".

Driven with asyncio.run rather than pytest-asyncio to keep the test
dependencies to pytest alone.
"""
import asyncio

import pytest

import message_handler as mh


@pytest.fixture
def routed(monkeypatch):
    """Record which command handler a message dispatches to."""
    calls = []

    def fake(name):
        async def _handler(*args):
            calls.append((name, args))
        return _handler

    for handler in ("handle_excel2", "handle_excel", "handle_daily",
                    "handle_confirm", "handle_dwall_export", "handle_help",
                    "handle_ask"):
        monkeypatch.setattr(mh.cmd, handler, fake(handler))

    # Nothing below the command block should run during these tests.
    monkeypatch.setattr(mh, "classify_and_parse",
                        lambda _t: pytest.fail("routing fell through to the parser"))
    return calls


def _send(text):
    asyncio.run(mh.handle_message("g1", "Sender", "65", text))


def test_excel2_routes_to_the_full_export(routed):
    _send("/excel2")
    assert routed[0][0] == "handle_excel2"


def test_excel2_is_not_swallowed_by_excel(routed):
    # The bug this guards: "/excel2" matching startswith("/excel") and being
    # read as /excel with the argument "2", which fails date parsing.
    _send("/excel2")
    assert [c[0] for c in routed] == ["handle_excel2"]


def test_excel2_takes_no_arguments(routed):
    _send("/excel2")
    assert routed[0] == ("handle_excel2", ("g1",))


def test_plain_excel_still_routes_with_no_args(routed):
    _send("/excel")
    assert routed[0] == ("handle_excel", ("g1", ""))


def test_excel_keeps_its_month_argument(routed):
    _send("/excel Jan 2026")
    assert routed[0] == ("handle_excel", ("g1", "Jan 2026"))


def test_excel2_is_case_insensitive(routed):
    _send("/EXCEL2")
    assert routed[0][0] == "handle_excel2"


def test_dwall_with_no_args_exports(routed):
    _send("/dwall")
    assert routed[0][0] == "handle_dwall_export"
