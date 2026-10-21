"""Profile window: list, start, stop. No real browser."""

from __future__ import annotations

import asyncio
import http.client
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from disguisebrowser import cli
from disguisebrowser.camoufox import profiles
from disguisebrowser.camoufox.profiles import ProfileStore
from disguisebrowser.errors import DisguiseError
from disguisebrowser.manager import DeskError, ManagerServer, ProfileDesk

IDENTITY: dict[str, Any] = {
    "navigator": {
        "userAgent": "Mozilla/5.0 (X11; Linux x86_64; rv:146.0) Gecko/20100101 Firefox/146.0",
        "hardwareConcurrency": 4,
    },
    "screen": {"width": 1920, "height": 1080},
    "webgl": {"unmaskedRenderer": "ANGLE (Intel, Mesa)"},
}


class _Context:
    def __init__(self) -> None:
        self.handlers: list[Any] = []

    def on(self, event: str, handler: Any) -> None:
        assert event == "close"
        self.handlers.append(handler)

    def fire(self) -> None:
        for handler in list(self.handlers):
            handler()


class _Mouse:
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []

    async def move(self, x: float, y: float) -> None:
        self.events.append(("move", x, y))

    async def down(self, button: str = "left") -> None:
        self.events.append(("down", button))

    async def up(self, button: str = "left") -> None:
        self.events.append(("up", button))

    async def wheel(self, dx: float, dy: float) -> None:
        self.events.append(("wheel", dx, dy))


class _Keyboard:
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []

    async def press(self, key: str) -> None:
        self.events.append(("press", key))

    async def insert_text(self, text: str) -> None:
        self.events.append(("text", text))


class FakePage:
    def __init__(self) -> None:
        self.visited: list[str] = []
        self.nav: list[str] = []
        self.front = 0
        self.shots = 0
        self.url = "about:blank"
        self.mouse = _Mouse()
        self.keyboard = _Keyboard()

    async def goto(self, url: str, **_kwargs: object) -> None:
        self.visited.append(url)
        self.url = url

    async def bring_to_front(self) -> None:
        self.front += 1

    async def screenshot(self, **_kwargs: object) -> bytes:
        self.shots += 1
        return b"\xff\xd8\xff\xd9"

    async def viewport(self) -> tuple[int, int]:
        return (1280, 720)

    async def go_back(self, **_kwargs: object) -> bool:
        self.nav.append("back")
        return True

    async def go_forward(self, **_kwargs: object) -> bool:
        self.nav.append("forward")
        return True

    async def reload(self, **_kwargs: object) -> None:
        self.nav.append("reload")


class FakeBrowser:
    def __init__(self) -> None:
        self.closed = False
        self.context = _Context()
        self.page = FakePage()
        self.pages = [self.page]

    async def close(self) -> None:
        self.closed = True
        self.context.fire()


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProfileStore:
    monkeypatch.setattr(
        profiles,
        "_draw_fpgen",
        lambda os_name: (json.loads(json.dumps(IDENTITY)), os_name or "linux"),
    )
    return ProfileStore(tmp_path / "profiles")


def _launch_factory() -> tuple[list[FakeBrowser], Any]:
    browsers: list[FakeBrowser] = []

    async def launch(name: str, store: ProfileStore, display: str) -> FakeBrowser:
        del name, store, display
        browser = FakeBrowser()
        browsers.append(browser)
        return browser

    return browsers, launch


async def _settle(browser: FakeBrowser, desk: ProfileDesk, name: str) -> None:
    for _ in range(10):
        await asyncio.sleep(0)
        if browser.closed and name not in desk._running:
            return
    raise AssertionError("browser did not stop")


@asynccontextmanager
async def _served(desk: ProfileDesk) -> AsyncIterator[ManagerServer]:
    server = ManagerServer(desk, port=0)
    server.start(asyncio.get_running_loop())
    try:
        yield server
    finally:
        await desk.shutdown()
        await asyncio.to_thread(server.stop)


