"""Browser: owns the Chrome process, the CDP connection, the optional virtual
display and the set of pages. Entry point is :func:`launch`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import secrets
import shutil
import tempfile
from collections.abc import Generator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from disguisebrowser.cdp import Connection, QuietCdpPolicy, Session
from disguisebrowser.display import VirtualDisplay
from disguisebrowser.errors import CdpError, ConnectionClosedError, EvaluationError, LaunchError
from disguisebrowser.exchange import Download, wants_geoip
from disguisebrowser.input import PRESETS, HumanProfile
from disguisebrowser.input.devices import CdpInputBackend, InputBackend
from disguisebrowser.input.xtest import XTestInputBackend
from disguisebrowser.launcher import (
    AttachedChrome,
    ChromeProcess,
    _http_get_json,
    launch_chrome,
)
from disguisebrowser.page import Page
from disguisebrowser.persona import GPU_PRESETS, Persona
from disguisebrowser.proxy import (
    GeoInfo,
    ProxyGateway,
    UpstreamProxy,
    languages_for_country,
    resolve_geo,
)
from disguisebrowser.warmup import WarmupVisit
from disguisebrowser.warmup import warmup as warmup_browser

log = logging.getLogger(__name__)

DisplayMode = Literal["auto", "host", "virtual"]
InputMode = Literal["auto", "cdp", "xtest"]
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_WORKER_TYPES = frozenset({"worker", "service_worker", "shared_worker"})


@dataclass
class AttachedTarget:
    """An iframe or worker paused at start and resumed without ``Runtime.enable``."""

    target_id: str
    session_id: str
    type: str
    url: str
    # Isolated world of an iframe, created on first use (see ``Browser._iframe_context``).
    context_id: int | None = None


class Browser:
    engine = "chrome"

    def __init__(
        self,
        connection: Connection,
        chrome: ChromeProcess | AttachedChrome,
        *,
        persona: Persona,
        display: VirtualDisplay | None,
        human: HumanProfile,
        gateway: ProxyGateway | None = None,
        geo: GeoInfo | None = None,
        input_mode: InputMode = "cdp",
        owns_process: bool = True,
    ) -> None:
        self.connection = connection
        self.chrome = chrome
        self.persona = persona
        self.display = display
        self.human = human
        self.gateway = gateway
        self.geo = geo
        self.input_mode: InputMode = input_mode
        self.owns_process = owns_process
        self.version: dict[str, Any] = {}
        self._rng = persona.rng("input") if persona.seed is not None else random.Random()
        self._pages: dict[str, Page] = {}
        self._children: dict[str, AttachedTarget] = {}
        self._child_initializing: set[str] = set()
        self._active_page: Page | None = None
        self._initializing: set[str] = set()
        self._page_futures: dict[str, asyncio.Future[Page]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._download_dir: Path | None = None
        self._download_waiters: list[asyncio.Future[Download]] = []
        self._download_meta: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ setup
    @property
    def root(self) -> Session:
        return self.connection.root

    def _input_backend(self, page: Page, session: Session) -> InputBackend:
        if self.input_mode == "xtest":
            display_name = self.display.display if self.display else os.environ.get("DISPLAY")
            if display_name:
                return XTestInputBackend(
                    display_name, page, session, owns_layout=self.display is not None
                )
            log.warning("input='xtest' requested but no X display is known; using CDP input")
        return CdpInputBackend(session)

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
        log.debug("attached %s %s (%s)", info["type"], info.get("url", ""), target_id[:8])
        if info["type"] != "page":
            if target_id in self._children or target_id in self._child_initializing:
                self._spawn(self._detach_duplicate(session_id))
                return
            self._child_initializing.add(target_id)
            self._spawn(
                self._initialize_child(session, info, bool(params.get("waitingForDebugger")))
            )
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

    async def _initialize_child(
        self, session: Session, info: dict[str, Any], waiting: bool
    ) -> None:
        """Prepare an OOPIF or worker, then let its first script run.

        ``waitForDebuggerOnStart`` pauses these targets before any script. Pages
        get an isolated world; workers get nothing observable — in particular
        not ``Runtime.enable``, which is the leak this client exists to avoid.
        """
        target_id = info["targetId"]
        kind = info["type"]
        if not session.session_id:
            log.warning("attached %s target without a session id; resuming", kind)
            self._child_initializing.discard(target_id)
            if waiting:
                await self._resume(session, kind)
            return
        target = AttachedTarget(target_id, session.session_id, kind, str(info.get("url", "")))
        try:
            if kind == "iframe":
                await self._prepare_iframe(session, target)
            elif kind not in _WORKER_TYPES:
                log.debug("resuming unhandled target type %s", kind)
        except Exception as exc:
            # Never leave a paused child behind: a frozen iframe is far more
            # visible than a missing isolated world.
            log.warning("target %s (%s) setup failed: %s", target_id, kind, exc)
        finally:
            self._children[target_id] = target
            self._child_initializing.discard(target_id)
            if waiting:
                await self._resume(session, kind)

    async def _prepare_iframe(self, session: Session, target: AttachedTarget) -> None:
        """Track the frame's URL. The isolated world is created lazily: before the
        paused frame commits its first document, ``Page.createIsolatedWorld``
        does not answer, and the frame cannot commit while paused."""
        await session.send("Page.enable")

        def on_navigated(params: dict[str, Any]) -> None:
            frame = params.get("frame", {})
            if frame.get("id") == target.target_id:
                target.url = str(frame.get("url", target.url))
                target.context_id = None  # the old world died with the old document

        session.on("Page.frameNavigated", on_navigated)

    async def _iframe_context(self, target: AttachedTarget, session: Session) -> int:
        if target.context_id is None:
            world = await session.send(
                "Page.createIsolatedWorld",
                {
                    "frameId": target.target_id,
                    "worldName": "w" + secrets.token_hex(6),
                    "grantUniveralAccess": False,
                },
            )
            target.context_id = int(world["executionContextId"])
        return target.context_id

    async def _resume(self, session: Session, kind: str) -> None:
        try:
            await session.send("Runtime.runIfWaitingForDebugger")
        except (CdpError, ConnectionClosedError) as exc:
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
        if target_id:
            self._children.pop(target_id, None)

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

    @property
    def targets(self) -> list[AttachedTarget]:
        """Iframes and workers attached since launch, besides top-level pages."""
        return list(self._children.values())

    async def evaluate_target(self, target_id: str, expression: str) -> Any:
        """Evaluate ``expression`` in an attached iframe (isolated world) or worker."""
        target = self._children.get(target_id)
        if target is None:
            raise LaunchError(f"no attached target {target_id}")
        params: dict[str, Any] = {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        }
        session = self.connection.session(target.session_id)
        if target.type == "iframe":
            params["contextId"] = await self._iframe_context(target, session)
        result = await session.send("Runtime.evaluate", params)
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            text = (details.get("exception") or {}).get("description") or details.get("text")
            raise EvaluationError(str(text or "JavaScript error"), details)
        value: Any = result.get("result", {}).get("value")
        return value

    async def warmup(
        self,
        urls: list[str] | None = None,
        *,
        count: int = 3,
        dwell: tuple[float, float] = (1.0, 2.5),
    ) -> list[WarmupVisit]:
        """Visit ordinary sites so the profile is not an empty first launch.

        See :func:`disguisebrowser.warmup.warmup`.
        """
        return await warmup_browser(self, urls=urls, count=count, dwell=dwell)

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
        if self.owns_process:
            try:
                await self.root.send("Browser.close", timeout=5)
            except Exception as exc:  # best effort — the process is terminated below anyway
                log.debug("Browser.close: %s", exc)
        await self.connection.close()
        if self.owns_process:
            await self.chrome.terminate()
            if self.display is not None:
                await self.display.stop()
            if self.gateway is not None:
                await self.gateway.stop()
        if self._download_dir is not None:
            shutil.rmtree(self._download_dir, ignore_errors=True)

    async def arm_download(self) -> asyncio.Future[Download]:
        """Allow a download and return a future for the next completed file.

        ``Browser.setDownloadBehavior`` only listens; it does not intercept the
        request. The saved name is the download's GUID so two files cannot
        overwrite each other. Register the future before the click.
        """
        if self._download_dir is None:
            directory = Path(tempfile.mkdtemp(prefix="disguise-dl-"))
            self._download_dir = directory
            self.root.on("Browser.downloadWillBegin", self._on_download_begin)
            self.root.on("Browser.downloadProgress", self._on_download_progress)
            await self.root.send(
                "Browser.setDownloadBehavior",
                {
                    "behavior": "allowAndName",
                    "downloadPath": str(directory),
                    "eventsEnabled": True,
                },
            )
        future: asyncio.Future[Download] = asyncio.get_running_loop().create_future()
        self._download_waiters.append(future)
        return future

    def _on_download_begin(self, params: dict[str, Any]) -> None:
        self._download_meta[str(params.get("guid", ""))] = params

    def _on_download_progress(self, params: dict[str, Any]) -> None:
        state = params.get("state")
        guid = str(params.get("guid", ""))
        if state == "inProgress":
            return
        meta = self._download_meta.pop(guid, {})
        if not self._download_waiters:
            return
        waiter = self._download_waiters.pop(0)
        if waiter.done():
            return
        if state != "completed" or self._download_dir is None:
            waiter.set_exception(LaunchError(f"download {guid or '?'} {state or 'failed'}"))
            return
        path = self._locate_download(guid, str(meta.get("suggestedFilename") or ""))
        waiter.set_result(
            Download(
                url=str(meta.get("url") or ""),
                suggested_filename=str(meta.get("suggestedFilename") or path.name),
                path=path,
            )
        )

    def _locate_download(self, guid: str, suggested: str) -> Path:
        assert self._download_dir is not None
        named = self._download_dir / guid
        if named.is_file():
            return named
        if suggested:
            alt = self._download_dir / suggested
            if alt.is_file():
                return alt
        files = [p for p in self._download_dir.iterdir() if p.is_file()]
        return files[-1] if files else named

    async def __aenter__(self) -> Browser:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def __repr__(self) -> str:
        return f"<Browser {self.version.get('product', '?')} pages={len(self._pages)}>"


class _Closable(Protocol):
    async def close(self) -> None: ...


class LaunchHandle[BrowserT: _Closable]:
    """Awaitable *and* async-context-manager result of :func:`launch`."""

    def __init__(self, coro_factory: Any) -> None:
        self._factory = coro_factory
        self._browser: BrowserT | None = None

    def __await__(self) -> Generator[Any, None, BrowserT]:
        return self._start().__await__()

    async def _start(self) -> BrowserT:
        if self._browser is None:
            self._browser = await self._factory()
        assert self._browser is not None
        return self._browser

    async def __aenter__(self) -> BrowserT:
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
    input: InputMode = "auto",
    geoip: bool | None = None,
    proxy_gateway: bool = True,
    window_manager: bool = True,
    unsafe: bool = False,
) -> LaunchHandle[Browser]:
    """Start a stealth Chrome.

    ``display``: ``"host"`` uses the current ``$DISPLAY``; ``"virtual"`` starts an
    Xvfb desktop with a Mesa GPU identity; ``"auto"`` picks ``host`` when a
    display is available and ``virtual`` otherwise. ``headless`` is an explicit
    opt-in with weaker stealth (see ADR-4).

    ``input``: ``"xtest"`` delivers mouse/keyboard events through the X server
    (genuine OS-level events, no ``screenX == pageX`` artifact); ``"cdp"`` uses
    DevTools ``Input.*``; ``"auto"`` picks XTest on a virtual display.

    ``geoip``: resolve the proxy's exit IP and align timezone and languages of
    the persona with it. ``None`` (the default) does this whenever
    ``persona.proxy`` is set; pass ``False`` to keep the host timezone behind a
    proxy. ``proxy_gateway=False`` hands the proxy URL to Chrome directly
    (credentials are then impossible).

    Usage::

        async with launch() as browser:
            page = await browser.new_page("https://example.com")

        browser = await launch()
    """
    profile = PRESETS[human] if isinstance(human, str) else human

    async def start() -> Browser:
        if display == "host" and not headless and not os.environ.get("DISPLAY"):
            raise LaunchError(
                "display='host' requires $DISPLAY; use display='virtual' (Xvfb) on servers"
            )
        cleanup: list[Any] = []  # async callables, run in reverse on failure
        try:
            # 1. Proxy gateway and geo alignment come first: the persona
            #    (timezone/languages) must be final before Chrome starts.
            chosen_persona = persona or Persona.from_host()
            chosen, gateway, geo, proxy_server = await _prepare_proxy(
                chosen_persona,
                geoip=wants_geoip(geoip, has_proxy=bool(chosen_persona.proxy)),
                use_gateway=proxy_gateway,
            )
            if gateway is not None:
                cleanup.append(gateway.stop)
            # 2. Display.
            chosen, virtual = _prepare_display(chosen, display, headless, window_manager)
            env: dict[str, str] = {}
            extra_args = tuple(args)
            if virtual is not None:
                await virtual.start()
                cleanup.append(virtual.stop)
                env = virtual.environment()
                extra_args = virtual.chrome_args + extra_args
            # 3. Chrome and the CDP connection.
            chrome = await launch_chrome(
                persona=chosen,
                executable=executable,
                user_data_dir=user_data_dir,
                headless=headless,
                extra_args=extra_args,
                env=env,
                proxy_server=proxy_server,
            )
            cleanup.append(chrome.terminate)
            connection = await Connection.open(chrome.ws_url, policy=QuietCdpPolicy(unsafe=unsafe))
            cleanup.append(connection.close)
            browser = Browser(
                connection,
                chrome,
                persona=chosen,
                display=virtual,
                human=profile,
                gateway=gateway,
                geo=geo,
                input_mode=_resolve_input_mode(input, virtual),
            )
            await browser._setup()
        except BaseException:
            for step in reversed(cleanup):
                with contextlib.suppress(Exception):
                    await step()
            raise
        log.info(
            "%s ready on %s, input=%s",
            browser.version.get("product"),
            env.get("DISPLAY", "host display"),
            browser.input_mode,
        )
        return browser

    return LaunchHandle(start)


async def _prepare_proxy(
    persona: Persona, *, geoip: bool, use_gateway: bool
) -> tuple[Persona, ProxyGateway | None, GeoInfo | None, str | None]:
    """Start the gateway for ``persona.proxy`` and optionally align the persona
    with the exit IP's geolocation. Returns the (possibly updated) persona, the
    gateway, the geo info and the ``--proxy-server`` value for Chrome."""
    if not persona.proxy:
        if geoip:
            raise LaunchError("geoip=True requires persona.proxy")
        return persona, None, None, None
    upstream = UpstreamProxy.parse(persona.proxy)
    gateway: ProxyGateway | None = None
    if use_gateway:
        gateway = ProxyGateway(upstream)
        await gateway.start()
        proxy_server = gateway.url
    elif upstream.has_credentials:
        raise LaunchError("Chrome cannot use proxy credentials directly; keep proxy_gateway=True")
    else:
        proxy_server = f"{upstream.scheme}://{upstream.host}:{upstream.port}"
    geo: GeoInfo | None = None
    if geoip:
        try:
            geo = await resolve_geo(upstream)
        except Exception:
            if gateway is not None:
                await gateway.stop()
            raise
        persona = replace(
            persona,
            timezone=geo.timezone or persona.timezone,
            languages=languages_for_country(geo.country),
        )
        log.info(
            "geoip: exit %s (%s) → timezone %s, languages %s",
            geo.ip, geo.country, persona.timezone, ",".join(persona.languages),
        )  # fmt: skip
    return persona, gateway, geo, proxy_server


def _prepare_display(
    persona: Persona,
    display: DisplayMode | VirtualDisplay,
    headless: bool,
    window_manager: bool,
) -> tuple[Persona, VirtualDisplay | None]:
    if isinstance(display, VirtualDisplay):
        return persona, display
    wants_virtual = not headless and (
        display == "virtual" or (display == "auto" and not os.environ.get("DISPLAY"))
    )
    if not wants_virtual:
        return persona, None
    if persona.gpu is None:
        # Stealthy by default: llvmpipe would otherwise announce "no real GPU".
        preset = persona.rng("gpu").choice(sorted(GPU_PRESETS))
        persona = replace(persona, gpu=GPU_PRESETS[preset])
    virtual = VirtualDisplay(
        persona.screen,
        gpu=persona.gpu,
        work_area=persona.work_area(),
        window_manager=window_manager,
        keyboard=persona.keyboard_layout(),
    )
    return persona, virtual


def connect(
    *,
    ws_url: str | None = None,
    port: int | None = None,
    host: str = "127.0.0.1",
    persona: Persona | None = None,
    human: str | HumanProfile = "default",
    input: InputMode = "auto",
    unsafe: bool = False,
    allow_remote: bool = False,
) -> LaunchHandle[Browser]:
    """Attach to a Chrome that is already running with a DevTools port.

    The browser is not relaunched and not closed on :meth:`Browser.close` —
    only the CDP connection is dropped. Identity flags (timezone, languages,
    GPU, proxy) are whatever that Chrome was started with; ``persona`` here
    only feeds the humanizer and the keyboard layout used for input.

    ``host`` / ``ws_url`` must be loopback unless ``allow_remote=True``.
    """
    profile = PRESETS[human] if isinstance(human, str) else human

    async def start() -> Browser:
        resolved, resolved_port = await _resolve_endpoint(
            ws_url, port, host, allow_remote=allow_remote
        )
        chosen = persona or Persona.from_host()
        connection = await Connection.open(resolved, policy=QuietCdpPolicy(unsafe=unsafe))
        try:
            browser = Browser(
                connection,
                AttachedChrome(port=resolved_port, ws_url=resolved, user_data_dir=Path(".")),
                persona=chosen,
                display=None,
                human=profile,
                input_mode=_resolve_input_mode(input, None),
                owns_process=False,
            )
            await browser._setup()
        except BaseException:
            await connection.close()
            raise
        log.info("attached to %s at %s", browser.version.get("product"), resolved)
        return browser

    return LaunchHandle(start)


async def _resolve_endpoint(
    ws_url: str | None, port: int | None, host: str, *, allow_remote: bool
) -> tuple[str, int]:
    if ws_url is None and port is None:
        raise LaunchError("connect() needs ws_url or port")
    if ws_url is None:
        assert port is not None
        if not allow_remote and host.lower() not in _LOOPBACK_HOSTS:
            raise LaunchError(
                f"refusing to connect to DevTools on {host}; pass allow_remote=True to override"
            )
        payload = await asyncio.to_thread(_http_get_json, f"http://{host}:{port}/json/version")
        found = payload.get("webSocketDebuggerUrl")
        if not found:
            raise LaunchError(f"http://{host}:{port}/json/version has no webSocketDebuggerUrl")
        return str(found), port
    if not allow_remote and not _endpoint_is_loopback(ws_url):
        raise LaunchError(f"refusing to connect to {ws_url}; pass allow_remote=True to override")
    parsed = urlsplit(ws_url)
    return ws_url, parsed.port or 0


def _endpoint_is_loopback(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() in _LOOPBACK_HOSTS


def _resolve_input_mode(requested: InputMode, virtual: VirtualDisplay | None) -> InputMode:
    if requested == "auto":
        return "xtest" if virtual is not None else "cdp"
    if requested == "xtest" and virtual is None and not os.environ.get("DISPLAY"):
        raise LaunchError("input='xtest' needs an X display (virtual or host)")
    return requested
