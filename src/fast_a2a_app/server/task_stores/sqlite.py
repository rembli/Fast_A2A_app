"""
sqlite.py — SQLite-backed A2A task store

A single-file, zero-infrastructure persistent store. Unlike
:class:`MemoryTaskStore` the state survives a process restart (tasks,
context index, cancel signals, and the progress log all live on disk),
but unlike Redis / Mongo / Postgres it is **single-writer**: SQLite is a
file, not a server, so this backend targets a single app process (the
common "one container with a mounted volume" deployment), not a
horizontally-scaled fleet.

WHEN TO USE
-----------
A single-process deployment that must survive restarts without standing
up an external database — e.g. a small internal agent on a mounted
volume. Tasks and their replayable progress log persist across reboots,
so the crash-recovery sweeper (:func:`clean_up_stale_tasks`) can unstick
in-flight tasks after a bounce.

WHEN NOT TO USE
---------------
Multiple app replicas sharing the store. SQLite's file locking serialises
writers and the live progress tail is in-process only (no cross-process
pub/sub), so cross-instance cancel and real-time progress fan-out across
replicas won't work. Use :class:`RedisTaskStore` / :class:`PostgresTaskStore`
for that.

CONCURRENCY
-----------
All access goes through one :class:`aiosqlite.Connection` (WAL mode) and
a single ``asyncio.Lock``. Within one process that is plenty: the write
volume is modest and serialising keeps multi-statement transactions and
the per-task ``seq`` allocation race-free without advisory locks. Live
progress tailing is delivered via in-process subscriber queues — the same
mechanism :class:`MemoryTaskStore` uses — fanned out under the lock so
subscribers observe entries in persisted order.

Rows carry an ``expires_at`` (unix seconds) and are filtered at read time
(``expires_at > :now``); a periodic external sweep can reclaim space.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Iterable
from pathlib import Path

import aiosqlite
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

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS a2a_tasks (
    id          TEXT PRIMARY KEY,
    task_json   TEXT NOT NULL,
    context_id  TEXT,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS a2a_tasks_context_id_idx
    ON a2a_tasks (context_id);
CREATE INDEX IF NOT EXISTS a2a_tasks_expires_at_idx
    ON a2a_tasks (expires_at);

CREATE TABLE IF NOT EXISTS a2a_context_index (
    context_id  TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    sequence    INTEGER NOT NULL,
    expires_at  REAL NOT NULL,
    PRIMARY KEY (context_id, task_id)
);
CREATE INDEX IF NOT EXISTS a2a_context_index_seq_idx
    ON a2a_context_index (context_id, sequence);

CREATE TABLE IF NOT EXISTS a2a_cancel_signals (
    task_id     TEXT PRIMARY KEY,
    expires_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS a2a_progress (
    task_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    message     TEXT NOT NULL,
    ts          REAL NOT NULL,
    expires_at  REAL NOT NULL,
    PRIMARY KEY (task_id, seq)
);
CREATE INDEX IF NOT EXISTS a2a_progress_expires_at_idx
    ON a2a_progress (expires_at);

CREATE TABLE IF NOT EXISTS a2a_progress_heartbeats (
    task_id     TEXT PRIMARY KEY,
    ts          REAL NOT NULL,
    expires_at  REAL NOT NULL
);
"""


