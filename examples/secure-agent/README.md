# Secure Agent

Exercises the **full secure stack** fast_a2a_app ships — a persistent
`SqliteTaskStore` plus generic OpenID Connect sign-in (Microsoft Entra ID by
default) — around a minimal **pydantic-ai** assistant.

```
examples/secure-agent/
├── agent.py          # a minimal pydantic-ai assistant (Azure OpenAI model + one tool)
├── config.py         # resolves the SQLite file path (DATA_DIR, with local fallback)
└── main.py           # wires SqliteTaskStore + install_auth + build_a2a_ui
```

Dependencies are managed by the parent project's `pyproject.toml` — a single
`poetry install` at the repo root covers every example.

## What it demonstrates

**1. Persistent task store (`SqliteTaskStore`).** Task state, conversation
history, and the replayable progress log live in one on-disk file
(`DATA_DIR/tasks.db`) and survive a restart — no external database.

```python
# main.py (lifespan)
task_store = await SqliteTaskStore.from_path(TASK_DB_PATH)
await clean_up_stale_tasks(task_store)          # settle crash-left tasks on boot
app.mount("/a2a", build_a2a_app(
    agent_card=agent_card,
    invoke=build_invoke(invoke),
    stream_invoke=build_stream_invoke(stream_invoke),
    task_store=task_store,
))
```

Because the store is persistent, the startup `clean_up_stale_tasks` sweep can
finalize any task a previous process left mid-flight (crash / restart) — stop
the server mid-reply, restart it, and the stuck task settles instead of
hanging the UI forever.

**2. Generic OIDC sign-in (`install_auth`).** One call gates the whole app
behind OpenID Connect. Microsoft Entra ID is the default; any other provider
works via the `OIDC_*` vars. No MSAL.

```python
# main.py (module level)
SETTINGS = install_auth(app)                    # reads ENTRA_* / OIDC_* from env
```

**3. Signed-in user's name in the UI (`auth_ui_kwargs`).** Wires the header's
Sign-in / Sign-out link *and* the name badge in one line.

```python
app.mount("/", build_a2a_ui(**auth_ui_kwargs(SETTINGS)))
```

When no credentials are configured, `auth_ui_kwargs` returns `{}` and the app
runs completely open — ideal for local dev.

## Running

```bash
# One-time: install fast_a2a_app + every example's deps from the repo root
poetry install

# One-time: create your .env from the shared template
cp examples/.env.example examples/.env
# edit examples/.env — set AZURE_AI_BASE_URL and AZURE_AI_DEPLOYMENT_NAME

az login                                        # AzureCliCredential for the model

cd examples/secure-agent
poetry run uvicorn main:app --reload
```

Open `http://localhost:8000/` and chat. The SQLite file appears at
`examples/secure-agent/data/tasks.db` (the local fallback for `DATA_DIR`).
Restart the server and your conversation history is still there.

### Turning on sign-in

Auth stays **off** until you supply credentials. To see the login gate and the
name badge, add a provider to `examples/.env`:

**Microsoft Entra ID** (the default — set these three):

```bash
ENTRA_CLIENT_ID=<application-client-id>
ENTRA_CLIENT_SECRET=<client-secret>
ENTRA_TENANT_ID=<directory-tenant-id>
```

**Any other OIDC provider** (generic):

```bash
OIDC_CLIENT_ID=<client-id>
OIDC_CLIENT_SECRET=<client-secret>
OIDC_ISSUER=https://accounts.google.com       # discovery is auto-derived
# OIDC_SCOPES=openid profile email            # optional
```

With credentials present:

- every route is gated — an unauthenticated browser is redirected to
  `/auth/login`, and API/SSE paths (`/a2a`, `/api/`, `/uploads`) get a bare
  `401`;
- after login the header shows **Sign out** and the signed-in user's **name**;
- the session is a signed cookie keyed by `SESSION_SECRET` (falls back to the
  client secret).

> **Redirect URI.** OIDC providers only redirect back to pre-registered URLs.
> This app's callback is `https://<app-host>/auth/callback` (derived from the
> request host). Register that exact URI on your app registration, or set
> `OIDC_REDIRECT_URI` to pin it. Running over plain `http://localhost` works
> only if your provider allows it; otherwise test behind an HTTPS tunnel.
