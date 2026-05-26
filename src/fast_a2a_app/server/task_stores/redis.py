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
    a2a:progress:{task_id}:log         — LIST of "{seq}|{ts}|{message}" entries,
                                         appended on each report_progress(...).
    a2a:progress:{task_id}:seq         — monotonic counter for the log.
    a2a:progress:{task_id}:hb          — last-heartbeat unix seconds (string).
    a2a:progress:{task_id}:channel     — pub/sub channel; ``append_progress``
                                         PUBLISHes the new seq, ``subscribe_progress``
                                         SUBSCRIBES and re-reads the log.
"""
from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import AsyncIterator

import redis.asyncio as aioredis
from google.protobuf.json_format import MessageToJson, Parse
from a2a.server.tasks import TaskStore
from a2a.types import ListTasksRequest, ListTasksResponse, Task

from ._types import ProgressEntry

log = logging.getLogger(__name__)

_TTL = 86_400          # 24 h
_CANCEL_SIGNAL_TTL = 300  # 5 min
_PROGRESS_TTL = 86_400  # 24 h — matches task TTL


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

    def _progress_log_key(self, task_id: str) -> str:
        return f"a2a:progress:{task_id}:log"

    def _progress_seq_key(self, task_id: str) -> str:
        return f"a2a:progress:{task_id}:seq"

    def _progress_hb_key(self, task_id: str) -> str:
        return f"a2a:progress:{task_id}:hb"

    def _progress_channel(self, task_id: str) -> str:
        return f"a2a:progress:{task_id}:channel"

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
        """Remove a task, its context index entry, and its progress log atomically."""
        context_id = await self._r.get(self._task_context_key(task_id))
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.delete(self._task_key(task_id))
            pipe.delete(self._task_context_key(task_id))
            pipe.delete(self._progress_log_key(task_id))
            pipe.delete(self._progress_seq_key(task_id))
            pipe.delete(self._progress_hb_key(task_id))
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

    # ── Progress log + heartbeat ──────────────────────────────────────────────

    async def append_progress(self, task_id: str, message: str) -> int:
        """Atomically allocate a sequence number, append the entry, refresh TTLs."""
        seq_key = self._progress_seq_key(task_id)
        log_key = self._progress_log_key(task_id)
        hb_key = self._progress_hb_key(task_id)
        now = time.time()

        seq = await self._r.incr(seq_key)
        # ``message`` may contain any byte sequence; the leading "seq|ts|" prefix
        # uses a single pipe so the splitter only consumes two — anything in the
        # message body is preserved verbatim.
        entry = f"{seq}|{now}|{message}"
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.rpush(log_key, entry)
            pipe.set(hb_key, str(now), ex=_PROGRESS_TTL)
            pipe.expire(seq_key, _PROGRESS_TTL)
            pipe.expire(log_key, _PROGRESS_TTL)
            # Wake any live ``subscribe_progress`` tailers. The publish is
            # part of the same pipeline so a subscriber that re-reads on
            # wake is guaranteed to see the appended entry.
            pipe.publish(self._progress_channel(task_id), str(seq))
            await pipe.execute()
        return seq

    async def read_progress(
        self,
        task_id: str,
        since_seq: int = 0,
    ) -> list[ProgressEntry]:
        """Return entries with seq > since_seq, in append order."""
        raw_entries = await self._r.lrange(self._progress_log_key(task_id), 0, -1)
        entries: list[ProgressEntry] = []
        for raw in raw_entries:
            try:
                seq_str, ts_str, message = raw.split("|", 2)
                seq = int(seq_str)
                if seq <= since_seq:
                    continue
                entries.append(ProgressEntry(seq=seq, message=message, ts=float(ts_str)))
            except (ValueError, AttributeError):
                log.warning("Skipping malformed progress entry for task_id=%s", task_id)
        return entries

    async def subscribe_progress(
        self,
        task_id: str,
        since_seq: int = 0,
    ) -> AsyncIterator[ProgressEntry]:
        """Live-tail progress for ``task_id`` via Redis pub/sub.

        Each ``append_progress`` PUBLISHes the new seq on the per-task
        channel; on every wake the subscriber re-reads the log past
        ``last_seq``. ``get_message`` with a short timeout keeps the
        async loop responsive to cancellation.
        """
        pubsub = self._r.pubsub()
        channel = self._progress_channel(task_id)
        await pubsub.subscribe(channel)
        try:
            last_seq = since_seq
            for entry in await self.read_progress(task_id, last_seq):
                last_seq = entry.seq
                yield entry
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=5.0,
                )
                if message is None or message.get("type") != "message":
                    continue
                for entry in await self.read_progress(task_id, last_seq):
                    last_seq = entry.seq
                    yield entry
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(channel)
            with contextlib.suppress(Exception):
                await pubsub.aclose()

    async def clear_progress(self, task_id: str) -> None:
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.delete(self._progress_log_key(task_id))
            pipe.delete(self._progress_seq_key(task_id))
            pipe.delete(self._progress_hb_key(task_id))
            await pipe.execute()

    async def heartbeat(self, task_id: str) -> None:
        await self._r.set(self._progress_hb_key(task_id), str(time.time()), ex=_PROGRESS_TTL)

    async def get_heartbeat(self, task_id: str) -> float | None:
        value = await self._r.get(self._progress_hb_key(task_id))
        if value is None:
            return None
        try:
            return float(value)
        except ValueError:
            return None

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
