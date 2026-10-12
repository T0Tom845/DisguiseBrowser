"""Fingerprint records: naming, storage, classification and diffing.

A record is the JSON produced by ``capture.js``. Fields are classified by
dotted path into categories (FORK_PLAN.md, section 6.1):

- ``deterministic`` — device and browser facts that must match the reference
  byte for byte (API surface, WebGL limits, codecs, fonts, voices, …);
- ``persona`` — choices of the persona rather than the device (language,
  timezone, keyboard layout, colour scheme);
- ``geometry`` — screen and window sizes, which follow the display;
- ``noise`` — render hashes expected to differ between device profiles and to
  stay stable within one;
- ``environment`` — things that legitimately change between runs (timestamps,
  battery, network, heap usage, focus).

Only ``deterministic`` differences fail a diff.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal

SCHEMA = 1

Category = Literal["deterministic", "persona", "geometry", "noise", "environment"]
CATEGORIES: tuple[Category, ...] = ("deterministic", "persona", "geometry", "noise", "environment")

# First matching pattern wins. Patterns are fnmatch globs over dotted paths;
# ``*`` also matches dots, so ``headers.*.host`` covers both header groups.
RULES: tuple[tuple[str, Category], ...] = (
    ("*.error", "deterministic"),  # a section that failed to collect is never "just persona"
    ("meta.*", "environment"),
    ("navigator.onLine", "environment"),
    ("storage.*", "environment"),
    ("performance.nowResolutionMs", "environment"),
    ("performance.memory.totalJSHeapSize", "environment"),
    ("performance.memory.usedJSHeapSize", "environment"),
    ("battery.*", "environment"),
    ("network.*", "environment"),
    ("audio.context.state", "environment"),
    ("misc.visibilityState", "environment"),
    ("misc.hasFocus", "environment"),
    ("misc.hidden", "environment"),
    ("misc.historyLength", "environment"),
    ("headers.*.downlink", "environment"),
    ("headers.*.rtt", "environment"),
    ("headers.*.ect", "environment"),
    ("canvas.scenes.*.pixelsHash", "noise"),
    ("canvas.scenes.*.dataUrlHash", "noise"),
    ("canvas.scenes.*.dataUrlLength", "noise"),
    ("canvas.scenes.*.offscreenPixelsHash", "noise"),
    ("webgl.render.*", "noise"),
    ("webgl2.render.*", "noise"),
    ("audio.offline.hash", "noise"),
    ("audio.offline.sum", "noise"),
    ("worker.canvas.*", "noise"),
    ("iframe.canvasTextHash", "noise"),
    ("navigator.language", "persona"),
    ("navigator.languages", "persona"),
    ("navigator.doNotTrack", "persona"),
    ("navigator.globalPrivacyControl", "persona"),
    ("headers.*.accept-language", "persona"),
    ("headers.*.sec-ch-prefers-*", "persona"),
    ("intl.*", "persona"),
    ("keyboard.*", "persona"),
    ("media_queries.(prefers-*", "persona"),
    ("media_queries.(forced-colors*", "persona"),
    ("media_queries.(inverted-colors*", "persona"),
    ("worker.language", "persona"),
    ("worker.languages", "persona"),
    ("worker.timeZone", "persona"),
    ("iframe.languages", "persona"),
    ("iframe.timeZone", "persona"),
    ("screen.*", "geometry"),
    ("iframe.screenWidth", "geometry"),
    ("iframe.devicePixelRatio", "geometry"),
    ("media_queries.(orientation*", "geometry"),
    ("media_queries.(min-resolution*", "geometry"),
    ("headers.*.viewport-width", "geometry"),
    ("headers.*.sec-ch-viewport-width", "geometry"),
    ("headers.*.dpr", "geometry"),
    ("headers.*.sec-ch-dpr", "geometry"),
    ("headers.*.width", "geometry"),
)


def classify(path: str) -> Category:
    for pattern, category in RULES:
        if fnmatchcase(path, pattern):
            return category
    return "deterministic"


# ---------------------------------------------------------------------- summary
@dataclass(frozen=True)
class Summary:
    os: str
    os_version: str | None
    chrome_version: str | None
    chrome_major: str | None
    gpu_renderer: str | None
    gpu_slug: str
    platform: str | None
    source: str | None
    brand: str = "chrome"
    """``chrome`` (Google Chrome brand present), ``chromium``, ``firefox`` or ``unknown``."""

    @property
    def label(self) -> str:
        version = self.chrome_version or (f"{self.chrome_major}.x" if self.chrome_major else "?")
        name = {"chrome": "Chrome", "chromium": "Chromium", "firefox": "Firefox"}.get(
            self.brand, "browser"
        )
        return f"{self.os} · {name} {version} · {self.gpu_renderer or 'no WebGL'}"


_OS_FROM_UA = (
    (re.compile(r"Windows NT"), "windows"),
    (re.compile(r"Mac OS X|Macintosh"), "macos"),
    (re.compile(r"CrOS"), "chromeos"),
    (re.compile(r"Android"), "android"),
    (re.compile(r"iPhone|iPad"), "ios"),
    (re.compile(r"Linux|X11"), "linux"),
)
_OS_FROM_CH = {
    "Windows": "windows",
    "macOS": "macos",
    "Linux": "linux",
    "Chrome OS": "chromeos",
    "Chromium OS": "chromeos",
    "Android": "android",
    "iOS": "ios",
}


def _get(record: dict[str, Any], *path: str) -> Any:
    node: Any = record
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _chrome_version(record: dict[str, Any]) -> str | None:
    full = _get(record, "ua_data", "uaFullVersion")
    if isinstance(full, str) and full:
        return full
    versions = _get(record, "ua_data", "fullVersionList")
    if isinstance(versions, list):
        for entry in versions:
            brand = str(entry.get("brand", ""))
            if "Chrome" in brand or "Chromium" in brand:
                return str(entry.get("version"))
    ua = _get(record, "navigator", "userAgent")
    match = re.search(r"Chrome/(\d+\.\d+\.\d+\.\d+)", str(ua or ""))
    if match:
        return match.group(1)
    firefox = re.search(r"Firefox/(\d+(?:\.\d+)*)", str(ua or ""))
    return firefox.group(1) if firefox else None


def _brand(record: dict[str, Any]) -> str:
    brands = _get(record, "ua_data", "brands")
    if not isinstance(brands, list):
        ua = str(_get(record, "navigator", "userAgent") or "")
        return "firefox" if "Firefox/" in ua and "Chrome/" not in ua else "unknown"
    names = {str(b.get("brand", "")) for b in brands if isinstance(b, dict)}
    if "Google Chrome" in names:
        return "chrome"
    if "Chromium" in names:
        return "chromium"
    return "unknown"


def summarize(record: dict[str, Any]) -> Summary:
    ua = str(_get(record, "navigator", "userAgent") or "")
    ch_platform = _get(record, "ua_data", "platform")
    os_name = _OS_FROM_CH.get(str(ch_platform), "") if ch_platform else ""
    if not os_name:
        os_name = next((name for pattern, name in _OS_FROM_UA if pattern.search(ua)), "unknown")
    os_version = _get(record, "ua_data", "platformVersion")
    version = _chrome_version(record)
    major = version.split(".", maxsplit=1)[0] if version else None
    renderer = _get(record, "webgl", "unmaskedRenderer") or _get(
        record, "webgl2", "unmaskedRenderer"
    )
    source = _get(record, "meta", "source", "kind")
    return Summary(
        os=os_name,
        os_version=str(os_version) if os_version else None,
        chrome_version=version,
        chrome_major=major,
        gpu_renderer=str(renderer) if renderer else None,
        gpu_slug=gpu_slug(str(renderer) if renderer else None),
        platform=str(_get(record, "navigator", "platform") or "") or None,
        source=str(source) if source else None,
        brand=_brand(record),
    )


_RENDERER_NOISE = re.compile(
    r"\(0x[0-9a-fA-F]+\)|Direct3D\d+|vs_\d_\d|ps_\d_\d|D3D\d+(-[\d.]+)?|OpenGL( ES)?( [\d.]+)?"
    r"|Vulkan( [\d.]+)?|Metal Renderer:?|Metal|Unspecified Version|LLVM [\d.]+|\d+ bits|ANGLE"
)


def gpu_slug(renderer: str | None) -> str:
    """``ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x2504) Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15)``
    → ``nvidia-geforce-rtx-3060``; ``ANGLE (Mesa, llvmpipe (LLVM 20.1.2 256 bits), OpenGL 4.5)``
    → ``mesa-llvmpipe``."""
    if not renderer:
        return "no-webgl"
    text = renderer
    if text.startswith("ANGLE (") and text.endswith(")"):
        text = text[len("ANGLE (") : -1]
    text = _RENDERER_NOISE.sub(" ", text)
    text = re.sub(r"\([^)]*\)", " ", text)  # remaining parenthesised driver details
    words = [w for w in re.split(r"[^A-Za-z0-9]+", text.lower()) if w]
    deduped: list[str] = []
    for word in words:
        if word not in deduped:
            deduped.append(word)
    slug = "-".join(deduped)[:64].strip("-")
    return slug or "unknown-gpu"


def default_path(record: dict[str, Any], root: Path) -> Path:
    """``<root>/<os>/<gpu-slug>/<major>.json`` for Google Chrome; other brands get a
    ``-chromium``/``-unknown`` suffix. A numeric suffix is added if the name is taken."""
    summary = summarize(record)
    directory = root / summary.os / summary.gpu_slug
    stem = summary.chrome_major or "unknown"
    if summary.brand != "chrome":
        stem = f"{stem}-{summary.brand}"
    candidate = directory / f"{stem}.json"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{counter}.json"
        counter += 1
    return candidate


def load_record(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "meta" not in data:
        raise ValueError(f"{path} is not a fingerprint record")
    return data


def save_record(record: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(record, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


# ---------------------------------------------------------------------- diff
_MISSING = object()


def flatten(node: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    """Leaves of a record keyed by dotted path. Lists of scalars stay whole so
    that extension lists and font lists compare as sets, not by index."""
    if isinstance(node, dict):
        if not node:
            yield prefix, {}
        for key, value in node.items():
            yield from flatten(value, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(node, list) and any(isinstance(item, dict | list) for item in node):
        for index, value in enumerate(node):
            yield from flatten(value, f"{prefix}[{index}]")
    else:
        yield prefix, node


@dataclass(frozen=True)
class Difference:
    path: str
    category: Category
    reference: Any
    other: Any

    @property
    def missing_in_reference(self) -> bool:
        return self.reference is _MISSING

    @property
    def missing_in_other(self) -> bool:
        return self.other is _MISSING

    def describe(self) -> str:
        if self.missing_in_reference:
            return f"{self.path}: only in B = {_short(self.other)}"
        if self.missing_in_other:
            return f"{self.path}: only in A = {_short(self.reference)}"
        if (
            isinstance(self.reference, list)
            and isinstance(self.other, list)
            and all(isinstance(x, str) for x in self.reference + self.other)
        ):
            removed = sorted(set(self.reference) - set(self.other))
            added = sorted(set(self.other) - set(self.reference))
            parts = []
            if removed:
                parts.append("-" + ", -".join(removed))
            if added:
                parts.append("+" + ", +".join(added))
            if not parts:
                parts.append("same items, different order")
            return f"{self.path}: " + "  ".join(parts)
        return f"{self.path}: A = {_short(self.reference)}  B = {_short(self.other)}"


def _short(value: Any, limit: int = 80) -> str:
    text = (
        json.dumps(value, ensure_ascii=False, sort_keys=True)
        if not isinstance(value, str)
        else value
    )
    return text if len(text) <= limit else text[: limit - 1] + "…"


def diff(
    reference: dict[str, Any], other: dict[str, Any], *, ignore: Iterable[str] = ()
) -> list[Difference]:
    """Differences between two records, ``A`` (reference) and ``B`` (other)."""
    ignored = tuple(ignore)
    left = dict(flatten(reference))
    right = dict(flatten(other))
    found: list[Difference] = []
    for path in sorted(set(left) | set(right)):
        if any(fnmatchcase(path, pattern) or path.startswith(pattern) for pattern in ignored):
            continue
        a = left.get(path, _MISSING)
        b = right.get(path, _MISSING)
        if a is _MISSING or b is _MISSING or a != b:
            found.append(Difference(path, classify(path), a, b))
    return found


def by_category(differences: list[Difference]) -> dict[Category, list[Difference]]:
    grouped: dict[Category, list[Difference]] = {c: [] for c in CATEGORIES}
    for d in differences:
        grouped[d.category].append(d)
    return grouped


def format_diff(
    differences: list[Difference], *, label_a: str = "A", label_b: str = "B", limit: int = 400
) -> str:
    grouped = by_category(differences)
    lines = [f"A = {label_a}", f"B = {label_b}", ""]
    for category in CATEGORIES:
        items = grouped[category]
        lines.append(f"[{category}] {len(items)} difference(s)")
        for item in items[:limit]:
            lines.append("  " + item.describe())
        if len(items) > limit:
            lines.append(f"  … {len(items) - limit} more")
        lines.append("")
    gate = len(grouped["deterministic"])
    lines.append(
        "deterministic set: "
        + ("identical" if gate == 0 else f"{gate} difference(s) — NOT identical")
    )
    return "\n".join(lines)


def diff_to_json(differences: list[Difference]) -> str:
    def value(v: Any) -> Any:
        return None if v is _MISSING else v

    payload = {
        "counts": {c: len(items) for c, items in by_category(differences).items()},
        "differences": [
            {
                "path": d.path,
                "category": d.category,
                "a": value(d.reference),
                "b": value(d.other),
                "only_in": "b" if d.missing_in_reference else ("a" if d.missing_in_other else None),
            }
            for d in differences
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
