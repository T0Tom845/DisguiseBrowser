"""``disguise profile ...`` and the engine flags of the launch commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from disguisebrowser import cli
from disguisebrowser.camoufox import profiles
from disguisebrowser.camoufox.profiles import ProfileStore

IDENTITY: dict[str, Any] = {
    "navigator": {"userAgent": "Mozilla/5.0 (X11; Linux x86_64; rv:146.0) Firefox/146.0"},
    "screen": {"width": 1920, "height": 1080},
    "webgl": {"unmaskedRenderer": "ANGLE (Intel, Mesa)"},
}


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        profiles,
        "_draw_fpgen",
        lambda os_name: (json.loads(json.dumps(IDENTITY)), os_name or "linux"),
    )


def run(root: Path, *argv: str) -> int:
    return cli.main(["profile", "--root", str(root), *argv])


def test_create_list_show_set_delete_cycle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path, "create", "shop-1", "--os", "windows", "--notes", "first") == 0
    assert "shop-1" in capsys.readouterr().out

    assert run(tmp_path, "list", "--json") == 0
    listed = json.loads(capsys.readouterr().out)
    assert [p["name"] for p in listed] == ["shop-1"]

    assert run(tmp_path, "show", "shop-1", "--json") == 0
    assert json.loads(capsys.readouterr().out)["os"] == "windows"

    assert run(tmp_path, "set", "shop-1", "--timezone", "Europe/Paris", "--lang", "fr-FR,fr") == 0
    stored = ProfileStore(tmp_path).load("shop-1")
    assert stored.timezone == "Europe/Paris"
    assert stored.languages == ("fr-FR", "fr")

    assert run(tmp_path, "set", "shop-1", "--timezone", "") == 0
    assert ProfileStore(tmp_path).load("shop-1").timezone is None

    assert run(tmp_path, "delete", "shop-1", "--yes") == 0
    assert ProfileStore(tmp_path).list() == []


def test_show_full_prints_the_stored_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, "create", "p")
    capsys.readouterr()
    assert run(tmp_path, "show", "p", "--full") == 0
    assert json.loads(capsys.readouterr().out)["identity"] == IDENTITY


def test_errors_exit_with_code_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(tmp_path, "create", "p")
    assert run(tmp_path, "create", "p") == 2
    assert "already exists" in capsys.readouterr().err
    assert run(tmp_path, "show", "ghost") == 2
    assert run(tmp_path, "set", "p") == 2  # nothing to change
    assert run(tmp_path, "create", "bad/name") == 2


def test_delete_requires_confirmation_off_a_terminal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, "create", "p")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert run(tmp_path, "delete", "p") == 2
    assert ProfileStore(tmp_path).exists("p")
    assert "--yes" in capsys.readouterr().err


def test_delete_asks_on_a_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run(tmp_path, "create", "p")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert run(tmp_path, "delete", "p") == 1
    assert ProfileStore(tmp_path).exists("p")
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    assert run(tmp_path, "delete", "p") == 0


def test_listing_when_empty_gives_a_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path, "list") == 0
    assert "disguise profile create" in capsys.readouterr().out


# ---------------------------------------------------------------------- launch flags
def test_launch_commands_accept_engine_flags() -> None:
    args = cli.build_parser().parse_args(
        ["open", "--engine", "camoufox", "--profile", "p", "--create", "--os", "macos", "--geoip"]
    )
    assert (args.engine, args.profile, args.create, args.os, args.geoip) == (
        "camoufox",
        "p",
        True,
        "macos",
        True,
    )
    assert cli.build_parser().parse_args(["test"]).engine == "chrome"


def test_camoufox_rejects_chrome_only_flags() -> None:
    args = cli.build_parser().parse_args(["open", "--engine", "camoufox", "--gpu", "intel-xe"])
    with pytest.raises(cli.DisguiseError, match="chrome engine only"):
        cli._launch(args)


def test_chrome_rejects_camoufox_only_flags() -> None:
    args = cli.build_parser().parse_args(["open", "--create"])
    with pytest.raises(cli.DisguiseError, match="--create applies to --engine camoufox only"):
        cli._launch(args)


def test_doctor_reports_camoufox_and_profiles() -> None:
    names = {c["check"] for c in cli.doctor_checks()}
    assert {"camoufox", "chrome", "profiles"} <= names
