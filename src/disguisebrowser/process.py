"""Child-process helpers shared by the launcher and the virtual display."""

from __future__ import annotations

import ctypes
import signal
import sys

_PR_SET_PDEATHSIG = 1


def die_with_parent() -> None:
    """``preexec_fn``: ask Linux to SIGTERM the child if this process dies abruptly.

    Chrome has no notion of its controller; without this a killed Python process
    leaves a browser (and its Xvfb) running forever.
    """
    if sys.platform != "linux":
        return
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
    except (OSError, AttributeError):  # best effort only
        pass
