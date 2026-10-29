"""
auth.py — Generic OpenID Connect sign-in for a fast_a2a_app FastAPI app

A drop-in login layer that secures the whole app (the A2A server, the chat
UI, and your own routes) behind an OpenID Connect provider. It is
**provider-agnostic** — it speaks standard OIDC authorization-code flow via
``authlib`` and discovers everything from the provider's
``.well-known/openid-configuration`` document, so it works with Microsoft
Entra ID (the default), Google, Okta, Keycloak, Auth0, and anything else
that implements OIDC. No MSAL, no provider SDKs.

ONE CALL
--------
::

    from fast_a2a_app.auth import install_auth

    app = FastAPI(lifespan=lifespan)
    install_auth(app)          # reads config from the environment

Then hand the UI the auth URLs so its header shows a Sign-in / Sign-out
link and the signed-in user's name::

    from fast_a2a_app.auth import auth_ui_kwargs

    app.mount("/", build_a2a_ui(..., **auth_ui_kwargs()))

CONFIGURATION (environment variables)
-------------------------------------
Microsoft Entra ID (the default — set these three and you're done)::

    ENTRA_CLIENT_ID
    ENTRA_CLIENT_SECRET
    ENTRA_TENANT_ID

Any other OIDC provider (generic)::

    OIDC_CLIENT_ID
    OIDC_CLIENT_SECRET
    OIDC_ISSUER            # e.g. https://accounts.google.com
    #   or OIDC_DISCOVERY_URL for a non-standard metadata path
    OIDC_SCOPES            # optional, default "openid profile email"

Optional for either::

    OIDC_REDIRECT_URI      # override the auto-derived https://<host>/auth/callback
    SESSION_SECRET         # cookie-signing key (falls back to the client secret)

Auth turns **on** only when a client id + secret + issuer can be resolved
from the environment (all three Entra vars, or the generic OIDC trio).
When they're absent — the normal local-dev case — the app runs completely
open: the ``/auth/*`` routes and session cookie are still mounted (so the
UI's Sign-in link works the moment you add credentials) but no route is
gated.

SSE NOTE
--------
The login gate is a **pure ASGI middleware**, not a ``BaseHTTPMiddleware``:
the latter buffers responses, which would break the A2A SSE stream on
``/a2a``. The gate only short-circuits *unauthenticated* requests;
authenticated ones pass straight through, so streaming is untouched.

REDIRECT URI
------------
OIDC providers only redirect back to pre-registered URLs. This app's
callback is ``https://<app-host>/auth/callback`` (derived from the request
host; ingress terminates TLS so the public scheme is always ``https``).
Register that exact URI on your app registration, or set
``OIDC_REDIRECT_URI`` to pin it.
"""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, FastAPI, HTTPException, Request
from starlette.responses import RedirectResponse, Response

log = logging.getLogger(__name__)

# The internal name authlib registers the OIDC client under.
_CLIENT_NAME = "oidc"

# Reachable without a session: health probes, the login handshake, and the
# UI's auth-status probe (which must answer for unauthenticated callers too).
_DEFAULT_PUBLIC_PATHS = {
    "/health", "/ready",
    "/auth/login", "/auth/callback", "/auth/logout", "/auth/status",
}
# Non-browser surfaces answer unauthenticated requests with a bare 401 (rather
# than an HTML login redirect a programmatic / SSE client can't follow).
_DEFAULT_API_PREFIXES = ("/a2a", "/api/", "/uploads", "/sandbox")


