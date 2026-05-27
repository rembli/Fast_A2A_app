# API reference

Every public symbol exported from `fast_a2a_app`. For task-oriented examples (prompt tuning, uploads, multi-part responses, typed widgets), see the [How-to guides](how-to.md).

```python
from fast_a2a_app import (
    # Server
    build_a2a_app, build_invoke, build_stream_invoke,
    # UI
    a2a_ui, build_a2a_ui,
    # Embedded artifact primitives
    text_artifact, data_artifact, file_artifact, image_artifact,
    # Specialised artifacts (typed `_type` envelopes)
    table_artifact, prompt_suggestions_artifact, map_artifact,
    # Typed-artifact registry
    ArtifactType, ArtifactTypeRegistry, artifact_types,
    # Prompt helpers (Level 2 building blocks)
    get_user_input, get_task_history, format_history,
    # Progress
    report_progress,
    # Storage / executor (lower-level)
    A2ATaskStore,
    MemoryTaskStore, RedisTaskStore, MongoTaskStore, PostgresTaskStore,
    ConfigurableAgentExecutor, ContextAwareRequestContextBuilder,
)
```

---

## `build_a2a_app(...)`

Assembles a Starlette ASGI app implementing the A2A protocol. Mount it at any path prefix.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `agent_card` | `AgentCard` | required | Pre-built A2A agent card (name, description, version, `supported_interfaces`, skills, capabilities) |
| `invoke` | `Callable \| None` | `None` | Non-streaming callable — wrap with `build_invoke()` |
| `stream_invoke` | `Callable \| None` | `None` | Streaming callable — wrap with `build_stream_invoke()` |
| `system_prompt` | `str \| None` | `None` | Prepended to every prompt before history and user input (Level 1) |
| `history_max_lines` | `int` | `12` | Number of prior conversation lines to inject; `0` disables history (Level 1) |
| `prompt_builder` | `Callable \| None` | auto | Custom `(RequestContext) -> str`; overrides `system_prompt` and `history_max_lines` (Level 2/3) |
| `on_task_start` | `Callable[[str], Awaitable] \| None` | `None` | Called before each task — useful for metrics or per-task locks |
| `on_task_cancel` | `Callable[[str, str], Awaitable] \| None` | `None` | Called on cancel with `(context_id, task_id)` |
| `task_store` | `A2ATaskStore \| None` | `MemoryTaskStore()` | Task store implementing the `A2ATaskStore` Protocol. Omit for the in-process `MemoryTaskStore` (single-process only); pass `RedisTaskStore.from_url(...)` / `MongoTaskStore.from_uri(...)` / `PostgresTaskStore.from_dsn(...)` for multi-process deployments. |
| `debug` | `bool` | `False` | Include exception details in failure messages and surface them in the UI |

---

## `build_invoke(run)`

Wraps any `async (prompt: str) -> str | Artifact` function — or `async (prompt: str, context: RequestContext) -> str | Artifact` — as a non-streaming A2A invoke.

The framework inspects your function's signature with `inspect.signature` and forwards the `RequestContext` only when you declare a second positional parameter. See [How-to → Reading raw input with `RequestContext`](how-to.md#reading-raw-input-with-requestcontext).

```python
from a2a.server.agent_execution import RequestContext
from a2a.types import Artifact
from fast_a2a_app import build_a2a_app, build_invoke, text_artifact

# (1) bare text
async def echo(prompt: str) -> str:
    return f"echo: {prompt}"

# (2) text + context
async def echo_with_ctx(prompt: str, context: RequestContext) -> str:
    return f"{context.context_id}: {prompt}"

# (3) full multi-part artifact
async def multipart(prompt: str) -> Artifact:
    return text_artifact(f"Result for: {prompt}")

app.mount("/a2a", build_a2a_app(agent_card=card, invoke=build_invoke(echo)))
```

---

## `build_stream_invoke(run)`

Wraps any of these shapes as a streaming A2A invoke:

