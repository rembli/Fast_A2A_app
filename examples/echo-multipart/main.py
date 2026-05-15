"""
main.py — Echo Multipart Agent

Demonstrates multi-message streaming: three separate messages streamed back
for a single prompt using the NEW_MESSAGE sentinel.

Run with:
    uvicorn main:app --reload --port 8000

Open http://localhost:8000/ — no API key required.

Uses the in-process ``MemoryTaskStore`` (the default when no
``task_store`` is passed), so the agent runs without any external
service. For multi-process / cross-instance deployments, instantiate
``RedisTaskStore.from_url(...)`` (or ``MongoTaskStore`` / ``PostgresTaskStore``)
and pass it as ``task_store=``.
"""
from __future__ import annotations

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from fast_a2a_app import a2a_ui, build_a2a_app, build_stream_invoke
from agent import agent_card, stream_invoke

load_dotenv()

app = FastAPI(title="Echo Multipart Agent", version="0.1.0")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


app.mount(
    "/a2a",
    build_a2a_app(
        agent_card=agent_card,
        stream_invoke=build_stream_invoke(stream_invoke),
        history_max_lines=0,   # echo is stateless — skip history injection
    ),
)

app.mount("/", a2a_ui)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
