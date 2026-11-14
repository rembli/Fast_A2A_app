"""Agent-card OAuth2 security advertisement (``apply_agent_card_security``).

When a ``bearer_audience`` is configured the agent's ``/a2a`` surface requires a
bearer token for that audience. The card is stamped with a standard A2A OAuth2
security scheme carrying the ready-to-use ``<audience>/.default`` scope and the
OIDC metadata URL, so a remote caller that resolves the card can mint the right
token with no pre-shared configuration. Without a bearer audience, or when the
author already declared security, the card is left untouched.
"""

from a2a.types import AgentCapabilities, AgentCard, SecurityScheme

from fast_a2a_app.auth import OIDCSettings, apply_agent_card_security

_ENTRA_DISCOVERY = (
    "https://login.microsoftonline.com/e8701075-0d9e-4ea1-991d-5a0d110a5d29"
    "/v2.0/.well-known/openid-configuration"
)
_AUD = "api://c0f9c2a6-95dd-4210-bce0-f7a65c80b0f1"


def _card(name="Agent"):
    return AgentCard(name=name, version="1.0.0", capabilities=AgentCapabilities(streaming=True))


def _settings(**over):
    base = dict(
        client_id="c",
        client_secret="s",
        discovery_url=_ENTRA_DISCOVERY,
        bearer_audience=_AUD,
    )
    base.update(over)
    return OIDCSettings(**base)


def test_advertises_scope_and_endpoints():
    card = apply_agent_card_security(_card("Data Analysis Agent"), _settings())
    assert set(card.security_schemes.keys()) == {"entra"}
    flow = card.security_schemes["entra"].oauth2_security_scheme.flows.authorization_code
    scope = f"{_AUD}/.default"
    assert scope in flow.scopes
    # Entra endpoints are derived from the discovery URL for spec-compliance.
    assert flow.authorization_url.endswith("/oauth2/v2.0/authorize")
    assert flow.token_url.endswith("/oauth2/v2.0/token")
    assert card.security_schemes["entra"].oauth2_security_scheme.oauth2_metadata_url == _ENTRA_DISCOVERY
    # The requirement references the scheme + scope so a client knows what to request.
    assert len(card.security_requirements) == 1
    assert list(card.security_requirements[0].schemes["entra"].list) == [scope]


def test_noop_without_bearer_audience():
    card = apply_agent_card_security(_card(), _settings(bearer_audience=None))
    assert len(card.security_schemes) == 0
    assert len(card.security_requirements) == 0


def test_respects_author_declared_security():
    card = _card()
    # Author already put something under security_schemes — leave it alone.
    card.security_schemes["custom"].CopyFrom(SecurityScheme())
    apply_agent_card_security(card, _settings())
    assert set(card.security_schemes.keys()) == {"custom"}


def test_idempotent():
    card = apply_agent_card_security(_card(), _settings())
    apply_agent_card_security(card, _settings())
    assert len(card.security_requirements) == 1
    assert set(card.security_schemes.keys()) == {"entra"}


def test_generic_oidc_leaves_endpoints_empty_but_sets_metadata_url():
    disc = "https://accounts.example.com/.well-known/openid-configuration"
    card = apply_agent_card_security(_card(), _settings(discovery_url=disc))
    flow = card.security_schemes["entra"].oauth2_security_scheme.flows.authorization_code
    assert flow.authorization_url == ""
    assert flow.token_url == ""
    assert card.security_schemes["entra"].oauth2_security_scheme.oauth2_metadata_url == disc
    assert f"{_AUD}/.default" in flow.scopes
