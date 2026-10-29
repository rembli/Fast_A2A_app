"""Bearer-token validation for the auth gate (0.9.0).

Validates ``fast_a2a_app._bearer.BearerValidator`` end-to-end with a self-signed
RSA key (no network): a token with the right audience + issuer is accepted; a
wrong audience, wrong issuer, expired token, or bad signature is rejected. Then
checks the gate admits a request carrying a valid bearer and 401s an invalid one.
"""

import asyncio
import time

import pytest
from authlib.jose import JsonWebKey, jwt

from fast_a2a_app._bearer import BearerValidator


ISSUER = "https://login.microsoftonline.com/tenant/v2.0"
AUD = "api://agent-app-id"


@pytest.fixture(scope="module")
def keys():
    key = JsonWebKey.generate_key("RSA", 2048, {"kid": "k1"}, is_private=True)
    jwks = {"keys": [key.as_dict(is_private=False)]}
    return key, jwks


def _token(key, *, aud=AUD, iss=ISSUER, exp_delta=3600, kid="k1"):
    now = int(time.time())
    header = {"alg": "RS256", "kid": kid}
    payload = {"iss": iss, "aud": aud, "sub": "user-1", "email": "u@x",
               "iat": now, "nbf": now, "exp": now + exp_delta}
    return jwt.encode(header, payload, key).decode("ascii")


def _validator(jwks):
    return BearerValidator(None, AUD, issuer=ISSUER, jwks=jwks)


def test_valid_token_accepted(keys):
    key, jwks = keys
    claims = asyncio.run(_validator(jwks).validate(_token(key)))
    assert claims and claims["sub"] == "user-1" and claims["email"] == "u@x"


def test_wrong_audience_rejected(keys):
    key, jwks = keys
    assert asyncio.run(_validator(jwks).validate(_token(key, aud="api://someone-else"))) is None


def test_wrong_issuer_rejected(keys):
    key, jwks = keys
    assert asyncio.run(_validator(jwks).validate(_token(key, iss="https://evil/v2.0"))) is None


def test_expired_token_rejected(keys):
    key, jwks = keys
    assert asyncio.run(_validator(jwks).validate(_token(key, exp_delta=-10))) is None


def test_bad_signature_rejected(keys):
    _key, jwks = keys
    other = JsonWebKey.generate_key("RSA", 2048, {"kid": "k1"}, is_private=True)
    forged = _token(other)  # signed by a key not in the JWKS
    assert asyncio.run(_validator(jwks).validate(forged)) is None


def test_garbage_token_rejected(keys):
    _key, jwks = keys
    assert asyncio.run(_validator(jwks).validate("not-a-jwt")) is None
    assert asyncio.run(_validator(jwks).validate("")) is None


# --- gate integration -------------------------------------------------------

from fast_a2a_app.auth import OIDCSettings, RequireAuthMiddleware


def _run_gate(mw, path, headers):
    scope = {"type": "http", "path": path, "session": {},
             "headers": [(k.encode(), v.encode()) for k, v in headers.items()]}
    status = {}
    async def receive():
        return {"type": "http.request"}
    async def send(msg):
        if msg["type"] == "http.response.start":
            status["code"] = msg["status"]
    asyncio.run(mw(scope, receive, send))
    return status.get("code")


def test_gate_admits_valid_bearer_and_blocks_invalid(keys):
    key, jwks = keys
    passed = {"n": 0}
    async def inner(scope, receive, send):
        passed["n"] += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    settings = OIDCSettings(client_id="c", client_secret="s",
                            discovery_url="https://x/.well-known", bearer_audience=AUD)
    mw = RequireAuthMiddleware(inner, settings, bearer=_validator(jwks))

    assert _run_gate(mw, "/a2a", {"authorization": f"Bearer {_token(key)}"}) == 200
    assert passed["n"] == 1
    assert _run_gate(mw, "/a2a", {"authorization": "Bearer bad"}) == 401
    assert _run_gate(mw, "/a2a", {}) == 401  # no token at all
