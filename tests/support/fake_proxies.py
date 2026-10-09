"""Minimal upstream proxies that record what they were asked to do.

They double as an executable specification of what the gateway must send:
``Proxy-Authorization: Basic`` for HTTP CONNECT and RFC 1929 username/password
for SOCKS5.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import struct
from dataclasses import dataclass, field


def _track(tasks: set[asyncio.Task[None]]) -> None:
    task = asyncio.current_task()
    if task is not None:
        tasks.add(task)  # type: ignore[arg-type]
        task.add_done_callback(tasks.discard)  # type: ignore[arg-type]


async def _relay(a_r: asyncio.StreamReader, a_w: asyncio.StreamWriter,
                 b_r: asyncio.StreamReader, b_w: asyncio.StreamWriter) -> None:  # fmt: skip
    async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while data := await src.read(65536):
                dst.write(data)
                await dst.drain()
        except (OSError, ConnectionError):
            pass
        finally:
            with contextlib.suppress(OSError):
                dst.write_eof()

    await asyncio.gather(pump(a_r, b_w), pump(b_r, a_w))


@dataclass
class FakeHttpConnectProxy:
    username: str | None = None
    password: str | None = None
    connects: list[str] = field(default_factory=list)
    auth_failures: int = 0
    port: int = 0
    _server: asyncio.AbstractServer | None = None
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)

    @property
    def url(self) -> str:
        auth = f"{self.username}:{self.password}@" if self.username else ""
        return f"http://{auth}127.0.0.1:{self.port}"

    async def start(self) -> FakeHttpConnectProxy:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._server:
            self._server.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._server.wait_closed(), 2)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        _track(self._tasks)
        try:
            request_line = (await reader.readline()).decode()
            headers: dict[str, str] = {}
            while (line := (await reader.readline()).decode()) not in ("\r\n", "\n", ""):
                key, _, value = line.partition(":")
                headers[key.strip().lower()] = value.strip()
            method, target, _ = request_line.split(" ", 2)
            if method != "CONNECT":
                writer.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                return
            if self.username:
                expected = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
                if headers.get("proxy-authorization") != f"Basic {expected}":
                    self.auth_failures += 1
                    writer.write(
                        b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                        b'Proxy-Authenticate: Basic realm="fake"\r\n\r\n'
                    )
                    return
            self.connects.append(target)
            host, _, port = target.rpartition(":")
            up_r, up_w = await asyncio.open_connection(host.strip("[]"), int(port))
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await _relay(reader, writer, up_r, up_w)
            up_w.close()
        except (OSError, ValueError, asyncio.IncompleteReadError):
            pass
        finally:
            with contextlib.suppress(Exception):
                await writer.drain()
            writer.close()


@dataclass
class FakeSocks5Proxy:
    username: str | None = None
    password: str | None = None
    connects: list[str] = field(default_factory=list)
    auth_failures: int = 0
    port: int = 0
    _server: asyncio.AbstractServer | None = None
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)

    @property
    def url(self) -> str:
        auth = f"{self.username}:{self.password}@" if self.username else ""
        return f"socks5://{auth}127.0.0.1:{self.port}"

    async def start(self) -> FakeSocks5Proxy:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._server:
            self._server.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._server.wait_closed(), 2)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        _track(self._tasks)
        try:
            _, count = await reader.readexactly(2)
            methods = await reader.readexactly(count)
            if self.username:
                if 0x02 not in methods:
                    writer.write(b"\x05\xff")
                    return
                writer.write(b"\x05\x02")
                await writer.drain()
                _, ulen = await reader.readexactly(2)
                user = (await reader.readexactly(ulen)).decode()
                plen = (await reader.readexactly(1))[0]
                password = (await reader.readexactly(plen)).decode()
                if (user, password) != (self.username, self.password):
                    self.auth_failures += 1
                    writer.write(b"\x01\x01")
                    return
                writer.write(b"\x01\x00")
            else:
                writer.write(b"\x05\x00")
            await writer.drain()
            _, cmd, _, atyp = await reader.readexactly(4)
            if atyp == 1:
                host = str(ipaddress.IPv4Address(await reader.readexactly(4)))
            elif atyp == 3:
                length = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(length)).decode()
            else:
                host = str(ipaddress.IPv6Address(await reader.readexactly(16)))
            port = struct.unpack("!H", await reader.readexactly(2))[0]
            if cmd != 1:
                writer.write(b"\x05\x07\x00\x01" + bytes(6))
                return
            self.connects.append(f"{host}:{port}")
            up_r, up_w = await asyncio.open_connection(host, port)
            writer.write(b"\x05\x00\x00\x01" + bytes(6))
            await writer.drain()
            await _relay(reader, writer, up_r, up_w)
            up_w.close()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            with contextlib.suppress(Exception):
                await writer.drain()
            writer.close()


async def socks5_connect(
    gateway_host: str, gateway_port: int, target_host: str, target_port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Plain SOCKS5 (no auth, domain address) client used to talk to the gateway."""
    reader, writer = await asyncio.open_connection(gateway_host, gateway_port)
    writer.write(b"\x05\x01\x00")
    await writer.drain()
    assert await reader.readexactly(2) == b"\x05\x00"
    encoded = target_host.encode()
    writer.write(
        b"\x05\x01\x00\x03" + bytes([len(encoded)]) + encoded + struct.pack("!H", target_port)
    )
    await writer.drain()
    header = await reader.readexactly(4)
    if header[1] != 0:
        writer.close()
        raise ConnectionError(f"gateway reply {header[1]}")
    atyp = header[3]
    await reader.readexactly({1: 4, 4: 16}.get(atyp, 0) or (await reader.readexactly(1))[0])
    await reader.readexactly(2)
    return reader, writer
