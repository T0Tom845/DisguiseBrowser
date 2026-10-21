"""The Camoufox engine end to end: identity, interaction, pinned persistent profiles, MCP."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("camoufox")
pytest.importorskip("playwright")

from disguisebrowser.camoufox import CamoufoxBrowser, ProfileStore, launch
from disguisebrowser.camoufox.profiles import ProfileError
from disguisebrowser.errors import EvaluationError, LaunchError
from disguisebrowser.fingerprint import capture_with_browser
from disguisebrowser.protocols import BrowserLike, PageLike

pytestmark = pytest.mark.integration


def _is_dir(path: Path) -> bool:
    return path.is_dir()


def _display() -> str:
    if shutil.which("Xvfb"):
        return "virtual"
    if os.environ.get("DISPLAY"):
        return "host"
    pytest.skip("needs Xvfb or $DISPLAY")


@pytest.fixture(scope="module")
async def fox(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[CamoufoxBrowser]:
    store = ProfileStore(tmp_path_factory.mktemp("profiles"))
    try:
        async with launch(
            profile="shared",
            store=store,
            create=True,
            os="windows",
            display=_display(),  # type: ignore[arg-type]
            main_world=True,
        ) as browser:
            yield browser
    except (ImportError, LaunchError) as exc:
        pytest.skip(f"Camoufox is not usable here: {exc}")


@pytest.fixture
async def page(fox: CamoufoxBrowser) -> AsyncIterator[PageLike]:
    from tests.conftest import screenshot_on_failure  # noqa: PLC0415

    tab = await fox.new_page()
    try:
        yield tab
    except Exception:
        await screenshot_on_failure(tab)
        raise
    finally:
        await tab.close()


# ---------------------------------------------------------------------- identity
async def test_satisfies_the_shared_protocols(fox: CamoufoxBrowser, page: PageLike) -> None:
    assert isinstance(fox, BrowserLike)
    assert isinstance(page, PageLike)
    assert fox.engine == "camoufox"
    assert fox.version["product"].startswith("Camoufox/")


async def test_device_matches_the_pinned_profile(fox: CamoufoxBrowser, page: PageLike) -> None:
    seen = await fox.identity()
    assert "Firefox/" in seen["userAgent"]
    assert "Windows NT 10.0" in seen["userAgent"]
    assert seen["platform"] == "Win32"
    assert fox.profile is not None
    # The OS part is pinned; Camoufox rewrites the Firefox version to its real build.
    pinned = re.sub(r"\d+\.\d+", "N", fox.profile.summary()["user_agent"])
    assert pinned == re.sub(r"\d+\.\d+", "N", seen["userAgent"])
    assert await page.evaluate("navigator.webdriver") is False


# ---------------------------------------------------------------------- interaction
async def test_snapshot_refs_drive_the_page(page: PageLike, fixture_server: str) -> None:
    await page.goto(f"{fixture_server}/interaction.html")
    assert await page.title() == "Interaction fixture"
    snap = (await page.snapshot()).text
    assert 'heading "Interaction fixture"' in snap
    button = re.search(r'button "Click me"[^\n]*?\[ref=(e\d+)\]', snap)
    textbox = re.search(r'textbox "Query"[^\n]*?\[ref=(e\d+)\]', snap)
    assert button and textbox, snap

    await (await page.element_by_ref(textbox.group(1))).type("hello")
    await (await page.element_by_ref(button.group(1))).click()

    assert await page.evaluate("document.querySelector('#query').value") == "hello"
    assert await page.evaluate("document.querySelector('#status').textContent") == "clicked"
    clicks = await page.evaluate("window.__events.clicks", world="main")
    assert len(clicks) == 1 and clicks[0]["isTrusted"] is True


async def test_scroll_and_late_elements(page: PageLike, fixture_server: str) -> None:
    await page.goto(f"{fixture_server}/interaction.html")
    handle = await page.wait_for_selector("#late", timeout=10, state="visible")
    assert handle is not None
    assert await handle.text() == "late element"
    await page.scroll(500)
    assert await page.evaluate("window.scrollY") > 100


async def test_screenshot_is_a_png(page: PageLike, fixture_server: str) -> None:
    await page.goto(f"{fixture_server}/interaction.html")
    assert (await page.screenshot())[:8] == b"\x89PNG\r\n\x1a\n"


async def test_main_world_sees_page_globals_and_is_opt_in(
    page: PageLike, fixture_server: str
) -> None:
    await page.goto(f"{fixture_server}/interaction.html")
    assert await page.evaluate("typeof window.__events", world="main") == "object"
    assert await page.evaluate("typeof window.__events") == "undefined"  # isolated world

    async with launch(display=_display()) as plain:  # type: ignore[arg-type]
        tab = await plain.new_page(f"{fixture_server}/interaction.html")
        with pytest.raises(EvaluationError, match=r"main_world=True"):
            await tab.evaluate("window.__events", world="main")


async def test_warmup_accepts_a_local_url(fox: CamoufoxBrowser, fixture_server: str) -> None:
    visits = await fox.warmup(urls=[f"{fixture_server}/interaction.html"], count=1)
    assert [v.ok for v in visits] == [True]


# ---------------------------------------------------------------------- capture
async def test_capture_records_the_engine(fox: CamoufoxBrowser) -> None:
    record = await capture_with_browser(fox, timeout=120)
    assert record["meta"]["source"]["engine"] == "camoufox"
    assert "Firefox/" in record["navigator"]["userAgent"]
    assert record["navigator"]["userAgent"] == record["worker"]["userAgent"]
    assert record["navigator"]["platform"] == record["worker"]["platform"]


# ---------------------------------------------------------------------- profiles
async def test_profile_pins_identity_and_storage_across_launches(
    tmp_path: Path, fixture_server: str
) -> None:
    store = ProfileStore(tmp_path)
    mode: Any = _display()
    tomorrow = time.time() + 86400

    async with launch(profile="stable", store=store, create=True, os="linux", display=mode) as one:
        first = await one.identity()
        page = await one.new_page(f"{fixture_server}/interaction.html")
        await page.evaluate("localStorage.setItem('seen', 'yes')")
        await one.context.add_cookies(
            [{"name": "k", "value": "v", "url": fixture_server, "expires": tomorrow}]
        )
        stored = store.load("stable")

    async with launch(profile="stable", store=store, display=mode) as two:
        second = await two.identity()
        page = await two.new_page(f"{fixture_server}/interaction.html")
        remembered = await page.evaluate("localStorage.getItem('seen')")
        cookies = await two.context.cookies(fixture_server)

    assert first["userAgent"] == second["userAgent"]
    assert first["screen"] == second["screen"]
    assert first["cores"] == second["cores"]
    assert remembered == "yes", "the profile must keep its storage"
    assert [c["name"] for c in cookies] == ["k"], "the profile must keep its cookies"
    assert store.load("stable") == stored, "relaunching must not rewrite the identity"


async def test_two_profiles_are_different_devices(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    mode: Any = _display()
    seen = []
    for name in ("a", "b"):
        async with launch(profile=name, store=store, create=True, os="windows", display=mode) as b:
            seen.append(json.dumps(await b.identity(), sort_keys=True))
            seen.append(json.dumps(b.profile.identity, sort_keys=True))  # type: ignore[union-attr]
    assert seen[0] != seen[2] or seen[1] != seen[3]


async def test_a_profile_cannot_run_twice(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    mode: Any = _display()
    async with launch(profile="solo", store=store, create=True, display=mode):
        with pytest.raises(LaunchError, match="in use"):
            await launch(profile="solo", store=store, display=mode)
        with pytest.raises(ProfileError, match="in use"):
            store.delete("solo")
    store.delete("solo")


async def test_ephemeral_run_cleans_up_after_itself() -> None:
    async with launch(display=_display()) as browser:  # type: ignore[arg-type]
        directory = Path(browser.user_data_dir)
        assert _is_dir(directory)
        assert browser.profile is None
    assert not _is_dir(directory)


async def test_startup_failure_releases_the_profile_lock(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    store.create("p", os="windows")
    with pytest.raises(LaunchError):
        await launch(
            profile="p",
            store=store,
            display=_display(),  # type: ignore[arg-type]
            camoufox_options={"executable_path": "/nonexistent/camoufox"},
        )
    with store.lock("p"):  # would raise if the failed start had leaked the lock
        pass


async def test_dialogs_responses_files_and_downloads(page: PageLike, fixture_server: str) -> None:
    await page.goto(f"{fixture_server}/exchange.html")
    async with page.expect_dialog() as pending:
        await page.click("#prompt", humanize=False)
        dialog = await pending.value()
        await dialog.accept("ada")
    assert await page.evaluate("document.querySelector('#out').textContent") == "ada"

    async with page.expect_response("payload.json") as pending:
        await page.click("#fetch", humanize=False)
        response = await pending.value()
    assert await response.json() == {"ok": True, "n": 1}

    field = await page.query("#file")
    assert field is not None
    await field.set_input_files(Path(__file__).parents[1] / "fixtures" / "sample.txt")
    assert await page.evaluate("document.querySelector('#file-name').textContent") == "sample.txt"

    async with page.expect_download() as pending:
        await page.click("#download", humanize=False)
        download = await pending.value()
    assert download.path.read_text(encoding="utf-8") == "sample-ok\n"


# ---------------------------------------------------------------------- MCP
async def test_mcp_session_on_camoufox(fixture_server: str, tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    from mcp import Client  # noqa: PLC0415

    from disguisebrowser.mcp_server import BrowserSessions, build_server  # noqa: PLC0415

    def text(result: Any) -> str:
        return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")

    def data(result: Any) -> Any:
        sc = result.structured_content
        if sc is not None:
            return sc.get("result", sc) if isinstance(sc, dict) else sc
        return json.loads(text(result))

    os.environ["DISGUISE_HOME"] = str(tmp_path)
    registry = BrowserSessions()
    try:
        async with Client(build_server(registry)) as client:
            made = data(
                await client.call_tool("profile_create", {"name": "agent-1", "os": "linux"})
            )
            assert made["name"] == "agent-1"
            listed = data(await client.call_tool("profile_list", {}))
            assert [p["name"] for p in listed] == ["agent-1"]

            launched = data(
                await client.call_tool(
                    "browser_launch",
                    {"engine": "camoufox", "profile": "agent-1", "display": _display()},
                )
            )
            sid = launched["session_id"]
            assert launched["engine"] == "camoufox"
            assert launched["profile"] == "agent-1"
            assert launched["browser"].startswith("Camoufox/")
            assert "chrome" not in launched

            await client.call_tool(
                "navigate", {"session_id": sid, "url": f"{fixture_server}/interaction.html"}
            )
            snap = text(await client.call_tool("snapshot", {"session_id": sid}))
            ref = re.search(r'button "Click me"[^\n]*?\[ref=(e\d+)\]', snap)
            assert ref, snap
            await client.call_tool("click", {"session_id": sid, "target": ref.group(1)})
            status = text(
                await client.call_tool("extract_text", {"session_id": sid, "selector": "#status"})
            )
            assert status == "clicked"

            listing = data(await client.call_tool("browser_list", {}))
            assert listing[0]["engine"] == "camoufox"

            wrong = await client.call_tool(
                "browser_launch", {"engine": "camoufox", "gpu": "intel-xe"}
            )
            assert wrong.is_error and "chrome options" in text(wrong)
            wrong = await client.call_tool("browser_launch", {"profile": "agent-1"})
            assert wrong.is_error and "engine='camoufox' only" in text(wrong)

            assert "closed" in text(await client.call_tool("browser_close", {"session_id": sid}))
    finally:
        os.environ.pop("DISGUISE_HOME", None)
        await registry.close_all()
