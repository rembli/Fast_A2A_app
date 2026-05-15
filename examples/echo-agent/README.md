# Echo Agent

Minimal fast_a2a_app integration — pure Python, no LLM, no API key, no AI framework.

```
examples/echo-agent/
├── agent.py          # Two plain async functions, zero external imports
├── main.py           # FastAPI app with fast_a2a_app wiring
└── requirements.txt  # fast_a2a_app only
```

## What it does

The agent reflects the user's message back. `invoke` returns it immediately as a single string; `stream_invoke` yields it word by word with a 50 ms delay to make streaming visible in the UI.

```python
# agent.py
async def invoke(prompt: str) -> str:
    return f"Echo: {prompt}"

async def stream_invoke(prompt: str) -> AsyncIterable[str]:
    words = f"Echo: {prompt}".split(" ")
    for i, word in enumerate(words):
        yield word if i == len(words) - 1 else word + " "
        await asyncio.sleep(0.05)
```

`main.py` passes them straight to `build_a2a_app` and bypasses history injection with a `prompt_builder` that returns the raw user input:

```python
task_store = RedisTaskStore.from_url(REDIS_URL) if REDIS_URL else None

app.mount(
    "/a2a",
    build_a2a_app(
        agent_card=agent_card,
        invoke=build_invoke(invoke),
        stream_invoke=build_stream_invoke(stream_invoke),
        prompt_builder=lambda ctx: ctx.get_user_input(),
        task_store=task_store,
    ),
)
app.mount("/", a2a_ui)
```

## Running

```bash
cd examples/echo-agent
pip install -e ../../
pip install -r requirements.txt

uvicorn main:app --reload
```

No `.env` needed — the echo agent requires no API key, and no `REDIS_URL` either. The in-process `MemoryTaskStore` is used by default; set `REDIS_URL` (in `examples/.env` or your shell) to opt into Redis for multi-process or cross-instance deployments.

Open `http://localhost:8000/` and type anything.
