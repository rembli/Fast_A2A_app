"""
main.py — Secure Agent

Exercises the full secure stack fast_a2a_app ships:

  * **Persistent task store** — ``SqliteTaskStore`` on a single on-disk file
    (``DATA_DIR/tasks.db``). Task state, conversation history, and the
    replayable progress log survive a restart with no external database.
  * **Generic OIDC sign-in** — ``install_auth`` gates the whole app behind
    OpenID Connect. Microsoft Entra ID by default; any OIDC provider via the
    ``OIDC_*`` env vars. No MSAL.
  * **Signed-in user's name in the UI** — ``auth_ui_kwargs`` wires the
    header's Sign-in/Sign-out link *and* the name badge (``/api/me``).

Auth stays **off** locally when no credentials are set, so you can run this
with just ``az login`` + the Azure OpenAI vars. Add the Entra vars (see the
README) to see the login gate and name badge switch on.

Startup also runs ``clean_up_stale_tasks`` so any task left mid-flight by a
previous process (crash / restart) is settled to a terminal state — only
possible *because* the store is persistent.

Run with:
    uvicorn main:app --app-dir examples/secure-agent --reload --port 8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()  # must run before agent.py reads env vars at module level

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from fast_a2a_app import (  # noqa: E402
    SqliteTaskStore,
    auth_ui_kwargs,
    build_a2a_app,
    build_a2a_ui,
    build_invoke,
    build_stream_invoke,
    clean_up_stale_tasks,
    install_auth,
)

from agent import agent_card, invoke, stream_invoke  # noqa: E402
from config import DEBUG, TASK_DB_PATH  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1) Persistent SQLite task store (survives restarts).
    task_store = await SqliteTaskStore.from_path(TASK_DB_PATH)

    # 2) Settle anything a previous process left WORKING when it stopped.
    await clean_up_stale_tasks(task_store)

    # 3) Mount the A2A server on the persistent store.
    app.mount(
        "/a2a",
        build_a2a_app(
            agent_card=agent_card,
            invoke=build_invoke(invoke),
            stream_invoke=build_stream_invoke(stream_invoke),
            task_store=task_store,
            debug=DEBUG,
        ),
    )

    # 4) Mount the chat UI. ``auth_ui_kwargs`` returns the login/logout/status
    #    + user-info URLs when auth is configured, or an empty dict when it
    #    isn't — so the header shows auth affordances only when they apply.
    app.mount("/", build_a2a_ui(**auth_ui_kwargs(SETTINGS)))

    try:
        yield
    finally:
        await task_store.close()


app = FastAPI(
    title="Secure Agent",
    description="A simple assistant behind OIDC sign-in with a persistent SQLite task store.",
    version="0.1.0",
    lifespan=lifespan,
)

# Generic OIDC sign-in (Microsoft Entra ID by default). The /auth routes +
# session cookie are always mounted; the login gate only enforces a login when
# credentials are configured (ENTRA_* or OIDC_* env vars), so local dev stays
# open. Wire it before the other middleware/mounts so its session + gate wrap
# everything. ``SETTINGS.enabled`` tells the UI whether to show auth links.
SETTINGS = install_auth(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["ops"])
async def health() -> dict:
    return {"status": "ok"}


@app.get("/ready", tags=["ops"])
async def ready() -> dict:
    return {"status": "ready"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