```python
async def fn(prompt: str) -> AsyncIterable[str | Artifact]: ...
async def fn(prompt: str, context: RequestContext) -> AsyncIterable[str | Artifact]: ...
```

Yields can mix plain strings (streamed as text deltas into one bubble) and full `Artifact` objects (each rendered as its own bubble). Also sets up the `report_progress()` ContextVar so live progress updates work out of the box — call `report_progress("step 2/5…")` anywhere during execution and it appears as a working-status event in the chat UI.

```python
async def stream_invoke(prompt: str, context: RequestContext):
    yield text_artifact("Working…")
    yield table_artifact(rows=[["a", 1], ["b", 2]], columns=["k", "v"])
    yield prompt_suggestions_artifact(
        [{"label": "Refine", "prompt": "Refine the answer."}],
        text="Anything else?",
    )
```

---

## `report_progress(message)`

Pushes a status string to the chat UI spinner. Has no effect outside a streaming context (safe to call unconditionally).

```python
@agent.tool
async def long_computation(ctx, n: int) -> str:
    report_progress(f"Computing step 1/{n}…")
    ...
    report_progress(f"Computing step 2/{n}…")
    return result
```

---

## `RequestContext` helpers

### `get_user_input(context)`

Returns the current user message text from a `RequestContext`. Use this in a custom `prompt_builder` so you don't need to know the internal SDK method name:

```python
from fast_a2a_app import get_user_input

def my_prompt(context) -> str:
    return f"Respond in JSON:\n{get_user_input(context)}"
```

### `get_task_history(context)`

Returns prior conversation as a list of `(role, text)` tuples ordered oldest → newest. `role` is the literal `"user"` or `"agent"`. Pulls from every `Task` in `context.related_tasks` (history messages + agent text artifacts). Returns `[]` when there is no prior history.

```python
for role, text in get_task_history(context):
    if role == "user":
        ...
```

> Agent text is sourced from BOTH `task.history` *and* `task.artifacts`. That includes transient status messages emitted via `report_progress`. When you want artifact-only agent text (e.g. for typed-message-history frameworks like pydantic-ai), walk `context.related_tasks` directly — see [`build_message_history` patterns in the examples](../examples/holiday-planner/agent.py).

### `format_history(history, *, max_lines=12, header="Conversation so far:")`

Renders `(role, text)` pairs as a prompt prefix — caps to the most recent `max_lines`, formats each as `"User: …"` / `"Agent: …"`, and prepends `header`. Returns `""` when the list is empty or `max_lines <= 0`.

```python
from fast_a2a_app import format_history, get_task_history, get_user_input

def my_prompt(context) -> str:
    return (
        "You are an expert.\n\n"
        + format_history(get_task_history(context), max_lines=6)
        + get_user_input(context)
    )
```

---

## Artifact builders

The package splits builders into two tiers: **embedded primitives** that wrap A2A protocol Parts directly, and **specialised artifacts** that carry a typed `_type` discriminator and route to a dedicated UI renderer. Anything without a recognised `_type` falls through to a generic key-value block.

### Embedded primitives

| Helper | Returns | UI rendering |
|---|---|---|
| `text_artifact(text, *, name="result")` | text-only Artifact | markdown bubble |
| `data_artifact(data, *, name="data", text=None)` | structured-data Artifact (protobuf `Value`); accepts any JSON-compatible dict. **NaN / ±Inf scrubbed to `None`** before reaching the proto so pandas data with missing cells round-trips through the task store's `MessageToJson` cleanly. | When `data._type` matches a registered typed renderer → that widget; otherwise generic key-value block |
| `file_artifact(content=None, *, url=None, filename, media_type, name=None, text=None)` | file Artifact — pass exactly one of inline `content` bytes or a `url` reference | download card; `image/*` media types render inline |
| `image_artifact(image_bytes=None, *, url=None, media_type="image/png", caption=None, filename=None, name="image")` | image Artifact — inline bytes or a stored URL | inline image preview + click-to-fullscreen (with prompt-suggestion pills surfaced from the same turn) |

