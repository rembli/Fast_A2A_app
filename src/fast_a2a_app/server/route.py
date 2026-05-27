"""
route.py — Framework-agnostic A2A protocol adapter

A2A (Agent-to-Agent) is a JSON-RPC protocol for agent interoperability.
Clients send tasks via HTTP POST and receive responses as a single JSON
reply or as a stream of Server-Sent Events. This module is the adapter
layer between the A2A protocol and your agent: it speaks A2A on one side
and calls your agent function on the other.

PUBLIC SURFACE
--------------
build_a2a_app               — Assembles a Starlette ASGI application that
                              handles all A2A JSON-RPC methods. Mount it
                              at any prefix in your FastAPI application.

build_invoke                — Wraps any async (str) -> str function as a
                              non-streaming A2A invoke callable.

build_stream_invoke         — Wraps any async (str) -> AsyncIterable[str]
                              generator as a streaming A2A invoke callable.

ConfigurableAgentExecutor   — AgentExecutor that calls invoke or
                              stream_invoke, handles errors, and routes
                              cancel signals. Supports optional
                              on_task_start / on_task_cancel hooks.

ContextAwareRequestContextBuilder
                            — RequestContextBuilder that enriches each
                              request with prior-task history for the same
                              context_id, enabling multi-turn continuity.

HOW STREAMING WORKS
-------------------
build_stream_invoke is a thin signature normaliser around the user's
generator; chunks land on the SSE event queue as text/artifact updates.
Progress messages flow on a separate path: tools call
report_progress(msg), the framework resolves the active executor and
task_id from request-scoped ContextVars, appends to the configured
A2ATaskStore, and a per-task subscriber inside ConfigurableAgentExecutor
re-emits each entry as a TASK_STATE_WORKING SSE event. The store is the
single source of truth — same path delivers progress to resubscribers.

HOW CROSS-INSTANCE CANCELLATION WORKS
--------------------------------------
A cancel request may arrive at replica B while the task runs on replica A.
cancel() either fires asyncio.Task.cancel() locally (same instance) or
calls A2ATaskStore.signal_cancel() (different instance) — the transport is
whatever the configured store uses. A background poller on the executing
instance calls is_cancel_signalled() every _CANCEL_POLL_INTERVAL seconds.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
import uuid
from collections.abc import AsyncIterable, Awaitable, Callable, Iterable

from a2a.helpers import (
    new_task_from_user_message,
    new_text_artifact,
    new_text_message,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.agent_execution.simple_request_context_builder import (
    SimpleRequestContextBuilder,
)
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler, validate_request_params
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.utils.errors import TaskNotFoundError
from a2a.types import (
    AgentCard,
    Artifact,
    GetTaskRequest,
    Part,
    Role,
    SendMessageRequest,
    SubscribeToTaskRequest,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from starlette.routing import Router

from .task_stores import A2ATaskStore, MemoryTaskStore
from .utils import _current_executor, _current_task_id

log = logging.getLogger(__name__)

_TRANSIENT_AGENT_TEXTS = frozenset({"processing request..."})

# Seconds between cancel-signal polls. Lower = faster cancellation, more Redis traffic.
_CANCEL_POLL_INTERVAL = 2

# Seconds between worker heartbeats. The progress-aware request handler treats
# a task whose heartbeat is older than _STALE_THRESHOLD as a crashed worker
# and surfaces it as TASK_STATE_FAILED on the next GetTask / SubscribeToTask.
_HEARTBEAT_INTERVAL = 5
_STALE_THRESHOLD = 30

# How often the non-local resubscribe branch re-checks task state for a
# terminal transition. The progress stream itself wakes on push (LISTEN /
# pub/sub) — this poll is only needed because terminal state lives on
# the Task, not in the progress log.
_REMOTE_STATE_POLL_INTERVAL = 2.0



# ── SSE / message helpers ─────────────────────────────────────────────────────


def _part_text(part: object) -> str | None:
    if hasattr(part, 'HasField') and not part.HasField('text'):
        return None
    text = getattr(part, 'text', None)
    if not text:
        return None
    return str(text).strip()


def _message_texts(message: object) -> list[str]:
    parts = getattr(message, "parts", None) or []
    return [text for part in parts if (text := _part_text(part))]


def _message_pairs(messages: Iterable[object], role: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for message in messages:
        for text in _message_texts(message):
            if role == "agent" and text.lower() in _TRANSIENT_AGENT_TEXTS:
                continue
            pairs.append((role, text))
    return pairs


def _task_pairs(task: object) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    history = getattr(task, "history", None) or []
    for message in history:
        role_int = getattr(message, "role", 0)
        role = "user" if role_int == Role.ROLE_USER else "agent"
        pairs.extend(_message_pairs([message], role))
    artifacts = getattr(task, "artifacts", None) or []
    for artifact in artifacts:
        for text in _message_texts(artifact):
            pairs.append(("agent", text))
    return pairs


def get_user_input(context: RequestContext) -> str:
    """Return the current user message text from a RequestContext.

    Use this when writing a custom ``prompt_builder`` so you don't need to
    know the internal SDK method name::

        def my_prompt(context: RequestContext) -> str:
            return f"Respond in JSON:\\n{get_user_input(context)}"

        build_a2a_app(..., prompt_builder=my_prompt)
    """
    return context.get_user_input()


def get_task_history(context: RequestContext) -> list[tuple[str, str]]:
    """Return prior conversation as ``(role, text)`` pairs, oldest first.

    ``role`` is the literal ``"user"`` or ``"agent"``. Pairs are extracted
    from every ``Task`` attached to ``context.related_tasks`` (history
    messages plus the agent's text artifacts). Returns ``[]`` when there
    is no prior history.

    Pair with :func:`format_history` to build a prompt prefix, or consume
    the raw pairs directly for custom routing logic::

        for role, text in get_task_history(context):
            ...
    """
    related_tasks = getattr(context, "related_tasks", None) or []
    pairs: list[tuple[str, str]] = []
    for task in related_tasks:
        pairs.extend(_task_pairs(task))
    return pairs


_ROLE_LABELS: dict[str, str] = {"user": "User", "agent": "Agent"}


def format_history(
    history: list[tuple[str, str]],
    *,
    max_lines: int = 12,
    header: str = "Conversation so far:",
) -> str:
    """Render ``(role, text)`` pairs as a prompt prefix.

    Caps to the most recent *max_lines* pairs, formats each as
    ``"User: …"`` / ``"Agent: …"``, and prepends *header*. Returns an
    empty string when *history* is empty or *max_lines* <= 0.

    Use in a custom ``prompt_builder``::

        def my_prompt(context: RequestContext) -> str:
            return (
                "You are an expert.\\n\\n"
                + format_history(get_task_history(context), max_lines=6)
                + get_user_input(context)
            )

        build_a2a_app(..., prompt_builder=my_prompt)
    """
    if not history or max_lines <= 0:
        return ""
    recent = history[-max_lines:]
    body = "\n".join(
        f"{_ROLE_LABELS.get(role, role.title())}: {text}" for role, text in recent
    )
    return f"{header}\n{body}\n\n"


# ── Request context builder ───────────────────────────────────────────────────


class ContextAwareRequestContextBuilder(SimpleRequestContextBuilder):
    """RequestContextBuilder that enriches each request with full conversation history."""

    def __init__(self, task_store: A2ATaskStore) -> None:
        super().__init__(
            should_populate_referred_tasks=True,
            task_store=task_store,
        )
        self._task_store = task_store

    async def build(
        self,
        context: ServerCallContext,
        params: SendMessageRequest | None = None,
        task_id: str | None = None,
        context_id: str | None = None,
        task: Task | None = None,
    ) -> RequestContext:
        request_context = await super().build(
            context=context,
            params=params,
            task_id=task_id,
            context_id=context_id,
            task=task,
        )

        effective_context_id = request_context.context_id
        if not effective_context_id:
            return request_context

        context_tasks = await self._task_store.list_by_context(
            effective_context_id,
            exclude_task_id=request_context.task_id,
        )
        known_task_ids = {rt.id for rt in request_context.related_tasks}
        for rt in context_tasks:
            if rt.id not in known_task_ids:
                request_context.attach_related_task(rt)

        return request_context


# ── Agent executor ────────────────────────────────────────────────────────────


class ConfigurableAgentExecutor(AgentExecutor):
    """A2A executor wrapping a callable agent with optional lifecycle hooks.

    Cross-instance cancellation: when a cancel request arrives at a different
    replica a signal is written to the shared task store. A background poller
    on the executing instance detects it and fires asyncio.Task.cancel().

    on_task_start(context_id)  — called before the agent starts work.
    on_task_cancel(context_id) — called when a cancel request is received.
    """

    def __init__(
        self,
        invoke: Callable[[str, RequestContext], Awaitable[str | Artifact]] | None = None,
        prompt_builder: Callable[[RequestContext], str] | None = None,
        stream_invoke: Callable[[str, RequestContext], AsyncIterable[str | Artifact]]
        | None = None,
        on_task_start: Callable[[str], Awaitable[None]] | None = None,
        on_task_cancel: Callable[[str, str], Awaitable[None]] | None = None,
        task_store: A2ATaskStore | None = None,
        debug: bool = False,
    ) -> None:
        self._invoke = invoke
        self._prompt_builder = prompt_builder or self._default_prompt_builder
        self._stream_invoke = stream_invoke
        self._on_task_start = on_task_start
        self._on_task_cancel = on_task_cancel
        self._task_store = task_store
        self._debug = debug
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._task_contexts: dict[str, str | None] = {}

    @staticmethod
    def _default_prompt_builder(context: RequestContext) -> str:
        return context.get_user_input()

    def is_running_locally(self, task_id: str) -> bool:
        """True if ``execute()`` is currently driving ``task_id`` in this process.

        Used by the progress-aware request handler to decide whether a live
        SSE re-subscription is possible (same-process reconnect) or whether
        the only safe response is a snapshot + persisted-progress replay.
        """
        running = self._running_tasks.get(task_id)
        return running is not None and not running.done()

    def report_progress(self, message: str) -> None:
        """Append a status string to the current task's progress log.

        Fire-and-forget: schedules a background write on the running event
        loop and returns immediately so callers don't pay round-trip latency.
        Silently no-ops when there is no task store, no task id in scope, or
        no running loop (e.g. called from a sync context after execute()
        exited).

        The task_id is resolved from the ``_current_task_id`` ContextVar
        rather than from instance state — the executor object is shared
        across concurrent requests, but each ``execute()`` runs in its own
        contextvars context so the task_id never collides.
        """
        task_id = _current_task_id.get()
        if not self._task_store or not task_id:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._safe_append_progress(task_id, message))

    async def _safe_append_progress(self, task_id: str, message: str) -> None:
        try:
            await self._task_store.append_progress(task_id, message)
        except Exception:
            log.warning(
                "Progress append failed (task=%s)", task_id, exc_info=True,
            )

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Run the agent for one A2A task, emitting task/status/artifact events."""
        message = context.message
        if message is None:
            raise ValueError("A2A request did not include a message")

        task = context.current_task or new_task_from_user_message(message)
        task_id = task.id
        context_id = task.context_id or context.context_id

        if self._on_task_start and context_id:
            await self._on_task_start(context_id)

        self._task_contexts[task_id] = context_id
        executor_token = _current_executor.set(self)
        task_id_token = _current_task_id.set(task_id)

        await event_queue.enqueue_event(task)
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                status=TaskStatus(
                    state=TaskState.TASK_STATE_WORKING,
                    message=new_text_message(
                        "Processing request...",
                        context_id=context_id,
                        task_id=task_id,
                    ),
                ),
            )
        )

        async def _do_work() -> None:
            try:
                prompt = self._prompt_builder(context)
                if self._stream_invoke is not None:
                    streamed = await self._stream_response(
                        prompt, context, event_queue, task_id, context_id
                    )
                    if streamed:
                        await event_queue.enqueue_event(
                            TaskStatusUpdateEvent(
                                task_id=task_id,
                                context_id=context_id,
                                status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                            )
                        )
                        return

                if self._invoke is None:
                    return
                response = await self._invoke(prompt, context)
            except Exception as exc:
                log.exception(
                    "A2A executor failed (task=%s context=%s)", task_id, context_id
                )
                with contextlib.suppress(Exception):
                    await event_queue.enqueue_event(
                        TaskStatusUpdateEvent(
                            task_id=task_id,
                            context_id=context_id,
                            status=TaskStatus(
                                state=TaskState.TASK_STATE_FAILED,
                                message=new_text_message(
                                    f"Agent execution failed: {exc}"
                                    if self._debug
                                    else "Something went wrong. Please try again.",
                                    context_id=context_id,
                                    task_id=task_id,
                                ),
                            ),
                        )
                    )
                return

            if isinstance(response, list):
                artifacts = response
            elif isinstance(response, Artifact):
                artifacts = [response]
            else:
                artifacts = [new_text_artifact(name="result", text=response.strip())]

            for artifact in artifacts:
                await event_queue.enqueue_event(
                    TaskArtifactUpdateEvent(
                        task_id=task_id,
                        context_id=context_id,
                        artifact=artifact,
                    )
                )
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                )
            )

        asyncio_task = asyncio.create_task(_do_work())
        self._running_tasks[task_id] = asyncio_task

        poll_task: asyncio.Task | None = None
        heartbeat_task: asyncio.Task | None = None
        progress_task: asyncio.Task | None = None
        if self._task_store and task_id:
            store = self._task_store

            async def _poll_cancel_signal() -> None:
                while not asyncio_task.done():
                    try:
                        if await store.is_cancel_signalled(task_id):
                            asyncio_task.cancel()
                            return
                    except Exception:
                        log.debug("Cancel-signal poll error (task=%s)", task_id)
                    await asyncio.sleep(_CANCEL_POLL_INTERVAL)

            async def _heartbeat() -> None:
                # Prime immediately so a fast-failing task still records a
                # heartbeat that the stale-detector can compare against.
                try:
                    await store.heartbeat(task_id)
                except Exception:
                    log.debug("Heartbeat write error (task=%s)", task_id)
                while not asyncio_task.done():
                    await asyncio.sleep(_HEARTBEAT_INTERVAL)
                    try:
                        await store.heartbeat(task_id)
                    except Exception:
                        log.debug("Heartbeat write error (task=%s)", task_id)

            async def _stream_progress() -> None:
                # Tail the persisted progress log and re-emit each entry as a
                # WORKING-status SSE event. ``report_progress`` writes flow
                # through the task store; this is the single path that turns
                # them into client-visible updates — same mechanism as the
                # remote-resubscribe tail, so local + cross-replica behave
                # identically.
                try:
                    async for entry in store.subscribe_progress(task_id):
                        await event_queue.enqueue_event(
                            TaskStatusUpdateEvent(
                                task_id=task_id,
                                context_id=context_id,
                                status=TaskStatus(
                                    state=TaskState.TASK_STATE_WORKING,
                                    message=new_text_message(
                                        entry.message,
                                        context_id=context_id,
                                        task_id=task_id,
                                    ),
                                ),
                            )
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning(
                        "Progress subscriber failed (task=%s)", task_id,
                        exc_info=True,
                    )

            poll_task = asyncio.create_task(_poll_cancel_signal())
            heartbeat_task = asyncio.create_task(_heartbeat())
            progress_task = asyncio.create_task(_stream_progress())

        try:
            await asyncio_task
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await event_queue.enqueue_event(
                    TaskStatusUpdateEvent(
                        task_id=task_id,
                        context_id=context_id,
                        status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
                    )
                )
        finally:
            if poll_task is not None:
                poll_task.cancel()
            if heartbeat_task is not None:
                heartbeat_task.cancel()
            if progress_task is not None:
                progress_task.cancel()
            if self._task_store and task_id:
                # Drop the persisted progress log now that the task has
                # reached a terminal state — no further replay is useful and
                # we don't want to leak rows up to the 24 h TTL.
                with contextlib.suppress(Exception):
                    await self._task_store.clear_progress(task_id)
            self._running_tasks.pop(task_id, None)
            self._task_contexts.pop(task_id, None)
            _current_task_id.reset(task_id_token)
            _current_executor.reset(executor_token)

    async def _stream_response(
        self,
        prompt: str,
        context: RequestContext,
        event_queue: EventQueue,
        task_id: str,
        context_id: str | None,
    ) -> bool:
        """Drain stream_invoke, routing progress to working-status events and text/artifact events.

        The generator may yield:
          - str          — accumulated into a streaming text artifact
          - NEW_MESSAGE  — flush the current text artifact and start a new one
          - Artifact     — emitted immediately as-is (preserves part types)
        """
        assert self._stream_invoke is not None

        artifact_id = str(uuid.uuid4())
        chunk_count = 0
        pending_chunk: str | None = None
        any_artifact = False

        async def _flush_pending(last: bool) -> None:
            nonlocal pending_chunk, chunk_count, artifact_id, any_artifact
            if pending_chunk is None:
                return
            await self._enqueue_text_chunk(
                event_queue,
                task_id,
                context_id,
                pending_chunk,
                artifact_id=artifact_id,
                append=chunk_count > 0,
                last_chunk=last,
            )
            any_artifact = True
            chunk_count += 1
            pending_chunk = None

        async for chunk in self._stream_invoke(prompt, context):
            if isinstance(chunk, Artifact):
                await _flush_pending(last=True)
                await event_queue.enqueue_event(
                    TaskArtifactUpdateEvent(
                        task_id=task_id,
                        context_id=context_id,
                        artifact=chunk,
                    )
                )
                any_artifact = True
                artifact_id = str(uuid.uuid4())
                chunk_count = 0
                continue

            if not chunk:
                continue

            if pending_chunk is None:
                pending_chunk = chunk
                continue

            await self._enqueue_text_chunk(
                event_queue,
                task_id,
                context_id,
                pending_chunk,
                artifact_id=artifact_id,
                append=chunk_count > 0,
                last_chunk=False,
            )
            any_artifact = True
            chunk_count += 1
            pending_chunk = chunk

        await _flush_pending(last=True)
        return any_artifact

    async def _enqueue_text_chunk(
        self,
        event_queue: EventQueue,
        task_id: str,
        context_id: str | None,
        text: str,
        *,
        artifact_id: str,
        append: bool,
        last_chunk: bool,
    ) -> None:
        await event_queue.enqueue_event(
            TaskArtifactUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                artifact=Artifact(
                    artifact_id=artifact_id,
                    name="result",
                    parts=[Part(text=text)],
                ),
                append=append,
                last_chunk=last_chunk,
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Cancel directly if running on this instance, or via the task store's signal for another replica."""
        task_id = context.task_id or ""
        context_id = (
            getattr(context, "context_id", None)
            or self._task_contexts.get(task_id)
        )

        if self._on_task_cancel and context_id:
            await self._on_task_cancel(context_id, task_id)

        running = self._running_tasks.get(task_id)
        if running and not running.done():
            running.cancel()
        elif self._task_store and task_id:
            with contextlib.suppress(Exception):
                await self._task_store.signal_cancel(task_id)

        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
            )
        )


