"""US-layout key definitions for CDP ``Input.dispatchKeyEvent``."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KeyDefinition:
    key: str
    code: str
    key_code: int
    text: str = ""
    shift: bool = False
    location: int = 0


_LETTERS = {chr(c): (f"Key{chr(c).upper()}", ord(chr(c).upper())) for c in range(97, 123)}
_DIGITS = {str(d): (f"Digit{d}", 48 + d) for d in range(10)}
_DIGIT_SHIFT = dict(zip("!@#$%^&*()", "1234567890", strict=True))
_PUNCTUATION: dict[str, tuple[str, int, str]] = {
    "`": ("Backquote", 192, "~"),
    "-": ("Minus", 189, "_"),
    "=": ("Equal", 187, "+"),
    "[": ("BracketLeft", 219, "{"),
    "]": ("BracketRight", 221, "}"),
    "\\": ("Backslash", 220, "|"),
    ";": ("Semicolon", 186, ":"),
    "'": ("Quote", 222, '"'),
    ",": ("Comma", 188, "<"),
    ".": ("Period", 190, ">"),
    "/": ("Slash", 191, "?"),
}

NAMED_KEYS: dict[str, KeyDefinition] = {
    "Enter": KeyDefinition("Enter", "Enter", 13, "\r"),
    "Tab": KeyDefinition("Tab", "Tab", 9, "\t"),
    "Space": KeyDefinition(" ", "Space", 32, " "),
    "Backspace": KeyDefinition("Backspace", "Backspace", 8),
    "Delete": KeyDefinition("Delete", "Delete", 46),
    "Escape": KeyDefinition("Escape", "Escape", 27),
    "ArrowLeft": KeyDefinition("ArrowLeft", "ArrowLeft", 37),
    "ArrowUp": KeyDefinition("ArrowUp", "ArrowUp", 38),
    "ArrowRight": KeyDefinition("ArrowRight", "ArrowRight", 39),
    "ArrowDown": KeyDefinition("ArrowDown", "ArrowDown", 40),
    "Home": KeyDefinition("Home", "Home", 36),
    "End": KeyDefinition("End", "End", 35),
    "PageUp": KeyDefinition("PageUp", "PageUp", 33),
    "PageDown": KeyDefinition("PageDown", "PageDown", 34),
    "Shift": KeyDefinition("Shift", "ShiftLeft", 16, location=1),
    "Control": KeyDefinition("Control", "ControlLeft", 17, location=1),
    "Alt": KeyDefinition("Alt", "AltLeft", 18, location=1),
    "Meta": KeyDefinition("Meta", "MetaLeft", 91, location=1),
    "F5": KeyDefinition("F5", "F5", 116),
}

MODIFIER_SHIFT = 8
MODIFIER_CTRL = 2
MODIFIER_ALT = 1
MODIFIER_META = 4


def definition_for_char(char: str) -> KeyDefinition | None:  # noqa: PLR0911 — a lookup table
    """Key definition producing ``char`` on a US keyboard, or ``None`` if not typeable."""
    if char in _LETTERS:
        code, key_code = _LETTERS[char]
        return KeyDefinition(char, code, key_code, char)
    lower = char.lower()
    if char.isupper() and lower in _LETTERS:
        code, key_code = _LETTERS[lower]
        return KeyDefinition(char, code, key_code, char, shift=True)
    if char in _DIGITS:
        code, key_code = _DIGITS[char]
        return KeyDefinition(char, code, key_code, char)
    if char in _DIGIT_SHIFT:
        code, key_code = _DIGITS[_DIGIT_SHIFT[char]]
        return KeyDefinition(char, code, key_code, char, shift=True)
    if char in _PUNCTUATION:
        code, key_code, _ = _PUNCTUATION[char]
        return KeyDefinition(char, code, key_code, char)
    for code, key_code, shifted in _PUNCTUATION.values():
        if char == shifted:
            return KeyDefinition(char, code, key_code, char, shift=True)
    if char == " ":
        return NAMED_KEYS["Space"]
    if char == "\n":
        return NAMED_KEYS["Enter"]
    if char == "\t":
        return NAMED_KEYS["Tab"]
    return None


def definition_for_name(name: str) -> KeyDefinition:
    if name in NAMED_KEYS:
        return NAMED_KEYS[name]
    if len(name) == 1:
        definition = definition_for_char(name)
        if definition is not None:
            return definition
    raise ValueError(f"unknown key {name!r}")
