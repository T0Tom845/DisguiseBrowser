"""Dialogs, response log and the geoip default. No browser."""

from __future__ import annotations

import asyncio

import pytest

from disguisebrowser import cli
from disguisebrowser.errors import DisguiseError
from disguisebrowser.exchange import (
    HttpResponse,
    JsDialog,
    Pending,
    ResponseLog,
    safe_filename,
    wants_geoip,
)


def _response(url: str) -> HttpResponse:
    async def read() -> bytes:
        return b"{}"

    return HttpResponse(
        url=url, method="GET", status=200, resource_type="XHR", headers={}, reader=read
    )


async def test_dialog_accept_and_dismiss_once() -> None:
    seen: list[tuple[bool, str | None]] = []

    async def reply(accept: bool, prompt_text: str | None) -> None:
        seen.append((accept, prompt_text))

    dialog = JsDialog(type="prompt", message="name?", default_value="anon", reply=reply)
    await dialog.accept("ada")
    assert seen == [(True, "ada")]
    with pytest.raises(DisguiseError, match="already handled"):
        await dialog.dismiss()


async def test_response_log_waits_for_a_later_match_only() -> None:
    log = ResponseLog()
    log.add(_response("https://example.test/old.json"))
    future = log.expect("new.json")
    assert not future.done()
    log.add(_response("https://example.test/other"))
    assert not future.done()
    log.add(_response("https://example.test/new.json"))
    assert future.result().url.endswith("new.json")


async def test_response_log_keeps_a_bounded_history() -> None:
    log = ResponseLog()
    for index in range(205):
        log.add(_response(f"https://example.test/{index}"))
    assert len(log.entries) == 200
    assert log.entries[0].url.endswith("/5")


async def test_pending_timeout_does_not_cancel_the_underlying_wait() -> None:
    future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    with pytest.raises(TimeoutError, match="a download"):
        await Pending(future, 0.01, "a download").value()
    assert not future.cancelled()


def test_safe_filename_strips_path_characters() -> None:
    assert safe_filename("../../etc/passwd") == "etc_passwd"
    assert safe_filename("...") == "download"


@pytest.mark.parametrize(
    ("flag", "proxy", "profile", "expected"),
    [
        (None, False, None, False),
        (None, True, None, True),
        (False, True, None, False),
        (True, False, None, True),
        (None, True, False, False),
        (None, False, True, True),
        (True, True, False, True),
    ],
)
def test_wants_geoip(flag: bool | None, proxy: bool, profile: bool | None, expected: bool) -> None:
    assert wants_geoip(flag, has_proxy=proxy, profile_geoip=profile) is expected


def test_geoip_flag_is_optional_on_every_engine() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["open"]).geoip is None
    assert parser.parse_args(["open", "--geoip"]).geoip is True
    assert parser.parse_args(["open", "--no-geoip"]).geoip is False
    camoufox = parser.parse_args(["open", "--engine", "camoufox", "--no-geoip"])
    assert camoufox.geoip is False
