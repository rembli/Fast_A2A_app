"""
utils.py — Shared A2A utilities

Provides ``report_progress(message)``: a fire-and-forget helper that agent
tools call to push a live status string to the client during a streaming
response.

State flow is request-scoped via two ContextVars set by the executor on
entry to ``execute()`` and reset on exit:

* ``_current_executor`` — the active :class:`ConfigurableAgentExecutor`
* ``_current_task_id``  — the task_id this request is executing

Two ContextVars (instead of one) keep the per-request state out of the
executor instance, which is shared across concurrent requests. Each
concurrent ``execute()`` runs in its own contextvars context, so the
task_id and executor binding never collide.

Inside the request, the executor's task is the parent of every agent step,
every pydantic-ai tool call, and any ``asyncio.create_task`` the user
spawns — all inherit a copy of the ContextVar values. So every
``report_progress`` call from inside the request resolves to the right
``(task_id, task_store)`` pair with no plumbing.

Outside an active request (module import, tests with no executor, a DBOS
workflow recovered on a fresh process where the original executor is gone)
the ContextVars are unset and ``report_progress`` is a silent no-op.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .route import ConfigurableAgentExecutor

log = logging.getLogger(__name__)


_current_executor: ContextVar["ConfigurableAgentExecutor | None"] = ContextVar(
    "fast_a2a_app.current_executor", default=None,
)

_current_task_id: ContextVar[str | None] = ContextVar(
    "fast_a2a_app.current_task_id", default=None,
)


def report_progress(message: str) -> None:
    """Append a status string to the current request's progress log.

    Resolves the active ``ConfigurableAgentExecutor`` from a request-scoped
    ContextVar set inside the executor's ``execute()`` body, and delegates
    to ``executor.report_progress(message)``. Fire-and-forget: the store
    write is scheduled as a background task on the current event loop, so
    the caller doesn't pay round-trip latency.

    Safe to call from sync or async tools, from helpers, and from
    ``asyncio.create_task`` siblings of the agent run (the ContextVar is
    copied at task creation). Silently no-ops if no executor is bound —
    e.g. at module-import time, in tests with no live request, or inside
    a DBOS workflow recovered on a fresh process.

    Live SSE delivery happens via the executor's progress subscriber;
    resubscribe replay reads the persisted log via ``store.read_progress``.
    The function returns immediately whether or not delivery succeeds —
    failures are logged but never raised, since a progress hiccup must not
    break the agent run.
    """
    executor = _current_executor.get()
    if executor is None:
        return
    executor.report_progress(message)
