"""
agent.py — Echo Multipart agent (no LLM, no external dependencies)

Demonstrates streaming multi-part responses: yields three separate Artifact
objects so each appears as a distinct message in the chat UI.

  artifact 1  — text part   (human-readable echo)
  artifact 2  — data part   (JSON metadata, rendered as key-value table)
  artifact 3  — file part   (downloadable plain-text file)
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterable
from typing import Literal

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, Artifact
from pydantic import BaseModel, ConfigDict, Field

from fast_a2a_app import data_artifact, file_artifact, text_artifact

APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")


agent_card = AgentCard(
    name="Echo Multipart Agent",
    description="Streams your message back as three separate messages.",
    version="0.1.0",
    supported_interfaces=[AgentInterface(url=f"{APP_BASE_URL}/a2a/", protocol_binding="JSONRPC")],
    capabilities=AgentCapabilities(streaming=True),
    default_input_modes=["text"],
    default_output_modes=["text"],
    skills=[
        AgentSkill(
            id="echo_multipart",
            name="Echo Multipart",
            description="Streams three separate messages: echo, stats, and uppercased.",
            tags=["demo", "streaming"],
        ),
    ],
)


class EchoMetadata(BaseModel):
    """Structured echo metadata — rendered as a key-value table in the chat UI.

    The Pydantic-style ``_type`` discriminator is aliased to ``type_`` so it
    stays a valid Python attribute; ``serialize_by_alias`` makes ``model_dump()``
    emit the leading-underscore wire form without an explicit ``by_alias=True``.
    """
    model_config = ConfigDict(serialize_by_alias=True)

    type_: Literal["ECHO_METADATA"] = Field(default="ECHO_METADATA", alias="_type")
    original: str
    uppercased: str
    word_count: int
    char_count: int


async def stream_invoke(prompt: str) -> AsyncIterable[str | Artifact]:
    words = prompt.split()

    yield text_artifact(f"Echo: {prompt}", name="echo")

    await asyncio.sleep(0.3)

    metadata = EchoMetadata(
        original=prompt,
        uppercased=prompt.upper(),
        word_count=len(words),
        char_count=len(prompt),
    )
    yield data_artifact(metadata.model_dump())

    await asyncio.sleep(0.3)

    file_content = (
        f"Echo: {prompt}\n\n"
        f"--- Metadata ---\n"
        f"Words     : {len(words)}\n"
        f"Characters: {len(prompt)}\n"
        f"Uppercased: {prompt.upper()}\n"
    )
    yield file_artifact(
        content=file_content.encode(),
        filename="echo.txt",
        media_type="text/plain",
    )
