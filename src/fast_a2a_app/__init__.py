"""
fast_a2a_app — Drop-in A2A server and chat UI for any FastAPI application.

fast_a2a_app is framework-agnostic: it works with pydantic-ai, LangChain,
LlamaIndex, plain Anthropic/OpenAI API calls, or any custom logic that
can expose an ``async (str) -> str`` function or an async generator.

Typical usage::

    from fastapi import FastAPI
    from a2a.types import AgentCapabilities, AgentCard, AgentInterface
    from fast_a2a_app import a2a_ui, build_a2a_app, build_invoke, build_stream_invoke

    app = FastAPI()

    agent_card = AgentCard(
        name="My Agent",
        description="Does cool things",
        version="1.0.0",
        supported_interfaces=[AgentInterface(
            url="http://localhost:8000/a2a/",
            protocol_binding="JSONRPC",
        )],
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text"],
        default_output_modes=["text"],
    )

    app.mount("/a2a", build_a2a_app(
        agent_card=agent_card,
        invoke=build_invoke(my_agent_fn),
        stream_invoke=build_stream_invoke(my_streaming_agent_fn),
    ))

    app.mount("/", a2a_ui)

**Prompt management** follows Progressive Disclosure — use only the level you need:

* **Level 0** — zero config: history injected automatically, nothing to set.
* **Level 1** — keyword params on ``build_a2a_app``: ``system_prompt`` and
  ``history_max_lines`` tune the built-in behaviour without any custom code.
* **Level 2** — compose with helpers: ``get_task_history``,
  ``format_history`` and ``get_user_input`` let you assemble a custom
  prompt from named pieces.
* **Level 3** — full control: pass any ``(RequestContext) -> str`` as
  ``prompt_builder`` to replace everything.

See ``build_a2a_app`` for detailed examples of each level.

Call ``report_progress("step 2/5…")`` from anywhere inside your agent
(tools, helpers, child tasks spawned with ``asyncio.create_task``) to push
live status updates to the chat UI. No task_id argument is required — the
framework resolves the active task from a request-scoped ContextVar set by
``ConfigurableAgentExecutor`` on each request. Each call appends to the
configured task store; the executor's per-task subscriber re-emits entries
as SSE working-status events, so live delivery and resubscribe replay
share one source of truth.
"""
from . import _sdk_compat as _sdk_compat
_sdk_compat.apply()

from ._version import __version__

from .server import (
    A2ATaskStore,
    ArtifactType,
    ArtifactTypeRegistry,
    ConfigurableAgentExecutor,
    ContextAwareRequestContextBuilder,
    MemoryTaskStore,
    MongoTaskStore,
    PostgresTaskStore,
    RedisTaskStore,
    artifact_types,
    bind_executor,
    build_a2a_app,
    build_invoke,
    build_stream_invoke,
    clean_up_stale_tasks,
    data_artifact,
    file_artifact,
    format_history,
    get_task_history,
    get_user_input,
    image_artifact,
    map_artifact,
    prompt_suggestions_artifact,
    report_progress,
    table_artifact,
    text_artifact,
)
from .ui import a2a_ui, build_a2a_ui

__all__ = [
    # Server
    "build_a2a_app",
    "build_invoke",
    "build_stream_invoke",
    "clean_up_stale_tasks",
    "bind_executor",
    # Prompt helpers (Level 2 building blocks)
    "get_task_history",
    "format_history",
    "get_user_input",
    # Artifact builders
    "text_artifact",
    "data_artifact",
    "table_artifact",
    "map_artifact",
    "file_artifact",
    "image_artifact",
    "prompt_suggestions_artifact",
    # Artifact-type registry (extending the chat UI with new typed widgets)
    "ArtifactType",
    "ArtifactTypeRegistry",
    "artifact_types",
    # Low-level
    "ConfigurableAgentExecutor",
    "ContextAwareRequestContextBuilder",
    "A2ATaskStore",
    "MemoryTaskStore",
    "MongoTaskStore",
    "PostgresTaskStore",
    "RedisTaskStore",
    "report_progress",
    # UI
    "a2a_ui",
    "build_a2a_ui",
    # Meta
    "__version__",
]
