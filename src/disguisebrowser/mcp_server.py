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
from disguisebrowser.browser import connect, launch
from disguisebrowser.camoufox.profiles import OSES, ProfileError, ProfileStore
from disguisebrowser.errors import DisguiseError
from disguisebrowser.persona import GPU_PRESETS, Persona
from disguisebrowser.protocols import BrowserLike, ElementLike, PageLike

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
DisguiseBrowser drives a stealth browser: real Google Chrome (engine="chrome", default) or
Camoufox, a Firefox build (engine="camoufox"). Workflow:
1. browser_launch → session_id (one call per independent browser; several may run at once).
   For a persistent, pinned identity use engine="camoufox" with a profile name
   (profile_create / profile_list first, or create=true on launch).
2. navigate(session_id, url), then snapshot(session_id) to read the page as an
   accessibility tree with [ref=eN] handles.
3. click / type_text / press_key / scroll with a ref (e.g. "e12") or a CSS selector.
4. extract_text / get_html / screenshot for content; wait_for before reading dynamic pages.
5. warmup(session_id) once on a fresh profile, before the target site.
6. browser_close when done. Interactions are humanized and may take a second or two.
   browser_connect attaches to a Chrome that is already running and does not close it.
"""


@dataclass
class _Session:
    browser: BrowserLike
    current: PageLike
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

    async def page(self, session_id: str) -> PageLike:
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


async def _resolve_target(page: PageLike, target: str, timeout: float = 15.0) -> ElementLike:
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

    @tool(
        description="Start an independent stealth browser; returns its session_id. "
        "engine='camoufox' (Firefox) takes profile (a stored identity name) instead of "
        "profile_dir and ignores gpu/seed: the device comes from the profile."
    )
    async def browser_launch(  # noqa: PLR0917 — MCP tool parameters are keyword arguments
        label: str = "",
        proxy: str | None = None,
        geoip: bool | None = None,
        timezone: str | None = None,
        languages: str | None = None,
        gpu: str | None = None,
        profile_dir: str | None = None,
        human: Literal["default", "careful", "fast"] = "default",
        display: Literal["auto", "host", "virtual"] = "auto",
        seed: int | None = None,
        engine: Literal["chrome", "camoufox"] = "chrome",
        profile: str | None = None,
        create: bool = False,
        os: Literal["windows", "macos", "linux"] | None = None,
    ) -> dict[str, Any]:
        overrides: dict[str, Any] = {}
        if timezone:
            overrides["timezone"] = timezone
        if languages:
            overrides["languages"] = tuple(part.strip() for part in languages.split(","))
        browser: BrowserLike
        if engine == "camoufox":
            if gpu or seed is not None or profile_dir:
                raise DisguiseError(
                    "gpu, seed and profile_dir are chrome options; for camoufox pass a "
                    "profile (see profile_create)"
                )
            from disguisebrowser.camoufox import launch as camoufox_launch  # noqa: PLC0415

            browser = await camoufox_launch(
                profile=profile,
                create=create,
                os=os,
                persona=Persona.from_host(**overrides) if overrides else None,
                proxy=proxy,
                geoip=geoip,
                human=human,
                display=display,
            )
        else:
            if profile or create or os:
                raise DisguiseError("profile, create and os apply to engine='camoufox' only")
            if proxy:
                overrides["proxy"] = proxy
            if gpu:
                if gpu not in GPU_PRESETS:
                    raise DisguiseError(f"unknown gpu preset {gpu!r}; one of {sorted(GPU_PRESETS)}")
                overrides["gpu"] = GPU_PRESETS[gpu]
            if seed is not None:
                overrides["seed"] = seed
            browser = await launch(
                persona=Persona.from_host(**overrides),
                display=display,
                user_data_dir=profile_dir,
                human=human,
                geoip=geoip,
            )
        session_id = secrets.token_hex(4)
        page = browser.pages[0] if browser.pages else await browser.new_page()
        registry.sessions[session_id] = _Session(browser, page, label)
        info: dict[str, Any] = {
            "session_id": session_id,
            "engine": browser.engine,
            "browser": browser.version.get("product"),
            "display": getattr(browser, "display_mode", None)
            or (browser.display.display if browser.display else "host"),
            "input": browser.input_mode,
            "timezone": browser.persona.timezone,
            "languages": list(browser.persona.languages),
            "gpu": browser.persona.gpu.renderer
            if browser.persona.gpu and browser.display
            else None,
            "exit_ip": browser.geo.ip if browser.geo else None,
        }
        if engine == "chrome":
            info["chrome"] = info["browser"]  # key kept for existing clients
        else:
            stored = getattr(browser, "profile", None)
            info["profile"] = stored.name if stored else None
        return info

    @tool(description="List stored Camoufox identities (profiles) with their pinned devices.")
    async def profile_list() -> list[dict[str, Any]]:
        return [p.summary() for p in ProfileStore().list()]

    @tool(
        description="Create a persistent Camoufox identity: a pinned device plus its own "
        "cookies and storage. Launch it later with "
        "browser_launch(engine='camoufox', profile=name). Deleting profiles is CLI-only."
    )
    async def profile_create(  # noqa: PLR0917 — MCP tool parameters are keyword arguments
        name: str,
        os: Literal["windows", "macos", "linux"] | None = None,
        proxy: str | None = None,
        geoip: bool = False,
        languages: str | None = None,
        timezone: str | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        if os is not None and os not in OSES:
            raise DisguiseError(f"os must be one of {', '.join(OSES)}")
        try:
            profile = ProfileStore().create(
                name,
                os=os,
                proxy=proxy,
                geoip=geoip,
                languages=tuple(p.strip() for p in languages.split(",")) if languages else None,
                timezone=timezone,
                notes=notes,
            )
        except ProfileError as exc:
            raise DisguiseError(str(exc)) from None
        return profile.summary()

    @tool(
        description="Attach to a Chrome already listening on a loopback DevTools port. "
        "Closing the session disconnects only; it does not stop that Chrome."
    )
    async def browser_connect(port: int, label: str = "") -> dict[str, Any]:
        browser = await connect(port=port)
        session_id = secrets.token_hex(4)
        page = browser.pages[0] if browser.pages else await browser.new_page()
        registry.sessions[session_id] = _Session(browser, page, label)
        return {
            "session_id": session_id,
            "engine": browser.engine,
            "chrome": browser.version.get("product"),
            "attached": True,
            "pages": len(browser.pages),
        }

    @tool(
        description=(
            "Visit ordinary sites so a fresh profile gains history, cache and cookies. "
            "Pass urls to override the built-in list."
        )
    )
    async def warmup(
        session_id: str, urls: list[str] | None = None, count: int = 3
    ) -> list[dict[str, Any]]:
        session = registry.get(session_id)
        visits = await session.browser.warmup(urls=urls, count=count)
        return [
            {"url": visit.url, "ok": visit.ok, "title": visit.title, "error": visit.error}
            for visit in visits
        ]

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
                "engine": s.browser.engine,
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

    @tool(
        description="How to answer alert/confirm/prompt when the caller is not waiting on one. "
        "prompt_text is sent when action is accept and the dialog is a prompt."
    )
    async def set_dialogs(
        session_id: str,
        action: Literal["accept", "dismiss"] = "dismiss",
        prompt_text: str = "",
    ) -> str:
        page = await registry.page(session_id)
        page.dialog_action = action
        page.dialog_prompt = prompt_text
        return f"dialogs will be {action}ed"

    @tool(
        description="Turn on passive HTTP capture (nothing is intercepted or rewritten) "
        "and list responses seen since it was turned on."
    )
    async def list_responses(session_id: str, url_contains: str = "") -> list[dict[str, Any]]:
        page = await registry.page(session_id)
        await page.watch_responses()
        rows = page.responses()
        if url_contains:
            rows = [row for row in rows if url_contains in row.url]
        return [
            {"url": row.url, "method": row.method, "status": row.status, "type": row.resource_type}
            for row in rows[-50:]
        ]

    @tool(description="Text body of the latest captured response whose URL contains url_contains.")
    async def response_body(session_id: str, url_contains: str, max_chars: int = 20000) -> str:
        page = await registry.page(session_id)
        await page.watch_responses()
        matches = [row for row in page.responses() if url_contains in row.url]
        if not matches:
            raise DisguiseError(f"no captured response contains {url_contains!r}")
        text = await matches[-1].text()
        return text[:max_chars] + ("\n…[truncated]" if len(text) > max_chars else "")

    @tool(description="Put a local file into an <input type=file> (ref or CSS selector).")
    async def upload_file(session_id: str, target: str, path: str) -> str:
        page = await registry.page(session_id)
        await (await _resolve_target(page, target)).set_input_files(path)
        return f"set {path} on {target}"

    @tool(
        description="Click a ref or CSS selector that starts a download, wait for the file, "
        "and return its name, size and text when the bytes are UTF-8."
    )
    async def download_file(session_id: str, target: str, timeout: float = 30) -> dict[str, Any]:
        page = await registry.page(session_id)
        async with page.expect_download(timeout=timeout) as pending:
            await (await _resolve_target(page, target)).click()
        download = await pending.value()
        data = download.path.read_bytes()
        try:
            text: str | None = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        return {
            "filename": download.suggested_filename,
            "bytes": len(data),
            "path": str(download.path),
            "text": text,
        }

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
