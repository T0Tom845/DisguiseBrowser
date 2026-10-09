"""Shared fixtures: a local HTTP server for HTML fixtures and a real Chrome
(virtual display when Xvfb is available) for integration tests.
"""

from __future__ import annotations

import os
import shutil
import threading
from collections.abc import AsyncIterator, Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import pytest_asyncio

from disguisebrowser import Browser, Page, launch
from disguisebrowser.errors import LaunchError
from disguisebrowser.launcher import find_chrome

FIXTURES = Path(__file__).parent / "fixtures"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


@pytest.fixture(scope="session")
def fixture_server() -> Iterator[str]:
    handler = partial(_QuietHandler, directory=str(FIXTURES))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


def _display_mode() -> str | None:
    if shutil.which("Xvfb"):
        return "virtual"
    if os.environ.get("DISPLAY"):
        return "host"
    return None


@pytest_asyncio.fixture(scope="session")
async def browser() -> AsyncIterator[Browser]:
    try:
        find_chrome()
    except LaunchError as exc:
        pytest.skip(f"integration tests need Google Chrome: {exc}")
    mode = _display_mode()
    if mode is None:
        pytest.skip("integration tests need Xvfb or a $DISPLAY")
    async with launch(display=mode) as instance:  # type: ignore[arg-type]
        yield instance


@pytest_asyncio.fixture
async def page(browser: Browser) -> AsyncIterator[Page]:
    tab = await browser.new_page()
    try:
        yield tab
    finally:
        await tab.close()