# ── Progress-aware request handler ────────────────────────────────────────────

_TERMINAL_TASK_STATES = frozenset({
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_REJECTED,
})


class ProgressAwareRequestHandler(DefaultRequestHandler):
    """DefaultRequestHandler that replays persisted progress on resubscribe
    and lazily marks zombie-WORKING tasks as FAILED.

    Two failure modes the upstream handler can't recover from on its own:

    1. **Crashed worker.** A task left in TASK_STATE_WORKING after its
       executing replica died would hang the UI indefinitely (no terminal
       event is ever emitted). On ``GetTask`` / ``SubscribeToTask`` we check
       the per-task heartbeat written by ``ConfigurableAgentExecutor``; if
       it's older than ``_STALE_THRESHOLD`` and no replica is currently
       running the task, we route the task through ``on_task_recover`` (if
       configured) or default-finalize it as ``TASK_STATE_FAILED`` so the
       UI sees a terminal state.

    2. **Lost progress.** ``report_progress(...)`` strings live only in
       the executor's in-memory SSE queue. A network blip, a tab refresh,
       or a worker bounce drops every message the UI hadn't already
       received. The executor now persists them via the task store; we
       replay the log here so the UI's thinking indicator picks up where
       it left off when ``resubscribeTaskStream()`` reconnects.

    Both checks are lazy — no background sweeper. The next request from
    a client is what triggers the FAILED transition. Pair with
    :func:`clean_up_stale_tasks` (called from app lifespan startup) to
    also cover the no-traffic case after a restart.
    """

    def __init__(
        self,
        *,
        agent_executor: ConfigurableAgentExecutor,
        task_store: A2ATaskStore,
        progress_store: A2ATaskStore,
        on_task_recover: Callable[[str], Awaitable[None]] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(
            agent_executor=agent_executor,
            task_store=task_store,
            **kwargs,
        )
        self._executor = agent_executor
        # Kept as a separate attribute so type checkers don't have to assume
        # ``self.task_store`` is the extended ``A2ATaskStore`` Protocol (the
        # SDK base class types it as the narrower SDK ``TaskStore``).
        self._progress_store: A2ATaskStore = progress_store
        self._on_task_recover = on_task_recover

    async def _is_stale(self, task_id: str) -> bool:
        """True if the task has a heartbeat older than ``_STALE_THRESHOLD``.

        Returns ``False`` (i.e. assume alive) when there's no heartbeat
        recorded — covers tasks created before the store was upgraded and
        tasks that never started.
        """
        try:
            hb = await self._progress_store.get_heartbeat(task_id)
        except Exception:
            log.debug("Stale check failed for task=%s", task_id, exc_info=True)
            return False
        if hb is None:
            return False
        return (time.time() - hb) > _STALE_THRESHOLD

    async def _recover_stuck_task(self, task: Task, context) -> Task:
        """Settle a task whose worker died mid-flight.

        Calls ``on_task_recover`` first (best-effort, advisory) so a
        custom hook can do agent-side cleanup — e.g. cancel a still-
        pending durable workflow, emit telemetry, write a tailored
        status message via ``finalize_task``. The framework then
        default-finalizes as ``TASK_STATE_FAILED`` via
        ``finalize_task``, which is idempotent: a hook that already
        wrote a terminal state leaves it intact.

        Returns the task in its post-recovery state. The original
        ``task`` argument is returned unchanged on any error so the
        caller still has something to yield.
        """
        if self._on_task_recover is not None:
            try:
                await self._on_task_recover(task.id)
            except Exception:
                log.exception(
                    "on_task_recover hook failed for task=%s — falling through to default finalize",
                    task.id,
                )

        with contextlib.suppress(Exception):
            await self._progress_store.finalize_task(
                task.id,
                state=TaskState.TASK_STATE_FAILED,
                status_message="Agent worker stopped unexpectedly. Please retry.",
            )
        with contextlib.suppress(Exception):
            refreshed = await self._progress_store.get(task.id, context)
            if refreshed is not None:
                return refreshed
        return task

    def _progress_to_event(
        self,
        task: Task,
        message: str,
    ) -> TaskStatusUpdateEvent:
        return TaskStatusUpdateEvent(
            task_id=task.id,
            context_id=task.context_id,
            status=TaskStatus(
                state=TaskState.TASK_STATE_WORKING,
                message=new_text_message(
                    message,
                    context_id=task.context_id,
                    task_id=task.id,
                ),
            ),
        )

    @validate_request_params
    async def on_get_task(self, params: GetTaskRequest, context):  # type: ignore[override]
        """Lazily mark zombie-WORKING tasks as FAILED before returning them.

        "Zombie" = WORKING task whose log hasn't been touched in
        ``_STALE_THRESHOLD`` seconds. Anything still actively writing
        progress (whether the in-process executor or a recovered
        durable-execution workflow) refreshes the heartbeat as a side
        effect of every ``append_progress`` write, so a stale heartbeat
        unambiguously means nobody is making forward progress on this
        task.
        """
        task = await super().on_get_task(params, context)
        if (
            task is not None
            and task.status.state == TaskState.TASK_STATE_WORKING
            and not self._executor.is_running_locally(task.id)
            and await self._is_stale(task.id)
        ):
            task = await self._recover_stuck_task(task, context)
        return task

    @validate_request_params
    async def on_subscribe_to_task(  # type: ignore[override]
        self,
        params: SubscribeToTaskRequest,
        context,
    ):
        """Replay persisted progress before delegating to the SDK's live stream.

        For tasks running on this replica: yield the current Task snapshot,
        replay any persisted progress as ``TASK_STATE_WORKING`` events, then
        attach to the live event queue via ``super().on_subscribe_to_task``
        (skipping its own initial Task event so we don't double-emit).

        For tasks not running locally (crashed worker, or another replica):
        replay everything we have from the store; the UI's snapshot fallback
        will poll for the final state. We don't delegate to super() in this
        case because the SDK would create a fresh ``ActiveTask`` and try to
        re-execute the agent.
        """
        task_id = params.id
        task = await self._progress_store.get(task_id, context)
        if task is None:
            # SDK's super() implementation hangs on a missing task (it
            # creates an empty ActiveTask and waits for events). Raising
            # TaskNotFoundError directly matches the JSON-RPC error mapping.
            raise TaskNotFoundError

        state = task.status.state

        # Terminal: just yield the snapshot. Nothing to subscribe to.
        if state in _TERMINAL_TASK_STATES:
            yield task
            return

        # WORKING but worker is gone — recover (custom hook or default
        # FAILED). Yielding both the full Task (so the UI's transcript
        # stays in sync) and an explicit status update lets
        # ``processStreamPayload`` raise immediately instead of waiting
        # for the snapshot fallback to detect it.
        if (
            state == TaskState.TASK_STATE_WORKING
            and not self._executor.is_running_locally(task_id)
            and await self._is_stale(task_id)
        ):
            task = await self._recover_stuck_task(task, context)
            yield task
            yield TaskStatusUpdateEvent(
                task_id=task.id,
                context_id=task.context_id,
                status=task.status,
            )
            return

        is_local = self._executor.is_running_locally(task_id)

        # Snapshot first so the client has the latest task state.
        yield task

        if is_local:
            # Same replica: progress + artifacts + terminal events all
            # flow through the SDK's in-process EventQueue. Replay the
            # persisted log so the UI's thinking indicator catches up,
            # then tap into ``super()`` for live events. ``super()``
            # yields the initial Task again first, which we drop to
            # avoid the client receiving two Task events back-to-back.
            try:
                entries = await self._progress_store.read_progress(task_id)
            except Exception:
                log.warning(
                    "Failed to read progress log for task=%s", task_id, exc_info=True,
                )
                entries = []
            for entry in entries:
                yield self._progress_to_event(task, entry.message)

            seen_initial = False
            async for event in super().on_subscribe_to_task(params, context):
                if not seen_initial and isinstance(event, Task):
                    seen_initial = True
                    continue
                yield event
            return

        # Different replica (or DBOS-recovered workflow) — the in-process
        # EventQueue is empty here, but ``report_progress`` calls on the
        # executing instance land in the task store. Live-tail the store
        # for progress events while polling task state in parallel so we
        # can yield the final Task snapshot when the worker terminates.
        async for event in self._tail_remote_progress(task, context):
            yield event

    async def _tail_remote_progress(self, task: Task, context):
        """Yield progress events (and terminal snapshot) for a non-local task.

        Two concurrent producers feed this loop:

        * ``subscribe_progress`` — push-driven (LISTEN/NOTIFY on Postgres,
          pub/sub on Redis, in-process queue on Memory, poll on Mongo).
          Yielded as ``TaskStatusUpdateEvent`` so the UI's thinking
          indicator updates in real time.
        * ``_poll_state`` — periodically re-reads the task; on terminal
          we yield the fresh Task snapshot and return so the UI flips
          out of WORKING.
        """
        task_id = task.id
        progress_iter = self._progress_store.subscribe_progress(task_id).__aiter__()

        async def _poll_state() -> Task | None:
            while True:
                await asyncio.sleep(_REMOTE_STATE_POLL_INTERVAL)
                try:
                    current = await self._progress_store.get(task_id, context)
                except Exception:
                    log.debug(
                        "Remote-tail state poll failed for task=%s",
                        task_id, exc_info=True,
                    )
                    continue
                if current is None:
                    return None
                if current.status.state in _TERMINAL_TASK_STATES:
                    return current

        state_task = asyncio.create_task(_poll_state())
        next_entry_task = asyncio.create_task(progress_iter.__anext__())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {state_task, next_entry_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if state_task in done:
                    final = state_task.result()
                    if final is not None:
                        yield final
                    return
                try:
                    entry = next_entry_task.result()
                except StopAsyncIteration:
                    # The progress iterator finished. Shouldn't happen
                    # for our backends (they run until aclose) but if
                    # it does the consumer falls back to GetTask polling.
                    return
                yield self._progress_to_event(task, entry.message)
                next_entry_task = asyncio.create_task(progress_iter.__anext__())
        finally:
            state_task.cancel()
            if not next_entry_task.done():
                next_entry_task.cancel()
            with contextlib.suppress(Exception):
                await progress_iter.aclose()


# ── Prompt building helpers ───────────────────────────────────────────────────


def _make_default_prompt_builder(
    system_prompt: str | None,
    history_max_lines: int,
) -> Callable[[RequestContext], str]:
    """Return the default prompt builder, parametrised by Level-1 options."""
    def _build(context: RequestContext) -> str:
        parts: list[str] = []
        if system_prompt:
            parts.append(system_prompt.rstrip() + "\n\n")
        prefix = format_history(
            get_task_history(context), max_lines=history_max_lines
        )
        if prefix:
            parts.append(prefix)
        parts.append(get_user_input(context))
        return "".join(parts)
    return _build


# ── Public factory functions ──────────────────────────────────────────────────


def _accepts_context(run: Callable) -> bool:
    """True if *run* declares a second positional parameter (the RequestContext)."""
    try:
        params = inspect.signature(run).parameters
    except (TypeError, ValueError):
        return False
    positional = [
        p for p in params.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                      inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return len(positional) >= 2


def build_invoke(
    run: Callable[[str], Awaitable[str | Artifact]]
    | Callable[[str, RequestContext], Awaitable[str | Artifact]],
) -> Callable[[str, RequestContext], Awaitable[str | Artifact]]:
    """Wrap any ``async (prompt) -> str | Artifact`` function as a non-streaming A2A invoke.

    Accepts plain-text, multi-part, and context-aware agents::

        # plain text
        async def my_agent(prompt: str) -> str:
            return "hello"

        # multi-part (text + data + file)
        async def my_agent(prompt: str) -> Artifact: ...

        # context-aware (e.g. for reading uploaded image parts)
        async def my_agent(prompt: str, context: RequestContext) -> Artifact:
            for part in context.message.parts:
                ...

        invoke=build_invoke(my_agent)
    """
    pass_context = _accepts_context(run)

    async def _invoke(prompt: str, context: RequestContext) -> str | Artifact:
        return await (run(prompt, context) if pass_context else run(prompt))

    return _invoke


def build_stream_invoke(
    run: Callable[[str], AsyncIterable[str | Artifact]]
    | Callable[[str, RequestContext], AsyncIterable[str | Artifact]],
) -> Callable[[str, RequestContext], AsyncIterable[str | Artifact]]:
    """Wrap any ``async (prompt) -> AsyncIterable[str | Artifact]`` generator as a streaming A2A invoke.

    Works with any AI framework or plain streaming API call::

        # plain
        async def my_agent(prompt: str) -> AsyncIterable[str]:
            async with client.messages.stream(...) as stream:
                async for chunk in stream.text_stream:
                    yield chunk

        # context-aware (e.g. for reading uploaded image parts)
        async def my_agent(prompt: str, context: RequestContext) -> AsyncIterable[str | Artifact]:
            for part in context.message.parts:
                ...
            yield artifact

        stream_invoke=build_stream_invoke(my_agent)

    The wrapper only normalises the ``(prompt, context)`` signature. Live
    progress updates flow independently: tools call
    ``report_progress(msg)``, the framework appends to the task store, and
    the executor's subscriber re-emits them as SSE status events.
    """
    pass_context = _accepts_context(run)

    async def _stream(prompt: str, context: RequestContext) -> AsyncIterable[str | Artifact]:
        gen = run(prompt, context) if pass_context else run(prompt)
        async for chunk in gen:
            if chunk:
                yield chunk

    return _stream


async def clean_up_stale_tasks(
    task_store: A2ATaskStore,
    *,
    on_task_recover: Callable[[str], Awaitable[None]] | None = None,
    threshold_secs: float = _STALE_THRESHOLD,
) -> list[str]:
    """Finalize tasks left WORKING by a previous process.

    Call this from your app's lifespan startup (after the task store is
    open) so a crashed-then-restarted process unsticks any tasks the
    UI is still polling on:

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            store = await PostgresTaskStore.from_dsn(POSTGRES_URL)
            await clean_up_stale_tasks(store, on_task_recover=my_recover_hook)
            ...

    For each stale task ``on_task_recover(task_id)`` is invoked first
    (best-effort, advisory) so the agent can do its own cleanup —
    cancel a stuck durable workflow, emit telemetry, write a tailored
    status message via ``finalize_task``. The framework then calls
    ``task_store.finalize_task(... TASK_STATE_FAILED)``, which is
    idempotent at the store layer: a hook that already wrote a
    terminal state leaves it intact.

    Returns the list of task_ids the function touched.
    """
    try:
        stale = await task_store.list_stale_working_tasks(threshold_secs)
    except Exception:
        log.exception("clean_up_stale_tasks: list_stale_working_tasks failed")
        return []

    if not stale:
        return []

    log.info(
        "clean_up_stale_tasks: %d stuck task(s) detected — recovering",
        len(stale),
    )
    for task_id in stale:
        if on_task_recover is not None:
            try:
                await on_task_recover(task_id)
            except Exception:
                log.exception(
                    "clean_up_stale_tasks: on_task_recover failed task=%s — falling through to default finalize",
                    task_id,
                )
        try:
            await task_store.finalize_task(
                task_id,
                state=TaskState.TASK_STATE_FAILED,
                status_message="Task interrupted during a restart. Please re-ask.",
            )
        except Exception:
            log.exception(
                "clean_up_stale_tasks: finalize_task failed task=%s", task_id,
            )
    return stale


def build_a2a_app(
    *,
    agent_card: AgentCard,
    invoke: Callable[[str, RequestContext], Awaitable[str | Artifact]] | None = None,
    stream_invoke: Callable[[str, RequestContext], AsyncIterable[str | Artifact]]
    | None = None,
    # ── Level 1: tune the built-in prompt without writing any code ────────────
    system_prompt: str | None = None,
    history_max_lines: int = 12,
    # ── Level 3: replace the entire prompt builder ────────────────────────────
    prompt_builder: Callable[[RequestContext], str] | None = None,
    on_task_start: Callable[[str], Awaitable[None]] | None = None,
    on_task_cancel: Callable[[str, str], Awaitable[None]] | None = None,
    on_task_recover: Callable[[str], Awaitable[None]] | None = None,
    task_store: A2ATaskStore | None = None,
    debug: bool = False,
):
    """Assemble a Starlette ASGI app handling all A2A JSON-RPC methods.

    Mount at a path prefix in FastAPI::

        app.mount("/a2a", build_a2a_app(agent_card=card, stream_invoke=...))

    Prompt management follows **Progressive Disclosure** — start at the level
    you need and ignore the rest.

    Level 0 — zero config
    ~~~~~~~~~~~~~~~~~~~~~
    Works out of the box. The last 12 lines of conversation history are
    automatically injected before the user's message. Nothing to configure::

        build_a2a_app(agent_card=card, stream_invoke=build_stream_invoke(my_fn))

    Level 1 — keyword parameters
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    Tune the built-in prompt without writing any code:

    ``system_prompt``
        Prepended to every prompt. Use it for persona, constraints, or
        output-format instructions::

            build_a2a_app(
                agent_card=card,
                stream_invoke=build_stream_invoke(my_fn),
                system_prompt="You are a concise travel planner. Reply in JSON.",
            )

    ``history_max_lines``
        Number of prior conversation lines to inject (default ``12``).
        Set to ``0`` to disable history entirely::

            build_a2a_app(..., history_max_lines=0)   # stateless agent
            build_a2a_app(..., history_max_lines=30)  # long-memory agent

    Level 2 — compose from helpers
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    Build your own prompt from the exported building blocks.  Pass the result
    as ``prompt_builder``::

        from fast_a2a_app import format_history, get_task_history, get_user_input

        def my_prompt(context: RequestContext) -> str:
            return (
                "You are an expert planner.\\n\\n"
                + format_history(get_task_history(context), max_lines=6)
                + f"Respond in JSON:\\n{get_user_input(context)}"
            )

        build_a2a_app(..., prompt_builder=my_prompt)

    Level 3 — full custom builder
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    Pass any ``(RequestContext) -> str`` as ``prompt_builder`` for complete
    control.  ``system_prompt`` and ``history_max_lines`` are ignored when a
    custom ``prompt_builder`` is supplied.

    The ``RequestContext`` exposes:

    * ``context.get_user_input()`` — the current user message text
    * ``context.related_tasks``    — prior ``Task`` objects for this conversation
    * ``context.current_task``     — the task being executed right now
    * ``context.message``          — the raw A2A ``Message`` object

    Supply ``task_store`` to use a Redis / Mongo / Postgres backend
    (or any custom :class:`A2ATaskStore` implementation). Omit it and the
    in-process :class:`MemoryTaskStore` is used — zero infrastructure,
    suitable for development and single-process deployments only.

    ``on_task_recover(task_id)`` is an async, advisory hook called
    whenever the framework detects a stuck WORKING task whose worker
    has died (lazy on ``GetTask`` / ``SubscribeToTask``, eager from
    :func:`clean_up_stale_tasks` at startup). Use it for agent-side
    cleanup — cancelling a stuck durable workflow, emitting telemetry,
    optionally writing a tailored status message via
    ``finalize_task``. After the hook returns the framework
    default-finalizes the task as ``TASK_STATE_FAILED`` (no-op if the
    hook already wrote a terminal state), so the UI always unsticks.

    Set ``debug=True`` to include exception details in agent failure messages.
    """
    resolved_prompt_builder = prompt_builder or _make_default_prompt_builder(
        system_prompt=system_prompt,
        history_max_lines=history_max_lines,
    )
    resolved_store: A2ATaskStore = task_store or MemoryTaskStore()
    executor = ConfigurableAgentExecutor(
        invoke=invoke,
        prompt_builder=resolved_prompt_builder,
        stream_invoke=stream_invoke,
        on_task_start=on_task_start,
        on_task_cancel=on_task_cancel,
        task_store=resolved_store,
        debug=debug,
    )
    request_handler = ProgressAwareRequestHandler(
        agent_executor=executor,
        task_store=resolved_store,
        progress_store=resolved_store,
        on_task_recover=on_task_recover,
        agent_card=agent_card,
        request_context_builder=ContextAwareRequestContextBuilder(resolved_store),
    )
    return Router(routes=(
        create_agent_card_routes(agent_card) +
        create_jsonrpc_routes(request_handler, rpc_url="/")
    ))
