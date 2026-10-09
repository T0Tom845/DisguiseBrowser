"""The two CDP leaks the architecture is built around, reproduced offline."""

from __future__ import annotations

import asyncio

import pytest

from disguisebrowser import Page

pytestmark = pytest.mark.integration


async def test_console_argument_getter_is_never_triggered(page: Page, fixture_server: str) -> None:
    await page.goto(f"{fixture_server}/runtime_enable_leak.html")
    await asyncio.sleep(0.6)
    # Keep working as a scraper would: isolated-world DOM access and screenshots.
    assert await page.query("p") is not None
    await page.screenshot()
    await asyncio.sleep(0.3)
    leak = await page.evaluate("window.__leak", world="main")
    assert leak["logs"] > 5, "fixture did not run"
    assert leak["stackReads"] == 0, "Error.stack getter fired: Runtime.enable-style leak"


async def test_isolated_world_calls_do_not_reach_main_world_hooks(
    page: Page, fixture_server: str
) -> None:
    await page.goto(f"{fixture_server}/main_world_hook.html")
    count = await page.call("() => document.getElementsByClassName('item').length")
    assert count == 2
    assert await page.evaluate("window.__calls.length", world="main") == 0


async def test_fixture_detects_main_world_execution(page: Page, fixture_server: str) -> None:
    """Sanity check of the fixture: opting into the main world *is* observable."""
    await page.goto(f"{fixture_server}/main_world_hook.html")
    await page.call("() => document.getElementsByClassName('item').length", world="main")
    stacks = await page.evaluate("window.__calls", world="main")
    assert len(stacks) == 1 and "<anonymous>" in stacks[0]


async def test_isolated_world_cannot_see_page_globals(page: Page, fixture_server: str) -> None:
    await page.goto(f"{fixture_server}/main_world_hook.html")
    assert await page.evaluate("typeof window.__calls") == "undefined"
    assert await page.evaluate("typeof window.__calls", world="main") == "object"


async def test_evaluated_code_has_no_source_url_marker(page: Page, fixture_server: str) -> None:
    await page.goto(f"{fixture_server}/main_world_hook.html")
    stack = await page.evaluate("new Error('x').stack")
    assert "sourceURL" not in stack and "pptr" not in stack and "playwright" not in stack.lower()
