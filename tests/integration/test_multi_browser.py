"""Several independent browsers in one process (the "N browsers per container" case)."""

from __future__ import annotations

import asyncio
import shutil

import pytest

from disguisebrowser import Persona, launch

pytestmark = pytest.mark.integration


async def test_three_browsers_side_by_side(fixture_server: str) -> None:
    if not shutil.which("Xvfb"):
        pytest.skip("needs Xvfb for independent virtual displays")
    handles = [
        launch(display="virtual", persona=Persona.from_host(seed=seed)) for seed in (11, 12, 13)
    ]
    browsers = await asyncio.gather(*handles)
    try:
        displays = {b.display.display for b in browsers if b.display}
        assert len(displays) == 3, "each browser must own its own X display"
        ports = {b.chrome.port for b in browsers}
        assert len(ports) == 3

        async def probe(index: int) -> dict[str, object]:
            page = await browsers[index].new_page(f"{fixture_server}/interaction.html")
            await page.type("#query", f"browser-{index}", humanize=False)
            field = await page.query("#query")
            assert field is not None
            value = await field.value()
            avail = await page.evaluate("[screen.availHeight, screen.height, document.hasFocus()]")
            return {"value": value, "avail": avail}

        results = await asyncio.gather(*(probe(i) for i in range(3)))
        assert [r["value"] for r in results] == ["browser-0", "browser-1", "browser-2"]
        for result in results:
            avail_height, height, has_focus = result["avail"]  # type: ignore[misc]
            assert avail_height < height, "work area must differ from the screen like on a desktop"
            assert has_focus is True
    finally:
        await asyncio.gather(*(b.close() for b in browsers))
