"""
agent.py — Echo agent (no LLM, no external dependencies)

The simplest possible fast_a2a_app agent: reflects the user's message back.
Demonstrates that fast_a2a_app works with pure Python — no AI framework, no API key.

  agent_card    → AgentCard wired with this agent's URL + skills
  invoke        → returns the prompt as a single string
  stream_invoke → yields the prompt word by word to demonstrate real streaming
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterable

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")


agent_card = AgentCard(
    name="Echo Agent",
    description="Reflects your message back, word by word.",
    version="0.1.0",
    supported_interfaces=[AgentInterface(url=f"{APP_BASE_URL}/a2a/", protocol_binding="JSONRPC")],
    capabilities=AgentCapabilities(streaming=True),
    default_input_modes=["text"],
    default_output_modes=["text"],
    skills=[
        AgentSkill(
            id="echo",
            name="Echo",
            description="Streams your message back to you word by word.",
            tags=[],
        ),
    ],
)


async def invoke(prompt: str) -> str:
    return f"Echo: {prompt}"


async def stream_invoke(prompt: str) -> AsyncIterable[str]:
    words = f"Echo: {prompt}".split(" ")
    for i, word in enumerate(words):
        yield word if i == len(words) - 1 else word + " "
        await asyncio.sleep(0.05)   # 50 ms delay to make streaming visible
