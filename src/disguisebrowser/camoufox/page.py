"""``CamoufoxPage`` / ``CamoufoxElement``: the :class:`~disguisebrowser.page.Page`
API on top of a Playwright page driving Camoufox (Firefox + Juggler).

Differences from the Chrome engine worth knowing:

* Evaluation runs in Juggler's isolated world by default, which the page cannot
  observe. ``world="main"`` needs ``launch(main_world=True)`` and goes through
  Camoufox's ``mw:`` prefix.
* Cursor humanization (Cursory recordings) is a launch-time switch of Camoufox,
  not per call: the ``humanize=`` argument of ``click``/``hover`` is accepted
  for API compatibility. Typing and scrolling honour it per call.
* Input events are produced by Firefox's own input handlers (trusted), not by a
  protocol shim, so there is no OS-level input backend.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import re
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from playwright.async_api import ElementHandle as PwElement
from playwright.async_api import Error as PwError
from playwright.async_api import Page as PwPage
from playwright.async_api import TimeoutError as PwTimeout

from disguisebrowser.errors import (
    DisguiseError,
    ElementNotFoundError,
    EvaluationError,
    NavigationError,
)
from disguisebrowser.exchange import (
    Download,
    HttpResponse,
    JsDialog,
    Pending,
    ResponseLog,
    existing_files,
    safe_filename,
)
from disguisebrowser.input import PRESETS, HumanProfile
from disguisebrowser.input.humanizer import scroll_steps, typing_delays
from disguisebrowser.protocols import WaitUntil, World
from disguisebrowser.snapshot import Snapshot

if TYPE_CHECKING:
    from disguisebrowser.camoufox.browser import CamoufoxBrowser

_WAIT_UNTIL = frozenset({"commit", "domcontentloaded", "load", "networkidle"})
_REF = re.compile(r"\[ref=([A-Za-z0-9]+)\]")
_SAFE_REF = re.compile(r"^[A-Za-z0-9]+$")
_SCROLL_STATE = "[window.scrollX, window.scrollY]"


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


@contextlib.asynccontextmanager
async def _errors(what: str, *, timeout: float | None = None) -> AsyncIterator[None]:
    """Translate Playwright errors into this library's exception types."""
    try:
        yield
    except PwTimeout as exc:
        limit = f" within {timeout:g}s" if timeout is not None else ""
        raise TimeoutError(f"{what} timed out{limit}: {_first_line(exc)}") from None
    except PwError as exc:
        raise EvaluationError(f"{what}: {_first_line(exc)}") from None


class CamoufoxKeyboard:
    """Keyboard with per-key humanized timing (same timing model as the Chrome engine)."""

    def __init__(self, page: CamoufoxPage) -> None:
        self._page = page

    async def down(self, key: str) -> None:
        await self._page._page.keyboard.down(key)

    async def up(self, key: str) -> None:
        await self._page._page.keyboard.up(key)

    async def press(self, key: str, *, humanize: bool = True) -> None:
        hold = self._page.rng.uniform(35.0, 95.0) if humanize else 0.0
        async with _errors(f"press {key!r}"):
            await self._page._page.keyboard.press(key, delay=hold)

    async def insert_text(self, text: str) -> None:
        await self._page._page.keyboard.insert_text(text)

    async def type(self, text: str, *, humanize: bool = True) -> None:
        keyboard = self._page._page.keyboard
        if not humanize:
            await keyboard.type(text)
            return
        delays = typing_delays(text, rng=self._page.rng, profile=self._page.human)
        for char, delay in zip(text, delays, strict=True):
            await asyncio.sleep(delay / 1000.0)
            async with _errors(f"type {char!r}"):
                # ``type`` falls back to insertText for characters that have no key on
                # the US layout (Cyrillic, CJK, …), so any script works.
                await keyboard.type(char)


