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
                              Also sets up the report_progress() ContextVar.

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
build_stream_invoke wraps your generator in an asyncio.Queue relay and sets
the _progress_cb ContextVar before starting the generator task. Any code
called during streaming can therefore call report_progress() to push a
non-final working-status SSE event to the chat UI.

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
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.types import (
    AgentCard,
    Artifact,
    Part,
    Role,
    SendMessageRequest,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from starlette.routing import Router

from .task_stores import A2ATaskStore, MemoryTaskStore
from .utils import _progress_cb

log = logging.getLogger(__name__)

_TRANSIENT_AGENT_TEXTS = frozenset({"processing request..."})

# Seconds between cancel-signal polls. Lower = faster cancellation, more Redis traffic.
_CANCEL_POLL_INTERVAL = 2

# Sentinel distinguishing in-band progress strings from the final response text.
# Null bytes make accidental collisions with real LLM output virtually impossible.
_PROGRESS_PREFIX = "\x00p\x00"



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

            poll_task = asyncio.create_task(_poll_cancel_signal())

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
            self._running_tasks.pop(task_id, None)
            self._task_contexts.pop(task_id, None)

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

            if chunk.startswith(_PROGRESS_PREFIX):
                progress_text = chunk[len(_PROGRESS_PREFIX):]
                await event_queue.enqueue_event(
                    TaskStatusUpdateEvent(
                        task_id=task_id,
                        context_id=context_id,
                        status=TaskStatus(
                            state=TaskState.TASK_STATE_WORKING,
                            message=new_text_message(
                                progress_text,
                                context_id=context_id,
                                task_id=task_id,
                            ),
                        ),
                    )
                )
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

    The wrapper sets up the ``report_progress()`` ContextVar so any code called
    during streaming can push live status updates to the chat UI — regardless of
    which AI framework (or none) your agent uses.
    """
    pass_context = _accepts_context(run)

    async def _stream(prompt: str, context: RequestContext) -> AsyncIterable[str | Artifact]:
        _DONE = object()
        queue: asyncio.Queue = asyncio.Queue()
        error_holder: list[BaseException | None] = [None]

        def _on_progress(msg: str) -> None:
            queue.put_nowait(_PROGRESS_PREFIX + msg)

        token = _progress_cb.set(_on_progress)

        async def _run() -> None:
            try:
                gen = run(prompt, context) if pass_context else run(prompt)
                async for chunk in gen:
                    if chunk:
                        queue.put_nowait(chunk)
            except asyncio.CancelledError as exc:
                error_holder[0] = exc
            except Exception as exc:
                error_holder[0] = exc
            finally:
                queue.put_nowait(_DONE)

        run_task = asyncio.create_task(_run())
        try:
            while True:
                item = await queue.get()
                if item is _DONE:
                    break
                yield item
        finally:
            _progress_cb.reset(token)
            if not run_task.done():
                run_task.cancel()

        exc = error_holder[0]
        if exc is not None:
            raise exc

    return _stream


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
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=resolved_store,
        agent_card=agent_card,
        request_context_builder=ContextAwareRequestContextBuilder(resolved_store),
    )
    return Router(routes=(
        create_agent_card_routes(agent_card) +
        create_jsonrpc_routes(request_handler, rpc_url="/")
    ))
