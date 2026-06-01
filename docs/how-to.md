# How-to guides

Task-oriented recipes. For the full surface of each function, see the [API reference](api.md).

## Contents

- [Project layout](#project-layout)
- [Using the `RequestContext`](#reading-raw-input-with-requestcontext)
- [Prompt management](#prompt-management)
- [Multi-part responses](#multi-part-responses)
- [Live progress updates](#live-progress-updates)
- [Configurable parameters via `/set`](#configurable-parameters-via-set)
- [File attachments](#file-attachments)
- [UI rendering conventions](#ui-rendering-conventions)
- [Lifecycle hooks](#lifecycle-hooks)
- [Choosing a task store](#choosing-a-task-store)
- [Durable agent execution](#durable-agent-execution)
- [Debug mode](#debug-mode)


---

## Project layout

Every example in [`examples/`](../examples) follows the same shape — start with two files, add a third only when you have enough environment-driven settings to make the duplication hurt.

```
my-agent/
├── agent.py     # AgentCard + invoke / stream_invoke (+ tools, prompts)
├── main.py      # FastAPI app — mounts build_a2a_app + the chat UI
└── config.py    # (optional) module-level constants from env vars
```

`agent.py` owns everything that describes the agent — its public metadata (the `AgentCard`), its skills, and the `invoke` / `stream_invoke` entry points the framework calls. Keeping the card next to the functions means the agent's description, its declared skills, and its actual behaviour can't drift out of sync:

```python
# agent.py
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from config import APP_BASE_URL

agent_card = AgentCard(
    name="My Agent",
    description="…",
    version="0.1.0",
    supported_interfaces=[AgentInterface(url=f"{APP_BASE_URL}/a2a/", protocol_binding="JSONRPC")],
    capabilities=AgentCapabilities(streaming=True),
    default_input_modes=["text"],
    default_output_modes=["text"],
    skills=[AgentSkill(id="chat", name="Chat", description="…", tags=[])],
)

async def invoke(prompt: str) -> str: ...
async def stream_invoke(prompt: str): ...
```

`main.py` is a thin composition root — no agent logic, just wiring:

```python
# main.py
from fastapi import FastAPI
from fast_a2a_app import a2a_ui, build_a2a_app, build_invoke, build_stream_invoke
from agent import agent_card, invoke, stream_invoke

app = FastAPI()
app.mount("/a2a", build_a2a_app(
    agent_card=agent_card,
    invoke=build_invoke(invoke),
    stream_invoke=build_stream_invoke(stream_invoke),
    # Omitting task_store uses the in-process MemoryTaskStore — single-process only.
    # For multi-process / cross-instance cancel, pass:
    #     task_store=RedisTaskStore.from_url(os.environ["REDIS_URL"])
    # (or MongoTaskStore.from_uri / PostgresTaskStore.from_dsn).
))
app.mount("/", a2a_ui)
```

### When to add `config.py`

A dedicated config module pays off the moment the same env-var read appears in two files, or the same module reaches for more than two or three settings. Pull every `os.environ` / `os.getenv` lookup into one place and import named constants everywhere else:

```python
# config.py
import os

# ── App / infra ──────────────────────────────────────────────────────────────
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")
REDIS_URL    = os.getenv("REDIS_URL", "redis://localhost:6379")
DEBUG        = os.getenv("DEBUG", "true").lower() in ("1", "true", "yes")

# ── Azure OpenAI ─────────────────────────────────────────────────────────────
AZURE_AI_BASE_URL        = os.environ.get("AZURE_AI_BASE_URL", "").strip().rstrip("/")
AZURE_AI_DEPLOYMENT_NAME = os.environ.get("AZURE_AI_DEPLOYMENT_NAME", "").strip() or "gpt-4o"

# ── Cost ceilings (per-turn) ─────────────────────────────────────────────────
MAX_TOOL_CALLS      = int(os.environ.get("MAX_TOOL_CALLS", "15"))
MAX_TOKENS_PER_TURN = int(os.environ.get("MAX_TOKENS_PER_TURN", "60000"))
```

The override surface is now one file — operators set env vars (or drop entries into `examples/.env`, which `main.py` loads via `python-dotenv` *before* the first `import config`), and a code-reader can find every tunable by reading `config.py` top-to-bottom. The two richer examples ([image-creator](../examples/image-creator), [holiday-planner](../examples/holiday-planner)) use this shape; the minimal echo / joke agents stay two-file because three names per module don't earn the indirection.

The single-file inline shape is fine when there's no second file to share state with — see the [60-second quickstart in the README](../README.md#60-second-quickstart). But the moment you add tools, prompt templates, message-history shaping, or a second auxiliary module (sandbox session, cache, image store…), splitting `agent.py` from `main.py` (and lifting env reads into `config.py`) pays off: each file owns one concern, and operators can audit every setting at a glance.

---

## Using the `RequestContext`

Start here — this is foundational. Every non-trivial agent in the repo reaches for `RequestContext`, but the rest of the guide assumes you already know what it is.

`build_a2a_app` calls your agent with a single string argument by default — the prompt the framework already assembled (system prompt + history + the user's text). That covers most chat agents. But anything beyond plain text needs the **raw request**: file uploads, slash commands routed before the LLM sees them, multi-turn workflow IDs, follow-up turns that reference an artifact from an earlier turn.

For these cases, declare a second positional parameter typed as `RequestContext` and `build_invoke` / `build_stream_invoke` will pass it through:

```python
from a2a.server.agent_execution import RequestContext
from fast_a2a_app import build_invoke, build_stream_invoke

# Without context — what most examples start with:
async def fn(prompt: str) -> str: ...

# With context — opt in by adding a second positional parameter:
async def fn(prompt: str, context: RequestContext) -> str: ...
async def fn(prompt: str, context: RequestContext) -> AsyncIterable[str | Artifact]: ...

build_a2a_app(invoke=build_invoke(fn), stream_invoke=build_stream_invoke(fn), ...)
```

The wrapper inspects your function's signature with `inspect.signature` and forwards the context only when you ask for it — no flag, no opt-in keyword. Both shapes work with `build_invoke` and `build_stream_invoke`; type the parameter as `RequestContext` from `a2a.server.agent_execution` so the IDE knows what's on it.

### What the context exposes

| Field                  | Use it for                                                                                                                |
|------------------------|---------------------------------------------------------------------------------------------------------------------------|
| `context.context_id`   | Stable ID for the **conversation**. Key external state by it — DB rows, locks, persistent sandbox sessions.               |
| `context.task_id`      | Stable ID for **this turn**. Per-task locks, idempotency, metrics.                                                        |
| `context.message`      | The current user message. Walk `context.message.parts` to read uploads (`url` / `raw`), detect slash commands, route input.|
| `context.related_tasks`| Prior `Task` objects in this conversation, chronological. Each has `.history` (user side) and `.artifacts` (agent side).  |
| `context.current_task` | The `Task` being executed now. Same shape as entries in `related_tasks`.                                                  |

`message.parts` is a list of protobuf `Part` objects. Each `Part` carries exactly one of `text`, `raw` (inline bytes), `url` (URL reference), or `data`. Use `part.WhichOneof("content")` to discriminate.

### Pattern 1 — read a file upload + a slash command

```python
async def stream_invoke(prompt: str, context: RequestContext):
    # Walk the parts ONCE; classify each.
    user_text, file_url = "", None
    for part in context.message.parts or []:
        kind = part.WhichOneof("content")
        if kind == "text" and part.text:
            user_text += part.text
        elif kind == "url" and part.url:
            file_url = part.url   # set by the UI after a successful /uploads POST

    if user_text.lower().startswith("/help"):
        yield text_artifact("…help text…")
        return

    if file_url:
        # process the file (it lives at file_url on the same FastAPI app)
        ...
```

Why read parts directly? The default prompt builder concatenates history + user text into one string for the LLM. Slash commands and file URLs would get drowned in that — they're routing decisions, not LLM input. Reading `context.message.parts` gives you the **untouched** current turn.

If you just need `(text, files)` from the current turn — text-or-image agent, document-question agent, etc. — skip the manual walk and use [`extract_current_turn`](../src/fast_a2a_app/server/commons/uploads.py) from `commons.uploads`. It handles the `raw` / `url` switch, takes an optional resolver for URL refs, and an optional MIME predicate to filter. See [§File attachments → Reading uploads in your agent](#reading-uploads-in-your-agent) for the wiring.

### Pattern 2 — key external state by `context_id`

```python
async def stream_invoke(prompt: str, context: RequestContext):
    # One sandbox kernel per conversation; survives across turns.
    session = await sandbox.ensure(context.context_id)
    result = await session.execute(...)
```

`context_id` is stable across every turn in a chat thread, so it's the right key for any state that should outlive a single tool call: a workflow's plan/approval state, a per-conversation DB row, a long-lived browser tab in a multi-turn web agent. (`task_id` changes every turn — use it for *per-turn* state only.)

### Pattern 3 — feed conversation history into a framework

pydantic-ai, LangChain, and similar frameworks all want history in their *own* message format, not a string prefix. Walk `context.related_tasks` and translate:

```python
from a2a.types import Role
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

def build_message_history(context: RequestContext):
    messages = []
    for task in context.related_tasks or []:
        # User text — first user-role message in the task's history.
        for msg in task.history or []:
            if msg.role == Role.ROLE_USER:
                user_text = "\n".join(
                    p.text for p in (msg.parts or [])
                    if p.WhichOneof("content") == "text" and p.text
                )
                if user_text:
                    messages.append(ModelRequest(parts=[UserPromptPart(content=user_text)]))
                break
        # Agent reply — text parts of all artifacts on this task.
        agent_text = "\n".join(
            p.text for art in (task.artifacts or []) for p in (art.parts or [])
            if p.WhichOneof("content") == "text" and p.text
        )
        if agent_text:
            messages.append(ModelResponse(parts=[TextPart(content=agent_text)]))
    return messages

async def stream_invoke(prompt, context):
    result = await my_agent.run(prompt, message_history=build_message_history(context))
    yield text_artifact(result.output)
```

Pair this with `history_max_lines=0` in `build_a2a_app` so the default prompt builder doesn't *also* prepend a string history — that would double-feed prior turns into the model.

### Pattern 4 — let a follow-up reference an earlier artifact

The user asks "*now plot it as a bar chart*" — *it* is the table the agent produced two turns ago. Walk `context.related_tasks` for the most recent `data_artifact` (or image, file, etc.):

```python
def latest_table_in_history(context: RequestContext) -> dict | None:
    latest = None
    for task in context.related_tasks or []:
        for art in task.artifacts or []:
            for part in art.parts or []:
                if part.WhichOneof("content") == "data" and part.data:
                    latest = part.data
    return latest
```

The `image-creator` example uses the same shape to find the most recent image so "*make it warmer*" picks up the right reference without re-uploading — but it goes through [`latest_file_in_history`](../src/fast_a2a_app/server/commons/uploads.py) from `commons.uploads`, which already walks both `task.history` (user uploads) and `task.artifacts` (agent-produced files) with a resolver + predicate. Reach for the helper for `raw`/`url` file parts; keep the manual walk above for custom `data` shapes the helpers don't know about.

> Tip — when you both **read raw input** and **build history**, set `history_max_lines=0` so the default builder stays out of the way; you're now driving prompt construction yourself.

---

## Prompt management

fast_a2a_app injects conversation history automatically, but you can take as much or as little control over prompt construction as you need. The API follows **Progressive Disclosure** — use only the level that fits your use case.

### Level 0 — zero config

Works out of the box. The last 12 lines of conversation history are prepended to the user's message as `"Conversation so far:\n…"`. Nothing to set.

```python
build_a2a_app(agent_card=card, stream_invoke=build_stream_invoke(my_fn))
```

### Level 1 — keyword parameters

Tune the built-in prompt without writing any code:

```python
build_a2a_app(
    agent_card=card,
    stream_invoke=build_stream_invoke(my_fn),
    system_prompt="You are a concise travel planner. Reply in JSON.",
    history_max_lines=6,   # default is 12; set to 0 for a stateless agent
)
```

`system_prompt` is prepended before the history block and the user message. `history_max_lines=0` disables history injection entirely.

### Level 2 — compose from helpers

Build a custom prompt from the exported building blocks:

```python
from fast_a2a_app import format_history, get_task_history, get_user_input

def my_prompt(context) -> str:
    return (
        "You are an expert planner.\n\n"
        + format_history(get_task_history(context), max_lines=4)
        + f"Respond in JSON:\n{get_user_input(context)}"
    )

build_a2a_app(..., prompt_builder=my_prompt)
```

`get_task_history(context)` returns raw `(role, text)` pairs (`role` is `"user"` or `"agent"`) so you can also route or filter conversation turns yourself. `format_history(pairs, *, max_lines, header)` is the formatter — pass it the pairs to render a `"Conversation so far:\n…"` block.

### Level 3 — full custom builder

Pass any `(RequestContext) -> str` as `prompt_builder` for complete control. `system_prompt` and `history_max_lines` are ignored when a custom `prompt_builder` is supplied.

```python
def my_prompt(context) -> str:
    # context.get_user_input()   — current user message
    # context.related_tasks      — prior Task objects for this conversation
    # context.current_task       — task being executed now
    # context.message            — raw A2A Message object
    return f"Be concise.\n{context.get_user_input()}"

build_a2a_app(..., prompt_builder=my_prompt)
```

---

## Multi-part responses

Return multiple parts (text + JSON data + a downloadable file) from a single agent turn.

### Non-streaming

`build_invoke` accepts `async (str) -> Artifact`:

```python
from a2a.types import Artifact, Part
from fast_a2a_app import build_a2a_app, build_invoke
import json, uuid

async def my_agent(prompt: str) -> Artifact:
    return Artifact(
        artifact_id=str(uuid.uuid4()),
        name="result",
        parts=[
            Part(text=f"Here is your data for: {prompt}"),
            Part(raw=json.dumps({"count": 42}).encode(), media_type="application/json"),
            Part(raw=b"file content", filename="out.txt", media_type="text/plain"),
        ],
    )
```

### Streaming

Use the artifact builders and `yield` them from your generator:

```python
from fast_a2a_app import (
    text_artifact, data_artifact, file_artifact, image_artifact,
)

async def stream_invoke(prompt: str):
    yield text_artifact("Working on it…")
    yield data_artifact({"count": 42, "ok": True}, text="Summary:")
    yield image_artifact(png_bytes, caption="Generated chart.")
    yield file_artifact(content=b"...", filename="report.pdf", media_type="application/pdf")
```

The UI renders each part with the right widget — markdown for text, key-value table for data, inline preview for images, download card for files.

See the [Echo Multipart example](../examples/echo-multipart/README.md) for a runnable zero-dependency demo.

---

## Live progress updates

Call `report_progress("…")` from anywhere inside a streaming agent to push a status string to the chat UI spinner. Outside a streaming context the call is a safe no-op, so tool functions don't need to know whether they're being streamed.

```python
from fast_a2a_app import report_progress

@agent.tool_plain
async def fetch_destinations(criteria: str) -> list[str]:
    report_progress("Searching destinations…")
    results = await search(criteria)
    report_progress(f"Found {len(results)} destinations, ranking…")
    return rank(results)
```

The status appears in the working-state indicator in the UI until the next status update or the final result.

---

## Configurable parameters via `/set`

When you want the user to switch knobs at runtime (which model, output size, output style, verbosity, …) without redeploying, declare a **single schema dict** in `agent.py`, hand it to the helpers in [`fast_a2a_app.server.commons.config`](../src/fast_a2a_app/server/commons/config.py), and call them from `stream_invoke`. The active values are recovered from A2A history each turn — no server-side store, no env-var-restart cycle, no duplicate truth.

Implemented end-to-end in [examples/image-creator/agent.py](../examples/image-creator/agent.py) and [examples/image-creator/main.py](../examples/image-creator/main.py).

### 1. Declare the schema in `agent.py`

```python
# agent.py
CONFIG_PARAMETERS: dict[str, dict] = {
    "model": {
        "description": "Image deployment used for generation.",
        "default": "gpt-image-1-mini",
        "values": {
            "gpt-image-1-mini": "Faster, lower-cost.",
            "gpt-image-1":      "Higher fidelity, slower.",
        },
    },
    "size": {
        "description": "Output image dimensions.",
        "default": "1024x1024",
        "values": {
            "1024x1024": "Square.",
            "1024x1536": "Portrait.",
            "1536x1024": "Landscape.",
        },
    },
}
```

Keep the schema next to the code that reads it — the helpers, the tool that consumes the resolved values, and the LLM-facing system prompt all live in `agent.py`.

### 2. Expose the schema at `GET /config`

```python
# main.py
from agent import CONFIG_PARAMETERS

@app.get("/config", tags=["ops"])
async def get_config() -> dict:
    return CONFIG_PARAMETERS
```

The endpoint serves the schema verbatim — no transformation. Admin UIs, monitoring, and the chat UI itself can discover what `/set` can switch.

### 3. Wire `commons.config` into `stream_invoke`

Three function calls + one constant cover the whole pattern. The helpers ship in [`fast_a2a_app.server.commons.config`](../src/fast_a2a_app/server/commons/config.py) and are framework-agnostic — they read from `RequestContext` and return `Artifact` / `dict[str, str]` / `str`, nothing else.

```python
# agent.py
from fast_a2a_app import build_stream_invoke, text_artifact
from fast_a2a_app.server.commons.config import (
    ACTIVE_SETTINGS_CONVENTION,
    format_active_settings,
    handle_set_command,
    resolve_config_from_history,
)

SYSTEM_PROMPT = (
    "You are a concise image-creation agent.\n\n"
    + ACTIVE_SETTINGS_CONVENTION
)

@my_agent.tool
async def generate_image(ctx: RunContext[AgentDeps], prompt: str) -> str:
    model = ctx.deps.config["model"]
    size  = ctx.deps.config["size"]
    style = ctx.deps.config["style"]
    # … pass them to the underlying API call.

async def stream_invoke(prompt: str, context: RequestContext):
    user_text = context.get_user_input()
    config    = resolve_config_from_history(context, CONFIG_PARAMETERS)

    if reply := handle_set_command(user_text, config, CONFIG_PARAMETERS):
        yield reply           # /set wizard short-circuits the agent loop
        return

    prompt_ext = f"{prompt}\n\n{format_active_settings(config, CONFIG_PARAMETERS)}"
    deps = AgentDeps(config=config)
    result = await my_agent.run(prompt_ext, deps=deps)
    yield text_artifact(str(result.output))
```

What each piece does:

| Helper | Purpose |
|---|---|
| `resolve_config_from_history(context, schema)` | Walks `context.related_tasks` for past `/set <param> <value>` user messages and returns the resolved snapshot. Unset parameters fall back to `schema[<name>]["default"]`. Latest valid assignment wins. |
| `handle_set_command(user_text, config, schema)` | Returns a wizard reply Artifact when `user_text` matches `/set …` (step-1 pills, step-2 pills, validation errors, or the confirmation message). Returns `None` for non-`/set` text — caller falls through. |
| `format_active_settings(config, schema)` | Renders the resolved dict as the `Active settings:\n- name: value` block to append to the user prompt. Values that match `schema[<name>]["default"]` are tagged `(default)`. |
| `ACTIVE_SETTINGS_CONVENTION` | Drop-in preamble for your system prompt explaining the convention to the orchestrator LLM so it doesn't restate the settings inside tool arguments or ignore non-default values in its replies. |

### Why both `deps` and `format_active_settings` are needed

The deps wiring (`AgentDeps(config=config)`) gets the values to your tools — `ctx.deps.config["model"]` reaches the API call directly, without ever passing through the LLM.

The `format_active_settings` block is what tells the orchestrator LLM that the settings exist. Skip it and the tool will still receive the right `model` / `size` / `style`, but the agent's textual reply will pretend nothing changed (it has no idea the user typed `/set style anime` two turns ago).

`ACTIVE_SETTINGS_CONVENTION` is the system-prompt counterpart — it teaches the LLM how to behave with the trailing block ("don't restate these in tool args", "acknowledge non-default values briefly", "suggest `/set` for one-off overrides").

### Adding app-specific slash commands

`handle_set_command` only handles `/set`. If your agent ships extra commands (`/help`, `/clear`, …), keep them in user code and call `handle_set_command` as a fall-through:

```python
def _handle_slash_command(user_text, config):
    if user_text.lower() == "/help":
        return text_artifact(HELP_TEXT)
    return handle_set_command(user_text, config, CONFIG_PARAMETERS)
```

### Why this shape

- **Single source of truth.** Adding a parameter or a value is a one-line edit to `CONFIG_PARAMETERS`. The `/set` pills, `GET /config` payload, validation, confirmation messages, and the `Active settings:` block all pick it up automatically.
- **No server-side state.** History-derived config is idempotent; turns are reproducible from the transcript alone. Works the same in single-process memory and multi-process Redis/Mongo/Postgres deployments.
- **Catalog-only by design.** Unknown parameter names and unknown values are rejected by `handle_set_command` with a recovery hint. Tools only ever see values that are in the enum.
- **Case-insensitive in, lowercase out.** `/SET Model gpt-image-1`, `/Set MODEL gpt-image-1`, and `/set model gpt-image-1` all behave the same; pills and docs use lowercase by convention so the surface reads consistently.
- **Both wiring paths still matter.** `ctx.deps.config` makes sure the API receives the right values. `format_active_settings` + `ACTIVE_SETTINGS_CONVENTION` make sure the LLM's replies acknowledge them. Skip either and the behaviour drifts in a way that's hard to diagnose later.

---

## File attachments

File upload is **opt-in**. By default the paperclip button is hidden. Enable it by passing `file_upload_api=` to `build_a2a_ui`:

```python
from fastapi import FastAPI, UploadFile
from fast_a2a_app import build_a2a_app, build_a2a_ui

app = FastAPI()

@app.post("/uploads")
async def upload(file: UploadFile):
    blob_id = save_to_storage(await file.read())
    return {
        "id": blob_id,
        "url": f"/uploads/{blob_id}",
        "mediaType": file.content_type,
        "filename": file.filename,
    }

app.mount("/a2a", build_a2a_app(...))
app.mount("/", build_a2a_ui(file_upload_api="/uploads"))
```

The UI then `POST`s files to `/uploads` as `multipart/form-data`, receives `{id, url, mediaType, filename}`, and sends a `{url, filename, mediaType}` part to the agent on the next user message.

### Restricting which file types the picker offers

By default the picker allows the four image formats the chat UI renders inline (`image/png`, `image/jpeg`, `image/webp`, `image/gif`). Override that with `accepted_file_types` — same format as the HTML `<input accept>` attribute (file extensions, MIME types, or MIME wildcards):

```python
# Single-file CSV / Excel uploader
app.mount("/", build_a2a_ui(
    file_upload_api="/uploads",
    accepted_file_types=[
        ".csv", ".tsv", ".xls", ".xlsx",
        "text/csv",
        "text/tab-separated-values",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ],
))

# Any image — wildcards work too
app.mount("/", build_a2a_ui(
    file_upload_api="/images",
    accepted_file_types="image/*",
))
```

A list is joined into a comma-separated string for you; a string passes through unchanged so you can paste a pre-formatted `accept=` value if you already have one. The picker's allowlist is **UX, not security** — always validate the `Content-Type` and size in your upload endpoint anyway.

### Reading uploads in your agent

Uploads reach your `invoke` / `stream_invoke` as parts on `context.message.parts`. Fresh uploads arrive **inline** as `raw` parts (bytes ride along with the message); follow-up turns that reference a prior file arrive as `url` parts (the UI re-sends the URL, you fetch the bytes from your store). Every non-trivial agent ends up writing the same walker over both cases plus another over `context.related_tasks` for "edit the thing I sent two turns ago" — so [`fast_a2a_app.server.commons.uploads`](../src/fast_a2a_app/server/commons/uploads.py) ships the three pieces.

```python
from fast_a2a_app.server.commons.uploads import (
    extract_current_turn,        # walks context.message.parts → (text, files)
    latest_file_in_history,      # walks context.related_tasks → most recent file
)

# Adapter from YOUR store to the resolver shape: url → (bytes, mime) | None.
def _resolve_url(url: str):
    blob_id = my_store.id_from_url(url)
    return my_store.get(blob_id) if blob_id else None

# Optional predicate — keeps only the file kinds your agent cares about.
def _is_image(_data, mime):
    return mime.startswith("image/")

async def stream_invoke(prompt: str, context: RequestContext):
    user_text, uploads = extract_current_turn(
        context, resolver=_resolve_url, predicate=_is_image,
    )
    # "make it warmer" with no fresh upload → fall back to whatever the
    # conversation last surfaced (user upload OR agent-produced image).
    previous = latest_file_in_history(
        context, resolver=_resolve_url, predicate=_is_image,
    ) if not uploads else None
    ...
```

What each piece does:

| Helper | Returns | Walks |
|---|---|---|
| `resolve_file_part(part, resolver=…)` | `(bytes, mime) \| None` | A single A2A `Part` — `raw` inline, `url` via your resolver, anything else → None. |
| `extract_current_turn(context, resolver=…, predicate=…)` | `(text, [(bytes, mime), …])` | `context.message.parts` — the **untouched current turn**, ideal for slash-command routing and per-turn upload handling. |
| `latest_file_in_history(context, resolver=…, predicate=…)` | `(bytes, mime) \| None` | `context.related_tasks` — both user-side message history and agent-side artifacts. Last match wins. |

Why a resolver callable and not a built-in store: every team's storage is different (S3, Bynder, Frontify, local disk), and the framework already makes `POST /uploads` opt-in. Commons stays storage-agnostic — you wire it to whatever backend the rest of your app uses by passing a 4-line adapter.

The image-creator example wires `image_store` (a local file-backed store next to `agent.py`) into both helpers — see [examples/image-creator/agent.py](../examples/image-creator/agent.py).

---

## UI rendering conventions

The bundled chat UI inspects each data part for a `_type` discriminator and routes it to a specialised widget when it matches one of the well-known envelopes below. Anything without a recognised `_type` falls through to a generic key-value renderer, so untyped `data_artifact` payloads still display — just less prettily.

The discriminator pattern follows the Pydantic convention: a leading-underscore `_type` field carrying an uppercase-snake-case tag, alongside whatever payload fields the widget needs.

### Recognised data envelopes

| `_type`               | Helper                          | Renders as                                                            |
|-----------------------|---------------------------------|------------------------------------------------------------------------|
| `TABLE`               | `table_artifact(...)`           | A real HTML `<table>` with column headers, alternating row shading, right-aligned monospace numerics, em-dash for nulls, and horizontal-scroll for wide schemas. |
| `PROMPT_SUGGESTIONS`  | `prompt_suggestions_artifact(...)` | A row of clickable pill buttons. Clicking a pill sends the suggestion's `prompt` text as a normal user message. |
| *(none)*              | `data_artifact(...)`            | Generic key-value block: one row per top-level key, types coloured (string / number / bool / null / object). For arbitrary JSON-shaped state. |

Every envelope can be paired with a `text` / `caption` argument that's rendered as a markdown caption immediately above the widget.

### `table_artifact` — tabular data

Use this for any `(rows, columns)` shape — top-N tables, group-by aggregates, schema previews, pivots. Cells are JSON-friendly scalars; `None` (or pandas `NaN`, which is auto-scrubbed at the framework boundary) renders as a faint em-dash.

```python
from fast_a2a_app import table_artifact

async def stream_invoke(prompt: str):
    yield table_artifact(
        rows=[["APAC", 38400, 0.38], ["EMEA", 22000, 0.21]],
        columns=["region", "revenue", "growth"],
        caption="Top 2 regions by revenue",
    )
```

If you omit `columns`, headers are auto-derived from the row width (`col_1`, `col_2`, …).

### `prompt_suggestions_artifact` — clickable follow-ups

Yield this to give the user one-click follow-up options. Each entry is a `{label, prompt}` pair: the label is the button text, the prompt is what gets sent as the user's next message.

```python
from fast_a2a_app import prompt_suggestions_artifact, image_artifact, text_artifact

async def stream_invoke(prompt: str):
    yield text_artifact("Here's your image.")
    yield image_artifact(url="/images/abc")
    yield prompt_suggestions_artifact(
        [
            {"label": "Make it warmer", "prompt": "Make the lighting warmer."},
            {"label": "Add a sunset",   "prompt": "Add a sunset in the background."},
        ],
        text="Want to refine?",
    )
```

Common patterns:

- **Closing pills** at the end of every turn — see `examples/holiday-planner`, which generates them dynamically from the conversation context rather than using a fixed list.
- **Slash-command shortcuts** — wire labels to `"/help"`, `"/clear"`, etc., so a new user can discover commands without typing them.
- **Multi-step routing** — instead of asking the user a free-form clarification, offer 2–3 enumerated branches the agent can switch on next turn.

### `data_artifact` — generic key-value (fallback)

Reach for this when the payload doesn't have a clean tabular shape: status blobs, counters, free-form metadata, ad-hoc structs.

```python
from fast_a2a_app import data_artifact

yield data_artifact({"count": 42, "ok": True, "tags": ["new", "verified"]},
                    text="Run finished:")
```

The renderer flattens one level and pretty-prints nested objects as compact JSON. If your payload is really tabular, prefer `table_artifact` — the table renderer handles wide data better.

### Adding a new typed widget

A typed widget has two halves, on opposite sides of the wire:

- **Python builder** — `src/fast_a2a_app/server/artifacts/<TAG>.py` — defines what the agent sends (the `_type` discriminator string and an optional convenience builder).
- **JavaScript renderer** — `src/fast_a2a_app/ui/renderers/<TAG>.js` — defines how the chat UI renders a data part whose `_type` matches.

The two files meet at the `_type` string only. They're independent: a `<TAG>.py` without a matching `<TAG>.js` is fine — the chat UI falls back to the generic key-value rendering of `data_artifact` for that tag. Likewise, a `<TAG>.js` without a `<TAG>.py` works if some agent emits `data_artifact({"_type": "<TAG>", ...})` by hand.

Use [TABLE.py](../src/fast_a2a_app/server/artifacts/TABLE.py) / [TABLE.js](../src/fast_a2a_app/ui/renderers/TABLE.js) and the `PROMPT_SUGGESTIONS` pair as templates.

**Python side** (`server/artifacts/TIMELINE.py`):

```python
from fast_a2a_app.server.artifacts import data_artifact

tag = "TIMELINE"

def timeline_artifact(events, *, caption=None, name="timeline"):
    return data_artifact(
        {"_type": tag, "events": list(events)},
        text=caption, name=name,
    )

builder = timeline_artifact
```

The package's autodiscover imports the module, exposes `timeline_artifact` at the package level (so you can `from fast_a2a_app.server.artifacts import timeline_artifact`), and registers `(tag, builder)` with `artifact_types`.

**JavaScript side** (`ui/renderers/TIMELINE.js`):

```javascript
window.A2A_RENDERERS = window.A2A_RENDERERS || {};
window.A2A_RENDERERS["TIMELINE"] = (value) => {
    const ul = el('ul', 'mt-2 space-y-1 text-sm text-slate-700');
    for (const e of (value.events || [])) {
        const li = el('li', '');
        li.textContent = `${e.when}: ${e.what}`;
        ul.appendChild(li);
    }
    return ul;
};
```

`build_a2a_ui` concatenates every `*.js` file from `ui/renderers/` into the served HTML at build time, so dropping a new file is enough — no edit to `index.html` or `route.py` required. The script runs in the bundled UI's scope and can call these globals:

- `el(tag, classes)` — DOM-creation helper.
- `sendSuggestion(prompt)` — submit a string as the next user message (used by the built-in `PROMPT_SUGGESTIONS` renderer).
- The chat's existing markdown / image / file rendering helpers if you need them.

**Tag conventions:**

- Uppercase snake-case (e.g. `TABLE`, `PROMPT_SUGGESTIONS`, `CRM_OPPORTUNITY`).
- Short generic names (`TABLE`, `CHART`) are framework-owned. Pick a domain prefix for application tags (`MYAPP_TIMELINE`) so two integrations can coexist without colliding.
- Re-registering an existing tag overrides the prior entry — useful for swapping the built-in `TABLE` renderer or builder for an app-specific one without forking the framework.

`build_a2a_ui` snapshots the renderers directory **at build time** — dropping a new `<TAG>.js` after the UI is built won't hot-reload. Restart the app (or call `build_a2a_ui()` again) to pick it up.

**Imperative registration** is still available for cases where adding a file to the framework directory isn't practical (third-party plugins, test fixtures):

```python
from fast_a2a_app import artifact_types

artifact_types.register("TIMELINE", builder=timeline_artifact)
```

That only handles the Python side. Custom renderers from outside the framework currently need to land in `ui/renderers/` (or you accept the generic key-value fallback).

---

## Lifecycle hooks

Run code before each task starts or when a task is cancelled — useful for metrics, per-task locks, or resetting state.

```python
async def on_task_start(task_id: str) -> None:
    metrics.increment("agent.task.started")

async def on_task_cancel(context_id: str, task_id: str) -> None:
    await release_lock(task_id)

build_a2a_app(
    agent_card=card,
    stream_invoke=build_stream_invoke(my_fn),
    on_task_start=on_task_start,
    on_task_cancel=on_task_cancel,
)
```

A third hook, `on_task_recover(task_id, task_store)`, fires when the framework detects a `TASK_STATE_WORKING` task whose worker has crashed — covered in [Durable agent execution → Crash recovery](#crash-recovery).

---

## Choosing a task store

`fast_a2a_app` ships four task-store backends — one module each under [`fast_a2a_app.server.task_stores`](../src/fast_a2a_app/server/task_stores/). Pick by deployment topology:

```python
from fast_a2a_app import (
    MemoryTaskStore, RedisTaskStore, MongoTaskStore, PostgresTaskStore,
    build_a2a_app,
)

# Default — single-process dev / tests / demos. No Docker required.
build_a2a_app(agent_card=card, stream_invoke=build_stream_invoke(my_fn))

# Redis — production multi-process / cross-instance cancel.
build_a2a_app(
    agent_card=card,
    stream_invoke=build_stream_invoke(my_fn),
    task_store=RedisTaskStore.from_url("redis://localhost:6379"),
)

# Mongo — production where Mongo is already deployed.
store = await MongoTaskStore.from_uri("mongodb://localhost:27017")
build_a2a_app(agent_card=card, stream_invoke=build_stream_invoke(my_fn),
              task_store=store)

# Postgres — production where Postgres is already deployed.
store = await PostgresTaskStore.from_dsn("postgresql://user:pw@localhost/fast_a2a")
build_a2a_app(agent_card=card, stream_invoke=build_stream_invoke(my_fn),
              task_store=store)
```

Every store logs an `INFO` line on initialization so the console makes it obvious which backend is running. `MemoryTaskStore` additionally warns about its single-process limitation — heed it if you scale beyond one process.

### Custom backend

Implement the `A2ATaskStore` Protocol against any datastore and pass it the same way:

```python
class MyDynamoTaskStore:
    async def save(self, task, context=None): ...
    async def get(self, task_id, context=None): ...
    async def delete(self, task_id, context=None): ...
    async def list(self, params, context=None): ...
    async def list_by_context(self, context_id, exclude_task_id=None): ...
    async def signal_cancel(self, task_id): ...
    async def is_cancel_signalled(self, task_id): ...

build_a2a_app(
    agent_card=card,
    stream_invoke=build_stream_invoke(my_fn),
    task_store=MyDynamoTaskStore(...),
)
```

Cross-instance cancel signals flow through the same Protocol, so a custom backend implementing `signal_cancel()` / `is_cancel_signalled()` handles cancellation on its own — no Redis dependency required.

---

## Durable agent execution

"Durable" means two different things, and the framework treats them as two layers you opt into independently:

1. **Surface durability** — the A2A task transcript (user messages, agent artifacts, progress log) survives crashes and restarts, and the UI never gets stuck on a `WORKING` task whose worker is gone. This is on by default and needs no extra code.
2. **Workflow durability** — the *in-flight* agent run resumes from the last completed step after a crash, so model calls and tool calls already paid for aren't repeated. This is opt-in via [DBOS](https://ai.pydantic.dev/durable-execution/dbos/).

The two layers compose: surface durability handles the protocol side regardless of whether the workflow itself is resumable.

### Default behaviour — surface durability, no extra config

Out of the box (any task store, including `MemoryTaskStore`), the framework already gives you:

- **Persisted transcript.** Every artifact and every `report_progress(...)` string is written to the task store as it happens. On reconnect, `SubscribeToTask` replays the log so the UI's spinner picks up where it left off.
- **Heartbeat-based crash detection.** The executor refreshes a per-task heartbeat every 5 seconds. A task that is still `TASK_STATE_WORKING` more than 30 s after its last heartbeat is treated as crashed.
- **Lazy crashed-task finalization.** Every `GetTask` / `SubscribeToTask` checks the heartbeat. Stale tasks are finalized as `TASK_STATE_FAILED` with `"Agent worker stopped unexpectedly. Please retry."`, so the UI sees a terminal state instead of hanging.
- **Cross-instance cancel.** `CancelTask` flows through the task store's `signal_cancel()` / `is_cancel_signalled()` so any replica can stop a task running on any other replica (`MemoryTaskStore` is single-process only — see [Choosing a task store](#choosing-a-task-store)).

What you do **not** get by default: the agent run itself is in-memory. If the worker process dies mid-turn, any model calls and tool calls already completed are wasted — the next attempt starts from scratch. For a 2-second chat reply that's fine. For a 30-second multi-tool plan, it isn't.

To also reconcile stale tasks **at boot** (before any client polls), call [`clean_up_stale_tasks`](api.md#clean_up_stale_tasks) from your lifespan startup:

```python
from contextlib import asynccontextmanager
from fast_a2a_app import PostgresTaskStore, clean_up_stale_tasks

@asynccontextmanager
async def lifespan(app):
    store = await PostgresTaskStore.from_dsn(POSTGRES_URL)
    await clean_up_stale_tasks(store)        # finalize anything left WORKING by the previous process
    try:
        yield
    finally:
        await store.close()
```

That's the whole default story — persistent backend + this one call covers "crashed worker, UI doesn't hang" without bringing in DBOS.

### Adding workflow durability with DBOS

When mid-run progress is *expensive enough that losing it hurts* — long tool chains, paid LLM calls, sandbox executions that take minutes — wrap the underlying pydantic-ai `Agent` with [`DBOSAgent`](https://ai.pydantic.dev/durable-execution/dbos/). Every model request and every tool call becomes a checkpointed step in Postgres; on process restart, in-flight workflows resume from the last completed step.

Four primitives plug DBOS into the A2A lifecycle:

| Primitive | What it does | Where it runs |
|---|---|---|
| `DBOSAgent(agent)` | Wraps a pydantic-ai `Agent`; checkpoints every step. | Inside `stream_invoke` / `invoke`. |
| `SetWorkflowID(task_id)` | Pins the DBOS workflow ID to the A2A `task_id` so the two share identity. | `with`-block around `.run()`. |
| [`on_task_cancel`](#lifecycle-hooks) | A2A user-pressed-Stop hook. Cancel the DBOS workflow so it isn't recovered later. | Triggered by the SDK. |
| [`on_task_recover`](#crash-recovery) + [`clean_up_stale_tasks`](#crash-recovery) | Crash-recovery hook + startup sweeper. Same primitives as the default flow, just doing more inside the hook. | App lifespan startup + lazy on `GetTask` / `SubscribeToTask`. |

#### Step 1 — install and configure DBOS

DBOS needs Postgres. Use the same database as `PostgresTaskStore`: DBOS owns the `dbos` schema, the task store owns bare `a2a_*` tables, they don't collide — and a shared Postgres means either *both* the workflow state and the task transcript survive a crash, or neither does (no split-brain).

```python
from dbos import DBOS, DBOSConfig

# Configure DBOS BEFORE constructing any DBOSAgent — its workflow
# registry only sees agents that exist at launch time.
DBOS(config=DBOSConfig({
    "name": "my_agent",
    "system_database_url": POSTGRES_URL,
}))
```

#### Step 2 — wrap the agent and pin the workflow ID

```python
from dbos import SetWorkflowID
from pydantic_ai import Agent
from pydantic_ai.durable_exec.dbos import DBOSAgent

# `name=` is the workflow identifier in the DBOS system database — keep it
# stable across redeploys so in-flight workflows survive a rolling restart.
my_agent = Agent(model=..., name="my_agent", end_strategy="exhaustive")
my_agent_durable = DBOSAgent(my_agent)

async def stream_invoke(prompt, context):
    # Pin the workflow ID to the A2A task_id so cancel/recover hooks can
    # target it by name. Without this, DBOS assigns a random UUID and
    # the hooks have nothing to address.
    with SetWorkflowID(context.task_id):
        result = await my_agent_durable.run(prompt, deps=deps)
    yield text_artifact(str(result.output))
```

#### Step 3 — launch / destroy DBOS in the lifespan

```python
from contextlib import asynccontextmanager
from dbos import DBOS

@asynccontextmanager
async def lifespan(_):
    DBOS.launch()              # resumes in-flight workflows, opens the system DB
    try:
        yield
    finally:
        DBOS.destroy(workflow_completion_timeout_sec=30)

app = FastAPI(lifespan=lifespan)
```

#### Step 4 — wire cancel and recover hooks

Without these, a Stop click leaves the DBOS workflow `PENDING` (and a future `DBOS.launch()` resumes a workflow whose client is long gone), and a crashed worker leaves the A2A task `WORKING` until a client polls.

```python
from a2a.types import TaskState
from dbos import DBOS
from fast_a2a_app import PostgresTaskStore, clean_up_stale_tasks

async def cancel_agent_workflow(context_id: str, task_id: str) -> None:
    if task_id:
        await DBOS.cancel_workflow_async(task_id)

async def recover_agent_workflow(task_id: str, task_store) -> None:
    # Workflow ID was pinned to task_id via SetWorkflowID — cancel it so a
    # fresh DBOS.launch() doesn't resume an orphan whose client is gone.
    # The framework then default-finalizes the A2A task as FAILED.
    await DBOS.cancel_workflow_async(task_id)

@asynccontextmanager
async def lifespan(app):
    DBOS.launch()
    store = await PostgresTaskStore.from_dsn(POSTGRES_URL)
    await clean_up_stale_tasks(store, on_task_recover=recover_agent_workflow)
    try:
        yield
    finally:
        await store.close()
        DBOS.destroy(workflow_completion_timeout_sec=30)

app.mount("/a2a", build_a2a_app(
    agent_card=card,
    stream_invoke=build_stream_invoke(stream_invoke),
    task_store=store,
    on_task_cancel=cancel_agent_workflow,
    on_task_recover=recover_agent_workflow,    # also fires lazily on GetTask/Subscribe
))
```

This is **Policy A — give up the in-flight task.** DBOS still gives you "no duplicate work" if the user re-asks the same question (a `result_cache` keyed by input hash reuses cached steps), but the original A2A task ends cleanly instead of a recovered DBOS workflow racing the user's retry. It's the right default for most apps.

#### Step 5 (optional) — Policy B: actively resume the task on a fresh worker

If discarding the task and asking the user to retry is *worse* than spending the compute to resume in the background, swap the recover hook for one that re-drives the agent end-to-end and writes its own terminal state. This needs [`bind_executor`](api.md#bind_executor) so `report_progress(...)` from inside the resumed run lands on the right task in the store and reaches any still-subscribed UI:

```python
from a2a.types import TaskState
from fast_a2a_app import bind_executor, report_progress

async def recover_agent_workflow(task_id: str, task_store) -> None:
    task = await task_store.get(task_id)
    if task is None:
        return
    prompt, context = await reconstruct_prompt_and_context(task)   # your call

    async with bind_executor(task_id, task_store):
        report_progress("Resuming after restart…")
        artifacts = []
        # DBOSAgent.run inside stream_invoke replays cached steps and
        # only re-executes the ones that hadn't checkpointed.
        async for chunk in stream_invoke(prompt, context):
            if not isinstance(chunk, str):
                artifacts.append(chunk)

    await task_store.finalize_task(
        task_id,
        state=TaskState.TASK_STATE_COMPLETED,
        artifacts=artifacts,
    )
```

Without `bind_executor`, a recovered run still completes (DBOS doesn't care about progress strings) but the UI sees nothing between "Resuming…" and the terminal event.

### Watching workflows live with dbos-argus

For local development, run [`dbos-argus`](https://github.com/tmarkovski/dbos-argus) as a read-only viewer over the DBOS Postgres tables:

```bash
uvx dbos-argus@latest --db-url "$POSTGRES_URL"
# open http://localhost:8090
```

It shows parent/child workflow trees, step status (`PENDING` / `SUCCESS` / `ERROR` / `CANCELLED`), retries, and completed/failed runs. No app integration is needed — Argus reads `dbos.workflow_status` directly. Pin the workflow ID to the A2A `task_id` (as the snippets above do) and you can paste a `task_id` straight from the chat UI into Argus's search.

### What is and isn't durable

With DBOS:

| Replayed on recovery | NOT replayed on recovery |
|---|---|
| Model requests (cached completions) | Tool side effects on `ctx.deps` (in-memory dataclass) |
| Tool return values | Files written to local disk during tool execution |
| The agent's final `result.output` | Streaming `report_progress(...)` events emitted before the crash |

Tools that mutate `ctx.deps` (e.g. appending to a `deps.generated` artifact list) WILL re-run their cached return values on recovery, but those mutations are lost — DBOS replays step *outputs*, not arbitrary in-memory state. Mitigation patterns:

1. **Return the data, don't mutate.** Have tools *return* the artifacts they want surfaced and collect them at the call site. The return value is what DBOS checkpoints.
2. **Layer a `result_cache`.** Key by a stable hash of the inputs (prompt + uploaded-file digests + parameters) so a repeat user query reproduces the artifacts cheaply, even from a brand-new process.
3. **Accept the degradation.** The recovered workflow still produces the same final text reply — just without the inline tables / charts the already-completed tools would have appended to `deps.generated`.

`report_progress` strings are deliberately *not* durable: they're UX, not state. If a worker crashes mid-run, the persisted log up to the crash is still replayed to any client that reconnects (that's the task store's job), but a *recovered* DBOS workflow doesn't re-emit them — use Policy B above with an explicit `report_progress("Resuming…")` if you want a UI heartbeat during the resume.

### When to use what

| Situation | Reach for |
|---|---|
| Local dev, demos, single-process. | Default (`MemoryTaskStore`). Crashes lose the task but you're restarting anyway. |
| Production chat: short turns, multi-replica, cheap to retry. | Persistent task store (`Redis` / `Mongo` / `Postgres`) + `clean_up_stale_tasks` in lifespan. No DBOS. |
| Long tool chains, expensive LLM calls, multi-minute sandbox runs — losing mid-run progress is painful but the user can re-ask. | DBOS + Policy A (cancel orphan workflow, let the framework finalize FAILED). |
| Background jobs the user can't easily retry (scheduled runs, queue-triggered work, agent-to-agent calls with no UI in the loop). | DBOS + Policy B (`on_task_recover` re-drives via `bind_executor` and finalizes COMPLETED). |

Start at the row that matches your deployment and only move down when a concrete failure mode bites. DBOS is great when you need it and overhead when you don't — a persistent task store alone already covers crash visibility for the protocol layer.

---

## Debug mode

Pass `debug=True` to `build_a2a_app` to surface full exception details in failure messages and inside the chat UI:

```python
build_a2a_app(
    agent_card=card,
    stream_invoke=build_stream_invoke(my_fn),
    debug=True,
)
```

Useful while iterating on tools, prompts, or multi-part artifacts. **Don't enable in production** — exception messages can leak internal paths and parameter values.

