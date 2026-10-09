"""Mouse and keyboard with humanized timing over a pluggable event backend.

Two backends exist:

* :class:`CdpInputBackend` — DevTools ``Input.*``. Events are trusted
  (``isTrusted === true``) but carry Chrome's known artifact
  ``screenX == pageX`` (crbug 40280325) that Cloudflare Turnstile checks.
* ``XTestInputBackend`` (``input/xtest.py``) — synthesizes events in the X
  server, so Chrome receives them exactly like from a physical device.

The kinematics (paths, timing) live in ``humanizer.py`` and are shared.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from typing import ClassVar, Literal, Protocol

from disguisebrowser.cdp import Session
from disguisebrowser.input import keys as keymap
from disguisebrowser.input.humanizer import (
    PRESETS,
    HumanProfile,
    mouse_path,
    scroll_steps,
    typing_delays,
)

Button = Literal["left", "right", "middle"]
_BUTTON_BITS: dict[Button, int] = {"left": 1, "right": 2, "middle": 4}


class InputBackend(Protocol):
    async def mouse_move(self, x: float, y: float, buttons: int) -> None: ...

    async def mouse_button(
        self, pressed: bool, button: Button, *, x: float, y: float, buttons: int, click_count: int
    ) -> None: ...

    async def mouse_wheel(self, x: float, y: float, delta_y: float) -> None: ...

    async def key(
        self, pressed: bool, definition: keymap.KeyDefinition, modifiers: int
    ) -> None: ...

    async def insert_text(self, text: str) -> None: ...


class CdpInputBackend:
    def __init__(self, session: Session) -> None:
        self._session = session

    async def _mouse(self, event_type: str, x: float, y: float, **extra: object) -> None:
        await self._session.send(
            "Input.dispatchMouseEvent",
            {
                "type": event_type,
                "x": round(x, 2),
                "y": round(y, 2),
                "pointerType": "mouse",
                **extra,
            },
        )

    async def mouse_move(self, x: float, y: float, buttons: int) -> None:
        await self._mouse("mouseMoved", x, y, button="none", buttons=buttons)

    async def mouse_button(
        self, pressed: bool, button: Button, *, x: float, y: float, buttons: int, click_count: int
    ) -> None:
        await self._mouse(
            "mousePressed" if pressed else "mouseReleased",
            x, y, button=button, buttons=buttons, clickCount=click_count,
        )  # fmt: skip

    async def mouse_wheel(self, x: float, y: float, delta_y: float) -> None:
        await self._mouse("mouseWheel", x, y, deltaX=0, deltaY=delta_y, buttons=0)

    async def key(self, pressed: bool, definition: keymap.KeyDefinition, modifiers: int) -> None:
        params: dict[str, object] = {
            "type": "keyDown" if pressed else "keyUp",
            "key": definition.key,
            "code": definition.code,
            "windowsVirtualKeyCode": definition.key_code,
            "nativeVirtualKeyCode": definition.key_code,
            "modifiers": modifiers,
            "location": definition.location,
        }
        if pressed and definition.text:
            params["text"] = definition.text
            params["unmodifiedText"] = definition.text.lower()
        await self._session.send("Input.dispatchKeyEvent", params)

    async def insert_text(self, text: str) -> None:
        await self._session.send("Input.insertText", {"text": text})


async def _sleep_ms(ms: float) -> None:
    if ms > 0:
        await asyncio.sleep(ms / 1000.0)


class Mouse:
    def __init__(
        self,
        backend: InputBackend,
        *,
        profile: HumanProfile = PRESETS["default"],
        rng: random.Random | None = None,
        viewport: Callable[[], tuple[int, int]] | None = None,
    ) -> None:
        self._backend = backend
        self.profile = profile
        self._rng = rng or random.Random()
        self._viewport = viewport
        self.position: tuple[float, float] | None = None
        self._buttons = 0

    def _origin(self) -> tuple[float, float]:
        """Where the cursor is, or a plausible entry point if we never moved it."""
        if self.position is not None:
            return self.position
        width, height = self._viewport() if self._viewport else (1280, 720)
        # Cursors enter a window from an edge, most often from the top/sides.
        edge = self._rng.random()
        if edge < 0.4:
            return (self._rng.uniform(0, width), 0.0)
        if edge < 0.7:
            return (0.0, self._rng.uniform(0, height))
        return (float(width), self._rng.uniform(0, height))

    async def move(
        self, x: float, y: float, *, humanize: bool = True, target_width: float = 24.0
    ) -> None:
        if not humanize:
            await self._backend.mouse_move(x, y, self._buttons)
            self.position = (x, y)
            return
        start = self._origin()
        path = mouse_path(
            start, (x, y), rng=self._rng, profile=self.profile, target_width=target_width
        )
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        for point in path:
            delay = t0 + point.t / 1000.0 - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            await self._backend.mouse_move(point.x, point.y, self._buttons)
            self.position = (point.x, point.y)
        self.position = (x, y)

    async def down(self, button: Button = "left", *, click_count: int = 1) -> None:
        x, y = self.position or self._origin()
        self._buttons |= _BUTTON_BITS[button]
        await self._backend.mouse_button(
            True, button, x=x, y=y, buttons=self._buttons, click_count=click_count
        )

    async def up(self, button: Button = "left", *, click_count: int = 1) -> None:
        x, y = self.position or self._origin()
        self._buttons &= ~_BUTTON_BITS[button]
        await self._backend.mouse_button(
            False, button, x=x, y=y, buttons=self._buttons, click_count=click_count
        )

    async def click(
        self,
        x: float,
        y: float,
        *,
        button: Button = "left",
        humanize: bool = True,
        target_width: float = 24.0,
    ) -> None:
        await self.move(x, y, humanize=humanize, target_width=target_width)
        if humanize:
            await _sleep_ms(self._rng.uniform(40.0, 140.0))
        await self.down(button)
        await _sleep_ms(self._rng.uniform(55.0, 130.0) if humanize else 10.0)
        await self.up(button)

    async def wheel(
        self,
        delta_y: float,
        *,
        x: float | None = None,
        y: float | None = None,
        humanize: bool = True,
    ) -> None:
        if x is None or y is None:
            px, py = self.position or self._origin()
            x = px if x is None else x
            y = py if y is None else y
        if self.position != (x, y):
            await self.move(x, y, humanize=humanize)
        if not humanize:
            await self._backend.mouse_wheel(x, y, delta_y)
            return
        for chunk, pause in scroll_steps(delta_y, rng=self._rng, profile=self.profile):
            await self._backend.mouse_wheel(x, y, chunk)
            await _sleep_ms(pause)


class Keyboard:
    def __init__(
        self,
        backend: InputBackend,
        *,
        profile: HumanProfile = PRESETS["default"],
        rng: random.Random | None = None,
    ) -> None:
        self._backend = backend
        self.profile = profile
        self._rng = rng or random.Random()
        self._modifiers = 0

    _MODIFIER_BITS: ClassVar[dict[str, int]] = {
        "Shift": keymap.MODIFIER_SHIFT,
        "Control": keymap.MODIFIER_CTRL,
        "Alt": keymap.MODIFIER_ALT,
        "Meta": keymap.MODIFIER_META,
    }

    async def down(self, key: str) -> None:
        definition = keymap.definition_for_name(key)
        self._modifiers |= self._MODIFIER_BITS.get(key, 0)
        await self._backend.key(True, definition, self._modifiers)

    async def up(self, key: str) -> None:
        definition = keymap.definition_for_name(key)
        self._modifiers &= ~self._MODIFIER_BITS.get(key, 0)
        await self._backend.key(False, definition, self._modifiers)

    async def press(self, key: str, *, humanize: bool = True) -> None:
        """Press and release a named key (``Enter``, ``Tab``, ``ArrowDown`` …) or a character."""
        definition = keymap.definition_for_name(key)
        if definition.shift:
            await self.down("Shift")
        await self._backend.key(True, definition, self._modifiers)
        await _sleep_ms(self._rng.uniform(35.0, 110.0) if humanize else 5.0)
        await self._backend.key(False, definition, self._modifiers)
        if definition.shift:
            await self.up("Shift")

    async def insert_text(self, text: str) -> None:
        """Commit text the way an IME does (no key events). Used for non-US characters."""
        await self._backend.insert_text(text)

    async def type(self, text: str, *, humanize: bool = True) -> None:
        """Type ``text`` character by character with human inter-key timing.

        Characters without a US-layout key (e.g. Cyrillic) are committed through
        ``Input.insertText`` — the same path an IME or a non-US layout takes.
        """
        delays = typing_delays(text, rng=self._rng, profile=self.profile)
        for char, delay in zip(text, delays, strict=True):
            await _sleep_ms(delay if humanize else 0)
            if keymap.definition_for_char(char) is None:
                await self.insert_text(char)
            else:
                await self.press(char, humanize=humanize)
