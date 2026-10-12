"""Profile warmup: a short, ordinary browsing session before the target site.

An empty profile — no cache, no cookies, no history — is itself a signal
(threat layer L6). Warmup visits a few long-lived public sites, scrolls, and
dwells, so Chrome writes history into the profile. With ``Persona.seed`` the
choice of sites is stable for a returning visitor.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from disguisebrowser.persona import Persona

if TYPE_CHECKING:
    from disguisebrowser.protocols import BrowserLike

log = logging.getLogger(__name__)

# Sites that have stayed up for years and do not sit behind an anti-bot wall
# on a plain GET. The set is larger than a single run so two seeds diverge.
WARMUP_URLS: tuple[str, ...] = (
    "https://www.wikipedia.org/",
    "https://www.wiktionary.org/",
    "https://en.wikipedia.org/wiki/Web_browser",
    "https://en.wikipedia.org/wiki/Linux",
    "https://archive.org/",
    "https://www.mozilla.org/",
    "https://www.debian.org/",
    "https://www.kernel.org/",
    "https://docs.python.org/3/",
    "https://pypi.org/",
)


@dataclass(frozen=True)
class WarmupVisit:
    url: str
    ok: bool
    title: str = ""
    error: str | None = None


def choose_urls(persona: Persona, *, count: int = 3) -> list[str]:
    """Pick ``count`` warmup URLs. Deterministic when ``persona.seed`` is set."""
    if count < 1:
        raise ValueError("count must be at least 1")
    pool = list(WARMUP_URLS)
    persona.rng("warmup").shuffle(pool)
    return pool[: min(count, len(pool))]


async def warmup(
    browser: BrowserLike,
    urls: Sequence[str] | None = None,
    *,
    count: int = 3,
    dwell: tuple[float, float] = (1.0, 2.5),
) -> list[WarmupVisit]:
    """Open each URL, scroll once, and wait. Failures are recorded, not raised."""
    if dwell[0] < 0 or dwell[1] < dwell[0]:
        raise ValueError(f"dwell must be a non-empty range in seconds, got {dwell}")
    chosen = list(urls) if urls is not None else choose_urls(browser.persona, count=count)
    page = browser.pages[0] if browser.pages else await browser.new_page()
    visits: list[WarmupVisit] = []
    for url in chosen:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30)
            await page.scroll(page.rng.uniform(240, 720))
            low, high = dwell
            await page.wait_for_timeout(page.rng.uniform(low, high) * 1000)
            title = await page.title()
            visits.append(WarmupVisit(url, True, title))
            log.info("warmup ok %s (%s)", url, title)
        except Exception as exc:
            log.warning("warmup failed %s: %s", url, exc)
            visits.append(WarmupVisit(url, False, error=str(exc)))
    return visits