class SqliteTaskStore(TaskStore):
    """SQLite-backed A2A task store using ``aiosqlite`` and ``expires_at`` columns."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._db = connection
        self._lock = asyncio.Lock()
        self._schema_ready = False
        # Per-task list of live-tail queues. Each ``subscribe_progress`` call
        # registers one queue; ``append_progress`` fans an entry out to every
        # queue while still holding ``_lock``, so subscribers see entries in
        # the same order as the persisted log — identical to MemoryTaskStore.
        self._progress_subscribers: dict[
            str, list[asyncio.Queue[ProgressEntry]]
        ] = {}
        log.info("SqliteTaskStore initialized")

    @classmethod
    async def from_path(cls, path: str) -> "SqliteTaskStore":
        """Build a ``SqliteTaskStore`` from a database file path and create tables.

        Parent directories are created if missing. ``":memory:"`` is accepted
        for an ephemeral in-process database (tests); it behaves like
        :class:`MemoryTaskStore` but exercises the SQL path.
        """
        if path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(
                parents=True, exist_ok=True,
            )
        connection = await aiosqlite.connect(path)
        # WAL + NORMAL: concurrent reads while a write is in flight, and a
        # durability/throughput trade-off that's safe for a crash-recovery
        # store (the sweeper re-settles anything lost on a hard kill).
        await connection.execute("PRAGMA journal_mode=WAL")
        await connection.execute("PRAGMA synchronous=NORMAL")
        await connection.execute("PRAGMA foreign_keys=ON")
        await connection.commit()
        store = cls(connection)
        await store.ensure_schema()
        return store

    async def ensure_schema(self) -> None:
        """Create the tables and indexes if they do not already exist. Idempotent."""
        if self._schema_ready:
            return
        async with self._lock:
            if self._schema_ready:
                return
            await self._db.executescript(_SCHEMA_SQL)
            await self._db.commit()
            self._schema_ready = True

    async def close(self) -> None:
        """Close the underlying connection. Safe to call once at shutdown."""
        async with self._lock:
            await self._db.close()

    # ── TaskStore protocol ────────────────────────────────────────────────────

    async def save(self, task: Task, context=None) -> None:
        """Persist a task and update the context index."""
        await self.ensure_schema()
        task_id = task.id
        context_id = task.context_id or None
        now = time.time()
        expires_at = now + _TTL_SECONDS
        payload = MessageToJson(task)

        async with self._lock:
            async with self._db.execute(
                "SELECT context_id FROM a2a_tasks WHERE id = ?", (task_id,),
            ) as cursor:
                row = await cursor.fetchone()
            previous_context_id = row[0] if row else None

            await self._db.execute(
                """
                INSERT INTO a2a_tasks (id, task_json, context_id, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    task_json  = excluded.task_json,
                    context_id = excluded.context_id,
                    expires_at = excluded.expires_at
                """,
                (task_id, payload, context_id, expires_at),
            )

            if previous_context_id and previous_context_id != context_id:
                await self._db.execute(
                    "DELETE FROM a2a_context_index WHERE context_id = ? AND task_id = ?",
                    (previous_context_id, task_id),
                )

            if context_id:
                async with self._db.execute(
                    "SELECT 1 FROM a2a_context_index WHERE context_id = ? AND task_id = ?",
                    (context_id, task_id),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing is None:
                    async with self._db.execute(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 "
                        "FROM a2a_context_index WHERE context_id = ?",
                        (context_id,),
                    ) as cursor:
                        seq_row = await cursor.fetchone()
                    sequence = seq_row[0] if seq_row else 1
                    await self._db.execute(
                        "INSERT INTO a2a_context_index "
                        "(context_id, task_id, sequence, expires_at) "
                        "VALUES (?, ?, ?, ?)",
                        (context_id, task_id, sequence, expires_at),
                    )
                else:
                    await self._db.execute(
                        "UPDATE a2a_context_index SET expires_at = ? "
                        "WHERE context_id = ? AND task_id = ?",
                        (expires_at, context_id, task_id),
                    )
            await self._db.commit()

    async def get(self, task_id: str, context=None) -> Task | None:
        """Return the Task for task_id, or None if absent / expired / corrupted."""
        async with self._lock:
            async with self._db.execute(
                "SELECT task_json FROM a2a_tasks WHERE id = ? AND expires_at > ?",
                (task_id, time.time()),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        try:
            return Parse(row[0], Task())
        except Exception:
            log.exception("Corrupted task payload in SQLite (task_id=%s)", task_id)
            return None

    async def delete(self, task_id: str, context=None) -> None:
        """Remove a task, its context index entry, and its progress log."""
        await self.ensure_schema()
        async with self._lock:
            async with self._db.execute(
                "SELECT context_id FROM a2a_tasks WHERE id = ?", (task_id,),
            ) as cursor:
                row = await cursor.fetchone()
            await self._db.execute("DELETE FROM a2a_tasks WHERE id = ?", (task_id,))
            if row and row[0]:
                await self._db.execute(
                    "DELETE FROM a2a_context_index WHERE context_id = ? AND task_id = ?",
                    (row[0], task_id),
                )
            await self._db.execute(
                "DELETE FROM a2a_progress WHERE task_id = ?", (task_id,),
            )
            await self._db.execute(
                "DELETE FROM a2a_progress_heartbeats WHERE task_id = ?", (task_id,),
            )
            await self._db.commit()

    async def list(self, params: ListTasksRequest, context=None) -> ListTasksResponse:
        """Return an empty list — full task listing is not required for A2A operation."""
        return ListTasksResponse()

    # ── Extended interface ────────────────────────────────────────────────────

    async def signal_cancel(self, task_id: str) -> None:
        """Write a short-lived cancel signal readable by is_cancel_signalled()."""
        await self.ensure_schema()
        expires_at = time.time() + _CANCEL_SIGNAL_TTL_SECONDS
        async with self._lock:
            await self._db.execute(
                """
                INSERT INTO a2a_cancel_signals (task_id, expires_at)
                VALUES (?, ?)
                ON CONFLICT (task_id) DO UPDATE SET expires_at = excluded.expires_at
                """,
                (task_id, expires_at),
            )
            await self._db.commit()

    async def is_cancel_signalled(self, task_id: str) -> bool:
        """Return True if signal_cancel was called and the signal has not expired."""
        async with self._lock:
            async with self._db.execute(
                "SELECT 1 FROM a2a_cancel_signals WHERE task_id = ? AND expires_at > ?",
                (task_id, time.time()),
            ) as cursor:
                row = await cursor.fetchone()
        return row is not None

    # ── Progress log + heartbeat ──────────────────────────────────────────────

    async def append_progress(self, task_id: str, message: str) -> int:
        """Allocate the next seq, insert the entry, bump the heartbeat, fan out.

        Serialised by ``self._lock`` so the ``MAX(seq)+1`` allocation can't
        race another append for the same task; the fan-out to live subscribers
        happens under the same lock so tailers observe persisted order.
        """
        await self.ensure_schema()
        now = time.time()
        expires_at = now + _PROGRESS_TTL_SECONDS
        async with self._lock:
            async with self._db.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM a2a_progress WHERE task_id = ?",
                (task_id,),
            ) as cursor:
                seq_row = await cursor.fetchone()
            seq = seq_row[0] if seq_row else 1
            await self._db.execute(
                "INSERT INTO a2a_progress (task_id, seq, message, ts, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, seq, message, now, expires_at),
            )
            await self._db.execute(
                """
                INSERT INTO a2a_progress_heartbeats (task_id, ts, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT (task_id) DO UPDATE SET
                    ts = excluded.ts,
                    expires_at = excluded.expires_at
                """,
                (task_id, now, expires_at),
            )
            await self._db.commit()
            entry = ProgressEntry(seq=seq, message=message, ts=now)
            for queue in self._progress_subscribers.get(task_id, ()):
                queue.put_nowait(entry)
        return seq

    async def read_progress(
        self,
        task_id: str,
        since_seq: int = 0,
    ) -> list[ProgressEntry]:
        async with self._lock:
            async with self._db.execute(
                """
                SELECT seq, message, ts
                FROM a2a_progress
                WHERE task_id = ? AND seq > ? AND expires_at > ?
                ORDER BY seq
                """,
                (task_id, since_seq, time.time()),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            ProgressEntry(seq=row[0], message=row[1], ts=row[2]) for row in rows
        ]

    async def subscribe_progress(
        self,
        task_id: str,
        since_seq: int = 0,
    ) -> AsyncIterator[ProgressEntry]:
        """Yield persisted entries first, then any future appends live.

        Live delivery is in-process only (SQLite has no pub/sub): a queue is
        registered under ``self._lock`` so every entry written after this
        point is fanned out to it, with the pre-registration snapshot replayed
        first and overlapping seqs suppressed.
        """
        await self.ensure_schema()
        queue: asyncio.Queue[ProgressEntry] = asyncio.Queue()
        async with self._lock:
            self._progress_subscribers.setdefault(task_id, []).append(queue)
            async with self._db.execute(
                """
                SELECT seq, message, ts
                FROM a2a_progress
                WHERE task_id = ? AND seq > ? AND expires_at > ?
                ORDER BY seq
                """,
                (task_id, since_seq, time.time()),
            ) as cursor:
                rows = await cursor.fetchall()
            snapshot = [
                ProgressEntry(seq=row[0], message=row[1], ts=row[2]) for row in rows
            ]
            last_seq = snapshot[-1].seq if snapshot else since_seq
        try:
            for entry in snapshot:
                yield entry
            while True:
                entry = await queue.get()
                if entry.seq <= last_seq:
                    continue
                last_seq = entry.seq
                yield entry
        finally:
            async with self._lock:
                subscribers = self._progress_subscribers.get(task_id)
                if subscribers is not None:
                    try:
                        subscribers.remove(queue)
                    except ValueError:
                        pass
                    if not subscribers:
                        self._progress_subscribers.pop(task_id, None)

    async def clear_progress(self, task_id: str) -> None:
        """Drop all progress records for a task. Called on terminal states."""
        async with self._lock:
            await self._db.execute(
                "DELETE FROM a2a_progress WHERE task_id = ?", (task_id,),
            )
            await self._db.execute(
                "DELETE FROM a2a_progress_heartbeats WHERE task_id = ?", (task_id,),
            )
            await self._db.commit()

    async def heartbeat(self, task_id: str) -> None:
        """Refresh the worker-liveness marker without writing progress."""
        await self.ensure_schema()
        now = time.time()
        expires_at = now + _PROGRESS_TTL_SECONDS
        async with self._lock:
            await self._db.execute(
                """
                INSERT INTO a2a_progress_heartbeats (task_id, ts, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT (task_id) DO UPDATE SET
                    ts = excluded.ts,
                    expires_at = excluded.expires_at
                """,
                (task_id, now, expires_at),
            )
            await self._db.commit()

    async def get_heartbeat(self, task_id: str) -> float | None:
        """Return the last heartbeat as unix seconds, or ``None`` if absent."""
        async with self._lock:
            async with self._db.execute(
                "SELECT ts FROM a2a_progress_heartbeats "
                "WHERE task_id = ? AND expires_at > ?",
                (task_id, time.time()),
            ) as cursor:
                row = await cursor.fetchone()
        return row[0] if row else None

    async def finalize_task(
        self,
        task_id: str,
        *,
        state: TaskState,
        status_message: str | None = None,
        artifacts: Iterable[Artifact] | None = None,
    ) -> bool:
        """Atomically transition ``task_id`` to ``state`` and clear progress.

        The whole read-modify-write runs under ``self._lock`` inside a single
        committed transaction, so concurrent finalizers serialise: the second
        one re-reads the now-terminal state and no-ops (returns ``False``).
        """
        await self.ensure_schema()
        async with self._lock:
            async with self._db.execute(
                "SELECT task_json FROM a2a_tasks WHERE id = ?", (task_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return False
            try:
                task = Parse(row[0], Task())
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
            await self._db.execute(
                "UPDATE a2a_tasks SET task_json = ?, expires_at = ? WHERE id = ?",
                (MessageToJson(task), time.time() + _TTL_SECONDS, task_id),
            )
            await self._db.execute(
                "DELETE FROM a2a_progress WHERE task_id = ?", (task_id,),
            )
            await self._db.execute(
                "DELETE FROM a2a_progress_heartbeats WHERE task_id = ?", (task_id,),
            )
            await self._db.commit()
        return True

    async def list_stale_working_tasks(self, threshold_secs: float) -> list[str]:
        """Return WORKING task_ids whose heartbeat is older than the threshold."""
        await self.ensure_schema()
        cutoff = time.time() - threshold_secs
        async with self._lock:
            async with self._db.execute(
                """
                SELECT t.id, t.task_json
                FROM a2a_tasks t
                JOIN a2a_progress_heartbeats h ON h.task_id = t.id
                WHERE h.ts < ? AND t.expires_at > ?
                """,
                (cutoff, time.time()),
            ) as cursor:
                rows = await cursor.fetchall()
        stale: list[str] = []
        for row in rows:
            try:
                task = Parse(row[1], Task())
            except Exception:
                continue
            if task.status.state == TaskState.TASK_STATE_WORKING:
                stale.append(row[0])
        return stale

    async def list_by_context(
        self,
        context_id: str,
        exclude_task_id: str | None = None,
    ) -> list[Task]:
        """Return all tasks for context_id in creation order."""
        now = time.time()
        async with self._lock:
            if exclude_task_id:
                query = """
                    SELECT t.task_json
                    FROM a2a_context_index ci
                    JOIN a2a_tasks t ON t.id = ci.task_id
                    WHERE ci.context_id = ? AND ci.task_id <> ? AND t.expires_at > ?
                    ORDER BY ci.sequence
                """
                args: tuple = (context_id, exclude_task_id, now)
            else:
                query = """
                    SELECT t.task_json
                    FROM a2a_context_index ci
                    JOIN a2a_tasks t ON t.id = ci.task_id
                    WHERE ci.context_id = ? AND t.expires_at > ?
                    ORDER BY ci.sequence
                """
                args = (context_id, now)
            async with self._db.execute(query, args) as cursor:
                rows = await cursor.fetchall()

        tasks: list[Task] = []
        for row in rows:
            try:
                tasks.append(Parse(row[0], Task()))
            except Exception:
                log.exception("Corrupted task payload in SQLite, skipping")
        return tasks
