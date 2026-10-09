"""Page-visible automation signals on a real Chrome launched by the library."""

from __future__ import annotations

import pytest

from disguisebrowser import Browser, Page

pytestmark = pytest.mark.integration


async def test_navigator_has_no_automation_markers(page: Page, fixture_server: str) -> None:
    await page.goto(f"{fixture_server}/interaction.html")
    env = await page.evaluate(
        "({ webdriver: navigator.webdriver, ua: navigator.userAgent, "
        "brands: navigator.userAgentData ? navigator.userAgentData.brands.length : -1, "
        "plugins: navigator.plugins.length, chrome: typeof window.chrome, "
        "permission: Notification.permission })"
    )
    assert env["webdriver"] is False
    assert "Headless" not in env["ua"]
    assert env["brands"] >= 2, "Client Hints must stay intact"
    assert env["plugins"] > 0
    assert env["chrome"] == "object"
    assert env["permission"] == "default"


async def test_screen_and_window_geometry_are_coherent(page: Page, fixture_server: str) -> None:
    await page.goto(f"{fixture_server}/interaction.html")
    geometry = await page.evaluate(
        "({ sw: screen.width, sh: screen.height, ow: outerWidth, oh: outerHeight, "
        "iw: innerWidth, ih: innerHeight, dpr: devicePixelRatio })"
    )
    assert geometry["sw"] >= 1024 and geometry["sh"] >= 700
    assert geometry["ow"] <= geometry["sw"] and geometry["oh"] <= geometry["sh"]
    assert geometry["iw"] <= geometry["ow"] and geometry["ih"] < geometry["oh"]
    assert (geometry["sw"], geometry["sh"]) != (800, 600), "headless default screen"


async def test_webgl_is_available_with_a_desktop_like_renderer(
    browser: Browser, page: Page, fixture_server: str
) -> None:
    await page.goto(f"{fixture_server}/interaction.html")
    renderer = await page.evaluate(
        "(() => { const gl = document.createElement('canvas').getContext('webgl'); "
        "if (!gl) return null; const d = gl.getExtension('WEBGL_debug_renderer_info'); "
        "return gl.getParameter(d.UNMASKED_RENDERER_WEBGL); })()"
    )
    assert renderer, "no WebGL context — a well-known bot tell"
    assert "SwiftShader" not in renderer
    if browser.display is not None and browser.persona.gpu is not None:
        assert browser.persona.gpu.vendor in renderer
        assert "llvmpipe" not in renderer


async def test_timezone_and_languages_follow_the_persona(
    browser: Browser, page: Page, fixture_server: str
) -> None:
    await page.goto(f"{fixture_server}/interaction.html")
    info = await page.evaluate(
        "({ tz: Intl.DateTimeFormat().resolvedOptions().timeZone, langs: navigator.languages })"
    )
    if browser.persona.timezone:
        assert info["tz"] == browser.persona.timezone
    assert tuple(info["langs"]) == browser.persona.languages
