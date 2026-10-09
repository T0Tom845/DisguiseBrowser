"""Page and ElementHandle: navigation, isolated-world evaluation, DOM access,
humanized interaction, screenshots and cookies — all through quiet CDP.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import random
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from disguisebrowser.cdp import Session
from disguisebrowser.errors import (
    CdpError,
    ElementNotFoundError,
    EvaluationError,
    NavigationError,
)
from disguisebrowser.input import PRESETS, HumanProfile, Keyboard, Mouse

if TYPE_CHECKING:
    from disguisebrowser.browser import Browser

log = logging.getLogger(__name__)

WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]
World = Literal["isolated", "main"]
_LIFECYCLE_NAMES: dict[str, str] = {
    "commit": "init",
    "domcontentloaded": "DOMContentLoaded",
    "load": "load",
    "networkidle": "networkIdle",
}
_STALE_CONTEXT_MARKERS = (
    "Cannot find context with specified id",
    "Execution context was destroyed",
    "Inspected target navigated or closed",
    "Cannot find default execution context",
)

_WAIT_FOR_SELECTOR_JS = """
(selector, state, timeoutMs) => new Promise((resolve, reject) => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const css = el.checkVisibility
      ? el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })
      : true;
    return css && r.width > 0 && r.height > 0;
  };
  const check = () => {
    const el = document.querySelector(selector);
    if (!el) return null;
    if (state === 'visible' && !visible(el)) return null;
    if (state === 'hidden' && visible(el)) return null;
    return el;
  };
  const initial = check();
  if (initial) return resolve(initial);
  if (state === 'hidden' && !document.querySelector(selector)) return resolve(null);
  let timer, interval;
  const done = (fn, value) => {
    observer.disconnect(); clearTimeout(timer); clearInterval(interval); fn(value);
  };
  const observer = new MutationObserver(() => { const el = check(); if (el) done(resolve, el); });
  observer.observe(document.documentElement, {
    childList: true, subtree: true, attributes: true, characterData: true,
  });
  interval = setInterval(() => { const el = check(); if (el) done(resolve, el); }, 100);
  timer = setTimeout(() => done(reject, new Error('__disguise_timeout__')), timeoutMs);
})
"""

_SCROLL_INTO_VIEW_FALLBACK_JS = (
    "function () { this.scrollIntoView({ block: 'center', inline: 'center', "
    "behavior: 'instant' }); }"
)


class Page:
    """A browser tab driven through its own flattened CDP session."""

    def __init__(
        self,
        browser: Browser,
        session: Session,
        target_id: str,
        *,
        human: HumanProfile = PRESETS["default"],
        rng: random.Random | None = None,
    ) -> None:
        self.browser = browser
        self.session = session
        self.target_id = target_id
        self.url = "about:blank"
        self.human = human
        self._rng = rng or random.Random()
        self._main_frame_id: str | None = None
        self._world_name = "w" + secrets.token_hex(6)
        self._object_group = "g" + secrets.token_hex(6)
        self._context_id: int | None = None
        self._viewport: tuple[int, int] | None = None
        self._origin: tuple[float, float, float] | None = None
        self._closed = False
        self.dialog_action: Literal["dismiss", "accept"] = "dismiss"
        backend = browser._input_backend(self, session)
        self.mouse = Mouse(backend, profile=human, rng=self._rng, viewport=self._viewport_hint)
        self.keyboard = Keyboard(backend, profile=human, rng=self._rng)

    # ------------------------------------------------------------------ lifecycle
    async def _initialize(self) -> None:
        s = self.session
        await s.send("Page.enable")
        await s.send("Page.setLifecycleEventsEnabled", {"enabled": True})
        tree = await s.send("Page.getFrameTree")
        frame = tree["frameTree"]["frame"]
        self._main_frame_id = frame["id"]
        self.url = frame.get("url", self.url)
        s.on("Page.frameNavigated", self._on_frame_navigated)
        s.on("Page.navigatedWithinDocument", self._on_navigated_within_document)
        s.on("Page.lifecycleEvent", self._on_lifecycle)
        s.on("Page.javascriptDialogOpening", self._on_dialog)
        await s.send("Runtime.runIfWaitingForDebugger")
        # A freshly opened tab is the one the user looks at: without this,
        # document.hasFocus() stays false on a bare virtual display.
        await self.activate()

    def _on_frame_navigated(self, params: dict[str, Any]) -> None:
        frame = params.get("frame", {})
        if frame.get("id") == self._main_frame_id:
            self.url = frame.get("url", self.url)
            self._invalidate_document()

    def _on_navigated_within_document(self, params: dict[str, Any]) -> None:
        if params.get("frameId") == self._main_frame_id:
            self.url = params.get("url", self.url)

    def _on_lifecycle(self, params: dict[str, Any]) -> None:
        if params.get("frameId") == self._main_frame_id and params.get("name") == "init":
            self._invalidate_document()

    async def _on_dialog(self, params: dict[str, Any]) -> None:
        log.info(
            "dialog %s: %r → %s", params.get("type"), params.get("message"), self.dialog_action
        )
        try:
            await self.session.send(
                "Page.handleJavaScriptDialog", {"accept": self.dialog_action == "accept"}
            )
        except CdpError as exc:
            log.debug("dialog already handled: %s", exc)

    def _invalidate_document(self) -> None:
        self._context_id = None
        self._viewport = None
        self._origin = None

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.browser._close_page(self)

    # ------------------------------------------------------------------ navigation
    async def goto(
        self, url: str, *, wait_until: WaitUntil = "load", timeout: float = 30.0
    ) -> None:
        if wait_until not in _LIFECYCLE_NAMES:
            raise ValueError(f"wait_until must be one of {sorted(_LIFECYCLE_NAMES)}")
        target_name = _LIFECYCLE_NAMES[wait_until]
        buffered: list[dict[str, Any]] = []
        unsubscribe = self.session.on("Page.lifecycleEvent", buffered.append)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            result = await self.session.send(
                "Page.navigate", {"url": url, "frameId": self._main_frame_id}, timeout=timeout
            )
            if result.get("errorText"):
                raise NavigationError(f"{url}: {result['errorText']}")
            loader_id = result.get("loaderId")
            if loader_id is None:  # same-document navigation: nothing to wait for
                return

            def matches(event: dict[str, Any]) -> bool:
                return (
                    event.get("frameId") == self._main_frame_id
                    and event.get("loaderId") == loader_id
                    and event.get("name") == target_name
                )

            if any(matches(event) for event in buffered):
                return
            remaining = max(0.05, deadline - loop.time())
            await self.session.wait_for("Page.lifecycleEvent", matches, timeout=remaining)
        finally:
            unsubscribe()

    async def reload(self, *, wait_until: WaitUntil = "load", timeout: float = 30.0) -> None:
        target_name = _LIFECYCLE_NAMES[wait_until]
        future = self.session.expect(
            "Page.lifecycleEvent",
            lambda e: e.get("frameId") == self._main_frame_id and e.get("name") == target_name,
        )
        await self.session.send("Page.reload")
        await asyncio.wait_for(future, timeout)

    async def wait_for_load_state(
        self, state: WaitUntil = "load", *, timeout: float = 30.0
    ) -> None:
        if state in ("load", "domcontentloaded"):
            ready = await self.evaluate("document.readyState")
            if ready == "complete" or (state == "domcontentloaded" and ready == "interactive"):
                return
        await self.session.wait_for(
            "Page.lifecycleEvent",
            lambda e: (
                e.get("frameId") == self._main_frame_id and e.get("name") == _LIFECYCLE_NAMES[state]
            ),
            timeout=timeout,
        )

    async def wait_for_timeout(self, ms: float) -> None:
        await asyncio.sleep(ms / 1000.0)

    async def bring_to_front(self) -> None:
        await self.session.send("Page.bringToFront")
        self.browser._active_page = self

    async def activate(self) -> None:
        """Make this the visible, focused tab unless it already is.

        Real users can only interact with the tab they see; input layers call
        this before dispatching events.
        """
        if self.browser._active_page is not self:
            await self.bring_to_front()

    # ------------------------------------------------------------------ evaluation
    async def _context(self) -> int:
        if self._context_id is None:
            result = await self.session.send(
                "Page.createIsolatedWorld",
                {
                    "frameId": self._main_frame_id,
                    "worldName": self._world_name,
                    "grantUniveralAccess": False,
                },
            )
            self._context_id = int(result["executionContextId"])
        return self._context_id

    async def _runtime(
        self, method: str, params: dict[str, Any], *, world: World
    ) -> dict[str, Any]:
        context_key = "contextId" if method == "Runtime.evaluate" else "executionContextId"
        for attempt in range(2):
            request = dict(params)
            if world == "isolated" and "objectId" not in request:
                request[context_key] = await self._context()
            elif (
                world == "main" and method == "Runtime.callFunctionOn" and "objectId" not in request
            ):
                # Without Runtime.enable we never learn the main world's context id, but
                # Runtime.evaluate defaults to it: materialize the function there and
                # call it through its objectId.
                function_object = await self.session.send(
                    "Runtime.evaluate",
                    {
                        "expression": f"({request.pop('functionDeclaration')})",
                        "objectGroup": self._object_group,
                    },
                )
                if "exceptionDetails" in function_object:
                    raise EvaluationError(
                        _describe_exception(function_object["exceptionDetails"]), function_object
                    )
                request["objectId"] = function_object["result"]["objectId"]
                request["functionDeclaration"] = (
                    "function (...args) { return this.apply(undefined, args); }"
                )
            try:
                result = await self.session.send(method, request)
            except CdpError as exc:
                stale = any(marker in exc.message for marker in _STALE_CONTEXT_MARKERS)
                if attempt == 0 and stale:
                    self._invalidate_document()
                    continue
                raise
            if "exceptionDetails" in result:
                raise EvaluationError(_describe_exception(result["exceptionDetails"]), result)
            remote: dict[str, Any] = result["result"]
            return remote
        raise AssertionError("unreachable")

    async def evaluate(self, expression: str, *, world: World = "isolated") -> Any:
        """Evaluate a JavaScript *expression* and return its JSON value."""
        remote = await self._runtime(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "objectGroup": self._object_group,
            },
            world=world,
        )
        return remote.get("value")

    async def call(self, function: str, *args: Any, world: World = "isolated") -> Any:
        """Call a JavaScript *function source* (``(a, b) => …``) with JSON arguments."""
        remote = await self._runtime(
            "Runtime.callFunctionOn",
            {
                "functionDeclaration": function,
                "arguments": [{"value": arg} for arg in args],
                "returnByValue": True,
                "awaitPromise": True,
                "objectGroup": self._object_group,
            },
            world=world,
        )
        return remote.get("value")

    async def _call_handle(
        self, function: str, *args: Any, world: World = "isolated"
    ) -> ElementHandle | None:
        remote = await self._runtime(
            "Runtime.callFunctionOn",
            {
                "functionDeclaration": function,
                "arguments": [{"value": arg} for arg in args],
                "returnByValue": False,
                "awaitPromise": True,
                "objectGroup": self._object_group,
            },
            world=world,
        )
        object_id = remote.get("objectId")
        if object_id is None or remote.get("subtype") == "null":
            return None
        return ElementHandle(self, object_id)

    # ------------------------------------------------------------------ DOM
    async def query(self, selector: str) -> ElementHandle | None:
        return await self._call_handle("(s) => document.querySelector(s)", selector)

    async def query_all(self, selector: str) -> list[ElementHandle]:
        collection = await self._call_handle(
            "(s) => Array.from(document.querySelectorAll(s))", selector
        )
        return await self._unpack_array(collection)

    async def xpath(self, expression: str) -> list[ElementHandle]:
        collection = await self._call_handle(
            """(xp) => {
                const it = document.evaluate(xp, document, null,
                    XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                return Array.from({ length: it.snapshotLength }, (_, i) => it.snapshotItem(i));
            }""",
            expression,
        )
        return await self._unpack_array(collection)

    async def _unpack_array(self, collection: ElementHandle | None) -> list[ElementHandle]:
        if collection is None:
            return []
        props = await self.session.send(
            "Runtime.getProperties", {"objectId": collection.object_id, "ownProperties": True}
        )
        handles = [
            ElementHandle(self, prop["value"]["objectId"])
            for prop in props["result"]
            if prop["name"].isdigit() and prop.get("value", {}).get("objectId")
        ]
        await collection.dispose()
        return handles

    async def wait_for_selector(
        self,
        selector: str,
        *,
        timeout: float = 30.0,
        state: Literal["attached", "visible", "hidden"] = "attached",
    ) -> ElementHandle | None:
        try:
            return await self._call_handle(
                _WAIT_FOR_SELECTOR_JS, selector, state, int(timeout * 1000)
            )
        except EvaluationError as exc:
            if "__disguise_timeout__" in exc.description:
                raise TimeoutError(
                    f"selector {selector!r} did not become {state} within {timeout}s"
                ) from None
            raise

    async def content(self) -> str:
        return str(await self.evaluate("document.documentElement.outerHTML"))

    async def title(self) -> str:
        return str(await self.evaluate("document.title"))

    async def viewport(self) -> tuple[int, int]:
        if self._viewport is None:
            size = await self.evaluate("[window.innerWidth, window.innerHeight]")
            self._viewport = (int(size[0]), int(size[1]))
        return self._viewport

    def _viewport_hint(self) -> tuple[int, int]:
        return self._viewport or (1280, 720)

    async def _wait_scroll_settled(self, *, started_from: Any = None, timeout: float = 1.0) -> None:
        """Wait until Chrome's smooth-scroll animation stops moving the document.

        Wheel events are processed asynchronously and animated; element boxes
        read mid-animation would send clicks off-target. With ``started_from``
        (the position before a gesture) we first wait for the scroll to begin.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        previous = await self.evaluate("[window.scrollX, window.scrollY]")
        if started_from is not None:
            begin_deadline = loop.time() + 0.4
            while previous == started_from and loop.time() < begin_deadline:
                await asyncio.sleep(0.03)
                previous = await self.evaluate("[window.scrollX, window.scrollY]")
        while loop.time() < deadline:
            await asyncio.sleep(0.06)
            current = await self.evaluate("[window.scrollX, window.scrollY]")
            if current == previous:
                return
            previous = current

    async def _content_origin(self) -> tuple[float, float, float]:
        """Screen position of the viewport's top-left corner and the DPR.

        Used by OS-level input to translate CSS coordinates into screen pixels.
        """
        if self._origin is None:
            v = await self.evaluate(
                "[window.screenX, window.screenY, outerWidth, innerWidth, outerHeight, "
                "innerHeight, devicePixelRatio]"
            )
            sx, sy, ow, iw, oh, ih, dpr = (float(n) for n in v)
            self._origin = (sx + (ow - iw) / 2, sy + (oh - ih), dpr)
        return self._origin

    # ------------------------------------------------------------------ interaction
    async def _require(self, selector: str, timeout: float) -> ElementHandle:
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

    async def scroll(self, delta_y: float, *, humanize: bool = True) -> None:
        width, height = await self.viewport()
        x = self._rng.uniform(width * 0.3, width * 0.7)
        y = self._rng.uniform(height * 0.3, height * 0.7)
        before = await self.evaluate("[window.scrollX, window.scrollY]")
        await self.mouse.wheel(delta_y, x=x, y=y, humanize=humanize)
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
        params: dict[str, Any] = {"format": format, "captureBeyondViewport": full_page}
        if quality is not None and format != "png":
            params["quality"] = quality
        if full_page:
            size = await self.evaluate(
                "[document.documentElement.scrollWidth, document.documentElement.scrollHeight]"
            )
            params["clip"] = {"x": 0, "y": 0, "width": size[0], "height": size[1], "scale": 1}
        result = await self.session.send("Page.captureScreenshot", params, timeout=60)
        data = base64.b64decode(result["data"])
        if path is not None:
            await asyncio.to_thread(Path(path).write_bytes, data)
        return data

    async def cookies(self, *urls: str) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"urls": list(urls)} if urls else {}
        result = await self.session.send("Network.getCookies", params)
        cookies: list[dict[str, Any]] = result["cookies"]
        return cookies

    async def set_cookies(self, cookies: list[dict[str, Any]]) -> None:
        await self.session.send("Network.setCookies", {"cookies": cookies})

    async def clear_cookies(self) -> None:
        await self.session.send("Network.clearBrowserCookies")

    def __repr__(self) -> str:
        return f"<Page {self.url!r}>"


