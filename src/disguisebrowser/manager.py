"""Local profile window: every stored identity, with a switch.

``disguise manager`` serves a single page on loopback and opens it in the
ordinary browser. Each switch starts or stops the Camoufox window for that
profile. The live view shows that page inside the manager and forwards
clicks, scrolling and typing into it, so the profile can be used without a
separate VNC session. The page never receives proxy passwords: list rows
come from :meth:`~disguisebrowser.camoufox.profiles.Profile.summary`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import signal
import threading
import webbrowser
from collections.abc import Awaitable, Callable, Coroutine
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any, Literal, TypedDict, cast
from urllib.parse import urlsplit

from disguisebrowser.camoufox.profiles import (
    OSES,
    Kind,
    ProfileError,
    ProfileStore,
    coerce_screen,
    parse_start_url,
    validate_name,
)
from disguisebrowser.errors import DisguiseError

log = logging.getLogger(__name__)

DisplayMode = Literal["auto", "host", "virtual"]
LaunchFn = Callable[[str, ProfileStore, DisplayMode], Awaitable[Any]]
_MAX_BODY = 64 * 1024
_PHASES = frozenset({"starting", "stopping"})
_FRAME_GAP = 0.08
_JPEG_QUALITY = 50
_MODIFIERS = frozenset({"Control", "Alt", "Meta", "Shift"})
_NAMED_KEYS = frozenset(
    {
        "Enter",
        "Backspace",
        "Tab",
        "Escape",
        "Delete",
        "Home",
        "End",
        "PageUp",
        "PageDown",
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        "Insert",
        "Space",
        *(f"F{index}" for index in range(1, 13)),
    }
)
_NAVIGATION = {"back": "go_back", "forward": "go_forward", "reload": "reload"}


class _Frame(TypedDict):
    jpeg: bytes
    width: int
    height: int
    url: str


class _LiveView:
    """Last picture of one profile, and the lock that keeps its input in order."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.at = 0.0
        self.jpeg = b""
        self.width = 0
        self.height = 0
        self.url = ""


