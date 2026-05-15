# Joke Agent

Shows fast_a2a_app wired to plain Azure OpenAI chat completions — no agent framework at all.

```
examples/joke-agent/
├── agent.py          # run_joke_agent + stream_joke_agent (raw chat completions)
└── main.py           # FastAPI app using build_invoke / build_stream_invoke
```

Dependencies are managed by the parent project's `pyproject.toml` — a single `poetry install` at the repo root covers every example.

## What it does

The agent is a stand-up comedian. It tells a joke on any topic, explains why it's funny, and offers to tell another. The only fast_a2a_app contract is:

- Non-streaming: `async (str) -> str`
- Streaming: `async (str) -> AsyncIterable[str]`

Both are plain functions calling `client.chat.completions.create` directly — no wrapper classes, no agent framework:

```python
# agent.py
async def run_joke_agent(prompt: str) -> str:
    response = await _client.chat.completions.create(
        model=AZURE_AI_DEPLOYMENT_NAME,
        max_completion_tokens=512,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    return (response.choices[0].message.content or "").strip()

async def stream_joke_agent(prompt: str) -> AsyncIterable[str]:
    stream = await _client.chat.completions.create(
        model=AZURE_AI_DEPLOYMENT_NAME,
        max_completion_tokens=512,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        stream=True,
    )
    async for chunk in stream:
        if not chunk.choices:
            continue
        text = chunk.choices[0].delta.content or ""
        if text:
            yield text
```

`main.py` wraps them with the helpers and mounts the app:

```python
app.mount("/a2a", build_a2a_app(
    agent_card=agent_card,
    invoke=build_invoke(run_joke_agent),
    stream_invoke=build_stream_invoke(stream_joke_agent),
))
app.mount("/", a2a_ui)
```

Uses the in-process `MemoryTaskStore` (fast_a2a_app's default). For multi-process or cross-instance deployments, pass `task_store=RedisTaskStore.from_url(...)`.

Authentication uses `AzureCliCredential` — managed identity, CLI login, or environment credentials, whatever is available.

## Running

```bash
# One-time: install fast_a2a_app + every example's deps from the repo root
poetry install

# One-time: create your .env from the shared template
cp examples/.env.example examples/.env
# edit examples/.env — set AZURE_AI_BASE_URL and AZURE_AI_DEPLOYMENT_NAME

az login                                        # AzureCliCredential

cd examples/joke-agent
poetry run uvicorn main:app --reload
```

Open `http://localhost:8000/` and try:

> *"Tell me a programming joke"* or *"Give me your best dad joke"*

Tokens stream directly from the Azure OpenAI API to the browser as they arrive.