@dataclass(frozen=True)
class OIDCSettings:
    """Resolved OpenID Connect configuration.

    Build one with :meth:`from_env` (the common path) or construct it
    directly to configure auth in code. ``enabled`` is ``True`` only when a
    client id, secret, and a discovery URL are all present.
    """

    client_id: str | None = None
    client_secret: str | None = None
    discovery_url: str | None = None
    scopes: str = "openid profile email"
    redirect_uri: str | None = None
    session_secret: str | None = None
    public_paths: frozenset[str] = field(default_factory=lambda: frozenset(_DEFAULT_PUBLIC_PATHS))
    api_prefixes: tuple[str, ...] = _DEFAULT_API_PREFIXES
    # When set, the gate also accepts an Authorization: Bearer access token whose
    # ``aud`` equals this value (verified against the provider JWKS/issuer), so
    # service-to-service callers can authenticate without a browser session. The
    # expected issuer is read from the discovery metadata unless overridden.
    bearer_audience: str | None = None
    bearer_issuer: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret and self.discovery_url)

    @classmethod
    def from_env(cls) -> "OIDCSettings":
        """Resolve settings from environment variables.

        Microsoft Entra ID (``ENTRA_*``) is tried first and, if its three
        variables are present, synthesises the standard v2.0 discovery URL.
        Otherwise the generic ``OIDC_*`` variables are read, accepting either
        an ``OIDC_ISSUER`` (``/.well-known/openid-configuration`` is appended)
        or an explicit ``OIDC_DISCOVERY_URL``.
        """
        entra_client_id = os.environ.get("ENTRA_CLIENT_ID")
        entra_client_secret = os.environ.get("ENTRA_CLIENT_SECRET")
        entra_tenant_id = os.environ.get("ENTRA_TENANT_ID")

        client_id = client_secret = discovery_url = None
        scopes = os.environ.get("OIDC_SCOPES", "openid profile email").strip()

        if entra_client_id and entra_client_secret and entra_tenant_id:
            client_id = entra_client_id
            client_secret = entra_client_secret
            discovery_url = (
                f"https://login.microsoftonline.com/{entra_tenant_id}"
                "/v2.0/.well-known/openid-configuration"
            )
        else:
            client_id = os.environ.get("OIDC_CLIENT_ID")
            client_secret = os.environ.get("OIDC_CLIENT_SECRET")
            explicit = os.environ.get("OIDC_DISCOVERY_URL", "").strip()
            issuer = os.environ.get("OIDC_ISSUER", "").strip().rstrip("/")
            if explicit:
                discovery_url = explicit
            elif issuer:
                discovery_url = f"{issuer}/.well-known/openid-configuration"

        return cls(
            client_id=client_id,
            client_secret=client_secret,
            discovery_url=discovery_url,
            scopes=scopes or "openid profile email",
            redirect_uri=os.environ.get("OIDC_REDIRECT_URI", "").strip() or None,
            session_secret=os.environ.get("SESSION_SECRET", "").strip() or None,
            bearer_audience=(os.environ.get("A2A_BEARER_AUDIENCE")
                             or os.environ.get("OIDC_BEARER_AUDIENCE") or "").strip() or None,
            bearer_issuer=(os.environ.get("A2A_BEARER_ISSUER")
                           or os.environ.get("OIDC_BEARER_ISSUER") or "").strip() or None,
        )


def _redirect_uri(request: Request, settings: OIDCSettings) -> str:
    """The OIDC callback URL — must match the URI registered on the provider.

    Built from the external host as ``https://<app-host>/auth/callback``.
    Ingress terminates TLS, so the public scheme is always ``https`` even
    though a proxy may reach the app over http on loopback.
    ``OIDC_REDIRECT_URI`` (``settings.redirect_uri``) overrides it.
    """
    if settings.redirect_uri:
        return settings.redirect_uri
    host = request.headers.get("x-forwarded-host") or request.url.hostname
    return f"https://{host}/auth/callback"


def _normalise_user(claims: dict) -> dict:
    """Map standard OIDC id-token claims to the UI's user shape.

    ``preferred_username`` is Entra's email-ish handle; ``email`` is the
    generic OIDC claim. We keep both covered so the name badge and the
    tooltip populate across providers.
    """
    return {
        "name": claims.get("name") or claims.get("preferred_username"),
        "email": claims.get("email") or claims.get("preferred_username"),
        "sub": claims.get("sub") or claims.get("oid"),
    }


