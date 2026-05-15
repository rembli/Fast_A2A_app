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
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from google.protobuf.json_format import MessageToJson, Parse
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from a2a.server.tasks import TaskStore
from a2a.types import ListTasksRequest, ListTasksResponse, Task

log = logging.getLogger(__name__)

_TTL_SECONDS = 86_400          # 24 h
_CANCEL_SIGNAL_TTL_SECONDS = 300  # 5 min


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
        """Remove a task and its context index entry."""
        document = await self._tasks.find_one(
            {"_id": task_id}, projection={"context_id": 1}
        )
        await self._tasks.delete_one({"_id": task_id})
        if document and document.get("context_id"):
            await self._context_index.delete_one(
                {"_id": self._index_id(document["context_id"], task_id)}
            )

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
