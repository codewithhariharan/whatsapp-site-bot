"""Command routing.

The /excel family is worth pinning down because the dispatcher matches on
`startswith`, which is order-sensitive and has bitten this file before.
"""
import asyncio

import pytest

import commands as cmd
import message_handler as mh

GROUP = "120363021760406818@g.us"


@pytest.fixture
def calls(monkeypatch):
    """Record which handler fires instead of running it."""
    seen = []

    async def excel(group_id, args=""):
        seen.append(("excel", args))

    async def dwall(group_id):
        seen.append(("dwall", None))

    monkeypatch.setattr(cmd, "handle_excel", excel)
    monkeypatch.setattr(cmd, "handle_dwall_export", dwall)
    return seen


def route(text):
    return asyncio.run(mh.handle_message(GROUP, "tester", "6500000000", text))


class TestExcelRouting:
    def test_bare_excel_passes_no_argument(self, calls):
        route("/excel")
        assert calls == [("excel", "")]

    def test_month_argument_is_forwarded(self, calls):
        route("/excel Jan 2026")
        assert calls == [("excel", "Jan 2026")]

    def test_routing_is_case_insensitive(self, calls):
        route("/EXCEL Aug 2026")
        assert calls == [("excel", "Aug 2026")]

    def test_excel2_is_gone_and_falls_through_to_excel(self, calls):
        # /excel2 was removed once it became an exact duplicate of the bare
        # form. startswith("/excel") still matches it, so it lands on the
        # monthly branch with args="2" and is answered with the format hint —
        # not silently swallowed.
        route("/excel2")
        assert calls == [("excel", "2")]

    def test_dwall_still_routes(self, calls):
        route("/dwall")
        assert calls == [("dwall", None)]
