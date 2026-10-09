"""DisguiseBrowser — live, stealth browser automation on the real system Chrome.

Quick start::

    import asyncio
    from disguisebrowser import launch

    async def main() -> None:
        async with launch() as browser:
            page = await browser.new_page("https://example.com")
            print(await page.title())

    asyncio.run(main())
"""

from disguisebrowser.browser import Browser, launch
from disguisebrowser.display import VirtualDisplay
from disguisebrowser.errors import (
    CdpError,
    DisguiseError,
    ElementNotFoundError,
    EvaluationError,
    LaunchError,
    NavigationError,
    QuietCdpViolationError,
)
from disguisebrowser.input import PRESETS, HumanProfile
from disguisebrowser.page import ElementHandle, Page
from disguisebrowser.persona import GPU_PRESETS, GpuIdentity, Persona, Screen, Window

__all__ = [
    "GPU_PRESETS",
    "PRESETS",
    "Browser",
    "CdpError",
    "DisguiseError",
    "ElementHandle",
    "ElementNotFoundError",
    "EvaluationError",
    "GpuIdentity",
    "HumanProfile",
    "LaunchError",
    "NavigationError",
    "Page",
    "Persona",
    "QuietCdpViolationError",
    "Screen",
    "VirtualDisplay",
    "Window",
    "launch",
]

__version__ = "0.1.0"
