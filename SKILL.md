---
name: FAST_A2A_APP
description: Build a production-ready AI agent with the fast_a2a_app library — a FastAPI-mounted A2A (Agent2Agent) JSON-RPC server with a built-in chat UI. Use when scaffolding, generating, or extending an AI agent that should expose A2A endpoints, ship a chat UI, support streaming, multi-turn history, tools, file uploads, or multi-part artifacts. Triggers on phrases like "build an agent", "A2A server", "fast_a2a_app", "add a chat UI", "expose my agent", or any request that combines an LLM/runtime with a FastAPI surface.
license: MIT
compatibility: Requires Python 3.11+ and FastAPI. The default in-process `MemoryTaskStore` needs no external service; for multi-process deployments swap in `RedisTaskStore` / `MongoTaskStore` / `PostgresTaskStore`. Built on a2a-sdk 1.0.x.
allowed-tools: Read Write Edit Bash Grep Glob
---

# fast_a2a_app — Agent Builder Skill

Turn any Python coroutine into an A2A-compliant server with a built-in chat UI. The library handles transport (JSON-RPC, SSE), discovery (`agent-card.json`), task lifecycle, multi-turn history, and cross-instance cancel. You write **one file — `agent.py`** — exporting an `AgentCard` plus an `invoke` / `stream_invoke` coroutine. `main.py` only wires it into FastAPI.

