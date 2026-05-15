# Holiday Planner Agent

A complete domain-specific agent built on fast_a2a_app using pydantic-ai and Azure OpenAI. Recommends destinations, plots them on an interactive map, builds day-by-day itineraries, estimates budgets, and surfaces travel essentials — all with live progress updates and one-click follow-ups.

```
examples/holiday-planner/
├── agent.py          # pydantic-ai agent + tools + deps + invoke/stream_invoke + suggestion generator
└── main.py           # FastAPI app, AgentCard, mounts
```

Dependencies are managed by the parent project's `pyproject.toml` — a single `poetry install` at the repo root covers every example.

## What it shows

- **Multi-tool pydantic-ai agent** — 4 tools (destinations, itinerary, budget, travel essentials). The chat model orchestrates; tools call `report_progress()` so the chat UI shows a live status indicator while each tool runs.
- **`MAP` artifact for recommendations** — `recommend_destinations` asks the LLM for `lat` + `lng` per destination and emits a `map_artifact` with one pin per suggestion. The chat UI renders an interactive [Leaflet](https://leafletjs.com)/OpenStreetMap map; Leaflet is lazy-loaded from a CDN on first map render.
- **Artifact-aware quick-reply pills** — the closing suggestion buttons read the artifacts the tools emitted this turn. When the map carries 3 pins, the buttons become "Pick Rome" / "Pick Athens" / "Pick Lisbon" so the user advances with a single click rather than typing.
- **Conversation-aware orchestration** — A2A history is converted into pydantic-ai `ModelMessage`s and passed as `message_history` so the chat model carries earlier feedback (*"moderate budget"*, *"no cold weather"*) across turns. Agent text is sourced from artifacts only — transient `report_progress` status messages don't leak into the LLM's view of past turns.
- **`/hello` is text-only** — the welcome message doesn't ship dead-click suggestion pills before the user has answered the agent's first question.
- **`agent.py` owns the A2A entry points** — `invoke` (one-shot) and `stream_invoke` (streaming) both live next to the tools, matching the layout of `image-creator` and `data-analysis-agent`. `main.py` is a thin FastAPI composition root.

## Tools

| Tool | Description | Side effect |
|---|---|---|
| `recommend_destinations` | 2–3 tailored destination suggestions with pros, cons, highlights | Drops a `MAP` artifact with one pin per recommendation |
| `create_itinerary` | Day-by-day plan with restaurants and local tips | — |
| `estimate_budget` | Cost breakdown table per person per day | — |
| `get_travel_essentials` | Visa, health, weather, and packing guide | — |

## Quick-reply pill flow

```
Agent: "Here are 3 picks for your September trip:"
       [MAP with Rome, Athens, Lisbon pins]
       "All three fit a moderate-budget, food-and-culture brief. Which sounds right?"
       [Pick Rome] [Pick Athens] [Pick Lisbon]
                ↑ click — no typing
User:  "Let's go with Athens."
Agent: → create_itinerary(destination="Athens", …) → day-by-day plan
       [Plan all 10 days] [Just Acropolis day] [Switch to Rome]
```

`generate_suggestions` in `agent.py` runs as a small out-of-band LLM call after `agent.run()`. It reads the agent's reply *and* a compact summary of the typed artifacts the tools just emitted (e.g. "MAP with 3 pin(s): Rome; Athens; Lisbon"). When the artifacts enumerate concrete choices, the LLM produces one button per choice; otherwise it falls back to plausible free-form responses.

## Running

```bash
# One-time: install fast_a2a_app + every example's deps from the repo root
poetry install

# One-time: create your .env from the shared template
cp examples/.env.example examples/.env
# edit examples/.env — set AZURE_AI_BASE_URL and AZURE_AI_DEPLOYMENT_NAME

docker run -d -p 6379:6379 redis:7-alpine    # optional — falls back to memory if REDIS_URL unset
az login

cd examples/holiday-planner
poetry run uvicorn main:app --reload
```

Open `http://localhost:8000/` and ask:

> *"I want to plan a 10-day trip somewhere in Southeast Asia in September, moderate budget, interested in food, temples, and nature. Can you help?"*

The agent will ask follow-up questions, then use its tools to recommend destinations (with pins on a real map), build a day-by-day itinerary, estimate costs, and provide travel essentials — all with live progress updates in the UI and one-click follow-ups based on what's on-screen.

## Implementation notes

`agent.py` exposes the two A2A entry points:

```python
async def invoke(prompt: str, context: RequestContext) -> Artifact: ...
async def stream_invoke(prompt: str, context: RequestContext) -> AsyncIterable[Artifact]: ...
```

Each:

1. Builds an `AgentDeps(generated=[])` for the turn.
2. Awaits `holiday_agent.run(prompt, deps=deps, message_history=build_message_history(context))` — pydantic-ai loops through tool calls until the chat model is done. Tools push rich artifacts (MAP, …) onto `deps.generated`.
3. Streams each collected artifact in order, then `generate_suggestions(text, deps.generated)` produces the closing pill buttons grounded in the on-screen artifacts.

The `build_message_history` walker filters agent text to `task.artifacts` only — transient `report_progress` updates stay out of the LLM's view of prior turns. The function carries a comment explaining why it doesn't just use the library's `get_task_history` helper.
