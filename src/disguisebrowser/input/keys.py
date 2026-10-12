"""Key definitions for CDP ``Input.dispatchKeyEvent`` and XTest.

The default layout is US. ``layout="us,ru"`` additionally maps ЙЦУКЕН: Cyrillic
letters and ``№`` are real key presses on the Russian XKB group (``group=1``),
while ASCII stays on the US group so mixed text does not depend on a group
switch for characters both layouts can produce.
"""

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
    # XKB group: 0 is the US layout, 1 is ЙЦУКЕН when the display loaded ``us,ru``.
    group: int = 0


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


# Physical key → ЙЦУКЕН letter. Windows virtual-key codes match the US key that
# shares the position (KeyQ is 81 whether it produces "q" or "й").
_RU_LETTERS: tuple[tuple[str, str, str, int], ...] = (
    ("й", "Й", "KeyQ", 81),
    ("ц", "Ц", "KeyW", 87),
    ("у", "У", "KeyE", 69),
    ("к", "К", "KeyR", 82),
    ("е", "Е", "KeyT", 84),
    ("н", "Н", "KeyY", 89),
    ("г", "Г", "KeyU", 85),
    ("ш", "Ш", "KeyI", 73),
    ("щ", "Щ", "KeyO", 79),
    ("з", "З", "KeyP", 80),
    ("х", "Х", "BracketLeft", 219),
    ("ъ", "Ъ", "BracketRight", 221),
    ("ф", "Ф", "KeyA", 65),
    ("ы", "Ы", "KeyS", 83),
    ("в", "В", "KeyD", 68),
    ("а", "А", "KeyF", 70),
    ("п", "П", "KeyG", 71),
    ("р", "Р", "KeyH", 72),
    ("о", "О", "KeyJ", 74),
    ("л", "Л", "KeyK", 75),
    ("д", "Д", "KeyL", 76),
    ("ж", "Ж", "Semicolon", 186),
    ("э", "Э", "Quote", 222),
    ("я", "Я", "KeyZ", 90),
    ("ч", "Ч", "KeyX", 88),
    ("с", "С", "KeyC", 67),
    ("м", "М", "KeyV", 86),
    ("и", "И", "KeyB", 66),
    ("т", "Т", "KeyN", 78),
    ("ь", "Ь", "KeyM", 77),
    ("б", "Б", "Comma", 188),
    ("ю", "Ю", "Period", 190),
    ("ё", "Ё", "Backquote", 192),
)


def _ru_chars() -> dict[str, tuple[str, int, bool]]:
    table: dict[str, tuple[str, int, bool]] = {}
    for lower, upper, code, key_code in _RU_LETTERS:
        table[lower] = (code, key_code, False)
        table[upper] = (code, key_code, True)
    table["№"] = ("Digit3", 51, True)
    return table


_RU_CHARS = _ru_chars()

# X11 keysyms from keysymdef.h (lowercase). Uppercase is Shift on the same key.
CYRILLIC_KEYSYMS: dict[str, int] = {
    "а": 0x06C1,
    "б": 0x06C2,
    "в": 0x06D7,
    "г": 0x06C7,
    "д": 0x06C4,
    "е": 0x06C5,
    "ё": 0x06A3,
    "ж": 0x06D6,
    "з": 0x06DA,
    "и": 0x06C9,
    "й": 0x06CA,
    "к": 0x06CB,
    "л": 0x06CC,
    "м": 0x06CD,
    "н": 0x06CE,
    "о": 0x06CF,
    "п": 0x06D0,
    "р": 0x06D2,
    "с": 0x06D3,
    "т": 0x06D4,
    "у": 0x06D5,
    "ф": 0x06C6,
    "х": 0x06C8,
    "ц": 0x06C3,
    "ч": 0x06DE,
    "ш": 0x06DB,
    "щ": 0x06DD,
    "ъ": 0x06DF,
    "ы": 0x06D9,
    "ь": 0x06D8,
    "э": 0x06DC,
    "ю": 0x06C0,
    "я": 0x06D1,
}

KEYBOARD_LAYOUTS = ("us", "us,ru")


def layout_has_ru(layout: str) -> bool:
    return "ru" in {part.strip() for part in layout.split(",")}


def definition_for_char(char: str, *, layout: str = "us") -> KeyDefinition | None:
    """Key definition producing ``char``, or ``None`` if it is not a key on ``layout``.

    ASCII always comes from the US group. Cyrillic and ``№`` require ``ru`` in
    ``layout`` and are marked ``group=1`` so XTest can switch to ЙЦУКЕН.
    """
    us = _definition_us(char)
    if us is not None or not layout_has_ru(layout):
        return us
    return _definition_ru(char)


def _definition_ru(char: str) -> KeyDefinition | None:
    found = _RU_CHARS.get(char)
    if found is None:
        return None
    code, key_code, shift = found
    return KeyDefinition(char, code, key_code, char, shift=shift, group=1)


def level0_keysym(definition: KeyDefinition) -> int | None:
    """Keysym that must sit on level 0 of ``definition.group``, if we can check it.

    ``None`` means "do not check" (US group, or a character without a known keysym).
    """
    if definition.group == 0:
        return None
    if definition.text == "№":
        return 0x0033  # digit 3; Shift produces numerosign
    return CYRILLIC_KEYSYMS.get(definition.text.lower())


def _definition_us(char: str) -> KeyDefinition | None:  # noqa: PLR0911 — a lookup table
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


def definition_for_name(name: str, *, layout: str = "us") -> KeyDefinition:
    if name in NAMED_KEYS:
        return NAMED_KEYS[name]
    if len(name) == 1:
        definition = definition_for_char(name, layout=layout)
        if definition is not None:
            return definition
    raise ValueError(f"unknown key {name!r}")
