"""Dialogs, passive HTTP capture and downloads, shared by both engines.

Network capture only listens. It does not pause, rewrite or intercept
requests, and it stays off until a caller asks for a response. That keeps a
normal navigation free of extra DevTools network commands.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from disguisebrowser.errors import DisguiseError

_RESPONSE_LIMIT = 200
_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
Reply = Callable[[bool, str | None], Awaitable[None]]
BodyReader = Callable[[], Awaitable[bytes]]


class JsDialog:
    """A page ``alert`` / ``confirm`` / ``prompt`` / ``beforeunload``.

    The page is blocked until :meth:`accept` or :meth:`dismiss`. Leaving
    :meth:`Page.expect_dialog` without either one dismisses it.
    """

    def __init__(self, *, type: str, message: str, default_value: str, reply: Reply) -> None:
        self.type = type
        self.message = message
        self.default_value = default_value
        self.handled = False
        self._reply = reply

    async def accept(self, prompt_text: str | None = None) -> None:
        """Close the dialog as OK. ``prompt_text`` replaces the prompt's default."""
        await self._settle(True, prompt_text)

    async def dismiss(self) -> None:
        """Close the dialog as Cancel. An ``alert`` just closes."""
        await self._settle(False, None)

    async def _settle(self, accept: bool, prompt_text: str | None) -> None:
        if self.handled:
            raise DisguiseError("dialog already handled")
        self.handled = True
        await self._reply(accept, prompt_text)

    def __repr__(self) -> str:
        return f"<JsDialog {self.type} {self.message!r}>"


class Pending[T]:
    """The value produced by an ``expect_*`` block."""

    def __init__(self, future: asyncio.Future[T], timeout: float, what: str) -> None:
        self._future = future
        self._timeout = timeout
        self._what = what

    async def value(self) -> T:
        try:
            # ``shield`` so a timeout here does not cancel the underlying wait:
            # the ``expect_*`` block still dismisses a dialog that arrives late.
            return await asyncio.wait_for(asyncio.shield(self._future), self._timeout)
        except TimeoutError:
            raise TimeoutError(
                f"timed out after {self._timeout:g}s waiting for {self._what}"
            ) from None


class HttpResponse:
    """One completed HTTP response observed while capture was on."""

    def __init__(
        self,
        *,
        url: str,
        method: str,
        status: int,
        resource_type: str,
        headers: dict[str, str],
        reader: BodyReader,
    ) -> None:
        self.url = url
        self.method = method
        self.status = status
        self.resource_type = resource_type
        self.headers = headers
        self._reader = reader
        self._body: bytes | None = None

    async def body(self) -> bytes:
        if self._body is None:
            self._body = await self._reader()
        return self._body

    async def text(self) -> str:
        return (await self.body()).decode("utf-8", errors="replace")

    async def json(self) -> Any:
        import json  # noqa: PLC0415

        return json.loads(await self.text())

    def __repr__(self) -> str:
        return f"<HttpResponse {self.status} {self.method} {self.url}>"


class ResponseLog:
    """Bounded list of responses plus waiters for the next matching URL.

    A waiter never matches a response that already arrived: arm it before the
    action that triggers the request, the same way the ``expect_*`` blocks do.
    """

    def __init__(self) -> None:
        self.entries: list[HttpResponse] = []
        self._waiters: list[tuple[str, asyncio.Future[HttpResponse]]] = []
        self.enabled = False

    def add(self, response: HttpResponse) -> None:
        self.entries.append(response)
        overflow = len(self.entries) - _RESPONSE_LIMIT
        if overflow > 0:
            del self.entries[:overflow]
        pending = []
        for needle, future in self._waiters:
            if future.done():
                continue
            if needle in response.url:
                future.set_result(response)
            else:
                pending.append((needle, future))
        self._waiters = pending

    def expect(self, url_contains: str) -> asyncio.Future[HttpResponse]:
        future: asyncio.Future[HttpResponse] = asyncio.get_running_loop().create_future()
        self._waiters.append((url_contains, future))
        return future

    def cancel(self, future: asyncio.Future[HttpResponse]) -> None:
        self._waiters = [(n, f) for n, f in self._waiters if f is not future]
        if not future.done():
            future.cancel()


@dataclass(frozen=True)
class Download:
    """A file the page saved. ``path`` is readable until the browser closes."""

    url: str
    suggested_filename: str
    path: Path


def existing_files(paths: tuple[str | Path, ...]) -> list[str]:
    """Absolute paths of files that exist. Empty input clears a file control."""
    files: list[str] = []
    for path in paths:
        file = Path(path).expanduser().resolve()
        if not file.is_file():
            raise DisguiseError(f"no such file: {file}")
        files.append(str(file))
    return files


def safe_filename(name: str) -> str:
    cleaned = _FILENAME.sub("_", name).strip("._")
    return (cleaned or "download")[:120]


def wants_geoip(flag: bool | None, *, has_proxy: bool, profile_geoip: bool | None = None) -> bool:
    """Whether this launch should align timezone and languages to the exit IP.

    An explicit flag always wins. Otherwise a stored profile keeps its own
    choice, and a proxy without a profile turns alignment on: a foreign IP with
    the host's timezone is the mismatch detectors look for first.
    """
    if flag is not None:
        return flag
    if profile_geoip is not None:
        return profile_geoip
    return has_proxy
