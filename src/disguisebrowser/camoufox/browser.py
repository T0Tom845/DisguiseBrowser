"""``CamoufoxBrowser`` and :func:`launch`: the Camoufox engine.

Same shape as :mod:`disguisebrowser.browser` — ``async with launch(...) as browser``
— with a persistent identity on top: a named :class:`~.profiles.Profile` pins the
device (fingerprint, fonts, voices, GPU parameters, audio seed) and keeps the
Firefox profile directory, so the same visitor comes back across launches.

Everything outside the browser is shared with the Chrome engine: the loopback
SOCKS5 proxy gateway (so Firefox sees an anonymous proxy and credentials never
reach its command line), the geo lookup through that tunnel, the humanized
typing/scrolling timing, warmup, scorecard and fingerprint capture.
"""

from __future__ import annotations

import contextlib
import logging
import os
import random
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any, Literal, cast

from camoufox.async_api import AsyncCamoufox
from playwright.async_api import BrowserContext
from playwright.async_api import Error as PwError
from playwright.async_api import Page as PwPage

from disguisebrowser.browser import LaunchHandle, _prepare_proxy
from disguisebrowser.camoufox.page import CamoufoxPage
from disguisebrowser.camoufox.profiles import (
    OSES,
    Kind,
    Profile,
    ProfileError,
    ProfileStore,
)
from disguisebrowser.errors import LaunchError
from disguisebrowser.input import PRESETS, HumanProfile
from disguisebrowser.persona import Persona
from disguisebrowser.proxy import GeoInfo, ProxyGateway
from disguisebrowser.warmup import WarmupVisit
from disguisebrowser.warmup import warmup as warmup_browser

log = logging.getLogger(__name__)

DisplayMode = Literal["auto", "host", "virtual"]

# Camoufox caps each cursor move at N seconds when ``humanize`` is a number.
_HUMANIZE = {"default": True, "careful": True, "fast": 0.8}


def _cleanup(steps: list[Callable[[], None]]) -> None:
    for step in reversed(steps):
        with contextlib.suppress(Exception):
            step()


class CamoufoxBrowser:
    """A running Camoufox with a persistent context; tabs share cookies and storage."""

    engine = "camoufox"
    input_mode = "playwright"
    display = None
    """Camoufox runs its own Xvfb (``display_mode == "virtual"``); there is no
    :class:`~disguisebrowser.display.VirtualDisplay` to expose."""

    def __init__(
        self,
        context: BrowserContext,
        manager: AsyncCamoufox,
        *,
        persona: Persona,
        human: HumanProfile,
        profile: Profile | None,
        gateway: ProxyGateway | None,
        geo: GeoInfo | None,
        main_world: bool,
        display_mode: str,
        cleanup: list[Callable[[], None]],
        user_data_dir: str,
    ) -> None:
        self.context = context
        self._manager = manager
        self.persona = persona
        self.human = human
        self.profile = profile
        self.gateway = gateway
        self.geo = geo
        self.main_world = main_world
        self.display_mode = display_mode
        self.user_data_dir = user_data_dir
        self.version: dict[str, Any] = {}
        self._cleanup = cleanup
        self._rng = persona.rng("input") if persona.seed is not None else random.Random()
        self._pages: dict[int, CamoufoxPage] = {}
        self._active_page: CamoufoxPage | None = None
        self._closed = False
        context.on("page", self._on_page)

    # ------------------------------------------------------------------ pages
    def _wrap(self, page: PwPage) -> CamoufoxPage:
        wrapped = self._pages.get(id(page))
        if wrapped is None:
            key = id(page)
            wrapped = CamoufoxPage(self, page, human=self.human, rng=self._rng)
            self._pages[key] = wrapped

            def forget(_closed: PwPage) -> None:
                self._pages.pop(key, None)

            page.on("close", forget)
        return wrapped

    def _on_page(self, page: PwPage) -> None:
        self._wrap(page)

    @property
    def pages(self) -> list[CamoufoxPage]:
        return [self._wrap(p) for p in self.context.pages if not p.is_closed()]

    async def new_page(self, url: str = "about:blank", **goto_kwargs: Any) -> CamoufoxPage:
        try:
            raw = await self.context.new_page()
        except PwError as exc:
            raise LaunchError(f"cannot open a tab: {exc}") from None
        page = self._wrap(raw)
        if url and url != "about:blank":
            await page.goto(url, **goto_kwargs)
        return page

    async def warmup(
        self,
        urls: list[str] | None = None,
        *,
        count: int = 3,
        dwell: tuple[float, float] = (1.0, 2.5),
    ) -> list[WarmupVisit]:
        """Visit ordinary sites so the profile is not an empty first launch."""
        return await warmup_browser(self, urls=urls, count=count, dwell=dwell)

    async def identity(self) -> dict[str, Any]:
        """What a page sees of this browser's device, for logs and agents."""
        page = self.pages[0] if self.pages else await self.new_page()
        values: dict[str, Any] = await page.evaluate(
            "({ userAgent: navigator.userAgent, platform: navigator.platform,"
            " cores: navigator.hardwareConcurrency, languages: [...navigator.languages],"
            " screen: [screen.width, screen.height],"
            " timezone: Intl.DateTimeFormat().resolvedOptions().timeZone })"
        )
        return values

    # ------------------------------------------------------------------ shutdown
    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._manager.__aexit__(None, None, None)
        except Exception as exc:  # the browser may already be gone
            log.debug("camoufox shutdown: %s", exc)
        if self.gateway is not None:
            with contextlib.suppress(Exception):
                await self.gateway.stop()
        _cleanup(self._cleanup)

    async def __aenter__(self) -> CamoufoxBrowser:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def __repr__(self) -> str:
        who = self.profile.name if self.profile else "ephemeral"
        return f"<CamoufoxBrowser {who} pages={len(self._pages)}>"


