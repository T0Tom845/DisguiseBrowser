"""Run the capture page inside a browser driven by this library."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from disguisebrowser import __version__
from disguisebrowser.fingerprint.record import SCHEMA
from disguisebrowser.fingerprint.server import CaptureServer

if TYPE_CHECKING:
    from disguisebrowser.protocols import BrowserLike

log = logging.getLogger(__name__)


def annotate(record: dict[str, Any], **source: Any) -> dict[str, Any]:
    """Record who captured this and from where (``meta.source``)."""
    meta = record.setdefault("meta", {})
    meta["capturedBy"] = f"disguise capture {__version__}"
    source.setdefault("address", meta.pop("remoteAddress", None))
    meta["source"] = {k: v for k, v in source.items() if v is not None}
    if meta.get("schema") != SCHEMA:
        log.warning("record schema %s differs from the tool's %s", meta.get("schema"), SCHEMA)
    return record


async def capture_with_browser(browser: BrowserLike, *, timeout: float = 120.0) -> dict[str, Any]:
    """Open the capture page in a new tab of ``browser`` and return the record.

    The page's own script does the collecting in its main world, which is the
    honest view a site gets; our CDP session only navigates and waits for the
    submission on the local server.
    """
    with CaptureServer() as server:
        page = await browser.new_page()
        try:
            await page.goto(server.url, wait_until="load", timeout=timeout)
            record = await server.wait(timeout)
        finally:
            await page.close()
    display = "virtual" if browser.display else "host"
    return annotate(
        record,
        kind="local",
        display=display,
        engine=browser.engine,
        browser=browser.version.get("product"),
        input=browser.input_mode,
        persona_gpu=browser.persona.gpu.renderer
        if browser.display and browser.persona.gpu
        else None,
        timezone=browser.persona.timezone,
        languages=list(browser.persona.languages),
    )