### Specialised artifacts (typed `_type` envelopes)

Each ships with a matching JS renderer in `fast_a2a_app/ui/renderers/<TAG>.js`. Registered automatically at package import via autodiscover.

| Helper | `_type` | UI rendering |
|---|---|---|
| `table_artifact(rows, *, columns=None, caption=None, name="table")` | `"TABLE"` | Real HTML `<table>` — column headers, alternating row shading, right-aligned monospace numerics, em-dash for nulls, horizontal-scroll for wide schemas |
| `prompt_suggestions_artifact(suggestions, *, text=None, name="prompt_suggestions")` | `"PROMPT_SUGGESTIONS"` | Row of clickable pill buttons; click submits the suggestion's `prompt` as the next user message. Clicked pill is highlighted (visual breadcrumb) and siblings dim. |
| `map_artifact(markers, *, center=None, zoom=None, caption=None, name="map")` | `"MAP"` | Interactive Leaflet/OpenStreetMap map (Leaflet lazy-loaded from CDN on first map). `markers` is `[{lat, lng, label?, popup?}, …]`; invalid coords dropped silently. |

```python
from fast_a2a_app import (
    text_artifact, data_artifact, file_artifact, image_artifact,
    table_artifact, prompt_suggestions_artifact, map_artifact,
)

async def stream_invoke(prompt, context):
    yield text_artifact("Computing…")
    yield table_artifact(
        rows=[["APAC", 38400], ["EMEA", 22000]],
        columns=["region", "revenue"],
        caption="Top regions",
    )
    yield image_artifact(url="/charts/abc.png", caption="Year-over-year")
    yield map_artifact(
        [{"lat": 41.9028, "lng": 12.4964, "label": "Rome"}],
        caption="Suggested destination",
    )
    yield prompt_suggestions_artifact(
        [{"label": "Drill into APAC", "prompt": "Break down APAC by country."}],
        text="What next?",
    )
```

### URL form vs. inline bytes

`image_artifact` and `file_artifact` both accept either inline `content` / `image_bytes` *or* a `url`. The URL form keeps large binaries out of the wire transcript and the browser's `localStorage` — store the bytes in your own backend (object store, sibling FastAPI endpoint, CDN) and ship just the URL.

### Typed-artifact registry

`artifact_types` is a process-wide `ArtifactTypeRegistry` populated at import time by walking `fast_a2a_app/server/artifacts/` and registering every uppercase `<TAG>.py` module.

```python
from fast_a2a_app import artifact_types, ArtifactType, ArtifactTypeRegistry

# Built-ins after import:
[t.tag for t in artifact_types.all()]
# → ['MAP', 'PROMPT_SUGGESTIONS', 'TABLE']

# Register your own at runtime:
artifact_types.register("MYAPP_TIMELINE", builder=timeline_artifact)
```

| Method | Purpose |
|---|---|
| `register(tag, *, builder=None)` | Adds (or overrides) a `(tag, builder)` pair |
| `unregister(tag)` | Removes a tag from the registry |
| `get(tag)` | Returns the `ArtifactType` record or `None` |
| `builder(tag)` | Convenience accessor for the Python builder |
| `all()` | All registered types in registration order |

