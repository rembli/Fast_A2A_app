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
from typing import Iterable, Protocol

from a2a.types import Artifact, ListTasksRequest, ListTasksResponse, Task, TaskState

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

    # ── Terminal finalize + stale scan ────────────────────────────────────────
    # ``finalize_task`` is the single durable path for writing a terminal
    # state. Callers include the framework's crash-recovery sweeper and
    # whatever agent-side hook the user wires via ``on_task_recover``.
    # Idempotent: a task already in a terminal state is left alone, so
    # racing finalizers don't clobber each other.

    async def finalize_task(
        self,
        task_id: str,
        *,
        state: TaskState,
        status_message: str | None = None,
        artifacts: Iterable[Artifact] | None = None,
    ) -> bool:
        """Atomically transition ``task_id`` to ``state`` and clear progress.

        Returns ``True`` if the task was transitioned by this call, ``False``
        if the task is missing or already terminal (no-op). Callers can rely
        on the boolean to decide whether to log the transition.

        ``status_message`` is wrapped as a single-text-part ``Message`` on
        ``task.status.message`` so the UI can surface a recovery reason.
        Any ``artifacts`` are appended to ``task.artifacts``.
        """
        ...

    async def list_stale_working_tasks(
        self,
        threshold_secs: float,
    ) -> list[str]:
        """Return task_ids in WORKING state whose heartbeat is older than
        ``threshold_secs`` (or absent entirely).

        Used by the startup sweeper to find tasks that were mid-flight when
        the process died and need a terminal write so the UI unsticks.
        """
        ...

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
