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

from typing import Protocol

from a2a.types import ListTasksRequest, ListTasksResponse, Task

from .memory import MemoryTaskStore
from .mongo import MongoTaskStore
from .postgres import PostgresTaskStore
from .redis import RedisTaskStore


class A2ATaskStore(Protocol):
    """Storage-agnostic interface for A2A task persistence.

    Extends the SDK's ``TaskStore`` contract with ``list_by_context`` (for
    conversation-history injection) and cancel-signal primitives (for
    cross-instance task cancellation).
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


__all__ = [
    "A2ATaskStore",
    "MemoryTaskStore",
    "MongoTaskStore",
    "PostgresTaskStore",
    "RedisTaskStore",
]