**Recommended framework: [pydantic-ai](https://ai.pydantic.dev/)** — typed tools, `RunContext` deps, `end_strategy="exhaustive"` for tool loops. The library is framework-agnostic (works with raw OpenAI/Anthropic SDKs, LangChain, plain Python), but every non-trivial example in this repo uses pydantic-ai.

---

## When to invoke

- Scaffolding or extending an agent that should expose A2A (`/a2a/...`) and/or a chat UI (`/`).
- Wrapping an existing pydantic-ai / LangChain / raw-SDK agent for A2A.
- Adding streaming, multi-turn history, tools, file uploads, multi-part artifacts, or HITL workflows.

Skip for: unrelated FastAPI work, plain LLM scripts with no chat surface, A2A *client* code.

---

## Before you scaffold — gather requirements

Don't open `agent.py` until the shape of the project is decided. The task store, history strategy, UI surface, and file-routing logic all fall out of a handful of upfront answers — guessing wrong means rewrites later. **Always propose a sensible default**, then let the user override with their own values. Bundle related questions through `AskUserQuestion` rather than firing all of them at once.

| # | Ask the user | Drives | Default to suggest |
|---|---|---|---|
| 1 | **Purpose & audience?** One sentence. | `AgentCard.description`, `AgentSkill` list, system prompt | — (must ask) |
| 2 | **Streaming or single-shot?** | `stream_invoke` vs `invoke`, `capabilities.streaming` | Streaming for any LLM-backed or tool-using agent |
| 3 | **Model / provider?** Azure OpenAI, OpenAI, Anthropic, local, …? | model client wiring; pydantic-ai vs raw SDK | Azure OpenAI + pydantic-ai unless the user has a constraint |
| 4 | **File uploads?** Which media types (CSV, image, PDF, none)? | `file_upload_api=`, `accepted_file_types=`, `extract_current_turn` — see [§File attachments](docs/how-to.md#file-attachments) | Off; opt in only when the use case needs it |
| 5 | **Runtime-configurable parameters?** Knobs to switch via chat (model variant, output size, style, verbosity, …)? | `CONFIG_PARAMETERS` + `/set` — see [§Configurable parameters via /set](docs/how-to.md#configurable-parameters-via-set) | Propose 2–3 parameters that fit the use case |
| 6 | **Multi-turn / HITL?** plan → approve → execute? | `workflow.py` split, same-turn guard, dedicated state prefix | Plain single-turn unless the user describes an approval flow |
| 7 | **Durable / crash-safe execution?** Must in-flight turns survive a restart? | `DBOSAgent` + Postgres — see [§Durable agent execution with DBOS](docs/how-to.md#durable-agent-execution-with-dbos) | Off; turn on for runs >30 s, expensive tool chains, or paid side-effects |
| 8 | **Task store?** Single-process or multi-process / cross-instance cancel? | `MemoryTaskStore` vs `Redis` / `Mongo` / `Postgres` — see [§Choosing a task store](docs/how-to.md#choosing-a-task-store) | **DBOS on → Postgres (mandatory).** Otherwise: `Memory` for dev; ask the user to pick `Redis` / `Mongo` / `Memory` for prod |
| 9 | **Custom UI widgets?** Anything beyond `TEXT` / `TABLE` / `MAP` / images / files / `PROMPT_SUGGESTIONS`? | new `<TAG>.py` + `<TAG>.js` pair — see [§Adding a new typed widget](docs/how-to.md#adding-a-new-typed-widget) | Reuse existing widgets; only add a custom one for a genuinely novel shape |
| 10 | **Conversation history strategy?** | `history_max_lines` + optional `_build_message_history` adapter — see [§Using the RequestContext](docs/how-to.md#using-the-requestcontext) | pydantic-ai → `history_max_lines=0` + adapter; raw SDK → default 12-line prefix |

The list above is the common set — extra questions (auth, observability, rate-limits, cost ceilings) may surface from the brief. If the user gives a vague one-liner ("a CSV chatbot"), propose a fully-defaulted plan and confirm before writing code.

**Hard rule:** when durable execution is required, `task_store` is locked to `PostgresTaskStore` — one Postgres covers both DBOS and the A2A store. Don't offer Redis / Mongo / Memory in that case.

---

## The agent.py contract

Every agent exports **two top-level names**:

1. `agent_card: AgentCard` — public metadata served at `/.well-known/agent-card.json`.
2. `invoke` and/or `stream_invoke` — the coroutine(s) the framework calls per turn.

### Callable shapes (pick one or both)

```python
# Non-streaming — returns a single string or Artifact.
async def invoke(prompt: str) -> str | Artifact: ...

# Streaming — yields text chunks and/or Artifacts.
async def stream_invoke(prompt: str) -> AsyncIterable[str | Artifact]: ...
```

Either shape may take an **optional second positional `RequestContext`** parameter — auto-detected via `inspect`. Use it when you need raw `context.message.parts` (uploads, slash commands) or `context.related_tasks` (conversation history).

```python
async def stream_invoke(prompt: str, context: RequestContext) -> AsyncIterable[str | Artifact]: ...
```

`main.py` wraps the function with `build_invoke(fn)` / `build_stream_invoke(fn)` before mounting — that's what enables `report_progress()` and shape detection.

---

## AgentCard — required and always the same shape

Reference from [examples/echo-multipart/agent.py](examples/echo-multipart/agent.py):

```python
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")

agent_card = AgentCard(
    name="Echo Multipart Agent",
    description="Streams your message back as three separate messages.",
    version="0.1.0",
    supported_interfaces=[
        AgentInterface(url=f"{APP_BASE_URL}/a2a/", protocol_binding="JSONRPC"),
    ],
    capabilities=AgentCapabilities(streaming=True),       # True iff stream_invoke is defined
    default_input_modes=["text"],                          # add "image" if you accept uploads
    default_output_modes=["text"],                         # add "data" for tables/maps, "image" for image_artifact
    skills=[
        AgentSkill(
            id="echo_multipart",
            name="Echo Multipart",
            description="Streams three separate messages: echo, stats, and uppercased.",
            tags=["demo", "streaming"],
        ),
    ],
)
```

**Rules:**
- `supported_interfaces[0].url` must end with **`/a2a/`** (trailing slash) and use `APP_BASE_URL` from env — never hard-code.
- `capabilities.streaming=True` ⇔ `stream_invoke` is provided.
- `default_input_modes` / `default_output_modes` must list every modality you actually use (`text`, `image`, `data`, `file`).
- One `AgentSkill` per user-visible capability — `id` is the stable handle, `name`/`description` show in discovery clients, `tags` are free-form.

---

## Recommended: pydantic-ai agent shape

The pattern used by [holiday-planner](examples/holiday-planner/agent.py) and [image-creator](examples/image-creator/agent.py):

```python
from dataclasses import dataclass, field
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from fast_a2a_app import report_progress, text_artifact, prompt_suggestions_artifact

@dataclass
class AgentDeps:
    """Per-turn state passed to tools via RunContext[AgentDeps]."""
    generated: list[Artifact] = field(default_factory=list)   # tools push rich artifacts here

_model = OpenAIModel(AZURE_AI_DEPLOYMENT_NAME, provider=OpenAIProvider(openai_client=_client))

my_agent: Agent[AgentDeps] = Agent(
    model=_model,
    deps_type=AgentDeps,
    system_prompt="You are …",
    end_strategy="exhaustive",   # MUST — without this the loop stops at the first text output
)

@my_agent.tool
async def my_tool(ctx: RunContext[AgentDeps], arg: str) -> str:
    """User-/LLM-facing docstring — pydantic-ai feeds this to the model for tool selection."""
    report_progress(f"Doing {arg}…")           # live status in the chat UI
    result = await do_work(arg)
    ctx.deps.generated.append(text_artifact(result))   # rich artifact alongside the text reply
    return result                                       # what the model sees

async def stream_invoke(prompt: str, context: RequestContext) -> AsyncIterable[str | Artifact]:
    deps = AgentDeps()
    result = await my_agent.run(                # NOT run_stream — see pitfall #4
        prompt, deps=deps,
        message_history=_build_message_history(context),
    )
    for art in deps.generated:                  # rich artifacts first…
        yield art
    if text := str(result.output or "").strip():
        yield text_artifact(text)               # …then the closing text
```

**Key choices:**
- `@my_agent.tool` for tools that need `RunContext` (deps, run_id); `@my_agent.tool_plain` for stateless ones.
- Push rich artifacts into `deps.generated` rather than returning them — tools must return **strings** the model can read.
- Call `report_progress(...)` at the start of any I/O-bound tool — the chat UI shows it live.

---

## Artifacts — multi-part output

```python
from fast_a2a_app import (
    text_artifact, data_artifact, file_artifact, image_artifact,
    prompt_suggestions_artifact, report_progress,
)
```

| Helper                            | Use for                                                                            |
|-----------------------------------|------------------------------------------------------------------------------------|
| `text_artifact(text)`             | Markdown reply.                                                                    |
| `data_artifact(dict)`             | Key-value tables / typed widgets. **Declare `_type` *inside* the dict** (TABLE, MAP, custom). |
| `file_artifact(content=… or url=…, filename, media_type)` | Downloadables. Prefer `url=` for non-trivial files (UI localStorage is ~5–10 MB). |
| `image_artifact(image_bytes=… or url=…, caption=…)`       | Inline images.                                                                     |
| `prompt_suggestions_artifact([{label, prompt}, …], text="…")` | Clickable quick-reply pills.                                                       |
| `report_progress(msg)`            | Live status during streaming (no-op in non-streaming `invoke`).                    |

---

## Conversation history with pydantic-ai

The default prompt builder prepends recent history as a string — fine for raw-SDK agents. **For pydantic-ai, set `history_max_lines=0` in `main.py` and feed history yourself** as typed `ModelMessage`s. Pattern from [examples/holiday-planner/agent.py:160](examples/holiday-planner/agent.py#L160):

```python
from a2a.types import Role
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart

def _build_message_history(context: RequestContext) -> list[ModelMessage]:
    messages: list[ModelMessage] = []
    for task in getattr(context, "related_tasks", None) or []:
        # User text from task.history (role=ROLE_USER).
        user_text = next((
            "\n".join(p.text for p in (msg.parts or []) if p.WhichOneof("content") == "text" and p.text).strip()
            for msg in (task.history or []) if msg.role == Role.ROLE_USER
        ), "")
        # Agent text from task.artifacts (NOT task.history — that includes report_progress noise).
        agent_text = "\n".join(
            p.text
            for art in (task.artifacts or [])
            for p in (art.parts or [])
            if p.WhichOneof("content") == "text" and p.text
        ).strip()
        if user_text:  messages.append(ModelRequest(parts=[UserPromptPart(content=user_text)]))
        if agent_text: messages.append(ModelResponse(parts=[TextPart(content=agent_text)]))
    return messages[-16:]   # cap so token spend stays predictable
```

Then in `stream_invoke`: `await my_agent.run(prompt, deps=deps, message_history=_build_message_history(context))`.

---

## File uploads via `commons.uploads`

For agents that accept uploads, three helpers in [`fast_a2a_app.server.commons.uploads`](src/fast_a2a_app/server/commons/uploads.py) replace the boilerplate walkers over `context.message.parts` (current turn) and `context.related_tasks` (prior turns). They're storage-agnostic — you pass a `resolver(url)` adapter for your store (S3, Bynder, local disk) and an optional `predicate(bytes, mime)` filter.

```python
from fast_a2a_app.server.commons.uploads import (
    extract_current_turn,         # walks context.message.parts → (text, files)
    latest_file_in_history,       # walks context.related_tasks → most recent file
    resolve_file_part,            # one Part → (bytes, mime) | None
)

def _resolve_url(url: str):
    blob_id = image_store.id_from_url(url)
    return image_store.get(blob_id) if blob_id else None   # → (bytes, mime) | None

def _is_image(_data, mime): return mime.startswith("image/")

async def stream_invoke(prompt: str, context: RequestContext):
    user_text, uploads = extract_current_turn(
        context, resolver=_resolve_url, predicate=_is_image,
    )
    # "make it warmer" with no fresh upload → fall back to the most recent file
    # in conversation history (user upload OR agent-produced).
    previous = latest_file_in_history(
        context, resolver=_resolve_url, predicate=_is_image,
    ) if not uploads else None
```

| Helper | Returns | Walks |
|---|---|---|
| `resolve_file_part(part, resolver=…)`             | `(bytes, mime) \| None`              | A single `Part` — inline `raw`, or `url` via your resolver.                                                |
| `extract_current_turn(context, resolver=…, predicate=…)`  | `(text, [(bytes, mime), …])` | `context.message.parts` — untouched current turn (slash-command routing + per-turn upload handling). |
| `latest_file_in_history(context, resolver=…, predicate=…)`| `(bytes, mime) \| None`      | `context.related_tasks` — both user-side history and agent-side artifacts; last match wins. |

Pair with `file_upload_api="/uploads"` on `build_a2a_ui` (paperclip is opt-in) and `accepted_file_types=…` if you want to restrict the picker. Full walkthrough in [docs/how-to.md → File attachments](docs/how-to.md#file-attachments).

---

## Configurable parameters via `/set`

When you want the user to switch knobs at runtime (model, output size, style, verbosity, …) without redeploying: declare a **single schema dict in `agent.py`** and wire it through the four helpers in [`fast_a2a_app.server.commons.config`](src/fast_a2a_app/server/commons/config.py). Active values are recovered from A2A history each turn — no server-side store. Implemented end-to-end in [examples/image-creator/agent.py](examples/image-creator/agent.py).

```python
# agent.py
from fast_a2a_app.server.commons.config import (
    ACTIVE_SETTINGS_CONVENTION,
    format_active_settings,
    handle_set_command,
    resolve_config_from_history,
)

CONFIG_PARAMETERS: dict[str, dict] = {
    "model": {"description": "Image deployment.", "default": "gpt-image-1-mini",
              "values": {"gpt-image-1-mini": "Faster.", "gpt-image-1": "Higher fidelity."}},
    "size":  {"description": "Output dimensions.", "default": "1024x1024",
              "values": {"1024x1024": "Square.", "1024x1536": "Portrait."}},
}

SYSTEM_PROMPT = "You are an image-creation agent.\n\n" + ACTIVE_SETTINGS_CONVENTION

async def stream_invoke(prompt: str, context: RequestContext):
    user_text = context.get_user_input()
    config = resolve_config_from_history(context, CONFIG_PARAMETERS)
    if reply := handle_set_command(user_text, config, CONFIG_PARAMETERS):
        yield reply                                # /set wizard short-circuits the agent loop
        return
    prompt_ext = f"{prompt}\n\n{format_active_settings(config, CONFIG_PARAMETERS)}"
    deps = AgentDeps(config=config)
    result = await my_agent.run(prompt_ext, deps=deps)
    yield text_artifact(str(result.output))

@my_agent.tool
async def generate_image(ctx: RunContext[AgentDeps], prompt: str) -> str:
    model = ctx.deps.config["model"]               # direct path — bypasses the LLM
    ...
```

Four helpers cover the whole pattern:

| Helper | Purpose |
|---|---|
| `resolve_config_from_history(context, schema)` | Walks `related_tasks` for past `/set <param> <value>` user messages; returns the resolved snapshot. Latest valid assignment wins; unset → schema default. |
| `handle_set_command(user_text, config, schema)` | Returns the pill-wizard Artifact for `/set`, `/set <param>`, `/set <param> <value>`, `/set cancel`, plus validation errors. Returns `None` for non-`/set` text — caller falls through. |
| `format_active_settings(config, schema)`        | Renders the `Active settings:\n- name: value (default)` block to append to the user prompt. |
| `ACTIVE_SETTINGS_CONVENTION`                    | Drop-in preamble for your system prompt teaching the LLM what the trailing block means. |

Expose the schema verbatim at `GET /config` from `main.py`:

```python
from agent import CONFIG_PARAMETERS

@app.get("/config", tags=["ops"])
async def get_config() -> dict:
    return CONFIG_PARAMETERS
```

**Both wiring paths matter:** `ctx.deps.config` gets values to your tools without LLM round-tripping; `format_active_settings` + `ACTIVE_SETTINGS_CONVENTION` make sure the orchestrator's textual replies acknowledge them. Skip either and behaviour drifts.

If you ship extra slash commands (`/help`, `/clear`), keep them in user code and call `handle_set_command` as a fall-through.

Full walkthrough in [docs/how-to.md → Configurable parameters via `/set`](docs/how-to.md#configurable-parameters-via-set).

---

## Hard rules — never violate

1. **Two callable shapes only.** Streaming: `async fn(prompt) -> AsyncIterable[str | Artifact]`. Non-streaming: `async fn(prompt) -> str | Artifact`. Either may take a second positional `RequestContext`.
2. **Provide an `AgentCard`.** All fields above are required; `supported_interfaces[0].url` ends with `/a2a/`; `streaming=True` ⇔ `stream_invoke` provided.
3. **No FastAPI imports in `agent.py`.** Keep transport in `main.py`. Agents must be reusable across surfaces.
4. **`pydantic-ai` agents use `agent.run()`, not `run_stream()`.** `run_stream()` quits at the first text output and skips later tool calls.
5. **`pydantic-ai` agents set `end_strategy="exhaustive"`.** Otherwise the loop stops at the first text output — usually a mid-workflow status message.
6. **Tools return strings, not bytes.** Persist binaries and return a URL/id (see `image_store` in [examples/image-creator/agent.py:262-281](examples/image-creator/agent.py#L262-L281)).
7. **`_type` lives inside the data dict**, not in the artifact `name`. `data_artifact({"_type": "TABLE", "rows": …})`, never `data_artifact({...}, name="TABLE")`.
8. **`load_dotenv()` before `from agent import …`** (in `main.py`). Env vars read at module-import time will otherwise be empty.

---

## Minimal scaffold

### `agent.py` (the file you actually write)
```python
from __future__ import annotations
import os
from collections.abc import AsyncIterable
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")

agent_card = AgentCard(
    name="My Agent", description="…", version="0.1.0",
    supported_interfaces=[AgentInterface(url=f"{APP_BASE_URL}/a2a/", protocol_binding="JSONRPC")],
    capabilities=AgentCapabilities(streaming=True),
    default_input_modes=["text"], default_output_modes=["text"],
    skills=[AgentSkill(id="chat", name="Chat", description="…", tags=[])],
)

async def stream_invoke(prompt: str) -> AsyncIterable[str]:
    # plug your pydantic-ai / OpenAI / Anthropic / etc. runtime here
    yield "hello"
```

### `main.py` (boilerplate — same for every agent)
```python
import os
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fast_a2a_app import a2a_ui, build_a2a_app, build_stream_invoke

load_dotenv()                                # MUST run before importing the agent
from agent import agent_card, stream_invoke  # noqa: E402

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/health")
async def health(): return {"status": "ok"}

app.mount("/a2a", build_a2a_app(
    agent_card=agent_card,
    stream_invoke=build_stream_invoke(stream_invoke),
    history_max_lines=0,                     # set 0 when feeding pydantic-ai history yourself
    # task_store=RedisTaskStore.from_url(REDIS_URL),   # multi-process only
))
app.mount("/", a2a_ui)                       # MUST be mounted AFTER /a2a
```

Boot: `poetry install && poetry run uvicorn main:app --reload --port 8000` → open <http://localhost:8000/>.

---

## `build_a2a_app` knobs (set in main.py)

| Parameter           | Default              | When to set                                                          |
|---------------------|----------------------|----------------------------------------------------------------------|
| `agent_card`        | required             | always                                                               |
| `invoke`            | `None`               | non-streaming path                                                   |
| `stream_invoke`     | `None`               | streaming path (recommended)                                         |
| `system_prompt`     | `None`               | prepend persona/format guidance (default prompt builder only)        |
| `history_max_lines` | `12`                 | `0` when framework manages history (e.g. pydantic-ai message_history)|
| `prompt_builder`    | auto                 | full control (inject `[cid:UUID]`, RAG context, custom prefix)       |
| `on_task_start`     | `None`               | metrics, locks                                                       |
| `on_task_cancel`    | `None`               | cancel external workflows (asyncio cancel is automatic)              |
| `task_store`        | `MemoryTaskStore`    | pass `RedisTaskStore.from_url(...)` / `Mongo` / `Postgres` for multi-process |
| `debug`             | `False`              | dev only — surfaces tracebacks in UI failure messages                |

---

## Multi-turn / HITL essentials

Required when the agent runs a workflow across turns with approval gates (plan → approve → execute → approve → assemble). Split into `main.py` + `agent.py` + `workflow.py` + `config.py` + `prompts.yaml`.

The five mandatory techniques:

1. **`end_strategy="exhaustive"`** on the main `Agent`.
2. **Same-turn guard** via `RunContext.run_id` — the *only* mechanical enforcement of human approval. In `create_plan`, store `str(ctx.run_id)`; in `confirm_plan`, reject when it matches. Both tools use `@agent.tool` to receive `RunContext`. Return `"ERROR: <recovery hint>"` strings the LLM can read.
3. **`context_id` injection** via custom `prompt_builder` appending ` [cid:{context.context_id}]` to user input. System-prompt the LLM to forward the UUID as a tool argument. Workflow state then namespaces by `context_id`.
4. **Dedicated Redis prefix** — `fast_a2a_app` owns `a2a:*`; your workflow uses `myagent:{cid}:{field}`. Reset uses `DELETE` (not `SET "0"`, which breaks `SET NX` gates). Confirmation flag uses `SET NX`.
5. **`asyncio.wait_for(..., timeout=120.0)`** around every direct LLM call inside a tool. Length-cap every free-text input at the top of the tool. Every error path returns `"ERROR: … <recovery hint>."` — the LLM reads it and waits for the next user turn.

Also: sub-agents with `output_type=<BaseModel>` for typed outputs; share one model instance across main + sub-agents; `/health` + `/ready` endpoints; `app.mount("/", a2a_ui)` gated on `if DEBUG:` in production.

---

## Pitfalls

1. Default `MemoryTaskStore` in a multi-worker deployment — tasks invisible across workers. Use Redis/Mongo/Postgres.
2. Mounting `/` before `/a2a` — the UI catch-all swallows protocol traffic.
3. Double history: `history_max_lines>0` while also passing `message_history` to pydantic-ai. Set it to `0`.
4. `agent.run_stream()` with tool-using agents — skips later tool calls. Use `agent.run()`.
5. Missing `end_strategy="exhaustive"` — agent stops at first text output.
6. Tools returning bytes — return a URL/id, persist binaries.
7. Hard-coded URLs in `AgentCard` — use `APP_BASE_URL` env.
8. `load_dotenv()` after `from agent import …` — env empty at import time.
9. `_type` in the artifact `name` instead of inside the data dict — typed widget renderers (TABLE / MAP / etc.) won't pick it up.
10. `report_progress` in non-streaming `invoke` — silent no-op.
11. No `asyncio.wait_for` around direct LLM calls in tools — silent hangs.
12. `/files/{filename}` without path-traversal guard (`.resolve()` + `startswith` check).
13. `allow_credentials=True` with `allow_origins=["*"]` — browsers reject the combo.
14. **Multi-turn:** missing same-turn guard — confident LLM bypasses user review.
15. **Multi-turn:** workflow keys colliding with `a2a:*` — use a dedicated prefix.
16. **`/set` parameters wired only into tool deps** — the orchestrator LLM never sees the active values, so its textual replies ignore them and it may re-state them as tool arguments. Always append `format_active_settings(config, schema)` to the prompt fed into `agent.run()` *and* include `ACTIVE_SETTINGS_CONVENTION` in the system prompt — passing values through `RunContext` alone isn't enough.
17. **Hand-rolling `/set` regex or upload walkers** — use the helpers in [`fast_a2a_app.server.commons.config`](src/fast_a2a_app/server/commons/config.py) and [`fast_a2a_app.server.commons.uploads`](src/fast_a2a_app/server/commons/uploads.py). The hand-rolled versions miss edge cases (case-insensitive `/SET`, `cancel` pill, URL-part fallback for re-sent uploads).

---

## Verification checklist

- [ ] `pyproject.toml` includes `fast_a2a_app`, `fastapi`, `uvicorn[standard]`, `python-dotenv`, the runtime (e.g. `pydantic-ai`), plus `pyyaml` for multi-turn.
- [ ] `load_dotenv()` runs before `from agent import …`.
- [ ] `/a2a` mounted before `/`.
- [ ] `AgentCard.supported_interfaces[0].url` ends with `/a2a/`; `streaming=True` ⇔ `stream_invoke` provided; all used modalities in `default_input_modes` / `default_output_modes`.
- [ ] `agent.py` has no FastAPI imports.
- [ ] pydantic-ai agents use `Agent(... end_strategy="exhaustive")` and `agent.run()` (not `run_stream()`); `history_max_lines=0` with a `_build_message_history(context)` adapter.
- [ ] Every I/O-bound tool calls `report_progress(...)` at least once.
- [ ] `GET /health` exists. Multi-process deployments pass `task_store=…`.
- [ ] **Multi-turn additional:** files split (`main.py` / `agent.py` / `workflow.py` / `config.py`); same-turn guard via `RunContext.run_id`; dedicated Redis prefix; `asyncio.wait_for(..., timeout=120.0)` around LLM calls; length-capped tool inputs; every error path returns a recovery hint; `/ready` alongside `/health`.
- [ ] Boot test: server starts, `GET /a2a/.well-known/agent-card.json` returns the card, `/` loads the UI, hello round-trips.

---

## Reference examples

In-repo, ordered by complexity:

- [examples/echo-agent](examples/echo-agent/agent.py) — pure Python, no LLM. Two callable shapes side by side.
- [examples/echo-multipart](examples/echo-multipart/agent.py) — yields `text_artifact` + `data_artifact` + `file_artifact` from one turn.
- [examples/joke-agent](examples/joke-agent/agent.py) — raw Azure OpenAI SDK, no agent framework.
- [examples/holiday-planner](examples/holiday-planner/agent.py) — full pydantic-ai pattern: `Agent[AgentDeps]`, `@tool`/`@tool_plain`, `MAP` artifact, message-history adapter.
- [examples/image-creator](examples/image-creator/agent.py) — pydantic-ai + uploads + slash commands + `RequestContext` parsing + multi-step tool plans.

In-repo examples share the parent project's `pyproject.toml` — run `poetry install` at the repo root, then `cd examples/<name> && poetry run uvicorn main:app --reload`.

Gather requirements first (see § Before you scaffold), lean on pydantic-ai for anything non-trivial, produce `agent.py` + `main.py` end-to-end, and verify against the checklist.
