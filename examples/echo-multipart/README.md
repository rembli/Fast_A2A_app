# Echo Multipart Agent

Demonstrates streaming multi-part responses — no LLM, no API key, no AI framework.

```
examples/echo-multipart/
├── agent.py          # Yields three separate Artifact objects from a single prompt
└── main.py           # FastAPI app wired with build_stream_invoke
```

Dependencies are managed by the parent project's `pyproject.toml` — a single `poetry install` at the repo root covers every example.

## What it does

For every user message the agent streams back three separate `Artifact` objects, each appearing as a distinct message bubble in the chat UI:

| Artifact | Part type | Rendered as |
|---|---|---|
| `echo` | `text` | Markdown bubble |
| `metadata` | `application/json` | Key-value data table |
| `echo.txt` | `text/plain` with `filename` | File download card |

```python
# agent.py
async def stream_invoke(prompt: str) -> AsyncIterable[str | Artifact]:
    words = prompt.split()

    yield Artifact(
        artifact_id=str(uuid.uuid4()),
        name="echo",
        parts=[Part(text=f"Echo: {prompt}")],
    )

    await asyncio.sleep(0.3)

    yield Artifact(
        artifact_id=str(uuid.uuid4()),
        name="metadata",
        parts=[Part(
            raw=json.dumps({
                "original": prompt,
                "uppercased": prompt.upper(),
                "word_count": len(words),
                "char_count": len(prompt),
            }).encode(),
            media_type="application/json",
        )],
    )

    await asyncio.sleep(0.3)

    yield Artifact(
        artifact_id=str(uuid.uuid4()),
        name="echo.txt",
        parts=[Part(
            raw=f"Echo: {prompt}\n".encode(),
            filename="echo.txt",
            media_type="text/plain",
        )],
    )
```

The 300 ms pauses between artifacts make the sequential delivery visible in the UI. Remove them for production use.

## Running

```bash
# One-time: install fast_a2a_app + every example's deps from the repo root
poetry install

cd examples/echo-multipart
poetry run uvicorn main:app --reload
```

No `.env` needed and no Redis required — the in-process `MemoryTaskStore` is used by default. Open `http://localhost:8000/` and type anything — you will see all three message types appear one after another.
