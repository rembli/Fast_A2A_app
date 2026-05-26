"""
utils.py — Shared A2A utilities

Provides report_progress(), a thin helper that agent tools call to push
live status strings to the client during a streaming response.

The callback is propagated via a ContextVar so it flows automatically
across async boundaries without any explicit threading or parameter
passing. build_stream_invoke (in route.py) sets the callback before
each run; calls outside a streaming context are silently ignored.

A second ContextVar — set by the executor — carries crash-recovery
metadata (task_id + task_store) so each ``report_progress(...)`` call
is also persisted to the configured ``A2ATaskStore``. The persisted
log lets the UI's resubscribe path replay messages after a transport
hiccup or a worker crash without changing the public API.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .task_stores import A2ATaskStore

log = logging.getLogger(__name__)

_progress_cb: contextvars.ContextVar[
    Callable[[str], None] | None
] = contextvars.ContextVar("_progress_cb", default=None)


@dataclass
class _ProgressMeta:
    """Crash-recovery metadata for ``report_progress(...)``.

    Set by ``ConfigurableAgentExecutor`` for the duration of one task; lets
    ``report_progress`` fire-and-forget a write to the configured task store
    in addition to pushing onto the live in-process SSE queue.
    """

    task_id: str
    task_store: "A2ATaskStore"


_progress_meta: contextvars.ContextVar[
    _ProgressMeta | None
] = contextvars.ContextVar("_progress_meta", default=None)


def report_progress(message: str) -> None:
    """Push a status string to the A2A streaming layer.

    Call from any agent tool to update the client's progress indicator.
    Has no effect outside a streaming context (non-streaming calls, tests).

    The message is also persisted to the configured ``A2ATaskStore`` when
    one is available, so it survives a worker crash and gets replayed on
    the next ``SubscribeToTask``.
    """
    cb = _progress_cb.get()
    if cb is not None:
        cb(message)

    meta = _progress_meta.get()
    if meta is not None:
        _persist_progress(meta, message)


def _persist_progress(meta: _ProgressMeta, message: str) -> None:
    """Fire-and-forget the store write so ``report_progress`` stays sync + fast.

    A failure here must never break the live stream — agent tools have no way
    to handle it, and the live SSE event has already been enqueued.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _write() -> None:
        try:
            await meta.task_store.append_progress(meta.task_id, message)
        except Exception:
            log.warning(
                "Failed to persist progress for task_id=%s", meta.task_id,
                exc_info=True,
            )

    loop.create_task(_write())
