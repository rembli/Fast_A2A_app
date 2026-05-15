---
name: fast-a2a-app
description: Build a production-ready AI agent with the fast_a2a_app library — a FastAPI-mounted A2A (Agent2Agent) JSON-RPC server with a built-in chat UI. Use when scaffolding, generating, or extending an AI agent that should expose A2A endpoints, ship a chat UI, support streaming, multi-turn history, tools, file uploads, or multi-part artifacts. Triggers on phrases like "build an agent", "A2A server", "fast_a2a_app", "add a chat UI", "expose my agent", or any request that combines an LLM/runtime with a FastAPI surface.
license: MIT
compatibility: Requires Python 3.11+ and FastAPI. The default in-process `MemoryTaskStore` needs no external service; for multi-process deployments swap in `RedisTaskStore` / `MongoTaskStore` / `PostgresTaskStore`. Built on a2a-sdk 1.0.x.
metadata:
  author: rembli
  homepage: https://github.com/rembli/fast_a2a_app
  version: "0.6.0"
allowed-tools: Read Write Edit Bash Grep Glob
---

# fast_a2a_app — Agent Builder Skill

A Starlette ASGI app that turns any Python coroutine into an A2A-compliant server with a built-in chat UI. Mounts into FastAPI; speaks A2A on the wire; calls *your* agent function on the inside; persists task state in a pluggable backend (memory by default; Redis / Mongo / Postgres for production). Wraps anything — pydantic-ai, LangChain, raw OpenAI/Anthropic SDKs, plain Python.

## When to invoke

- Scaffolding or extending an agent that should expose A2A (`/a2a/...`) and/or a chat UI (`/`).
- Wrapping an existing pydantic-ai / LangChain / raw-SDK agent for A2A.
- Adding streaming, multi-turn history, tools, file uploads, multi-part artifacts, or human-in-the-loop workflows.

Skip for: unrelated FastAPI work, plain LLM scripts with no chat surface, A2A *client* code.

---

## Hard rules — never violate

1. **Two callable shapes.** Streaming: `async def fn(prompt: str) -> AsyncIterable[str | Artifact]`. Non-streaming: `async def fn(prompt: str) -> str | Artifact`. Either may take an optional second positional `RequestContext` — auto-detected via `inspect`.
2. **Wrap before mounting.** `invoke=build_invoke(fn)` and/or `stream_invoke=build_stream_invoke(fn)`. The wrapper enables `report_progress()` and the `(prompt, context)` shape detection.
3. **Mount `/a2a` before `/`.** The UI is a catch-all and will swallow protocol traffic if mounted first. In production, gate the UI on `DEBUG`.
4. **Task store is pluggable, memory by default.** Omit `a2a_task_store` and an in-process `MemoryTaskStore` is used — zero infrastructure, single process only. For multi-process / cross-instance cancel, pass `a2a_task_store=RedisTaskStore.from_url(REDIS_URL)` (or `MongoTaskStore.from_uri(...)` / `PostgresTaskStore.from_dsn(...)`).
5. **Provide an `AgentCard`.** Required: `name`, `description`, `version`, `supported_interfaces=[AgentInterface(url=f"{APP_BASE_URL}/a2a/", protocol_binding="JSONRPC")]`, `capabilities`, `default_input_modes`, `default_output_modes`. Set `streaming=True` iff `stream_invoke` is provided. Add `skills=[AgentSkill(...)]` per capability.

---

## Public API

```python
from fast_a2a_app import (
    build_a2a_app, build_invoke, build_stream_invoke,
    text_artifact, data_artifact, file_artifact, image_artifact,
    prompt_suggestions_artifact, report_progress,
    get_user_input, get_task_history, format_history,
    a2a_ui, build_a2a_ui,
    MemoryTaskStore, RedisTaskStore, MongoTaskStore, PostgresTaskStore,
)
from a2a.types import (
    AgentCapabilities, AgentCard, AgentInterface, AgentSkill,
    Artifact, Part, Role,
)
from a2a.server.agent_execution import RequestContext   # only if fn takes context
```

`build_a2a_app(...)` knobs:

