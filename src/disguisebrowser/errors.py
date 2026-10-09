"""Exception hierarchy for DisguiseBrowser."""

from __future__ import annotations

from typing import Any


class DisguiseError(Exception):
    """Base class for all DisguiseBrowser errors."""


class LaunchError(DisguiseError):
    """Chrome could not be located, started or connected to."""


class ConnectionClosedError(DisguiseError):
    """The CDP connection was closed while a command or wait was in flight."""


class CdpError(DisguiseError):
    """Chrome answered a CDP command with an error object."""

    def __init__(self, method: str, code: int, message: str, data: Any = None) -> None:
        self.method = method
        self.code = code
        self.message = message
        self.data = data
        detail = f"{method} failed: {message} (code {code})"
        if data:
            detail += f" — {data}"
        super().__init__(detail)


class QuietCdpViolationError(DisguiseError):
    """A CDP command that is observable from the page was about to be sent.

    See ``disguisebrowser.cdp.policy`` for the list of forbidden commands and
    the reasons. Pass ``unsafe=True`` to ``launch()`` to opt out deliberately.
    """


class EvaluationError(DisguiseError):
    """JavaScript evaluated in the page threw an exception."""

    def __init__(self, description: str, details: dict[str, Any] | None = None) -> None:
        self.description = description
        self.details = details or {}
        super().__init__(description)


class NavigationError(DisguiseError):
    """Navigation failed (network error, blocked, aborted)."""


class ElementNotFoundError(DisguiseError, TimeoutError):
    """A selector matched nothing within the allotted time.

    Also a ``TimeoutError`` so that generic timeout handling keeps working.
    """


class DisplayError(DisguiseError):
    """The virtual display (Xvfb) could not be started."""
