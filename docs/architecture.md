# Architecture

This document describes how fast_a2a_app is laid out internally — the runtime topology, the storage layer, conversation history injection, and the streaming pipeline. Read [Design choices](design.md) first if you want the *why*.

## Module layout

| Module | Responsibility |
|---|---|
| `fast_a2a_app.server` | A2A JSON-RPC server (streaming SSE, multi-turn history, cross-instance cancel) |
| `fast_a2a_app.server.task_stores` | Task persistence backends — one module per backend (`memory`, `redis`, `mongo`, `postgres`). The `A2ATaskStore` Protocol is defined in `__init__.py`; `build_a2a_app` defaults to `MemoryTaskStore()` when no store is supplied |
| `fast_a2a_app.server.artifacts` | Artifact builders + typed-artifact registry. Embedded primitives (`text`, `data`, `file`, `image`) in `_core.py`; specialised types each in their own `<TAG>.py` file with autodiscover at import time |
| `fast_a2a_app.ui` | Self-contained browser chat UI — single HTML file, no build step |
| `fast_a2a_app.ui.renderers` | One `<TAG>.js` per typed artifact. `build_a2a_ui` concatenates them into the served HTML at build time so `index.html` carries only the standard / embedded artifact rendering |
| `fast_a2a_app._sdk_compat` | Startup monkey-patch fixing a protobuf C-extension incompatibility in a2a-sdk 1.0.2; safe no-op on future SDK versions |

## Runtime topology

```
FastAPI app
├── /a2a    ← Starlette ASGI app (build_a2a_app)
│   ├── POST /                            SendMessage, SendStreamingMessage, CancelTask, …
│   └── GET  /.well-known/agent-card.json
└── /       ← a2a_ui (Starlette, single HTML file)

Task store (pluggable; one of MemoryTaskStore / RedisTaskStore / MongoTaskStore / PostgresTaskStore)
├── task:{id}                  Task JSON (24 h TTL)
├── context:{cid}:tasks        Context index (task_id → sequence)
└── cancel:{id}                Cancel signal (5 min TTL)
```

The mount points are deliberately separate: the protocol server lives under `/a2a`, the UI lives at `/`, and your FastAPI application owns everything in between (auth middleware, custom routes, health checks, etc.).

## Protocol surface

Built on `a2a-sdk` 1.0.x:

| Method | Purpose |
|---|---|
| `SendMessage` | Single-shot request/response (non-streaming) |
| `SendStreamingMessage` | Streaming SSE response |
| `CancelTask` | Immediate or cross-replica cancellation |
| `SubscribeToTask` | Reconnect to an in-flight stream after a network blip |
| `GetTask` | Snapshot fallback for page-reload recovery |
| `.well-known/agent-card.json` | Agent discovery |

## Storage layer

All server-side state — including cross-instance cancel signals — is owned by the configured `A2ATaskStore`. Four implementations ship in `fast_a2a_app.server.task_stores`, each in its own module:

| Store | Module | Persistence | Cross-instance cancel | When to use |
|---|---|---|---|---|
| `MemoryTaskStore` | `task_stores/memory.py` | None (RAM, lost on restart) | No — single process only | Dev, tests, demos. The default when `task_store` is omitted. |
| `RedisTaskStore` | `task_stores/redis.py` | Native TTL keys (24 h) | Yes — short-TTL key | Production. Universally available, supports horizontal scale out of the box. |
| `MongoTaskStore` | `task_stores/mongo.py` | TTL indexes on `expires_at` | Yes — `cancel_signals` collection | Production where Mongo is already the operational store. |
| `PostgresTaskStore` | `task_stores/postgres.py` | `expires_at` columns + read-time filter | Yes — `a2a_cancel_signals` table | Production where Postgres is already the operational store. |