# ---------------------------------------------------------------------- options
def humanize_option(human: str | HumanProfile) -> bool | float:
    name = human if isinstance(human, str) else human.name
    return _HUMANIZE.get(name, True)


def _geo_config(geo: GeoInfo) -> tuple[dict[str, Any], dict[str, Any]]:
    """Camoufox ``config`` and Firefox prefs that align the device with the exit IP."""
    config: dict[str, Any] = {}
    prefs: dict[str, Any] = {}
    latitude, longitude = geo.raw.get("latitude"), geo.raw.get("longitude")
    if isinstance(latitude, int | float) and isinstance(longitude, int | float):
        config["geolocation:latitude"] = float(latitude)
        config["geolocation:longitude"] = float(longitude)
    # WebRTC would otherwise reveal the real address behind the proxy.
    if ":" in geo.ip:
        config["webrtc:ipv6"] = geo.ip
    elif geo.ip:
        config["webrtc:ipv4"] = geo.ip
        prefs["network.dns.disableIPv6"] = True
    return config, prefs


def build_options(
    *,
    profile: Profile | None,
    persona: Persona,
    user_data_dir: str,
    headless: bool | Literal["virtual"],
    humanize: bool | float,
    proxy_server: str | None,
    geo: GeoInfo | None,
    main_world: bool,
    os_name: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Keyword arguments for ``AsyncCamoufox``. Pure: no browser, network or disk."""
    options: dict[str, Any] = {
        "headless": headless,
        "humanize": humanize,
        "persistent_context": True,
        "user_data_dir": user_data_dir,
        "main_world_eval": main_world,
        # Pinned identities and our own locale/geo values are deliberate, not leaks.
        "i_know_what_im_doing": True,
        "locale": list(persona.languages),
    }
    if profile is not None:
        options["os"] = profile.os
        options["fingerprint" if profile.kind == "fpgen" else "fingerprint_preset"] = (
            profile.identity
        )
    elif os_name:
        options["os"] = os_name
    config: dict[str, Any] = {}
    prefs: dict[str, Any] = {}
    if persona.timezone:
        config["timezone"] = persona.timezone
    if proxy_server:
        options["proxy"] = {"server": proxy_server}
    if geo is not None:
        geo_config, prefs = _geo_config(geo)
        config.update(geo_config)
    if config:
        options["config"] = config
    if prefs:
        options["firefox_user_prefs"] = prefs
    if extra:
        options.update(extra)
    return options


def resolve_display(display: DisplayMode, headless: bool) -> tuple[bool | Literal["virtual"], str]:
    """``(Camoufox headless value, mode label)`` for the requested display."""
    if headless:
        return True, "headless"
    has_display = bool(os.environ.get("DISPLAY"))
    if display == "host":
        if not has_display:
            raise LaunchError(
                "display='host' requires $DISPLAY; use display='virtual' (Xvfb) on servers"
            )
        return False, "host"
    if display == "auto" and has_display:
        return False, "host"
    if shutil.which("Xvfb"):
        return "virtual", "virtual"
    if display == "virtual":
        raise LaunchError("display='virtual' needs Xvfb (apt install xvfb)")
    log.warning("no $DISPLAY and no Xvfb: falling back to headless Camoufox")
    return True, "headless"


@dataclass(frozen=True)
class _Request:
    """The arguments of :func:`launch`, resolved step by step in :meth:`start`."""

    profile: str | Profile | None
    store: ProfileStore | None
    create: bool
    os_name: str | None
    kind: Kind
    persona: Persona | None
    proxy: str | None
    geoip: bool | None
    display: DisplayMode
    headless: bool
    human: HumanProfile
    main_world: bool
    user_data_dir: str | None
    extra: dict[str, Any] | None

    # ------------------------------------------------------------------ steps
    def resolve_profile(self) -> Profile | None:
        if self.profile is None or isinstance(self.profile, Profile):
            return self.profile
        store = self.store or ProfileStore()
        try:
            if store.exists(self.profile):
                return store.load(self.profile)
            if not self.create:
                raise LaunchError(
                    f"no profile named {self.profile!r}; create it first "
                    f"(disguise profile create {self.profile}) or pass create=True"
                )
            return store.create(
                self.profile,
                os=self.os_name,
                kind=self.kind,
                proxy=self.proxy,
                geoip=bool(self.geoip),
                languages=self.persona.languages if self.persona else None,
                timezone=self.persona.timezone if self.persona else None,
            )
        except ProfileError as exc:
            raise LaunchError(str(exc)) from None

    def open_directory(self, resolved: Profile | None, cleanup: list[Callable[[], None]]) -> str:
        """Profile directory: stored (exclusive lock), explicit, or a temporary one."""
        if resolved is not None:
            store = self.store or ProfileStore()
            lock = store.lock(resolved.name)
            try:
                lock.__enter__()
            except ProfileError as exc:
                raise LaunchError(str(exc)) from None

            def release() -> None:
                lock.__exit__(None, None, None)

            cleanup.append(release)
            directory = str(store.user_data_dir(resolved.name))
        elif self.user_data_dir is not None:
            directory = self.user_data_dir
        else:
            directory = tempfile.mkdtemp(prefix="disguise-fox-")
            cleanup.append(partial(shutil.rmtree, directory, ignore_errors=True))
        Path(directory).mkdir(parents=True, exist_ok=True)
        return directory

    async def prepare_network(
        self, resolved: Profile | None
    ) -> tuple[Persona, ProxyGateway | None, GeoInfo | None, str | None]:
        base = self.persona or (resolved.persona() if resolved else Persona.from_host())
        if self.proxy:
            base = replace(base, proxy=self.proxy)
        want_geoip = self.geoip if self.geoip is not None else bool(resolved and resolved.geoip)
        final, gateway, geo, proxy_server = await _prepare_proxy(
            base, geoip=want_geoip, use_gateway=True
        )
        if resolved is not None:  # explicit profile settings beat geo defaults
            if resolved.languages:
                final = replace(final, languages=resolved.languages)
            if resolved.timezone:
                final = replace(final, timezone=resolved.timezone)
        return final, gateway, geo, proxy_server

    async def start(self) -> CamoufoxBrowser:
        cleanup: list[Callable[[], None]] = []
        gateway: ProxyGateway | None = None
        manager: AsyncCamoufox | None = None
        try:
            resolved = self.resolve_profile()
            directory = self.open_directory(resolved, cleanup)
            final, gateway, geo, proxy_server = await self.prepare_network(resolved)
            headless_mode, display_mode = resolve_display(self.display, self.headless)
            options = build_options(
                profile=resolved,
                persona=final,
                user_data_dir=directory,
                headless=headless_mode,
                humanize=humanize_option(self.human),
                proxy_server=proxy_server,
                geo=geo,
                main_world=self.main_world,
                os_name=self.os_name,
                extra=self.extra,
            )
            manager = AsyncCamoufox(**options)  # type: ignore[no-untyped-call]
            try:
                context = cast(BrowserContext, await manager.__aenter__())
            except Exception as exc:
                manager = None
                raise LaunchError(f"Camoufox failed to start: {exc}") from exc
            browser = CamoufoxBrowser(
                context,
                manager,
                persona=final,
                human=self.human,
                profile=resolved,
                gateway=gateway,
                geo=geo,
                main_world=self.main_world,
                display_mode=display_mode,
                cleanup=cleanup,
                user_data_dir=directory,
            )
            if not browser.pages:
                await browser.new_page()
            browser.version = await _version(browser)
        except BaseException:
            if manager is not None:
                with contextlib.suppress(Exception):
                    await manager.__aexit__(None, None, None)
            if gateway is not None:
                with contextlib.suppress(Exception):
                    await gateway.stop()
            _cleanup(cleanup)
            raise
        log.info(
            "%s ready (%s, profile=%s)",
            browser.version.get("product"),
            display_mode,
            resolved.name if resolved else "ephemeral",
        )
        return browser


def launch(
    *,
    profile: str | Profile | None = None,
    store: ProfileStore | None = None,
    create: bool = False,
    os: str | None = None,
    kind: Kind = "fpgen",
    persona: Persona | None = None,
    proxy: str | None = None,
    geoip: bool | None = None,
    display: DisplayMode = "auto",
    headless: bool = False,
    human: str | HumanProfile = "default",
    main_world: bool = False,
    user_data_dir: str | os.PathLike[str] | None = None,
    camoufox_options: dict[str, Any] | None = None,
) -> LaunchHandle[CamoufoxBrowser]:
    """Start Camoufox.

    ``profile``: name of a stored profile (or a :class:`Profile`). The pinned
    device and the Firefox profile directory are reused; ``proxy``/``geoip``
    override the stored network settings for this run only. With
    ``create=True`` a missing name is created (``os``/``kind`` choose the device).
    Without a profile the run is ephemeral: a fresh random device and a
    temporary profile directory, removed on close.

    ``display``: ``"host"`` uses ``$DISPLAY``, ``"virtual"`` lets Camoufox start
    Xvfb, ``"auto"`` picks ``host`` when a display exists, else ``virtual``, else
    headless. ``headless=True`` forces headless (weaker stealth).

    ``main_world``: allow ``page.evaluate(..., world="main")`` (Camoufox's opt-in
    ``mw:`` evaluation). ``camoufox_options``: extra keyword arguments passed
    straight to ``AsyncCamoufox``.

    Usage::

        async with launch(profile="shop-1", create=True, os="windows") as browser:
            page = await browser.new_page("https://example.com")
    """
    if os is not None and os not in OSES:
        raise LaunchError(f"os must be one of {', '.join(OSES)}, got {os!r}")
    if user_data_dir is not None and profile is not None:
        raise LaunchError("pass either profile or user_data_dir, not both")
    request = _Request(
        profile=profile,
        store=store,
        create=create,
        os_name=os,
        kind=kind,
        persona=persona,
        proxy=proxy,
        geoip=geoip,
        display=display,
        headless=headless,
        human=PRESETS[human] if isinstance(human, str) else human,
        main_world=main_world,
        user_data_dir=str(user_data_dir) if user_data_dir is not None else None,
        extra=camoufox_options,
    )
    return LaunchHandle(request.start)


async def _version(browser: CamoufoxBrowser) -> dict[str, Any]:
    try:
        from camoufox.pkgman import installed_verstr  # noqa: PLC0415

        build = str(installed_verstr())
    except Exception:
        build = "unknown"
    page = browser.pages[0]
    user_agent = str(await page.evaluate("navigator.userAgent"))
    return {"product": f"Camoufox/{build}", "userAgent": user_agent, "camoufox": build}
