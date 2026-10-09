"""Minimal, quiet CDP client over WebSocket.

Design notes
------------
* One WebSocket to the *browser* endpoint; page/iframe/worker targets are
  addressed through flattened sessions (``sessionId`` in every message).
* No protocol code generation in v0.1: ``send("Domain.method", {...})`` returns
  the raw ``result`` dict. Typed wrappers live where they are used.
* Every outgoing command passes through :class:`QuietCdpPolicy`.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed as WsConnectionClosed

from disguisebrowser.cdp.policy import QuietCdpPolicy
from disguisebrowser.errors import CdpError, ConnectionClosedError

log = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None] | None]
Predicate = Callable[[dict[str, Any]], bool]

DEFAULT_COMMAND_TIMEOUT = 30.0


class Session:
    """A CDP session (browser-level when ``session_id`` is ``None``)."""

    def __init__(self, connection: Connection, session_id: str | None) -> None:
        self.connection = connection
        self.session_id = session_id
        self._handlers: defaultdict[str, list[EventHandler]] = defaultdict(list)
        self._waiters: defaultdict[str, list[tuple[Predicate | None, asyncio.Future[Any]]]] = (
            defaultdict(list)
        )
        self.detached = False

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = DEFAULT_COMMAND_TIMEOUT,
    ) -> dict[str, Any]:
        return await self.connection.send(
            method, params, session_id=self.session_id, timeout=timeout
        )

    def on(self, method: str, handler: EventHandler) -> Callable[[], None]:
        """Subscribe to an event; returns an unsubscribe callable."""
        self._handlers[method].append(handler)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._handlers[method].remove(handler)

        return unsubscribe

    def expect(self, method: str, predicate: Predicate | None = None) -> asyncio.Future[Any]:
        """Return a future resolved with the params of the next matching event.

        Register *before* sending the command that triggers the event so that
        fast events are never missed.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._waiters[method].append((predicate, future))
        return future

    async def wait_for(
        self,
        method: str,
        predicate: Predicate | None = None,
        *,
        timeout: float | None = DEFAULT_COMMAND_TIMEOUT,
    ) -> dict[str, Any]:
        future = self.expect(method, predicate)
        try:
            result: dict[str, Any] = await asyncio.wait_for(future, timeout)
        except TimeoutError:
            self._discard_waiter(method, future)
            raise TimeoutError(f"timed out after {timeout}s waiting for {method}") from None
        return result

    def _discard_waiter(self, method: str, future: asyncio.Future[Any]) -> None:
        self._waiters[method] = [(p, f) for p, f in self._waiters[method] if f is not future]

    def _dispatch(self, method: str, params: dict[str, Any]) -> None:
        for handler in list(self._handlers.get(method, ())):
            try:
                outcome = handler(params)
                if inspect.isawaitable(outcome):
                    task = asyncio.ensure_future(outcome)
                    self.connection._track(task)
            except Exception:
                log.exception("event handler for %s failed", method)
        remaining: list[tuple[Predicate | None, asyncio.Future[Any]]] = []
        for predicate, future in self._waiters.get(method, ()):
            if future.done():
                continue
            try:
                matched = predicate is None or predicate(params)
            except Exception:
                log.exception("event predicate for %s failed", method)
                matched = False
            if matched:
                future.set_result(params)
            else:
                remaining.append((predicate, future))
        if method in self._waiters:
            self._waiters[method] = remaining

    def _fail_all(self, exc: BaseException) -> None:
        for waiters in self._waiters.values():
            for _, future in waiters:
                if not future.done():
                    future.set_exception(exc)
        self._waiters.clear()


class Connection:
    """Owns the WebSocket and multiplexes commands/events across sessions."""

    def __init__(self, ws: ClientConnection, url: str, policy: QuietCdpPolicy) -> None:
        self._ws = ws
        self.url = url
        self.policy = policy
        self._next_id = 0
        self._pending: dict[int, tuple[str, asyncio.Future[dict[str, Any]]]] = {}
        self._sessions: dict[str | None, Session] = {}
        self.root = self.session(None)
        self._closed = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._reader = asyncio.create_task(self._read_loop(), name="cdp-reader")

    @classmethod
    async def open(cls, url: str, *, policy: QuietCdpPolicy | None = None) -> Connection:
        ws = await connect(url, max_size=None, compression=None, ping_interval=None)
        return cls(ws, url, policy or QuietCdpPolicy())

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def session(self, session_id: str | None) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            session = Session(self, session_id)
            self._sessions[session_id] = session
        return session

    def drop_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            session.detached = True
            session._fail_all(ConnectionClosedError(f"session {session_id} detached"))

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float | None = DEFAULT_COMMAND_TIMEOUT,
    ) -> dict[str, Any]:
        if self.closed:
            raise ConnectionClosedError("CDP connection is closed")
        params = params or {}
        self.policy.check(method, params)
        self._next_id += 1
        message_id = self._next_id
        payload: dict[str, Any] = {"id": message_id, "method": method, "params": params}
        if session_id is not None:
            payload["sessionId"] = session_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[message_id] = (method, future)
        log.debug("→ %s %s %s", session_id or "browser", method, _short(params))
        try:
            await self._ws.send(json.dumps(payload))
        except WsConnectionClosed as exc:
            self._pending.pop(message_id, None)
            raise ConnectionClosedError("CDP connection closed while sending") from exc
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            self._pending.pop(message_id, None)
            raise TimeoutError(f"{method} did not answer within {timeout}s") from None

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                self._handle(json.loads(raw))
        except WsConnectionClosed:
            pass
        except Exception:
            log.exception("CDP reader loop crashed")
        finally:
            self._shutdown(ConnectionClosedError("CDP connection closed"))

    def _handle(self, message: dict[str, Any]) -> None:
        message_id = message.get("id")
        if message_id is not None:
            pending = self._pending.pop(message_id, None)
            if pending is None:
                return
            method, future = pending
            if future.done():
                return
            if "error" in message:
                err = message["error"]
                future.set_exception(
                    CdpError(method, err.get("code", 0), err.get("message", ""), err.get("data"))
                )
            else:
                future.set_result(message.get("result", {}))
            return
        event = message.get("method")
        if not event:
            return
        params = message.get("params", {})
        session_id = message.get("sessionId")
        log.debug("← %s %s %s", session_id or "browser", event, _short(params))
        # The browser-level session also sees every event so that target
        # bookkeeping (attach/detach) can be done in one place. Target.* events
        # carry the affected sessionId in their own params.
        if session_id is not None:
            self.session(session_id)._dispatch(event, params)
            if not event.startswith("Target."):
                return
        self.root._dispatch(event, params)

    def _track(self, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _shutdown(self, exc: BaseException) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        for _, future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
        for session in self._sessions.values():
            session._fail_all(exc)

    async def close(self) -> None:
        if not self._closed.is_set():
            await self._ws.close()
        if not self._reader.done():
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        for task in list(self._tasks):
            task.cancel()
        self._shutdown(ConnectionClosedError("CDP connection closed"))


def _short(obj: Any, limit: int = 160) -> str:
    text = json.dumps(obj, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"
