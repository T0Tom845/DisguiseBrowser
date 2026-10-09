"""Proxy gateway against fake upstream proxies — no Chrome involved."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from tests.support.fake_proxies import FakeHttpConnectProxy, FakeSocks5Proxy, socks5_connect

from disguisebrowser.proxy import (
    GatewayStats,
    ProxyError,
    ProxyGateway,
    UpstreamProxy,
    fetch_json_via,
    languages_for_country,
    parse_geo,
    resolve_geo,
)


@pytest_asyncio.fixture
async def echo_server() -> AsyncIterator[tuple[str, int]]:
    """HTTP server answering GET / with JSON that echoes the request line."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = (await reader.readline()).decode().strip()
        while (await reader.readline()) not in (b"\r\n", b""):
            pass
        body = json.dumps({"request": request, "ip": "203.0.113.7", "country_code": "de",
                           "timezone": {"id": "Europe/Berlin"}}).encode()  # fmt: skip
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield "127.0.0.1", port
    finally:
        server.close()
        await server.wait_closed()


async def _get_via_gateway(gateway: ProxyGateway, host: str, port: int) -> dict[str, object]:
    assert gateway.port is not None
    reader, writer = await socks5_connect("127.0.0.1", gateway.port, host, port)
    writer.write(f"GET /probe HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    payload: dict[str, object] = json.loads(raw.split(b"\r\n\r\n", 1)[1])
    return payload


def test_parse_upstream_variants() -> None:
    http = UpstreamProxy.parse("http://user:p%40ss@proxy.example:8080")
    assert (http.scheme, http.host, http.port, http.username, http.password) == (
        "http", "proxy.example", 8080, "user", "p%40ss",
    )  # fmt: skip
    assert UpstreamProxy.parse("socks5h://1.2.3.4:1080").scheme == "socks5"
    assert UpstreamProxy.parse("1.2.3.4:3128").scheme == "http"
    with pytest.raises(ProxyError):
        UpstreamProxy.parse("ftp://x:21")
    with pytest.raises(ProxyError):
        UpstreamProxy.parse("http://nohostport")


async def test_gateway_through_http_connect_with_auth(echo_server: tuple[str, int]) -> None:
    upstream = await FakeHttpConnectProxy("alice", "s3cret").start()
    try:
        async with ProxyGateway(UpstreamProxy.parse(upstream.url)) as gateway:
            assert gateway.url.startswith("socks5://127.0.0.1:")
            payload = await _get_via_gateway(gateway, *echo_server)
            assert payload["request"] == "GET /probe HTTP/1.1"
            assert upstream.connects == [f"{echo_server[0]}:{echo_server[1]}"]
            assert upstream.auth_failures == 0
            assert gateway.stats.connections == 1 and gateway.stats.bytes_down > 0
    finally:
        await upstream.stop()


async def test_gateway_through_socks5_with_auth(echo_server: tuple[str, int]) -> None:
    upstream = await FakeSocks5Proxy("bob", "hunter2").start()
    try:
        async with ProxyGateway(UpstreamProxy.parse(upstream.url)) as gateway:
            payload = await _get_via_gateway(gateway, *echo_server)
            assert payload["request"] == "GET /probe HTTP/1.1"
            assert upstream.connects == [f"{echo_server[0]}:{echo_server[1]}"]
    finally:
        await upstream.stop()


async def test_gateway_without_credentials(echo_server: tuple[str, int]) -> None:
    upstream = await FakeSocks5Proxy().start()
    try:
        async with ProxyGateway(UpstreamProxy.parse(upstream.url)) as gateway:
            assert (await _get_via_gateway(gateway, *echo_server))["request"].startswith("GET")
    finally:
        await upstream.stop()


async def test_wrong_credentials_fail_the_tunnel(echo_server: tuple[str, int]) -> None:
    upstream = await FakeHttpConnectProxy("alice", "s3cret").start()
    try:
        wrong = UpstreamProxy("http", "127.0.0.1", upstream.port, "alice", "nope")
        async with ProxyGateway(wrong) as gateway:
            assert gateway.port is not None
            with pytest.raises(ConnectionError, match="reply 1"):
                await socks5_connect("127.0.0.1", gateway.port, *echo_server)
            assert upstream.auth_failures == 1
            assert gateway.stats.failures == 1
    finally:
        await upstream.stop()


async def test_upstream_down_is_reported(echo_server: tuple[str, int]) -> None:
    dead = UpstreamProxy("http", "127.0.0.1", 9, None, None)  # discard port, nothing listens
    with pytest.raises(ProxyError, match="cannot reach upstream"):
        await dead.open(*echo_server)


async def test_fetch_json_and_geo_through_upstream(echo_server: tuple[str, int]) -> None:
    upstream = await FakeSocks5Proxy("bob", "hunter2").start()
    try:
        proxy = UpstreamProxy.parse(upstream.url)
        host, port = echo_server
        payload = await fetch_json_via(proxy, f"http://{host}:{port}/json")
        assert payload["request"] == "GET /json HTTP/1.1"
        geo = await resolve_geo(proxy, f"http://{host}:{port}/geo")
        assert (geo.ip, geo.country, geo.timezone) == ("203.0.113.7", "DE", "Europe/Berlin")
    finally:
        await upstream.stop()


def test_parse_geo_accepts_common_payload_shapes() -> None:
    ipwho = parse_geo(
        {"ip": "1.1.1.1", "country_code": "US", "timezone": {"id": "America/Chicago"}}
    )
    assert (ipwho.country, ipwho.timezone) == ("US", "America/Chicago")
    ipapi = parse_geo({"query": "2.2.2.2", "countryCode": "FR", "timezone": "Europe/Paris"})
    assert (ipapi.ip, ipapi.country, ipapi.timezone) == ("2.2.2.2", "FR", "Europe/Paris")
    assert parse_geo({"ip": "3.3.3.3", "country": "Germany"}).country is None
    with pytest.raises(ProxyError):
        parse_geo({"success": False, "message": "quota"})


def test_languages_for_country() -> None:
    assert languages_for_country("de") == ("de-DE", "de", "en-US", "en")
    assert languages_for_country("RU")[0] == "ru-RU"
    assert languages_for_country(None) == ("en-US", "en")
    assert languages_for_country("ZZ") == ("en-US", "en")


def test_stats_dataclass_defaults() -> None:
    stats = GatewayStats()
    assert (stats.connections, stats.failures, stats.targets) == (0, 0, [])
