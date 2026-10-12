"""DisguiseBrowser — live, stealth browser automation on the real system Chrome.

Quick start::

    import asyncio
    from disguisebrowser import launch

    async def main() -> None:
        async with launch() as browser:
            page = await browser.new_page("https://example.com")
            print(await page.title())

    asyncio.run(main())

Firefox via Camoufox, with pinned persistent identities (needs the ``camoufox`` extra)::

    from disguisebrowser import launch_camoufox

    async with launch_camoufox(profile="shop-1", create=True, os="windows") as browser:
        ...
"""

from typing import TYPE_CHECKING, Any

from disguisebrowser.browser import Browser, connect, launch
from disguisebrowser.camoufox.profiles import Profile, ProfileStore
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
from disguisebrowser.warmup import WarmupVisit, choose_urls, warmup

if TYPE_CHECKING:
    from disguisebrowser.camoufox import CamoufoxBrowser
    from disguisebrowser.camoufox import launch as launch_camoufox

__all__ = [
    "GPU_PRESETS",
    "PRESETS",
    "Browser",
    "CamoufoxBrowser",
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
    "Profile",
    "ProfileStore",
    "QuietCdpViolationError",
    "Screen",
    "VirtualDisplay",
    "WarmupVisit",
    "Window",
    "choose_urls",
    "connect",
    "launch",
    "launch_camoufox",
    "warmup",
]


def __getattr__(name: str) -> Any:
    # The Camoufox engine is an optional extra: import it only when asked for.
    if name == "launch_camoufox":
        from disguisebrowser.camoufox import launch as camoufox_launch  # noqa: PLC0415

        return camoufox_launch
    if name == "CamoufoxBrowser":
        from disguisebrowser.camoufox import CamoufoxBrowser  # noqa: PLC0415

        return CamoufoxBrowser
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__version__ = "0.3.0"