def _build_router(oauth: OAuth, settings: OIDCSettings) -> APIRouter:
    """Create the ``/auth/*`` + ``/api/me`` routes bound to this OIDC client."""
    router = APIRouter()
    client = getattr(oauth, _CLIENT_NAME)

    @router.get("/auth/login")
    async def login(request: Request):
        """Kick off the authorization-code flow and redirect to the provider."""
        return await client.authorize_redirect(request, _redirect_uri(request, settings))

    @router.get("/auth/callback")
    async def callback(request: Request):
        """Complete the flow: exchange the code for tokens and record the user."""
        try:
            token = await client.authorize_access_token(request)
        except Exception as exc:  # authlib raises OAuthError on provider failure
            log.warning("Login failed: %s", exc)
            raise HTTPException(401, "login failed") from exc
        # authlib parses + validates the id_token into ``userinfo`` when the
        # ``openid`` scope is present and server metadata (jwks) is loaded.
        claims = token.get("userinfo") or {}
        if not claims:
            raise HTTPException(401, "no id_token in provider response")
        request.session["user"] = _normalise_user(dict(claims))
        log.info("User signed in: %s", request.session["user"].get("email"))
        return RedirectResponse("/")

    @router.get("/auth/logout")
    async def logout(request: Request):
        """Clear the local session (does not sign the user out of the provider)."""
        request.session.clear()
        return RedirectResponse("/")

    @router.get("/auth/status")
    async def auth_status(request: Request) -> dict:
        """Public probe for the UI header link (Sign in vs Sign out)."""
        return {"authenticated": bool(request.session.get("user"))}

    @router.get("/api/me")
    async def me(request: Request) -> dict:
        """Report auth state + the signed-in user (drives the UI name badge)."""
        if not settings.enabled:
            return {"auth_enabled": False, "user": None}
        return {"auth_enabled": True, "user": request.session.get("user")}

    return router


class RequireAuthMiddleware:
    """Pure-ASGI gate: block unauthenticated requests, pass the rest through.

    Reads the session populated by ``SessionMiddleware`` (which must sit
    outside this one). Authenticated requests are handed straight to the
    inner app with no response wrapping, so SSE on ``/a2a`` streams
    unimpeded. API prefixes get a bare ``401``; browser paths are redirected
    to ``/auth/login``.

    When a :class:`BearerValidator` is configured, API-prefix requests that
    carry a valid ``Authorization: Bearer`` access token are also admitted —
    the service-to-service path, alongside the browser session cookie.
    """

    def __init__(self, app, settings: OIDCSettings, bearer=None) -> None:
        self.app = app
        self._public_paths = settings.public_paths
        self._api_prefixes = settings.api_prefixes
        self._bearer = bearer
        # Advertised on the 401 challenge so a service-to-service caller can
        # discover which audience to mint a token for (see _challenge_headers).
        self._bearer_audience = settings.bearer_audience
        self._discovery_url = settings.discovery_url

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        session = scope.get("session") or {}
        if session.get("user") or path in self._public_paths:
            await self.app(scope, receive, send)
            return
        is_api = path.startswith(self._api_prefixes)
        if is_api and self._bearer is not None:
            claims = await self._bearer.validate(_bearer_token(scope))
            if claims is not None:
                # Expose the verified caller to the app the same shape a session
                # does, so downstream handlers can read request.session["user"].
                sess = scope.get("session")
                if isinstance(sess, dict):
                    sess.setdefault("user", {"sub": claims.get("sub"),
                                             "email": claims.get("email") or claims.get("upn"),
                                             "token": "bearer"})
                await self.app(scope, receive, send)
                return
        if is_api:
            response: Response = Response(status_code=401, headers=self._challenge_headers())
        else:
            response = RedirectResponse("/auth/login")
        await response(scope, receive, send)

    def _challenge_headers(self) -> dict:
        """RFC 6750 ``WWW-Authenticate`` challenge for a rejected API request.

        When bearer acceptance is on, advertise the ``resource`` (the audience a
        token must carry) and a ready-to-use ``scope`` (``<audience>/.default``),
        plus the ``authorization_uri`` (the OIDC discovery document). A
        service-to-service client can then mint the right token and retry without
        the operator pre-sharing the audience out of band. Absent a configured
        bearer audience there's nothing to mint for, so no challenge is sent.
        """
        if not (self._bearer is not None and self._bearer_audience):
            return {}
        parts = [
            f'resource="{self._bearer_audience}"',
            f'scope="{self._bearer_audience}/.default"',
        ]
        if self._discovery_url:
            parts.append(f'authorization_uri="{self._discovery_url}"')
        return {"WWW-Authenticate": "Bearer " + ", ".join(parts)}


def _bearer_token(scope) -> str:
    """Extract the ``Authorization: Bearer`` token from an ASGI scope, or ``""``."""
    for name, value in scope.get("headers") or []:
        if name == b"authorization":
            raw = value.decode("latin-1")
            if raw[:7].lower() == "bearer ":
                return raw[7:].strip()
            return ""
    return ""