class ElementHandle:
    """A reference to a DOM element living in the page's isolated world."""

    def __init__(self, page: Page, object_id: str) -> None:
        self.page = page
        self.object_id = object_id

    async def _call(self, function: str, *args: Any, return_by_value: bool = True) -> Any:
        remote = await self.page._runtime(
            "Runtime.callFunctionOn",
            {
                "functionDeclaration": function,
                "objectId": self.object_id,
                "arguments": [{"value": arg} for arg in args],
                "returnByValue": return_by_value,
                "awaitPromise": True,
                "objectGroup": self.page._object_group,
            },
            world="isolated",
        )
        return remote.get("value") if return_by_value else remote

    async def evaluate(self, function: str, *args: Any) -> Any:
        """Call ``function`` with ``this`` bound to the element."""
        return await self._call(function, *args)

    async def text(self) -> str:
        return str(await self._call("function () { return this.innerText ?? this.textContent; }"))

    async def text_content(self) -> str:
        return str(await self._call("function () { return this.textContent; }"))

    async def inner_html(self) -> str:
        return str(await self._call("function () { return this.innerHTML; }"))

    async def outer_html(self) -> str:
        return str(await self._call("function () { return this.outerHTML; }"))

    async def attribute(self, name: str) -> str | None:
        value = await self._call("function (n) { return this.getAttribute(n); }", name)
        return None if value is None else str(value)

    async def attributes(self) -> dict[str, str]:
        result = await self._call(
            "function () { return Object.fromEntries("
            "[...this.attributes].map(a => [a.name, a.value])); }"
        )
        return dict(result or {})

    async def value(self) -> str:
        return str(await self._call("function () { return this.value ?? ''; }"))

    async def bounding_box(self) -> dict[str, float] | None:
        box = await self._call(
            "function () { const r = this.getBoundingClientRect(); "
            "return { x: r.x, y: r.y, width: r.width, height: r.height }; }"
        )
        if not box or box["width"] <= 0 or box["height"] <= 0:
            return None
        return {k: float(v) for k, v in box.items()}

    async def is_visible(self) -> bool:
        return bool(
            await self._call(
                "function () { const r = this.getBoundingClientRect(); "
                "const css = this.checkVisibility ? this.checkVisibility({ checkOpacity: true, "
                "checkVisibilityCSS: true }) : true; return css && r.width > 0 && r.height > 0; }"
            )
        )

    async def query(self, selector: str) -> ElementHandle | None:
        remote = await self._call(
            "function (s) { return this.querySelector(s); }", selector, return_by_value=False
        )
        object_id = remote.get("objectId")
        if object_id is None or remote.get("subtype") == "null":
            return None
        return ElementHandle(self.page, object_id)

    async def query_all(self, selector: str) -> list[ElementHandle]:
        remote = await self._call(
            "function (s) { return Array.from(this.querySelectorAll(s)); }",
            selector,
            return_by_value=False,
        )
        collection = (
            ElementHandle(self.page, remote["objectId"]) if remote.get("objectId") else None
        )
        return await self.page._unpack_array(collection)

    async def scroll_into_view(self, *, humanize: bool = True) -> dict[str, float]:
        """Bring the element into the viewport with wheel gestures; returns its box."""
        for _ in range(8):
            await self.page._wait_scroll_settled()
            box = await self.bounding_box()
            if box is None:
                break
            width, height = await self.page.viewport()
            center_y = box["y"] + box["height"] / 2
            center_x = box["x"] + box["width"] / 2
            if 0.08 * height <= center_y <= 0.92 * height and 0 <= center_x <= width:
                return box
            if not humanize:
                break
            delta = max(-900.0, min(900.0, center_y - height / 2))
            if abs(delta) < 1:
                break
            await self.page.scroll(delta, humanize=True)
            await asyncio.sleep(self.page._rng.uniform(0.08, 0.2))
        await self._call(_SCROLL_INTO_VIEW_FALLBACK_JS)
        await self.page._wait_scroll_settled()
        box = await self.bounding_box()
        if box is None:
            raise ElementNotFoundError("element has no layout box (hidden or detached)")
        return box

    def _aim(self, box: dict[str, float]) -> tuple[float, float]:
        """Point inside the box, normally distributed around its center."""
        rng = self.page._rng
        x = box["x"] + box["width"] / 2 + rng.gauss(0, box["width"] / 6)
        y = box["y"] + box["height"] / 2 + rng.gauss(0, box["height"] / 6)
        x = min(max(x, box["x"] + 2), box["x"] + box["width"] - 2)
        y = min(max(y, box["y"] + 2), box["y"] + box["height"] - 2)
        return x, y

    async def hover(self, *, humanize: bool = True) -> None:
        box = await self.scroll_into_view(humanize=humanize)
        x, y = self._aim(box)
        await self.page.mouse.move(
            x, y, humanize=humanize, target_width=min(box["width"], box["height"])
        )

    async def click(
        self, *, humanize: bool = True, button: Literal["left", "right", "middle"] = "left"
    ) -> None:
        box = await self.scroll_into_view(humanize=humanize)
        x, y = self._aim(box)
        await self.page.mouse.click(
            x, y, button=button, humanize=humanize, target_width=min(box["width"], box["height"])
        )

    async def type(self, text: str, *, humanize: bool = True, clear: bool = False) -> None:
        await self.click(humanize=humanize)
        if clear:
            await self.page.keyboard.down("Control")
            await self.page.keyboard.press("a", humanize=humanize)
            await self.page.keyboard.up("Control")
            await self.page.keyboard.press("Backspace", humanize=humanize)
        await self.page.keyboard.type(text, humanize=humanize)

    async def dispose(self) -> None:
        with contextlib.suppress(CdpError):
            await self.page.session.send("Runtime.releaseObject", {"objectId": self.object_id})

    def __repr__(self) -> str:
        return f"<ElementHandle {self.object_id}>"


def _describe_exception(details: dict[str, Any]) -> str:
    exception = details.get("exception") or {}
    description = exception.get("description") or details.get("text") or "JavaScript error"
    if isinstance(description, str):
        return description.splitlines()[0] if description else "JavaScript error"
    return json.dumps(description)
