"""Camoufox engine: persistent, per-profile Firefox identities.

Install with ``pip install 'disguisebrowser[camoufox]'`` and fetch the browser
once with ``camoufox fetch``. The profile store works without the extra; the
browser classes are imported on first use so that listing profiles never needs
Playwright.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from disguisebrowser.camoufox.profiles import (
    OSES,
    Profile,
    ProfileError,
    ProfileStore,
    default_root,
)

if TYPE_CHECKING:
    from disguisebrowser.camoufox.browser import CamoufoxBrowser, launch
    from disguisebrowser.camoufox.page import CamoufoxElement, CamoufoxPage

__all__ = [
    "OSES",
    "CamoufoxBrowser",
    "CamoufoxElement",
    "CamoufoxPage",
    "Profile",
    "ProfileError",
    "ProfileStore",
    "default_root",
    "launch",
]

_LAZY = {
    "CamoufoxBrowser": "browser",
    "launch": "browser",
    "CamoufoxPage": "page",
    "CamoufoxElement": "page",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        import importlib  # noqa: PLC0415

        module = importlib.import_module(f"disguisebrowser.camoufox.{module_name}")
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.split(".")[0] in {"camoufox", "playwright"}:
            raise ImportError(
                "the Camoufox engine needs the 'camoufox' extra: "
                "pip install 'disguisebrowser[camoufox]' && camoufox fetch"
            ) from exc
        raise
    return getattr(module, name)
