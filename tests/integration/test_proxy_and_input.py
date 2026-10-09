"""Chrome through the proxy gateway, geoip alignment and XTest vs CDP input."""

from __future__ import annotations

import os
import shutil

import pytest
from tests.support.fake_proxies import FakeHttpConnectProxy

from disguisebrowser import Browser, Persona, launch

pytestmark = pytest.mark.integration

# Chrome never proxies loopback unless told so; needed to route the fixture
# server through the gateway in tests.
PROXY_LOOPBACK = ("--proxy-bypass-list=<-loopback>",)


def _display_mode() -> str:
    if shutil.which("Xvfb"):
        return "virtual"
    if os.environ.get("DISPLAY"):
        return "host"
    pytest.skip("needs Xvfb or $DISPLAY")


async def test_chrome_traffic_goes_through_authenticated_upstream(fixture_server: str) -> None:
    upstream = await FakeHttpConnectProxy("alice", "s3cret").start()
    try:
        persona = Persona.from_host(proxy=upstream.url)
        async with launch(persona=persona, display=_display_mode(), args=PROXY_LOOPBACK) as browser:  # type: ignore[arg-type]
            assert browser.gateway is not None and browser.gateway.url.startswith("socks5://")
            page = await browser.new_page(f"{fixture_server}/interaction.html")
            assert await page.title() == "Interaction fixture"
            host_port = fixture_server.removeprefix("http://")
            assert host_port in upstream.connects, upstream.connects
            assert upstream.auth_failures == 0
            assert browser.gateway.stats.connections >= 1
    finally:
        await upstream.stop()


async def test_geoip_aligns_timezone_and_languages(fixture_server: str) -> None:
    upstream = await FakeHttpConnectProxy("alice", "s3cret").start()
    geo_url = f"{fixture_server}/geo.json"
    os.environ["DISGUISE_GEOIP_URL"] = geo_url
    try:
        persona = Persona.from_host(proxy=upstream.url, timezone="Europe/Moscow")
        async with launch(
            persona=persona, display=_display_mode(), geoip=True, args=PROXY_LOOPBACK
        ) as browser:  # type: ignore[arg-type]
            assert browser.geo is not None and browser.geo.country == "DE"
            assert browser.persona.timezone == "Europe/Berlin"
            assert browser.persona.languages[0] == "de-DE"
            page = await browser.new_page(f"{fixture_server}/interaction.html")
            info = await page.evaluate(
                "({ tz: Intl.DateTimeFormat().resolvedOptions().timeZone, "
                "langs: navigator.languages })"
            )
            assert info["tz"] == "Europe/Berlin"
            assert info["langs"][0] == "de-DE"
    finally:
        os.environ.pop("DISGUISE_GEOIP_URL", None)
        await upstream.stop()


async def test_xtest_click_has_real_screen_coordinates(
    browser: Browser, fixture_server: str
) -> None:
    if browser.input_mode != "xtest":
        pytest.skip("XTest input is only used on an X display")
    page = await browser.new_page(f"{fixture_server}/interaction.html")
    try:
        await page.click("#target")
        events = await page.evaluate("window.__events", world="main")
        click = events["clicks"][0]
        assert click["isTrusted"] is True
        # Real pointer events are reported relative to the screen, not the page.
        assert (click["screenX"], click["screenY"]) != (click["x"], click["y"])
        origin_x, origin_y, _ = await page._content_origin()
        assert abs(click["screenX"] - (click["x"] + origin_x)) <= 2
        assert abs(click["screenY"] - (click["y"] + origin_y)) <= 2
        # A physical mouse reports pressure 0.5 while a button is held
        # (Pointer Events spec); CDP-synthesized events report 0.
        assert events["lastPointerDown"]["pressure"] == 0.5
        assert events["lastPointerDown"]["pointerType"] == "mouse"
    finally:
        await page.close()


async def test_cdp_input_still_works_as_a_fallback(fixture_server: str) -> None:
    async with launch(display=_display_mode(), input="cdp") as browser:  # type: ignore[arg-type]
        assert browser.input_mode == "cdp"
        page = await browser.new_page(f"{fixture_server}/interaction.html")
        await page.click("#target", humanize=False)
        events = await page.evaluate("window.__events", world="main")
        assert events["clicks"][0]["isTrusted"] is True
        assert events["lastPointerDown"]["pointerType"] == "mouse"
