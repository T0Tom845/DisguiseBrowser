"""Scorecard: run the current configuration against public detection pages.

Analog of CloakBrowser's ``cloaktest``. The pages are live and change over
time, so this is a manual/periodic tool, not a CI gate (see docs, section 9).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from disguisebrowser.page import Page

log = logging.getLogger(__name__)

Verdict = Literal["pass", "warn", "fail", "unknown"]
Judge = Callable[[Any], tuple[Verdict, str]]
Interact = Callable[[Page], Awaitable[None]]


@dataclass(frozen=True)
class Detector:
    name: str
    url: str
    extract: str
    judge: Judge
    settle_seconds: float = 4.0
    interact: Interact | None = None
    description: str = ""


@dataclass
class Result:
    detector: str
    verdict: Verdict
    summary: str
    url: str
    raw: Any = None
    error: str | None = None
    seconds: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------- judges
def _judge_sannysoft(raw: Any) -> tuple[Verdict, str]:
    failed = raw.get("failed", []) if isinstance(raw, dict) else []
    total = raw.get("total", 0) if isinstance(raw, dict) else 0
    if not total:
        return "unknown", "results table not found"
    if not failed:
        return "pass", f"0 of {total} checks failed"
    return "fail", f"{len(failed)} failed: " + "; ".join(failed[:6])


def _judge_rebrowser(raw: Any) -> tuple[Verdict, str]:
    rows = raw if isinstance(raw, list) else []
    if not rows:
        return "unknown", "results table not found"
    red = [r for r in rows if r.startswith("🔴")]
    if red:
        return "fail", "; ".join(r[:90] for r in red)
    green = sum(1 for r in rows if r.startswith("🟢"))
    return "pass", f"{green} green, {len(rows) - green} not triggered"


def _judge_dabi(raw: Any) -> tuple[Verdict, str]:
    verdict = (raw or {}).get("verdict", "") if isinstance(raw, dict) else ""
    fired = (raw or {}).get("fired") if isinstance(raw, dict) else None
    if "human" in verdict.lower():
        return "pass", verdict
    if "bot" in verdict.lower():
        return "fail", f"{verdict} — {', '.join(fired or []) or 'no details'}"
    return "unknown", verdict[:120] or "no verdict text"


def _judge_browserscan(raw: Any) -> tuple[Verdict, str]:
    text = str(raw or "")
    if re.search(r"\bRobot\b", text):
        return "fail", "verdict: Robot"
    if re.search(r"\bNormal\b", text):
        return "pass", "verdict: Normal"
    return "unknown", "could not find a verdict on the page"


def _judge_tls(raw: Any) -> tuple[Verdict, str]:
    if not isinstance(raw, dict) or not raw.get("ja4"):
        return "unknown", "no TLS data"
    ua = str(raw.get("ua", ""))
    if "Headless" in ua:
        return "fail", f"HeadlessChrome in UA; JA4 {raw['ja4']}"
    return "pass", f"JA4 {raw['ja4']} · HTTP/2 {raw.get('akamai', '?')} · {raw.get('h2', '?')}"


# ---------------------------------------------------------------------- interactions
async def _rebrowser_interact(page: Page) -> None:
    # Trigger the tests the way our library really works: DOM calls from the
    # isolated world. A main-world driver would light up 'mainWorldExecution'.
    await page.call(
        "() => { document.getElementsByClassName('div'); "
        "document.getElementById('detections-json'); return true; }"
    )
    await asyncio.sleep(2.0)


# ---------------------------------------------------------------------- catalogue
DETECTORS: tuple[Detector, ...] = (
    Detector(
        name="sannysoft",
        url="https://bot.sannysoft.com/",
        description="classic headless/webdriver checks",
        extract="""() => {
            const rows = [...document.querySelectorAll('table tr')].map(tr => {
                const tds = [...tr.querySelectorAll('td')];
                if (tds.length < 2) return null;
                const failed = /failed/.test(tds[1].className);
                return failed ? tds[0].innerText.trim().replace(/\\s+/g, ' ') + ' => '
                    + tds[1].innerText.trim().slice(0, 60) : '';
            }).filter(r => r !== null);
            return { total: rows.length, failed: rows.filter(Boolean) };
        }""",
        judge=_judge_sannysoft,
    ),
    Detector(
        name="rebrowser",
        url="https://bot-detector.rebrowser.net/",
        description="CDP leaks: Runtime.enable, sourceURL, main-world execution, CSP, viewport",
        extract="""() => [...document.querySelectorAll('table tr')]
            .map(tr => tr.innerText.replace(/\\s+/g, ' ').trim())
            .filter(s => /^(🟢|🔴|⚪️|⚪)/.test(s))""",
        judge=_judge_rebrowser,
        settle_seconds=2.5,
        interact=_rebrowser_interact,
    ),
    Detector(
        name="deviceandbrowserinfo",
        url="https://deviceandbrowserinfo.com/are_you_a_bot",
        description="automation signals incl. isAutomatedWithCDP, webdriver, headless UA",
        extract="""() => {
            const text = document.body.innerText;
            const m = text.match(/You are (a bot|human)[^\\n]*/i);
            let details = null;
            for (const pre of document.querySelectorAll('pre, code')) {
                try {
                    const j = JSON.parse(pre.innerText);
                    if ('isBot' in j) { details = j; break; }
                } catch (e) {}
            }
            const fired = details
                ? Object.entries(details.details || {})
                    .filter(([, v]) => v === true).map(([k]) => k)
                : null;
            return { verdict: m ? m[0] : '', fired };
        }""",
        judge=_judge_dabi,
        settle_seconds=8.0,
    ),
    Detector(
        name="browserscan",
        url="https://www.browserscan.net/bot-detection",
        description="aggregate bot verdict (text heuristic)",
        extract="() => document.body.innerText.slice(0, 20000)",
        judge=_judge_browserscan,
        settle_seconds=8.0,
    ),
    Detector(
        name="tls",
        url="https://tls.peet.ws/api/all",
        description="JA3/JA4 TLS and HTTP/2 fingerprint (should be real Chrome's)",
        extract="""() => {
            try {
                const j = JSON.parse(document.body.innerText);
                return { ja3: j.tls?.ja3_hash, ja4: j.tls?.ja4,
                         akamai: j.http2?.akamai_fingerprint_hash,
                         h2: j.http_version, ua: j.user_agent };
            } catch (e) { return null; }
        }""",
        judge=_judge_tls,
        settle_seconds=1.0,
    ),
)


async def run_detector(page: Page, detector: Detector, *, timeout: float = 60.0) -> Result:
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        await page.goto(detector.url, wait_until="load", timeout=timeout)
        await asyncio.sleep(detector.settle_seconds)
        if detector.interact is not None:
            await detector.interact(page)
        raw = await page.call(detector.extract)
        verdict, summary = detector.judge(raw)
        return Result(
            detector.name, verdict, summary, detector.url, raw=raw, seconds=loop.time() - started
        )
    except Exception as exc:
        log.warning("%s failed: %s", detector.name, exc)
        return Result(
            detector.name, "unknown", f"error: {exc}", detector.url, error=str(exc),
            seconds=loop.time() - started,
        )  # fmt: skip


async def run_scorecard(
    page: Page, detectors: tuple[Detector, ...] = DETECTORS, *, timeout: float = 60.0
) -> list[Result]:
    results = []
    for detector in detectors:
        log.info("running %s …", detector.name)
        results.append(await run_detector(page, detector, timeout=timeout))
    return results


_ICONS = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "unknown": "????"}


def format_report(results: list[Result], environment: dict[str, Any] | None = None) -> str:
    lines = []
    if environment:
        lines.append("environment: " + ", ".join(f"{k}={v}" for k, v in environment.items()))
        lines.append("")
    width = max(len(r.detector) for r in results) if results else 10
    for result in results:
        lines.append(
            f"{_ICONS[result.verdict]}  {result.detector:<{width}}  {result.summary}  "
            f"({result.seconds:.1f}s)"
        )
    counts = {v: sum(1 for r in results if r.verdict == v) for v in _ICONS}
    lines.append("")
    lines.append(
        f"pass {counts['pass']} · warn {counts['warn']} · fail {counts['fail']} · "
        f"unknown {counts['unknown']}"
    )
    return "\n".join(lines)


def to_json(results: list[Result], environment: dict[str, Any] | None = None) -> str:
    return json.dumps(
        {
            "environment": environment or {},
            "results": [
                {
                    "detector": r.detector,
                    "verdict": r.verdict,
                    "summary": r.summary,
                    "url": r.url,
                    "seconds": round(r.seconds, 2),
                    "raw": r.raw,
                    "error": r.error,
                }
                for r in results
            ],
        },
        ensure_ascii=False,
        indent=2,
    )
