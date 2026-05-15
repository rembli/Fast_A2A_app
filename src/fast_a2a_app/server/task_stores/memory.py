"""
memory.py — In-process A2A task store

A dependency-free :class:`MemoryTaskStore` that keeps tasks, the
context-id index, and cancel signals in Python dicts. The default when
no ``task_store`` is passed to ``build_a2a_app``.

WHEN TO USE
-----------
Development, tests, and demos — anywhere booting Redis/Mongo/Postgres
just to try a hello-world agent is unnecessary friction.

WHEN NOT TO USE
---------------
Anything beyond a single uvicorn process. State lives in RAM, so:

* tasks do not survive a restart;
* worker processes do not share state (cross-instance cancel cannot work);
* there is no TTL housekeeping beyond the lazy 5-minute cancel signal.

Swap in :class:`RedisTaskStore`, :class:`MongoTaskStore`, or
:class:`PostgresTaskStore` for production.
"""
from __future__ import annotations

import asyncio
import logging
import time

from google.protobuf.json_format import MessageToJson, Parse
from a2a.server.tasks import TaskStore
from a2a.types import ListTasksRequest, ListTasksResponse, Task

log = logging.getLogger(__name__)

_CANCEL_SIGNAL_TTL = 300  # 5 min — matches RedisTaskStore.


class MemoryTaskStore(TaskStore):
    """In-process A2A task store backed by plain dicts."""

    def __init__(self) -> None:
        self._tasks: dict[str, str] = {}
        self._task_context: dict[str, str] = {}
        self._context_index: dict[str, dict[str, int]] = {}
        self._context_sequence: dict[str, int] = {}
        self._cancel_signals: dict[str, float] = {}
        self._lock = asyncio.Lock()
        log.info(
            "MemoryTaskStore initialized — in-process only; "
            "tasks do NOT survive restarts and the process cannot be horizontally scaled.",
        )

    # ── TaskStore protocol ────────────────────────────────────────────────────

    async def save(self, task: Task, context=None) -> None:
        """Persist a task and update the context index."""
        task_id = task.id
        context_id = task.context_id or None
        payload = MessageToJson(task)

        async with self._lock:
            previous_context_id = self._task_context.get(task_id)
            self._tasks[task_id] = payload

            if previous_context_id and previous_context_id != context_id:
                index = self._context_index.get(previous_context_id)
                if index is not None:
                    index.pop(task_id, None)

            if context_id:
                self._task_context[task_id] = context_id
                index = self._context_index.setdefault(context_id, {})
                if task_id not in index:
                    sequence = self._context_sequence.get(context_id, 0) + 1
                    self._context_sequence[context_id] = sequence
                    index[task_id] = sequence
            else:
                self._task_context.pop(task_id, None)

    async def get(self, task_id: str, context=None) -> Task | None:
        """Return the Task for task_id, or None if absent or corrupted."""
        async with self._lock:
            payload = self._tasks.get(task_id)
        if payload is None:
            return None
        try:
            return Parse(payload, Task())
        except Exception:
            log.exception("Corrupted task payload in MemoryTaskStore (task_id=%s)", task_id)
            return None

    async def delete(self, task_id: str, context=None) -> None:
        """Remove a task and its context index entry."""
        async with self._lock:
            self._tasks.pop(task_id, None)
            context_id = self._task_context.pop(task_id, None)
            if context_id:
                index = self._context_index.get(context_id)
                if index is not None:
                    index.pop(task_id, None)

    async def list(self, params: ListTasksRequest, context=None) -> ListTasksResponse:
        """Return an empty list — full task listing is not required for A2A operation."""
        return ListTasksResponse()

    # ── Extended interface ────────────────────────────────────────────────────

    async def signal_cancel(self, task_id: str) -> None:
        """Write a short-lived cancel signal readable by is_cancel_signalled() within this process."""
        async with self._lock:
            self._cancel_signals[task_id] = time.monotonic() + _CANCEL_SIGNAL_TTL

    async def is_cancel_signalled(self, task_id: str) -> bool:
        """Return True if signal_cancel was called and the signal has not expired."""
        async with self._lock:
            expiry = self._cancel_signals.get(task_id)
            if expiry is None:
                return False
            if expiry <= time.monotonic():
                self._cancel_signals.pop(task_id, None)
                return False
            return True

    async def list_by_context(
        self,
        context_id: str,
        exclude_task_id: str | None = None,
    ) -> list[Task]:
        """Return all tasks for context_id in creation order."""
        async with self._lock:
            index = self._context_index.get(context_id, {})
            ordered = sorted(
                (
                    (task_id, sequence)
                    for task_id, sequence in index.items()
                    if not exclude_task_id or task_id != exclude_task_id
                ),
                key=lambda item: item[1],
            )
            payloads = [self._tasks.get(task_id) for task_id, _ in ordered]

        tasks: list[Task] = []
        for payload in payloads:
            if payload is None:
                continue
            try:
                tasks.append(Parse(payload, Task()))
            except Exception:
                log.exception("Corrupted task payload in MemoryTaskStore, skipping")
        return tasks
