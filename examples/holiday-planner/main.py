"""
main.py — Holiday Planner FastAPI application

fast_a2a_app is framework-agnostic: build_invoke / build_stream_invoke accept any
``async (str) -> str`` function or ``async (str) -> AsyncIterable[str]`` generator.
The thin adapter below bridges pydantic-ai's agent.run() interface to that contract.

Because build_stream_invoke sets up the report_progress() ContextVar before calling
the generator, report_progress() calls from agent tools still reach the chat UI as
live working-status SSE events — no framework-specific plumbing needed.

Run with:
    uvicorn main:app --reload --port 8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()  # must run before agent.py reads env vars at module level

from fast_a2a_app import (  # noqa: E402
    RedisTaskStore,
    a2a_ui,
    build_a2a_app,
    build_invoke,
    build_stream_invoke,
)
from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
import uvicorn  # noqa: E402

from agent import agent_card, invoke, stream_invoke  # noqa: E402
from config import DEBUG, REDIS_URL  # noqa: E402

task_store = RedisTaskStore.from_url(REDIS_URL) if REDIS_URL else None


# ── App ────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield


app = FastAPI(
    title="Holiday Planner Agent",
    description="AI-powered holiday planning via the A2A protocol",
    version="0.1.0",
    lifespan=lifespan,
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
        invoke=build_invoke(invoke),
        stream_invoke=build_stream_invoke(stream_invoke),
        # We feed conversation history straight into pydantic-ai via
        # message_history (see _invoke / _stream_invoke). Disable the
        # default builder's text-prefix injection so the model doesn't
        # see every prior turn twice.
        history_max_lines=0,
        task_store=task_store,
        debug=DEBUG,
    ),
)

app.mount("/", a2a_ui)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
