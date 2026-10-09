"""Virtual desktop for servers: Xvfb with GLX, an optional window manager, an
EWMH work-area shim and a GPU identity applied at the Mesa driver level (no
JavaScript involved).

Why not headless? Headless Chrome announces itself (``HeadlessChrome`` UA,
800x600 screen without a work area, DPR 1, no GPU). A real X server with a
software GL stack looks like an ordinary Linux desktop instead — see ADR-4/5.

Several displays can run side by side in one process (one per browser), so
display numbers are reserved process-wide and Xvfb start-up is retried when
another process grabbed the same number first.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
import tempfile
from pathlib import Path
from xml.sax.saxutils import quoteattr

from Xlib import Xatom
from Xlib import display as xlib_display

from disguisebrowser.errors import DisplayError
from disguisebrowser.persona import GpuIdentity, Screen, Window
from disguisebrowser.process import die_with_parent

log = logging.getLogger(__name__)

SYSTEM_DRIRC_DIR = Path("/usr/share/drirc.d")
WINDOW_MANAGERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("openbox", ("--sm-disable",)),
    ("fluxbox", ()),
    ("matchbox-window-manager", ("-use_titlebar", "no")),
    ("xfwm4", ("--sm-client-disable",)),
    ("icewm", ()),
)
_START_ATTEMPTS = 5
_reserved_displays: set[int] = set()


class MesaIdentity:
    """Materializes a driconf directory that makes Mesa report the given GPU.

    ``DRIRC_CONFIGDIR`` *replaces* the system ``drirc.d`` directory, so the
    system defaults are copied alongside our override to keep Chrome's regular
    driver workarounds (``adaptive_sync``, ``no_fp16`` …) in effect.
    """

    def __init__(self, gpu: GpuIdentity, system_dir: Path = SYSTEM_DRIRC_DIR) -> None:
        self.gpu = gpu
        self.system_dir = system_dir

    def render(self) -> str:
        return (
            '<?xml version="1.0" standalone="yes"?>\n'
            '<!DOCTYPE driconf SYSTEM "driconf.dtd">\n'
            "<driconf>\n"
            "  <device>\n"
            '    <application name="all">\n'
            f'      <option name="force_gl_vendor" value={quoteattr(self.gpu.vendor)}/>\n'
            f'      <option name="force_gl_renderer" value={quoteattr(self.gpu.renderer)}/>\n'
            "    </application>\n"
            "  </device>\n"
            "</driconf>\n"
        )

    def materialize(self, into: Path) -> dict[str, str]:
        into.mkdir(parents=True, exist_ok=True)
        if self.system_dir.is_dir():
            for conf in sorted(self.system_dir.glob("*.conf")):
                shutil.copy(conf, into / conf.name)
        (into / "99-disguisebrowser.conf").write_text(self.render())
        return {
            "DRIRC_CONFIGDIR": str(into),
            "MESA_GL_VERSION_OVERRIDE": self.gpu.gl_version,
        }


def publish_work_area(display_name: str, screen: Screen, work_area: Window) -> None:
    """Set the EWMH root-window properties Chrome reads for ``screen.avail*``.

    Chrome only requires ``_NET_SUPPORTED`` to list ``_NET_WORKAREA`` and a
    current desktop index; no window manager has to be running. We deliberately
    do not advertise ``_NET_ACTIVE_WINDOW`` so Chrome keeps focusing its own
    windows via ``XSetInputFocus``.
    """
    conn = xlib_display.Display(display_name)
    try:
        root = conn.screen().root
        atom = conn.intern_atom
        supported = [
            atom(name)
            for name in (
                "_NET_SUPPORTED",
                "_NET_WORKAREA",
                "_NET_NUMBER_OF_DESKTOPS",
                "_NET_CURRENT_DESKTOP",
                "_NET_DESKTOP_GEOMETRY",
                "_NET_DESKTOP_VIEWPORT",
            )
        ]
        root.change_property(atom("_NET_SUPPORTED"), Xatom.ATOM, 32, supported)
        root.change_property(atom("_NET_NUMBER_OF_DESKTOPS"), Xatom.CARDINAL, 32, [1])
        root.change_property(atom("_NET_CURRENT_DESKTOP"), Xatom.CARDINAL, 32, [0])
        root.change_property(
            atom("_NET_DESKTOP_GEOMETRY"), Xatom.CARDINAL, 32, [screen.width, screen.height]
        )
        root.change_property(atom("_NET_DESKTOP_VIEWPORT"), Xatom.CARDINAL, 32, [0, 0])
        root.change_property(
            atom("_NET_WORKAREA"),
            Xatom.CARDINAL,
            32,
            [work_area.x, work_area.y, work_area.width, work_area.height],
        )
        conn.sync()
    finally:
        conn.close()


class VirtualDisplay:
    """An Xvfb server sized from the persona's screen, optionally with a WM."""

    def __init__(
        self,
        screen: Screen,
        *,
        gpu: GpuIdentity | None = None,
        work_area: Window | None = None,
        window_manager: bool = True,
        dpi: int = 96,
    ) -> None:
        self.screen = screen
        self.gpu = gpu
        self.work_area = work_area
        self.window_manager = window_manager
        self.dpi = dpi
        self.display: str | None = None
        self.number: int | None = None
        self._xvfb: asyncio.subprocess.Process | None = None
        self._wm: asyncio.subprocess.Process | None = None
        self._workdir: Path | None = None
        self._env: dict[str, str] = {}

    @staticmethod
    def available() -> bool:
        return shutil.which("Xvfb") is not None

    @staticmethod
    def window_manager_available() -> str | None:
        for name, _ in WINDOW_MANAGERS:
            if shutil.which(name):
                return name
        return None

    async def start(self) -> None:
        if self._xvfb is not None:
            return
        xvfb = shutil.which("Xvfb")
        if xvfb is None:
            raise DisplayError("Xvfb is not installed (apt install xvfb)")
        self._workdir = Path(tempfile.mkdtemp(prefix="disguise-display-"))
        geometry = f"{self.screen.width}x{self.screen.height}x24"
        last_error: DisplayError | None = None
        for _ in range(_START_ATTEMPTS):
            number = _reserve_display_number()
            try:
                self._xvfb = await self._spawn_xvfb(xvfb, number, geometry)
                await self._wait_for_socket(number)
            except DisplayError as exc:
                last_error = exc
                _reserved_displays.discard(number)
                if self._xvfb is not None and self._xvfb.returncode is None:
                    self._xvfb.kill()
                    await self._xvfb.wait()
                self._xvfb = None
                if "already active" in str(exc) or "listening sockets" in str(exc):
                    continue  # lost a race against another process; try the next number
                break
            self.number = number
            self.display = f":{number}"
            break
        if self._xvfb is None:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None
            raise last_error or DisplayError("could not start Xvfb")
        assert self.display is not None
        self._env = {"DISPLAY": self.display, "LIBGL_ALWAYS_SOFTWARE": "1"}
        if self.gpu is not None:
            self._env.update(MesaIdentity(self.gpu).materialize(self._workdir / "drirc.d"))
        if self.window_manager:
            await self._start_window_manager()
        if self._wm is None and self.work_area is not None:
            await asyncio.to_thread(publish_work_area, self.display, self.screen, self.work_area)
        log.info("virtual display %s ready (%s)", self.display, geometry)

    async def _spawn_xvfb(
        self, xvfb: str, number: int, geometry: str
    ) -> asyncio.subprocess.Process:
        cmd = [
            xvfb,
            f":{number}",
            "-screen", "0", geometry,
            "-dpi", str(self.dpi),
            "+extension", "GLX",
            "+extension", "RANDR",
            "+render",
            "-nolisten", "tcp",
            "-noreset",
        ]  # fmt: skip
        return await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=die_with_parent,
        )

    async def _wait_for_socket(self, number: int, timeout: float = 10.0) -> None:
        socket_path = Path(f"/tmp/.X11-unix/X{number}")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        assert self._xvfb is not None
        while loop.time() < deadline:
            if socket_path.exists():  # noqa: ASYNC240 — a local stat() call, microseconds
                return
            if self._xvfb.returncode is not None:
                stderr = b""
                if self._xvfb.stderr is not None:
                    stderr = await self._xvfb.stderr.read()
                raise DisplayError(
                    f"Xvfb exited with {self._xvfb.returncode}: {stderr.decode(errors='replace')}"
                )
            await asyncio.sleep(0.05)
        raise DisplayError(f"Xvfb did not create {socket_path} within {timeout}s")

    async def _start_window_manager(self) -> None:
        for name, args in WINDOW_MANAGERS:
            path = shutil.which(name)
            if path is None:
                continue
            self._wm = await asyncio.create_subprocess_exec(
                path,
                *args,
                env={**os.environ, **self._env},
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                preexec_fn=die_with_parent,
            )
            log.info(
                "window manager %s started on %s (it owns _NET_WORKAREA; add a panel such as "
                "tint2 for a desktop-like work area)",
                name,
                self.display,
            )
            return
        log.debug("no window manager found; publishing the work area ourselves")

    def environment(self) -> dict[str, str]:
        """Environment variables Chrome must inherit to use this display."""
        if self.display is None:
            raise DisplayError("virtual display is not started")
        return dict(self._env)

    @property
    def chrome_args(self) -> tuple[str, ...]:
        # Software GL is blocklisted by default; without this WebGL is absent — a bot tell.
        return ("--ignore-gpu-blocklist",)

    async def stop(self) -> None:
        for proc in (self._wm, self._xvfb):
            if proc is not None and proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
        self._wm = None
        self._xvfb = None
        if self._workdir is not None:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None
        if self.number is not None:
            _reserved_displays.discard(self.number)
        self.display = None
        self.number = None
        self._env = {}

    async def __aenter__(self) -> VirtualDisplay:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()


def _display_in_use(number: int) -> bool:
    if Path(f"/tmp/.X11-unix/X{number}").exists() or Path(f"/tmp/.X{number}-lock").exists():
        return True
    # X servers also listen on a Linux abstract socket, which leaves no file behind
    # (e.g. a server started in another mount namespace).
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        try:
            probe.connect(f"\0/tmp/.X11-unix/X{number}")
        except OSError:
            return False
        return True


def _reserve_display_number(start: int = 99, end: int = 999) -> int:
    """Pick a display number no other display *in this process* holds.

    Runs without awaiting, so concurrent ``start()`` calls in one event loop
    cannot interleave here; cross-process races are handled by retrying in
    :meth:`VirtualDisplay.start`.
    """
    for number in range(start, end):
        if number in _reserved_displays or _display_in_use(number):
            continue
        _reserved_displays.add(number)
        return number
    raise DisplayError("no free X display number between :99 and :999")
