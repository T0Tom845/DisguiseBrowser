"""Proxy Gateway: a loopback SOCKS5 server for Chrome that tunnels to an
upstream HTTP(S) or SOCKS5 proxy, adding the credentials Chrome cannot send.

Why a gateway instead of ``--proxy-server`` with credentials or CDP
``Fetch.authRequired``:

* Chrome ignores ``user:pass@`` in ``--proxy-server``; the only built-in way is
  the auth dialog or ``Fetch`` interception, and interception shows up in
  Resource Timing.
* Chrome talks SOCKS5 to us, so it never emits ``Proxy-Connection``/
  ``Proxy-Authorization`` headers itself and resolves DNS remotely
  (hostnames are forwarded to the proxy — no local DNS leak).
* The same tunnel is used to look up the exit IP's geolocation so that the
  Persona's timezone and languages can be aligned with the proxy (``geoip``).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import json
import logging
import os
import ssl
import struct
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from disguisebrowser.errors import DisguiseError

log = logging.getLogger(__name__)

SOCKS_VERSION = 5
_NO_AUTH = 0x00
_USERNAME_PASSWORD = 0x02
_NO_ACCEPTABLE = 0xFF
_CMD_CONNECT = 0x01
_ATYP_IPV4 = 0x01
_ATYP_DOMAIN = 0x03
_ATYP_IPV6 = 0x04
_REPLY_SUCCESS = 0x00
_REPLY_GENERAL_FAILURE = 0x01
_REPLY_CMD_NOT_SUPPORTED = 0x07
_REPLY_ATYP_NOT_SUPPORTED = 0x08

DEFAULT_GEOIP_URL = "https://ipwho.is/"
"""Returns ``ip``, ``country_code`` and ``timezone.id`` as JSON; overridable via
``DISGUISE_GEOIP_URL`` (any endpoint returning ip / country / timezone fields)."""

CONNECT_TIMEOUT = 20.0
RELAY_BUFFER = 64 * 1024


class ProxyError(DisguiseError):
    """The upstream proxy refused or failed a connection."""


@dataclass(frozen=True)
class UpstreamProxy:
    scheme: str  # http | https | socks5 | socks5h
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    @classmethod
    def parse(cls, url: str) -> UpstreamProxy:
        parts = urlsplit(url if "://" in url else f"http://{url}")
        scheme = parts.scheme.lower()
        if scheme == "socks5h":
            scheme = "socks5"
        if scheme not in ("http", "https", "socks5"):
            raise ProxyError(f"unsupported upstream proxy scheme {parts.scheme!r}")
        if not parts.hostname or not parts.port:
            raise ProxyError(f"proxy must be scheme://[user:pass@]host:port, got {url!r}")
        return cls(scheme, parts.hostname, parts.port, parts.username, parts.password)

    @property
    def has_credentials(self) -> bool:
        return bool(self.username)

    async def open(self, host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open a TCP tunnel to ``host:port`` through the upstream proxy."""
        if self.scheme == "socks5":
            return await self._open_socks5(host, port)
        return await self._open_http_connect(host, port)

    async def _connect_upstream(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        ssl_context = ssl.create_default_context() if self.scheme == "https" else None
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(
                    self.host,
                    self.port,
                    ssl=ssl_context,
                    server_hostname=self.host if ssl_context else None,
                ),
                CONNECT_TIMEOUT,
            )
        except (OSError, TimeoutError) as exc:
            raise ProxyError(f"cannot reach upstream proxy {self.host}:{self.port}: {exc}") from exc

    async def _open_http_connect(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await self._connect_upstream()
        target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        request = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
        if self.username is not None:
            token = base64.b64encode(f"{self.username}:{self.password or ''}".encode()).decode()
            request.append(f"Proxy-Authorization: Basic {token}")
        writer.write(("\r\n".join(request) + "\r\n\r\n").encode())
        try:
            await writer.drain()
            status_line = await asyncio.wait_for(reader.readline(), CONNECT_TIMEOUT)
            while True:  # consume response headers
                line = await asyncio.wait_for(reader.readline(), CONNECT_TIMEOUT)
                if line in (b"\r\n", b"\n", b""):
                    break
        except (OSError, TimeoutError) as exc:
            writer.close()
            raise ProxyError(f"upstream CONNECT to {target} failed: {exc}") from exc
        parts = status_line.decode(errors="replace").split()
        if len(parts) < 2 or not parts[1].startswith("2"):
            writer.close()
            status = status_line.decode(errors="replace").strip()
            raise ProxyError(f"upstream proxy refused CONNECT {target}: {status}")
        return reader, writer

    async def _open_socks5(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await self._connect_upstream()
        try:
            methods = (
                bytes([_USERNAME_PASSWORD, _NO_AUTH]) if self.has_credentials else bytes([_NO_AUTH])
            )
            writer.write(bytes([SOCKS_VERSION, len(methods)]) + methods)
            await writer.drain()
            version, method = await asyncio.wait_for(reader.readexactly(2), CONNECT_TIMEOUT)
            if version != SOCKS_VERSION or method == _NO_ACCEPTABLE:
                raise ProxyError("upstream SOCKS5 proxy accepted none of our auth methods")
            if method == _USERNAME_PASSWORD:
                user = (self.username or "").encode()
                password = (self.password or "").encode()
                writer.write(bytes([1, len(user)]) + user + bytes([len(password)]) + password)
                await writer.drain()
                _, status = await asyncio.wait_for(reader.readexactly(2), CONNECT_TIMEOUT)
                if status != 0:
                    raise ProxyError("upstream SOCKS5 proxy rejected the credentials")
            writer.write(bytes([SOCKS_VERSION, _CMD_CONNECT, 0]) + _encode_address(host, port))
            await writer.drain()
            header = await asyncio.wait_for(reader.readexactly(4), CONNECT_TIMEOUT)
            if header[1] != _REPLY_SUCCESS:
                raise ProxyError(
                    f"upstream SOCKS5 CONNECT to {host}:{port} failed (reply {header[1]})"
                )
            await _read_address(reader, header[3])
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as exc:
            writer.close()
            raise ProxyError(f"upstream SOCKS5 handshake failed: {exc}") from exc
        except ProxyError:
            writer.close()
            raise
        return reader, writer


def _encode_address(host: str, port: int) -> bytes:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        encoded = host.encode("idna")
        return bytes([_ATYP_DOMAIN, len(encoded)]) + encoded + struct.pack("!H", port)
    if ip.version == 4:
        return bytes([_ATYP_IPV4]) + ip.packed + struct.pack("!H", port)
    return bytes([_ATYP_IPV6]) + ip.packed + struct.pack("!H", port)


async def _read_address(reader: asyncio.StreamReader, atyp: int) -> tuple[str, int]:
    if atyp == _ATYP_IPV4:
        raw = await reader.readexactly(4)
        host = str(ipaddress.IPv4Address(raw))
    elif atyp == _ATYP_IPV6:
        raw = await reader.readexactly(16)
        host = str(ipaddress.IPv6Address(raw))
    elif atyp == _ATYP_DOMAIN:
        length = (await reader.readexactly(1))[0]
        host = (await reader.readexactly(length)).decode("idna")
    else:
        raise ProxyError(f"unsupported SOCKS5 address type {atyp}")
    port = struct.unpack("!H", await reader.readexactly(2))[0]
    return host, int(port)


@dataclass
class GatewayStats:
    connections: int = 0
    failures: int = 0
    bytes_up: int = 0
    bytes_down: int = 0
    targets: list[str] = field(default_factory=list)


class ProxyGateway:
    """Loopback SOCKS5 server (no auth) that forwards every CONNECT upstream."""

    def __init__(self, upstream: UpstreamProxy, *, host: str = "127.0.0.1") -> None:
        self.upstream = upstream
        self.host = host
        self.port: int | None = None
        self.stats = GatewayStats()
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def url(self) -> str:
        if self.port is None:
            raise ProxyError("gateway is not started")
        return f"socks5://{self.host}:{self.port}"

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._handle_client, self.host, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("proxy gateway %s → %s://%s:%s", self.url, self.upstream.scheme,
                 self.upstream.host, self.upstream.port)  # fmt: skip

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        for task in list(self._tasks):
            task.cancel()
        with contextlib.suppress(Exception):
            await self._server.wait_closed()
        self._server = None
        self.port = None

    async def __aenter__(self) -> ProxyGateway:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            target = await self._socks5_handshake(reader, writer)
            if target is None:
                return
            host, port = target
            self.stats.connections += 1
            self.stats.targets.append(f"{host}:{port}")
            try:
                upstream_reader, upstream_writer = await self.upstream.open(host, port)
            except ProxyError as exc:
                self.stats.failures += 1
                log.warning("gateway: %s", exc)
                writer.write(_reply(_REPLY_GENERAL_FAILURE))
                await writer.drain()
                return
            writer.write(_reply(_REPLY_SUCCESS))
            await writer.drain()
            await self._relay(reader, writer, upstream_reader, upstream_writer)
        except (OSError, asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            for w in (writer, upstream_writer):
                if w is not None:
                    w.close()

    async def _socks5_handshake(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> tuple[str, int] | None:
        version, count = await reader.readexactly(2)
        if version != SOCKS_VERSION:
            return None
        methods = await reader.readexactly(count)
        if _NO_AUTH not in methods:
            writer.write(bytes([SOCKS_VERSION, _NO_ACCEPTABLE]))
            await writer.drain()
            return None
        writer.write(bytes([SOCKS_VERSION, _NO_AUTH]))
        await writer.drain()
        version, command, _, atyp = await reader.readexactly(4)
        if version != SOCKS_VERSION:
            return None
        try:
            host, port = await _read_address(reader, atyp)
        except ProxyError:
            writer.write(_reply(_REPLY_ATYP_NOT_SUPPORTED))
            await writer.drain()
            return None
        if command != _CMD_CONNECT:  # UDP ASSOCIATE / BIND: Chrome does not need them
            writer.write(_reply(_REPLY_CMD_NOT_SUPPORTED))
            await writer.drain()
            return None
        return host, port

    async def _relay(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        async def pump(
            source: asyncio.StreamReader, sink: asyncio.StreamWriter, counter: str
        ) -> None:
            try:
                while chunk := await source.read(RELAY_BUFFER):
                    sink.write(chunk)
                    setattr(self.stats, counter, getattr(self.stats, counter) + len(chunk))
                    await sink.drain()
            except (OSError, ConnectionError):
                pass
            finally:
                with contextlib.suppress(OSError):
                    sink.write_eof()

        await asyncio.gather(
            pump(client_reader, upstream_writer, "bytes_up"),
            pump(upstream_reader, client_writer, "bytes_down"),
        )


def _reply(code: int) -> bytes:
    return bytes([SOCKS_VERSION, code, 0, _ATYP_IPV4, 0, 0, 0, 0, 0, 0])


# ---------------------------------------------------------------------- geolocation
@dataclass(frozen=True)
class GeoInfo:
    ip: str
    country: str | None
    timezone: str | None
    raw: dict[str, Any] = field(default_factory=dict)


async def fetch_json_via(
    upstream: UpstreamProxy, url: str, *, timeout: float = 15.0
) -> dict[str, Any]:
    """Minimal HTTP/1.1 GET through the upstream tunnel (TLS via start_tls)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ProxyError(f"unsupported geo URL {url!r}")
    secure = parts.scheme == "https"
    port = parts.port or (443 if secure else 80)
    reader, writer = await upstream.open(parts.hostname, port)
    try:
        if secure:
            await asyncio.wait_for(
                writer.start_tls(ssl.create_default_context(), server_hostname=parts.hostname),
                timeout,
            )
        path = parts.path or "/"
        if parts.query:
            path += f"?{parts.query}"
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {parts.hostname}\r\nAccept: application/json\r\n"
            "User-Agent: Mozilla/5.0\r\nConnection: close\r\n\r\n"
        )
        writer.write(request.encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), timeout)
    finally:
        writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    status = head.split(b" ", 2)
    if len(status) < 2 or not status[1].startswith(b"2"):
        raise ProxyError(f"geo lookup {url} failed: {head.splitlines()[0]!r}")
    if b"transfer-encoding: chunked" in head.lower():
        body = _dechunk(body)
    try:
        payload: dict[str, Any] = json.loads(body.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProxyError(f"geo lookup {url} returned non-JSON: {exc}") from exc
    return payload


def _dechunk(body: bytes) -> bytes:
    out = bytearray()
    while body:
        size_line, _, body = body.partition(b"\r\n")
        try:
            size = int(size_line.split(b";")[0].strip() or b"0", 16)
        except ValueError:
            break
        if size == 0:
            break
        out += body[:size]
        body = body[size + 2 :]
    return bytes(out)


def parse_geo(payload: dict[str, Any]) -> GeoInfo:
    """Accepts ipwho.is, ip-api.com and ipinfo-style payloads."""
    if payload.get("success") is False:
        raise ProxyError(f"geo provider error: {payload.get('message', payload)}")
    timezone = payload.get("timezone")
    if isinstance(timezone, dict):
        timezone = timezone.get("id") or timezone.get("name")
    ip = payload.get("ip") or payload.get("query") or ""
    country = payload.get("country_code") or payload.get("countryCode") or payload.get("country")
    if isinstance(country, str) and len(country) != 2:
        country = None
    return GeoInfo(
        str(ip), country.upper() if isinstance(country, str) else None, timezone, payload
    )


async def resolve_geo(upstream: UpstreamProxy, url: str | None = None) -> GeoInfo:
    endpoint = url or os.environ.get("DISGUISE_GEOIP_URL") or DEFAULT_GEOIP_URL
    return parse_geo(await fetch_json_via(upstream, endpoint))


COUNTRY_LANGUAGES: dict[str, tuple[str, ...]] = {
    "US": ("en-US", "en"),
    "GB": ("en-GB", "en"),
    "CA": ("en-CA", "en", "fr-CA", "fr"),
    "AU": ("en-AU", "en"),
    "IE": ("en-IE", "en"),
    "NZ": ("en-NZ", "en"),
    "DE": ("de-DE", "de", "en-US", "en"),
    "AT": ("de-AT", "de", "en-US", "en"),
    "CH": ("de-CH", "de", "fr-CH", "fr", "en-US", "en"),
    "FR": ("fr-FR", "fr", "en-US", "en"),
    "BE": ("nl-BE", "nl", "fr-BE", "fr", "en-US", "en"),
    "NL": ("nl-NL", "nl", "en-US", "en"),
    "ES": ("es-ES", "es", "en-US", "en"),
    "MX": ("es-MX", "es", "en-US", "en"),
    "AR": ("es-AR", "es", "en-US", "en"),
    "IT": ("it-IT", "it", "en-US", "en"),
    "PT": ("pt-PT", "pt", "en-US", "en"),
    "BR": ("pt-BR", "pt", "en-US", "en"),
    "RU": ("ru-RU", "ru", "en-US", "en"),
    "UA": ("uk-UA", "uk", "ru", "en-US", "en"),
    "BY": ("ru-RU", "ru", "be", "en-US", "en"),
    "KZ": ("ru-RU", "ru", "kk", "en-US", "en"),
    "PL": ("pl-PL", "pl", "en-US", "en"),
    "CZ": ("cs-CZ", "cs", "en-US", "en"),
    "SE": ("sv-SE", "sv", "en-US", "en"),
    "NO": ("nb-NO", "nb", "en-US", "en"),
    "DK": ("da-DK", "da", "en-US", "en"),
    "FI": ("fi-FI", "fi", "en-US", "en"),
    "TR": ("tr-TR", "tr", "en-US", "en"),
    "JP": ("ja-JP", "ja", "en-US", "en"),
    "KR": ("ko-KR", "ko", "en-US", "en"),
    "CN": ("zh-CN", "zh", "en-US", "en"),
    "TW": ("zh-TW", "zh", "en-US", "en"),
    "IN": ("en-IN", "en", "hi"),
    "ID": ("id-ID", "id", "en-US", "en"),
    "VN": ("vi-VN", "vi", "en-US", "en"),
    "TH": ("th-TH", "th", "en-US", "en"),
    "IL": ("he-IL", "he", "en-US", "en"),
    "AE": ("ar-AE", "ar", "en-US", "en"),
    "SA": ("ar-SA", "ar", "en-US", "en"),
    "ZA": ("en-ZA", "en"),
    "SG": ("en-SG", "en", "zh-CN", "zh"),
}


def languages_for_country(country: str | None) -> tuple[str, ...]:
    if country and country.upper() in COUNTRY_LANGUAGES:
        return COUNTRY_LANGUAGES[country.upper()]
    return ("en-US", "en")
