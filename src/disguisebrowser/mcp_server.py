"""MCP server: stealth browsers for AI agents, several sessions per process.

Every ``browser_launch`` starts an independent browser (own display, profile,
proxy, persona) and returns a ``session_id``; all other tools take that id.
The accessibility snapshot is the primary way for a model to "see" a page:
it is compact and yields ``[ref=eN]`` handles for ``click``/``type_text``.

Run with ``disguise mcp`` (stdio) or ``disguise mcp --http --port 8000``.
Requires the ``mcp`` extra: ``pip install 'disguisebrowser[mcp]'``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import logging
import secrets
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.server.mcpserver.utilities.types import Image
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise SystemExit(
        "The MCP server needs the 'mcp' extra: pip install 'disguisebrowser[mcp]'"
    ) from exc

from disguisebrowser import __version__
from disguisebrowser.browser import Browser, launch
from disguisebrowser.errors import DisguiseError
from disguisebrowser.page import ElementHandle, Page
from disguisebrowser.persona import GPU_PRESETS, Persona

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
DisguiseBrowser drives a real, stealth Google Chrome. Workflow:
1. browser_launch → session_id (one call per independent browser; several may run at once).
2. navigate(session_id, url), then snapshot(session_id) to read the page as an
   accessibility tree with [ref=eN] handles.
3. click / type_text / press_key / scroll with a ref (e.g. "e12") or a CSS selector.
4. extract_text / get_html / screenshot for content; wait_for before reading dynamic pages.
5. browser_close when done. Interactions are humanized and may take a second or two.
"""


@dataclass
class _Session:
    browser: Browser
    current: Page
    label: str = ""


@dataclass
class BrowserSessions:
    """Registry of running browsers, keyed by short session ids."""

    sessions: dict[str, _Session] = field(default_factory=dict)

    def get(self, session_id: str) -> _Session:
        try:
            return self.sessions[session_id]
        except KeyError:
            raise DisguiseError(
                f"unknown session {session_id!r}; call browser_launch first"
            ) from None

    async def page(self, session_id: str) -> Page:
        session = self.get(session_id)
        if session.current.closed:
            pages = session.browser.pages
            if not pages:
                session.current = await session.browser.new_page()
            else:
                session.current = pages[-1]
        return session.current

    async def close_all(self) -> None:
        sessions = list(self.sessions.values())
        self.sessions.clear()
        await asyncio.gather(*(s.browser.close() for s in sessions), return_exceptions=True)


async def _resolve_target(page: Page, target: str, timeout: float = 15.0) -> ElementHandle:
    if target.startswith("e") and target[1:].isdigit():
        return await page.element_by_ref(target)
    handle = await page.wait_for_selector(target, timeout=timeout, state="attached")
    if handle is None:
        raise DisguiseError(f"no element matches {target!r}")
    return handle


