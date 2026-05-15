"""
redis.py — Redis-backed A2A task store

Stores tasks, the context-id index, and cancel signals in Redis. The
recommended backend for production: horizontally scalable, native TTL,
cross-instance cancellation for free via short-TTL keys.

KEY SCHEMA
----------
::

    a2a:task:{task_id}                 — full Task JSON, TTL 24 h.
    a2a:context:{context_id}:tasks     — HASH task_id → sequence (creation order).
    a2a:context:{context_id}:sequence  — monotonic counter.
    a2a:cancel:{task_id}               — short-lived flag, TTL 5 min,
                                         used for cross-instance cancel signalling.
"""
from __future__ import annotations

import logging

import redis.asyncio as aioredis
from google.protobuf.json_format import MessageToJson, Parse
from a2a.server.tasks import TaskStore
from a2a.types import ListTasksRequest, ListTasksResponse, Task

log = logging.getLogger(__name__)

_TTL = 86_400          # 24 h
_CANCEL_SIGNAL_TTL = 300  # 5 min


class RedisTaskStore(TaskStore):
    """Redis-backed A2A task store with a context-id index.

    Pass an ``aioredis.Redis`` client (``decode_responses=True``) obtained
    from ``redis.asyncio.from_url()`` or your own connection pool. For
    the common case, use :meth:`from_url` to build the client for you.
    """

    def __init__(self, client: aioredis.Redis) -> None:
        self._r = client
        log.info("RedisTaskStore initialized")

    @classmethod
    def from_url(cls, url: str) -> "RedisTaskStore":
        """Build a ``RedisTaskStore`` from a Redis URL.

        Convenience wrapper around ``aioredis.from_url(url, decode_responses=True)``.
        """
        return cls(aioredis.from_url(url, decode_responses=True))

    # ── Key schema ────────────────────────────────────────────────────────────

    def _task_key(self, task_id: str) -> str:
        return f"a2a:task:{task_id}"

    def _cancel_key(self, task_id: str) -> str:
        return f"a2a:cancel:{task_id}"

    def _task_context_key(self, task_id: str) -> str:
        return f"a2a:task:{task_id}:context"

    def _context_index_key(self, context_id: str) -> str:
        return f"a2a:context:{context_id}:tasks"

    def _context_sequence_key(self, context_id: str) -> str:
        return f"a2a:context:{context_id}:sequence"

    # ── TaskStore protocol ────────────────────────────────────────────────────

    async def save(self, task: Task, context=None) -> None:
        """Persist a task and update the context index."""
        task_id = task.id
        context_id = task.context_id or None
        previous_context_id = await self._r.get(self._task_context_key(task_id))

        sequence: int | None = None
        if context_id:
            exists_in_context = await self._r.hexists(
                self._context_index_key(context_id), task_id
            )
            if not exists_in_context:
                sequence = await self._r.incr(self._context_sequence_key(context_id))

        async with self._r.pipeline(transaction=True) as pipe:
            pipe.set(
                self._task_key(task_id),
                MessageToJson(task),
                ex=_TTL,
            )

            if previous_context_id and previous_context_id != context_id:
                pipe.hdel(self._context_index_key(previous_context_id), task_id)

            if context_id:
                pipe.set(self._task_context_key(task_id), context_id, ex=_TTL)
                if sequence is not None:
                    pipe.hset(self._context_index_key(context_id), task_id, sequence)
                pipe.expire(self._context_index_key(context_id), _TTL)
                pipe.expire(self._context_sequence_key(context_id), _TTL)
            else:
                pipe.delete(self._task_context_key(task_id))

            await pipe.execute()

    async def get(self, task_id: str, context=None) -> Task | None:
        """Return the Task for task_id, or None if absent or corrupted."""
        payload = await self._r.get(self._task_key(task_id))
        if payload is None:
            return None
        try:
            return Parse(payload, Task())
        except Exception:
            log.exception("Corrupted task payload in Redis (task_id=%s)", task_id)
            return None

    async def delete(self, task_id: str, context=None) -> None:
        """Remove a task and its context index entry atomically."""
        context_id = await self._r.get(self._task_context_key(task_id))
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.delete(self._task_key(task_id))
            pipe.delete(self._task_context_key(task_id))
            if context_id:
                pipe.hdel(self._context_index_key(context_id), task_id)
            await pipe.execute()

    async def list(self, params: ListTasksRequest, context=None) -> ListTasksResponse:
        """Return an empty list — full task listing is not required for A2A operation."""
        return ListTasksResponse()

    # ── Extended interface ────────────────────────────────────────────────────

    async def signal_cancel(self, task_id: str) -> None:
        """Write a short-lived cancel-signal readable by is_cancel_signalled() on any replica."""
        await self._r.set(self._cancel_key(task_id), "1", ex=_CANCEL_SIGNAL_TTL)

    async def is_cancel_signalled(self, task_id: str) -> bool:
        """Return True if signal_cancel was called and the signal has not expired."""
        return bool(await self._r.exists(self._cancel_key(task_id)))

    async def list_by_context(
        self,
        context_id: str,
        exclude_task_id: str | None = None,
    ) -> list[Task]:
        """Return all tasks for context_id in creation order."""
        indexed_task_ids = await self._r.hgetall(self._context_index_key(context_id))
        ordered_task_ids = [
            task_id
            for task_id, _ in sorted(
                (
                    (task_id, int(sequence))
                    for task_id, sequence in indexed_task_ids.items()
                ),
                key=lambda item: item[1],
            )
            if not exclude_task_id or task_id != exclude_task_id
        ]
        if not ordered_task_ids:
            return []

        async with self._r.pipeline(transaction=True) as pipe:
            for task_id in ordered_task_ids:
                pipe.get(self._task_key(task_id))
            payloads = await pipe.execute()

        tasks: list[Task] = []
        for payload in payloads:
            if payload is None:
                continue
            try:
                tasks.append(Parse(payload, Task()))
            except Exception:
                log.exception("Corrupted task payload in Redis, skipping")
        return tasks
