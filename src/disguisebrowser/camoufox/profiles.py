"""Named browser profiles for the Camoufox engine.

A profile is what makes "the same visitor" come back: a pinned device identity
(``profile.json``) plus a persistent Firefox profile directory (``user-data/``)
with cookies, storage and history, plus the network personality (proxy,
language, timezone). Camoufox draws a *fresh* identity on every launch; pinning
the drawn identity makes every dependent draw (fonts, voices, GPU parameters,
media devices, audio seed) reproducible, because Camoufox derives them from a
salt hashed out of the pinned identity.

Layout under the store root (default ``~/.local/share/disguisebrowser/profiles``,
override with ``$DISGUISE_HOME``)::

    <name>/profile.json   identity + settings, mode 0600 (may hold proxy credentials)
    <name>/user-data/     Firefox profile directory
    <name>/.lock          held while a browser runs on this profile
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import random
import re
import secrets
import shutil
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from disguisebrowser.errors import DisguiseError
from disguisebrowser.persona import Persona, Screen

SCHEMA = 1
OSES = ("windows", "macos", "linux")
Kind = Literal["fpgen", "preset"]
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ProfileError(DisguiseError):
    """A profile is missing, malformed, already exists or is in use."""


def default_root() -> Path:
    home = os.environ.get("DISGUISE_HOME")
    if home:
        return Path(home) / "profiles"
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data) / "disguisebrowser" / "profiles"


def validate_name(name: str) -> str:
    if not _NAME.match(name):
        raise ProfileError(
            f"invalid profile name {name!r}: use letters, digits, '.', '_' or '-' "
            "(1-64 characters, starting with a letter or digit)"
        )
    return name


def os_of_user_agent(user_agent: str) -> str | None:
    """``windows`` / ``macos`` / ``linux`` for a Firefox UA string, else ``None``."""
    if "Windows" in user_agent:
        return "windows"
    if "Macintosh" in user_agent or "Mac OS X" in user_agent:
        return "macos"
    if "Linux" in user_agent or "X11" in user_agent:
        return "linux"
    return None


@dataclass(frozen=True)
class Profile:
    """Everything needed to relaunch the same visitor."""

    name: str
    os: str
    kind: Kind
    identity: dict[str, Any]
    """fpgen fingerprint (``kind="fpgen"``) or a bundled real-device preset."""
    proxy: str | None = None
    geoip: bool = False
    languages: tuple[str, ...] | None = None
    timezone: str | None = None
    seed: int | None = None
    notes: str = ""
    screen_width: int | None = None
    """Chosen screen width. ``None`` leaves Camoufox free to fit the monitor."""
    screen_height: int | None = None
    start_url: str | None = None
    """Page the manager opens when this profile's window starts."""
    created_at: str = ""
    camoufox: str = ""
    """Camoufox browser build the identity was drawn for (informational)."""
    schema: int = SCHEMA
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ (de)serialization
    def to_json(self) -> str:
        data = asdict(self)
        data["languages"] = list(self.languages) if self.languages else None
        return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> Profile:
        try:
            data = json.loads(text)
            if data.get("schema") != SCHEMA:
                raise ProfileError(f"unsupported profile schema {data.get('schema')!r}")
            languages = data.get("languages")
            screen_width = _optional_dim(data.get("screen_width"))
            screen_height = _optional_dim(data.get("screen_height"))
            if screen_width is None or screen_height is None:
                screen_width = screen_height = None
            raw_url = data.get("start_url")
            start_url = raw_url.strip() or None if isinstance(raw_url, str) else None
            return cls(
                name=data["name"],
                os=data["os"],
                kind=data["kind"],
                identity=data["identity"],
                proxy=data.get("proxy"),
                geoip=bool(data.get("geoip", False)),
                languages=tuple(languages) if languages else None,
                timezone=data.get("timezone"),
                seed=data.get("seed"),
                notes=data.get("notes", ""),
                screen_width=screen_width,
                screen_height=screen_height,
                start_url=start_url,
                created_at=data.get("created_at", ""),
                camoufox=data.get("camoufox", ""),
                extra=data.get("extra", {}),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProfileError(f"malformed profile.json: {exc}") from exc

    # ------------------------------------------------------------------ views
    def summary(self) -> dict[str, Any]:
        """Short, human-oriented description of the pinned device."""
        if self.kind == "fpgen":
            nav = self.identity.get("navigator", {})
            screen = self.identity.get("screen", {})
            gpu = self.identity.get("webgl", {}) or self.identity.get("videoCard", {})
            ua = nav.get("userAgent", "")
            width, height = screen.get("width"), screen.get("height")
            renderer = gpu.get("unmaskedRenderer") or gpu.get("renderer") or gpu.get("vendor") or ""
            cores = nav.get("hardwareConcurrency")
        else:
            nav = self.identity.get("navigator", {})
            screen = self.identity.get("screen", {})
            ua = nav.get("userAgent", "")
            width, height = screen.get("width"), screen.get("height")
            renderer = self.identity.get("webgl", {}).get("unmaskedRenderer", "")
            cores = nav.get("hardwareConcurrency")
        return {
            "name": self.name,
            "os": self.os,
            "kind": self.kind,
            "user_agent": ua,
            "screen": f"{width}x{height}" if width and height else "",
            "gpu": renderer,
            "cores": cores,
            "proxy": _mask_proxy(self.proxy),
            "geoip": self.geoip,
            "languages": list(self.languages) if self.languages else None,
            "timezone": self.timezone,
            "start_url": self.start_url,
            "created_at": self.created_at,
        }

    def persona(self) -> Persona:
        """Network personality of the profile on top of the honest host persona."""
        overrides: dict[str, Any] = {}
        if self.languages:
            overrides["languages"] = self.languages
        if self.timezone:
            overrides["timezone"] = self.timezone
        if self.proxy:
            overrides["proxy"] = self.proxy
        if self.seed is not None:
            overrides["seed"] = self.seed
        return Persona.from_host(**overrides)


# Same vertical chrome camoufox.fingerprints.fix_screen_no_taskbar would add.
# A chosen screen skips that fix, so the gap has to travel with the choice.
_TASKBAR = {"windows": 40, "macos": 25, "linux": 27}
_SCREEN_TEXT = re.compile(r"^(\d{3,5})x(\d{3,5})$")
_MAX_URL = 2048


def _dim(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _optional_dim(value: Any) -> int | None:
    """A stored dimension, or ``None`` when the file has no choice."""
    return _dim(value)


def _nonneg(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _taskbar(os_name: str) -> int:
    return _TASKBAR.get(os_name, 40)


def _gap(old_size: int, old_avail: int | None, new_size: int, *, minimum: int) -> int:
    """Keep the old screen-to-avail gap, and invent ``minimum`` when there is none."""
    if old_avail is None:
        gap = minimum
    else:
        gap = max(0, old_size - old_avail)
        if gap == 0:
            gap = minimum
    return max(1, new_size - gap)


def _boxes(identity: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    screen = identity.get("screen")
    window = identity.get("window")
    screen_box = screen if isinstance(screen, dict) else {}
    window_box = window if isinstance(window, dict) else {}
    if not window_box and isinstance(screen_box.get("window"), dict):
        window_box = screen_box["window"]
    return screen_box, window_box


def coerce_screen(value: Any) -> tuple[int, int]:
    """``WIDTHxHEIGHT`` (or a pair of ints) inside a plausible desktop range."""
    if isinstance(value, str):
        text = re.sub(r"\s+", "", value).replace("*", "x").replace("×", "x").replace("X", "x")
        match = _SCREEN_TEXT.match(text)
        if match is None:
            raise ProfileError("screen must be WIDTHxHEIGHT, for example 1920x1080")
        width, height = int(match.group(1)), int(match.group(2))
    elif (
        isinstance(value, tuple)
        and len(value) == 2
        and all(isinstance(part, int) and not isinstance(part, bool) for part in value)
    ):
        width, height = value
    else:
        raise ProfileError("screen must be WIDTHxHEIGHT, for example 1920x1080")
    problems = Screen(width, height).validate()
    if problems:
        raise ProfileError(problems[0])
    return width, height


def parse_start_url(value: Any) -> str | None:
    """An http(s) address, or ``None`` when the field is cleared."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProfileError("start_url must be an http(s) address")
    text = value.strip()
    if not text:
        return None
    if len(text) > _MAX_URL:
        raise ProfileError("start_url is too long")
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ProfileError("start_url must be an http(s) address")
    return text


def _place_window(
    window: Any,
    *,
    old_avail: tuple[int, int],
    old_screen: tuple[int, int],
    new_screen: tuple[int, int],
    new_avail: tuple[int, int],
) -> None:
    if not isinstance(window, dict):
        return
    _place_axis(
        window,
        axis="Width",
        pos_key="screenX",
        old_screen=old_screen[0],
        old_avail=old_avail[0],
        new_screen=new_screen[0],
        new_avail=new_avail[0],
    )
    _place_axis(
        window,
        axis="Height",
        pos_key="screenY",
        old_screen=old_screen[1],
        old_avail=old_avail[1],
        new_screen=new_screen[1],
        new_avail=new_avail[1],
    )


def _place_axis(
    window: dict[str, Any],
    *,
    axis: str,
    pos_key: str,
    old_screen: int,
    old_avail: int,
    new_screen: int,
    new_avail: int,
) -> None:
    outer_key = f"outer{axis}"
    inner_key = f"inner{axis}"
    outer = _dim(window.get(outer_key))
    if outer is None:
        return
    inner = _dim(window.get(inner_key))
    chrome = max(0, outer - inner) if inner is not None else None
    filled = outer >= old_avail - 16 or outer >= old_screen - 16
    resized = new_avail if filled else min(outer, new_avail)
    window[outer_key] = max(1, resized)
    if chrome is not None:
        window[inner_key] = max(1, resized - chrome)
    pos = _nonneg(window.get(pos_key))
    if pos is None:
        return
    window[pos_key] = 0 if filled else min(pos, max(0, new_screen - resized))


def apply_screen(
    identity: dict[str, Any], width: int, height: int, *, os_name: str
) -> dict[str, Any]:
    """Return a copy of ``identity`` whose screen and window fit ``width``×``height``."""
    width, height = coerce_screen((width, height))
    updated: dict[str, Any] = json.loads(json.dumps(identity))
    screen = updated.get("screen")
    if not isinstance(screen, dict):
        screen = {}
        updated["screen"] = screen
    old_w = _dim(screen.get("width")) or width
    old_h = _dim(screen.get("height")) or height
    old_aw = _dim(screen.get("availWidth")) or old_w
    old_ah = _dim(screen.get("availHeight")) or old_h
    screen["width"] = width
    screen["height"] = height
    screen["availWidth"] = _gap(old_w, _dim(screen.get("availWidth")), width, minimum=0)
    screen["availHeight"] = _gap(
        old_h, _dim(screen.get("availHeight")), height, minimum=_taskbar(os_name)
    )
    _place_window(
        updated.get("window"),
        old_avail=(old_aw, old_ah),
        old_screen=(old_w, old_h),
        new_screen=(width, height),
        new_avail=(screen["availWidth"], screen["availHeight"]),
    )
    nested = screen.get("window")
    if nested is not updated.get("window"):
        _place_window(
            nested,
            old_avail=(old_aw, old_ah),
            old_screen=(old_w, old_h),
            new_screen=(width, height),
            new_avail=(screen["availWidth"], screen["availHeight"]),
        )
    orientation = screen.get("orientation")
    if isinstance(orientation, dict):
        landscape = width >= height
        orientation["type"] = "landscape-primary" if landscape else "portrait-primary"
        if "angle" in orientation:
            orientation["angle"] = 0
    return updated


def geometry_config(profile: Profile) -> dict[str, Any]:
    """Camoufox config that keeps a chosen screen.

    Any ``screen.*`` or ``window.*`` key marks the geometry as intentional, so
    Camoufox will not lift it to 1366×768 or shrink it to the host monitor.
    """
    width, height = _dim(profile.screen_width), _dim(profile.screen_height)
    if width is None or height is None:
        return {}
    identity = profile.identity if isinstance(profile.identity, dict) else {}
    screen, window = _boxes(identity)
    old_w = _dim(screen.get("width")) or width
    old_h = _dim(screen.get("height")) or height
    avail_w = _gap(old_w, _dim(screen.get("availWidth")), width, minimum=0)
    avail_h = _gap(old_h, _dim(screen.get("availHeight")), height, minimum=_taskbar(profile.os))
    outer_w = _dim(window.get("outerWidth"))
    outer_h = _dim(window.get("outerHeight"))
    if outer_w is None or outer_w > avail_w:
        outer_w = avail_w
    if outer_h is None or outer_h > avail_h:
        outer_h = avail_h
    pos_x = min(_nonneg(window.get("screenX")) or 0, max(0, width - outer_w))
    pos_y = min(_nonneg(window.get("screenY")) or 0, max(0, height - outer_h))
    config: dict[str, Any] = {
        "screen.width": width,
        "screen.height": height,
        "screen.availWidth": avail_w,
        "screen.availHeight": avail_h,
        "window.outerWidth": outer_w,
        "window.outerHeight": outer_h,
        "window.screenX": pos_x,
        "window.screenY": pos_y,
    }
    for source, dest in (("colorDepth", "screen.colorDepth"), ("pixelDepth", "screen.pixelDepth")):
        value = _dim(screen.get(source))
        if value is not None:
            config[dest] = value
    return config


def _mask_proxy(proxy: str | None) -> str | None:
    if not proxy or "@" not in proxy:
        return proxy
    scheme, _, rest = proxy.partition("://")
    return f"{scheme}://***@{rest.rsplit('@', 1)[1]}" if rest else proxy


def _normalize(value: Any) -> dict[str, Any]:
    """What a ``profile.json`` round trip would produce.

    Camoufox hashes the pinned identity into the salt of every dependent draw,
    so the very first launch must see the same structure as every later one.
    """
    result: dict[str, Any] = json.loads(json.dumps(value, default=str))
    return result


def _draw_fpgen(os_name: str | None) -> tuple[dict[str, Any], str]:
    from camoufox.fingerprints import generate_fingerprint  # noqa: PLC0415

    try:
        fingerprint = _normalize(generate_fingerprint(os=os_name))
    except Exception as exc:  # fpgen downloads its model on first use
        raise ProfileError(
            f"could not draw a device identity ({type(exc).__name__}: {exc}); fpgen fetches "
            "its model from GitHub on first use, so the first run needs network access"
        ) from exc
    ua = str(fingerprint.get("navigator", {}).get("userAgent", ""))
    return fingerprint, os_name or os_of_user_agent(ua) or "linux"


def _draw_preset(os_name: str | None, seed: int | None) -> tuple[dict[str, Any], str]:
    from camoufox.fingerprints import load_presets  # noqa: PLC0415

    presets = (load_presets() or {}).get("presets", {})
    keys = [os_name] if os_name else [key for key in OSES if presets.get(key)]
    pool = [(key, preset) for key in keys for preset in presets.get(key, [])]
    if not pool:
        raise ProfileError(f"no bundled presets for os={os_name!r}")
    rng = random.Random(seed) if seed is not None else random.SystemRandom()
    key, preset = rng.choice(pool)
    return _normalize(preset), key


def _installed_camoufox() -> str:
    try:
        from camoufox.pkgman import installed_verstr  # noqa: PLC0415

        return str(installed_verstr())
    except Exception:  # not fetched yet, or a layout this code does not know
        return ""


class ProfileStore:
    """Directory of named profiles."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self.root = Path(root) if root is not None else default_root()

    # ------------------------------------------------------------------ paths
    def path(self, name: str) -> Path:
        return self.root / validate_name(name)

    def user_data_dir(self, name: str) -> Path:
        return self.path(name) / "user-data"

    def exists(self, name: str) -> bool:
        return (self.path(name) / "profile.json").is_file()

    # ------------------------------------------------------------------ CRUD
    def create(
        self,
        name: str,
        *,
        os: str | None = None,
        kind: Kind = "fpgen",
        seed: int | None = None,
        proxy: str | None = None,
        geoip: bool = False,
        languages: tuple[str, ...] | None = None,
        timezone: str | None = None,
        notes: str = "",
        screen: Any = None,
        start_url: str | None = None,
    ) -> Profile:
        """Draw a new device identity and store it under ``name``."""
        validate_name(name)
        if os is not None and os not in OSES:
            raise ProfileError(f"os must be one of {', '.join(OSES)}, got {os!r}")
        if geoip and not proxy:
            raise ProfileError("geoip=True needs a proxy: the exit IP decides the locale")
        chosen = coerce_screen(screen) if screen is not None else None
        homepage = parse_start_url(start_url)
        if self.exists(name):
            raise ProfileError(f"profile {name!r} already exists")
        if kind == "fpgen":
            identity, resolved_os = _draw_fpgen(os)
        elif kind == "preset":
            identity, resolved_os = _draw_preset(os, seed)
        else:
            raise ProfileError(f"kind must be 'fpgen' or 'preset', got {kind!r}")
        screen_width = screen_height = None
        if chosen is not None:
            screen_width, screen_height = chosen
            identity = apply_screen(identity, screen_width, screen_height, os_name=resolved_os)
        profile = Profile(
            name=name,
            os=resolved_os,
            kind=kind,
            identity=identity,
            proxy=proxy,
            geoip=geoip,
            languages=languages,
            timezone=timezone,
            seed=seed if seed is not None else secrets.randbits(31),
            notes=notes,
            screen_width=screen_width,
            screen_height=screen_height,
            start_url=homepage,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            camoufox=_installed_camoufox(),
        )
        problems = profile.persona().validate()
        if problems:
            raise ProfileError("; ".join(problems))
        self.save(profile)
        return profile

    def save(self, profile: Profile) -> None:
        directory = self.path(profile.name)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "profile.json"
        temporary = directory / ".profile.json.tmp"
        # 0600: the proxy URL may carry credentials.
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(profile.to_json() + "\n")
        os.replace(temporary, target)

    def load(self, name: str) -> Profile:
        file = self.path(name) / "profile.json"
        if not file.is_file():
            raise ProfileError(f"no profile named {name!r} in {self.root}")
        return Profile.from_json(file.read_text(encoding="utf-8"))

    def update(self, name: str, **changes: Any) -> Profile:
        """Change proxy, locale, notes, the start page, or the screen size.

        A new screen size is written into the identity. The next launch then
        redraws fonts and noise seeds from that identity; the user agent and
        the GPU stay as they were.
        """
        screen = changes.pop("screen", None)
        allowed = {"proxy", "geoip", "languages", "timezone", "notes", "seed", "start_url"}
        unknown = set(changes) - allowed
        if unknown:
            raise ProfileError(
                f"cannot change {', '.join(sorted(unknown))}: the device identity is pinned"
            )
        if "start_url" in changes:
            changes["start_url"] = parse_start_url(changes["start_url"])
        profile = self.load(name)
        screen_changed = False
        if screen is not None:
            width, height = coerce_screen(screen)
            current = profile.identity.get("screen") if isinstance(profile.identity, dict) else {}
            current = current if isinstance(current, dict) else {}
            same = current.get("width") == width and current.get("height") == height
            if same:
                if profile.screen_width != width or profile.screen_height != height:
                    profile = replace(profile, screen_width=width, screen_height=height)
                    screen_changed = True
            else:
                profile = replace(
                    profile,
                    identity=apply_screen(profile.identity, width, height, os_name=profile.os),
                    screen_width=width,
                    screen_height=height,
                )
                screen_changed = True
        if changes:
            profile = replace(profile, **changes)
        elif not screen_changed:
            return profile
        if profile.geoip and not profile.proxy:
            raise ProfileError("geoip=True needs a proxy")
        self.save(profile)
        return profile

    def list(self) -> list[Profile]:
        if not self.root.is_dir():
            return []
        found = []
        for child in sorted(self.root.iterdir()):
            if (child / "profile.json").is_file():
                with contextlib.suppress(ProfileError):
                    found.append(self.load(child.name))
        return found

    def delete(self, name: str) -> None:
        directory = self.path(name)
        if not directory.is_dir():
            raise ProfileError(f"no profile named {name!r} in {self.root}")
        with self.lock(name):  # refuses to delete a profile a browser is using
            shutil.rmtree(directory)

    def in_use(self, name: str) -> bool:
        """True when some browser already holds this profile's lock.

        The probe uses the same non-blocking exclusive lock as :meth:`lock` and
        drops it at once. A failed acquire must not unlock: a second descriptor
        in this process cannot release the lock another descriptor still holds.
        """
        directory = self.path(name)
        if not directory.is_dir():
            return False
        with open(directory / ".lock", "a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False

    # ------------------------------------------------------------------ locking
    @contextlib.contextmanager
    def lock(self, name: str) -> Iterator[None]:
        """Exclusive advisory lock: one browser per profile directory."""
        directory = self.path(name)
        directory.mkdir(parents=True, exist_ok=True)
        handle = open(directory / ".lock", "w", encoding="utf-8")  # noqa: SIM115
        try:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ProfileError(f"profile {name!r} is in use by another browser") from None
            yield
        finally:
            handle.close()