| Parameter           | Default                  | Set when                                                                |
|---------------------|--------------------------|-------------------------------------------------------------------------|
| `agent_card`        | required                 | always                                                                  |
| `invoke`            | `None`                   | non-streaming path                                                      |
| `stream_invoke`     | `None`                   | streaming path (recommended)                                            |
| `system_prompt`     | `None`                   | Level 1 — prepend persona/format guidance                               |
| `history_max_lines` | `12`                     | `0` = stateless or framework-managed history; tune for chat depth       |
| `prompt_builder`    | auto                     | Level 3 — full control (e.g. inject `[cid:UUID]`, RAG context)          |
| `on_task_start`     | `None`                   | metrics, locks                                                          |
| `on_task_cancel`    | `None`                   | cancel DBOS workflows, release locks (asyncio cancel is automatic)     |
| `a2a_task_store`    | `MemoryTaskStore`        | pass `RedisTaskStore.from_url(...)` / `MongoTaskStore.from_uri(...)` / `PostgresTaskStore.from_dsn(...)` for multi-process |
| `debug`             | `False`                  | dev only — surfaces tracebacks in UI failure messages                   |

**Artifacts:** `text_artifact(text)` (markdown), `data_artifact(dict)` (table; declare `_type` *inside* the dict), `file_artifact(content=… or url=…, filename, media_type)`, `image_artifact(image_bytes=… or url=…, caption=…)`, `prompt_suggestions_artifact([{label, prompt}, …])` (clickable pills). Prefer URL form for non-trivial files — the UI's `localStorage` quota is ~5–10 MB.

---

## Decision tree

```
Streaming?     → stream_invoke (yield strs + Artifacts)   else invoke
History?       → 0 = stateless · default = string-prefix · framework-managed = 0 + walk context.related_tasks yourself
Uploads?       → build_a2a_ui(file_upload_api="/path") + a POST endpoint returning {id,url,mediaType,filename}
Raw input?     → declare fn(prompt, context); read context.message.parts (uploads, slash commands, custom routing)
Multi-turn HITL?→ see "Multi-turn essentials" below
```

---

## Minimal scaffold

```python
# main.py
import os
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fast_a2a_app import a2a_ui, build_a2a_app, build_stream_invoke

load_dotenv()                           # MUST run before `from agent import ...`
from agent import stream_invoke         # noqa: E402

APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/health")
async def health(): return {"status": "ok"}

agent_card = AgentCard(
    name="My Agent", description="…", version="0.1.0",
    supported_interfaces=[AgentInterface(url=f"{APP_BASE_URL}/a2a/", protocol_binding="JSONRPC")],
    capabilities=AgentCapabilities(streaming=True),
    default_input_modes=["text"], default_output_modes=["text"],
    skills=[AgentSkill(id="...", name="...", description="...", tags=[])],
)

app.mount("/a2a", build_a2a_app(
    agent_card=agent_card,
    stream_invoke=build_stream_invoke(stream_invoke),
    # a2a_task_store=RedisTaskStore.from_url(REDIS_URL),  # uncomment for multi-process
))
app.mount("/", a2a_ui)
```

`agent.py` exports an `async def stream_invoke(prompt) -> AsyncIterable[str | Artifact]`. Build the body around your runtime — raw SDK, pydantic-ai `agent.run()`, etc. The default prompt builder prepends recent history, so the model sees prior turns without you replaying them.

### Variants
- **Multi-part output:** yield `text_artifact(...)`, `data_artifact(...)`, `file_artifact(...)`, `image_artifact(...)` from `stream_invoke`.
- **Quick-reply buttons:** yield `prompt_suggestions_artifact([{label, prompt}, …], text="…")`.
- **Image uploads:** add `POST /images` returning `{id,url,mediaType,filename}`; use `build_a2a_ui(file_upload_api="/images")`; in the agent declare `(prompt, context)` and read `context.message.parts`.
- **pydantic-ai with tools:** prefer `agent.run()` over `run_stream()` (the latter quits at the first text output, skipping later tool calls). When feeding `message_history` yourself, set `history_max_lines=0` so the default builder doesn't double-inject. Walk `context.related_tasks` to assemble messages — pull user text from `task.history` (role=ROLE_USER), agent text from `task.artifacts`, skip slash-command turns.

---

## Multi-turn / human-in-the-loop essentials

Required when the agent runs a workflow across turns with approval gates (plan → approve → execute → approve → assemble) or produces a downloadable file. Split into `main.py` + `agent.py` + `workflow.py` (Protocol + Redis impl + business class) + `config.py` + `prompts.yaml`.

