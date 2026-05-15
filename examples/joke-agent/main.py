"""
main.py — Joke Agent

Demonstrates wiring a plain chat-completions agent (no AI framework) to
fast_a2a_app using build_invoke / build_stream_invoke.

Run with:
    uvicorn main:app --reload --port 8000
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()  # must run before agent.py reads env vars at module level

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from fast_a2a_app import (  # noqa: E402
    a2a_ui,
    build_a2a_app,
    build_invoke,
    build_stream_invoke,
)
from agent import agent_card, run_joke_agent, stream_joke_agent  # noqa: E402

# Uses the in-process MemoryTaskStore (fast_a2a_app's default). Swap in
# `task_store=RedisTaskStore.from_url(...)` for multi-process or
# cross-instance deployments.
DEBUG = os.getenv("DEBUG", "true").lower() in ("1", "true", "yes")

app = FastAPI(
    title="Joke Agent",
    description="Tells jokes via the A2A protocol — powered by the Anthropic chat completions API",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["ops"])
async def health() -> dict:
    return {"status": "ok"}


app.mount(
    "/a2a",
    build_a2a_app(
        agent_card=agent_card,
        invoke=build_invoke(run_joke_agent),
        stream_invoke=build_stream_invoke(stream_joke_agent),
        debug=DEBUG,
    ),
)

app.mount("/", a2a_ui)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
