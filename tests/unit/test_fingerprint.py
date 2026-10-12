"""Fingerprint records: classification, diff, naming and the capture server."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from disguisebrowser.fingerprint import (
    CaptureServer,
    by_category,
    classify,
    default_path,
    diff,
    diff_to_json,
    flatten,
    format_diff,
    gpu_slug,
    load_record,
    save_record,
    summarize,
)


def _record(**overrides: object) -> dict:
    base = {
        "meta": {"schema": 1, "captureId": "a", "capturedAt": "2026-10-11T00:00:00Z"},
        "navigator": {
            "userAgent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/154.0.0.0 Safari/537.36",
            "platform": "Linux x86_64",
            "languages": ["en-US", "en"],
            "hardwareConcurrency": 8,
        },
        "ua_data": {
            "platform": "Linux",
            "platformVersion": "6.8.0",
            "uaFullVersion": "154.0.8037.97",
            "brands": [{"brand": "Google Chrome", "version": "154"}],
            "fullVersionList": [{"brand": "Google Chrome", "version": "154.0.8037.97"}],
        },
        "webgl": {
            "unmaskedRenderer": "ANGLE (AMD, AMD Radeon RX 6600 (radeonsi navi23 LLVM 20.1.2), "
            "OpenGL 4.6)",
            "extensions": ["EXT_a", "EXT_b"],
            "render": {"pixelsHash": "aaaa"},
        },
        "canvas": {"scenes": {"text": {"pixelsHash": "1111", "stable": True}}},
        "screen": {"width": 1920},
        "intl": {"dateTimeFormat": {"timeZone": "Europe/Berlin"}},
        "battery": {"level": 1},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------- classification
def test_classification_by_path() -> None:
    assert classify("navigator.hardwareConcurrency") == "deterministic"
    assert classify("webgl.extensions") == "deterministic"
    assert classify("canvas.scenes.text.stable") == "deterministic"
    assert classify("canvas.scenes.text.pixelsHash") == "noise"
    assert classify("webgl.render.pixelsHash") == "noise"
    assert classify("audio.offline.hash") == "noise"
    assert classify("navigator.languages") == "persona"
    assert classify("headers.fetch.values.accept-language") == "persona"
    assert classify("intl.dateTimeFormat.timeZone") == "persona"
    assert classify("media_queries.(prefers-color-scheme: dark)") == "persona"
    assert classify("screen.window.innerWidth") == "geometry"
    assert classify("headers.navigation.values.viewport-width") == "geometry"
    assert classify("intl.error") == "deterministic"
    assert classify("battery.error") == "deterministic"
    assert classify("meta.capturedAt") == "environment"
    assert classify("battery.level") == "environment"
    assert classify("performance.memory.usedJSHeapSize") == "environment"
    assert classify("performance.memory.jsHeapSizeLimit") == "deterministic"


# ---------------------------------------------------------------------- flatten / diff
def test_flatten_keeps_scalar_lists_whole_and_indexes_object_lists() -> None:
    flat = dict(flatten({"a": {"b": [1, 2]}, "c": [{"d": 1}, {"d": 2}], "e": {}}))
    assert flat == {"a.b": [1, 2], "c[0].d": 1, "c[1].d": 2, "e": {}}


def test_identical_records_have_no_differences() -> None:
    assert diff(_record(), _record()) == []


def test_diff_reports_each_category() -> None:
    other = _record()
    other["navigator"]["hardwareConcurrency"] = 16
    other["webgl"]["extensions"] = ["EXT_a", "EXT_c"]
    other["webgl"]["render"]["pixelsHash"] = "bbbb"
    other["navigator"]["languages"] = ["de-DE", "de"]
    other["screen"]["width"] = 2560
    other["battery"]["level"] = 0.5
    other["new_section"] = {"x": 1}
    del other["canvas"]
    differences = diff(_record(), other)
    grouped = by_category(differences)
    paths = {d.path for d in grouped["deterministic"]}
    assert paths == {
        "navigator.hardwareConcurrency",
        "webgl.extensions",
        "new_section.x",
        "canvas.scenes.text.stable",
    }
    assert [d.path for d in grouped["noise"]] == [
        "canvas.scenes.text.pixelsHash",
        "webgl.render.pixelsHash",
    ]
    assert [d.path for d in grouped["persona"]] == ["navigator.languages"]
    assert [d.path for d in grouped["geometry"]] == ["screen.width"]
    assert [d.path for d in grouped["environment"]] == ["battery.level"]

    text = format_diff(differences)
    assert "webgl.extensions: -EXT_b  +EXT_c" in text
    assert "new_section.x: only in B = 1" in text
    assert "canvas.scenes.text.stable: only in A = true" in text
    assert "4 difference(s) — NOT identical" in text

    payload = json.loads(diff_to_json(differences))
    assert payload["counts"]["deterministic"] == 4
    only_in = {d["path"]: d["only_in"] for d in payload["differences"]}
    assert only_in["new_section.x"] == "b"
    assert only_in["canvas.scenes.text.stable"] == "a"


def test_diff_ignore_accepts_prefixes_and_globs() -> None:
    other = _record()
    other["navigator"]["hardwareConcurrency"] = 16
    other["webgl"]["extensions"] = []
    assert diff(_record(), other, ignore=["navigator"]) != []
    assert diff(_record(), other, ignore=["navigator", "webgl.*"]) == []


# ---------------------------------------------------------------------- summary / naming
def test_summary_prefers_client_hints_and_full_version() -> None:
    summary = summarize(_record())
    assert summary.os == "linux"
    assert summary.os_version == "6.8.0"
    assert summary.chrome_version == "154.0.8037.97"
    assert summary.chrome_major == "154"
    assert summary.gpu_slug == "amd-radeon-rx-6600"
    assert summary.brand == "chrome"
    assert "Chrome 154.0.8037.97" in summary.label


def test_chromium_brand_is_recognised_and_named(tmp_path: Path) -> None:
    record = _record()
    record["ua_data"]["brands"] = [{"brand": "Chromium", "version": "154"}]
    summary = summarize(record)
    assert summary.brand == "chromium"
    assert "Chromium 154" in summary.label
    assert default_path(record, tmp_path).name == "154-chromium.json"


def test_summary_falls_back_to_user_agent() -> None:
    record = _record(ua_data={"available": False}, webgl={"available": False})
    record["navigator"]["userAgent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/153.0.0.0 Safari/537.36"
    )
    summary = summarize(record)
    assert summary.os == "windows"
    assert summary.chrome_major == "153"
    assert summary.gpu_slug == "no-webgl"


def test_gpu_slug_strips_driver_noise() -> None:
    assert (
        gpu_slug(
            "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x00002504) Direct3D11 vs_5_0 ps_5_0, "
            "D3D11-31.0.15.3623)"
        )
        == "nvidia-geforce-rtx-3060"
    )
    assert gpu_slug("ANGLE (Mesa, llvmpipe (LLVM 20.1.2 256 bits), OpenGL 4.5)") == "mesa-llvmpipe"
    assert (
        gpu_slug("ANGLE (Intel, Mesa Intel(R) Iris(R) Xe Graphics (ADL GT2), OpenGL 4.6)")
        == "intel-mesa-iris-xe-graphics"
    )
    assert (
        gpu_slug("ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)") == "apple-m2"
    )
    assert gpu_slug(None) == "no-webgl"


def test_default_path_and_round_trip(tmp_path: Path) -> None:
    record = _record()
    first = default_path(record, tmp_path)
    assert first == tmp_path / "linux" / "amd-radeon-rx-6600" / "154.json"
    save_record(record, first)
    assert default_path(record, tmp_path).name == "154-2.json"
    loaded = load_record(first)
    assert loaded == record
    assert first.read_text().endswith("\n")


# ---------------------------------------------------------------------- server
def test_capture_server_serves_page_echo_and_submit() -> None:
    with CaptureServer() as server:
        page = urllib.request.urlopen(server.url, timeout=5)
        body = page.read().decode()
        assert (
            "Accept-CH" in page.headers
            and "Sec-CH-UA-Platform-Version" in page.headers["Accept-CH"]
        )
        assert "Critical-CH" in page.headers
        assert '<script id="nav-headers" type="application/json">[[' in body
        assert "__NAV_HEADERS__" not in body

        script = urllib.request.urlopen(server.url + "capture.js", timeout=5).read().decode()
        assert "fnv1a64" in script

        request = urllib.request.Request(server.url + "echo", headers={"X-Probe": "1"})
        echoed = json.loads(urllib.request.urlopen(request, timeout=5).read())
        assert ["X-Probe", "1"] in echoed

        payload = json.dumps({"meta": {"schema": 1}, "navigator": {}}).encode()
        post = urllib.request.Request(
            server.url + "submit", data=payload, headers={"Content-Type": "application/json"}
        )
        assert urllib.request.urlopen(post, timeout=5).status == 204
        record = server.wait_sync(timeout=5)
        assert record["navigator"] == {}
        assert record["meta"]["remoteAddress"] == "127.0.0.1"

        bad = urllib.request.Request(
            server.url + "submit", data=b"[]", headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(bad, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
        else:  # pragma: no cover
            raise AssertionError("array payload must be rejected")
