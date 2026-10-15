"""
agent.py — Secure Agent (pydantic-ai)

A small **pydantic-ai** assistant used to exercise the full secure stack in
``main.py``: a persistent ``SqliteTaskStore`` and generic OIDC / Microsoft
Entra sign-in.

The agent itself is deliberately minimal — the point of this example is the
*wiring*, not the model. It carries conversation history across turns (so you
can verify history survives a restart via the SQLite store) and ships one tool
(``get_current_time``) that calls ``report_progress`` so you can watch a
progress entry land in the SQLite progress log and replay on resubscribe.

Authentication to the model uses AzureCliCredential (managed identity, CLI
login, or environment credentials — whatever is available).
"""
from __future__ import annotations

from collections.abc import AsyncIterable
from datetime import datetime, timezone

from a2a.server.agent_execution import RequestContext
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Role,
)
from azure.identity.aio import AzureCliCredential, get_bearer_token_provider
from openai import AsyncOpenAI
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart as ChatTextPart,
    UserPromptPart,
)
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from config import APP_BASE_URL, AZURE_AI_BASE_URL, AZURE_AI_DEPLOYMENT_NAME
from fast_a2a_app import report_progress

# ── Agent card ────────────────────────────────────────────────────────────────

agent_card = AgentCard(
    name="Secure Agent",
    description="A simple assistant behind OIDC sign-in with a persistent SQLite task store.",
    version="0.1.0",
    supported_interfaces=[AgentInterface(url=f"{APP_BASE_URL}/a2a/", protocol_binding="JSONRPC")],
    capabilities=AgentCapabilities(streaming=True),
    default_input_modes=["text"],
    default_output_modes=["text"],
    skills=[
        AgentSkill(
            id="assist",
            name="Assist",
            description="Answers questions in a friendly, concise way.",
            tags=[],
        ),
    ],
)


# ── Model (Azure OpenAI via pydantic-ai) ──────────────────────────────────────

_client = AsyncOpenAI(
    base_url=f"{AZURE_AI_BASE_URL}/openai/v1",
    api_key=get_bearer_token_provider(AzureCliCredential(), "https://ai.azure.com/.default"),
)
_model = OpenAIModel(AZURE_AI_DEPLOYMENT_NAME, provider=OpenAIProvider(openai_client=_client))

assistant: Agent = Agent(
    model=_model,
    system_prompt=(
        "You are a helpful, concise assistant. Answer clearly and briefly in "
        "plain language with a friendly tone. If the user asks what time or "
        "date it is, call the get_current_time tool rather than guessing."
    ),
)


# ── Tools ─────────────────────────────────────────────────────────────────────


@assistant.tool
async def get_current_time(ctx: RunContext[None]) -> str:
    """Return the current UTC date and time in ISO-8601 form.

    Calls ``report_progress`` so you can watch a status tick persist to the
    SQLite progress log (and replay on resubscribe / after a restart).
    """
    report_progress("Checking the clock…")
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


# ── Conversation history ──────────────────────────────────────────────────────

_HISTORY_MAX_MESSAGES = 16  # ≈8 turns


def build_message_history(context: RequestContext) -> list[ModelMessage]:
    """Convert A2A related-task history into pydantic-ai ModelMessages.

    Walks ``related_tasks`` chronologically, emitting one ``ModelRequest``
    (the user turn) + one ``ModelResponse`` (the agent's artifact text) per
    task so the model can follow the conversation. Agent text is sourced from
    ``task.artifacts`` only — not ``task.history`` — so transient
    ``report_progress`` status messages aren't replayed as if they were
    replies. Because the task store is persistent, this history survives a
    restart.
    """
    messages: list[ModelMessage] = []
    for task in getattr(context, "related_tasks", None) or []:
        user_text = ""
        for msg in getattr(task, "history", None) or []:
            if getattr(msg, "role", 0) != Role.ROLE_USER:
                continue
            texts = [
                p.text for p in (msg.parts or [])
                if p.WhichOneof("content") == "text" and p.text
            ]
            if texts:
                user_text = "\n".join(texts).strip()
                break

        agent_text_parts: list[str] = []
        for art in getattr(task, "artifacts", None) or []:
            for part in art.parts or []:
                if part.WhichOneof("content") == "text" and part.text:
                    agent_text_parts.append(part.text)
        agent_text = "\n".join(agent_text_parts).strip()

        if user_text:
            messages.append(ModelRequest(parts=[UserPromptPart(content=user_text)]))
        if agent_text:
            messages.append(ModelResponse(parts=[ChatTextPart(content=agent_text)]))

    if len(messages) > _HISTORY_MAX_MESSAGES:
        messages = messages[-_HISTORY_MAX_MESSAGES:]
    return messages


# ── A2A invoke entry points ───────────────────────────────────────────────────


async def invoke(prompt: str, context: RequestContext) -> str:
    """Non-streaming entry — runs the full tool-call chain and returns text."""
    result = await assistant.run(prompt, message_history=build_message_history(context))
    return str(result.output or "").strip() or "(no response)"


async def stream_invoke(prompt: str, context: RequestContext) -> AsyncIterable[str]:
    """Streaming entry — streams the assistant's reply token by token.

    ``run_stream`` drives the tool-call chain and streams the final text; any
    ``report_progress`` tick a tool emits flows to the UI (and the SQLite
    progress log) on its own path.
    """
    async with assistant.run_stream(
        prompt, message_history=build_message_history(context),
    ) as stream:
        async for text in stream.stream_text(delta=True):
            if text:
                yield text
