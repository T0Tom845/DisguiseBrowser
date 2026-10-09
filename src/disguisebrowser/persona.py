"""Persona: the single source of truth for a browser identity.

Rule of thumb (ADR-8): everything that cannot be changed *below* JavaScript is
taken honestly from the host. A Persona therefore never carries a fake
User-Agent or platform; it carries the knobs we can turn without JS hooks:
languages (``--lang`` + Accept-Language prefs), timezone (``TZ`` for the whole
process), screen/window geometry (Xvfb size + window flags), GPU identity
(Mesa driconf on a virtual display) and the proxy.
"""

from __future__ import annotations

import os
import random
import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import available_timezones

_LANG_TAG = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
_PROXY_SCHEMES = frozenset({"http", "https", "socks4", "socks5", "socks5h"})


@dataclass(frozen=True)
class Screen:
    width: int = 1920
    height: int = 1080
    dpr: float = 1.0

    def validate(self) -> list[str]:
        problems = []
        if not (800 <= self.width <= 7680 and 600 <= self.height <= 4320):
            problems.append(f"screen {self.width}x{self.height} is not a plausible desktop size")
        if self.dpr not in (1.0, 1.25, 1.5, 1.75, 2.0):
            problems.append(f"devicePixelRatio {self.dpr} is unusual for a desktop")
        return problems


@dataclass(frozen=True)
class Window:
    width: int
    height: int
    x: int = 0
    y: int = 0

    def fits(self, screen: Screen) -> bool:
        return (
            self.x >= 0
            and self.y >= 0
            and self.x + self.width <= screen.width
            and self.y + self.height <= screen.height
        )


@dataclass(frozen=True)
class GpuIdentity:
    """GL vendor/renderer as Mesa reports them.

    Chrome exposes them as ``ANGLE (vendor, renderer, OpenGL x.y)`` in
    ``WEBGL_debug_renderer_info``.
    """

    vendor: str
    renderer: str
    gl_version: str = "4.6"


GPU_PRESETS: dict[str, GpuIdentity] = {
    # Strings follow the Mesa 25.x renderer format for radeonsi / iris.
    "amd-rx6600": GpuIdentity("AMD", "AMD Radeon RX 6600 (radeonsi, navi23, ACO)"),
    "amd-rx7600": GpuIdentity("AMD", "AMD Radeon RX 7600 (radeonsi, gfx1102, ACO)"),
    "amd-renoir": GpuIdentity("AMD", "AMD Radeon Graphics (radeonsi, renoir, ACO)"),
    "amd-vega8": GpuIdentity("AMD", "AMD Radeon Vega 8 Graphics (radeonsi, raven, ACO)"),
    "intel-uhd630": GpuIdentity("Intel", "Mesa Intel(R) UHD Graphics 630 (CFL GT2)"),
    "intel-xe": GpuIdentity("Intel", "Mesa Intel(R) Xe Graphics (TGL GT2)"),
    "intel-arc-a770": GpuIdentity("Intel", "Mesa Intel(R) Arc(tm) A770 Graphics (DG2)"),
}


