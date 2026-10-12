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

from disguisebrowser.errors import DisguiseError
from disguisebrowser.persona import Persona

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
    ) -> Profile:
        """Draw a new device identity and store it under ``name``."""
        validate_name(name)
        if os is not None and os not in OSES:
            raise ProfileError(f"os must be one of {', '.join(OSES)}, got {os!r}")
        if geoip and not proxy:
            raise ProfileError("geoip=True needs a proxy: the exit IP decides the locale")
        if self.exists(name):
            raise ProfileError(f"profile {name!r} already exists")
        if kind == "fpgen":
            identity, resolved_os = _draw_fpgen(os)
        elif kind == "preset":
            identity, resolved_os = _draw_preset(os, seed)
        else:
            raise ProfileError(f"kind must be 'fpgen' or 'preset', got {kind!r}")
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
        """Change network settings (proxy, geoip, languages, timezone, notes, seed)."""
        allowed = {"proxy", "geoip", "languages", "timezone", "notes", "seed"}
        unknown = set(changes) - allowed
        if unknown:
            raise ProfileError(
                f"cannot change {', '.join(sorted(unknown))}: the device identity is pinned"
            )
        profile = replace(self.load(name), **changes)
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
