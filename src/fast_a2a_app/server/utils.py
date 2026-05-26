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


# Process-global fallback resolver. Used when ``_progress_meta`` is not
# set in the current ContextVar — typically because the work is running
# inside a durable-execution workflow (e.g., DBOS) that was recovered on
# a different process after a crash, so the executor's ContextVar token
# never got handed down. The resolver returns ``(task_id, task_store)``
# at call time, letting the example wire it to whatever runtime symbol
# carries the current task id (e.g., ``DBOS.workflow_id``).
_progress_resolver: Callable[
    [], "_ProgressMeta | None"
] | None = None


def register_progress_resolver(
    resolver: Callable[[], tuple[str, "A2ATaskStore"] | None] | None,
) -> None:
    """Register a process-global fallback for ``report_progress`` persistence.

    The executor sets a ``_progress_meta`` ContextVar before calling the
    agent so ``report_progress`` writes flow into the active task store.
    ContextVars don't survive a process crash, however — when a durable
    workflow (DBOS, Temporal, …) is recovered on a fresh process, the
    ContextVar isn't set on its event loop, and ``report_progress``
    becomes a no-op.

    A resolver registered here is consulted when the ContextVar is
    missing. It should return ``(task_id, task_store)`` for the work
    currently running on this thread/loop, or ``None`` if no task is
    active (the call is then silently ignored).

    Example wiring for a DBOS-recovered workflow::

        from fast_a2a_app import register_progress_resolver
        from dbos import DBOS

        def _resolver():
            wf_id = DBOS.workflow_id  # set by SetWorkflowID(task_id)
            return (wf_id, task_store) if wf_id else None

        register_progress_resolver(_resolver)

    Pass ``None`` to clear an existing resolver (e.g., during teardown
    in tests).
    """
    global _progress_resolver
    if resolver is None:
        _progress_resolver = None
        return

    def _wrapped() -> _ProgressMeta | None:
        try:
            result = resolver()
        except Exception:
            log.debug("Progress resolver raised", exc_info=True)
            return None
        if result is None:
            return None
        task_id, task_store = result
        return _ProgressMeta(task_id=task_id, task_store=task_store)

    _progress_resolver = _wrapped


def report_progress(message: str) -> None:
    """Push a status string to the A2A streaming layer.

    Call from any agent tool to update the client's progress indicator.
    Has no effect outside a streaming context (non-streaming calls, tests).

    The message is also persisted to the configured ``A2ATaskStore`` when
    one is available, so it survives a worker crash and gets replayed on
    the next ``SubscribeToTask``. After a DBOS-style recovery the
    ContextVar isn't set; if a resolver has been registered via
    ``register_progress_resolver`` it provides the (task_id, task_store)
    pair so persistence still happens.
    """
    cb = _progress_cb.get()
    if cb is not None:
        cb(message)

    meta = _progress_meta.get()
    if meta is None and _progress_resolver is not None:
        meta = _progress_resolver()
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
