"""Pure option building for the Camoufox engine, plus scorecard waivers."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("camoufox")
pytest.importorskip("playwright")

from disguisebrowser import scorecard
from disguisebrowser.camoufox import browser as fox
from disguisebrowser.camoufox.profiles import Profile
from disguisebrowser.errors import LaunchError
from disguisebrowser.persona import Persona
from disguisebrowser.proxy import GeoInfo

IDENTITY: dict[str, Any] = {"navigator": {"userAgent": "x"}, "screen": {"width": 1, "height": 1}}


def _options(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "profile": None,
        "persona": Persona(languages=("en-US", "en"), timezone="Europe/Berlin", seed=1),
        "user_data_dir": "/tmp/x",
        "headless": True,
        "humanize": True,
        "proxy_server": None,
        "geo": None,
        "main_world": False,
    }
    return fox.build_options(**{**base, **overrides})


def test_pinned_fpgen_profile_sets_fingerprint_and_os() -> None:
    profile = Profile(name="p", os="windows", kind="fpgen", identity=IDENTITY)
    options = _options(profile=profile)
    assert options["fingerprint"] == IDENTITY
    assert "fingerprint_preset" not in options
    assert options["os"] == "windows"
    assert options["persistent_context"] is True
    assert options["user_data_dir"] == "/tmp/x"


def test_pinned_preset_profile_uses_the_preset_key() -> None:
    profile = Profile(name="p", os="macos", kind="preset", identity=IDENTITY)
    options = _options(profile=profile)
    assert options["fingerprint_preset"] == IDENTITY
    assert "fingerprint" not in options


def test_ephemeral_run_has_no_pinned_identity() -> None:
    options = _options(os_name="linux")
    assert "fingerprint" not in options
    assert options["os"] == "linux"


def test_locale_and_timezone_follow_the_persona() -> None:
    options = _options()
    assert options["locale"] == ["en-US", "en"]
    assert options["config"]["timezone"] == "Europe/Berlin"
    assert options["i_know_what_im_doing"] is True


def test_geo_aligns_webrtc_and_location_with_the_exit_ip() -> None:
    geo = GeoInfo(
        ip="203.0.113.7",
        country="DE",
        timezone="Europe/Berlin",
        raw={"latitude": 52.52, "longitude": 13.4},
    )
    options = _options(geo=geo, proxy_server="socks5://127.0.0.1:9")
    assert options["proxy"] == {"server": "socks5://127.0.0.1:9"}
    assert options["config"]["webrtc:ipv4"] == "203.0.113.7"
    assert options["config"]["geolocation:latitude"] == 52.52
    assert options["firefox_user_prefs"]["network.dns.disableIPv6"] is True


def test_ipv6_exit_uses_the_ipv6_slot() -> None:
    geo = GeoInfo(ip="2001:db8::1", country="DE", timezone="Europe/Berlin", raw={})
    options = _options(geo=geo)
    assert options["config"]["webrtc:ipv6"] == "2001:db8::1"
    assert "webrtc:ipv4" not in options["config"]
    assert "firefox_user_prefs" not in options


def test_extra_options_win() -> None:
    assert _options(extra={"humanize": False})["humanize"] is False


def test_main_world_flag_is_forwarded() -> None:
    assert _options(main_world=True)["main_world_eval"] is True


def test_display_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(fox.shutil, "which", lambda _name: "/usr/bin/Xvfb")
    assert fox.resolve_display("auto", False) == ("virtual", "virtual")
    assert fox.resolve_display("auto", True) == (True, "headless")
    with pytest.raises(LaunchError, match="requires \\$DISPLAY"):
        fox.resolve_display("host", False)

    monkeypatch.setenv("DISPLAY", ":0")
    assert fox.resolve_display("auto", False) == (False, "host")
    assert fox.resolve_display("virtual", False) == ("virtual", "virtual")


def test_display_without_xvfb(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(fox.shutil, "which", lambda _name: None)
    assert fox.resolve_display("auto", False) == (True, "headless")
    with pytest.raises(LaunchError, match="needs Xvfb"):
        fox.resolve_display("virtual", False)


def test_humanize_option_maps_presets() -> None:
    assert fox.humanize_option("fast") is not fox.humanize_option("careful")
    assert fox.humanize_option("no-such-preset") is True


def test_launch_rejects_conflicting_arguments() -> None:
    with pytest.raises(LaunchError, match="os must be"):
        fox.launch(os="beos")
    with pytest.raises(LaunchError, match="either profile or user_data_dir"):
        fox.launch(profile="a", user_data_dir="/tmp/y")


async def test_launch_without_a_profile_name_never_touches_the_store(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from disguisebrowser.camoufox.profiles import ProfileStore  # noqa: PLC0415

    handle = fox.launch(profile="ghost", store=ProfileStore(tmp_path))
    with pytest.raises(LaunchError, match="no profile named 'ghost'"):
        await handle
    assert not (tmp_path / "ghost").exists()


# ---------------------------------------------------------------------- scorecard waivers
def test_waivers_remove_inapplicable_failures_only() -> None:
    raw = {"total": 10, "failed": ["Chrome (New) => missing (failed)", "WebDriver => present"]}
    result = scorecard._apply_waivers(raw, ("Chrome (New)",))
    assert result["failed"] == ["WebDriver => present"]
    assert result["waived"] == ["Chrome (New) => missing (failed)"]
    assert result["total"] == 9
    verdict, summary = scorecard._judge_sannysoft(result)
    assert verdict == "fail"
    assert "n/a for this engine" in summary


def test_waived_only_run_passes_and_says_so() -> None:
    raw = {"total": 5, "failed": ["Chrome (New) => missing (failed)"]}
    verdict, summary = scorecard._judge_sannysoft(scorecard._apply_waivers(raw, ("Chrome (New)",)))
    assert verdict == "pass"
    assert "1 check n/a" in summary


def test_waivers_do_not_apply_to_other_engines() -> None:
    sannysoft = next(d for d in scorecard.DETECTORS if d.name == "sannysoft")
    assert "chrome" not in sannysoft.waive
    assert "Chrome (New)" in sannysoft.waive["camoufox"]
    assert scorecard._apply_waivers({"total": 1, "failed": ["x"]}, ()) == {
        "total": 1,
        "failed": ["x"],
    }
