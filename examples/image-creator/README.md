# Image Creator Agent

A pydantic-ai agent that generates and iterates on images with Azure OpenAI's `gpt-image-1-mini` (and `gpt-image-1`). The chat model orchestrates: it interprets vague intent, expands prompts, can call mock external tools (web search, brand-asset lookup), and chains multiple image-generation steps for plans like *"three variations and combine the best two"*.

```
examples/image-creator/
├── agent.py          # pydantic-ai agent + tools + slash commands
├── image_store.py    # Filesystem-backed image storage (independent module)
└── main.py           # FastAPI wiring + /images endpoints
```

Dependencies are managed by the parent project's `pyproject.toml` — a single `poetry install` at the repo root covers every example.

## What it shows

- **Two models per turn** — a chat model (`AZURE_AI_DEPLOYMENT_NAME`, e.g. `gpt-4o`) drives the agent loop; an image model (`gpt-image-1-mini` by default) renders pixels.
- **Tool-driven workflow** — `generate_image`, `expand_intent`, `rewrite_prompt`, `web_search`, `brand_asset_lookup`. The chat model picks tools and chains them.
- **Multi-step plans** — *"generate three variations of this in different lighting"* — the agent calls `generate_image` repeatedly; each result streams into the chat as it lands.
- **Conversation-aware orchestration** — A2A history is converted into pydantic-ai `ModelMessage`s and passed as `message_history` so the chat model carries forward style preferences and feedback (*"keep the warm palette"*, *"no people"*) across turns instead of starting from scratch every time.
- **Prompt suggestions** — onboarding starters (after `/hello` / empty input), edit options (when an image is uploaded without text), and refinement options (after a successful generation) are emitted as `PROMPT_SUGGESTIONS` data parts and rendered as clickable buttons by the UI.
- **URL-based image artifacts** — generated and uploaded images live in `image_store` (filesystem under `tmp/`); the agent ships only `/images/<id>` URLs over the wire. Transcripts stay compact and survive a refresh because the UI re-fetches via the sibling endpoint.
- **Multi-part user input** — attach button in the UI uploads images via `POST /images` so even base64 data never enters `localStorage`.
- **Fullscreen image viewer** — click any image in the chat for a lightbox with prev/next navigation; the dedicated input there sends the currently-viewed image as a reference and stays open while the agent generates the next image (loader resumes on reopen if the user closed mid-turn).
- **Slash commands** — `/hello`, `/help`, `/models`, `/models <id>` short-circuit the agent and return canned responses (no LLM call). `/hello` is text-only — the starter prompts surface where they earn their keep (empty composer, image-without-text uploads, post-generation refinement) rather than pinning them to the welcome turn.
- **`accepted_file_types` on the file picker** — `build_a2a_ui(file_upload_api=..., accepted_file_types=[...])` narrows the chat's file picker to the formats the server actually accepts (`image/png`, `image/jpeg`, `image/webp`, `image/gif`). Non-image attachments would render a generic file tile rather than a broken-image thumbnail anyway, but matching server and picker keeps the UX clean.
- **Conversation-scoped state** — active image model is recovered by scanning prior `/models <id>` commands in the related-task history.

## Tools (in `agent.py`)

| Tool | Purpose |
|---|---|
| `generate_image(prompt, reference_image_url=None)` | Create or edit an image — only tool that produces pixels. Returns the URL. |
| `expand_intent(vague_request)` | LLM call that turns *"more retro"* into a 3–5 sentence concrete brief (palette, lighting, lens, mood). |
| `rewrite_prompt(rough_prompt)` | LLM call that tightens an under-specified prompt into a single clean paragraph. |
| `web_search(query)` | **Mock.** Returns canned visual cues. Wire to Brave / SerpAPI / OpenAI Responses-API web search for production. |
| `brand_asset_lookup(asset_name)` | **Mock.** Wire to your DAM (S3, Bynder, Frontify…) to fetch internal packshots / logos as references. |

## Slash commands

| Command | Effect |
|---|---|
| `/hello` | Welcome message — auto-sent by the UI on a new context. |
| `/help` | Capabilities summary. |
| `/models` | List + switch image models (catalog-only). |
| `/models <id>` | Switch the image deployment for this conversation. |

## Image-model catalog

| ID | Notes |
|---|---|
| `gpt-image-1-mini` (default) | Faster, lower-cost. |
| `gpt-image-1` | Higher fidelity, slower. |

To enable a different image deployment, add an entry to the `MODELS` dict in `agent.py`. (Catalog-only by design — `/models <unknown-name>` is rejected.)

## Running

```bash
# 1. Install fast_a2a_app + every example's deps from the repo root
poetry install

# 2. Authenticate to Azure
az login
# In examples/.env (shared with joke-agent / holiday-planner):
#   AZURE_AI_BASE_URL=https://<your-resource>.services.ai.azure.com
#   AZURE_AI_DEPLOYMENT_NAME=gpt-4o     # chat model for orchestration

# Optional — overrides the default 1024x1024 image output size
export IMAGE_SIZE=1024x1024

# 3. Start Redis (optional — falls back to memory if REDIS_URL unset)
docker run -d -p 6379:6379 redis:7-alpine

# 4. Run
cd examples/image-creator
poetry run uvicorn main:app --reload
```

Open `http://localhost:8000/`. The UI greets you via `/hello`. Try:

- *"a moody product shot of a perfume bottle on wet stone, soft rim light"*
- (attach `packshot.jpg`) *"give it a cinematic feel"* — agent will call `expand_intent` then `generate_image` with the reference
- *"make three variations: one minimal, one premium, one playful"* — multi-step plan
- *"a 90s anime poster of a samurai cat"* — agent may call `web_search` first

## Implementation notes

`agent.py` exposes the two A2A entry points:

```python
async def invoke(prompt: str, context: RequestContext) -> Artifact: ...
async def stream_invoke(prompt: str, context: RequestContext) -> AsyncIterable[str | Artifact]: ...
```

Each:
1. Extracts the user's text + image parts from `context.message.parts` (raw bytes for fresh uploads, URL parts for prior-turn references resolved via `image_store.get`).
2. Short-circuits a slash command if matched, and emits prompt suggestions for empty / image-only / post-generation paths.
3. Pre-stores any current-turn upload (and the most recent prior image) in `image_store` so the agent can refer to them by URL.
4. Builds an `AgentDeps` and a prompt with an `Available references:` preamble listing the URLs the agent can use.
5. Builds `message_history` from `context.related_tasks` (text-only, capped at ~6 turns) so the chat model carries earlier feedback forward.
6. Awaits `image_agent.run(prompt, deps=deps, message_history=...)` — pydantic-ai loops through tool calls until the chat model is done.
7. Streams each artifact stashed in `deps.generated` (one per `generate_image` call), then the agent's final text reply (bundled with refinement-suggestion buttons).

`image_store.py` is an independent module — no dependency on `fast_a2a_app` or the UI. The `/images/{id}` GET endpoint and `/images` POST endpoint live in `main.py` and use the singleton store.

The chat-model client (`AsyncOpenAI` + Azure CLI bearer-token provider) is shared between the orchestration agent and the in-tool LLM helpers (`expand_intent`, `rewrite_prompt`).
