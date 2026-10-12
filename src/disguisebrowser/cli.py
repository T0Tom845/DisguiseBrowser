"""``disguise`` command line: doctor, test (scorecard), capture/diff, warmup, open, profile, mcp."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
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
from disguisebrowser.browser import LaunchHandle, launch
from disguisebrowser.camoufox.profiles import (
    OSES,
    Profile,
    ProfileError,
    ProfileStore,
    default_root,
)
from disguisebrowser.display import SYSTEM_DRIRC_DIR, VirtualDisplay
from disguisebrowser.errors import DisguiseError, LaunchError
from disguisebrowser.fingerprint import (
    CaptureServer,
    annotate,
    by_category,
    capture_with_browser,
    default_path,
    diff,
    diff_to_json,
    format_diff,
    load_record,
    save_record,
    summarize,
)
from disguisebrowser.input import PRESETS
from disguisebrowser.launcher import chrome_version, find_chrome
from disguisebrowser.persona import GPU_PRESETS, Persona
from disguisebrowser.protocols import BrowserLike
from disguisebrowser.scorecard import DETECTORS, format_report, run_scorecard, to_json


def _check(label: str, ok: bool, detail: str, hint: str = "") -> dict[str, Any]:
    return {"check": label, "ok": ok, "detail": detail, "hint": hint}


def _camoufox_check() -> dict[str, Any]:
    hint = "pip install 'disguisebrowser[camoufox]' && python -m camoufox fetch"
    if importlib.util.find_spec("camoufox") is None:
        return _check("camoufox", False, "package not installed", hint)
    try:
        from camoufox.pkgman import installed_verstr  # noqa: PLC0415

        build = str(installed_verstr())
    except Exception as exc:  # no browser build fetched yet
        return _check("camoufox", False, f"browser build missing ({exc})", hint)
    return _check("camoufox", True, f"Camoufox/{build}")


def doctor_checks() -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = [_camoufox_check()]
    try:
        chrome = find_chrome()
        checks.append(_check("chrome", True, f"{chrome} ({chrome_version(chrome)})"))
    except LaunchError as exc:
        checks.append(_check("chrome", False, str(exc), "install Google Chrome stable"))
    checks.append(_check("profiles", True, str(default_root())))
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
            "panel",
            shutil.which("tint2") is not None,
            shutil.which("tint2") or "none",
            "apt install tint2 — a panel so screen.availHeight differs from screen.height",
        )
    )
    checks.append(
        _check(
            "setxkbmap",
            shutil.which("setxkbmap") is not None,
            shutil.which("setxkbmap") or "missing",
            "apt install x11-xkb-utils — ЙЦУКЕН for XTest on the virtual display",
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
    required = "camoufox" if args.engine == "camoufox" else "chrome"
    return 0 if all(c["ok"] for c in checks if c["check"] == required) else 1


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
    if getattr(args, "keyboard", None):
        overrides["keyboard"] = args.keyboard
    return Persona.from_host(**overrides) if overrides else persona


_CHROME_ONLY = {"gpu": "--gpu", "keyboard": "--keyboard", "seed": "--seed"}


def _launch_camoufox(args: argparse.Namespace) -> LaunchHandle[Any]:
    from disguisebrowser.camoufox import launch as camoufox_launch  # noqa: PLC0415

    unsupported = [flag for dest, flag in _CHROME_ONLY.items() if getattr(args, dest, None)]
    if unsupported:
        raise DisguiseError(
            f"{', '.join(unsupported)} apply to the chrome engine only; with --engine camoufox "
            "the device comes from the profile (disguise profile create --os/--kind/--seed)"
        )
    overrides: dict[str, Any] = {}
    if args.timezone:
        overrides["timezone"] = args.timezone
    if args.lang:
        overrides["languages"] = tuple(args.lang.split(","))
    return camoufox_launch(
        profile=args.profile,
        create=args.create,
        os=args.os,
        persona=Persona.from_host(**overrides) if overrides else None,
        proxy=args.proxy,
        geoip=True if args.geoip else None,
        display=args.display,
        headless=args.headless,
        human=args.human,
    )


def _launch(args: argparse.Namespace) -> LaunchHandle[Any]:
    """Start the engine chosen by ``--engine``; the callers only use ``BrowserLike``."""
    if args.engine == "camoufox":
        return _launch_camoufox(args)
    for dest, flag in (("create", "--create"), ("os", "--os"), ("geoip", "--geoip")):
        if getattr(args, dest, None):
            raise DisguiseError(f"{flag} applies to --engine camoufox only")
    return launch(
        persona=_persona_from_args(args),
        display=args.display,
        headless=args.headless,
        user_data_dir=args.profile,
        human=args.human,
    )


def _display_label(browser: BrowserLike, args: argparse.Namespace) -> str:
    label = getattr(browser, "display_mode", None)
    if isinstance(label, str):
        return label
    return "virtual" if browser.display else ("headless" if args.headless else "host")


def _profile_location(browser: BrowserLike) -> str:
    chrome = getattr(browser, "chrome", None)
    return str(getattr(chrome or browser, "user_data_dir", "?"))


async def _run_test(args: argparse.Namespace) -> int:
    selected = (
        tuple(d for d in DETECTORS if d.name in set(args.only.split(",")))
        if args.only
        else DETECTORS
    )
    async with _launch(args) as browser:
        page = browser.pages[0] if browser.pages else await browser.new_page()
        env = {
            "engine": browser.engine,
            "chrome": browser.version.get("product"),
            "display": _display_label(browser, args),
            "gpu": browser.persona.gpu.renderer
            if browser.persona.gpu and browser.display
            else "host",
            "timezone": browser.persona.timezone,
            "languages": ",".join(browser.persona.languages),
        }
        results = await run_scorecard(page, selected, timeout=args.timeout, engine=browser.engine)
    print(to_json(results, env) if args.json else format_report(results, env))
    if args.report:
        await asyncio.to_thread(Path(args.report).write_text, to_json(results, env))
    return 1 if any(r.verdict == "fail" for r in results) else 0


async def _run_open(args: argparse.Namespace) -> int:
    async with _launch(args) as browser:
        page = browser.pages[0] if browser.pages else await browser.new_page()
        if args.url:
            await page.goto(args.url)
        where = (
            f"virtual display {browser.display.display}"
            if browser.display
            else f"the {_display_label(browser, args)} display"
        )
        print(f"{browser.version.get('product')} is running on {where}")
        print(f"profile: {_profile_location(browser)}")
        print("press Ctrl+C to close")
        with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
            while browser.pages:  # noqa: ASYNC110 — polling the tab list is the intent here
                await asyncio.sleep(0.5)
    return 0


async def _run_warmup(args: argparse.Namespace) -> int:
    async with _launch(args) as browser:
        visits = await browser.warmup(urls=args.url or None, count=args.visits)
    for visit in visits:
        if visit.ok:
            print(f"ok  {visit.url}  {visit.title}")
        else:
            print(f"!!  {visit.url}  {visit.error}")
    return 0 if any(visit.ok for visit in visits) else 1


def _save_capture(record: dict[str, Any], args: argparse.Namespace) -> Path:
    summary = summarize(record)
    path = Path(args.out) if args.out else default_path(record, Path(args.root))
    save_record(record, path)
    print(f"{summary.label}")
    print(f"saved {path}")
    return path


async def _run_capture(args: argparse.Namespace) -> int:
    if args.serve:
        return await _run_capture_serve(args)
    async with _launch(args) as browser:
        record = await capture_with_browser(browser, timeout=args.timeout)
    _save_capture(record, args)
    return 0


async def _run_capture_serve(args: argparse.Namespace) -> int:
    host, _, port_text = args.bind.rpartition(":")
    server = CaptureServer(host or "127.0.0.1", int(port_text or 0))
    server.start()
    saved: dict[str, Path] = {}
    try:
        print(f"capture page: http://localhost:{server.port}/")
        print("open it in the browser to capture; from another machine use an SSH tunnel:")
        print(
            f"  ssh -L {server.port}:127.0.0.1:{server.port} <this-host>   (bound to {args.bind})"
        )
        print("the page must be a secure context (localhost) for Client Hints, WebGPU and devices")
        print(f"waiting for {args.count} capture(s); Ctrl+C to stop")
        while len(saved) < args.count:
            try:
                record = await server.wait(timeout=3600.0)
            except TimeoutError:
                continue
            annotate(record, kind="remote")
            capture_id = str(record.get("meta", {}).get("captureId", ""))
            if capture_id in saved:  # resubmission (e.g. with local fonts) replaces the file
                save_record(record, saved[capture_id])
                print(f"updated {saved[capture_id]}")
                continue
            saved[capture_id] = _save_capture(record, args)
    finally:
        server.stop()
    return 0


def _run_diff(args: argparse.Namespace) -> int:
    reference = load_record(args.a)
    other = load_record(args.b)
    differences = diff(reference, other, ignore=args.ignore)
    if args.json:
        print(diff_to_json(differences))
    else:
        print(
            format_diff(
                differences,
                label_a=f"{args.a} ({summarize(reference).label})",
                label_b=f"{args.b} ({summarize(other).label})",
            )
        )
    return 1 if by_category(differences)["deterministic"] else 0


def _add_launch_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--display", choices=("auto", "host", "virtual"), default="auto")
    parser.add_argument("--headless", action="store_true", help="opt-in, weaker stealth")
    parser.add_argument(
        "--engine",
        choices=("chrome", "camoufox"),
        default="chrome",
        help="browser engine (camoufox needs the 'camoufox' extra)",
    )
    parser.add_argument(
        "--profile",
        help="chrome: persistent user-data-dir; camoufox: stored profile name "
        "(see 'disguise profile'); default: temporary",
    )
    parser.add_argument(
        "--create", action="store_true", help="camoufox: create --profile if it does not exist"
    )
    parser.add_argument("--os", choices=OSES, help="camoufox: device OS for a new profile")
    parser.add_argument(
        "--geoip", action="store_true", help="camoufox: align timezone/locale/geo to the proxy exit"
    )
    parser.add_argument("--human", choices=sorted(PRESETS), default="default")
    parser.add_argument(
        "--gpu", choices=sorted(GPU_PRESETS), help="GPU identity on the virtual display"
    )
    parser.add_argument("--timezone", help="IANA timezone, e.g. Europe/Berlin")
    parser.add_argument("--lang", help="comma-separated languages, e.g. de-DE,de,en-US,en")
    parser.add_argument("--proxy", help="scheme://[user:pass@]host:port")
    parser.add_argument(
        "--keyboard",
        choices=("us", "us,ru"),
        help="XKB layout on the virtual display (default: us,ru when a language is ru)",
    )
    parser.add_argument(
        "--seed", type=int, help="deterministic geometry/GPU for a returning visitor"
    )


def _store(args: argparse.Namespace) -> ProfileStore:
    return ProfileStore(args.root) if args.root else ProfileStore()


def _print_profile(profile: Profile, *, as_json: bool) -> None:
    summary = profile.summary()
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    print(f"{summary['name']}  [{summary['os']}/{summary['kind']}]")
    for key in ("user_agent", "screen", "gpu", "cores", "proxy", "geoip", "languages", "timezone"):
        if summary[key] not in (None, "", False):
            print(f"  {key:<11} {summary[key]}")
    if profile.notes:
        print(f"  notes       {profile.notes}")


def _profile_create(args: argparse.Namespace) -> int:
    store = _store(args)
    try:
        profile = store.create(
            args.name,
            os=args.os,
            kind=args.kind,
            seed=args.seed,
            proxy=args.proxy,
            geoip=args.geoip,
            languages=tuple(args.lang.split(",")) if args.lang else None,
            timezone=args.timezone,
            notes=args.notes,
        )
    except ProfileError as exc:
        raise DisguiseError(str(exc)) from None
    _print_profile(profile, as_json=args.json)
    return 0


def _profile_list(args: argparse.Namespace) -> int:
    profiles = _store(args).list()
    if args.json:
        print(json.dumps([p.summary() for p in profiles], ensure_ascii=False, indent=2))
        return 0
    if not profiles:
        print("no profiles yet: disguise profile create NAME --os windows")
    for profile in profiles:
        info = profile.summary()
        proxy = f"  proxy={info['proxy']}" if info["proxy"] else ""
        print(f"{info['name']:<24} {info['os']:<8} {info['screen']:<10} {info['gpu']}{proxy}")
    return 0


def _profile_show(args: argparse.Namespace) -> int:
    try:
        profile = _store(args).load(args.name)
    except ProfileError as exc:
        raise DisguiseError(str(exc)) from None
    if args.full:
        print(profile.to_json())
    else:
        _print_profile(profile, as_json=args.json)
    return 0


def _profile_set(args: argparse.Namespace) -> int:
    changes: dict[str, Any] = {}
    if args.proxy is not None:
        changes["proxy"] = args.proxy or None  # '' clears the proxy
    if args.geoip is not None:
        changes["geoip"] = args.geoip
    if args.lang is not None:
        changes["languages"] = tuple(args.lang.split(",")) if args.lang else None
    if args.timezone is not None:
        changes["timezone"] = args.timezone or None
    if args.notes is not None:
        changes["notes"] = args.notes
    if not changes:
        raise DisguiseError("nothing to change: pass --proxy/--geoip/--lang/--timezone/--notes")
    try:
        profile = _store(args).update(args.name, **changes)
    except ProfileError as exc:
        raise DisguiseError(str(exc)) from None
    _print_profile(profile, as_json=False)
    return 0


def _profile_delete(args: argparse.Namespace) -> int:
    if not args.yes and sys.stdin.isatty():
        answer = input(f"delete profile {args.name!r} with all its cookies and storage? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1
    elif not args.yes:
        raise DisguiseError("refusing to delete without --yes when not on a terminal")
    try:
        _store(args).delete(args.name)
    except ProfileError as exc:
        raise DisguiseError(str(exc)) from None
    print(f"deleted {args.name}")
    return 0


def _add_profile_commands(sub: Any) -> None:
    profile = sub.add_parser(
        "profile", help="pinned, persistent Camoufox identities (create/list/show/set/delete)"
    )
    profile.add_argument("--root", help=f"profile directory (default: {default_root()})")
    actions = profile.add_subparsers(dest="profile_command", required=True)

    create = actions.add_parser("create", help="draw a device identity and store it")
    create.add_argument("name")
    create.add_argument("--os", choices=OSES, help="default: matches the host")
    create.add_argument(
        "--kind",
        choices=("fpgen", "preset"),
        default="fpgen",
        help="fpgen: generated device; preset: one of the bundled real devices",
    )
    create.add_argument("--seed", type=int, help="reproducible draw (preset kind)")
    create.add_argument("--proxy", help="scheme://[user:pass@]host:port")
    create.add_argument("--geoip", action="store_true", help="align locale/timezone to the proxy")
    create.add_argument("--lang", help="comma-separated languages")
    create.add_argument("--timezone")
    create.add_argument("--notes", default="")
    create.add_argument("--json", action="store_true")
    create.set_defaults(func=_profile_create)

    listing = actions.add_parser("list", help="list stored profiles")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=_profile_list)

    show = actions.add_parser("show", help="describe one profile")
    show.add_argument("name")
    show.add_argument("--json", action="store_true")
    show.add_argument("--full", action="store_true", help="print the whole profile.json")
    show.set_defaults(func=_profile_show)

    update = actions.add_parser(
        "set", help="change network settings (the device itself is never rerolled)"
    )
    update.add_argument("name")
    update.add_argument("--proxy", help="'' clears it")
    update.add_argument("--geoip", action=argparse.BooleanOptionalAction, default=None)
    update.add_argument("--lang", help="'' clears it")
    update.add_argument("--timezone", help="'' clears it")
    update.add_argument("--notes")
    update.set_defaults(func=_profile_set)

    delete = actions.add_parser("delete", help="remove a profile with its cookies and storage")
    delete.add_argument("name")
    delete.add_argument("-y", "--yes", action="store_true")
    delete.set_defaults(func=_profile_delete)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="disguise", description="DisguiseBrowser CLI")
    parser.add_argument("--version", action="version", version=f"disguisebrowser {__version__}")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check Chrome, Xvfb, Mesa, fonts and the host persona")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument(
        "--engine", choices=("chrome", "camoufox"), default="chrome", help="engine that must pass"
    )
    doctor.set_defaults(func=cmd_doctor)

    test = sub.add_parser("test", help="run the detection scorecard")
    _add_launch_options(test)
    test.add_argument("--only", help="comma-separated detector names")
    test.add_argument("--timeout", type=float, default=60.0)
    test.add_argument("--json", action="store_true")
    test.add_argument("--report", help="write JSON report to this path")
    test.set_defaults(func=lambda a: asyncio.run(_run_test(a)))

    warm = sub.add_parser("warmup", help="browse a few ordinary sites into a persistent profile")
    _add_launch_options(warm)
    warm.add_argument("--url", action="append", default=[], help="URL to visit (repeatable)")
    warm.add_argument(
        "--visits", type=int, default=3, help="how many built-in sites, if --url is omitted"
    )
    warm.set_defaults(func=lambda a: asyncio.run(_run_warmup(a)))

    capture = sub.add_parser(
        "capture",
        help="record everything a page can observe about this browser (reference fingerprint)",
    )
    _add_launch_options(capture)
    capture.add_argument("--out", help="write the record here (default: ROOT/os/gpu/major.json)")
    capture.add_argument("--root", default="fingerprints", help="directory for auto-named records")
    capture.add_argument("--timeout", type=float, default=120.0)
    capture.add_argument(
        "--serve",
        action="store_true",
        help="do not launch a browser: serve the capture page for a real browser to open",
    )
    capture.add_argument("--bind", default="127.0.0.1:8765", help="host:port for --serve")
    capture.add_argument("--count", type=int, default=1, help="captures to collect with --serve")
    capture.set_defaults(func=lambda a: asyncio.run(_run_capture(a)))

    diff_ = sub.add_parser(
        "diff", help="compare two fingerprint records (A = reference, B = other)"
    )
    diff_.add_argument("a")
    diff_.add_argument("b")
    diff_.add_argument(
        "--ignore",
        action="append",
        default=[],
        help="dotted path prefix or glob to skip (repeatable)",
    )
    diff_.add_argument("--json", action="store_true")
    diff_.set_defaults(func=_run_diff)

    open_ = sub.add_parser("open", help="open a stealth browser and keep it running")
    _add_launch_options(open_)
    open_.add_argument("url", nargs="?")
    open_.set_defaults(func=lambda a: asyncio.run(_run_open(a)))

    _add_profile_commands(sub)

    mcp = sub.add_parser(
        "mcp",
        help="run the MCP server for AI agents (needs the 'mcp' extra)",
        add_help=False,
    )
    mcp.add_argument("mcp_args", nargs=argparse.REMAINDER)
    mcp.set_defaults(func=_run_mcp)
    return parser


def _run_mcp(args: argparse.Namespace) -> int:
    # Imported lazily: the MCP SDK is an optional extra.
    from disguisebrowser.mcp_server import main as mcp_main  # noqa: PLC0415

    return mcp_main(args.mcp_args)


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
