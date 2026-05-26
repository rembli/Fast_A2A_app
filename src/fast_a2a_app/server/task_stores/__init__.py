"""
task_stores — pluggable A2A task persistence backends.

One module per backend:

* :mod:`fast_a2a_app.server.task_stores.memory`   — in-process dict store,
  single-process only, the default when ``build_a2a_app`` is called
  without an explicit ``task_store``. Needs no external service —
  ideal for development, tests, and demos that should boot without
  Docker.
* :mod:`fast_a2a_app.server.task_stores.redis`    — Redis-backed store,
  the recommended production backend (horizontal scale, cross-instance
  cancel via short-TTL keys).
* :mod:`fast_a2a_app.server.task_stores.mongo`    — MongoDB-backed store
  using ``motor`` and TTL indexes.
* :mod:`fast_a2a_app.server.task_stores.postgres` — Postgres-backed store
  using ``asyncpg`` and ``expires_at`` columns.

Every backend implements the :class:`A2ATaskStore` Protocol below; pass any
instance to ``build_a2a_app(task_store=...)``.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from a2a.types import ListTasksRequest, ListTasksResponse, Task

from ._types import ProgressEntry
from .memory import MemoryTaskStore
from .mongo import MongoTaskStore
from .postgres import PostgresTaskStore
from .redis import RedisTaskStore


class A2ATaskStore(Protocol):
    """Storage-agnostic interface for A2A task persistence.

    Extends the SDK's ``TaskStore`` contract with ``list_by_context`` (for
    conversation-history injection), cancel-signal primitives (for
    cross-instance task cancellation), and a per-task append-only progress
    log + worker heartbeat (so ``report_progress(...)`` strings survive a
    crash and the UI can replay them on resubscribe).
    """

    async def save(self, task: Task, context=None) -> None: ...
    async def get(self, task_id: str, context=None) -> Task | None: ...
    async def delete(self, task_id: str, context=None) -> None: ...
    async def list(self, params: ListTasksRequest, context=None) -> ListTasksResponse: ...

    async def list_by_context(
        self,
        context_id: str,
        exclude_task_id: str | None = None,
    ) -> list[Task]: ...
    async def signal_cancel(self, task_id: str) -> None: ...
    async def is_cancel_signalled(self, task_id: str) -> bool: ...

    # ── Progress log + worker heartbeat ───────────────────────────────────────
    # Backends persist progress events so they survive a worker crash. On
    # resubscribe, the request handler replays the log so the UI's thinking
    # indicator picks up where it left off. ``heartbeat`` is bumped by the
    # executor while the task is running; ``get_heartbeat`` lets the handler
    # detect zombie-WORKING tasks whose worker has died.

    async def append_progress(self, task_id: str, message: str) -> int:
        """Append ``message`` to the task's progress log and bump the heartbeat.

        Returns the new monotonic sequence number (starts at 1).
        """
        ...

    async def read_progress(
        self,
        task_id: str,
        since_seq: int = 0,
    ) -> list[ProgressEntry]: ...

    def subscribe_progress(
        self,
        task_id: str,
        since_seq: int = 0,
    ) -> AsyncIterator[ProgressEntry]:
        """Live-tail the task's progress log as an async iterator.

        Yields any persisted entries with ``seq > since_seq`` first
        (catch-up), then waits for new entries written via
        ``append_progress`` and yields them as they arrive. The iterator
        runs until the consumer stops iterating or calls ``aclose()``.

        Backends implement the live-tail differently — in-process queues
        for ``MemoryTaskStore``, ``LISTEN/NOTIFY`` for Postgres, pub/sub
        for Redis, polling for Mongo — but the consumer-facing contract
        is identical.
        """
        ...

    async def clear_progress(self, task_id: str) -> None:
        """Drop all progress records for a task. Called on terminal states."""
        ...

    async def heartbeat(self, task_id: str) -> None:
        """Refresh the worker-liveness marker without writing progress."""
        ...

    async def get_heartbeat(self, task_id: str) -> float | None:
        """Return the last heartbeat as unix seconds, or ``None`` if absent."""
        ...


__all__ = [
    "A2ATaskStore",
    "MemoryTaskStore",
    "MongoTaskStore",
    "PostgresTaskStore",
    "ProgressEntry",
    "RedisTaskStore",
]
