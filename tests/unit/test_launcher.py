import json
from pathlib import Path

import pytest

from disguisebrowser.errors import LaunchError
from disguisebrowser.launcher import (
    LaunchConfig,
    build_args,
    default_preferences,
    headless_user_agent,
    prepare_profile,
)
from disguisebrowser.persona import Persona, Window

FORBIDDEN_FLAGS = (
    "--enable-automation",
    "--headless",
    "--disable-blink-features",
    "--test-type",
    "--no-sandbox",
    "--remote-debugging-pipe",
    "--remote-debugging-port=0",
)


def _config(tmp_path: Path, **persona_kwargs: object) -> LaunchConfig:
    persona = Persona(languages=("de-DE", "de", "en-US", "en"), **persona_kwargs)  # type: ignore[arg-type]
    return LaunchConfig(
        executable=Path("/usr/bin/google-chrome"),
        user_data_dir=tmp_path / "profile",
        persona=persona,
        port=9333,
    )


def test_launch_args_follow_the_quiet_recipe(tmp_path: Path) -> None:
    args = build_args(_config(tmp_path, window=Window(1280, 720, 10, 20)))
    assert "--remote-debugging-port=9333" in args
    assert f"--user-data-dir={tmp_path / 'profile'}" in args
    assert "--lang=de-DE" in args
    assert "--window-size=1280,720" in args and "--window-position=10,20" in args
    assert args[-1] == "about:blank"
    for flag in FORBIDDEN_FLAGS:
        assert not any(a.startswith(flag) for a in args), flag


def test_proxy_flag_without_credentials(tmp_path: Path) -> None:
    args = build_args(_config(tmp_path, proxy="socks5://10.0.0.1:1080"))
    assert "--proxy-server=socks5://10.0.0.1:1080" in args


def test_authenticated_proxy_is_rejected_for_now(tmp_path: Path) -> None:
    with pytest.raises(LaunchError, match="authenticated proxies"):
        build_args(_config(tmp_path, proxy="http://user:pass@10.0.0.1:8080"))


def test_headless_user_agent_strips_headless_marker() -> None:
    ua = headless_user_agent("154.0.8037.97")
    assert "Headless" not in ua and "Chrome/154.0.0.0" in ua and "X11; Linux x86_64" in ua


def test_prepare_profile_writes_and_merges_preferences(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    persona = Persona(languages=("fr-FR", "fr"), proxy="http://10.0.0.1:3128")
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Preferences").write_text(json.dumps({"custom": {"keep": 1}}))
    prepare_profile(profile, persona)
    prefs = json.loads((profile / "Default" / "Preferences").read_text())
    assert prefs["custom"] == {"keep": 1}
    assert prefs["intl"]["accept_languages"] == "fr-FR,fr"
    assert prefs["translate"]["enabled"] is False
    assert prefs["profile"]["exit_type"] == "Normal"
    assert prefs["webrtc"]["ip_handling_policy"] == "disable_non_proxied_udp"
    assert (profile / "First Run").exists()


def test_default_preferences_without_proxy_keep_public_interface_only() -> None:
    prefs = default_preferences(Persona())
    assert prefs["webrtc"]["ip_handling_policy"] == "default_public_interface_only"
    assert prefs["webrtc"]["nonproxied_udp_enabled"] is True
