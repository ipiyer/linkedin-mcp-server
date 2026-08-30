"""Tool-level tests for the guarded Approva wrappers.

These cover the two defects the 2026-08-30 review found in
``approva_connect``: a refusal message that could never be reached, and a
write that handed back a full page scrape.
"""

from __future__ import annotations

import json

import pytest
from fastmcp import FastMCP

import linkedin_mcp_server.tools.approva as approva_tools
from linkedin_mcp_server.tools.approva import register_approva_tools


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    """A server with the Approva tools and the kill switch demonstrably unset."""
    monkeypatch.setenv("APPROVA_STOP_FILE", str(tmp_path / "STOP"))
    server = FastMCP("test")
    register_approva_tools(server)
    return server


class _StubPage:
    url = "https://www.linkedin.com/in/someone/"

    async def evaluate(self, *_args, **_kwargs):
        return "an ordinary profile page"


class _StubExtractor:
    """Stands in for upstream's extractor, returning its real result shape."""

    def __init__(self):
        self._page = _StubPage()

    async def _goto_with_auth_checks(self, _url):
        return None

    async def connect_with_person(self, username, *, note=None):
        return {
            "url": f"https://www.linkedin.com/in/{username}/",
            "status": "connected",
            "message": "Connection request sent.",
            "note_sent": bool(note),
            "profile": "the entire scraped profile page, thousands of characters",
        }


async def _call(server, name, args):
    result = await server.call_tool(name, args)
    return json.loads(result.content[0].text)


class TestInviteNoteLimit:
    async def test_an_over_long_note_is_refused_before_the_browser_opens(self, mcp):
        # No extractor is stubbed here on purpose: if the refusal did not
        # short-circuit, this would try to start a real browser.
        with pytest.raises(Exception) as excinfo:
            await mcp.call_tool(
                "approva_connect",
                {"linkedin_username": "someone", "note": "y" * 301},
            )
        message = str(excinfo.value)
        assert "301 characters" in message
        assert "300" in message

    async def test_the_schema_does_not_pre_empt_that_refusal(self, mcp):
        # A max_length on the field is enforced by pydantic before the tool
        # body runs, which made the message above unreachable and handed the
        # caller a bare validation error instead.
        tools = await mcp.list_tools()
        connect = next(t for t in tools if t.name == "approva_connect")
        note = connect.parameters["properties"]["note"]
        assert "maxLength" not in json.dumps(note)


class TestConnectResultShape:
    async def test_the_scraped_profile_is_not_returned_from_a_write(
        self, mcp, monkeypatch
    ):
        async def _ready(*_args, **_kwargs):
            return _StubExtractor()

        monkeypatch.setattr(approva_tools, "get_ready_extractor", _ready)
        payload = await _call(
            mcp, "approva_connect", {"linkedin_username": "someone", "note": "hi"}
        )

        assert "profile" not in payload
        # The fields the bot actually dispatches on survive.
        assert payload["status"] == "connected"
        assert payload["note_sent"] is True
        assert payload["page_state"] == "normal"