The **mandatory techniques**:

1. **`end_strategy="exhaustive"`** on the main pydantic-ai `Agent`. Without it the loop stops at the first text output — often a mid-workflow status message, not the final reply.
2. **Same-turn guard** is the *only* mechanical enforcement of human approval. In `create_plan`, store `str(ctx.run_id)` next to the plan. In `confirm_plan`, reject when `str(ctx.run_id) == stored`. Both tools use `@agent.tool` (not `@agent.tool_plain`) to receive `RunContext`. Return an `"ERROR: …"` string with a recovery hint — the LLM reads it and waits for the next user turn.
3. **`context_id` injection** via a custom `prompt_builder` that appends ` [cid:{context.context_id}]` to the user input. In `main_system`, instruct the LLM: *"Each user message ends with `[cid:UUID]` — pass that UUID unchanged as the `context_id` argument to every tool that requires it."* Tools then namespace state by `context_id`.
4. **Dual Redis namespacing.** `fast_a2a_app` owns `a2a:*`; your workflow uses `{prefix}:{cid}:{field}` (e.g. `myagent:…`). Two clients, one Redis instance, distinct prefixes — never share `a2a:`.
5. **Workflow primitives.** Confirmation flag uses `SET NX` (atomic + idempotent). Reset on a new plan uses `DELETE` — never `SET "0"`, because `SET NX` then sees the key as present and breaks the gate forever. Per-step results live in a Redis HASH keyed by index so individual steps can be regenerated in place. 24-h TTL renewed on each write.
6. **Sub-agents with `output_type=<BaseModel>`** for any step needing typed output — pydantic-ai uses tool-use internally, so no regex/manual parsing. Share one `model = AnthropicModel(...)` instance across main + sub-agents to reuse the connection pool.
7. **`asyncio.wait_for(..., timeout=120.0)`** around every direct LLM call inside a tool. Converts a stalled API into a clean `TimeoutError`.
8. **Error-handling contract.** Validation, state guard, and corrupted-data failures `return f"ERROR: …. <recovery hint>."`. Unexpected exceptions: `log.exception(...)` then `raise` — the executor surfaces them as failed task events. Always include the recovery hint; that string is the LLM's only path back to a working state.
9. **Input length guards** (`MAX_REQUIREMENTS_LEN`, `MAX_CONTEXT_LEN`, `MAX_FEEDBACK_LEN`) at the top of every tool that accepts free text, before any LLM call.
10. **File output:** filenames as `{kind}_{YYYYMMDD_HHMMSS}_{uuid8}.{ext}`. `/download/{filename}` must `.resolve()` both paths and check `startswith` to reject path-traversal. Run a `_cleanup_loop()` as a `lifespan` background task to delete files older than 24 h.
11. **Cancellation** is automatic — `CancelledError` propagates from the executor through `agent.run()` and `asyncio.wait_for()`. Don't poll for cancellation in tool code.
12. **Production gating:** `/health` (liveness) + `/ready` (readiness); `allow_credentials=True` only when `ALLOWED_ORIGINS != ["*"]` (browsers reject the combo); `app.mount("/", a2a_ui)` wrapped in `if DEBUG:`.

The four-file split exists so each module has one job: `main.py` = composition root (no business logic); `agent.py` = agent + sub-agents + tools (module-level `_workflow` singleton); `workflow.py` = state persistence (Protocol + impl + business rules); `config.py` = env + prompt loading at import time (fail-fast). `prompts.yaml` keeps prompts editable without code changes.

---

## Pitfalls

