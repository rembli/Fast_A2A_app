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

    a2a_progress(
        task_id      TEXT NOT NULL,
        seq          BIGINT NOT NULL,
        message      TEXT NOT NULL,
        ts           DOUBLE PRECISION NOT NULL,
        expires_at   TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (task_id, seq)
    );

    a2a_progress_heartbeats(
        task_id      TEXT PRIMARY KEY,
        ts           DOUBLE PRECISION NOT NULL,
        expires_at   TIMESTAMPTZ NOT NULL
    );
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Iterable
from datetime import datetime, timedelta, timezone

import asyncpg
from google.protobuf.json_format import MessageToJson, Parse
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
# Single global LISTEN channel; the payload carries the task_id so a
# subscriber filters in-process rather than maintaining per-task LISTENs.
_PROGRESS_CHANNEL = "a2a_progress"

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

CREATE TABLE IF NOT EXISTS a2a_progress (
    task_id     TEXT NOT NULL,
    seq         BIGINT NOT NULL,
    message     TEXT NOT NULL,
    ts          DOUBLE PRECISION NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (task_id, seq)
);
CREATE INDEX IF NOT EXISTS a2a_progress_expires_at_idx
    ON a2a_progress (expires_at);

CREATE TABLE IF NOT EXISTS a2a_progress_heartbeats (
    task_id     TEXT PRIMARY KEY,
    ts          DOUBLE PRECISION NOT NULL,
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
        """Remove a task, its context index entry, and its progress log."""
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
                await connection.execute(
                    "DELETE FROM a2a_progress WHERE task_id = $1", task_id,
                )
                await connection.execute(
                    "DELETE FROM a2a_progress_heartbeats WHERE task_id = $1", task_id,
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

    # ── Progress log + heartbeat ──────────────────────────────────────────────

    async def append_progress(self, task_id: str, message: str) -> int:
        """Allocate the next seq, insert the entry, and bump the heartbeat."""
        await self.ensure_schema()
        now = time.time()
        expires_at = _utcnow() + timedelta(seconds=_PROGRESS_TTL_SECONDS)
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                seq = await connection.fetchval(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM a2a_progress "
                    "WHERE task_id = $1",
                    task_id,
                )
                await connection.execute(
                    """
                    INSERT INTO a2a_progress (task_id, seq, message, ts, expires_at)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    task_id, seq, message, now, expires_at,
                )
                await connection.execute(
                    """
                    INSERT INTO a2a_progress_heartbeats (task_id, ts, expires_at)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (task_id) DO UPDATE SET
                        ts = EXCLUDED.ts,
                        expires_at = EXCLUDED.expires_at
                    """,
                    task_id, now, expires_at,
                )
                # Wake any live ``subscribe_progress`` tailers. The NOTIFY
                # fires when the surrounding transaction commits, so the
                # row a subscriber re-reads is guaranteed visible.
                await connection.execute(
                    "SELECT pg_notify($1, $2)", _PROGRESS_CHANNEL, task_id,
                )
        return seq

    async def read_progress(
        self,
        task_id: str,
        since_seq: int = 0,
    ) -> list[ProgressEntry]:
        rows = await self._pool.fetch(
            """
            SELECT seq, message, ts
            FROM a2a_progress
            WHERE task_id = $1 AND seq > $2 AND expires_at > NOW()
            ORDER BY seq
            """,
            task_id, since_seq,
        )
        return [
            ProgressEntry(seq=row["seq"], message=row["message"], ts=row["ts"])
            for row in rows
        ]

    async def subscribe_progress(
        self,
        task_id: str,
        since_seq: int = 0,
    ) -> AsyncIterator[ProgressEntry]:
        """Live-tail progress for ``task_id`` using ``LISTEN/NOTIFY``.

        Holds one pooled connection for the LISTEN; releases it on
        consumer aclose / GC. On each NOTIFY the subscriber re-reads
        ``a2a_progress`` for rows past ``last_seq`` — cheap and avoids
        encoding the entire entry into the 8 KB NOTIFY payload limit.
        """
        await self.ensure_schema()
        wake: asyncio.Queue[None] = asyncio.Queue()

        def _on_notify(_connection, _pid, _channel, payload):
            if payload == task_id:
                wake.put_nowait(None)

        async with self._pool.acquire() as listen_conn:
            await listen_conn.add_listener(_PROGRESS_CHANNEL, _on_notify)
            try:
                last_seq = since_seq
                for entry in await self.read_progress(task_id, last_seq):
                    last_seq = entry.seq
                    yield entry
                while True:
                    await wake.get()
                    for entry in await self.read_progress(task_id, last_seq):
                        last_seq = entry.seq
                        yield entry
            finally:
                with contextlib.suppress(Exception):
                    await listen_conn.remove_listener(
                        _PROGRESS_CHANNEL, _on_notify,
                    )

    async def clear_progress(self, task_id: str) -> None:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM a2a_progress WHERE task_id = $1", task_id,
                )
                await connection.execute(
                    "DELETE FROM a2a_progress_heartbeats WHERE task_id = $1", task_id,
                )

    async def heartbeat(self, task_id: str) -> None:
        await self.ensure_schema()
        expires_at = _utcnow() + timedelta(seconds=_PROGRESS_TTL_SECONDS)
        await self._pool.execute(
            """
            INSERT INTO a2a_progress_heartbeats (task_id, ts, expires_at)
            VALUES ($1, $2, $3)
            ON CONFLICT (task_id) DO UPDATE SET
                ts = EXCLUDED.ts,
                expires_at = EXCLUDED.expires_at
            """,
            task_id, time.time(), expires_at,
        )

    async def get_heartbeat(self, task_id: str) -> float | None:
        value = await self._pool.fetchval(
            "SELECT ts FROM a2a_progress_heartbeats "
            "WHERE task_id = $1 AND expires_at > NOW()",
            task_id,
        )
        return value

    async def finalize_task(
        self,
        task_id: str,
        *,
        state: TaskState,
        status_message: str | None = None,
        artifacts: Iterable[Artifact] | None = None,
    ) -> bool:
        """Atomic terminal write inside a single transaction.

        Uses ``SELECT ... FOR UPDATE`` so concurrent finalizers serialize on
        the task row; the in-flight transaction sees the most recent state,
        short-circuits if already terminal, and commits the new payload +
        progress cleanup together.
        """
        await self.ensure_schema()
        new_payload: str | None = None
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    "SELECT task_json, context_id, expires_at "
                    "FROM a2a_tasks WHERE id = $1 FOR UPDATE",
                    task_id,
                )
                if row is None:
                    return False
                try:
                    task = Parse(row["task_json"], Task())
                except Exception:
                    log.exception(
                        "finalize_task: corrupted payload for %s", task_id,
                    )
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
                new_payload = MessageToJson(task)
                await connection.execute(
                    "UPDATE a2a_tasks SET task_json = $1, expires_at = $2 "
                    "WHERE id = $3",
                    new_payload,
                    _utcnow() + timedelta(seconds=_TTL_SECONDS),
                    task_id,
                )
                await connection.execute(
                    "DELETE FROM a2a_progress WHERE task_id = $1", task_id,
                )
                await connection.execute(
                    "DELETE FROM a2a_progress_heartbeats WHERE task_id = $1",
                    task_id,
                )
        return True

    async def list_stale_working_tasks(self, threshold_secs: float) -> list[str]:
        """Find WORKING tasks whose heartbeat is older than the threshold.

        Joins ``a2a_progress_heartbeats`` (small, TTL-bounded) against
        ``a2a_tasks`` so the scan only inspects tasks with recorded
        heartbeats; tasks WORKING but never ticked won't appear here, but
        the request-handler's lazy stale-detection still catches them on
        the next GetTask.
        """
        await self.ensure_schema()
        cutoff = time.time() - threshold_secs
        rows = await self._pool.fetch(
            """
            SELECT t.id, t.task_json
            FROM a2a_tasks t
            JOIN a2a_progress_heartbeats h ON h.task_id = t.id
            WHERE h.ts < $1
              AND t.expires_at > NOW()
            """,
            cutoff,
        )
        stale: list[str] = []
        for row in rows:
            try:
                task = Parse(row["task_json"], Task())
            except Exception:
                continue
            if task.status.state == TaskState.TASK_STATE_WORKING:
                stale.append(row["id"])
        return stale

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
