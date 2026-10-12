"""Workers and out-of-process iframes are resumed without Runtime.enable."""

from __future__ import annotations

import asyncio

import pytest

from disguisebrowser import Browser

pytestmark = pytest.mark.integration


async def _wait_title(page, title: str, timeout: float = 8.0) -> None:  # type: ignore[no-untyped-def]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await page.title() == title:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"title did not become {title!r} (still {await page.title()!r})")


async def test_worker_runs_because_it_was_resumed(browser: Browser, fixture_server: str) -> None:
    page = await browser.new_page(f"{fixture_server}/frames.html")
    try:
        await _wait_title(page, "worker-ready")
        frames = await page.frames()
        assert any(str(frame["url"]).endswith("/child.html") for frame in frames)
        workers = [t for t in browser.targets if t.type == "worker"]
        assert any(t.url.endswith("/worker.js") for t in workers), [
            f"{t.type} {t.url}" for t in browser.targets
        ]
    finally:
        await page.close()


async def test_cross_origin_iframe_is_resumed(browser: Browser, fixture_server: str) -> None:
    # Site isolation keys on scheme + host: 127.0.0.1 and localhost are different
    # sites even on the same port, so the child lands in its own process.
    other = fixture_server.replace("127.0.0.1", "localhost") + "/child.html"
    page = await browser.new_page(f"{fixture_server}/oopif.html")
    try:
        await page.evaluate(f"document.querySelector('#frame').src = {other!r}")
        iframe = await _wait_iframe(browser)
        text = await browser.evaluate_target(iframe.target_id, "document.body.innerText")
        assert "child-ready" in str(text)
        assert iframe.context_id is not None, "OOPIF evaluation must use an isolated world"
        assert any(str(frame["url"]) == other for frame in await page.frames())
    finally:
        await page.close()


async def _wait_iframe(browser: Browser, timeout: float = 8.0):  # type: ignore[no-untyped-def]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        matches = [
            target
            for target in browser.targets
            if target.type == "iframe" and target.url.endswith("/child.html")
        ]
        if matches:
            return matches[0]
        await asyncio.sleep(0.1)
    seen = [f"{target.type} {target.url}" for target in browser.targets]
    raise AssertionError(f"no cross-origin iframe attached; targets: {seen}")
