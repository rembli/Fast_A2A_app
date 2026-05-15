"""
agent.py — Joke agent using Azure OpenAI chat completions

This example shows how fast_a2a_app works with *any* agent implementation —
no pydantic-ai required. The only contract is:

  - A non-streaming agent is an  ``async (prompt: str) -> str`` function.
  - A streaming agent is an      ``async (prompt: str) -> AsyncIterable[str]`` generator.

Both are then wrapped with ``build_invoke`` / ``build_stream_invoke``
and passed to ``build_a2a_app``.

Authentication uses AzureCliCredential (managed identity, CLI login, or
environment credentials — whatever is available in the environment).
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterable

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from azure.identity.aio import AzureCliCredential, get_bearer_token_provider
from openai import AsyncOpenAI

# ── Agent card ────────────────────────────────────────────────────────────────

APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")

agent_card = AgentCard(
    name="Joke Agent",
    description="Your AI stand-up comedian — ask for any kind of joke.",
    version="0.1.0",
    supported_interfaces=[AgentInterface(url=f"{APP_BASE_URL}/a2a/", protocol_binding="JSONRPC")],
    capabilities=AgentCapabilities(streaming=True),
    default_input_modes=["text"],
    default_output_modes=["text"],
    skills=[
        AgentSkill(
            id="tell_joke",
            name="Tell a joke",
            description=(
                "Tells a joke on any topic with explanation and comic timing. "
                "Supports puns, dad jokes, one-liners, programming humour, and more."
            ),
            tags=[],
        ),
    ],
)


# ── Azure OpenAI config ───────────────────────────────────────────────────────

AZURE_AI_BASE_URL = os.environ.get("AZURE_AI_BASE_URL", "").strip().rstrip("/")
AZURE_AI_DEPLOYMENT_NAME = os.environ.get("AZURE_AI_DEPLOYMENT_NAME", "").strip() or "gpt-4o"

_client = AsyncOpenAI(
    base_url=f"{AZURE_AI_BASE_URL}/openai/v1",
    api_key=get_bearer_token_provider(AzureCliCredential(), "https://ai.azure.com/.default"),
)


_SYSTEM_PROMPT = """You are a stand-up comedian AI with impeccable comic timing.
Your specialty is telling jokes in a structured, satisfying format.

For every user message:
1. Tell a joke that fits the topic or mood they mention (or a random great one if they just say "tell me a joke").
2. Briefly explain why it's funny — the punchline mechanic, the wordplay, or the twist.
3. Offer to tell another or ask what type of joke they'd prefer next.

Types you excel at: puns, one-liners, self-deprecating tech humour, dad jokes,
absurdist humour, and observational comedy.

Keep it clean, clever, and upbeat. Never be offensive. Emojis are encouraged."""


# ── Non-streaming agent ───────────────────────────────────────────────────────


async def run_joke_agent(prompt: str) -> str:
    """Complete one turn — returns the full response as a string."""
    response = await _client.chat.completions.create(
        model=AZURE_AI_DEPLOYMENT_NAME,
        max_completion_tokens=512,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    return (response.choices[0].message.content or "").strip()


# ── Streaming agent ───────────────────────────────────────────────────────────


async def stream_joke_agent(prompt: str) -> AsyncIterable[str]:
    """Stream one turn — yields token chunks as they arrive from the API."""
    stream = await _client.chat.completions.create(
        model=AZURE_AI_DEPLOYMENT_NAME,
        max_completion_tokens=512,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        stream=True,
    )
    async for chunk in stream:
        if not chunk.choices:
            continue
        text = chunk.choices[0].delta.content or ""
        if text:
            yield text