def _request(port: int, method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    payload = response.read()
    status = response.status
    conn.close()
    return status, payload


async def request(
    port: int, method: str, path: str, body: dict[str, Any] | None = None
) -> tuple[int, Any]:
    raw = None if body is None else json.dumps(body).encode()
    status, payload = await asyncio.to_thread(_request, port, method, path, raw)
    if payload.startswith(b"{") or payload.startswith(b"["):
        return status, json.loads(payload)
    return status, payload


async def test_start_stop_busy_and_delete(store: ProfileStore) -> None:
    browsers, launch = _launch_factory()
    desk = ProfileDesk(store, display="host", launch=launch)
    store.create("shop", proxy="socks5://user:secret@10.0.0.1:1080")

    with store.lock("shop"):
        assert desk.rows()[0]["status"] == "busy"
        with pytest.raises(DeskError, match="другом окне"):
            await desk.start("shop")

    row = (await desk.start("shop"))["profiles"][0]
    assert row["status"] == "running"
    assert row["proxy"] == "socks5://***@10.0.0.1:1080"
    assert "secret" not in json.dumps(row)
    assert len(browsers) == 1

    with pytest.raises(DeskError, match="выключи"):
        await desk.delete("shop")

    stopped = await desk.stop("shop")
    assert stopped["profiles"][0]["status"] == "stopped"
    assert browsers[0].closed

    await desk.update("shop", {"notes": "витрина", "timezone": "Europe/Moscow"})
    assert store.load("shop").notes == "витрина"
    assert store.load("shop").proxy and "secret" in store.load("shop").proxy

    await desk.delete("shop")
    assert store.list() == []


async def test_second_start_launches_once(store: ProfileStore) -> None:
    calls = 0
    release = asyncio.Event()

    async def launch(name: str, store: ProfileStore, display: str) -> FakeBrowser:
        nonlocal calls
        del name, store, display
        calls += 1
        await release.wait()
        return FakeBrowser()

    desk = ProfileDesk(store, launch=launch)
    store.create("p")
    first = asyncio.create_task(desk.start("p"))
    second = asyncio.create_task(desk.start("p"))
    await asyncio.sleep(0)
    release.set()
    await first
    await second
    assert calls == 1
    assert desk.rows()[0]["status"] == "running"


async def test_shutdown_closes_a_start_still_in_flight(store: ProfileStore) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    browsers: list[FakeBrowser] = []

    async def launch(name: str, store: ProfileStore, display: str) -> FakeBrowser:
        del name, store, display
        entered.set()
        await release.wait()
        browser = FakeBrowser()
        browsers.append(browser)
        return browser

    desk = ProfileDesk(store, launch=launch)
    store.create("p")
    starting = asyncio.create_task(desk.start("p"))
    await entered.wait()
    closing = asyncio.create_task(desk.shutdown())
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(DeskError, match="закрывается"):
        await starting
    await closing
    assert browsers[0].closed
    with pytest.raises(DeskError, match="закрывается"):
        await desk.start("p")


async def test_closing_the_window_stops_the_profile(store: ProfileStore) -> None:
    browsers, launch = _launch_factory()
    desk = ProfileDesk(store, launch=launch)
    store.create("p")
    await desk.start("p")
    browsers[0].context.fire()
    await _settle(browsers[0], desk, "p")
    assert desk.rows()[0]["status"] == "stopped"


async def test_http_lists_switches_and_hides_proxy_passwords(store: ProfileStore) -> None:
    browsers, launch = _launch_factory()
    desk = ProfileDesk(store, launch=launch)
    async with _served(desk) as server:
        status, page = await request(server.port, "GET", "/")
        assert status == 200
        text = page.decode()
        assert "Профили" in text
        assert "Разрешение" in text
        assert "В браузер" in text
        assert "Перейти" not in text
        assert 'id="stage"' in text
        assert b"secret" not in page

        status, created = await request(
            server.port,
            "POST",
            "/api/profiles",
            {"name": "shop", "os": "windows", "proxy": "socks5://user:secret@10.0.0.1:1080"},
        )
        assert status == 201
        assert created["profiles"][0]["name"] == "shop"
        assert "secret" not in json.dumps(created)

        status, again = await request(server.port, "POST", "/api/profiles", {"name": "shop"})
        assert status == 409
        assert "уже есть" in again["error"]

        status, bad = await asyncio.to_thread(
            _request, server.port, "POST", "/api/profiles", b"{not json"
        )
        assert status == 400
        assert "JSON" in json.loads(bad)["error"]

        status, started = await request(server.port, "POST", "/api/profiles/shop/start")
        assert status == 200
        assert started["running"] == 1
        assert len(browsers) == 1

        status, listed = await request(server.port, "GET", "/api/profiles")
        assert listed["profiles"][0]["status"] == "running"
        assert "secret" not in json.dumps(listed)

        status, _denied = await request(server.port, "DELETE", "/api/profiles/shop")
        assert status == 409

        status, patched = await request(
            server.port,
            "PATCH",
            "/api/profiles/shop",
            {"notes": "один", "timezone": "Europe/Paris"},
        )
        assert status == 200
        assert patched["profiles"][0]["notes"] == "один"
        assert store.load("shop").timezone == "Europe/Paris"
        assert "secret" in (store.load("shop").proxy or "")

        status, stopped = await request(server.port, "POST", "/api/stop-all")
        assert status == 200
        assert stopped["running"] == 0
        assert browsers[0].closed

        status, gone = await request(server.port, "DELETE", "/api/profiles/shop")
        assert status == 200
        assert gone["profiles"] == []


async def test_screen_choice_and_opening_a_page(store: ProfileStore) -> None:
    browsers, launch = _launch_factory()
    desk = ProfileDesk(store, launch=launch)
    async with _served(desk) as server:
        status, bad_screen = await request(
            server.port, "POST", "/api/profiles", {"name": "tiny", "screen": "100x100"}
        )
        assert status == 400
        assert "800" in bad_screen["error"]
        assert store.list() == []

        status, created = await request(
            server.port,
            "POST",
            "/api/profiles",
            {"name": "shop", "screen": "1366x768", "start_url": "https://example.com"},
        )
        assert status == 201
        assert created["profiles"][0]["screen"] == "1366x768"
        assert created["profiles"][0]["start_url"] == "https://example.com"
        saved = store.load("shop")
        assert (saved.screen_width, saved.screen_height) == (1366, 768)
        assert saved.identity["screen"]["width"] == 1366
        assert saved.identity["screen"]["availHeight"] < 768

        status, _denied = await request(
            server.port, "POST", "/api/profiles/shop/open", {"url": "javascript:alert(1)"}
        )
        assert status == 400
        assert browsers == []
        assert store.load("shop").start_url == "https://example.com"

        status, opened = await request(
            server.port, "POST", "/api/profiles/shop/open", {"url": "https://example.com/in"}
        )
        assert status == 200
        assert opened["profiles"][0]["status"] == "running"
        assert opened["profiles"][0]["start_url"] == "https://example.com/in"
        assert browsers[0].page.visited == ["https://example.com/in"]
        assert browsers[0].page.front == 1

        status, _again = await request(
            server.port, "POST", "/api/profiles/shop/open", {"url": "https://example.com/next"}
        )
        assert status == 200
        assert len(browsers) == 1
        assert browsers[0].page.visited == ["https://example.com/in", "https://example.com/next"]
        assert browsers[0].page.front == 2

        status, _blocked = await request(
            server.port, "PATCH", "/api/profiles/shop", {"screen": "1920x1080"}
        )
        assert status == 409
        assert store.load("shop").identity["screen"]["width"] == 1366

        await desk.stop("shop")
        status, resized = await request(
            server.port, "PATCH", "/api/profiles/shop", {"screen": "1920x1080"}
        )
        assert status == 200
        assert resized["profiles"][0]["screen"] == "1920x1080"
        assert store.load("shop").identity["navigator"]["userAgent"]
        assert store.load("shop").screen_width == 1920


def _exchange(
    port: int, method: str, path: str, body: bytes | None = None
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    payload = response.read()
    got = {key.lower(): value for key, value in response.getheaders()}
    status = response.status
    conn.close()
    return status, got, payload


async def test_live_view_forwards_pointer_keys_and_history(store: ProfileStore) -> None:
    browsers, launch = _launch_factory()
    desk = ProfileDesk(store, launch=launch)
    async with _served(desk) as server:
        await request(server.port, "POST", "/api/profiles", {"name": "shop"})
        status, _headers, body = await asyncio.to_thread(
            _exchange, server.port, "GET", "/api/profiles/shop/frame"
        )
        assert status == 409
        assert "включи" in json.loads(body)["error"]

        await request(server.port, "POST", "/api/profiles/shop/open", {"url": "https://example.com/in"})
        status, headers, jpeg = await asyncio.to_thread(
            _exchange, server.port, "GET", "/api/profiles/shop/frame"
        )
        assert status == 200
        assert headers["content-type"] == "image/jpeg"
        assert headers["x-view-width"] == "1280"
        assert headers["x-view-height"] == "720"
        assert headers["x-view-url"] == "https://example.com/in"
        assert jpeg == b"\xff\xd8\xff\xd9"
        assert browsers[0].page.shots == 1

        status, again_headers, again = await asyncio.to_thread(
            _exchange, server.port, "GET", "/api/profiles/shop/frame"
        )
        assert status == 200
        assert again == jpeg
        assert again_headers["x-view-width"] == "1280"
        assert browsers[0].page.shots == 1

        status, moved = await request(
            server.port,
            "POST",
            "/api/profiles/shop/input",
            {"kind": "move", "x": 5000, "y": 20},
        )
        assert status == 200
        assert moved == {"ok": True}
        status, _clicked = await request(
            server.port,
            "POST",
            "/api/profiles/shop/input",
            {"kind": "down", "x": 12, "y": 8, "button": "right"},
        )
        assert status == 200
        await request(
            server.port,
            "POST",
            "/api/profiles/shop/input",
            {"kind": "up", "x": 12, "y": 8, "button": "right"},
        )
        await request(
            server.port,
            "POST",
            "/api/profiles/shop/input",
            {"kind": "wheel", "x": 4, "y": 5, "dx": 0, "dy": -40},
        )
        await request(
            server.port, "POST", "/api/profiles/shop/input", {"kind": "text", "text": "привет"}
        )
        await request(
            server.port, "POST", "/api/profiles/shop/input", {"kind": "press", "key": "Control+A"}
        )
        await request(server.port, "POST", "/api/profiles/shop/input", {"kind": "back"})
        page = browsers[0].page
        assert page.mouse.events == [
            ("move", 1279, 20),
            ("move", 12, 8),
            ("down", "right"),
            ("move", 12, 8),
            ("up", "right"),
            ("move", 4, 5),
            ("wheel", 0, -40),
        ]
        assert page.keyboard.events == [("text", "привет"), ("press", "Control+A")]
        assert page.nav == ["back"]

        status, _bad = await request(
            server.port, "POST", "/api/profiles/shop/input", {"kind": "press", "key": "../x"}
        )
        assert status == 400
        status, _off = await request(
            server.port, "POST", "/api/profiles/shop/input", {"kind": "move", "x": True, "y": 1}
        )
        assert status == 400

        await desk.stop("shop")
        status, _stopped = await request(
            server.port, "POST", "/api/profiles/shop/input", {"kind": "press", "key": "Enter"}
        )
        assert status == 409


def test_manager_command_parses() -> None:
    args = cli.build_parser().parse_args(
        ["manager", "--no-open", "--port", "9", "--root", "/tmp/profiles", "--display", "host"]
    )
    assert args.command == "manager"
    assert args.no_open is True
    assert args.port == 9
    assert args.display == "host"
    assert args.func is cli._cmd_manager


def test_manager_refuses_a_public_address(store: ProfileStore) -> None:
    desk = ProfileDesk(store, launch=_launch_factory()[1])
    server = ManagerServer(desk, host="0.0.0.0", port=0)
    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(DisguiseError, match=r"127\.0\.0\.1"):
            server.start(loop)
    finally:
        loop.close()
