"""Profile store: pinned identities, safe updates, locking. No browser, no network."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from disguisebrowser.camoufox import profiles
from disguisebrowser.camoufox.profiles import (
    Profile,
    ProfileError,
    ProfileStore,
    apply_screen,
    default_root,
    geometry_config,
    os_of_user_agent,
    validate_name,
)

FAKE_IDENTITY: dict[str, Any] = {
    "navigator": {
        "userAgent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) Gecko/20100101 Firefox/146.0"
        ),
        "hardwareConcurrency": 8,
    },
    "screen": {"width": 1920, "height": 1080},
    "webgl": {"unmaskedRenderer": "ANGLE (NVIDIA, GeForce GTX 980)"},
}


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProfileStore:
    def fake_draw(os_name: str | None) -> tuple[dict[str, Any], str]:
        return json.loads(json.dumps(FAKE_IDENTITY)), os_name or "windows"

    monkeypatch.setattr(profiles, "_draw_fpgen", fake_draw)
    return ProfileStore(tmp_path / "profiles")


@pytest.mark.parametrize("name", ["a", "shop-1", "Work_2.eu", "x" * 64])
def test_valid_names(name: str) -> None:
    assert validate_name(name) == name


@pytest.mark.parametrize("name", ["", "-lead", ".hidden", "a/b", "../x", "x" * 65, "sp ace", "ü"])
def test_invalid_names_are_rejected(name: str) -> None:
    with pytest.raises(ProfileError):
        validate_name(name)


def test_create_persists_and_roundtrips(store: ProfileStore) -> None:
    created = store.create("shop-1", os="windows", proxy="socks5://u:p@10.0.0.1:1080", notes="n")
    loaded = store.load("shop-1")
    assert loaded == created
    assert loaded.identity == FAKE_IDENTITY
    assert loaded.os == "windows"
    assert loaded.seed is not None
    assert store.exists("shop-1")


def test_profile_file_is_private(store: ProfileStore) -> None:
    store.create("p", proxy="http://user:secret@10.0.0.1:8080")
    mode = stat.S_IMODE((store.path("p") / "profile.json").stat().st_mode)
    assert mode == 0o600


def test_duplicate_name_is_refused(store: ProfileStore) -> None:
    store.create("p")
    with pytest.raises(ProfileError, match="already exists"):
        store.create("p")


def test_create_validates_inputs(store: ProfileStore) -> None:
    with pytest.raises(ProfileError, match="os must be"):
        store.create("p", os="beos")
    with pytest.raises(ProfileError, match="needs a proxy"):
        store.create("p", geoip=True)
    with pytest.raises(ProfileError, match="kind must be"):
        store.create("p", kind="bogus")  # type: ignore[arg-type]
    assert not store.exists("p")


def test_screen_choice_rewrites_geometry_and_keeps_the_device(store: ProfileStore) -> None:
    original = {
        "navigator": {"userAgent": "same-browser"},
        "screen": {
            "width": 1920,
            "height": 1080,
            "availWidth": 1920,
            "availHeight": 1040,
            "colorDepth": 24,
            "orientation": {"type": "landscape-primary", "angle": 0},
        },
        "window": {
            "outerWidth": 1920,
            "outerHeight": 1040,
            "innerWidth": 1900,
            "innerHeight": 1000,
            "screenX": 0,
            "screenY": 0,
        },
    }
    created = store.create("p", os="windows", screen="2560x1440")
    assert created.screen_width == 2560
    assert created.identity["screen"]["width"] == 2560
    assert created.identity["screen"]["availHeight"] == 1440 - 40
    assert created.identity["navigator"]["userAgent"].startswith("Mozilla")

    fitted = apply_screen(original, 2560, 1440, os_name="windows")
    assert original["screen"]["width"] == 1920
    assert fitted["screen"]["availWidth"] == 2560
    assert fitted["screen"]["availHeight"] == 1400
    assert fitted["window"]["outerWidth"] == 2560
    assert fitted["window"]["outerHeight"] == 1400
    assert fitted["window"]["innerWidth"] == 2540
    assert fitted["window"]["innerHeight"] == 1360
    assert fitted["navigator"]["userAgent"] == "same-browser"
    again = apply_screen(fitted, 2560, 1440, os_name="windows")
    assert again["screen"] == fitted["screen"]
    assert again["window"] == fitted["window"]

    windowed = apply_screen(
        {
            **original,
            "window": {"outerWidth": 1280, "outerHeight": 720, "screenX": 100, "screenY": 40},
        },
        2560,
        1440,
        os_name="windows",
    )
    assert windowed["window"]["outerWidth"] == 1280
    assert windowed["window"]["screenX"] == 100

    with pytest.raises(ProfileError, match="not a plausible"):
        store.create("tiny", screen="100x100")
    with pytest.raises(ProfileError, match="start_url must be"):
        store.create("web", start_url="javascript:alert(1)")

    same = store.create("same", os="windows")
    pinned = store.update("same", screen="1920x1080")
    assert pinned.screen_width == 1920
    assert pinned.identity["screen"] == same.identity["screen"]
    grown = store.update("same", screen="1680x1050")
    assert grown.identity["screen"]["height"] == 1050
    assert grown.identity["navigator"]["userAgent"] == same.identity["navigator"]["userAgent"]
    assert geometry_config(grown)["screen.height"] == 1050
    assert geometry_config(same) == {}


def test_old_profile_without_screen_choice_still_loads(store: ProfileStore) -> None:
    store.create("p", start_url="https://example.com")
    file = store.path("p") / "profile.json"
    data = json.loads(file.read_text())
    data.pop("screen_width")
    data.pop("screen_height")
    data.pop("start_url")
    file.write_text(json.dumps(data))
    loaded = store.load("p")
    assert loaded.screen_width is None
    assert loaded.screen_height is None
    assert loaded.start_url is None


def test_update_changes_network_not_device(store: ProfileStore) -> None:
    before = store.create("p", os="windows")
    after = store.update("p", timezone="Europe/Berlin", languages=("de-DE", "de"), notes="x")
    assert after.timezone == "Europe/Berlin"
    assert after.languages == ("de-DE", "de")
    assert after.identity == before.identity
    assert store.load("p").timezone == "Europe/Berlin"
    with pytest.raises(ProfileError, match="identity is pinned"):
        store.update("p", identity={})
    with pytest.raises(ProfileError, match="identity is pinned"):
        store.update("p", os="linux")


def test_update_geoip_needs_proxy_and_can_clear(store: ProfileStore) -> None:
    store.create("p")
    with pytest.raises(ProfileError, match="needs a proxy"):
        store.update("p", geoip=True)
    store.update("p", proxy="socks5://10.0.0.1:1080", geoip=True)
    assert store.update("p", proxy=None, geoip=False).proxy is None


def test_list_is_sorted_and_skips_broken_profiles(store: ProfileStore) -> None:
    store.create("b")
    store.create("a")
    broken = store.path("broken")
    broken.mkdir(parents=True)
    (broken / "profile.json").write_text("{not json")
    assert [p.name for p in store.list()] == ["a", "b"]


def test_list_of_a_missing_root_is_empty(tmp_path: Path) -> None:
    assert ProfileStore(tmp_path / "nothing").list() == []


def test_delete_removes_everything(store: ProfileStore) -> None:
    store.create("p")
    (store.user_data_dir("p")).mkdir(parents=True)
    (store.user_data_dir("p") / "cookies.sqlite").write_text("x")
    store.delete("p")
    assert not store.path("p").exists()
    with pytest.raises(ProfileError, match="no profile named"):
        store.delete("p")


def test_lock_is_exclusive_and_blocks_delete(store: ProfileStore) -> None:
    store.create("p")
    with store.lock("p"):
        with pytest.raises(ProfileError, match="in use"), store.lock("p"):
            pass
        with pytest.raises(ProfileError, match="in use"):
            store.delete("p")
    store.delete("p")  # released


def test_in_use_sees_the_lock_without_releasing_it(store: ProfileStore) -> None:
    store.create("p")
    assert store.in_use("p") is False
    with store.lock("p"):
        assert store.in_use("p") is True
        with pytest.raises(ProfileError, match="in use"), store.lock("p"):
            pass
    assert store.in_use("p") is False
    assert store.in_use("missing") is False


def test_load_unknown_profile(store: ProfileStore) -> None:
    with pytest.raises(ProfileError, match="no profile named"):
        store.load("ghost")


def test_malformed_and_newer_schema_are_reported(store: ProfileStore) -> None:
    store.create("p")
    file = store.path("p") / "profile.json"
    data = json.loads(file.read_text())
    file.write_text(json.dumps({**data, "schema": 99}))
    with pytest.raises(ProfileError):
        store.load("p")
    file.write_text(json.dumps({k: v for k, v in data.items() if k != "identity"}))
    with pytest.raises(ProfileError, match="malformed"):
        store.load("p")


def test_summary_masks_credentials(store: ProfileStore) -> None:
    profile = store.create("p", proxy="socks5://user:secret@10.0.0.1:1080")
    summary = profile.summary()
    assert summary["proxy"] == "socks5://***@10.0.0.1:1080"
    assert "secret" not in json.dumps(summary)
    assert summary["screen"] == "1920x1080"
    assert "GTX 980" in summary["gpu"]


def test_persona_carries_profile_network_settings(store: ProfileStore) -> None:
    profile = store.create(
        "p", proxy="socks5://10.0.0.1:1080", languages=("fr-FR", "fr"), timezone="Europe/Paris"
    )
    persona = profile.persona()
    assert persona.languages == ("fr-FR", "fr")
    assert persona.timezone == "Europe/Paris"
    assert persona.proxy == "socks5://10.0.0.1:1080"
    assert persona.seed == profile.seed


def test_profile_json_is_stable(store: ProfileStore) -> None:
    profile = store.create("p")
    assert Profile.from_json(profile.to_json()) == profile


def test_default_root_honours_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DISGUISE_HOME", str(tmp_path))
    assert default_root() == tmp_path / "profiles"
    monkeypatch.delenv("DISGUISE_HOME")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert default_root() == tmp_path / "xdg" / "disguisebrowser" / "profiles"


@pytest.mark.parametrize(
    ("user_agent", "expected"),
    [
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) Gecko/20100101 Firefox/146.0",
            "windows",
        ),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:147.0) Gecko/20100101 Firefox/147.0",
            "macos",
        ),
        ("Mozilla/5.0 (X11; Linux x86_64; rv:146.0) Gecko/20100101 Firefox/146.0", "linux"),
        ("curl/8", None),
    ],
)
def test_os_of_user_agent(user_agent: str, expected: str | None) -> None:
    assert os_of_user_agent(user_agent) == expected
