# Changelog

## 0.6.0 — 2026-05-11

### Fixed

- **Fullscreen suggestion pills now disabled while a task is running** —
  pills in the fullscreen image viewer were still clickable during an
  active turn, letting users fire overlapping requests. Pills are now
  disabled when `busy` or `fullscreenBusy` is true, and `setBusy()`
  toggles all existing pill buttons in sync with global busy state.
- **Sandbox `pip install` on Python 3.12** — the `_ensure()` helper in
  the sandbox preamble now passes `--break-system-packages` to pip,
  fixing silent install failures for `statsmodels`, `scikit-learn`,
  `seaborn`, and `prophet` on PEP 668 environments (Python ≥ 3.12
  externally-managed base images).
- **`MAX_TOOL_CALLS` default raised to 15** (was 8). Complex analyses
  that chain `inspect_dataset` → multiple `run_analysis` →
  `query_table` → `plot_chart` → final response easily exceed 8
  requests. Override via the `MAX_TOOL_CALLS` env var.

### Added

- **`fast_a2a_app.server.task_stores/` is now a package** with one
  module per backend, replacing the single-file `task_store.py`:
  - `memory.py` — `MemoryTaskStore`, an in-process dict store that
    needs no external service. **The new default** when
    `task_store` is omitted from `build_a2a_app`, so a
    hello-world agent boots without Docker. Single-process only.
  - `redis.py` — `RedisTaskStore` (moved unchanged), now with a
    `RedisTaskStore.from_url(url)` classmethod for ergonomic
    construction.
  - `mongo.py` — `MongoTaskStore`, async MongoDB-backed using
    `motor` and TTL indexes. Build via
    `await MongoTaskStore.from_uri("mongodb://…")`.
  - `postgres.py` — `PostgresTaskStore`, async Postgres-backed using
    `asyncpg` and `expires_at` columns. Build via
    `await PostgresTaskStore.from_dsn("postgresql://…")`.
  - The `A2ATaskStore` Protocol lives in
    `fast_a2a_app.server.task_stores.__init__` and is re-exported
    at top level alongside all four store classes.
- Every task store now logs an `INFO` line on initialization so the
  console makes it obvious which backend is live. `MemoryTaskStore`
  additionally warns about its single-process limitation.

### Changed

- **`build_a2a_app` no longer accepts `redis_url` or `redis_client`.**
  Backends own their own connection setup; pass a fully-constructed
  `task_store=` instead. The default when omitted is
  `MemoryTaskStore()`.
- Examples updated to the new pattern: `echo-agent`, `echo-multipart`,
  and `joke-agent` rely on the memory default and need no Docker;
  `holiday-planner` and `image-creator` opt into Redis when `REDIS_URL`
  is set in the environment, otherwise fall back to memory.
