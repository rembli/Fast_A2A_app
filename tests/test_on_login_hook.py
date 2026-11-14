"""install_auth ``on_login`` hook — hand the app the full token set on sign-in.

The framework keeps only user claims in the session. An app that needs to call a
downstream API On-Behalf-Of the user must persist the delegated tokens itself;
``on_login`` delivers the authlib token dict (access/refresh token + claims) at
the moment of a successful sign-in so it can.
"""

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

from fast_a2a_app.auth import OIDCSettings, _build_router

_TOKEN = {
    "access_token": "at-123",
    "refresh_token": "rt-456",
    "expires_at": 9999999999,
    "userinfo": {"email": "u@example.com", "name": "U"},
}


def _app_with_router(on_login):
    """A FastAPI app whose /auth/callback is wired to a fake OIDC client that
    returns ``_TOKEN`` (no network), plus the on_login hook under test."""
    async def authorize_access_token(request):
        return dict(_TOKEN)

    oauth = SimpleNamespace(oidc=SimpleNamespace(authorize_access_token=authorize_access_token))
    app = FastAPI()
    app.include_router(_build_router(oauth, OIDCSettings(client_id="c", client_secret="s",
                                                         discovery_url="https://x/.well-known"),
                                     on_login=on_login))
    app.add_middleware(SessionMiddleware, secret_key="test")
    return app


def test_on_login_receives_full_token():
    seen = {}

    async def hook(request, token):
        seen["token"] = token
        seen["email"] = request.session["user"]["email"]

    client = TestClient(_app_with_router(hook), follow_redirects=False)
    resp = client.get("/auth/callback")
    assert resp.status_code in (302, 307)
    # The hook got the delegated tokens (refresh token included) …
    assert seen["token"]["refresh_token"] == "rt-456"
    assert seen["token"]["access_token"] == "at-123"
    # … and the session user was already populated when it ran.
    assert seen["email"] == "u@example.com"


def test_no_hook_still_signs_in():
    client = TestClient(_app_with_router(None), follow_redirects=False)
    resp = client.get("/auth/callback")
    assert resp.status_code in (302, 307)


def test_hook_failure_fails_login():
    async def boom(request, token):
        raise RuntimeError("store down")

    client = TestClient(_app_with_router(boom), follow_redirects=False, raise_server_exceptions=False)
    resp = client.get("/auth/callback")
    assert resp.status_code == 500  # login fails rather than silently dropping tokens
