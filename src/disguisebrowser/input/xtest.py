"""OS-level input through the X server's XTEST extension.

Events synthesized this way enter Chrome through the same path as a physical
mouse and keyboard: ``screenX/screenY`` are real screen coordinates,
``movementX/Y`` and timestamps come from the X server, and there is no CDP
``Input.*`` traffic at all. Requires an X display (our virtual desktop or a
host display).

On a virtual display this process owns, Cyrillic is typed by locking the XKB
group to ЙЦУКЕН (``XkbLockGroup`` — no Alt+Shift key events the page could
see) and pressing the physical key. Anywhere else, characters that are not on
the active group fall back to ``Input.insertText``.
"""

from __future__ import annotations

import ctypes
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
# Modifiers share a keycode across groups; switching group while they are down
# would race the letter that follows, so they never change the locked group.
_MODIFIER_CODES = frozenset({"ShiftLeft", "ControlLeft", "AltLeft", "MetaLeft"})
_XKB_USE_CORE_KBD = 0x0100
# Path points outside the viewport produce no event; give up after a few.
_CALIBRATION_ATTEMPTS = 6

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


class _XkbGroupLock:
    """Locks the core keyboard's XKB group without synthesizing a key chord.

    python-xlib speaks the X protocol itself and does not expose ``XkbLockGroup``,
    so this uses libX11 on its own connection to the same display.
    """

    def __init__(self, display_name: str) -> None:
        self._lib = ctypes.CDLL("libX11.so.6")
        self._lib.XOpenDisplay.restype = ctypes.c_void_p
        self._lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self._lib.XkbLockGroup.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
        self._lib.XkbLockGroup.restype = ctypes.c_int
        self._lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self._lib.XSync.restype = ctypes.c_int
        self._lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self._dpy = self._lib.XOpenDisplay(display_name.encode())
        if not self._dpy:
            raise DisguiseError(f"XOpenDisplay({display_name}) failed while switching layout")

    def lock(self, group: int) -> None:
        if not self._lib.XkbLockGroup(self._dpy, _XKB_USE_CORE_KBD, group):
            raise DisguiseError(f"XkbLockGroup({group}) failed")
        # Round-trip so the group is in effect before the next XTest event,
        # which travels on the python-xlib connection.
        self._lib.XSync(self._dpy, 0)

    def close(self) -> None:
        if self._dpy:
            self._lib.XCloseDisplay(self._dpy)
            self._dpy = 0


class XTestInputBackend:
    """Delivers mouse/keyboard events to the display a page's window lives on."""

    def __init__(
        self, display_name: str, page: Page, session: Session, *, owns_layout: bool = False
    ) -> None:
        self.display_name = display_name
        self.owns_layout = owns_layout
        self._page = page
        self._session = session
        self._conn: xlib_display.Display | None = None
        self._xkb: _XkbGroupLock | None = None
        self._group = 0

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
        if self._xkb is not None:
            self._xkb.close()
            self._xkb = None

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
        page = self._page
        if page._origin_calibrated or page._calibration_attempts >= _CALIBRATION_ATTEMPTS:
            self._fake(X.MotionNotify, x=sx, y=sy)
            return
        # First moves on this document: learn the real viewport origin from the
        # event Chrome receives instead of trusting window geometry (window
        # managers add frames the geometry estimate cannot see).
        page._calibration_attempts += 1
        try:
            await page._install_pointer_probe()
            self._fake(X.MotionNotify, x=sx, y=sy)
            await page._calibrate_origin((sx, sy))
        except DisguiseError as exc:
            log.debug("pointer calibration failed: %s", exc)
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
        try:
            keycode = self._keycode_for(definition)
        except DisguiseError:
            if definition.text:
                # Press commits the character; the matching release must not commit it again.
                if pressed:
                    log.debug("XTest cannot type %r; using insertText", definition.text)
                    await self.insert_text(definition.text)
                return
            raise
        self._fake(X.KeyPress if pressed else X.KeyRelease, keycode)

    async def insert_text(self, text: str) -> None:
        await self._page.activate()
        await self._session.send("Input.insertText", {"text": text})

    # ------------------------------------------------------------------ helpers
    def _keycode_for(self, definition: keymap.KeyDefinition) -> int:
        keycode = self._physical_keycode(definition)
        if definition.code not in _MODIFIER_CODES:
            self._ensure_group(definition, keycode)
        return keycode

    def _physical_keycode(self, definition: keymap.KeyDefinition) -> int:
        """Keycode of the physical key, looked up via its US-group keysym."""
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

    def _ensure_group(self, definition: keymap.KeyDefinition, keycode: int) -> None:
        """Lock the XKB group this key belongs to, on a display we own."""
        group = definition.group
        if group and not self.owns_layout:
            raise DisguiseError(
                f"{definition.text!r} needs the ЙЦУКЕН group; it is switched only on a "
                "virtual display this process owns"
            )
        expected = keymap.level0_keysym(definition)
        if expected is not None and not self._keycode_has_keysym(keycode, expected):
            raise DisguiseError(
                f"display {self.display_name} is not mapped to ЙЦУКЕН "
                f"({definition.text!r}: keysym {expected:#x} is not on keycode {keycode})"
            )
        if group == self._group:
            return
        if self._xkb is None:
            self._xkb = _XkbGroupLock(self.display_name)
        self._xkb.lock(group)
        self._group = group

    def _keycode_has_keysym(self, keycode: int, expected: int) -> bool:
        """True if ``expected`` sits on any level/group of this physical key.

        ``us,ru`` puts two or four levels in each group, so the core-protocol
        index of the Cyrillic keysym is not a fixed ``group * 2``.
        """
        conn = self._x()
        for index in range(8):
            try:
                if int(conn.keycode_to_keysym(keycode, index)) == expected:
                    return True
            except (IndexError, TypeError):
                return False
        return False
