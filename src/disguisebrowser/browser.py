"""Browser: owns the Chrome process, the CDP connection, the optional virtual
display and the set of pages. Entry point is :func:`launch`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from collections.abc import Generator
from dataclasses import replace
from typing import Any, Literal

from disguisebrowser.cdp import Connection, QuietCdpPolicy, Session
from disguisebrowser.display import VirtualDisplay
from disguisebrowser.errors import CdpError, LaunchError
from disguisebrowser.input import PRESETS, HumanProfile
from disguisebrowser.launcher import ChromeProcess, launch_chrome
from disguisebrowser.page import Page
from disguisebrowser.persona import GPU_PRESETS, Persona

log = logging.getLogger(__name__)

DisplayMode = Literal["auto", "host", "virtual"]


class Browser:
    def __init__(
        self,
        connection: Connection,
        chrome: ChromeProcess,
        *,
        persona: Persona,
        display: VirtualDisplay | None,
        human: HumanProfile,
    ) -> None:
        self.connection = connection
        self.chrome = chrome
        self.persona = persona
        self.display = display
        self.human = human
        self.version: dict[str, Any] = {}
        self._rng = persona.rng("input") if persona.seed is not None else random.Random()
        self._pages: dict[str, Page] = {}
        self._initializing: set[str] = set()
        self._page_futures: dict[str, asyncio.Future[Page]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    # ------------------------------------------------------------------ setup
    @property
    def root(self) -> Session:
        return self.connection.root

    async def _setup(self) -> None:
        self.version = await self.root.send("Browser.getVersion")
        self.root.on("Target.attachedToTarget", self._on_attached)
        self.root.on("Target.detachedFromTarget", self._on_detached)
        await self.root.send(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
        )
        # Browser-level auto-attach covers new targets and (usually) the existing
        # ones; make sure every existing page is attached *and* initialized.
        targets = await self.root.send("Target.getTargets")
        for info in targets["targetInfos"]:
            if info["type"] == "page":
                await self._ensure_page(info["targetId"])

    def _on_attached(self, params: dict[str, Any]) -> None:
        """Synchronous bookkeeping; the asynchronous part runs in a tracked task."""
        info = params["targetInfo"]
        session_id = params["sessionId"]
        target_id = info["targetId"]
        session = self.connection.session(session_id)
        if info["type"] != "page":
            # iframes, workers, service workers: nothing to configure yet — let them run.
            if params.get("waitingForDebugger"):
                self._spawn(self._resume(session, info["type"]))
            return
        if target_id in self._pages or target_id in self._initializing:
            # Auto-attach and an explicit attach raced; keep the first session only.
            self._spawn(self._detach_duplicate(session_id))
            return
        self._initializing.add(target_id)
        self._spawn(self._initialize_page(session, target_id))

    async def _initialize_page(self, session: Session, target_id: str) -> None:
        page = Page(self, session, target_id, human=self.human, rng=self._rng)
        try:
            await page._initialize()
        except CdpError as exc:
            log.warning("page %s failed to initialize: %s", target_id, exc)
            future = self._page_futures.pop(target_id, None)
            if future is not None and not future.done():
                future.set_exception(LaunchError(f"page {target_id} failed to initialize: {exc}"))
            return
        finally:
            self._initializing.discard(target_id)
        self._pages[target_id] = page
        future = self._page_futures.pop(target_id, None)
        if future is not None and not future.done():
            future.set_result(page)

    async def _resume(self, session: Session, kind: str) -> None:
        try:
            await session.send("Runtime.runIfWaitingForDebugger")
        except CdpError as exc:
            log.debug("could not resume %s target: %s", kind, exc)

    async def _detach_duplicate(self, session_id: str) -> None:
        try:
            await self.root.send("Target.detachFromTarget", {"sessionId": session_id})
        except CdpError as exc:
            log.debug("detachFromTarget: %s", exc)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_detached(self, params: dict[str, Any]) -> None:
        session_id = params.get("sessionId")
        target_id = params.get("targetId")
        if session_id:
            self.connection.drop_session(session_id)
        if target_id and target_id in self._pages:
            self._pages.pop(target_id)._closed = True

    async def _ensure_page(self, target_id: str, timeout: float = 10.0) -> Page:
        """Return the initialized Page for ``target_id``, attaching if necessary."""
        existing = self._pages.get(target_id)
        if existing is not None:
            return existing
        future = self._page_futures.get(target_id)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            self._page_futures[target_id] = future
        if target_id not in self._initializing:
            try:
                await self.root.send(
                    "Target.attachToTarget", {"targetId": target_id, "flatten": True}
                )
            except CdpError as exc:
                self._page_futures.pop(target_id, None)
                raise LaunchError(f"cannot attach to page {target_id}: {exc}") from exc
        return await asyncio.wait_for(future, timeout)

    # ------------------------------------------------------------------ pages
    @property
    def pages(self) -> list[Page]:
        return [p for p in self._pages.values() if not p.closed]

    async def new_page(self, url: str = "about:blank", **goto_kwargs: Any) -> Page:
        result = await self.root.send("Target.createTarget", {"url": "about:blank"})
        page = await self._ensure_page(result["targetId"])
        if url and url != "about:blank":
            await page.goto(url, **goto_kwargs)
        return page

    async def _close_page(self, page: Page) -> None:
        self._pages.pop(page.target_id, None)
        try:
            await self.root.send("Target.closeTarget", {"targetId": page.target_id})
        except CdpError as exc:
            log.debug("closeTarget: %s", exc)

    # ------------------------------------------------------------------ shutdown
    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        try:
            await self.root.send("Browser.close", timeout=5)
        except Exception as exc:  # best effort — the process is terminated below anyway
            log.debug("Browser.close: %s", exc)
        await self.connection.close()
        await self.chrome.terminate()
        if self.display is not None:
            await self.display.stop()

    async def __aenter__(self) -> Browser:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def __repr__(self) -> str:
        return f"<Browser {self.version.get('product', '?')} pages={len(self._pages)}>"


class LaunchHandle:
    """Awaitable *and* async-context-manager result of :func:`launch`."""

    def __init__(self, coro_factory: Any) -> None:
        self._factory = coro_factory
        self._browser: Browser | None = None

    def __await__(self) -> Generator[Any, None, Browser]:
        return self._start().__await__()

    async def _start(self) -> Browser:
        if self._browser is None:
            self._browser = await self._factory()
        assert self._browser is not None
        return self._browser

    async def __aenter__(self) -> Browser:
        return await self._start()

    async def __aexit__(self, *exc: object) -> None:
        if self._browser is not None:
            await self._browser.close()


def launch(
    *,
    persona: Persona | None = None,
    display: DisplayMode | VirtualDisplay = "auto",
    headless: bool = False,
    executable: str | os.PathLike[str] | None = None,
    user_data_dir: str | os.PathLike[str] | None = None,
    args: tuple[str, ...] | list[str] = (),
    human: str | HumanProfile = "default",
    window_manager: bool = True,
    unsafe: bool = False,
) -> LaunchHandle:
    """Start a stealth Chrome.

    ``display``: ``"host"`` uses the current ``$DISPLAY``; ``"virtual"`` starts an
    Xvfb desktop with a Mesa GPU identity; ``"auto"`` picks ``host`` when a
    display is available and ``virtual`` otherwise. ``headless`` is an explicit
    opt-in with weaker stealth (see ADR-4).

    Usage::

        async with launch() as browser:
            page = await browser.new_page("https://example.com")

        browser = await launch()
    """
    profile = PRESETS[human] if isinstance(human, str) else human

    async def start() -> Browser:
        chosen_persona = persona or Persona.from_host()
        virtual: VirtualDisplay | None = None
        if display == "host" and not headless and not os.environ.get("DISPLAY"):
            raise LaunchError(
                "display='host' requires $DISPLAY; use display='virtual' (Xvfb) on servers"
            )
        if isinstance(display, VirtualDisplay):
            virtual = display
        elif not headless and (
            display == "virtual" or (display == "auto" and not os.environ.get("DISPLAY"))
        ):
            if chosen_persona.gpu is None:
                # Stealthy by default: llvmpipe would otherwise announce "no real GPU".
                preset = chosen_persona.rng("gpu").choice(sorted(GPU_PRESETS))
                chosen_persona = replace(chosen_persona, gpu=GPU_PRESETS[preset])
            virtual = VirtualDisplay(
                chosen_persona.screen, gpu=chosen_persona.gpu, window_manager=window_manager
            )
        env: dict[str, str] = {}
        extra_args = tuple(args)
        if virtual is not None:
            await virtual.start()
            env = virtual.environment()
            extra_args = virtual.chrome_args + extra_args
        try:
            chrome = await launch_chrome(
                persona=chosen_persona,
                executable=executable,
                user_data_dir=user_data_dir,
                headless=headless,
                extra_args=extra_args,
                env=env,
            )
        except Exception:
            if virtual is not None:
                await virtual.stop()
            raise
        try:
            connection = await Connection.open(chrome.ws_url, policy=QuietCdpPolicy(unsafe=unsafe))
            browser = Browser(
                connection, chrome, persona=chosen_persona, display=virtual, human=profile
            )
            await browser._setup()
        except Exception:
            await chrome.terminate()
            if virtual is not None:
                await virtual.stop()
            raise
        log.info(
            "%s ready on %s", browser.version.get("product"), env.get("DISPLAY", "host display")
        )
        return browser

    return LaunchHandle(start)
