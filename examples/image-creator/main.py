"""
main.py — Image Creator FastAPI application

Wires the Azure OpenAI image agent (agent.py) into fast_a2a_app.

The agent functions accept ``(prompt, context)`` so they can read
multi-part user input (attached reference images) and the
conversation's prior artifacts (for "iterate on the last image"
prompts). ``build_invoke`` / ``build_stream_invoke`` detect the
extra positional parameter via ``inspect`` and forward the
``RequestContext`` automatically.

Run with:
    uvicorn main:app --reload --port 8000

Requires:
- Redis (``docker run -d -p 6379:6379 redis:7-alpine``)
- Azure CLI auth (``az login``)
- ``AZURE_AI_BASE_URL`` env var pointing at your Azure AI Foundry host (no path)
- The active image deployment / size / style are picked from
  ``CONFIG_PARAMETERS`` in ``agent.py`` (defaults: ``gpt-image-1-mini``,
  ``1024x1024``, ``natural``; switch per-conversation with ``/set``).
- The same schema is exposed at ``GET /config``.
"""
from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()  # must run before agent.py reads env vars at module level

from fastapi import FastAPI, HTTPException, UploadFile  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import Response  # noqa: E402

from fast_a2a_app import (  # noqa: E402
    RedisTaskStore,
    build_a2a_app,
    build_a2a_ui,
    build_invoke,
    build_stream_invoke,
)
from agent import CONFIG_PARAMETERS, agent_card, invoke, stream_invoke  # noqa: E402
from config import (  # noqa: E402
    ALLOWED_UPLOAD_TYPES,
    DEBUG,
    MAX_UPLOAD_BYTES,
    REDIS_URL,
)
from image_store import store as image_store  # noqa: E402

task_store = RedisTaskStore.from_url(REDIS_URL) if REDIS_URL else None


app = FastAPI(
    title="Image Creator Agent",
    description="Generate and iterate on images with Google Gemini / Imagen via A2A.",
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


@app.get("/config", tags=["ops"])
async def get_config() -> dict:
    """Expose the per-conversation parameter schema (``model`` / ``size`` /
    ``style``) along with each parameter's default and allowed values.

    The schema lives in ``agent.CONFIG_PARAMETERS`` next to the code that
    reads it; this endpoint just serves it verbatim so external clients
    (admin UIs, monitoring, the chat UI itself) can discover what the
    ``/set`` slash command can switch.
    """
    return CONFIG_PARAMETERS


@app.get("/images/{image_id}", tags=["images"])
async def serve_image(image_id: str):
    """Serve a stored image by id. URLs of the form ``/images/<id>`` are
    produced by the agent (via ``image_store.url_for``) and embedded in
    URL-based image artifacts. The chat UI fetches them like any other
    static asset, so the wire transcript stays compact."""
    result = image_store.get(image_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Image not found or expired")
    content, media_type = result
    return Response(
        content=content,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.post("/images", tags=["images"])
async def upload_image(file: UploadFile) -> dict:
    """Accept a user-uploaded image and return a URL the UI can use as a
    reference part. Pre-uploading on file-pick keeps base64 image bytes out
    of the chat's localStorage transcript (which has a ~5-10 MB quota) and
    means a page refresh doesn't lose recently-uploaded references."""
    media_type = (file.content_type or "").lower()
    if media_type not in ALLOWED_UPLOAD_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported media type: {media_type or 'unknown'}",
        )
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large")
    image_id = image_store.put(content, media_type=media_type)
    return {
        "id": image_id,
        "url": image_store.url_for(image_id),
        "mediaType": media_type,
        "filename": file.filename or f"upload-{image_id}",
    }


# Use the default prompt builder with a small history window. The agent
# receives ``prompt`` (= recent dialogue + current user input) and feeds it to
# the model, so stylistic continuity ("keep the same lighting as before") works
# alongside the visual carry-over from the latest generated image.
# Slash commands are detected against ``context.message`` directly inside the
# agent, so the history prefix doesn't interfere with command parsing.
app.mount(
    "/a2a",
    build_a2a_app(
        agent_card=agent_card,
        invoke=build_invoke(invoke),
        stream_invoke=build_stream_invoke(stream_invoke),
        history_max_lines=4,
        task_store=task_store,
        debug=DEBUG,
    ),
)

app.mount("/", build_a2a_ui(
    file_upload_api="/images",
    accepted_file_types=list(ALLOWED_UPLOAD_TYPES),
))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
