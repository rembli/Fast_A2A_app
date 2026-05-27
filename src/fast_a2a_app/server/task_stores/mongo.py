"""
mongo.py — MongoDB-backed A2A task store

Persists tasks, the context-id index, and cancel signals in three
collections. TTL indexes drop expired documents server-side without a
background sweeper.

COLLECTIONS
-----------
``tasks``::

    {"_id": task_id, "task_json": "...", "context_id": "..." | None,
     "expires_at": datetime}     — TTL index on ``expires_at`` (24 h).

``context_index``::

    {"_id": "{context_id}:{task_id}", "context_id": "...",
     "task_id": "...", "sequence": int, "expires_at": datetime}
                                 — TTL index on ``expires_at`` (24 h).

``cancel_signals``::

    {"_id": task_id, "expires_at": datetime}
                                 — TTL index on ``expires_at`` (5 min).

``progress``::

    {"_id": "{task_id}:{seq}", "task_id": "...", "seq": int,
     "message": "...", "ts": float, "expires_at": datetime}
                                 — append-only log; TTL on ``expires_at`` (24 h).

``progress_heartbeats``::

    {"_id": task_id, "ts": float, "expires_at": datetime}
                                 — bumped by the executor; TTL (24 h).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Iterable
from datetime import datetime, timedelta, timezone

from google.protobuf.json_format import MessageToJson, Parse
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from a2a.helpers import new_text_message
from a2a.server.tasks import TaskStore
from a2a.types import Artifact, ListTasksRequest, ListTasksResponse, Task, TaskState

from ._types import ProgressEntry

_TERMINAL_TASK_STATES = frozenset({
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_REJECTED,
})

log = logging.getLogger(__name__)

_TTL_SECONDS = 86_400          # 24 h
_CANCEL_SIGNAL_TTL_SECONDS = 300  # 5 min
_PROGRESS_TTL_SECONDS = 86_400  # 24 h — matches task TTL
# Live-tail poll interval. Mongo change streams would give push semantics
# but require a replica set / sharded cluster — polling at a modest rate
# keeps the backend usable against a standalone ``mongod``.
_SUBSCRIBE_POLL_INTERVAL = 0.5  # seconds


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class MongoTaskStore(TaskStore):
    """MongoDB-backed A2A task store using ``motor`` and TTL indexes."""

    def __init__(
        self,
        client: AsyncIOMotorClient,
        database_name: str = "fast_a2a",
    ) -> None:
        self._client = client
        self._db: AsyncIOMotorDatabase = client[database_name]
        self._tasks = self._db["tasks"]
        self._context_index = self._db["context_index"]
        self._cancel_signals = self._db["cancel_signals"]
        self._progress = self._db["progress"]
        self._progress_heartbeats = self._db["progress_heartbeats"]
        self._indexes_ready = False
        log.info("MongoTaskStore initialized (database=%s)", database_name)

    @classmethod
    async def from_uri(
        cls,
        uri: str,
        database_name: str = "fast_a2a",
    ) -> "MongoTaskStore":
        """Build a ``MongoTaskStore`` from a MongoDB URI and create TTL indexes."""
        store = cls(AsyncIOMotorClient(uri), database_name=database_name)
        await store.ensure_indexes()
        return store

    async def ensure_indexes(self) -> None:
        """Create TTL indexes on ``expires_at``. Idempotent."""
        if self._indexes_ready:
            return
        await self._tasks.create_index("expires_at", expireAfterSeconds=0)
        await self._tasks.create_index("context_id")
        await self._context_index.create_index("expires_at", expireAfterSeconds=0)
        await self._context_index.create_index(
            [("context_id", 1), ("sequence", 1)],
        )
        await self._cancel_signals.create_index("expires_at", expireAfterSeconds=0)
        await self._progress.create_index("expires_at", expireAfterSeconds=0)
        await self._progress.create_index([("task_id", 1), ("seq", 1)])
        await self._progress_heartbeats.create_index(
            "expires_at", expireAfterSeconds=0
        )
        self._indexes_ready = True

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _index_id(context_id: str, task_id: str) -> str:
        return f"{context_id}:{task_id}"

    # ── TaskStore protocol ────────────────────────────────────────────────────

    async def save(self, task: Task, context=None) -> None:
        """Persist a task and update the context index."""
        await self.ensure_indexes()
        task_id = task.id
        context_id = task.context_id or None
        expires_at = _utcnow() + timedelta(seconds=_TTL_SECONDS)

        existing = await self._tasks.find_one(
            {"_id": task_id}, projection={"context_id": 1}
        )
        previous_context_id = existing.get("context_id") if existing else None

        await self._tasks.replace_one(
            {"_id": task_id},
            {
                "_id": task_id,
                "task_json": MessageToJson(task),
                "context_id": context_id,
                "expires_at": expires_at,
            },
            upsert=True,
        )

        if previous_context_id and previous_context_id != context_id:
            await self._context_index.delete_one(
                {"_id": self._index_id(previous_context_id, task_id)}
            )

        if context_id:
            already_indexed = await self._context_index.find_one(
                {"_id": self._index_id(context_id, task_id)},
                projection={"_id": 1},
            )
            if already_indexed is None:
                # Sequence: max existing + 1 within this context. A unique
                # (context_id, sequence) index isn't worth the failure mode —
                # concurrent saves for the same context are rare and the
                # ordering is only used to render history.
                last = await self._context_index.find_one(
                    {"context_id": context_id},
                    sort=[("sequence", -1)],
                    projection={"sequence": 1},
                )
                sequence = (last["sequence"] if last else 0) + 1
                await self._context_index.insert_one({
                    "_id": self._index_id(context_id, task_id),
                    "context_id": context_id,
                    "task_id": task_id,
                    "sequence": sequence,
                    "expires_at": expires_at,
                })
            else:
                await self._context_index.update_one(
                    {"_id": self._index_id(context_id, task_id)},
                    {"$set": {"expires_at": expires_at}},
                )

    async def get(self, task_id: str, context=None) -> Task | None:
        """Return the Task for task_id, or None if absent or corrupted."""
        document = await self._tasks.find_one({"_id": task_id})
        if document is None:
            return None
        try:
            return Parse(document["task_json"], Task())
        except Exception:
            log.exception("Corrupted task payload in Mongo (task_id=%s)", task_id)
            return None

    async def delete(self, task_id: str, context=None) -> None:
        """Remove a task, its context index entry, and its progress log."""
        document = await self._tasks.find_one(
            {"_id": task_id}, projection={"context_id": 1}
        )
        await self._tasks.delete_one({"_id": task_id})
        if document and document.get("context_id"):
            await self._context_index.delete_one(
                {"_id": self._index_id(document["context_id"], task_id)}
            )
        await self._progress.delete_many({"task_id": task_id})
        await self._progress_heartbeats.delete_one({"_id": task_id})

    async def list(self, params: ListTasksRequest, context=None) -> ListTasksResponse:
        """Return an empty list — full task listing is not required for A2A operation."""
        return ListTasksResponse()

    # ── Extended interface ────────────────────────────────────────────────────

    async def signal_cancel(self, task_id: str) -> None:
        """Write a short-lived cancel signal readable by is_cancel_signalled() on any replica."""
        await self.ensure_indexes()
        await self._cancel_signals.replace_one(
            {"_id": task_id},
            {
                "_id": task_id,
                "expires_at": _utcnow() + timedelta(seconds=_CANCEL_SIGNAL_TTL_SECONDS),
            },
            upsert=True,
        )

    async def is_cancel_signalled(self, task_id: str) -> bool:
        """Return True if signal_cancel was called and the signal has not expired."""
        document = await self._cancel_signals.find_one(
            {"_id": task_id, "expires_at": {"$gt": _utcnow()}},
            projection={"_id": 1},
        )
        return document is not None

    # ── Progress log + heartbeat ──────────────────────────────────────────────

    async def append_progress(self, task_id: str, message: str) -> int:
        """Append a progress entry; ``seq`` is allocated as ``max(seq)+1`` for the task.

        Concurrent calls for the same task are rare (one worker per task) so the
        non-atomic read-then-write is acceptable; the unique ``_id`` index
        ensures a duplicate seq would surface as an exception rather than silent
        corruption.
        """
        await self.ensure_indexes()
        now = time.time()
        expires_at = _utcnow() + timedelta(seconds=_PROGRESS_TTL_SECONDS)

        last = await self._progress.find_one(
            {"task_id": task_id},
            sort=[("seq", -1)],
            projection={"seq": 1},
        )
        seq = (last["seq"] if last else 0) + 1
        await self._progress.insert_one({
            "_id": f"{task_id}:{seq}",
            "task_id": task_id,
            "seq": seq,
            "message": message,
            "ts": now,
            "expires_at": expires_at,
        })
        await self._progress_heartbeats.replace_one(
            {"_id": task_id},
            {"_id": task_id, "ts": now, "expires_at": expires_at},
            upsert=True,
        )
        return seq

    async def read_progress(
        self,
        task_id: str,
        since_seq: int = 0,
    ) -> list[ProgressEntry]:
        """Return entries with seq > since_seq in ascending seq order."""
        cursor = self._progress.find(
            {"task_id": task_id, "seq": {"$gt": since_seq}},
            projection={"seq": 1, "message": 1, "ts": 1, "_id": 0},
        ).sort("seq", 1)
        return [
            ProgressEntry(seq=doc["seq"], message=doc["message"], ts=doc["ts"])
            async for doc in cursor
        ]

    async def subscribe_progress(
        self,
        task_id: str,
        since_seq: int = 0,
    ) -> AsyncIterator[ProgressEntry]:
        """Live-tail progress for ``task_id`` by polling ``a2a_progress``.

        Mongo's push primitives (change streams, tailable cursors on
        capped collections) require a replica set or a capped collection
        respectively. Polling at ``_SUBSCRIBE_POLL_INTERVAL`` works
        against a vanilla standalone ``mongod`` with no setup; the
        consumer-facing API is identical to the other backends.
        """
        last_seq = since_seq
        while True:
            for entry in await self.read_progress(task_id, last_seq):
                last_seq = entry.seq
                yield entry
            await asyncio.sleep(_SUBSCRIBE_POLL_INTERVAL)

    async def clear_progress(self, task_id: str) -> None:
        await self._progress.delete_many({"task_id": task_id})
        await self._progress_heartbeats.delete_one({"_id": task_id})

    async def heartbeat(self, task_id: str) -> None:
        await self.ensure_indexes()
        expires_at = _utcnow() + timedelta(seconds=_PROGRESS_TTL_SECONDS)
        await self._progress_heartbeats.replace_one(
            {"_id": task_id},
            {"_id": task_id, "ts": time.time(), "expires_at": expires_at},
            upsert=True,
        )

    async def get_heartbeat(self, task_id: str) -> float | None:
        document = await self._progress_heartbeats.find_one(
            {"_id": task_id}, projection={"ts": 1}
        )
        if document is None:
            return None
        return document.get("ts")

    async def finalize_task(
        self,
        task_id: str,
        *,
        state: TaskState,
        status_message: str | None = None,
        artifacts: Iterable[Artifact] | None = None,
    ) -> bool:
        """Atomic-ish terminal write — read, mutate, replace, then drop progress.

        Idempotent: returns ``False`` if the task is missing or already in
        a terminal state. Race window between the read and the replace is
        bounded by the idempotency rule.
        """
        await self.ensure_indexes()
        document = await self._tasks.find_one(
            {"_id": task_id}, projection={"task_json": 1, "context_id": 1},
        )
        if document is None:
            return False
        try:
            task = Parse(document["task_json"], Task())
        except Exception:
            log.exception("finalize_task: corrupted payload for %s", task_id)
            return False
        if task.status.state in _TERMINAL_TASK_STATES:
            return False
        task.status.state = state
        if status_message is not None:
            task.status.message.CopyFrom(new_text_message(
                status_message,
                context_id=task.context_id or None,
                task_id=task.id,
            ))
        if artifacts:
            task.artifacts.extend(artifacts)

        expires_at = _utcnow() + timedelta(seconds=_TTL_SECONDS)
        await self._tasks.replace_one(
            {"_id": task_id},
            {
                "_id": task_id,
                "task_json": MessageToJson(task),
                "context_id": document.get("context_id"),
                "expires_at": expires_at,
            },
            upsert=True,
        )
        await self._progress.delete_many({"task_id": task_id})
        await self._progress_heartbeats.delete_one({"_id": task_id})
        return True

    async def list_stale_working_tasks(self, threshold_secs: float) -> list[str]:
        """Find WORKING tasks whose latest heartbeat is older than the threshold.

        We sweep ``progress_heartbeats`` (TTL-pruned, small) and confirm the
        task is still WORKING before listing it — concurrent finalize calls
        may have already moved a task to terminal without clearing the
        heartbeat document yet.
        """
        await self.ensure_indexes()
        now_ts = time.time()
        cutoff = now_ts - threshold_secs
        stale: list[str] = []
        hb_cursor = self._progress_heartbeats.find(
            {"ts": {"$lt": cutoff}}, projection={"_id": 1},
        )
        candidate_ids = [doc["_id"] async for doc in hb_cursor]
        if not candidate_ids:
            return stale
        task_cursor = self._tasks.find(
            {"_id": {"$in": candidate_ids}}, projection={"_id": 1, "task_json": 1},
        )
        async for document in task_cursor:
            try:
                task = Parse(document["task_json"], Task())
            except Exception:
                continue
            if task.status.state == TaskState.TASK_STATE_WORKING:
                stale.append(document["_id"])
        return stale

    async def list_by_context(
        self,
        context_id: str,
        exclude_task_id: str | None = None,
    ) -> list[Task]:
        """Return all tasks for context_id in creation order."""
        query: dict = {"context_id": context_id}
        if exclude_task_id:
            query["task_id"] = {"$ne": exclude_task_id}
        cursor = self._context_index.find(
            query, projection={"task_id": 1, "sequence": 1}
        ).sort("sequence", 1)
        ordered_task_ids = [doc["task_id"] async for doc in cursor]
        if not ordered_task_ids:
            return []

        task_documents = self._tasks.find(
            {"_id": {"$in": ordered_task_ids}},
            projection={"task_json": 1},
        )
        by_id: dict[str, str] = {
            document["_id"]: document["task_json"]
            async for document in task_documents
        }

        tasks: list[Task] = []
        for task_id in ordered_task_ids:
            payload = by_id.get(task_id)
            if payload is None:
                continue
            try:
                tasks.append(Parse(payload, Task()))
            except Exception:
                log.exception("Corrupted task payload in Mongo, skipping")
        return tasks