Adding a new typed widget = drop `<TAG>.py` in `server/artifacts/` (and optionally `<TAG>.js` in `ui/renderers/` for a bespoke UI; missing JS falls through to the generic key-value renderer). See [How-to → Adding a new typed widget](how-to.md#adding-a-new-typed-widget).

---

## UI

### `a2a_ui`

Pre-built Starlette ASGI app serving the self-contained single-page chat interface. No build step, no npm. Mount it at `"/"` to serve the UI with default settings (no file upload).

```python
app.mount("/", a2a_ui)
```

### `build_a2a_ui(...)`

Build a fresh UI app with configuration applied at template-substitution time.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `file_upload_api` | `str \| None` | `None` | URL the paperclip should `POST` files to as `multipart/form-data`. Endpoint must return `{id, url, mediaType, filename}`. When `None`, the attach button is hidden. |
| `accepted_file_types` | `list[str] \| str \| None` | `None` (images only) | What the file picker accepts. Same format as the HTML `<input accept>` attribute — file extensions (`".csv"`), MIME types (`"text/csv"`), or wildcards (`"image/*"`). Pass a list (joined with commas) or a pre-formatted string. |

```python
app.mount("/", build_a2a_ui(
    file_upload_api="/uploads",
    accepted_file_types=[".csv", ".xlsx", "text/csv"],
))
```

The UI reads the agent card from `/a2a/.well-known/agent-card.json` to populate the header name and the collapsible info panel.

#### Served routes

`build_a2a_ui` returns a Starlette app that serves four assets:

| Route | Content |
|---|---|
| `/` | `index.html` with `/* UI_CONFIG */` and `/* DATA_TYPE_RENDERERS */` placeholders substituted |
| `/styles.css` | The chat UI's stylesheet (Tailwind overrides + dark-mode rules + table-zebra + pill-selected) |
| `/app.js` | The chat UI's JavaScript bundle |
| `/a2a-ui.png` | Logo asset |

Renderer files from `fast_a2a_app/ui/renderers/*.js` are concatenated into the served HTML at build time; they're not exposed as separate URLs.

---

## Storage

### `A2ATaskStore` (Protocol)

Pluggable storage for tasks, context indices, and cancel signals.

```python
class A2ATaskStore(Protocol):
    async def save(self, task, call_context): ...
    async def get(self, task_id): ...
    async def list_by_context(self, context_id, exclude_task_id=None): ...
    async def signal_cancel(self, task_id): ...
    async def is_cancel_signalled(self, task_id): ...
```

Implement this against any datastore and pass it to `build_a2a_app(task_store=...)`. Four built-in implementations ship in `fast_a2a_app.server.task_stores`:

| Store | When to use | Constructor |
|---|---|---|
| `MemoryTaskStore` | Dev, tests, single-process demos. The default when `task_store` is omitted. State lives in RAM — no persistence, no cross-instance cancel. | `MemoryTaskStore()` |
| `RedisTaskStore` | Production. Native TTL, horizontal scale, cross-instance cancel via short-TTL keys. | `RedisTaskStore(client)` or `RedisTaskStore.from_url("redis://…")` |
| `MongoTaskStore` | Production where Mongo is the operational data store. TTL indexes drop expired docs server-side. | `MongoTaskStore(client, database_name="fast_a2a")` or `await MongoTaskStore.from_uri("mongodb://…")` |
| `PostgresTaskStore` | Production where Postgres is the operational data store. `expires_at` columns + read-time filtering. | `PostgresTaskStore(pool)` or `await PostgresTaskStore.from_dsn("postgresql://…")` |

Every store logs an `INFO` line on initialization so the console makes it obvious which backend is live; `MemoryTaskStore` additionally warns about its single-process limitation.

### `MemoryTaskStore`

In-process dict store, no external service required.

```python
from fast_a2a_app import MemoryTaskStore, build_a2a_app

# Explicit (same as the default)
build_a2a_app(..., task_store=MemoryTaskStore())

# Or simply omit task_store and the default is applied for you.
```

### `RedisTaskStore`

Redis-backed store. Key layout:

| Key | TTL |
|---|---|
| `a2a:task:{id}` (full task JSON) | 24 h |
| `a2a:context:{cid}:tasks` (HASH `task_id → sequence`) | 24 h |
| `a2a:cancel:{id}` (cancel signal) | 5 min |

```python
from fast_a2a_app import RedisTaskStore, build_a2a_app

build_a2a_app(
    ...,
    task_store=RedisTaskStore.from_url("redis://localhost:6379"),
)
```

### `MongoTaskStore`

Async MongoDB-backed store using `motor` and TTL indexes (`tasks.expires_at` = 24 h, `cancel_signals.expires_at` = 5 min). `from_uri` creates the indexes for you; if you build the store from a pre-existing `AsyncIOMotorClient`, call `await store.ensure_indexes()` once before serving traffic.

```python
from fast_a2a_app import MongoTaskStore, build_a2a_app

store = await MongoTaskStore.from_uri("mongodb://localhost:27017")
build_a2a_app(..., task_store=store)
```

Collections live in the `fast_a2a` database by default — override via the `database_name=` argument.

### `PostgresTaskStore`

Async Postgres-backed store using `asyncpg`. `from_dsn` creates the schema (three tables + indexes) on first call; if you build it from a pre-existing pool, call `await store.ensure_schema()` once before serving traffic. Expired rows are filtered at read time via `expires_at > NOW()` — schedule an external sweeper if disk reclamation matters.

```python
from fast_a2a_app import PostgresTaskStore, build_a2a_app

store = await PostgresTaskStore.from_dsn(
    "postgresql://user:pw@localhost/fast_a2a",
)
build_a2a_app(..., task_store=store)
```

---

## Low-level

### `ConfigurableAgentExecutor`

The internal executor that runs `invoke` / `stream_invoke` against the A2A SDK's event loop. Honours `on_task_start` / `on_task_cancel` hooks and surfaces `report_progress` calls as `TASK_STATE_WORKING` status events. Rarely instantiated directly — `build_a2a_app` wires it up for you.

### `ContextAwareRequestContextBuilder`

Builds a `RequestContext` whose `related_tasks` is populated from the task store. The SDK calls this internally; supply a custom one to `build_a2a_app(request_context_builder=...)` if you need to override how prior turns are loaded.

### `ArtifactType` / `ArtifactTypeRegistry`

Dataclass + registry described above under [Typed-artifact registry](#typed-artifact-registry).

```python
@dataclass(frozen=True)
class ArtifactType:
    tag: str                                            # `_type` discriminator value
    builder: Callable[..., Artifact] | None = None      # optional Python helper
```

### `clean_up_stale_tasks`

```python
async def clean_up_stale_tasks(
    task_store: A2ATaskStore,
    *,
    on_task_recover: Callable[[str, A2ATaskStore], Awaitable[None]] | None = None,
    threshold_secs: float = 30,
) -> list[str]: ...
```

Sweeps the task store for `TASK_STATE_WORKING` tasks whose heartbeat is older than `threshold_secs` (default 30 s) and routes each through `on_task_recover` (if supplied) before default-finalizing them as `TASK_STATE_FAILED`. Call from app lifespan startup — after the task store opens and after `DBOS.launch()` — so a crashed-then-restarted process reconciles stuck tasks without waiting for a client to poll. Returns the list of `task_id`s touched. The same lazy heartbeat check fires on every `GetTask` / `SubscribeToTask`; `clean_up_stale_tasks` covers the no-traffic case. See [Crash recovery](how-to.md#crash-recovery).

### `bind_executor`

```python
@contextlib.asynccontextmanager
async def bind_executor(task_id: str, task_store: A2ATaskStore): ...
```

Async context manager that re-binds the request-scoped `ContextVar`s `report_progress(...)` reads (`_current_executor` + `_current_task_id`), so progress writes from inside the `async with` block land on `task_id` and reach any subscribed UI via the store's live-tail. Use it from a custom `on_task_recover` hook to actively re-drive a stuck task after a worker crash — see Policy B under [Crash recovery](how-to.md#crash-recovery). Outside a normal A2A request this is the only way to make `report_progress` non-silent.

---

## Versioning

Current release: **0.6.0**. `__version__` is also exported at module level:

```python
import fast_a2a_app
fast_a2a_app.__version__  # → "0.6.0"
```

See [CHANGELOG.md](../CHANGELOG.md) for the full release history.