1. Using the default `MemoryTaskStore` in a multi-worker deployment (uvicorn `--workers 2+`, gunicorn, or any horizontally-scaled setup). Tasks live in the worker that handled them — sibling workers see nothing, cross-instance cancel cannot work. Pass `a2a_task_store=RedisTaskStore.from_url(...)` (or Mongo / Postgres) for anything beyond a single process.
2. Mounting `/` before `/a2a` — the UI catch-all swallows protocol traffic.
3. Double history: leaving `history_max_lines=12` while *also* passing `message_history` to a pydantic-ai agent. Set it to `0` when feeding history yourself.
4. Passing a bare function to `build_a2a_app`. Always wrap with `build_invoke` / `build_stream_invoke`.
5. Calling `report_progress` from non-streaming `invoke` — it's a no-op there.
6. Returning bytes from a tool. Tools return strings; persist binaries and return a URL/id.
7. Hard-coding URLs in `AgentCard`. Use `APP_BASE_URL` env.
8. `load_dotenv()` after `from agent import …` — env vars read at agent module-import time will be empty.
9. `run_stream()` for tool-using pydantic-ai agents. Use `agent.run()`; `run_stream()` skips later tool calls.
10. Putting `_type` in the artifact `name`. It belongs *inside* the data dict.
11. no `asyncio.wait_for` around direct LLM calls — silent hangs.
12. `/files/{filename}` without path-traversal guard.
13. `allow_credentials=True` with `allow_origins=["*"]` — browsers reject it.
14. **Multi-turn:** missing `end_strategy="exhaustive"` — pydantic-ai stops at the first text output.
15. **Multi-turn:** no same-turn guard — a confident LLM bypasses user review.
16. **Multi-turn:** workflow keys colliding with `a2a:*`. Use a dedicated prefix.
17. **Multi-turn:** polling cancellation in tool code. `CancelledError` already propagates.

---

## Verification checklist

**All agents:**
- [ ] Deps include `fast_a2a_app`, `fastapi`, `uvicorn[standard]`, `python-dotenv`, the runtime, plus `pyyaml` for multi-turn.
- [ ] `load_dotenv()` runs before `from agent import …`.
- [ ] `/a2a` mounted before `/`.
- [ ] `AgentCard.supported_interfaces[0].url` ends with `/a2a/`; `streaming=True` ⇔ `stream_invoke` provided.
- [ ] `GET /health` exists. Multi-process deployments pass `a2a_task_store=RedisTaskStore.from_url(...)` (or Mongo / Postgres); single-process demos let the default `MemoryTaskStore` apply.
- [ ] If using a framework with its own message-history (pydantic-ai, etc.): `history_max_lines=0` AND a `_build_message_history(context)` adapter.
- [ ] Every tool that does I/O calls `report_progress(...)` at least once (streaming only).
- [ ] Boot test: server starts, `GET /a2a/.well-known/agent-card.json` returns the card, `/` loads the UI, hello message round-trips.

**Multi-turn / HITL agents (additional):**
- [ ] Files split into `main.py` / `agent.py` / `workflow.py` / `config.py`.
- [ ] Main `Agent(..., end_strategy="exhaustive")`.
- [ ] `create_plan` and `confirm_plan` use `@agent.tool` with `ctx: RunContext[None]`; same-turn guard rejects when `str(ctx.run_id) == stored`.
- [ ] Workflow keys use a non-`a2a:` prefix; reset uses `DELETE`; confirmation uses `SET NX`.
- [ ] Every direct LLM call wrapped in `asyncio.wait_for(..., timeout=120.0)`.
- [ ] Every user-text input length-capped at the top of its tool.
- [ ] Every `"ERROR: …"` return includes a recovery hint.
- [ ] `/files/{filename}` resolves both paths and rejects path-traversal; `_cleanup_loop` runs in `lifespan`.
- [ ] `/ready` exists alongside `/health`; CORS `allow_credentials` gated on explicit origins.

---

## Reference layouts

**Simple agent (default):**
```
my_agent/
├── main.py             # FastAPI app, AgentCard, mounts, /health, optional uploads
├── agent.py            # invoke / stream_invoke + tools (no FastAPI imports)
├── requirements.txt
└── .env.example
```

**Multi-turn / HITL agent:**
```
my_agent/
├── main.py             # FastAPI app, AgentCard, mounts, /health, optional uploads
├── agent.py            # invoke / stream_invoke + tools (no FastAPI imports)
├── workflow.py         # workflows (no FastAPI imports)
├── config.py           
├── requirements.txt
└── .env.example
```

Boot:
```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000   # open http://localhost:8000/
```
(For multi-process deployments add `docker run -d -p 6379:6379 redis:7-alpine` and pass `a2a_task_store=RedisTaskStore.from_url("redis://localhost:6379")` to `build_a2a_app`.)

Decide simple vs. multi-turn from the user's prompt, produce the files end-to-end, and verify against the matching checklist.
