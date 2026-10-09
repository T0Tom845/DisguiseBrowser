"""Locating, configuring and starting the real system Chrome.

The launch recipe is deliberately minimal (ADR-1): an explicit DevTools port
on loopback, our own profile directory and a handful of flags any power user
might pass. No ``--enable-automation``, no ``--headless`` unless asked, no
``--disable-blink-features``, no ``--test-type``, no ``--no-sandbox``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from disguisebrowser.errors import LaunchError
from disguisebrowser.persona import Persona
from disguisebrowser.process import die_with_parent

log = logging.getLogger(__name__)

CHROME_CANDIDATES: tuple[str, ...] = (
    "google-chrome",
    "google-chrome-stable",
    "google-chrome-beta",
    "/opt/google/chrome/chrome",
    "chromium",
    "chromium-browser",
    "/snap/bin/chromium",
)


def find_chrome(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the Chrome executable, preferring Google Chrome stable."""
    if explicit is not None:
        path = Path(explicit)
        if path.is_file():
            return path
        found = shutil.which(str(explicit))
        if found:
            return Path(found)
        raise LaunchError(f"Chrome executable not found: {explicit}")
    env = os.environ.get("DISGUISE_CHROME")
    if env:
        return find_chrome(env)
    for candidate in CHROME_CANDIDATES:
        found = shutil.which(candidate) if not candidate.startswith("/") else candidate
        if found and Path(found).is_file():
            return Path(found)
    raise LaunchError(
        "Google Chrome was not found. Install it (https://www.google.com/chrome/) or set "
        "DISGUISE_CHROME=/path/to/chrome"
    )


@lru_cache(maxsize=8)
def chrome_version(executable: Path) -> str:
    """Full version string such as ``154.0.8037.97``."""
    try:
        out = subprocess.run(
            [str(executable), "--version"], capture_output=True, text=True, timeout=15, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LaunchError(f"cannot run {executable} --version: {exc}") from exc
    match = re.search(r"(\d+\.\d+\.\d+\.\d+)", out)
    if not match:
        raise LaunchError(f"unexpected output from {executable} --version: {out!r}")
    return match.group(1)


def headless_user_agent(version: str) -> str:
    """UA a real Linux Chrome of this major version sends (headless adds 'Headless')."""
    major = version.split(".", maxsplit=1)[0]
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


@dataclass
class LaunchConfig:
    executable: Path
    user_data_dir: Path
    persona: Persona
    port: int
    headless: bool = False
    extra_args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    initial_url: str = "about:blank"


def build_args(config: LaunchConfig) -> list[str]:
    persona = config.persona
    window = persona.effective_window()
    args = [
        f"--remote-debugging-port={config.port}",
        f"--user-data-dir={config.user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--password-store=basic",
        f"--lang={persona.primary_language}",
        f"--window-size={window.width},{window.height}",
        f"--window-position={window.x},{window.y}",
    ]
    proxy = persona.proxy_parts()
    if proxy is not None:
        scheme, host, port, username, _ = proxy
        if username:
            # v0.1 has no local auth gateway yet; Chrome cannot take inline credentials.
            raise LaunchError(
                "authenticated proxies are not supported yet (planned Proxy Gateway); "
                "use an IP-whitelisted proxy or a local forwarder"
            )
        args.append(f"--proxy-server={scheme}://{host}:{port}")
    if config.headless:
        args.append("--headless=new")
        args.append(f"--user-agent={headless_user_agent(chrome_version(config.executable))}")
    args.extend(config.extra_args)
    args.append(config.initial_url)
    return args


def default_preferences(persona: Persona) -> dict[str, Any]:
    """Profile preferences that remove first-run UI and keep the identity coherent."""
    webrtc_policy = "disable_non_proxied_udp" if persona.proxy else "default_public_interface_only"
    return {
        "browser": {"check_default_browser": False, "has_seen_welcome_page": True},
        "credentials_enable_autosignin": False,
        "credentials_enable_service": False,
        "intl": {"accept_languages": ",".join(persona.languages)},
        "privacy_sandbox": {
            "m1": {
                "consent_decision_made": True,
                "eea_notice_acknowledged": True,
                "row_notice_acknowledged": True,
            }
        },
        "profile": {"exit_type": "Normal", "exited_cleanly": True},
        "translate": {"enabled": False},
        "webrtc": {
            "ip_handling_policy": webrtc_policy,
            "multiple_routes_enabled": False,
            "nonproxied_udp_enabled": not persona.proxy,
        },
    }


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def prepare_profile(user_data_dir: Path, persona: Persona) -> None:
    """Create/refresh the profile so Chrome starts without dialogs or bubbles."""
    default_dir = user_data_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    (user_data_dir / "First Run").touch(exist_ok=True)
    prefs_path = default_dir / "Preferences"
    existing: dict[str, Any] = {}
    if prefs_path.is_file():
        try:
            existing = json.loads(prefs_path.read_text())
        except json.JSONDecodeError:
            log.warning("Preferences in %s are corrupt; rewriting", user_data_dir)
    prefs_path.write_text(json.dumps(_deep_merge(existing, default_preferences(persona))))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@dataclass
class ChromeProcess:
    process: asyncio.subprocess.Process
    port: int
    ws_url: str
    user_data_dir: Path
    temporary_profile: bool
    stderr_tail: deque[str]

    async def terminate(self, grace: float = 5.0) -> None:
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), grace)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.temporary_profile:
            shutil.rmtree(self.user_data_dir, ignore_errors=True)


