"""Dialogs, passive response bodies, file inputs and downloads on real Chrome."""

from __future__ import annotations

from pathlib import Path

import pytest

from disguisebrowser import Page

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent.parent / "fixtures"


async def _text(page: Page, selector: str) -> str:
    element = await page.query(selector)
    assert element is not None
    return await element.text()


async def test_prompt_can_be_answered(page: Page, fixture_server: str) -> None:
    await page.goto(f"{fixture_server}/exchange.html")
    async with page.expect_dialog() as pending:
        await page.click("#prompt", humanize=False)
        dialog = await pending.value()
        assert dialog.type == "prompt"
        assert dialog.message == "name?"
        assert dialog.default_value == "anon"
        await dialog.accept("ada")
    assert await _text(page, "#out") == "ada"


async def test_unhandled_dialog_is_dismissed(page: Page, fixture_server: str) -> None:
    await page.goto(f"{fixture_server}/exchange.html")
    async with page.expect_dialog() as pending:
        await page.click("#confirm", humanize=False)
        await pending.value()
    assert await _text(page, "#out") == "dismissed"


async def test_response_body_is_the_payload_the_page_fetched(
    page: Page, fixture_server: str
) -> None:
    await page.goto(f"{fixture_server}/exchange.html")
    async with page.expect_response("payload.json") as pending:
        await page.click("#fetch", humanize=False)
        response = await pending.value()
    assert response.status == 200
    assert response.method == "GET"
    assert await response.json() == {"ok": True, "n": 1}
    assert '"ok": true' in await _text(page, "#net")


async def test_file_input_receives_a_local_file(page: Page, fixture_server: str) -> None:
    await page.goto(f"{fixture_server}/exchange.html")
    field = await page.query("#file")
    assert field is not None
    await field.set_input_files(FIXTURES / "sample.txt")
    assert await _text(page, "#file-name") == "sample.txt"


async def test_download_is_saved(page: Page, fixture_server: str) -> None:
    await page.goto(f"{fixture_server}/exchange.html")
    async with page.expect_download() as pending:
        await page.click("#download", humanize=False)
        download = await pending.value()
    assert download.suggested_filename == "sample.txt"
    assert download.path.read_text(encoding="utf-8") == "sample-ok\n"
