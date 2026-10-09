"""End-to-end MCP workflow through the in-process client of the official SDK."""

from __future__ import annotations

import json
import os
import re
import shutil
from typing import Any

import pytest

pytest.importorskip("mcp")
from mcp import Client

from disguisebrowser.mcp_server import BrowserSessions, build_server

pytestmark = pytest.mark.integration


def _text(result: Any) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


def _ref(snapshot: str, prefix: str) -> str:
    match = re.search(re.escape(prefix) + r"[^\n]*?\[ref=(e\d+)\]", snapshot)
    assert match, f"{prefix} not found in snapshot:\n{snapshot}"
    return match.group(1)


def _data(result: Any) -> Any:
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(_text(result))


async def test_agent_workflow(fixture_server: str) -> None:
    if not shutil.which("Xvfb") and not os.environ.get("DISPLAY"):
        pytest.skip("needs Xvfb or $DISPLAY")
    registry = BrowserSessions()
    server = build_server(registry)
    try:
        async with Client(server) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            assert {"browser_launch", "snapshot", "click", "type_text", "screenshot"} <= tools

            launched = _data(await client.call_tool("browser_launch", {"label": "test", "seed": 7}))
            sid = launched["session_id"]
            assert launched["chrome"].startswith("Chrome/")
            assert len(registry.sessions) == 1

            nav = _data(
                await client.call_tool(
                    "navigate", {"session_id": sid, "url": f"{fixture_server}/interaction.html"}
                )
            )
            assert nav["title"] == "Interaction fixture"

            snap = _text(await client.call_tool("snapshot", {"session_id": sid}))
            assert 'heading "Interaction fixture"' in snap
            ref = _ref(snap, 'button "Click me"')
            textbox_ref = _ref(snap, 'textbox "Query"')

            await client.call_tool(
                "type_text", {"session_id": sid, "target": textbox_ref, "text": "agent"}
            )
            await client.call_tool("click", {"session_id": sid, "target": ref})
            status = _text(
                await client.call_tool("extract_text", {"session_id": sid, "selector": "#status"})
            )
            assert status == "clicked"
            value = _data(
                await client.call_tool(
                    "evaluate",
                    {"session_id": sid, "expression": "document.querySelector('#query').value"},
                )
            )
            assert value["value"] == "agent"

            shot = await client.call_tool("screenshot", {"session_id": sid})
            assert any(getattr(c, "type", "") == "image" for c in shot.content)

            tabs = _data(await client.call_tool("tabs", {"session_id": sid}))
            assert len(tabs) >= 1 and any(t["current"] for t in tabs)

            listing = _data(await client.call_tool("browser_list", {}))
            assert listing[0]["session_id"] == sid and listing[0]["label"] == "test"

            closed = _text(await client.call_tool("browser_close", {"session_id": sid}))
            assert "closed" in closed
            assert registry.sessions == {}
    finally:
        await registry.close_all()


async def test_unknown_session_is_a_tool_error() -> None:
    server = build_server(BrowserSessions())
    async with Client(server) as client:
        result = await client.call_tool("snapshot", {"session_id": "nope"})
        assert result.is_error
        assert "unknown session" in _text(result)
