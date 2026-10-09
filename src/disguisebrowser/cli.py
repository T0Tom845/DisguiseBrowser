"""``disguise`` command line: doctor, test (scorecard), open."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from disguisebrowser import __version__
from disguisebrowser.browser import launch
from disguisebrowser.display import SYSTEM_DRIRC_DIR, VirtualDisplay
from disguisebrowser.errors import DisguiseError, LaunchError
from disguisebrowser.input import PRESETS
from disguisebrowser.launcher import chrome_version, find_chrome
from disguisebrowser.persona import GPU_PRESETS, Persona
from disguisebrowser.scorecard import DETECTORS, format_report, run_scorecard, to_json


def _check(label: str, ok: bool, detail: str, hint: str = "") -> dict[str, Any]:
    return {"check": label, "ok": ok, "detail": detail, "hint": hint}


def doctor_checks() -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    try:
        chrome = find_chrome()
        checks.append(_check("chrome", True, f"{chrome} ({chrome_version(chrome)})"))
    except LaunchError as exc:
        checks.append(_check("chrome", False, str(exc), "install Google Chrome stable"))
    checks.append(
        _check(
            "host display",
            bool(os.environ.get("DISPLAY")),
            os.environ.get("DISPLAY") or "no $DISPLAY — virtual display will be used",
        )
    )
    checks.append(
        _check(
            "xvfb",
            VirtualDisplay.available(),
            shutil.which("Xvfb") or "missing",
            "apt install xvfb (needed for display=virtual)",
        )
    )
    wm = VirtualDisplay.window_manager_available()
    checks.append(
        _check(
            "window manager",
            wm is not None,
            wm or "none found",
            "apt install openbox — gives focus/work-area semantics on the virtual desktop",
        )
    )
    checks.append(
        _check(
            "mesa drirc",
            SYSTEM_DRIRC_DIR.is_dir(),
            str(SYSTEM_DRIRC_DIR) if SYSTEM_DRIRC_DIR.is_dir() else "missing",
            "apt install libgl1-mesa-dri — software GL + GPU identity on the virtual desktop",
        )
    )
    glx = _glx_renderer()
    checks.append(
        _check(
            "software GL (llvmpipe)",
            glx is not None and "llvmpipe" in glx,
            glx or "could not probe (needs Xvfb and glxinfo from mesa-utils)",
            "apt install mesa-utils to verify; WebGL must exist or the browser is flagged",
        )
    )
    fonts = _font_check()
    checks.append(
        _check(
            "fonts",
            fonts["emoji"] and fonts["total"] > 100,
            f"{fonts['total']} families, emoji={'yes' if fonts['emoji'] else 'no'}",
            "apt install fonts-noto-color-emoji fonts-noto-cjk fonts-liberation",
        )
    )
    persona = Persona.from_host()
    checks.append(
        _check(
            "host persona",
            not persona.validate(),
            f"languages={','.join(persona.languages)} timezone={persona.timezone} "
            f"screen={persona.screen.width}x{persona.screen.height}",
            "; ".join(persona.validate()),
        )
    )
    return checks


def _glx_renderer() -> str | None:
    if not shutil.which("glxinfo") or not shutil.which("xvfb-run"):
        return None
    try:
        out = subprocess.run(
            ["xvfb-run", "-a", "-s", "-screen 0 640x480x24 +extension GLX", "glxinfo", "-B"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env={**os.environ, "LIBGL_ALWAYS_SOFTWARE": "1"},
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        if "renderer string" in line:
            return line.split(":", 1)[1].strip()
    return None


def _font_check() -> dict[str, Any]:
    if not shutil.which("fc-list"):
        return {"total": 0, "emoji": False}
    try:
        out = subprocess.run(
            ["fc-list", ":", "family"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {"total": 0, "emoji": False}
    families = {line.strip() for line in out.splitlines() if line.strip()}
    return {"total": len(families), "emoji": any("Emoji" in f for f in families)}


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = doctor_checks()
    if args.json:
        print(json.dumps(checks, ensure_ascii=False, indent=2))
    else:
        for check in checks:
            mark = "ok " if check["ok"] else "!! "
            line = f"{mark} {check['check']:<22} {check['detail']}"
            if not check["ok"] and check["hint"]:
                line += f"\n    → {check['hint']}"
            print(line)
    return 0 if all(c["ok"] for c in checks if c["check"] in ("chrome",)) else 1


def _persona_from_args(args: argparse.Namespace) -> Persona:
    persona = Persona.from_host()
    overrides: dict[str, Any] = {}
    if getattr(args, "gpu", None):
        overrides["gpu"] = GPU_PRESETS[args.gpu]
    if getattr(args, "timezone", None):
        overrides["timezone"] = args.timezone
    if getattr(args, "lang", None):
        overrides["languages"] = tuple(args.lang.split(","))
    if getattr(args, "proxy", None):
        overrides["proxy"] = args.proxy
    if getattr(args, "seed", None) is not None:
        overrides["seed"] = args.seed
    return Persona.from_host(**overrides) if overrides else persona


async def _run_test(args: argparse.Namespace) -> int:
    persona = _persona_from_args(args)
    selected = (
        tuple(d for d in DETECTORS if d.name in set(args.only.split(",")))
        if args.only
        else DETECTORS
    )
    async with launch(
        persona=persona,
        display=args.display,
        headless=args.headless,
        user_data_dir=args.profile,
        human=args.human,
    ) as browser:
        page = browser.pages[0] if browser.pages else await browser.new_page()
        env = {
            "chrome": browser.version.get("product"),
            "display": "virtual" if browser.display else ("headless" if args.headless else "host"),
            "gpu": browser.persona.gpu.renderer
            if browser.persona.gpu and browser.display
            else "host",
            "timezone": browser.persona.timezone,
            "languages": ",".join(browser.persona.languages),
        }
        results = await run_scorecard(page, selected, timeout=args.timeout)
    print(to_json(results, env) if args.json else format_report(results, env))
    if args.report:
        await asyncio.to_thread(Path(args.report).write_text, to_json(results, env))
    return 1 if any(r.verdict == "fail" for r in results) else 0


async def _run_open(args: argparse.Namespace) -> int:
    persona = _persona_from_args(args)
    async with launch(
        persona=persona,
        display=args.display,
        headless=args.headless,
        user_data_dir=args.profile,
        human=args.human,
    ) as browser:
        page = browser.pages[0] if browser.pages else await browser.new_page()
        if args.url:
            await page.goto(args.url)
        where = (
            f"virtual display {browser.display.display}" if browser.display else "the host display"
        )
        print(f"{browser.version.get('product')} is running on {where}")
        print(f"profile: {browser.chrome.user_data_dir}")
        print("press Ctrl+C to close")
        with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
            while browser.pages:  # noqa: ASYNC110 — polling the tab list is the intent here
                await asyncio.sleep(0.5)
    return 0


def _add_launch_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--display", choices=("auto", "host", "virtual"), default="auto")
    parser.add_argument("--headless", action="store_true", help="opt-in, weaker stealth")
    parser.add_argument("--profile", help="persistent user-data-dir (default: temporary)")
    parser.add_argument("--human", choices=sorted(PRESETS), default="default")
    parser.add_argument(
        "--gpu", choices=sorted(GPU_PRESETS), help="GPU identity on the virtual display"
    )
    parser.add_argument("--timezone", help="IANA timezone, e.g. Europe/Berlin")
    parser.add_argument("--lang", help="comma-separated languages, e.g. de-DE,de,en-US,en")
    parser.add_argument("--proxy", help="scheme://host:port (no credentials in v0.1)")
    parser.add_argument(
        "--seed", type=int, help="deterministic geometry/GPU for a returning visitor"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="disguise", description="DisguiseBrowser CLI")
    parser.add_argument("--version", action="version", version=f"disguisebrowser {__version__}")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check Chrome, Xvfb, Mesa, fonts and the host persona")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    test = sub.add_parser("test", help="run the detection scorecard")
    _add_launch_options(test)
    test.add_argument("--only", help="comma-separated detector names")
    test.add_argument("--timeout", type=float, default=60.0)
    test.add_argument("--json", action="store_true")
    test.add_argument("--report", help="write JSON report to this path")
    test.set_defaults(func=lambda a: asyncio.run(_run_test(a)))

    open_ = sub.add_parser("open", help="open a stealth browser and keep it running")
    _add_launch_options(open_)
    open_.add_argument("url", nargs="?")
    open_.set_defaults(func=lambda a: asyncio.run(_run_open(a)))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    level = logging.WARNING - 10 * min(args.verbose, 2)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return int(args.func(args))
    except DisguiseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
