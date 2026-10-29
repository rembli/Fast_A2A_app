"""Bearer-token validation for the auth gate.

By default the gate (``RequireAuthMiddleware``) admits only requests that carry
the browser **session cookie** set at interactive sign-in. That blocks
*service-to-service* callers — another backend (e.g. an orchestrator calling
this agent's ``/a2a``) has no cookie, only an OAuth **access token**.

When a ``bearer_audience`` is configured, this module lets the gate also accept a
valid ``Authorization: Bearer <jwt>`` on the API surfaces: the token's signature
(provider JWKS), issuer, audience, and expiry are verified. A caller that
presents a token minted for this service's audience is then authenticated —
enabling OBO (the end user's identity forwarded) or client-credentials (the
caller's own app identity).

Validation is opt-in: with no ``bearer_audience`` set, behaviour is unchanged.
Keys are discovered from the same OIDC metadata the sign-in flow already uses and
cached in memory, refetched when an unknown ``kid`` appears (provider rotation).
"""
from __future__ import annotations

import logging
import time

import httpx
from authlib.jose import JsonWebKey, JsonWebToken
from authlib.jose.errors import JoseError

log = logging.getLogger("fast_a2a_app.auth")

# Only RS256 (what Entra and the common OIDC providers sign access tokens with).
_JWT = JsonWebToken(["RS256"])
_JWKS_MIN_REFETCH_S = 60  # don't hammer the JWKS endpoint on a burst of bad kids


class BearerValidator:
    """Validates provider-issued access tokens against a fixed audience.

    ``discovery_url`` is the OIDC ``.well-known`` document; ``issuer`` and the
    JWKS URI are read from it (``issuer`` may be overridden). Construct with
    ``issuer`` + ``jwks`` pre-set to bypass network (tests).
    """

    def __init__(self, discovery_url: str | None, audience: str, *,
                 issuer: str | None = None, jwks: dict | None = None) -> None:
        self._discovery_url = discovery_url
        self._audience = audience
        self._issuer = issuer
        self._jwks_uri: str | None = None
        self._keyset = JsonWebKey.import_key_set(jwks) if jwks else None
        self._fetched_at = 0.0

    async def _load_metadata(self, client: httpx.AsyncClient) -> None:
        if self._issuer and self._jwks_uri:
            return
        if not self._discovery_url:
            raise RuntimeError("bearer validation needs a discovery_url or an explicit issuer+jwks")
        resp = await client.get(self._discovery_url)
        resp.raise_for_status()
        meta = resp.json()
        self._issuer = self._issuer or meta.get("issuer")
        self._jwks_uri = meta.get("jwks_uri")

    async def _ensure_keys(self, *, kid: str | None = None) -> None:
        # Refetch when we have no keys, or an unknown kid shows up (rotation) —
        # rate-limited so a storm of bad tokens can't trigger a fetch storm.
        have_kid = False
        if self._keyset is not None and kid:
            try:
                have_kid = self._keyset.find_by_kid(kid) is not None
            except (ValueError, KeyError):
                have_kid = False
        if self._keyset is not None and (kid is None or have_kid):
            return
        if time.monotonic() - self._fetched_at < _JWKS_MIN_REFETCH_S and self._keyset is not None:
            return
        async with httpx.AsyncClient(timeout=10.0) as client:
            await self._load_metadata(client)
            resp = await client.get(self._jwks_uri)
            resp.raise_for_status()
            self._keyset = JsonWebKey.import_key_set(resp.json())
            self._fetched_at = time.monotonic()

    async def validate(self, token: str) -> dict | None:
        """Return the token's claims if valid for this audience, else ``None``."""
        if not token:
            return None
        try:
            kid = _peek_kid(token)
            await self._ensure_keys(kid=kid)
            claims = _JWT.decode(
                token,
                key=self._keyset,
                claims_options={
                    "iss": {"essential": True, "values": [self._issuer]} if self._issuer else {},
                    "aud": {"essential": True, "value": self._audience},
                    "exp": {"essential": True},
                },
            )
            claims.validate()  # exp/nbf/iat + the options above; raises on failure
            return dict(claims)
        except (JoseError, httpx.HTTPError, ValueError, KeyError) as exc:
            log.info("bearer token rejected: %s", exc)
            return None


def _peek_kid(token: str) -> str | None:
    """Read the ``kid`` from a JWT header without verifying (to pick the key)."""
    import base64
    import json

    try:
        header_b64 = token.split(".", 1)[0]
        pad = "=" * (-len(header_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(header_b64 + pad)).get("kid")
    except Exception:  # noqa: BLE001 — malformed header → let decode reject it
        return None