def _expected_failures(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Turn our domain errors into ToolError so the model gets the message."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except (DisguiseError, TimeoutError) as exc:
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc

    return wrapper


def build_server(sessions: BrowserSessions | None = None) -> MCPServer:  # noqa: PLR0915
    registry = sessions or BrowserSessions()
    server = MCPServer("disguisebrowser", instructions=INSTRUCTIONS, version=__version__)

    def tool(
        **meta: Any,
    ) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
        def register(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
            return server.tool(**meta)(_expected_failures(fn))

        return register

    @tool(description="Start an independent stealth browser; returns its session_id.")
    async def browser_launch(  # noqa: PLR0917 — MCP tool parameters are keyword arguments
        label: str = "",
        proxy: str | None = None,
        geoip: bool = False,
        timezone: str | None = None,
        languages: str | None = None,
        gpu: str | None = None,
        profile_dir: str | None = None,
        human: Literal["default", "careful", "fast"] = "default",
        display: Literal["auto", "host", "virtual"] = "auto",
        seed: int | None = None,
    ) -> dict[str, Any]:
        overrides: dict[str, Any] = {}
        if proxy:
            overrides["proxy"] = proxy
        if timezone:
            overrides["timezone"] = timezone
        if languages:
            overrides["languages"] = tuple(part.strip() for part in languages.split(","))
        if gpu:
            if gpu not in GPU_PRESETS:
                raise DisguiseError(f"unknown gpu preset {gpu!r}; one of {sorted(GPU_PRESETS)}")
            overrides["gpu"] = GPU_PRESETS[gpu]
        if seed is not None:
            overrides["seed"] = seed
        persona = Persona.from_host(**overrides)
        browser = await launch(
            persona=persona, display=display, user_data_dir=profile_dir, human=human, geoip=geoip
        )
        session_id = secrets.token_hex(4)
        page = browser.pages[0] if browser.pages else await browser.new_page()
        registry.sessions[session_id] = _Session(browser, page, label)
        return {
            "session_id": session_id,
            "chrome": browser.version.get("product"),
            "display": browser.display.display if browser.display else "host",
            "input": browser.input_mode,
            "timezone": browser.persona.timezone,
            "languages": list(browser.persona.languages),
            "gpu": browser.persona.gpu.renderer
            if browser.persona.gpu and browser.display
            else None,
            "exit_ip": browser.geo.ip if browser.geo else None,
        }

    @tool(description="Close a browser session and free its resources.")
    async def browser_close(session_id: str) -> str:
        session = registry.sessions.pop(session_id, None)
        if session is None:
            return f"session {session_id} was not running"
        await session.browser.close()
        return f"session {session_id} closed"

    @tool(description="List running browser sessions.")
    async def browser_list() -> list[dict[str, Any]]:
        return [
            {
                "session_id": sid,
                "label": s.label,
                "tabs": len(s.browser.pages),
                "url": s.current.url,
                "proxy": bool(s.browser.gateway),
            }
            for sid, s in registry.sessions.items()
        ]

    @tool(description="Navigate the current tab to a URL.")
    async def navigate(
        session_id: str,
        url: str,
        wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = "load",
    ) -> dict[str, str]:
        page = await registry.page(session_id)
        if "://" not in url:
            url = "https://" + url
        await page.goto(url, wait_until=wait_until)
        return {"url": page.url, "title": await page.title()}

    @tool(description="Go back in the tab's history.")
    async def go_back(session_id: str) -> dict[str, Any]:
        page = await registry.page(session_id)
        moved = await page.go_back()
        return {"moved": moved, "url": page.url}

    @tool(
        description="Read the page as an accessibility tree with [ref=eN] handles "
        "usable by click/type_text/hover. Call again after the page changes."
    )
    async def snapshot(session_id: str, max_lines: int = 400) -> str:
        page = await registry.page(session_id)
        snap = await page.snapshot(max_lines=max_lines)
        header = f"url: {page.url}\ntitle: {await page.title()}\n"
        return header + (snap.text or "(empty accessibility tree)")

    @tool(description="Humanized click on a ref (e.g. e12) or CSS selector.")
    async def click(session_id: str, target: str) -> str:
        page = await registry.page(session_id)
        await (await _resolve_target(page, target)).click()
        await asyncio.sleep(0.3)
        return f"clicked {target}; now at {page.url}"

    @tool(description="Move the pointer over a ref or CSS selector.")
    async def hover(session_id: str, target: str) -> str:
        page = await registry.page(session_id)
        await (await _resolve_target(page, target)).hover()
        return f"hovering {target}"

    @tool(
        description="Click a field (ref or CSS selector) and type text with human timing. "
        "clear=true selects and deletes existing text first."
    )
    async def type_text(
        session_id: str, target: str, text: str, clear: bool = False, press_enter: bool = False
    ) -> str:
        page = await registry.page(session_id)
        await (await _resolve_target(page, target)).type(text, clear=clear)
        if press_enter:
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.5)
        return f"typed {len(text)} characters into {target}"

    @tool(description="Press a key: Enter, Tab, Escape, ArrowDown, Backspace, a, …")
    async def press_key(session_id: str, key: str) -> str:
        page = await registry.page(session_id)
        await page.activate()
        await page.keyboard.press(key)
        return f"pressed {key}"

    @tool(description="Scroll the page by delta_y CSS pixels (negative = up).")
    async def scroll(session_id: str, delta_y: float = 600) -> str:
        page = await registry.page(session_id)
        await page.scroll(delta_y)
        position = await page.evaluate("[window.scrollY, document.documentElement.scrollHeight]")
        return f"scrollY={position[0]} of {position[1]}"

    @tool(description="Visible text of the page or of a CSS selector.")
    async def extract_text(
        session_id: str, selector: str | None = None, max_chars: int = 20000
    ) -> str:
        page = await registry.page(session_id)
        if selector:
            handle = await _resolve_target(page, selector)
            text = await handle.text()
        else:
            text = str(await page.evaluate("document.body ? document.body.innerText : ''"))
        return text[:max_chars] + ("\n…[truncated]" if len(text) > max_chars else "")

    @tool(description="HTML of the page or of a CSS selector.")
    async def get_html(session_id: str, selector: str | None = None, max_chars: int = 50000) -> str:
        page = await registry.page(session_id)
        if selector:
            html = await (await _resolve_target(page, selector)).outer_html()
        else:
            html = await page.content()
        return html[:max_chars] + ("\n…[truncated]" if len(html) > max_chars else "")

    @tool(description="PNG screenshot of the current tab.")
    async def screenshot(session_id: str, full_page: bool = False) -> Image:
        page = await registry.page(session_id)
        return Image(data=await page.screenshot(full_page=full_page), format="png")

    @tool(
        description="Evaluate a JavaScript expression in an isolated world (DOM access, "
        "no page globals) and return its JSON value."
    )
    async def evaluate(session_id: str, expression: str) -> dict[str, Any]:
        page = await registry.page(session_id)
        return {"value": await page.evaluate(expression)}

    @tool(description="Wait until a CSS selector is attached/visible/hidden.")
    async def wait_for(
        session_id: str,
        selector: str,
        state: Literal["attached", "visible", "hidden"] = "visible",
        timeout: float = 30,
    ) -> str:
        page = await registry.page(session_id)
        await page.wait_for_selector(selector, state=state, timeout=timeout)
        return f"{selector} is {state}"

    @tool(description="List tabs of a session; the current one is marked.")
    async def tabs(session_id: str) -> list[dict[str, Any]]:
        session = registry.get(session_id)
        current = await registry.page(session_id)
        return [
            {"index": i, "url": p.url, "current": p is current}
            for i, p in enumerate(session.browser.pages)
        ]

    @tool(description="Open a new tab (optionally at a URL) and make it current.")
    async def new_tab(session_id: str, url: str | None = None) -> dict[str, Any]:
        session = registry.get(session_id)
        page = await session.browser.new_page(url or "about:blank")
        session.current = page
        return {"index": session.browser.pages.index(page), "url": page.url}

    @tool(description="Switch to the tab with the given index.")
    async def select_tab(session_id: str, index: int) -> dict[str, Any]:
        session = registry.get(session_id)
        pages = session.browser.pages
        if not 0 <= index < len(pages):
            raise DisguiseError(f"tab index {index} out of range (0..{len(pages) - 1})")
        session.current = pages[index]
        await session.current.bring_to_front()
        return {"index": index, "url": session.current.url}

    @tool(description="Close the current tab (or the one at index).")
    async def close_tab(session_id: str, index: int | None = None) -> str:
        session = registry.get(session_id)
        pages = session.browser.pages
        page = pages[index] if index is not None and 0 <= index < len(pages) else session.current
        await page.close()
        return f"closed tab {page.url}"

    return server


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="disguise mcp", description="DisguiseBrowser MCP server")
    parser.add_argument(
        "--http", action="store_true", help="serve Streamable HTTP instead of stdio"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("-v", "--verbose", action="count", default=0)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING - 10 * min(args.verbose, 2),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    registry = BrowserSessions()
    server = build_server(registry)

    async def serve() -> None:
        try:
            if args.http:
                await server.run_streamable_http_async(host=args.host, port=args.port)
            else:
                await server.run_stdio_async()
        finally:
            # Same event loop as the browsers — a clean shutdown of Chrome, Xvfb
            # and gateways instead of relying on PR_SET_PDEATHSIG.
            with contextlib.suppress(Exception):
                await registry.close_all()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
