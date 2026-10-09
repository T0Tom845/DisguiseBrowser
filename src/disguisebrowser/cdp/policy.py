"""Quiet-CDP policy: the list of DevTools commands we refuse to send.

Anti-bot vendors fingerprint *how* a browser is driven, not only what it
claims to be. Several CDP commands have side effects that page JavaScript can
observe. Keeping this list explicit (and tested) is the core of ADR-2/ADR-3
in ``docs/ARCHITECTURE.md``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from disguisebrowser.errors import QuietCdpViolationError

Params = Mapping[str, Any]


@dataclass(frozen=True)
class Rule:
    """A forbidden command, optionally with a predicate that makes it acceptable."""

    method: str
    reason: str
    allowed_if: Callable[[Params], bool] | None = None

    def violated_by(self, params: Params) -> bool:
        if self.allowed_if is None:
            return True
        return not self.allowed_if(params)


def _has(*keys: str) -> Callable[[Params], bool]:
    return lambda p: all(p.get(k) not in (None, "", {}) for k in keys)


def _has_any(*keys: str) -> Callable[[Params], bool]:
    return lambda p: any(p.get(k) not in (None, "", {}) for k in keys)


RULES: tuple[Rule, ...] = (
    Rule(
        "Runtime.enable",
        "enables execution-context and console events; the inspector then serializes "
        "console arguments, which the page detects via a getter on a logged Error "
        "(the leak used by Cloudflare/DataDome). Everything we need works without it.",
    ),
    Rule(
        "Console.enable",
        "legacy console domain with the same argument-serialization side effect.",
    ),
    Rule(
        "Page.setBypassCSP",
        "a page whose CSP is silently disabled can notice it (rebrowser 'bypassCsp' test).",
    ),
    Rule(
        "Runtime.addBinding",
        "adds a global function to the main world unless scoped to an isolated world.",
        allowed_if=_has_any("executionContextName", "executionContextId"),
    ),
    Rule(
        "Page.addScriptToEvaluateOnNewDocument",
        "init scripts in the main world are detectable (prototype tampering, "
        "'__pwInitScripts'); only isolated-world scripts (worldName) are allowed.",
        allowed_if=_has("worldName"),
    ),
    Rule(
        "Network.setUserAgentOverride",
        "a UA override without userAgentMetadata wipes Client Hints (sec-ch-ua headers), "
        "which servers check without running any JavaScript.",
        allowed_if=_has("userAgentMetadata"),
    ),
    Rule(
        "Emulation.setUserAgentOverride",
        "same as Network.setUserAgentOverride.",
        allowed_if=_has("userAgentMetadata"),
    ),
    Rule(
        "Emulation.setDeviceMetricsOverride",
        "viewport emulation desynchronizes window geometry from the reported screen; "
        "set the real window size via Persona instead.",
    ),
    Rule(
        "Emulation.setAutomationOverride",
        "can only force navigator.webdriver to true (verified on Chrome 154).",
    ),
    Rule(
        "Emulation.setTimezoneOverride",
        "per-target emulation; workers and other targets keep the real timezone. "
        "Persona.timezone sets TZ for the whole browser process instead.",
    ),
    Rule(
        "Emulation.setLocaleOverride",
        "per-target emulation; use Persona.languages (--lang + Accept-Language prefs).",
    ),
)


class QuietCdpPolicy:
    """Checks outgoing commands against :data:`RULES`.

    ``unsafe=True`` disables enforcement entirely; ``allow`` whitelists specific
    methods for callers who understand the trade-off.
    """

    def __init__(self, *, unsafe: bool = False, allow: frozenset[str] = frozenset()) -> None:
        self.unsafe = unsafe
        self.allow = allow
        self._rules = {rule.method: rule for rule in RULES}

    def check(self, method: str, params: Params | None = None) -> None:
        if self.unsafe or method in self.allow:
            return
        rule = self._rules.get(method)
        if rule is not None and rule.violated_by(params or {}):
            raise QuietCdpViolationError(
                f"{method} is forbidden by the quiet-CDP policy: {rule.reason}"
            )

    @property
    def forbidden_methods(self) -> frozenset[str]:
        return frozenset(self._rules)
