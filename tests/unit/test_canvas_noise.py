"""The canvas readback noise, and that the C++ patch implements the same one."""

from __future__ import annotations

import random
import shutil
import subprocess
from pathlib import Path

import pytest

from disguisebrowser.camoufox.canvas_noise import apply_noise

ROOT = Path(__file__).resolve().parents[2]
FORK = ROOT / "fork" / "camoufox"


def _scene() -> bytearray:
    """Mostly white, with a run of antialiased gray so the picture is not flat."""
    data = bytearray([255, 255, 255, 255] * 64)
    for index in range(20, 40):
        level = 80 + index
        data[index * 4 : index * 4 + 4] = bytes((level, level - 10, level - 20, 255))
    return data


def test_uniform_and_disabled_canvases_stay_byte_exact() -> None:
    for fill in ((255, 255, 255, 255), (0, 0, 0, 255), (255, 0, 0, 255)):
        original = bytearray(fill * 32)
        noisy = bytearray(original)
        apply_noise(noisy, seed=99)
        assert noisy == original

    scene = _scene()
    untouched = bytearray(scene)
    apply_noise(untouched, seed=0)
    assert untouched == scene


def test_one_profile_is_stable_and_another_profile_differs() -> None:
    first = bytearray(_scene())
    again = bytearray(_scene())
    apply_noise(first, seed=7)
    apply_noise(again, seed=7)
    assert first == again

    other = bytearray(_scene())
    apply_noise(other, seed=8)
    assert other != first


def test_a_single_antialiased_pixel_moves_by_one_and_white_stays() -> None:
    original = bytearray([255, 255, 255, 255] * 16)
    original[5 * 4 : 5 * 4 + 4] = bytes((200, 180, 160, 255))
    noisy = bytearray(original)
    apply_noise(noisy, seed=42)

    diffs = [index for index in range(len(original)) if original[index] != noisy[index]]
    assert len(diffs) == 1
    assert diffs[0] // 4 == 5
    assert diffs[0] % 4 != 3
    assert abs(original[diffs[0]] - noisy[diffs[0]]) == 1


def test_photo_noise_is_sparse_one_lsb_and_spares_alpha() -> None:
    rng = random.Random(1)
    original = bytearray(rng.randrange(1, 255) for _ in range(64 * 64 * 4))
    for index in range(3, len(original), 4):
        original[index] = 255
    noisy = bytearray(original)
    apply_noise(noisy, seed=123456)

    changed = 0
    for index in range(0, len(original), 4):
        before = original[index : index + 4]
        after = noisy[index : index + 4]
        assert after[3] == before[3]
        slots = [slot for slot in range(3) if before[slot] != after[slot]]
        assert len(slots) <= 1
        if slots:
            assert abs(before[slots[0]] - after[slots[0]]) == 1
            changed += 1
    assert changed > 0
    assert changed / (len(original) // 4) < 0.02


def test_bgra_and_rgba_noise_the_same_logical_pixels() -> None:
    rgba = bytearray(_scene())
    bgra = bytearray()
    for index in range(0, len(rgba), 4):
        red, green, blue, alpha = rgba[index : index + 4]
        bgra += bytes((blue, green, red, alpha))
    apply_noise(rgba, seed=11, order="rgba")
    apply_noise(bgra, seed=11, order="bgra")
    back = bytearray()
    for index in range(0, len(bgra), 4):
        blue, green, red, alpha = bgra[index : index + 4]
        back += bytes((red, green, blue, alpha))
    assert back == rgba


def _added_file(patch: str, path: str) -> str:
    capture = False
    added: list[str] = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            capture = line.startswith(f"diff --git a/{path} ")
            continue
        if not capture or not line.startswith("+") or line.startswith("+++"):
            continue
        added.append(line[1:])
    return "\n".join(added) + "\n"


def test_patch_embeds_the_manager_sources() -> None:
    patch = (FORK / "zz-canvas-readback.patch").read_text()
    for name in ("CanvasFingerprintManager.cpp", "CanvasFingerprintManager.h"):
        assert _added_file(patch, f"dom/canvas/{name}") == (FORK / name).read_text()


@pytest.fixture(scope="module")
def noise_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    compiler = shutil.which("g++")
    if compiler is None:
        pytest.skip("g++ is not installed")
    directory = tmp_path_factory.mktemp("canvas-noise")
    binary = directory / "noise_selftest"
    compiled = subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-DDISGUISE_CANVAS_NOISE_SELFTEST",
            "-I",
            str(FORK),
            str(FORK / "noise_selftest.cc"),
            str(FORK / "CanvasFingerprintManager.cpp"),
            "-o",
            str(binary),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if compiled.returncode != 0:
        raise AssertionError(compiled.stderr)
    return binary


def _cpp(binary: Path, data: bytes, seed: int, order: str) -> bytes:
    result = subprocess.run(
        [str(binary), str(seed), order],
        input=data,
        check=True,
        capture_output=True,
    )
    return result.stdout


def test_cpp_patch_matches_the_python_noise(noise_binary: Path) -> None:
    rng = random.Random(2)
    photo = bytes(rng.randrange(256) for _ in range(48 * 4))
    cases: list[tuple[bytes, int, str]] = [
        (bytes(_scene()), 7, "rgba"),
        (bytes(_scene()), 8, "bgra"),
        (bytes([255, 255, 255, 255] * 32), 99, "rgba"),
        (photo, 123456, "rgba"),
        (photo, 0, "bgra"),
    ]
    for data, seed, order in cases:
        python = bytearray(data)
        apply_noise(python, seed, order)  # type: ignore[arg-type]
        assert _cpp(noise_binary, data, seed, order) == bytes(python)
