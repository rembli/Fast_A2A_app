"""
postgres.py — Postgres-backed A2A task store

Persists tasks, the context-id index, and cancel signals in three tables.
Expired rows are filtered out at read time (``expires_at > NOW()``); a
periodic external sweeper can reclaim space if needed.

SCHEMA
------
::

    a2a_tasks(
        id           TEXT PRIMARY KEY,
        task_json    TEXT NOT NULL,
        context_id   TEXT,
        expires_at   TIMESTAMPTZ NOT NULL
    );

    a2a_context_index(
        context_id   TEXT NOT NULL,
        task_id      TEXT NOT NULL,
        sequence     BIGINT NOT NULL,
        expires_at   TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (context_id, task_id)
    );

    a2a_cancel_signals(
        task_id      TEXT PRIMARY KEY,
        expires_at   TIMESTAMPTZ NOT NULL
    );
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import asyncpg
from google.protobuf.json_format import MessageToJson, Parse
from a2a.server.tasks import TaskStore
from a2a.types import ListTasksRequest, ListTasksResponse, Task

log = logging.getLogger(__name__)

_TTL_SECONDS = 86_400          # 24 h
_CANCEL_SIGNAL_TTL_SECONDS = 300  # 5 min

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS a2a_tasks (
    id          TEXT PRIMARY KEY,
    task_json   TEXT NOT NULL,
    context_id  TEXT,
    expires_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS a2a_tasks_context_id_idx
    ON a2a_tasks (context_id);
CREATE INDEX IF NOT EXISTS a2a_tasks_expires_at_idx
    ON a2a_tasks (expires_at);

CREATE TABLE IF NOT EXISTS a2a_context_index (
    context_id  TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    sequence    BIGINT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (context_id, task_id)
);
CREATE INDEX IF NOT EXISTS a2a_context_index_seq_idx
    ON a2a_context_index (context_id, sequence);

CREATE TABLE IF NOT EXISTS a2a_cancel_signals (
    task_id     TEXT PRIMARY KEY,
    expires_at  TIMESTAMPTZ NOT NULL
);
"""


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class PostgresTaskStore(TaskStore):
    """Postgres-backed A2A task store using ``asyncpg`` and ``expires_at`` columns."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._schema_ready = False
        log.info("PostgresTaskStore initialized")

    @classmethod
    async def from_dsn(cls, dsn: str) -> "PostgresTaskStore":
        """Build a ``PostgresTaskStore`` from a Postgres DSN and create tables."""
        pool = await asyncpg.create_pool(dsn)
        store = cls(pool)
        await store.ensure_schema()
        return store

    async def ensure_schema(self) -> None:
        """Create the tables and indexes if they do not already exist. Idempotent."""
        if self._schema_ready:
            return
        async with self._pool.acquire() as connection:
            await connection.execute(_SCHEMA_SQL)
        self._schema_ready = True

    # ── TaskStore protocol ────────────────────────────────────────────────────

    async def save(self, task: Task, context=None) -> None:
        """Persist a task and update the context index."""
        await self.ensure_schema()
        task_id = task.id
        context_id = task.context_id or None
        expires_at = _utcnow() + timedelta(seconds=_TTL_SECONDS)
        payload = MessageToJson(task)

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                previous_context_id = await connection.fetchval(
                    "SELECT context_id FROM a2a_tasks WHERE id = $1 FOR UPDATE",
                    task_id,
                )
                await connection.execute(
                    """
                    INSERT INTO a2a_tasks (id, task_json, context_id, expires_at)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (id) DO UPDATE SET
                        task_json  = EXCLUDED.task_json,
                        context_id = EXCLUDED.context_id,
                        expires_at = EXCLUDED.expires_at
                    """,
                    task_id, payload, context_id, expires_at,
                )

                if previous_context_id and previous_context_id != context_id:
                    await connection.execute(
                        "DELETE FROM a2a_context_index WHERE context_id = $1 AND task_id = $2",
                        previous_context_id, task_id,
                    )

                if context_id:
                    existing = await connection.fetchval(
                        "SELECT 1 FROM a2a_context_index WHERE context_id = $1 AND task_id = $2",
                        context_id, task_id,
                    )
                    if existing is None:
                        sequence = await connection.fetchval(
                            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM a2a_context_index "
                            "WHERE context_id = $1",
                            context_id,
                        )
                        await connection.execute(
                            "INSERT INTO a2a_context_index "
                            "(context_id, task_id, sequence, expires_at) "
                            "VALUES ($1, $2, $3, $4)",
                            context_id, task_id, sequence, expires_at,
                        )
                    else:
                        await connection.execute(
                            "UPDATE a2a_context_index SET expires_at = $1 "
                            "WHERE context_id = $2 AND task_id = $3",
                            expires_at, context_id, task_id,
                        )

    async def get(self, task_id: str, context=None) -> Task | None:
        """Return the Task for task_id, or None if absent / expired / corrupted."""
        row = await self._pool.fetchrow(
            "SELECT task_json FROM a2a_tasks WHERE id = $1 AND expires_at > NOW()",
            task_id,
        )
        if row is None:
            return None
        try:
            return Parse(row["task_json"], Task())
        except Exception:
            log.exception("Corrupted task payload in Postgres (task_id=%s)", task_id)
            return None

    async def delete(self, task_id: str, context=None) -> None:
        """Remove a task and its context index entry."""
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                context_id = await connection.fetchval(
                    "DELETE FROM a2a_tasks WHERE id = $1 RETURNING context_id",
                    task_id,
                )
                if context_id:
                    await connection.execute(
                        "DELETE FROM a2a_context_index WHERE context_id = $1 AND task_id = $2",
                        context_id, task_id,
                    )

    async def list(self, params: ListTasksRequest, context=None) -> ListTasksResponse:
        """Return an empty list — full task listing is not required for A2A operation."""
        return ListTasksResponse()

    # ── Extended interface ────────────────────────────────────────────────────

    async def signal_cancel(self, task_id: str) -> None:
        """Write a short-lived cancel signal readable by is_cancel_signalled() on any replica."""
        await self.ensure_schema()
        expires_at = _utcnow() + timedelta(seconds=_CANCEL_SIGNAL_TTL_SECONDS)
        await self._pool.execute(
            """
            INSERT INTO a2a_cancel_signals (task_id, expires_at)
            VALUES ($1, $2)
            ON CONFLICT (task_id) DO UPDATE SET expires_at = EXCLUDED.expires_at
            """,
            task_id, expires_at,
        )

    async def is_cancel_signalled(self, task_id: str) -> bool:
        """Return True if signal_cancel was called and the signal has not expired."""
        value = await self._pool.fetchval(
            "SELECT 1 FROM a2a_cancel_signals WHERE task_id = $1 AND expires_at > NOW()",
            task_id,
        )
        return value is not None

    async def list_by_context(
        self,
        context_id: str,
        exclude_task_id: str | None = None,
    ) -> list[Task]:
        """Return all tasks for context_id in creation order."""
        if exclude_task_id:
            rows = await self._pool.fetch(
                """
                SELECT t.task_json
                FROM a2a_context_index ci
                JOIN a2a_tasks t ON t.id = ci.task_id
                WHERE ci.context_id = $1
                  AND ci.task_id <> $2
                  AND t.expires_at > NOW()
                ORDER BY ci.sequence
                """,
                context_id, exclude_task_id,
            )
        else:
            rows = await self._pool.fetch(
                """
                SELECT t.task_json
                FROM a2a_context_index ci
                JOIN a2a_tasks t ON t.id = ci.task_id
                WHERE ci.context_id = $1
                  AND t.expires_at > NOW()
                ORDER BY ci.sequence
                """,
                context_id,
            )

        tasks: list[Task] = []
        for row in rows:
            try:
                tasks.append(Parse(row["task_json"], Task()))
            except Exception:
                log.exception("Corrupted task payload in Postgres, skipping")
        return tasks
