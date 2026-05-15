"""
utils.py — Shared A2A utilities

Provides report_progress(), a thin helper that agent tools call to push
live status strings to the client during a streaming response.

The callback is propagated via a ContextVar so it flows automatically
across async boundaries without any explicit threading or parameter
passing. build_stream_invoke (in route.py) sets the callback before
each run; calls outside a streaming context are silently ignored.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable

_progress_cb: contextvars.ContextVar[
    Callable[[str], None] | None
] = contextvars.ContextVar("_progress_cb", default=None)


def report_progress(message: str) -> None:
    """Push a status string to the A2A streaming layer.

    Call from any agent tool to update the client's progress indicator.
    Has no effect outside a streaming context (non-streaming calls, tests).
    """
    cb = _progress_cb.get()
    if cb is not None:
        cb(message)