All four lay state out the same way conceptually: full Task JSON keyed by `task_id` (24 h TTL); a context index mapping `task_id → sequence` per `context_id` (24 h TTL); and a short-lived cancel signal keyed by `task_id` (5 min TTL). The Redis-style table from earlier versions of this doc maps onto Mongo collections and Postgres tables with the same names — see [api.md](api.md#storage) for the concrete schemas.

Every store logs an `INFO` line on initialization so the console makes it obvious which backend is live. `MemoryTaskStore` additionally warns about its single-process limitation.

Start a backing service only when you need multi-process / cross-instance cancel:

```bash
docker run -d -p 6379:6379 redis:7-alpine     # OR
docker run -d -p 27017:27017 mongo:7          # OR
docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=pw postgres:16
```

Then construct the store and hand it to `build_a2a_app`:

```python
build_a2a_app(..., task_store=RedisTaskStore.from_url("redis://localhost:6379"))
build_a2a_app(..., task_store=await MongoTaskStore.from_uri("mongodb://localhost:27017"))
build_a2a_app(..., task_store=await PostgresTaskStore.from_dsn("postgresql://postgres:pw@localhost/postgres"))
```

Task persistence *and* cancel signalling both go through the `A2ATaskStore` Protocol — a custom backend implements `signal_cancel()` / `is_cancel_signalled()` however it likes (e.g. a `cancellations` collection in Mongo).

## Conversation history injection

Each A2A task has a `context_id` shared across all turns of a conversation. `ContextAwareRequestContextBuilder` fetches all prior tasks for the same `context_id` from the configured task store and attaches them to the `RequestContext` as `related_tasks`.

The default `prompt_builder` then:

1. Calls `get_task_history()` to extract `(role, text)` pairs ordered oldest → newest.
2. Calls `format_history()` to render the most recent `history_max_lines` (default: 12) of them as `"Conversation so far:\n…"`.
3. Optionally prepends `system_prompt`.
4. Appends the current user message.

The agent therefore sees recent history without the client needing to replay it. Depth and format are fully configurable — see [How-to → Prompt management](how-to.md#prompt-management).

## Streaming pipeline

`build_stream_invoke` wraps your async generator in an `asyncio.Queue`-based relay:

1. Before starting the generator, it sets a `ContextVar` callback that `report_progress()` reads.
2. Strings from `report_progress()` are pushed into the queue with a sentinel prefix; `ConfigurableAgentExecutor` routes them to non-final `statusUpdate` events (state `TASK_STATE_WORKING`) — these power the live spinner messages in the UI.
3. All other strings yielded from the generator become `artifactUpdate` events — the streaming text the user sees.

The result: any code called during streaming can push live status updates with a single `report_progress("…")` call, regardless of which framework (or none) the agent uses.

## Cross-instance cancel

When a client calls `CancelTask`, the executor first checks whether the task is running on the local replica — if so, it cancels the in-process `asyncio.Task` directly. Otherwise it calls `A2ATaskStore.signal_cancel(task_id)`. Every replica polls `is_cancel_signalled()` during task execution; whichever replica is currently running the task observes the signal and aborts. The signalling transport is whatever the configured store uses — a short-TTL key in Redis, a document in `cancel_signals` for Mongo, a row in `a2a_cancel_signals` for Postgres — so cancellation works across horizontally-scaled deployments without sticky sessions. The default `MemoryTaskStore` only sees signals from its own process; horizontally-scaled deployments must swap in a shared backend.

## Page-reload recovery

The chat UI persists the active task ID in `localStorage`. On page load, it attempts `SubscribeToTask` to rejoin an in-flight stream; if the task has already completed, it falls back to `GetTask` for a snapshot. URL-form image and file parts re-fetch from the agent's storage endpoint, so refresh-safe galleries work without bloating `localStorage` with base64 blobs.

## Artifact taxonomy

Artifacts are A2A's payload shape: a name plus a list of `Part`s, where each part is one of `text`, `data`, or a binary (`raw` / `url`). fast_a2a_app sorts them into two tiers:

**Embedded primitives** — wrap the A2A protocol Parts directly, no `_type` discriminator. The chat UI dispatches them via the protobuf `content` oneof (and, for images, by media-type sniffing inside `renderFilePartEl`):

| Builder | Wire shape | UI renders as |
|---|---|---|
| `text_artifact(text)` | `Part(text=…)` | Markdown bubble |
| `data_artifact(dict)` | `Part(data=…)` | Generic key-value block (fallback when no specialised renderer matches) |
| `file_artifact(url=…, …)` | `Part(url=…, mediaType=…)` | Download card |
| `image_artifact(url=…, …)` | `Part(url=…, mediaType="image/*")` | Inline image preview + click-to-zoom |

**Specialised typed artifacts** — `data_artifact` payloads with an explicit `_type` discriminator. Each lives in its own Python file under `fast_a2a_app/server/artifacts/<TAG>.py`; a matching JS renderer (optional) under `fast_a2a_app/ui/renderers/<TAG>.js`. The Python autodiscover registers the `(tag, builder)` pair with `artifact_types`; `build_a2a_ui` inlines every `<TAG>.js` it finds into the served HTML.

| Tag | Builder | UI renders as |
|---|---|---|
| `TABLE` | `table_artifact(rows, columns=…, caption=…)` | Real HTML `<table>` with column headers, alternating row shading, right-aligned numerics, em-dash for nulls |
| `PROMPT_SUGGESTIONS` | `prompt_suggestions_artifact([{label, prompt}, …], text=…)` | Row of clickable pill buttons; click sends the suggestion as the next user message |
| `MAP` | `map_artifact(markers, *, center=…, zoom=…, caption=…)` | Interactive Leaflet/OSM map (Leaflet lazy-loaded from CDN on first map render) |

Tags without a matching `<TAG>.js` fall through to the generic key-value renderer, so a Python-only registration still produces something usable.

```
fast_a2a_app/server/artifacts/        fast_a2a_app/ui/renderers/
├── __init__.py        (registry +    ├── TABLE.js
│   autodiscover)                     ├── PROMPT_SUGGESTIONS.js
├── _core.py           (text/data/    └── MAP.js
│   file/image)
├── TABLE.py
├── PROMPT_SUGGESTIONS.py
└── MAP.py
```

Adding a new typed widget is one file on each side; the registry, `__init__.py`, and `index.html` need no edits. See [How-to → Adding a new typed widget](how-to.md#adding-a-new-typed-widget).

### Non-finite-float scrubbing

`_dict_to_value` (the function that wraps Python dicts into protobuf `Struct` payloads for `data_artifact`) replaces `NaN` / `±Inf` with `null` before they enter the proto. The downstream A2A task store serialises tasks via `MessageToJson`, which strictly rejects non-finite numbers — agents handling pandas / numpy data routinely produce them. Scrubbing at the artifact-construction boundary fixes the issue once for every builder built on top of `data_artifact` (every typed artifact); the IEEE `v != v` identity catches Python `float`, `numpy.float32`, `numpy.float64`, and other duck-typed NaN values.
