import pytest

from disguisebrowser.browser import _endpoint_is_loopback, connect
from disguisebrowser.errors import LaunchError


def test_loopback_detection() -> None:
    assert _endpoint_is_loopback("ws://127.0.0.1:9222/devtools/browser/abc")
    assert _endpoint_is_loopback("ws://localhost:9222/devtools/browser/abc")
    assert not _endpoint_is_loopback("ws://8.8.8.8:9222/devtools/browser/abc")


async def test_connect_refuses_a_remote_devtools_port() -> None:
    with pytest.raises(LaunchError, match="allow_remote"):
        await connect(ws_url="ws://8.8.8.8:9222/devtools/browser/abc")


async def test_connect_requires_an_endpoint() -> None:
    with pytest.raises(LaunchError, match="ws_url or port"):
        await connect()