class DeskError(DisguiseError):
    """A manager request that should be shown to the person at the window."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _display_mode(value: str) -> DisplayMode:
    if value not in ("auto", "host", "virtual"):
        raise DisguiseError("display must be auto, host or virtual")
    return cast(DisplayMode, value)


def _loopback(host: str) -> str:
    if host == "localhost":
        return "127.0.0.1"
    if host not in {"127.0.0.1", "::1"}:
        raise DisguiseError(
            "the manager only listens on 127.0.0.1; it can read profile names and set proxies"
        )
    return host


def _failure_text(exc: BaseException) -> str:
    text = " ".join(str(exc).split()).strip() or type(exc).__name__
    return text[:500]


_PROFILE_ERRORS: tuple[tuple[str, str, int], ...] = (
    ("already exists", "Профиль с таким именем уже есть", 409),
    ("no profile named", "Нет такого профиля", 404),
    ("in use", "Этот профиль сейчас занят", 409),
    ("needs a proxy", "Подстройка под прокси работает только вместе с прокси", 400),
    (
        "invalid profile name",
        "Имя: латиница, цифры, точка, _ или -, с буквы или цифры, до 64 символов",
        400,
    ),
    ("os must be", "Система: windows, macos или linux", 400),
    ("kind must be", "Устройство: fpgen или preset", 400),
    ("cannot change", "Устройство уже закреплено, его не меняют", 400),
    ("screen must be", "Разрешение: например 1920x1080", 400),
    ("not a plausible desktop", "Разрешение должно быть от 800×600 до 7680×4320", 400),
    ("start_url is too long", "Адрес слишком длинный", 400),
    ("start_url must be", "Адрес: только http:// или https://", 400),
)


def profile_error(exc: ProfileError) -> DeskError:
    """Russian text for the window; the store itself speaks English, like the CLI."""
    text = str(exc)
    for needle, message, status in _PROFILE_ERRORS:
        if needle in text:
            return DeskError(message, status)
    return DeskError(text, 400)


def _check_name(name: str) -> str:
    try:
        return validate_name(name)
    except ProfileError as exc:
        raise profile_error(exc) from None


def _optional_text(data: dict[str, Any], key: str) -> str | None:
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if not isinstance(value, str):
        raise DeskError(f"{key}: жду строку")
    value = value.strip()
    return value or None


def _languages(value: Any) -> tuple[str, ...] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        parts = tuple(part.strip() for part in value.split(",") if part.strip())
        return parts or None
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        parts = tuple(part.strip() for part in value if part.strip())
        return parts or None
    raise DeskError("Языки: строка через запятую или список строк")


def _parse_create(data: dict[str, Any]) -> dict[str, Any]:
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise DeskError("Нужно имя профиля")
    os_name = _optional_text(data, "os")
    if os_name is not None and os_name not in OSES:
        raise DeskError("Система: windows, macos или linux")
    kind = data.get("kind", "fpgen")
    if kind not in ("fpgen", "preset"):
        raise DeskError("Устройство: fpgen или preset")
    seed = data.get("seed")
    if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool)):
        raise DeskError("seed: целое число")
    geoip = data.get("geoip", False)
    if not isinstance(geoip, bool):
        raise DeskError("geoip: true или false")
    notes = data.get("notes", "")
    if notes is None:
        notes = ""
    if not isinstance(notes, str):
        raise DeskError("Заметка: жду строку")
    screen = data.get("screen")
    start_url = data.get("start_url")
    return {
        "name": name.strip(),
        "os": os_name,
        "kind": cast(Kind, kind),
        "seed": seed,
        "proxy": _optional_text(data, "proxy"),
        "geoip": geoip,
        "languages": _languages(data.get("languages")),
        "timezone": _optional_text(data, "timezone"),
        "notes": notes,
        "screen": None if screen in (None, "") else _profile_value(coerce_screen, screen),
        "start_url": None
        if start_url in (None, "")
        else _profile_value(parse_start_url, start_url),
    }


def _proxy_change(proxy: Any) -> str | None:
    if proxy is None or proxy == "":
        return None
    if not isinstance(proxy, str):
        raise DeskError("Прокси: жду строку")
    return proxy.strip() or None


def _clearable_text(value: Any, label: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise DeskError(f"{label}: жду строку")
    return value.strip() or None


def _profile_value(parser: Callable[[Any], Any], value: Any) -> Any:
    try:
        return parser(value)
    except ProfileError as exc:
        raise profile_error(exc) from None


def _notes(value: Any) -> str:
    if not isinstance(value, str):
        raise DeskError("Заметка: жду строку")
    return value


def _coord(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DeskError(f"{label}: число")
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > 16_000:
        raise DeskError(f"{label}: вне экрана")
    return number


def _span(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DeskError("Скролл: число")
    number = float(value)
    if not math.isfinite(number) or abs(number) > 4_000:
        raise DeskError("Слишком большой скролл")
    return number


def _key_name(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 40:
        raise DeskError("Неизвестная клавиша")
    parts = value.split("+")
    if not parts or len(parts) > 4:
        raise DeskError("Неизвестная клавиша")
    *mods, key = parts
    if any(mod not in _MODIFIERS for mod in mods) or len(mods) != len(set(mods)):
        raise DeskError("Неизвестная клавиша")
    if key == "Space":
        key = " "
    printable = len(key) == 1 and key.isprintable() and key != "+"
    if key not in _NAMED_KEYS and not printable:
        raise DeskError("Неизвестная клавиша")
    return "+".join((*mods, key))


def _typed(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise DeskError("Пустой ввод")
    if len(value) > 2000:
        raise DeskError("Слишком длинный ввод")
    if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        raise DeskError("В тексте есть управляющие символы")
    return value


def _parse_input(data: dict[str, Any]) -> dict[str, Any]:
    kind = data.get("kind")
    if kind in _NAVIGATION:
        return {"kind": kind}
    if kind in {"move", "down", "up", "wheel"}:
        button = data.get("button", "left")
        if button not in {"left", "middle", "right"}:
            raise DeskError("Кнопка мыши: left, middle или right")
        return {
            "kind": kind,
            "x": _coord(data.get("x"), "x"),
            "y": _coord(data.get("y"), "y"),
            "button": button,
            "dx": _span(data.get("dx")),
            "dy": _span(data.get("dy")),
        }
    if kind == "press":
        return {"kind": "press", "key": _key_name(data.get("key"))}
    if kind == "text":
        return {"kind": "text", "text": _typed(data.get("text"))}
    raise DeskError("Не понял действие")


def _shot(view: _LiveView) -> _Frame:
    return {"jpeg": view.jpeg, "width": view.width, "height": view.height, "url": view.url}


def _fit(x: float, y: float, width: int, height: int) -> tuple[float, float]:
    """Keep a click inside the last picture. ``0`` means the size is not known yet."""
    if width > 1:
        x = min(x, width - 1)
    if height > 1:
        y = min(y, height - 1)
    return x, y


def _page_url(page: Any) -> str:
    url = getattr(page, "url", "")
    if not isinstance(url, str) or len(url) > 2048 or any(char in url for char in "\r\n\x00"):
        return ""
    try:
        url.encode("latin-1")
    except UnicodeEncodeError:
        return ""
    return url


async def _viewport(page: Any) -> tuple[int, int]:
    view = getattr(page, "viewport", None)
    if view is None:
        return 0, 0
    try:
        size = await view()
    except Exception:
        return 0, 0
    if (
        isinstance(size, tuple)
        and len(size) == 2
        and all(isinstance(part, int) and not isinstance(part, bool) and part > 0 for part in size)
    ):
        return size
    return 0, 0


async def _capture(page: Any) -> tuple[bytes, int, int, str]:
    raw = getattr(page, "playwright_page", None)
    try:
        if raw is not None:
            data = await raw.screenshot(type="jpeg", quality=_JPEG_QUALITY, timeout=5_000)
        else:
            shot = getattr(page, "screenshot", None)
            if shot is None:
                raise DeskError("В этом окне нет картинки", 502)
            data = await shot(format="jpeg", quality=_JPEG_QUALITY)
    except DeskError:
        raise
    except Exception as exc:
        raise DeskError(_failure_text(exc), 502) from None
    if isinstance(data, bytearray):
        data = bytes(data)
    if not isinstance(data, bytes) or not data:
        raise DeskError("Не получилось снять окно", 502)
    width, height = await _viewport(page)
    return data, width, height, _page_url(page)


def _pointer(page: Any) -> Any:
    raw = getattr(page, "playwright_page", None)
    mouse = getattr(raw, "mouse", None) if raw is not None else None
    if mouse is None:
        mouse = getattr(page, "mouse", None)
    if mouse is None:
        raise DeskError("В этом окне нельзя кликать", 502)
    return mouse


def _keys(page: Any) -> Any:
    raw = getattr(page, "playwright_page", None)
    keyboard = getattr(raw, "keyboard", None) if raw is not None else None
    if keyboard is None:
        keyboard = getattr(page, "keyboard", None)
    if keyboard is None:
        raise DeskError("В этом окне нельзя печатать", 502)
    return keyboard


async def _front_tab(browser: Any) -> Any:
    pages = [page for page in list(getattr(browser, "pages", []) or []) if page is not None]
    active = getattr(browser, "_active_page", None)
    if active is not None and active in pages:
        return active
    if pages:
        return pages[0]
    factory = getattr(browser, "new_page", None)
    if factory is None:
        raise DeskError("В этом окне нет вкладки", 409)
    return await factory()


async def _drive(page: Any, event: dict[str, Any], view: _LiveView) -> None:
    kind = event["kind"]
    try:
        if kind in _NAVIGATION:
            method = getattr(page, _NAVIGATION[kind], None)
            if method is None:
                raise DeskError("Это окно так не умеет", 409)
            await method(wait_until="commit")
            return
        if kind in {"move", "down", "up", "wheel"}:
            mouse = _pointer(page)
            x, y = _fit(event["x"], event["y"], view.width, view.height)
            await mouse.move(x, y)
            if kind == "down":
                await mouse.down(button=event["button"])
            elif kind == "up":
                await mouse.up(button=event["button"])
            elif kind == "wheel":
                await mouse.wheel(event["dx"], event["dy"])
            return
        keyboard = _keys(page)
        if kind == "press":
            await keyboard.press(event["key"])
            return
        await keyboard.insert_text(event["text"])
    except DeskError:
        raise
    except Exception as exc:
        raise DeskError(_failure_text(exc), 502) from None


def _parse_update(data: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if "proxy" in data:
        changes["proxy"] = _proxy_change(data["proxy"])
    if "geoip" in data:
        if not isinstance(data["geoip"], bool):
            raise DeskError("geoip: true или false")
        changes["geoip"] = data["geoip"]
    if "languages" in data:
        changes["languages"] = _languages(data["languages"])
    if "timezone" in data:
        changes["timezone"] = _clearable_text(data["timezone"], "Часовой пояс")
    if "notes" in data:
        changes["notes"] = _notes(data["notes"])
    if "screen" in data and data["screen"] not in (None, ""):
        changes["screen"] = _profile_value(coerce_screen, data["screen"])
    if "start_url" in data:
        changes["start_url"] = _profile_value(parse_start_url, data["start_url"])
    if not changes:
        raise DeskError("Нечего сохранять")
    return changes


async def launch_stored_profile(name: str, store: ProfileStore, display: DisplayMode) -> Any:
    """Start the Camoufox window for a stored profile. Imported lazily."""
    from disguisebrowser.camoufox import launch as camoufox_launch  # noqa: PLC0415

    # A person is moving the pointer. Camoufox's cursor paths would lag behind them.
    return await camoufox_launch(
        profile=name, store=store, display=display, camoufox_options={"humanize": False}
    )


async def _present(browser: Any, url: str | None) -> None:
    """Show ``browser`` and, when ``url`` is set, open that address in it."""
    pages = list(getattr(browser, "pages", []) or [])
    page = pages[0] if pages else None
    if page is None:
        factory = getattr(browser, "new_page", None)
        if factory is None:
            if url:
                raise DeskError("Не получилось открыть адрес в этом окне")
            return
        page = await factory(url) if url else await factory()
    elif url:
        goto = getattr(page, "goto", None)
        if goto is None:
            raise DeskError("Не получилось открыть адрес в этом окне")
        await goto(url, wait_until="commit")
    front = getattr(page, "bring_to_front", None)
    if front is not None:
        await front()


async def _close_quietly(browser: Any) -> None:
    close = getattr(browser, "close", None)
    if close is None:
        return
    with contextlib.suppress(Exception):
        await close()


class ProfileDesk:
    """Start and stop stored profiles on one event loop.

    The loop is the caller's. Playwright objects created by ``launch`` stay on
    it; the HTTP thread only submits coroutines.
    """

    def __init__(
        self,
        store: ProfileStore | None = None,
        *,
        display: DisplayMode = "auto",
        launch: LaunchFn | None = None,
    ) -> None:
        self.store = store or ProfileStore()
        self.display = display
        self._launch = launch or launch_stored_profile
        self._running: dict[str, Any] = {}
        self._phase: dict[str, str] = {}
        self._errors: dict[str, str] = {}
        self._views: dict[str, _LiveView] = {}
        self._gates: dict[str, asyncio.Lock] = {}
        self._jobs: set[asyncio.Task[None]] = set()
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise DeskError("Менеджер уже закрывается", 409)

    def _gate(self, name: str) -> asyncio.Lock:
        gate = self._gates.get(name)
        if gate is None:
            gate = asyncio.Lock()
            self._gates[name] = gate
        return gate

    def _forget(self, name: str, browser: Any) -> None:
        if self._running.get(name) is browser:
            del self._running[name]
        self._views.pop(name, None)

    def _listen(self, name: str, browser: Any) -> None:
        """When the person closes the window, release the lock and the proxy."""
        context = getattr(browser, "context", None)
        subscribe = getattr(context, "on", None)
        if subscribe is None:
            return
        loop = asyncio.get_running_loop()

        def stopped(*_args: object) -> None:
            loop.call_soon_threadsafe(self._on_window_closed, name, browser)

        subscribe("close", stopped)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._jobs.add(task)
        task.add_done_callback(self._job_done)

    def _job_done(self, task: asyncio.Task[None]) -> None:
        self._jobs.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log.debug("profile close: %s", error)

    def _on_window_closed(self, name: str, browser: Any) -> None:
        if self._running.get(name) is not browser:
            return
        if self._phase.get(name) == "stopping":
            return
        self._spawn(self._finish_close(name, browser))

    def _schedule_finish(self, name: str, browser: Any) -> None:
        if self._phase.get(name) == "stopping":
            return
        self._phase[name] = "stopping"
        self._spawn(self._finish_close(name, browser))

    def _status(self, name: str) -> str:
        phase = self._phase.get(name)
        if phase:
            return phase
        browser = self._running.get(name)
        if browser is not None:
            if bool(getattr(browser, "closed", False)):
                self._schedule_finish(name, browser)
                return "stopping"
            return "running"
        try:
            if self.store.in_use(name):
                return "busy"
        except ProfileError:
            return "stopped"
        return "stopped"

    def _browser_is_open(self, name: str) -> bool:
        if name in self._running or self._phase.get(name) in _PHASES:
            return True
        try:
            return self.store.in_use(name)
        except ProfileError:
            return False

    async def _show(self, name: str, browser: Any, url: str | None) -> None:
        try:
            await _present(browser, url)
        except Exception as exc:
            self._errors[name] = _failure_text(exc)
            return
        self._errors.pop(name, None)

    def rows(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for profile in self.store.list():
            row = profile.summary()
            row["notes"] = profile.notes
            row["status"] = self._status(profile.name)
            row["error"] = self._errors.get(profile.name)
            found.append(row)
        return found

    async def snapshot(self) -> dict[str, Any]:
        profiles = self.rows()
        return {
            "profiles": profiles,
            "running": sum(1 for row in profiles if row["status"] in {"running", "starting"}),
            "display": self.display,
            "host_display": bool(os.environ.get("DISPLAY")),
            "root": str(self.store.root),
        }

    def _arm(self, name: str, browser: Any) -> None:
        self._running[name] = browser
        self._phase.pop(name, None)
        self._errors.pop(name, None)
        self._listen(name, browser)

    async def start(self, name: str) -> dict[str, Any]:
        self._ensure_open()
        _check_name(name)
        async with self._gate(name):
            await self._start_held(name)
        return await self.snapshot()

    async def _start_held(self, name: str) -> None:
        if not self.store.exists(name):
            raise DeskError("Нет такого профиля", 404)
        current = self._running.get(name)
        if current is not None:
            if not bool(getattr(current, "closed", False)):
                return
            await self._close_held(name, current)
        if self.store.in_use(name):
            raise DeskError("Этот профиль уже открыт в другом окне. Закрой его там.", 409)
        self._phase[name] = "starting"
        self._errors.pop(name, None)
        try:
            browser = await self._launch(name, self.store, self.display)
            if self._closed:
                await _close_quietly(browser)
                raise DeskError("Менеджер уже закрывается", 409)
            self._arm(name, browser)
            log.info("profile %s started on %s", name, self.display)
            homepage = self.store.load(name).start_url
            if homepage:
                await self._show(name, browser, homepage)
        except DeskError:
            raise
        except Exception as exc:
            self._errors[name] = _failure_text(exc)
            raise DeskError(self._errors[name], 502) from None
        finally:
            if name not in self._running:
                self._phase.pop(name, None)

    async def stop(self, name: str) -> dict[str, Any]:
        _check_name(name)
        browser = self._running.get(name)
        if browser is None:
            if not self.store.exists(name):
                raise DeskError("Нет такого профиля", 404)
            if self.store.in_use(name):
                raise DeskError("Этот профиль уже открыт в другом окне. Закрой его там.", 409)
            return await self.snapshot()
        await self._finish_close(name, browser)
        return await self.snapshot()

    async def _finish_close(self, name: str, browser: Any) -> None:
        async with self._gate(name):
            await self._close_held(name, browser)

    async def _close_held(self, name: str, browser: Any) -> None:
        current = self._running.get(name)
        if current is None or current is not browser:
            return
        self._phase[name] = "stopping"
        try:
            await current.close()
            log.info("profile %s stopped", name)
        except Exception as exc:
            self._errors[name] = _failure_text(exc)
            raise DeskError(self._errors[name], 502) from None
        finally:
            self._forget(name, browser)
            if self._phase.get(name) == "stopping":
                self._phase.pop(name, None)

    async def stop_all(self) -> dict[str, Any]:
        for name in list(self._running):
            with contextlib.suppress(DeskError):
                await self.stop(name)
        return await self.snapshot()

    async def create(self, data: dict[str, Any]) -> dict[str, Any]:
        self._ensure_open()
        fields = _parse_create(data)
        try:
            await asyncio.to_thread(partial(self.store.create, **fields))
        except ProfileError as exc:
            raise profile_error(exc) from None
        return await self.snapshot()

    async def open_profile(self, name: str, data: dict[str, Any]) -> dict[str, Any]:
        """Start the window if needed, then open ``url`` and bring it forward."""
        self._ensure_open()
        _check_name(name)
        if not self.store.exists(name):
            raise DeskError("Нет такого профиля", 404)
        if "url" in data:
            url = _profile_value(parse_start_url, data.get("url"))
            try:
                await asyncio.to_thread(partial(self.store.update, name, start_url=url))
            except ProfileError as exc:
                raise profile_error(exc) from None
        async with self._gate(name):
            current = self._running.get(name)
            if current is not None and not bool(getattr(current, "closed", False)):
                await self._show(name, current, self.store.load(name).start_url)
            else:
                await self._start_held(name)
        return await self.snapshot()

    def _require_browser(self, name: str) -> Any:
        self._ensure_open()
        _check_name(name)
        if not self.store.exists(name):
            raise DeskError("Нет такого профиля", 404)
        browser = self._running.get(name)
        if browser is None or bool(getattr(browser, "closed", False)):
            raise DeskError("Сначала включи профиль", 409)
        return browser

    async def frame(self, name: str) -> _Frame:
        """JPEG of the profile's current page, plus its CSS size and URL."""
        browser = self._require_browser(name)
        view = self._views.setdefault(name, _LiveView())
        now = asyncio.get_running_loop().time()
        if view.jpeg and now - view.at < _FRAME_GAP:
            return _shot(view)
        async with view.lock:
            now = asyncio.get_running_loop().time()
            if view.jpeg and now - view.at < _FRAME_GAP:
                return _shot(view)
            page = await _front_tab(browser)
            jpeg, width, height, url = await _capture(page)
            view.jpeg = jpeg
            view.width = width
            view.height = height
            view.url = url
            view.at = asyncio.get_running_loop().time()
            return _shot(view)

    async def interact(self, name: str, data: dict[str, Any]) -> dict[str, Any]:
        """Forward one pointer, key or history action into the running page."""
        event = _parse_input(data)
        browser = self._require_browser(name)
        view = self._views.setdefault(name, _LiveView())
        async with view.lock:
            await _drive(await _front_tab(browser), event, view)
        return {"ok": True}

    async def update(self, name: str, data: dict[str, Any]) -> dict[str, Any]:
        self._ensure_open()
        _check_name(name)
        changes = _parse_update(data)
        if not self.store.exists(name):
            raise DeskError("Нет такого профиля", 404)
        if "screen" in changes and self._browser_is_open(name):
            current = await asyncio.to_thread(self.store.load, name)
            width, height = changes["screen"]
            have = current.identity.get("screen") if isinstance(current.identity, dict) else {}
            have = have if isinstance(have, dict) else {}
            if have.get("width") != width or have.get("height") != height:
                raise DeskError(
                    "Сначала выключи профиль. Разрешение применяется при запуске.", 409
                )
            changes.pop("screen")
        if not changes:
            return await self.snapshot()
        try:
            await asyncio.to_thread(partial(self.store.update, name, **changes))
        except ProfileError as exc:
            raise profile_error(exc) from None
        self._errors.pop(name, None)
        return await self.snapshot()

    async def delete(self, name: str) -> dict[str, Any]:
        _check_name(name)
        async with self._gate(name):
            if name in self._running or self._phase.get(name) in _PHASES:
                raise DeskError("Сначала выключи профиль", 409)
            if self.store.in_use(name):
                raise DeskError("Профиль открыт в другом окне. Сначала закрой его.", 409)
            try:
                await asyncio.to_thread(self.store.delete, name)
            except ProfileError as exc:
                raise profile_error(exc) from None
            self._errors.pop(name, None)
            self._phase.pop(name, None)
        return await self.snapshot()

    async def shutdown(self) -> None:
        """Close every window this desk opened. In-flight starts close themselves."""
        self._closed = True
        for name in list(self._gates):
            async with self._gate(name):
                browser = self._running.get(name)
                if browser is None:
                    continue
                with contextlib.suppress(DeskError, Exception):
                    await self._close_held(name, browser)
        if self._jobs:
            await asyncio.gather(*list(self._jobs), return_exceptions=True)


