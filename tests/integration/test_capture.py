"""Live fingerprint capture on the real browser launched by the library."""

from __future__ import annotations

import pytest

from disguisebrowser import Browser
from disguisebrowser.fingerprint import by_category, capture_with_browser, diff, summarize

pytestmark = pytest.mark.integration

SECTIONS = (
    "navigator", "ua_data", "headers", "screen", "media_queries", "intl", "api_surface",
    "js_engine", "webgl", "webgl2", "webgpu", "canvas", "text_metrics", "emoji", "fonts",
    "form_controls", "default_fonts", "audio", "media", "eme", "rtc", "devices", "speech",
    "permissions", "storage", "performance", "battery", "network", "keyboard", "css",
    "chrome_object", "misc", "worker", "iframe",
)  # fmt: skip


@pytest.fixture(scope="module")
async def record(browser: Browser) -> dict:
    return await capture_with_browser(browser, timeout=120)


async def test_every_section_is_present_without_errors(record: dict) -> None:
    for name in SECTIONS:
        assert name in record, name
        assert not (isinstance(record[name], dict) and "error" in record[name]), (
            name,
            record[name],
        )
    assert record["meta"]["schema"] == 1
    assert record["meta"]["source"]["kind"] == "local"
    assert record["meta"]["secureContext"] is True


async def test_headers_carry_client_hints(record: dict) -> None:
    values = record["headers"]["fetch"]["values"]
    assert "sec-ch-ua" in values and "sec-ch-ua-platform" in values
    # Accept-CH on the first response must make the browser send high-entropy hints.
    assert "sec-ch-ua-platform-version" in values
    assert "sec-ch-ua-full-version-list" in values
    assert values["user-agent"] == record["navigator"]["userAgent"]
    navigation = record["headers"]["navigation"]["values"]
    assert navigation["sec-fetch-dest"] == "document"


async def test_window_worker_and_iframe_agree(record: dict) -> None:
    worker, frame, nav = record["worker"], record["iframe"], record["navigator"]
    assert worker.get("error") is None
    assert worker["userAgent"] == nav["userAgent"] == frame["userAgent"]
    assert worker["platform"] == nav["platform"] == frame["platform"]
    assert (
        worker["hardwareConcurrency"] == nav["hardwareConcurrency"] == frame["hardwareConcurrency"]
    )
    assert worker["timeZone"] == record["intl"]["dateTimeFormat"]["timeZone"] == frame["timeZone"]
    assert worker["webgl"]["unmaskedRenderer"] == record["webgl"]["unmaskedRenderer"]
    assert frame["webglRenderer"] == record["webgl"]["unmaskedRenderer"]
    # Same device → same pixels, whichever context draws them.
    text = record["canvas"]["scenes"]["text"]
    assert text["stable"] is True
    assert text["pixelsHash"] == text["offscreenPixelsHash"] == worker["canvas"]["text"]
    assert text["pixelsHash"] == frame["canvasTextHash"]
    assert record["canvas"]["white"]["pure"] is True
    assert record["webgl"]["renderStable"] is True
    assert record["audio"]["offline"]["stable"] is True


async def test_webgl_dump_is_complete(record: dict) -> None:
    gl = record["webgl"]
    assert gl["available"] is True and gl["unmaskedRenderer"]
    assert gl["parameters"]["MAX_TEXTURE_SIZE"] >= 2048
    assert len(gl["parameters"]["MAX_VIEWPORT_DIMS"]) == 2
    assert gl["precision"]["FRAGMENT_SHADER.HIGH_FLOAT"] is not None
    assert "WEBGL_debug_renderer_info" in gl["extensions"]
    gl2 = record["webgl2"]
    assert gl2["available"] is True
    assert "MAX_3D_TEXTURE_SIZE" in gl2["parameters"]


async def test_summary_describes_this_browser(record: dict, browser: Browser) -> None:
    summary = summarize(record)
    assert summary.os == "linux"
    assert summary.chrome_major is not None
    assert str(browser.version.get("product", "")).endswith(summary.chrome_version or "?")
    assert summary.gpu_slug not in ("no-webgl", "unknown-gpu")


async def test_second_capture_is_deterministic(record: dict, browser: Browser) -> None:
    again = await capture_with_browser(browser, timeout=120)
    grouped = by_category(diff(record, again))
    assert grouped["deterministic"] == [], [d.describe() for d in grouped["deterministic"]]
    assert grouped["noise"] == [], [d.describe() for d in grouped["noise"]]
    assert grouped["persona"] == []