class CamoufoxPage:
    """A tab of a Camoufox browser."""

    def __init__(
        self,
        browser: CamoufoxBrowser,
        page: PwPage,
        *,
        human: HumanProfile = PRESETS["default"],
        rng: random.Random | None = None,
    ) -> None:
        self.browser = browser
        self._page = page
        self.human = human
        self._rng = rng or random.Random()
        self.keyboard = CamoufoxKeyboard(self)
        self.dialog_action: Literal["dismiss", "accept"] = "dismiss"
        self.dialog_prompt = ""
        self._dialog_future: asyncio.Future[JsDialog] | None = None
        self._open_dialog: JsDialog | None = None
        self._responses = ResponseLog()
        page.on("dialog", self._on_dialog)

    async def _on_dialog(self, dialog: Any) -> None:
        async def reply(accept: bool, prompt_text: str | None) -> None:
            try:
                if accept:
                    await dialog.accept(prompt_text or "")
                else:
                    await dialog.dismiss()
            except PwError:
                pass

        wrapped = JsDialog(
            type=str(dialog.type),
            message=str(dialog.message),
            default_value=str(dialog.default_value or ""),
            reply=reply,
        )
        waiting = self._dialog_future
        if waiting is not None and not waiting.done():
            self._open_dialog = wrapped
            waiting.set_result(wrapped)
            return
        try:
            if self.dialog_action == "accept":
                await wrapped.accept(self.dialog_prompt or None)
            else:
                await wrapped.dismiss()
        except (DisguiseError, PwError):
            pass

    @contextlib.asynccontextmanager
    async def expect_dialog(self, *, timeout: float = 5.0) -> Any:
        """See :meth:`disguisebrowser.page.Page.expect_dialog`."""
        if self._dialog_future is not None:
            raise DisguiseError("already waiting for a dialog")
        future: asyncio.Future[JsDialog] = asyncio.get_running_loop().create_future()
        self._dialog_future = future
        try:
            yield Pending(future, timeout, "a JavaScript dialog")
        finally:
            self._dialog_future = None
            dialog = self._open_dialog
            self._open_dialog = None
            if not future.done():
                future.cancel()
            elif dialog is not None and not dialog.handled:
                await dialog.dismiss()

    # ------------------------------------------------------------------ state
    @property
    def playwright_page(self) -> PwPage:
        """Escape hatch for Playwright features this API does not wrap."""
        return self._page

    @property
    def url(self) -> str:
        return self._page.url

    @property
    def closed(self) -> bool:
        return self._page.is_closed()

    @property
    def rng(self) -> random.Random:
        return self._rng

    async def close(self) -> None:
        if not self._page.is_closed():
            with contextlib.suppress(PwError):
                await self._page.close()

    # ------------------------------------------------------------------ navigation
    async def goto(
        self, url: str, *, wait_until: WaitUntil = "load", timeout: float = 30.0
    ) -> None:
        if wait_until not in _WAIT_UNTIL:
            raise ValueError(f"wait_until must be one of {sorted(_WAIT_UNTIL)}")
        try:
            await self._page.goto(url, wait_until=wait_until, timeout=timeout * 1000)
        except PwTimeout:
            raise TimeoutError(f"{url} did not reach {wait_until!r} within {timeout:g}s") from None
        except PwError as exc:
            raise NavigationError(f"{url}: {_first_line(exc)}") from None

    async def reload(self, *, wait_until: WaitUntil = "load", timeout: float = 30.0) -> None:
        async with _errors("reload", timeout=timeout):
            await self._page.reload(wait_until=wait_until, timeout=timeout * 1000)

    async def wait_for_load_state(
        self, state: WaitUntil = "load", *, timeout: float = 30.0
    ) -> None:
        if state == "commit":
            return
        async with _errors(f"wait for {state}", timeout=timeout):
            await self._page.wait_for_load_state(state, timeout=timeout * 1000)

    async def wait_for_timeout(self, ms: float) -> None:
        await asyncio.sleep(ms / 1000.0)

    async def bring_to_front(self) -> None:
        await self._page.bring_to_front()
        self.browser._active_page = self

    async def activate(self) -> None:
        """Playwright pages are always focus-emulated; kept for API parity."""
        await self.bring_to_front()

    async def go_back(self, *, wait_until: WaitUntil = "load", timeout: float = 30.0) -> bool:
        return await self._history(self._page.go_back, wait_until, timeout)

    async def go_forward(self, *, wait_until: WaitUntil = "load", timeout: float = 30.0) -> bool:
        return await self._history(self._page.go_forward, wait_until, timeout)

    async def _history(self, step: Any, wait_until: WaitUntil, timeout: float) -> bool:
        before = self._page.url
        async with _errors("history navigation", timeout=timeout):
            response = await step(wait_until=wait_until, timeout=timeout * 1000)
        # Same-document moves (``#hash``, pushState) return no response.
        return response is not None or self._page.url != before

    # ------------------------------------------------------------------ evaluation
    def _source(self, source: str, world: World) -> str:
        if world == "isolated":
            return source
        if not self.browser.main_world:
            raise EvaluationError(
                "world='main' needs launch(main_world=True): Camoufox only enables "
                "main-world evaluation on request"
            )
        return "mw:" + source

    async def evaluate(self, expression: str, *, world: World = "isolated") -> Any:
        async with _errors("evaluate"):
            return await self._page.evaluate(self._source(expression, world))

    async def call(self, function: str, *args: Any, world: World = "isolated") -> Any:
        """Call a JavaScript *function source* (``(a, b) => …``) with JSON arguments."""
        async with _errors("call"):
            if world == "isolated":
                return await self._page.evaluate(f"(a) => ({function})(...a)", list(args))
            # The ``mw:`` prefix turns the whole string into a main-world expression, so
            # arguments are inlined as JSON instead of passed through Playwright.
            return await self._page.evaluate(
                self._source(f"({function})(...{json.dumps(list(args))})", world)
            )

    # ------------------------------------------------------------------ DOM
    def _wrap(self, handle: PwElement | None) -> CamoufoxElement | None:
        return CamoufoxElement(self, handle) if handle is not None else None

    async def query(self, selector: str) -> CamoufoxElement | None:
        async with _errors(f"query {selector!r}"):
            return self._wrap(await self._page.query_selector(selector))

    async def query_all(self, selector: str) -> list[CamoufoxElement]:
        async with _errors(f"query_all {selector!r}"):
            handles = await self._page.query_selector_all(selector)
        return [CamoufoxElement(self, handle) for handle in handles]

    async def xpath(self, expression: str) -> list[CamoufoxElement]:
        return await self.query_all(f"xpath={expression}")

    async def wait_for_selector(
        self,
        selector: str,
        *,
        timeout: float = 30.0,
        state: Literal["attached", "visible", "hidden"] = "attached",
    ) -> CamoufoxElement | None:
        try:
            handle = await self._page.wait_for_selector(
                selector, state=state, timeout=timeout * 1000
            )
        except PwTimeout:
            raise TimeoutError(
                f"selector {selector!r} did not become {state} within {timeout:g}s"
            ) from None
        except PwError as exc:
            raise EvaluationError(f"wait_for_selector {selector!r}: {_first_line(exc)}") from None
        return self._wrap(handle)

    async def snapshot(self, *, max_lines: int = 400) -> Snapshot:
        """Accessibility tree with ``[ref=eN]`` handles, from Playwright's AI snapshot."""
        async with _errors("snapshot"):
            text = await self._page.aria_snapshot(mode="ai", timeout=60_000)
        lines = text.splitlines()
        truncated = len(lines) > max_lines
        kept = lines[:max_lines]
        if truncated:
            kept.append(f"… (truncated, {len(lines) - max_lines} more lines)")
        refs = {ref: index for index, line in enumerate(kept) for ref in _REF.findall(line)}
        return Snapshot(text="\n".join(kept), refs=refs, truncated=truncated)

    async def element_by_ref(self, ref: str) -> CamoufoxElement:
        """Resolve a ``[ref=eN]`` from the last :meth:`snapshot` into a handle."""
        if not _SAFE_REF.match(ref):
            raise ElementNotFoundError(f"malformed ref {ref!r}")
        try:
            handle = await self._page.locator(f"aria-ref={ref}").element_handle(timeout=3000)
        except (PwError, PwTimeout):
            raise ElementNotFoundError(f"unknown ref {ref!r}; take a new snapshot first") from None
        return CamoufoxElement(self, handle)

    async def frames(self) -> list[dict[str, str | None]]:
        """Every frame of this tab, including cross-origin iframes."""
        frames = self._page.frames
        ids = {id(frame): f"f{index}" for index, frame in enumerate(frames)}
        found: list[dict[str, str | None]] = []
        for frame in frames:
            parent = frame.parent_frame
            found.append(
                {
                    "id": ids[id(frame)],
                    "url": frame.url,
                    "name": frame.name or "",
                    "parent_id": ids.get(id(parent)) if parent is not None else None,
                }
            )
        return found

    async def content(self) -> str:
        return str(await self.evaluate("document.documentElement.outerHTML"))

    async def title(self) -> str:
        return str(await self.evaluate("document.title"))

    async def viewport(self) -> tuple[int, int]:
        size = await self.evaluate("[window.innerWidth, window.innerHeight]")
        return int(size[0]), int(size[1])

    # ------------------------------------------------------------------ interaction
    async def _require(self, selector: str, timeout: float) -> CamoufoxElement:
        try:
            handle = await self.wait_for_selector(selector, timeout=timeout, state="attached")
        except TimeoutError as exc:
            raise ElementNotFoundError(str(exc)) from None
        if handle is None:
            raise ElementNotFoundError(f"selector {selector!r} matched nothing")
        return handle

    async def click(self, selector: str, *, humanize: bool = True, timeout: float = 30.0) -> None:
        await (await self._require(selector, timeout)).click(humanize=humanize)

    async def hover(self, selector: str, *, humanize: bool = True, timeout: float = 30.0) -> None:
        await (await self._require(selector, timeout)).hover(humanize=humanize)

    async def type(
        self,
        selector: str,
        text: str,
        *,
        humanize: bool = True,
        clear: bool = False,
        timeout: float = 30.0,
    ) -> None:
        await (await self._require(selector, timeout)).type(text, humanize=humanize, clear=clear)

    async def _wait_scroll_settled(self, *, started_from: Any = None, timeout: float = 1.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        previous = await self.evaluate(_SCROLL_STATE)
        if started_from is not None:
            begin_deadline = loop.time() + 0.4
            while previous == started_from and loop.time() < begin_deadline:
                await asyncio.sleep(0.03)
                previous = await self.evaluate(_SCROLL_STATE)
        while loop.time() < deadline:
            await asyncio.sleep(0.06)
            current = await self.evaluate(_SCROLL_STATE)
            if current == previous:
                return
            previous = current

    async def scroll(self, delta_y: float, *, humanize: bool = True) -> None:
        width, height = await self.viewport()
        x = self._rng.uniform(width * 0.3, width * 0.7)
        y = self._rng.uniform(height * 0.3, height * 0.7)
        before = await self.evaluate(_SCROLL_STATE)
        async with _errors("scroll"):
            await self._page.mouse.move(x, y)
            if humanize:
                for step, pause in scroll_steps(delta_y, rng=self._rng, profile=self.human):
                    await self._page.mouse.wheel(0, step)
                    await asyncio.sleep(pause / 1000.0)
            else:
                await self._page.mouse.wheel(0, delta_y)
        await self._wait_scroll_settled(started_from=before)

    # ------------------------------------------------------------------ capture & state
    async def screenshot(
        self,
        path: str | Path | None = None,
        *,
        full_page: bool = False,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int | None = None,
    ) -> bytes:
        if format == "webp":
            raise ValueError("Firefox cannot encode webp screenshots; use png or jpeg")
        options: dict[str, Any] = {"full_page": full_page, "type": format}
        if quality is not None and format == "jpeg":
            options["quality"] = quality
        async with _errors("screenshot"):
            data = await self._page.screenshot(**options)
        if path is not None:
            await asyncio.to_thread(Path(path).write_bytes, data)
        return data

    async def watch_responses(self) -> None:
        """Start passive capture. Playwright's response listener does not intercept."""
        if self._responses.enabled:
            return
        self._page.on("response", self._on_response)
        self._responses.enabled = True

    def responses(self) -> list[HttpResponse]:
        return list(self._responses.entries)

    def _on_response(self, response: Any) -> None:
        request = response.request

        async def read() -> bytes:
            return bytes(await response.body())

        self._responses.add(
            HttpResponse(
                url=str(response.url),
                method=str(request.method),
                status=int(response.status),
                resource_type=str(request.resource_type),
                headers={str(k): str(v) for k, v in response.headers.items()},
                reader=read,
            )
        )

    @contextlib.asynccontextmanager
    async def expect_response(self, url_contains: str, *, timeout: float = 30.0) -> Any:
        await self.watch_responses()
        future = self._responses.expect(url_contains)
        try:
            yield Pending(future, timeout, f"a response containing {url_contains!r}")
        finally:
            self._responses.cancel(future)

    @contextlib.asynccontextmanager
    async def expect_download(self, *, timeout: float = 30.0) -> Any:
        future: asyncio.Future[Download] = asyncio.get_running_loop().create_future()
        async with self._page.expect_download(timeout=timeout * 1000) as info:

            async def resolve() -> None:
                try:
                    raw = await info.value
                    directory = Path(tempfile.mkdtemp(prefix="disguise-dl-"))
                    target = directory / safe_filename(str(raw.suggested_filename))
                    await raw.save_as(target)
                    if not future.done():
                        future.set_result(
                            Download(
                                url=str(raw.url),
                                suggested_filename=str(raw.suggested_filename),
                                path=target,
                            )
                        )
                except Exception as exc:
                    if not future.done():
                        future.set_exception(exc)

            task = asyncio.create_task(resolve())
            try:
                yield Pending(future, timeout, "a download")
            finally:
                if not future.done():
                    task.cancel()

    async def cookies(self, *urls: str) -> list[dict[str, Any]]:
        return [dict(c) for c in await self._page.context.cookies(list(urls))]

    async def set_cookies(self, cookies: list[dict[str, Any]]) -> None:
        await self._page.context.add_cookies(cookies)  # type: ignore[arg-type]

    async def clear_cookies(self) -> None:
        await self._page.context.clear_cookies()

    def __repr__(self) -> str:
        return f"<CamoufoxPage {self.url!r}>"


class CamoufoxElement:
    """A DOM element handle (isolated world)."""

    def __init__(self, page: CamoufoxPage, handle: PwElement) -> None:
        self.page = page
        self._handle = handle

    async def evaluate(self, function: str, *args: Any) -> Any:
        """Call ``function`` with ``this`` bound to the element."""
        async with _errors("element evaluate"):
            return await self._handle.evaluate(f"(el, a) => ({function}).apply(el, a)", list(args))

    async def text(self) -> str:
        return str(
            await self.evaluate("function () { return this.innerText ?? this.textContent; }")
        )

    async def text_content(self) -> str:
        return str(await self.evaluate("function () { return this.textContent; }"))

    async def inner_html(self) -> str:
        return str(await self.evaluate("function () { return this.innerHTML; }"))

    async def outer_html(self) -> str:
        return str(await self.evaluate("function () { return this.outerHTML; }"))

    async def attribute(self, name: str) -> str | None:
        value = await self.evaluate("function (n) { return this.getAttribute(n); }", name)
        return None if value is None else str(value)

    async def attributes(self) -> dict[str, str]:
        result = await self.evaluate(
            "function () { return Object.fromEntries("
            "[...this.attributes].map(a => [a.name, a.value])); }"
        )
        return dict(result or {})

    async def value(self) -> str:
        return str(await self.evaluate("function () { return this.value ?? ''; }"))

    async def bounding_box(self) -> dict[str, float] | None:
        box = await self.evaluate(
            "function () { const r = this.getBoundingClientRect(); "
            "return { x: r.x, y: r.y, width: r.width, height: r.height }; }"
        )
        if not box or box["width"] <= 0 or box["height"] <= 0:
            return None
        return {k: float(v) for k, v in box.items()}

    async def is_visible(self) -> bool:
        return bool(
            await self.evaluate(
                "function () { const r = this.getBoundingClientRect(); "
                "const css = this.checkVisibility ? this.checkVisibility({ checkOpacity: true, "
                "checkVisibilityCSS: true }) : true; return css && r.width > 0 && r.height > 0; }"
            )
        )

    async def query(self, selector: str) -> CamoufoxElement | None:
        async with _errors(f"query {selector!r}"):
            return self.page._wrap(await self._handle.query_selector(selector))

    async def query_all(self, selector: str) -> list[CamoufoxElement]:
        async with _errors(f"query_all {selector!r}"):
            handles = await self._handle.query_selector_all(selector)
        return [CamoufoxElement(self.page, handle) for handle in handles]

    async def scroll_into_view(self, *, humanize: bool = True) -> dict[str, float]:
        async with _errors("scroll into view"):
            await self._handle.scroll_into_view_if_needed(timeout=10_000)
        box = await self.bounding_box()
        if box is None:
            raise ElementNotFoundError("element has no layout box (hidden or detached)")
        return box

    async def hover(self, *, humanize: bool = True) -> None:
        async with _errors("hover"):
            await self._handle.hover()

    async def click(
        self, *, humanize: bool = True, button: Literal["left", "right", "middle"] = "left"
    ) -> None:
        async with _errors("click"):
            await self._handle.click(button=button)

    async def set_input_files(self, *paths: str | Path) -> None:
        """Set the files of an ``<input type="file">``. Pass nothing to clear it."""
        files = await asyncio.to_thread(existing_files, paths)
        async with _errors("set input files"):
            await self._handle.set_input_files(files)

    async def type(self, text: str, *, humanize: bool = True, clear: bool = False) -> None:
        await self.click(humanize=humanize)
        keyboard = self.page.keyboard
        if clear:
            await keyboard.down("ControlOrMeta")
            await keyboard.press("a", humanize=humanize)
            await keyboard.up("ControlOrMeta")
            await keyboard.press("Backspace", humanize=humanize)
        await keyboard.type(text, humanize=humanize)

    async def dispose(self) -> None:
        with contextlib.suppress(PwError):
            await self._handle.dispose()

    def __repr__(self) -> str:
        return "<CamoufoxElement>"
