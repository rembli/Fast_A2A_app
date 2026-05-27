"""fast_a2a_app.server — A2A protocol adapter."""
from .artifacts import (
    ArtifactType,
    ArtifactTypeRegistry,
    artifact_types,
    data_artifact,
    file_artifact,
    image_artifact,
    map_artifact,
    prompt_suggestions_artifact,
    table_artifact,
    text_artifact,
)
from .route import (
    bind_executor,
    build_a2a_app,
    build_invoke,
    build_stream_invoke,
    clean_up_stale_tasks,
    format_history,
    get_task_history,
    get_user_input,
    ConfigurableAgentExecutor,
    ContextAwareRequestContextBuilder,
)
from .task_stores import (
    A2ATaskStore,
    MemoryTaskStore,
    MongoTaskStore,
    PostgresTaskStore,
    RedisTaskStore,
)
from .utils import report_progress

__all__ = [
    # App factory
    "build_a2a_app",
    # Startup recovery helper
    "clean_up_stale_tasks",
    # Recovery-side ContextVar binding (re-binds report_progress)
    "bind_executor",
    # Invoke wrappers
    "build_invoke",
    "build_stream_invoke",
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
]