def install_auth(
    app: FastAPI,
    *,
    settings: OIDCSettings | None = None,
    require_login: bool | None = None,
) -> OIDCSettings:
    """Wire the ``/auth`` routes, session cookie, and (when enabled) the gate.

    Call once, right after creating the FastAPI app and before mounting the
    A2A server / UI, so the session + gate wrap everything.

    Args:
        app: the FastAPI application to secure.
        settings: explicit configuration. Defaults to
            :meth:`OIDCSettings.from_env` (Entra-first).
        require_login: force the login gate on/off. ``None`` (default) ties
            it to ``settings.enabled`` — gated in the cluster where
            credentials are injected, open locally where they aren't.

    Returns the resolved :class:`OIDCSettings` so the caller can branch on
    ``settings.enabled`` (e.g. to pass :func:`auth_ui_kwargs` to the UI).
    """
    from starlette.middleware.sessions import SessionMiddleware

    settings = settings or OIDCSettings.from_env()
    gate_on = settings.enabled if require_login is None else require_login
    log.info(
        "install_auth: OIDC %s, login gate %s",
        "configured" if settings.enabled else "not configured",
        "ON" if gate_on else "OFF",
    )

    if settings.enabled:
        oauth = OAuth()
        oauth.register(
            name=_CLIENT_NAME,
            client_id=settings.client_id,
            client_secret=settings.client_secret,
            server_metadata_url=settings.discovery_url,
            client_kwargs={"scope": settings.scopes},
        )
        app.include_router(_build_router(oauth, settings))
    else:
        # No provider configured: still mount the routes so the UI's Sign-in
        # link is wired. ``/auth/status`` answers ``{authenticated: false}``
        # and ``/api/me`` reports ``auth_enabled: false``; the login handshake
        # itself 503s until credentials are supplied.
        app.include_router(_build_disabled_router(settings))

    # Install the gate + UserBadge-free design: the UI reads /api/me natively.
    # Added before SessionMiddleware so that — since Starlette runs the
    # last-added middleware outermost — the session is populated first and the
    # gate can read it.
    if gate_on:
        bearer = None
        if settings.enabled and settings.bearer_audience:
            from ._bearer import BearerValidator

            bearer = BearerValidator(
                settings.discovery_url,
                settings.bearer_audience,
                issuer=settings.bearer_issuer,
            )
            log.info("install_auth: bearer-token acceptance ON (aud=%s)", settings.bearer_audience)
        app.add_middleware(RequireAuthMiddleware, settings=settings, bearer=bearer)

    # Sign session cookies. In the cluster the stable, replica-shared client
    # secret keys the signature; locally a random per-process key is fine
    # (sessions simply don't survive a restart, which local dev never needs).
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret
        or settings.client_secret
        or secrets.token_hex(32),
        https_only=settings.enabled,
        same_site="lax",
    )
    return settings


def _build_disabled_router(settings: OIDCSettings) -> APIRouter:
    """Routes for the auth-not-configured case (local dev with no credentials)."""
    router = APIRouter()

    @router.get("/auth/login")
    async def login() -> Response:
        raise HTTPException(503, "sign-in is not configured on this deployment")

    @router.get("/auth/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/")

    @router.get("/auth/status")
    async def auth_status(request: Request) -> dict:
        return {"authenticated": bool(request.session.get("user"))}

    @router.get("/api/me")
    async def me(request: Request) -> dict:
        return {"auth_enabled": False, "user": None}

    return router


def auth_ui_kwargs(
    settings: OIDCSettings | None = None,
    *,
    login_url: str = "/auth/login",
    logout_url: str = "/auth/logout",
    auth_status_url: str = "/auth/status",
    user_info_url: str = "/api/me",
) -> dict:
    """Return the ``build_a2a_ui`` kwargs that wire the UI's auth affordances.

    When auth is enabled, the UI header shows a Sign-in / Sign-out link and
    (via ``user_info_url``) the signed-in user's name. When it's disabled an
    empty dict is returned so the UI shows no auth UI at all::

        settings = install_auth(app)
        app.mount("/", build_a2a_ui(..., **auth_ui_kwargs(settings)))
    """
    settings = settings or OIDCSettings.from_env()
    if not settings.enabled:
        return {}
    return {
        "login_url": login_url,
        "logout_url": logout_url,
        "auth_status_url": auth_status_url,
        "user_info_url": user_info_url,
    }
