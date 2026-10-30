"""Public agent-card gate behavior (0.10.0).

The A2A agent card is a discovery document the spec serves unauthenticated.
The auth gate admits an anonymous request for ``<base>/.well-known/agent-card.json``
by default (``public_agent_card=True``), regardless of mount base, while still
401ing other API paths. Setting ``public_agent_card=False`` restores the old
behavior where the card is gated like the rest of the API.
"""

import asyncio

from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH

from fast_a2a_app.auth import OIDCSettings, RequireAuthMiddleware


def _run_gate(mw, path, headers=None):
    scope = {"type": "http", "path": path, "session": {},
             "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()]}
    status = {}
    async def receive():
        return {"type": "http.request"}
    async def send(msg):
        if msg["type"] == "http.response.start":
            status["code"] = msg["status"]
    asyncio.run(mw(scope, receive, send))
    return status.get("code")


def _gate(settings):
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    return RequireAuthMiddleware(inner, settings)


_ENABLED = dict(client_id="c", client_secret="s", discovery_url="https://x/.well-known")
_CARD_AT_A2A = "/a2a" + AGENT_CARD_WELL_KNOWN_PATH


def test_card_public_by_default():
    mw = _gate(OIDCSettings(**_ENABLED))
    assert _run_gate(mw, _CARD_AT_A2A) == 200          # under /a2a mount
    assert _run_gate(mw, AGENT_CARD_WELL_KNOWN_PATH) == 200  # at site root


def test_other_api_paths_still_gated():
    mw = _gate(OIDCSettings(**_ENABLED))
    assert _run_gate(mw, "/a2a") == 401
    assert _run_gate(mw, "/a2a/uploads") == 401


def test_card_gated_when_opted_out():
    mw = _gate(OIDCSettings(**_ENABLED, public_agent_card=False))
    assert _run_gate(mw, _CARD_AT_A2A) == 401
