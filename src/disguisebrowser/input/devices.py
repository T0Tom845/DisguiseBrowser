"""Mouse and keyboard driven through CDP ``Input.*`` with humanized timing.

CDP-dispatched events are *trusted* (``isTrusted === true``) — the browser
generates them itself. What this layer adds is realistic kinematics; what it
cannot fix is the known Chrome artifact ``screenX == pageX`` for CDP events
(crbug 40280325), which phase 2 addresses with OS-level XTest input.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from typing import Literal

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


async def _sleep_ms(ms: float) -> None:
    if ms > 0:
        await asyncio.sleep(ms / 1000.0)


class Mouse:
    def __init__(
        self,
        session: Session,
        *,
        profile: HumanProfile = PRESETS["default"],
        rng: random.Random | None = None,
        viewport: Callable[[], tuple[int, int]] | None = None,
    ) -> None:
        self._session = session
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

    async def _dispatch(self, event_type: str, x: float, y: float, **extra: object) -> None:
        params: dict[str, object] = {
            "type": event_type,
            "x": round(x, 2),
            "y": round(y, 2),
            "buttons": self._buttons,
            "pointerType": "mouse",
            **extra,
        }
        await self._session.send("Input.dispatchMouseEvent", params)

    async def move(
        self, x: float, y: float, *, humanize: bool = True, target_width: float = 24.0
    ) -> None:
        if not humanize:
            await self._dispatch("mouseMoved", x, y, button="none")
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
            await self._dispatch("mouseMoved", point.x, point.y, button="none")
            self.position = (point.x, point.y)
        self.position = (x, y)

    async def down(self, button: Button = "left", *, click_count: int = 1) -> None:
        x, y = self.position or self._origin()
        self._buttons |= _BUTTON_BITS[button]
        await self._dispatch("mousePressed", x, y, button=button, clickCount=click_count)

    async def up(self, button: Button = "left", *, click_count: int = 1) -> None:
        x, y = self.position or self._origin()
        self._buttons &= ~_BUTTON_BITS[button]
        await self._dispatch("mouseReleased", x, y, button=button, clickCount=click_count)

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
        self.position = (x, y)
        if not humanize:
            await self._dispatch("mouseWheel", x, y, deltaX=0, deltaY=delta_y)
            return
        for chunk, pause in scroll_steps(delta_y, rng=self._rng, profile=self.profile):
            await self._dispatch("mouseWheel", x, y, deltaX=0, deltaY=chunk)
            await _sleep_ms(pause)


class Keyboard:
    def __init__(
        self,
        session: Session,
        *,
        profile: HumanProfile = PRESETS["default"],
        rng: random.Random | None = None,
    ) -> None:
        self._session = session
        self.profile = profile
        self._rng = rng or random.Random()
        self._modifiers = 0

    async def _key_event(self, event_type: str, definition: keymap.KeyDefinition) -> None:
        params: dict[str, object] = {
            "type": event_type,
            "key": definition.key,
            "code": definition.code,
            "windowsVirtualKeyCode": definition.key_code,
            "nativeVirtualKeyCode": definition.key_code,
            "modifiers": self._modifiers,
            "location": definition.location,
        }
        if event_type == "keyDown" and definition.text:
            params["text"] = definition.text
            params["unmodifiedText"] = definition.text.lower()
        await self._session.send("Input.dispatchKeyEvent", params)

    async def down(self, key: str) -> None:
        definition = keymap.definition_for_name(key)
        if key == "Shift":
            self._modifiers |= keymap.MODIFIER_SHIFT
        elif key == "Control":
            self._modifiers |= keymap.MODIFIER_CTRL
        elif key == "Alt":
            self._modifiers |= keymap.MODIFIER_ALT
        elif key == "Meta":
            self._modifiers |= keymap.MODIFIER_META
        await self._key_event("keyDown", definition)

    async def up(self, key: str) -> None:
        definition = keymap.definition_for_name(key)
        if key == "Shift":
            self._modifiers &= ~keymap.MODIFIER_SHIFT
        elif key == "Control":
            self._modifiers &= ~keymap.MODIFIER_CTRL
        elif key == "Alt":
            self._modifiers &= ~keymap.MODIFIER_ALT
        elif key == "Meta":
            self._modifiers &= ~keymap.MODIFIER_META
        await self._key_event("keyUp", definition)

    async def press(self, key: str, *, humanize: bool = True) -> None:
        """Press and release a named key (``Enter``, ``Tab``, ``ArrowDown`` …) or a character."""
        definition = keymap.definition_for_name(key)
        if definition.shift:
            await self.down("Shift")
        await self._key_event("keyDown", definition)
        await _sleep_ms(self._rng.uniform(35.0, 110.0) if humanize else 5.0)
        await self._key_event("keyUp", definition)
        if definition.shift:
            await self.up("Shift")

    async def insert_text(self, text: str) -> None:
        """Commit text the way an IME does (no key events). Used for non-US characters."""
        await self._session.send("Input.insertText", {"text": text})

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