@dataclass(frozen=True)
class Persona:
    languages: tuple[str, ...] = ("en-US", "en")
    timezone: str | None = None
    screen: Screen = field(default_factory=Screen)
    window: Window | None = None
    gpu: GpuIdentity | None = None
    proxy: str | None = None
    seed: int | None = None

    # ------------------------------------------------------------------ factories
    @classmethod
    def from_host(cls, **overrides: object) -> Persona:
        """Honest persona: languages, timezone and screen taken from this machine."""
        persona = cls(
            languages=detect_languages(),
            timezone=detect_timezone(),
            screen=detect_screen() or Screen(),
        )
        return replace(persona, **overrides) if overrides else persona  # type: ignore[arg-type]

    # ------------------------------------------------------------------ derived values
    @property
    def primary_language(self) -> str:
        return self.languages[0]

    def rng(self, salt: str = "") -> random.Random:
        """Deterministic RNG when ``seed`` is set (same profile → same geometry)."""
        if self.seed is None:
            return random.Random()
        return random.Random(f"{self.seed}:{salt}")

    def effective_window(self) -> Window:
        """Explicit window, or a plausible desktop layout derived from the screen."""
        if self.window is not None:
            return self.window
        rng = self.rng("window")
        screen = self.screen
        if rng.random() < 0.7:
            # "Maximized" under a top panel of 28–48 px, as on GNOME/KDE desktops.
            panel = rng.choice((28, 32, 36, 40, 48))
            return Window(screen.width, screen.height - panel, 0, panel)
        width = min(screen.width, rng.choice((1280, 1366, 1440, 1536, 1600)))
        height = min(screen.height - 48, rng.choice((720, 768, 800, 864, 900)))
        x = rng.randint(0, max(0, screen.width - width))
        y = rng.randint(0, max(0, screen.height - height))
        return Window(width, height, x, y)

    def proxy_parts(self) -> tuple[str, str, int, str | None, str | None] | None:
        """``(scheme, host, port, username, password)`` or ``None``."""
        if not self.proxy:
            return None
        parts = urlsplit(self.proxy if "://" in self.proxy else f"http://{self.proxy}")
        if not parts.hostname or not parts.port:
            raise ValueError(f"proxy must be scheme://[user:pass@]host:port, got {self.proxy!r}")
        return parts.scheme, parts.hostname, parts.port, parts.username, parts.password

    # ------------------------------------------------------------------ validation
    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.languages:
            problems.append("at least one language is required")
        for tag in self.languages:
            if not _LANG_TAG.match(tag):
                problems.append(f"{tag!r} is not a BCP-47 language tag like 'ru-RU'")
        if self.timezone is not None and self.timezone not in available_timezones():
            problems.append(f"unknown IANA timezone {self.timezone!r}")
        problems.extend(self.screen.validate())
        window = self.effective_window()
        if not window.fits(self.screen):
            problems.append(
                f"window {window.width}x{window.height}@{window.x},{window.y} does not fit "
                f"the {self.screen.width}x{self.screen.height} screen"
            )
        if self.proxy:
            try:
                scheme, *_ = self.proxy_parts() or ("",)
            except ValueError as exc:
                problems.append(str(exc))
            else:
                if scheme not in _PROXY_SCHEMES:
                    problems.append(f"unsupported proxy scheme {scheme!r}")
        return problems


# ---------------------------------------------------------------------- host detection
def detect_languages(environ: dict[str, str] | None = None) -> tuple[str, ...]:
    """Derive Chrome-style Accept-Language chain from the POSIX locale variables."""
    env = os.environ if environ is None else environ
    candidates: list[str] = []
    if env.get("LANGUAGE"):
        candidates.extend(env["LANGUAGE"].split(":"))
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if env.get(var):
            candidates.append(env[var])
    tags: list[str] = []
    for raw in candidates:
        tag = _posix_locale_to_tag(raw)
        if tag and tag not in tags:
            tags.append(tag)
            base = tag.split("-")[0]
            if base not in tags:
                tags.append(base)
    for fallback in ("en-US", "en"):
        if fallback not in tags:
            tags.append(fallback)
    return tuple(tags)


def _posix_locale_to_tag(value: str) -> str | None:
    value = value.split(".", maxsplit=1)[0].split("@", maxsplit=1)[0].strip()
    if not value or value in {"C", "POSIX"}:
        return None
    lang, _, region = value.partition("_")
    lang = lang.lower()
    if not re.fullmatch(r"[a-z]{2,3}", lang):
        return None
    return f"{lang}-{region.upper()}" if region else lang


def detect_timezone(environ: dict[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    tz = env.get("TZ", "").lstrip(":")
    if tz and tz in available_timezones():
        return tz
    etc_timezone = Path("/etc/timezone")
    if etc_timezone.is_file():
        name = etc_timezone.read_text().strip()
        if name in available_timezones():
            return name
    localtime = Path("/etc/localtime")
    if localtime.is_symlink():
        target = os.path.realpath(localtime)
        marker = "/zoneinfo/"
        if marker in target:
            name = target.split(marker, 1)[1]
            if name in available_timezones():
                return name
    return None


def detect_screen(display: str | None = None) -> Screen | None:
    """Current X screen size via ``xdpyinfo`` (host display only)."""
    display = display or os.environ.get("DISPLAY")
    if not display:
        return None
    try:
        out = subprocess.run(
            ["xdpyinfo", "-display", display],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"dimensions:\s+(\d+)x(\d+) pixels", out)
    if not match:
        return None
    return Screen(int(match.group(1)), int(match.group(2)))