async def launch_chrome(
    *,
    persona: Persona,
    executable: str | os.PathLike[str] | None = None,
    user_data_dir: str | os.PathLike[str] | None = None,
    headless: bool = False,
    extra_args: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
    startup_timeout: float = 30.0,
) -> ChromeProcess:
    problems = persona.validate()
    if problems:
        raise LaunchError("persona is not coherent: " + "; ".join(problems))
    exe = find_chrome(executable)
    temporary = user_data_dir is None
    profile = (
        Path(tempfile.mkdtemp(prefix="disguise-profile-"))
        if user_data_dir is None
        else Path(user_data_dir).expanduser().resolve()  # noqa: ASYNC240 — local path only
    )
    await asyncio.to_thread(prepare_profile, profile, persona)
    port = free_port()
    config = LaunchConfig(
        executable=exe,
        user_data_dir=profile,
        persona=persona,
        port=port,
        headless=headless,
        extra_args=extra_args,
        env=dict(env or {}),
    )
    process_env = {**os.environ, **config.env}
    if persona.timezone:
        process_env["TZ"] = persona.timezone
    args = build_args(config)
    log.info("launching %s (port %s, profile %s)", exe, port, profile)
    log.debug("chrome args: %s", " ".join(args))
    process = await asyncio.create_subprocess_exec(
        str(exe),
        *args,
        env=process_env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=die_with_parent,
    )
    tail: deque[str] = deque(maxlen=50)
    assert process.stderr is not None
    drain = asyncio.create_task(_drain(process.stderr, tail), name="chrome-stderr")
    try:
        ws_url = await _discover_ws_url(port, process, startup_timeout)
    except Exception:
        drain.cancel()
        if process.returncode is None:
            process.kill()
            await process.wait()
        if temporary:
            shutil.rmtree(profile, ignore_errors=True)
        raise
    drain.add_done_callback(lambda _: None)
    return ChromeProcess(process, port, ws_url, profile, temporary, tail)


async def _drain(stream: asyncio.StreamReader, tail: deque[str]) -> None:
    try:
        while line := await stream.readline():
            tail.append(line.decode(errors="replace").rstrip())
    except (asyncio.CancelledError, ValueError):
        pass


async def _discover_ws_url(port: int, process: asyncio.subprocess.Process, timeout: float) -> str:
    url = f"http://127.0.0.1:{port}/json/version"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_error: Exception | None = None
    while loop.time() < deadline:
        if process.returncode is not None:
            raise LaunchError(f"Chrome exited early with code {process.returncode}")
        try:
            payload = await asyncio.to_thread(_http_get_json, url)
            ws_url = payload.get("webSocketDebuggerUrl")
            if ws_url:
                return str(ws_url)
        except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
        await asyncio.sleep(0.1)
    raise LaunchError(f"DevTools endpoint {url} did not come up within {timeout}s: {last_error}")


def _http_get_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=1.0) as response:
        data: dict[str, Any] = json.loads(response.read())
        return data
