"""Connection/Session behaviour against an in-process fake CDP endpoint."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from websockets.asyncio.server import ServerConnection, serve

from disguisebrowser.cdp import Connection, QuietCdpPolicy
from disguisebrowser.errors import CdpError, ConnectionClosedError, QuietCdpViolationError


async def _fake_cdp(ws: ServerConnection) -> None:
    async for raw in ws:
        message = json.loads(raw)
        method, params, mid = message["method"], message.get("params", {}), message["id"]
        session_id = message.get("sessionId")
        reply: dict[str, Any] = {"id": mid}
        if session_id:
            reply["sessionId"] = session_id
        if method == "Echo.ok":
            reply["result"] = {"echo": params}
        elif method == "Echo.error":
            reply["error"] = {"code": -32000, "message": "boom", "data": "details"}
        elif method == "Echo.event":
            event: dict[str, Any] = {"method": "Echo.fired", "params": {"n": params.get("n")}}
            if session_id:
                event["sessionId"] = session_id
            await ws.send(json.dumps(event))
            reply["result"] = {}
        elif method == "Echo.hang":
            continue
        elif method == "Echo.close":
            await ws.close()
            return
        else:
            reply["result"] = {}
        await ws.send(json.dumps(reply))


@pytest_asyncio.fixture
async def endpoint() -> AsyncIterator[str]:
    async with serve(_fake_cdp, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        yield f"ws://127.0.0.1:{port}/devtools/browser/fake"


@pytest_asyncio.fixture
async def connection(endpoint: str) -> AsyncIterator[Connection]:
    conn = await Connection.open(endpoint)
    try:
        yield conn
    finally:
        await conn.close()


async def test_roundtrip_and_sessions(connection: Connection) -> None:
    assert await connection.root.send("Echo.ok", {"a": 1}) == {"echo": {"a": 1}}
    session = connection.session("S1")
    assert await session.send("Echo.ok", {"b": 2}) == {"echo": {"b": 2}}


async def test_error_is_mapped_to_cdp_error(connection: Connection) -> None:
    with pytest.raises(CdpError) as info:
        await connection.root.send("Echo.error")
    assert info.value.code == -32000 and info.value.message == "boom"
    assert info.value.data == "details" and info.value.method == "Echo.error"


async def test_policy_blocks_before_sending(connection: Connection) -> None:
    with pytest.raises(QuietCdpViolationError):
        await connection.root.send("Runtime.enable")
    unsafe = await Connection.open(connection.url, policy=QuietCdpPolicy(unsafe=True))
    try:
        assert await unsafe.root.send("Runtime.enable") == {}
    finally:
        await unsafe.close()


async def test_events_reach_session_handlers_and_waiters(connection: Connection) -> None:
    session = connection.session("S2")
    seen: list[dict[str, Any]] = []
    unsubscribe = session.on("Echo.fired", seen.append)
    waiter = session.expect("Echo.fired", lambda p: p.get("n") == 2)
    await session.send("Echo.event", {"n": 1})
    await session.send("Echo.event", {"n": 2})
    assert await asyncio.wait_for(waiter, 2) == {"n": 2}
    assert [e["n"] for e in seen] == [1, 2]
    unsubscribe()
    await session.send("Echo.event", {"n": 3})
    assert [e["n"] for e in seen] == [1, 2]
    # Session-scoped events do not leak into the browser-level session.
    root_seen: list[dict[str, Any]] = []
    connection.root.on("Echo.fired", root_seen.append)
    await session.send("Echo.event", {"n": 4})
    assert root_seen == []


async def test_async_handlers_are_scheduled(connection: Connection) -> None:
    done = asyncio.Event()

    async def handler(params: dict[str, Any]) -> None:
        await asyncio.sleep(0)
        done.set()

    connection.root.on("Echo.fired", handler)
    await connection.root.send("Echo.event", {"n": 0})
    await asyncio.wait_for(done.wait(), 2)


async def test_command_timeout(connection: Connection) -> None:
    with pytest.raises(TimeoutError, match=r"Echo\.hang"):
        await connection.root.send("Echo.hang", timeout=0.1)


async def test_wait_for_timeout_cleans_up(connection: Connection) -> None:
    with pytest.raises(TimeoutError, match=r"Echo\.never"):
        await connection.root.wait_for("Echo.never", timeout=0.05)
    assert connection.root._waiters["Echo.never"] == []


async def test_remote_close_fails_pending_and_marks_closed(endpoint: str) -> None:
    conn = await Connection.open(endpoint)
    pending = asyncio.ensure_future(conn.root.send("Echo.hang", timeout=5))
    await asyncio.sleep(0.05)
    with pytest.raises(ConnectionClosedError):
        await conn.root.send("Echo.close")
    with pytest.raises(ConnectionClosedError):
        await pending
    assert conn.closed
    with pytest.raises(ConnectionClosedError):
        await conn.root.send("Echo.ok")
    await conn.close()
