"""Canvas readback noise for one pinned device.

The browser applies this in C++ (``fork/camoufox/CanvasFingerprintManager.cpp``).
``apply_noise`` is the same algorithm, so a unit test can compile the C++ and
compare. Keep the two copies in step.

What the noise is allowed to do:

* one profile and the same picture always produce the same bytes;
* another profile produces different bytes;
* a flat canvas (white, black, one solid color) is returned unchanged;
* a changed pixel moves by one level, on one color channel, and only where the
  picture is already not flat.
"""

from __future__ import annotations

from typing import Any, Literal

Order = Literal["rgba", "bgra"]

_FNV_OFFSET = 2166136261
_FNV_PRIME = 16777619
_RATE_MASK = 511
_RATE_LIMIT = 2
_CANVAS_MUL = 2246822519
_CANVAS_ADD = 3266489917


def _u32(value: int) -> int:
    return value & 0xFFFFFFFF


def _mix(seed: int, content: int, index: int) -> int:
    hash_ = _u32(_u32(seed * 0x9E3779B1) ^ _u32(content * 0x85EBCA77) ^ _u32(index * 0xC2B2AE3D))
    hash_ = _u32(hash_ ^ (hash_ >> 16))
    hash_ = _u32(hash_ * 0x7FEB352D)
    hash_ = _u32(hash_ ^ (hash_ >> 15))
    hash_ = _u32(hash_ * 0x846CA68B)
    hash_ = _u32(hash_ ^ (hash_ >> 16))
    return hash_


def _channels(order: Order) -> tuple[int, int, int, int]:
    if order == "bgra":
        return (2, 1, 0, 3)
    if order == "rgba":
        return (0, 1, 2, 3)
    raise ValueError(order)


def _flat(data: bytearray, pixels: int, index: int) -> bool:
    pixel = data[index * 4 : index * 4 + 4]
    if 0 < index < pixels - 1:
        return (
            pixel == data[(index - 1) * 4 : index * 4]
            and pixel == data[(index + 1) * 4 : (index + 2) * 4]
        )
    neighbor = 1 if index == 0 else index - 1
    return pixel == data[neighbor * 4 : neighbor * 4 + 4]


def _mid(pixel: bytearray, channels: tuple[int, int, int, int]) -> bool:
    return any(pixel[channels[i]] not in (0, 255) for i in range(3))


def _eligible(
    data: bytearray, pixels: int, index: int, channels: tuple[int, int, int, int]
) -> bool:
    pixel = data[index * 4 : index * 4 + 4]
    if pixel[3] == 0 or _flat(data, pixels, index):
        return False
    return _mid(pixel, channels)


def _perturb(pixel: bytearray, channels: tuple[int, int, int, int], hash_: int) -> None:
    start = (hash_ >> 8) % 3
    for step in range(3):
        logical = (start + step) % 3
        slot = channels[logical]
        if pixel[slot] not in (0, 255):
            pixel[slot] ^= 1
            return


def apply_noise(data: bytearray, seed: int, order: Order = "rgba") -> None:
    """Noise ``data`` in place. ``seed`` 0 leaves it alone."""
    if seed == 0 or len(data) < 16 or len(data) % 4 != 0:
        return
    pixels = len(data) // 4
    if pixels > 0xFFFFFFFF:
        return
    channels = _channels(order)

    content = _FNV_OFFSET
    uniform = True
    for index in range(pixels):
        pixel = data[index * 4 : index * 4 + 4]
        if uniform and index > 0 and pixel != data[:4]:
            uniform = False
        for channel in channels:
            content = _u32((content ^ pixel[channel]) * _FNV_PRIME)
    if uniform:
        return

    picks: list[tuple[int, int]] = []
    best_hash: int | None = None
    best_index = 0
    for index in range(pixels):
        if not _eligible(data, pixels, index, channels):
            continue
        hash_ = _mix(seed, content, index)
        if best_hash is None or hash_ < best_hash:
            best_hash = hash_
            best_index = index
        if (hash_ & _RATE_MASK) < _RATE_LIMIT:
            picks.append((index, hash_))
    if not picks and best_hash is not None:
        picks.append((best_index, best_hash))
    for index, hash_ in picks:
        pixel = data[index * 4 : index * 4 + 4]
        _perturb(pixel, channels, hash_)
        data[index * 4 : index * 4 + 4] = pixel


def canvas_seed(identity: Any | None) -> int:
    """Non-zero seed for one device. A pinned identity keeps the same seed.

    An empty identity draws a fresh seed for that launch. The number is not the
    audio seed: Camoufox already derives ``audio:seed`` on its own.
    """
    from camoufox.fingerprints import identity_salt  # noqa: PLC0415

    salt = identity_salt(identity)
    seed = _u32(salt * _CANVAS_MUL + _CANVAS_ADD)
    return seed or 1
