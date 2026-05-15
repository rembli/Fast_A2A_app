# Design choices

This document explains the philosophy behind fast_a2a_app — why it exists alongside other A2A integrations, what it deliberately is and isn't, and the trade-offs behind each decision.

## The bet on A2A

The library is built on a single bet: **agent-to-agent communication will standardise on A2A** the same way machine-to-machine RPC standardised on HTTP+JSON. Without a shared protocol, every integration between two agents is bespoke; with one, the agent ecosystem becomes composable — clients, orchestrators, marketplaces, and other agents can all talk to any agent that speaks the protocol.

If that bet is right, the bottleneck becomes adoption: how easy is it for a working Python agent to *become* a fully spec-compliant A2A server? fast_a2a_app exists to make that path as short as possible.

## Mount point, not a framework

fast_a2a_app is a **plain Starlette ASGI app you mount into a FastAPI application at a path prefix**. It is not a replacement framework or a process supervisor. Everything outside the mounted prefix — authentication middleware, custom routes, dependency injection, observability, health checks, rate limiting — stays in your application and is unchanged.

The boundary is intentional. Production agent applications need real auth, real CORS rules, real metrics. Trying to bake those into a protocol library makes it opinionated in places where opinions don't generalise. Mounting keeps protocol concerns and application concerns cleanly separated.

## Framework-agnostic by default

The library has **zero dependency on any agent framework**. `build_a2a_app` accepts two plain callables:

| Callable | Signature |
|---|---|
| `invoke` | `async (prompt: str) -> str` *or* `async (prompt: str) -> Artifact` |
| `stream_invoke` | `async (prompt: str) -> AsyncIterable[str]` |

Wrap any agent runtime — raw Anthropic/OpenAI calls, LangChain, LlamaIndex, Pydantic AI, custom Python — in those two signatures and you're done.

This is a deliberate departure from libraries like Pydantic AI's own [`FastA2A`](https://ai.pydantic.dev/), which is excellent if you're already inside the Pydantic AI ecosystem. fast_a2a_app targets the case where you *aren't* — or where you want the freedom to switch frameworks without rewriting the protocol layer.

## Progressive disclosure for prompt management

The default prompt builder injects the last 12 lines of conversation history before the user message. Most agents need exactly that and nothing else. But "most" isn't "all," so the API exposes four levels of control:

1. **Zero-config** — works out of the box.
2. **Keyword params** — `system_prompt=` and `history_max_lines=` for tuning without code.
3. **Helper composition** — `format_history` + `get_task_history` + `get_user_input` to assemble a custom prompt from primitives.
4. **Full custom builder** — pass any `(RequestContext) -> str` for complete control.

You only pay the complexity of the level you actually use. See [How-to → Prompt management](how-to.md#prompt-management) for examples.

## Pluggable task store, memory by default

All server-side state — tasks, context indices, cancel signals — lives behind the `A2ATaskStore` Protocol. Four backends ship in `fast_a2a_app.server.task_stores`, one module per backend:

| Backend | Class | When to reach for it |
|---|---|---|
| In-process dicts | `MemoryTaskStore` (default) | Dev, tests, and demos. Boots without any external service — no Docker, no cloud provisioning. Single process only. |
| Redis | `RedisTaskStore` | Production deployments where Redis is acceptable. Native TTL, horizontal scale, cross-instance cancel via short-TTL keys. |
| MongoDB | `MongoTaskStore` | Production where Mongo is already the operational data store. TTL indexes do the housekeeping server-side. |
| Postgres | `PostgresTaskStore` | Production where Postgres is already the operational data store. `expires_at` columns + read-time filtering. |

The default matters: omitting `task_store` from `build_a2a_app` gives you `MemoryTaskStore`, which means a hello-world agent boots with `uvicorn main:app` — no `docker run redis` step in the README. The trade-off is explicit: memory tasks do not survive restarts and cannot be shared across worker processes, so every store logs an `INFO` line on initialization that names the backend, and `MemoryTaskStore` additionally warns about its single-process limitation. Production users see exactly which backend is live on the very first console line.

Switching backends is one constructor call away (`task_store=RedisTaskStore.from_url(...)`, or the Mongo/Postgres equivalents). Custom backends — DynamoDB, FoundationDB, an in-house service — plug in the same way by implementing the `A2ATaskStore` Protocol. Task persistence *and* cancel signalling go through that one Protocol, so a custom backend implements `signal_cancel()` / `is_cancel_signalled()` however it likes.

## Self-contained UI, no build step

The chat UI is a single static HTML file with vanilla JavaScript — no React, no npm, no bundler. This is a deliberate trade-off: a richer UI framework would let us write more idiomatic code, but at the cost of making the library harder to install and slower to boot.

Because the UI is just a static file, it's easy to:
- Mount at any path prefix.
- Replace entirely with your own SPA — the wire protocol is documented A2A, so any client can speak it.
- Read or fork — it's a single file you can paste into a browser to inspect.

## Typed artifacts as an extension surface

The A2A protocol supports a `data` part for any JSON-shaped payload. fast_a2a_app turns that into a real extension point by reserving a `_type` discriminator on the dict and routing matching payloads to specialised renderers — a real `<table>` for tabular data, a Leaflet map for geographic data, clickable pills for follow-up suggestions, etc. Anything without a recognised `_type` falls through to a generic key-value block, so untyped `data_artifact` payloads still display.

Three constraints shape the API:

1. **The split between Python and JS is enforced by file system, not class hierarchy.** Each typed widget is a `<TAG>.py` next to `<TAG>.js`. The Python side declares `tag` + `builder`; the JS side registers a renderer into `window.A2A_RENDERERS["TAG"]`. They meet at the discriminator string — neither side knows about the other directly.
2. **Both halves are optional.** A `<TAG>.py` with no matching `<TAG>.js` falls through to the generic renderer; a `<TAG>.js` with no Python counterpart works if some agent emits `data_artifact({"_type": "TAG", ...})` by hand. This lets contributors land one half of a widget at a time.
3. **Adding a typed widget never touches the registry or the UI shell.** Autodiscover walks both directories at import time. Drop a file in, restart, done. The cost of taking opinions on widget shape is paid once by the framework, not by every new integration.

The trade-off this avoids: a single monolithic `widgets.py` that knows about every typed widget. That works for a fixed set; it doesn't work once applications start adding their own (`CRM_OPPORTUNITY`, `LIVE_GRAPH`, `RUN_BUTTON`, …).

## What the library does *not* do

- **No agent runtime.** It doesn't decide how your agent thinks, reasons, or calls tools — that's the job of whatever framework (or plain Python) you wrap.
- **No authentication.** That belongs in the FastAPI middleware that wraps the mount point.
- **No multi-tenant isolation.** A `context_id` separates conversations within a single deployment; cross-tenant isolation should be handled at the application layer (per-tenant Redis prefixes, per-tenant deployments, etc.).
- **No persistent transcript beyond Redis TTL.** Tasks expire after 24 h by default. If you need long-term storage, snapshot on `on_task_start` / completion to your own datastore.

These omissions are features, not gaps — they keep the library small enough to read end-to-end and avoid taking opinions in places where applications need to differ.
