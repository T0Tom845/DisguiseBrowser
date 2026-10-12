"""Local HTTP server for the fingerprint capture page.

Serves ``capture.html``/``capture.js``, embeds the navigation request headers
into the page, echoes request headers on ``/echo`` and receives the finished
record on ``/submit``. Every response carries ``Accept-CH``/``Critical-CH`` so
Chrome sends the high-entropy Client Hints a real site could ask for.

A thread-based ``http.server`` keeps this free of extra dependencies; the async
side waits on a queue through ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

ACCEPT_CH = ", ".join(
    (
        "Sec-CH-UA-Arch",
        "Sec-CH-UA-Bitness",
        "Sec-CH-UA-Full-Version",
        "Sec-CH-UA-Full-Version-List",
        "Sec-CH-UA-Model",
        "Sec-CH-UA-Platform-Version",
        "Sec-CH-UA-WoW64",
        "Sec-CH-UA-Form-Factors",
        "Sec-CH-Prefers-Color-Scheme",
        "Sec-CH-Prefers-Reduced-Motion",
        "Sec-CH-Prefers-Reduced-Transparency",
        "Device-Memory",
        "DPR",
        "Viewport-Width",
        "Width",
        "Downlink",
        "ECT",
        "RTT",
        "Save-Data",
    )
)
CRITICAL_CH = ", ".join(
    (
        "Sec-CH-UA-Arch",
        "Sec-CH-UA-Bitness",
        "Sec-CH-UA-Full-Version-List",
        "Sec-CH-UA-Model",
        "Sec-CH-UA-Platform-Version",
        "Sec-CH-UA-WoW64",
        "Sec-CH-UA-Form-Factors",
    )
)


def _asset(name: str) -> str:
    return resources.files("disguisebrowser.fingerprint").joinpath(name).read_text(encoding="utf-8")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "disguise-capture"
    sys_version = ""

    def __init__(self, *args: Any, submissions: queue.Queue[dict[str, Any]], **kwargs: Any) -> None:
        self._submissions = submissions
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: object) -> None:
        log.debug("capture server: " + format, *args)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Accept-CH", ACCEPT_CH)
        self.send_header("Critical-CH", CRITICAL_CH)
        self.send_header("Permissions-Policy", "local-fonts=(self)")
        self.end_headers()
        self.wfile.write(body)

    def _header_pairs(self) -> list[list[str]]:
        return [[k, v] for k, v in self.headers.items()]

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path in ("/", "/index.html", "/capture.html"):
            headers_json = json.dumps(self._header_pairs()).replace("<", "\\u003c")
            html = _asset("capture.html").replace("__NAV_HEADERS__", headers_json)
            self._send(HTTPStatus.OK, html.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/capture.js":
            self._send(
                HTTPStatus.OK,
                _asset("capture.js").encode("utf-8"),
                "text/javascript; charset=utf-8",
            )
        elif path == "/echo":
            body = json.dumps(self._header_pairs()).encode("utf-8")
            self._send(HTTPStatus.OK, body, "application/json")
        else:
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if path != "/submit":
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            return
        try:
            record = json.loads(raw)
            if not isinstance(record, dict):
                raise ValueError("record must be an object")
        except ValueError as exc:
            self._send(HTTPStatus.BAD_REQUEST, str(exc).encode(), "text/plain")
            return
        record.setdefault("meta", {})["remoteAddress"] = self.client_address[0]
        self._submissions.put(record)
        self._send(HTTPStatus.NO_CONTENT, b"", "text/plain")


class CaptureServer:
    """Serve the capture page and collect submitted records."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._host = host
        self._port = port
        self._submissions: queue.Queue[dict[str, Any]] = queue.Queue()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        handler = partial(_Handler, submissions=self._submissions)
        self._server = ThreadingHTTPServer((self._host, self._port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="capture-server", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("server is not running")
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        """Loopback URL for a browser on this machine (a secure context)."""
        return f"http://127.0.0.1:{self.port}/"

    def wait_sync(self, timeout: float | None) -> dict[str, Any]:
        return self._submissions.get(timeout=timeout)

    async def wait(self, timeout: float = 120.0) -> dict[str, Any]:
        """Next submitted record; raises ``TimeoutError`` if none arrives."""
        try:
            return await asyncio.to_thread(self.wait_sync, timeout)
        except queue.Empty:
            raise TimeoutError(f"no fingerprint was submitted within {timeout:.0f}s") from None

    def __enter__(self) -> CaptureServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
