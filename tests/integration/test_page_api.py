"""Scraping/automation API against the local interaction fixture."""

from __future__ import annotations

import pytest

from disguisebrowser import Browser, ElementNotFoundError, EvaluationError, Page

pytestmark = pytest.mark.integration


async def test_navigation_title_and_content(page: Page, fixture_server: str) -> None:
    await page.goto(f"{fixture_server}/interaction.html")
    assert page.url.endswith("/interaction.html")
    assert await page.title() == "Interaction fixture"
    assert '<button id="target">' in await page.content()


async def test_queries_and_element_accessors(page: Page, fixture_server: str) -> None:
    await page.goto(f"{fixture_server}/interaction.html")
    title = await page.query("#title")
    assert title is not None
    assert await title.text() == "Interaction fixture"
    assert await title.attribute("id") == "title"
    assert (await title.attributes()) == {"id": "title"}
    spacers = await page.query_all(".spacer")
    assert len(spacers) == 2
    by_xpath = await page.xpath("//button[@id='target']")
    assert len(by_xpath) == 1 and await by_xpath[0].text() == "Click me"
    assert await page.query("#nope") is None
    scoped = await (await page.query("label")).query("input")  # type: ignore[union-attr]
    assert scoped is not None and await scoped.attribute("id") == "query"


async def test_wait_for_selector_sees_late_elements(page: Page, fixture_server: str) -> None:
    await page.goto(f"{fixture_server}/interaction.html")
    late = await page.wait_for_selector("#late", state="visible", timeout=5)
    assert late is not None and await late.text() == "late element"
    with pytest.raises(TimeoutError):
        await page.wait_for_selector("#never", timeout=0.3)


async def test_humanized_click_scrolls_and_delivers_trusted_event(
    page: Page, fixture_server: str
) -> None:
    await page.goto(f"{fixture_server}/interaction.html")
    await page.click("#target")
    events = await page.evaluate("window.__events", world="main")
    assert len(events["clicks"]) == 1
    click = events["clicks"][0]
    assert click["isTrusted"] is True and click["button"] == 0
    assert events["moves"] > 5, "cursor teleported instead of travelling"
    assert events["wheels"] > 0, "element below the fold must be reached by scrolling"
    status = await page.query("#status")
    assert status is not None and await status.text() == "clicked"


async def test_humanized_typing_produces_key_events(page: Page, fixture_server: str) -> None:
    await page.goto(f"{fixture_server}/interaction.html")
    await page.type("#query", "Hi, Мир!")
    field = await page.query("#query")
    assert field is not None and await field.value() == "Hi, Мир!"
    keys = await page.evaluate("window.__events.keys", world="main")
    assert [k["key"] for k in keys[:4]] == ["Shift", "H", "i", ","]
    assert all(k["isTrusted"] for k in keys)


async def test_type_with_clear(page: Page, fixture_server: str) -> None:
    await page.goto(f"{fixture_server}/interaction.html")
    await page.type("#query", "first", humanize=False)
    await page.type("#query", "second", humanize=False, clear=True)
    field = await page.query("#query")
    assert field is not None and await field.value() == "second"


async def test_screenshot_png(page: Page, fixture_server: str, tmp_path: object) -> None:
    await page.goto(f"{fixture_server}/interaction.html")
    data = await page.screenshot()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


async def test_evaluation_errors_surface(page: Page, fixture_server: str) -> None:
    await page.goto(f"{fixture_server}/interaction.html")
    with pytest.raises(EvaluationError, match="ReferenceError"):
        await page.evaluate("undefinedThing + 1")
    with pytest.raises(ElementNotFoundError):
        await page.click("#missing", timeout=0.2)


async def test_cookies_roundtrip(page: Page, fixture_server: str) -> None:
    await page.goto(f"{fixture_server}/interaction.html")
    await page.set_cookies([{"name": "flavour", "value": "oatmeal", "url": fixture_server}])
    names = {c["name"]: c["value"] for c in await page.cookies(fixture_server)}
    assert names.get("flavour") == "oatmeal"
    assert await page.evaluate("document.cookie") == "flavour=oatmeal"


async def test_multiple_pages_are_independent(browser: Browser, fixture_server: str) -> None:
    first = await browser.new_page(f"{fixture_server}/interaction.html")
    second = await browser.new_page(f"{fixture_server}/main_world_hook.html")
    try:
        assert await first.title() == "Interaction fixture"
        assert await second.title() == "Main-world hook probe"
        assert first in browser.pages and second in browser.pages
    finally:
        await first.close()
        await second.close()
    assert first not in browser.pages and second not in browser.pages


async def test_dialogs_are_dismissed_automatically(page: Page, fixture_server: str) -> None:
    await page.goto(f"{fixture_server}/interaction.html")
    result = await page.evaluate("window.confirm('continue?')", world="main")
    assert result is False