- **`data-analysis-agent` rebuilt on Postgres + DBOS + Anthropic Opus
  4.7 via Azure AI Foundry:**
  - Reasoning LLM swapped from Azure OpenAI (gpt-4o via `AsyncOpenAI`)
    to Anthropic Claude Opus 4.7 (via `AsyncAnthropicFoundry` +
    `pydantic_ai.models.anthropic.AnthropicModel`). Matches the
    `ppt-agent` sibling.
  - The pydantic-ai agent is now wrapped in
    `pydantic_ai.durable_exec.dbos.DBOSAgent` for durable execution
    — every model request and tool call is a checkpointed DBOS step
    in Postgres; mid-run crashes resume from the last completed
    step on next boot.
  - Task store switched from conditional Redis to
    `PostgresTaskStore.from_dsn(POSTGRES_URL)`. Both DBOS and the
    A2A task store share a single Postgres instance — DBOS owns
    the `dbos` schema, the task store owns `a2a_*` tables.
  - `result_cache` moved from Redis to Postgres (same DB as task
    store and DBOS). Redis is no longer needed for this example.
  - New env vars: `POSTGRES_URL`, `AZURE_AI_ENDPOINT`,
    `AZURE_DEPLOYMENT_NAME` (default `claude-opus-4-7`). Old env vars
    `AZURE_AI_BASE_URL` and `AZURE_AI_DEPLOYMENT_NAME` removed.
  - Recommended companion tool:
    [`dbos-argus`](https://github.com/tmarkovski/dbos-argus) for
    a read-only browser view of in-flight workflows. Run via
    `uvx dbos-argus@latest --db-url $POSTGRES_URL`.
- New top-level docs section in [docs/how-to.md](docs/how-to.md):
  *"Durable agent execution with DBOS"* — covering DBOSAgent wiring,
  Postgres co-tenancy with `PostgresTaskStore`, DBOS workflow
  cancellation via `SetWorkflowID` + `on_task_cancel`, dbos-argus for
  local observability, and the replay-vs-side-effect trade-offs to
  expect.
- New library dependencies: `anthropic>=0.40`, `dbos>=2.0`.

### Removed

- `redis_url` and `redis_client` parameters on `build_a2a_app`.
  Migration: replace `redis_url=REDIS_URL` with
  `task_store=RedisTaskStore.from_url(REDIS_URL)`.

## 0.5.0 — 2026-05-11

### Added

- **Typed-artifact extension surface** — typed data parts (`{"_type":
  "TAG", ...}`) now route through a public registry so applications
  can add their own typed widgets without forking the framework.
  - **`fast_a2a_app.server.artifacts/` is now a package** containing
    one Python file per artifact:
    - `_core.py` — embedded primitives: `text_artifact`,
      `data_artifact`, `file_artifact`, `image_artifact` (no `_type`
      envelope).
    - `TABLE.py`, `PROMPT_SUGGESTIONS.py`, `MAP.py` — specialised
      artifacts, each declaring `tag` + `builder` constants.
    - `__init__.py` — autodiscover walks the directory at import,
      registers every `(tag, builder)` pair with `artifact_types`,
      and re-exports each builder by its function name.
  - **`fast_a2a_app.artifact_types`** — process-wide
    `ArtifactTypeRegistry` singleton. `register(tag, *, builder=…)` for
    imperative registration; `all()` for introspection. Renamed from
    the earlier internal `data_types` / `DataType` names.
  - **`fast_a2a_app/ui/renderers/<TAG>.js`** — UI-side renderer files,
    one per typed artifact. `build_a2a_ui` concatenates them into the
    served HTML at build time so `index.html` carries only the
    standard-artifact rendering (markdown / key-value / file /
    inline-image). A `<TAG>.py` without a matching `<TAG>.js` falls
    through to the generic key-value renderer.
  - Adding a new typed widget = drop `MYTAG.py` into
    `server/artifacts/` and (optionally) `MYTAG.js` into
    `ui/renderers/`. No edits to `__init__.py`, `index.html`, or any
    registry call required.
- **`table_artifact(rows, columns=…, caption=…)`** — real HTML
  `<table>` rendering: column headers, alternating row shading,
  right-aligned tabular-nums for numerics, em-dash for nulls,
  horizontal-scroll for wide schemas. Light/dark CSS lives in
  `index.html`'s `<style>` block; the JS renderer is
  `ui/renderers/TABLE.js`.
- **`map_artifact(markers, *, center=…, zoom=…, caption=…)`** —
  Leaflet/OpenStreetMap renderer for geographic data. `markers` is
  `[{lat, lng, label?, popup?}, …]`; invalid coordinates are dropped
  silently so partial LLM output still produces a useful map.
  Leaflet is lazy-loaded from a CDN on first map render so chats that
  never emit a map pay zero Leaflet bytes. `ui/renderers/MAP.js`.
- **`build_a2a_ui(accepted_file_types=...)`** — narrows the chat's
  file picker via the same syntax as the HTML `<input accept>`
  attribute. Accepts a list (`[".csv", "text/csv"]`) or a pre-formed
  string (`"image/*"`). Defaults to images so unspecified callers
  see the same behaviour as before.
- **Non-image attachment tiles** — the chat UI's preview strip and
  user-bubble thumbnails now render a generic file tile (emoji icon
  + filename) for any attachment whose `mediaType` isn't `image/*`,
  replacing the broken-image icon that browsers showed before.
- **`examples/data-analysis-agent/`** — new full example. Uploads CSV
  / Excel / TSV files, runs LLM-written pandas/matplotlib code in an
  isolated Jupyter sandbox (agent-infra/sandbox), surfaces results
  as text + tables + charts + downloadable exports. Multi-file
  conversations, per-context kernel persistence, Redis-backed result
  caching keyed on `(file_hashes, user_prompt)`, dynamic
  history-grounded follow-up suggestions.
- **`AsyncIterable[str | Artifact]` streaming agents now standard** —
  both `holiday-planner` and `image-creator` emit rich artifacts
  (maps, prompt suggestions) from their tools via
  `RunContext[AgentDeps]`. `data-analysis-agent` follows the same
  pattern.
- **`MAP` in `holiday-planner`** — `recommend_destinations` now asks
  the model for `lat` + `lng` per destination and emits a `MAP`
  artifact with one pin per recommendation so the user can see the
  geography at a glance.
- **Artifact-aware suggestion generator in `holiday-planner`** —
  `generate_suggestions(reply, generated)` now reads the typed
  artifacts the tools emitted (`MAP` with N pins → "Pick Rome" /
  "Pick Athens" pills, etc.) so the user can advance without typing.
- **How-to guides expanded** — new sections on `RequestContext`,
  file attachments (incl. `accepted_file_types`), UI rendering
  conventions, and adding a new typed widget.

### Changed

- **Framework-level scrubbing of non-finite floats** — `data_artifact`
  (and therefore every typed artifact built on top of it) replaces
  `NaN` / `±Inf` with `null` before they enter the protobuf `Struct`.
  Fixes a class of `MessageToJson` serialisation errors that hit
  anyone surfacing pandas data with missing cells. Catches Python
  `float`, every `numpy.floating` subtype, and other duck-typed NaN
  values via the IEEE `v != v` identity.
- **UI dispatch unified** — `renderDataPartEl` is now a single registry
  lookup (`window.A2A_RENDERERS[value._type]`) with a generic
  key-value fallback. Hard-coded `if (value._type === 'PROMPT_SUGGESTIONS')`
  branches removed.
- **`/hello` returns text only** in every agent — the starter
  suggestion pills it used to pin to the welcome turn were offering
  dead clicks before the user had uploaded a dataset or attached an
  image. Starters are still shown where they earn their keep (empty
  composer, dataset uploaded without a question).
- **`holiday-planner` layout matches `image-creator`** —
  `_invoke` / `_stream_invoke` moved out of `main.py` into
  `agent.py` as public `invoke` / `stream_invoke`; the
  `_generate_suggestions` helper became `generate_suggestions` in
  `agent.py` too.
- **`data-analysis-agent` result cache keys** include a version
  prefix (`dataagent:cache:v4:`) so wire-format changes abandon
  stale entries via TTL rather than poisoning new turns.

### Removed

- `ArtifactType.js_renderer` field and `ArtifactTypeRegistry.js_snippet()`
  method — the Python registry no longer tracks JS renderers. UI
  renderers live in `ui/renderers/<TAG>.js` and are discovered by
  `build_a2a_ui` directly.

---

## 0.4.0 — 2026-05-06

### Added

- **Progressive Disclosure prompt management** — `build_a2a_app` now exposes
  two new keyword parameters that let developers tune the built-in prompt
  without writing any custom code:

  - `system_prompt: str | None` — prepended to every prompt; use it for
    persona, constraints, or output-format instructions.
  - `history_max_lines: int = 12` — controls how many prior conversation
    lines are injected (the default is unchanged). Set to `0` for a
    stateless agent, or raise the value for longer memory.

- **`get_user_input(context)`** — new public helper that returns the current
  user message text from a `RequestContext`. Exported from the top-level
  package alongside `build_conversation_prefix`. Use both helpers to compose
  a custom `prompt_builder` without knowing the internal SDK method name.

- **`build_conversation_prefix` gains `max_lines` parameter** — the history
  window is now configurable at the call site when writing a custom
  `prompt_builder`. The default (`12`) is unchanged, so existing code is
  unaffected.

### Changed

- `build_a2a_app`: `prompt_builder` default changed from a module-level
  function reference to `None`; the default builder is now constructed lazily
  from `system_prompt` and `history_max_lines`. Behaviour is identical when
  neither parameter is set.
- `build_a2a_app` docstring rewritten to document all four prompt-management
  levels (zero-config, keyword params, helper composition, full custom builder)
  with runnable examples.

---

## 0.3.0 — 2026-05-05

### Added

- **Multi-part responses** — `build_invoke` now accepts `async (str) -> Artifact`
  in addition to `async (str) -> str`. Return an `Artifact` with any combination
  of text, data, and file parts; the executor detects the return type at runtime
  and routes accordingly — no separate wrapper or executor needed.
- **`examples/echo-multipart/`** — new zero-dependency example demonstrating
  multi-part A2A responses: text echo, JSON metadata (data part), and a
  downloadable file (file part). Agent uses only `json.dumps` — no protobuf
  struct building required.
- **UI: adaptive stream toggle** — the stream toggle is now hidden when the agent
  card reports `capabilities.streaming = false`, so non-streaming agents present a
  clean input bar with no inapplicable control.
- **UI: data part widget** — parts whose `media_type` is `application/json` are
  rendered as a labeled key-value table with color-coded value types (strings,
  numbers, booleans). Raw JSON brackets are never shown to the user.
- **UI: file part widget** — raw binary parts are rendered as a download card
  showing a type icon, filename, and media type. Clicking "Download" creates a
  temporary Blob URL and triggers a browser download.

### Changed

- **`build_invoke` signature broadened** — parameter type is now
  `Callable[[str], Awaitable[str | Artifact]]`; existing `str`-returning agents
  are unaffected.
- **`build_a2a_app` `invoke` parameter** — type widened to
  `Callable[[str, RequestContext], Awaitable[str | Artifact]]` to match.

---

## 0.2.0 — 2026-05-05

### Changed

- **A2A protocol upgraded to v1.0** — migrated from a2a-sdk pydantic API to
  protobuf-based a2a-sdk 1.0.x. All wire types (Task, Message, Part, Artifact,
  TaskState, Role) are now protobuf messages; Redis serialisation uses
  `MessageToJson` / `Parse` from `google.protobuf.json_format`.
- **`AgentCard` construction** — `url=` removed from `AgentCard`; replaced by
  `supported_interfaces=[AgentInterface(url=..., protocol_binding="JSONRPC")]`.
- **`TaskState` enum values** now use SCREAMING_SNAKE_CASE
  (`TASK_STATE_WORKING`, `TASK_STATE_COMPLETED`, etc.).
- **`Part` construction** simplified to `Part(text=text)` (protobuf oneof field).
- **`a2a.helpers`** — history/artifact/message construction now uses
  `new_task_from_user_message`, `new_text_artifact`, `new_text_message`.
- `DefaultRequestHandler` now requires `agent_card=` argument.
- `ContextAwareRequestContextBuilder.build()` updated to new SDK signature;
  uses `request_context.attach_related_task()` to inject history.
- **UI speaks A2A v1.0** — all JSON-RPC method names (`SendStreamingMessage`,
  `SendMessage`, `CancelTask`, `SubscribeToTask`, `GetTask`), role values
  (`ROLE_USER`, `ROLE_AGENT`), and state strings (`TASK_STATE_*`) updated.
  Requests now send `A2A-Version: 1.0` header required by the SDK.
- `max_tokens` → `max_completion_tokens` in all OpenAI API calls (required by
  newer model versions).
- `openai_client=` moved from `OpenAIModel(...)` to
  `OpenAIProvider(openai_client=...)` in pydantic-ai ≥ 0.2.

### Added

- `_sdk_compat.py` — startup monkey-patch fixing protobuf C-extension
  incompatibility in a2a-sdk 1.0.2 (`field.label` → `field.is_repeated`).
  Applied automatically on import; safe no-op on future SDK versions.
- **`SendMessage` support** — non-streaming path handled alongside streaming.
- **UI stream toggle** — checkbox in the input bar switches between streaming
  (`SendStreamingMessage`) and non-streaming (`SendMessage`) mode; preference
  persisted in `localStorage`.
- `examples/joke-agent/` and `examples/echo-agent/` added alongside the
  existing holiday planner.

### Fixed

- `load_dotenv()` moved before agent import in `joke-agent/main.py` so env vars
  are populated before module-level client construction.
- Azure credential provider wrapped in `async def` to satisfy newer OpenAI SDK
  which `await`s the `api_key` callable.
- Empty `chunk.choices` guard added to streaming loops (final usage chunk has
  no choices).
- Streaming token spacing: `collectTexts` now concatenates parts within one
  artifact before returning, preserving inter-token whitespace and preventing
  `join('\n\n')` from inserting paragraph breaks between individual tokens.

### Removed

- `ensure_context_marker` — no longer needed; `contextId` is a first-class
  field on the A2A `Message` and the SDK populates `context.context_id`
  directly. The `[cid:…]` text embedding is gone from both client and server.
- `enable_v0_3_compat=True` from `create_jsonrpc_routes` — no longer needed
  once the UI speaks v1.0.

---

## 0.1.0 — 2026-05-05

Initial release.

### Added

- `fast_a2a_app.server` — A2A protocol adapter extracted and cleaned from ppt-agent
  - `build_a2a_app()` factory assembling a Starlette ASGI app
  - `build_invoke()` / `build_stream_invoke()` wrappers for any async callable
  - `report_progress()` for live tool status updates via ContextVar
  - `RedisTaskStore` with context-id indexing and cross-instance cancel signals
  - `ConfigurableAgentExecutor` with `on_task_start` / `on_task_cancel` hooks
    (`on_task_cancel` receives `(context_id, task_id)`)
  - `ContextAwareRequestContextBuilder` for multi-turn history injection
  - `debug` parameter for controlling error verbosity
  - `redis_url` parameter replacing hard-coded config import
- `fast_a2a_app.ui` — Self-contained browser chat UI (no build step, no npm)
  - Streaming SSE with real-time progress indicator
  - localStorage persistence (context ID, transcript, active task)
  - Page-reload recovery via `SubscribeToTask` and `GetTask` fallback
  - Markdown rendering with DOMPurify sanitisation
  - Collapsible agent card panel
- `examples/holiday-planner/` — Full example application
  - Holiday planning agent with 4 tools: destinations, itinerary, budget, essentials
  - FastAPI app wiring agent + fast_a2a_app server + fast_a2a_app UI
