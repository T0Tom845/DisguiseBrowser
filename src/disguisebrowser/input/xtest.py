"""OS-level input through the X server's XTEST extension.

Events synthesized this way enter Chrome through the same path as a physical
mouse and keyboard: ``screenX/screenY`` are real screen coordinates,
``movementX/Y`` and timestamps come from the X server, and there is no CDP
``Input.*`` traffic at all. Requires an X display (our virtual desktop or a
host display); non-US characters still go through ``Input.insertText``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from Xlib import XK, X
from Xlib import display as xlib_display
from Xlib.ext import xtest

from disguisebrowser.cdp import Session
from disguisebrowser.errors import DisguiseError
from disguisebrowser.input import keys as keymap
from disguisebrowser.input.humanizer import WHEEL_NOTCH_PX

if TYPE_CHECKING:
    from disguisebrowser.page import Page

log = logging.getLogger(__name__)

Button = Literal["left", "right", "middle"]
_X_BUTTONS: dict[Button, int] = {"left": 1, "middle": 2, "right": 3}
_WHEEL_UP, _WHEEL_DOWN = 4, 5

# DOM ``code`` (physical key) → X keysym name of the key's unshifted level.
# Shift is pressed separately by ``Keyboard`` for shifted characters.
_CODE_KEYSYMS: dict[str, str] = {
    "Enter": "Return",
    "Tab": "Tab",
    "Space": "space",
    "Backspace": "BackSpace",
    "Delete": "Delete",
    "Escape": "Escape",
    "ArrowLeft": "Left",
    "ArrowUp": "Up",
    "ArrowRight": "Right",
    "ArrowDown": "Down",
    "Home": "Home",
    "End": "End",
    "PageUp": "Prior",
    "PageDown": "Next",
    "ShiftLeft": "Shift_L",
    "ControlLeft": "Control_L",
    "AltLeft": "Alt_L",
    "MetaLeft": "Super_L",
    "F5": "F5",
    "Backquote": "grave",
    "Minus": "minus",
    "Equal": "equal",
    "BracketLeft": "bracketleft",
    "BracketRight": "bracketright",
    "Backslash": "backslash",
    "Semicolon": "semicolon",
    "Quote": "apostrophe",
    "Comma": "comma",
    "Period": "period",
    "Slash": "slash",
}


class XTestInputBackend:
    """Delivers mouse/keyboard events to the display a page's window lives on."""

    def __init__(self, display_name: str, page: Page, session: Session) -> None:
        self.display_name = display_name
        self._page = page
        self._session = session
        self._conn: xlib_display.Display | None = None

    # ------------------------------------------------------------------ X plumbing
    def _x(self) -> xlib_display.Display:
        if self._conn is None:
            conn = xlib_display.Display(self.display_name)
            if not conn.has_extension("XTEST"):
                conn.close()
                raise DisguiseError(f"display {self.display_name} lacks the XTEST extension")
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    async def _screen_point(self, x: float, y: float) -> tuple[int, int]:
        """CSS viewport coordinates → X screen pixels."""
        await self._page.activate()
        origin_x, origin_y, dpr = await self._page._content_origin()
        return round(origin_x + x * dpr), round(origin_y + y * dpr)

    def _fake(self, event: int, detail: int = 0, *, x: int = 0, y: int = 0) -> None:
        conn = self._x()
        xtest.fake_input(conn, event, detail, x=x, y=y)
        conn.sync()

    # ------------------------------------------------------------------ backend API
    async def mouse_move(self, x: float, y: float, buttons: int) -> None:
        sx, sy = await self._screen_point(x, y)
        self._fake(X.MotionNotify, x=sx, y=sy)

    async def mouse_button(
        self, pressed: bool, button: Button, *, x: float, y: float, buttons: int, click_count: int
    ) -> None:
        sx, sy = await self._screen_point(x, y)
        self._fake(X.MotionNotify, x=sx, y=sy)
        self._fake(X.ButtonPress if pressed else X.ButtonRelease, _X_BUTTONS[button])

    async def mouse_wheel(self, x: float, y: float, delta_y: float) -> None:
        sx, sy = await self._screen_point(x, y)
        self._fake(X.MotionNotify, x=sx, y=sy)
        notches = max(1, round(abs(delta_y) / WHEEL_NOTCH_PX))
        button = _WHEEL_DOWN if delta_y > 0 else _WHEEL_UP
        for _ in range(notches):
            self._fake(X.ButtonPress, button)
            self._fake(X.ButtonRelease, button)

    async def key(self, pressed: bool, definition: keymap.KeyDefinition, modifiers: int) -> None:
        await self._page.activate()
        self._fake(X.KeyPress if pressed else X.KeyRelease, self._keycode_for(definition))

    async def insert_text(self, text: str) -> None:
        await self._page.activate()
        await self._session.send("Input.insertText", {"text": text})

    # ------------------------------------------------------------------ helpers
    def _keycode_for(self, definition: keymap.KeyDefinition) -> int:
        conn = self._x()
        code = definition.code
        if code in _CODE_KEYSYMS:
            keysym = XK.string_to_keysym(_CODE_KEYSYMS[code])
        elif code.startswith("Key") and len(code) == 4:
            keysym = ord(code[3].lower())  # Latin-1 keysyms equal the code point
        elif code.startswith("Digit") and len(code) == 6:
            keysym = ord(code[5])
        else:
            raise DisguiseError(f"no X keysym mapping for key code {code!r}")
        keycode = conn.keysym_to_keycode(keysym)
        if not keycode:
            raise DisguiseError(f"key {code!r} (keysym {keysym:#x}) is not on the X keyboard map")
        return int(keycode)