class _Bridge:
    """Run desk coroutines on the asyncio loop from the HTTP threads."""

    def __init__(self, desk: ProfileDesk, loop: asyncio.AbstractEventLoop) -> None:
        self.desk = desk
        self.loop = loop

    def call[T](self, coro: Coroutine[Any, Any, T], timeout: float = 180.0) -> T:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except TimeoutError as exc:
            raise DeskError(
                "Браузер ещё запускается. Список обновится сам через несколько секунд.", 504
            ) from exc


def _page_bytes() -> bytes:
    text = resources.files("disguisebrowser").joinpath("manager.html").read_text(encoding="utf-8")
    return text.encode("utf-8")


_PAGE = _page_bytes()
_PROFILE_PATH = re.compile(
    r"^/api/profiles/([A-Za-z0-9][A-Za-z0-9._-]{0,63})(?:/(start|stop|open|frame|input))?$"
)


def _route_path(raw: str) -> str:
    path = urlsplit(raw).path
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "disguise-manager"
    sys_version = ""

    def __init__(self, *args: Any, bridge: _Bridge, **kwargs: Any) -> None:
        self._bridge = bridge
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: object) -> None:
        log.debug("manager: " + format, *args)

    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(HTTPStatus(status), body, "application/json; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        header = self.headers.get("Content-Length")
        if not header:
            return {}
        try:
            length = int(header)
        except ValueError as exc:
            raise DeskError("Не получилось прочитать JSON") from exc
        if length < 0 or length > _MAX_BODY:
            raise DeskError("Слишком большой запрос")
        raw = self.rfile.read(length)
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DeskError("Не получилось прочитать JSON") from exc
        if not isinstance(data, dict):
            raise DeskError("Жду JSON-объект")
        return data

    def _discard_body(self) -> None:
        """Keep-alive connections break if a body is left unread."""
        header = self.headers.get("Content-Length")
        if not header:
            return
        try:
            length = int(header)
        except ValueError as exc:
            raise DeskError("Не получилось прочитать JSON") from exc
        if length < 0 or length > _MAX_BODY:
            raise DeskError("Слишком большой запрос")
        if length:
            self.rfile.read(length)

    def _collection(self, method: str) -> tuple[int, dict[str, Any]]:
        desk = self._bridge.desk
        if method == "GET":
            return 200, self._bridge.call(desk.snapshot())
        if method == "POST":
            return 201, self._bridge.call(desk.create(self._read_json()))
        raise DeskError("Метод не поддерживается", 405)

    def _stop_everything(self, method: str) -> tuple[int, dict[str, Any]]:
        if method != "POST":
            raise DeskError("Метод не поддерживается", 405)
        self._discard_body()
        return 200, self._bridge.call(self._bridge.desk.stop_all())

    def _one_profile(
        self, method: str, name: str, action: str | None
    ) -> tuple[int, dict[str, Any]]:
        desk = self._bridge.desk
        if action == "start" and method == "POST":
            self._discard_body()
            return 200, self._bridge.call(desk.start(name))
        if action == "stop" and method == "POST":
            self._discard_body()
            return 200, self._bridge.call(desk.stop(name))
        if action == "open" and method == "POST":
            return 200, self._bridge.call(desk.open_profile(name, self._read_json()))
        if action == "input" and method == "POST":
            return 200, self._bridge.call(desk.interact(name, self._read_json()), timeout=15)
        if action is None and method == "PATCH":
            return 200, self._bridge.call(desk.update(name, self._read_json()))
        if action is None and method == "DELETE":
            self._discard_body()
            return 200, self._bridge.call(desk.delete(name))
        raise DeskError("Метод не поддерживается", 405)

    def _dispatch(self, method: str) -> tuple[int, dict[str, Any]]:
        path = _route_path(self.path)
        if path == "/api/profiles":
            return self._collection(method)
        if path == "/api/stop-all":
            return self._stop_everything(method)
        match = _PROFILE_PATH.match(path)
        if match is None:
            if path.startswith("/api/"):
                raise DeskError("Нет такой страницы", 404)
            raise DeskError("Метод не поддерживается", 405)
        return self._one_profile(method, match.group(1), match.group(2))

    def _handle(self, method: str) -> None:
        try:
            status, payload = self._dispatch(method)
        except DeskError as exc:
            self._send_json(exc.status, {"error": str(exc)})
            return
        except ProfileError as exc:
            mapped = profile_error(exc)
            self._send_json(mapped.status, {"error": str(mapped)})
            return
        except Exception as exc:
            log.exception("manager request failed")
            self._send_json(500, {"error": _failure_text(exc)})
            return
        self._send_json(status, payload)

    def _send_frame(self, name: str) -> None:
        try:
            shot = self._bridge.call(self._bridge.desk.frame(name), timeout=12)
        except DeskError as exc:
            self._send_json(exc.status, {"error": str(exc)})
            return
        except ProfileError as exc:
            mapped = profile_error(exc)
            self._send_json(mapped.status, {"error": str(mapped)})
            return
        except Exception as exc:
            log.exception("manager frame failed")
            self._send_json(500, {"error": _failure_text(exc)})
            return
        jpeg = shot.get("jpeg")
        if not isinstance(jpeg, bytes) or not jpeg:
            self._send_json(502, {"error": "Не получилось снять окно"})
            return
        extra = {
            "X-View-Width": str(shot.get("width") or 0),
            "X-View-Height": str(shot.get("height") or 0),
        }
        url = shot.get("url")
        if isinstance(url, str) and url:
            extra["X-View-URL"] = url
        self._send(HTTPStatus.OK, jpeg, "image/jpeg", extra)

    def do_GET(self) -> None:
        path = _route_path(self.path)
        if path in {"/", "/index.html"}:
            self._send(HTTPStatus.OK, _PAGE, "text/html; charset=utf-8")
            return
        match = _PROFILE_PATH.match(path)
        if match is not None and match.group(2) == "frame":
            self._send_frame(match.group(1))
            return
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_DELETE(self) -> None:
        self._handle("DELETE")


class ManagerServer:
    """Loopback HTTP server for the profile window."""

    def __init__(self, desk: ProfileDesk, host: str = "127.0.0.1", port: int = 8787) -> None:
        self.desk = desk
        self.host = host
        self._port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        host = _loopback(self.host)
        self.host = host
        handler = partial(_Handler, bridge=_Bridge(self.desk, loop))
        try:
            httpd = ThreadingHTTPServer((host, self._port), handler)
        except OSError as exc:
            raise DisguiseError(
                f"cannot listen on {host}:{self._port} ({exc.strerror or exc})"
            ) from exc
        httpd.daemon_threads = True
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="disguise-manager", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        httpd = self._httpd
        if httpd is None:
            return
        self._httpd = None
        httpd.shutdown()
        httpd.server_close()

    @property
    def port(self) -> int:
        if self._httpd is None:
            raise RuntimeError("manager is not running")
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}/"


def _install_stop(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, stop.set)


async def serve_manager(
    *,
    store: ProfileStore | None = None,
    host: str = "127.0.0.1",
    port: int = 8787,
    display: str = "auto",
    open_browser: bool = True,
) -> int:
    """Serve the profile window until Ctrl+C, then close browsers it opened.

    Shutdown runs on the asyncio loop *before* the HTTP server is joined.
    Joining first deadlocks: a request thread is waiting for this loop, and
    ``server.shutdown`` waits for that thread.
    """
    desk = ProfileDesk(store, display=_display_mode(display))
    server = ManagerServer(desk, host, port)
    server.start(asyncio.get_running_loop())
    stop = asyncio.Event()
    _install_stop(stop)
    url = server.url
    print(f"profile manager: {url}", flush=True)
    print("Ctrl+C closes this manager and the browsers it opened", flush=True)
    if open_browser and not webbrowser.open(url, new=1):
        print("open that URL in a browser", flush=True)
    try:
        await stop.wait()
    finally:
        await desk.shutdown()
        await asyncio.to_thread(server.stop)
    return 130
