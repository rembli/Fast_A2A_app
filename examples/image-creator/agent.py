"""
agent.py — Image Creator agent (pydantic-ai + Azure OpenAI)

A pydantic-ai agent that orchestrates image creation, editing, and
multi-step workflows. The chat model interprets intent, picks tools, and
chains them; the tools deal with pixels, references, and external lookups.

Tools available to the orchestrator:

  - ``generate_image``     create or edit an image (the only tool that
                           actually produces pixels)
  - ``expand_intent``      turn a vague style note into a specific
                           visual prompt before generation
  - ``rewrite_prompt``     tighten a user's rough prompt
  - ``web_search``         find references for styles / concepts (mock)
  - ``brand_asset_lookup`` fetch internal product packshots (mock)

Slash commands (``/hello``, ``/help``, ``/set``) bypass the agent and
short-circuit to canned responses. ``/set`` is a two-step pill wizard
over the ``CONFIG_PARAMETERS`` schema (``model`` / ``size`` / ``style``).
Slash-command parsing is case-insensitive; replies always lowercase.

Two models are used per turn:
  - **Chat model** (``AZURE_AI_DEPLOYMENT_NAME``, e.g. ``gpt-4o``) for
    orchestration, intent expansion, and prompt rewriting.
  - **Image model** (from ``CONFIG_PARAMETERS["model"]["values"]``,
    switched via ``/set model <id>``) for pixels.

Authentication: AzureCliCredential (``az login``).
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import uuid
from collections.abc import AsyncIterable
from dataclasses import dataclass, field

from a2a.server.agent_execution import RequestContext
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Artifact,
    Part,
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

from config import (
    APP_BASE_URL,
    AZURE_AI_BASE_URL,
    AZURE_AI_DEPLOYMENT_NAME,
)
from fast_a2a_app import (
    image_artifact,
    prompt_suggestions_artifact,
    report_progress,
    text_artifact,
)
from fast_a2a_app.server.commons.config import (
    ACTIVE_SETTINGS_CONVENTION,
    format_active_settings,
    handle_set_command,
    resolve_config_from_history,
)
from fast_a2a_app.server.commons.uploads import (
    extract_current_turn,
    latest_file_in_history,
)
from image_store import store as image_store

log = logging.getLogger(__name__)

# ── Agent card ────────────────────────────────────────────────────────────────
# Public A2A metadata served at ``/.well-known/agent-card.json`` — the agent's
# identity, declared skills, and supported I/O modes for discovery clients.

agent_card = AgentCard(
    name="Image Creator",
    description=(
        "Generates and iterates on images using Azure OpenAI's gpt-image-1-mini. "
        "Accepts text prompts and reference images (e.g. product packshots) for "
        "image-to-image editing."
    ),
    version="0.1.0",
    supported_interfaces=[AgentInterface(url=f"{APP_BASE_URL}/a2a/", protocol_binding="JSONRPC")],
    capabilities=AgentCapabilities(streaming=True),
    default_input_modes=["text", "image"],
    default_output_modes=["text", "image"],
    skills=[
        AgentSkill(
            id="generate_image",
            name="Generate image",
            description="Create an image from a natural-language prompt.",
            tags=[],
        ),
        AgentSkill(
            id="edit_image",
            name="Edit reference image",
            description="Iterate on an attached image (product packshot, photo, etc.) with edit instructions.",
            tags=[],
        ),
        AgentSkill(
            id="iterate_image",
            name="Iterate on last image",
            description="Refine the last generated image with follow-up prompts (Gemini 2.5 Flash Image).",
            tags=[],
        ),
    ],
)


# ── Configurable agent parameters ─────────────────────────────────────────────
# Schema for the per-conversation parameters the user can switch at runtime
# (``model`` / ``size`` / ``style``). Each entry declares a default plus an
# enum-like ``values`` mapping (value → short description).
#
# Lives in agent.py — next to the code that reads it — so the schema, the
# defaults, the slash-command handler, and the tool that consumes the
# resolved values all sit together. ``main.py`` imports ``CONFIG_PARAMETERS``
# verbatim and serves it at ``GET /config`` (no transformation).
#
# State is conversation-scoped and recovered from the related-task history by
# scanning for ``/set <param> <value>`` user messages — no server-side store.

CONFIG_PARAMETERS: dict[str, dict] = {
    "model": {
        "description": "Image deployment used for generation.",
        "default": "gpt-image-1-mini",
        "values": {
            "gpt-image-1-mini": "Faster, lower-cost OpenAI image model.",
            "gpt-image-1": "Higher fidelity, slower OpenAI image model.",
        },
    },
    "size": {
        "description": "Output image dimensions.",
        "default": "1024x1024",
        "values": {
            "1024x1024": "Square — best general-purpose default.",
            "1024x1536": "Portrait — vertical compositions, posters.",
            "1536x1024": "Landscape — banners, wide scenes.",
        },
    },
    "style": {
        "description": "Visual style directive prepended to every prompt.",
        "default": "natural",
        "values": {
            "natural": "Photorealistic, true-to-life rendering.",
            "vivid": "Hyper-real, dramatic lighting and saturation.",
            "anime": "Anime / hand-drawn illustration aesthetic.",
            "watercolour": "Soft watercolour painting style.",
        },
    },
}


# ── Azure OpenAI clients ──────────────────────────────────────────────────────
# The chat model (orchestration + helper LLM calls) is fixed via env; the
# image model (pixels) is conversation-scoped and switched via ``/set model``.

_openai_client = AsyncOpenAI(
    base_url=f"{AZURE_AI_BASE_URL}/openai/v1",
    api_key=get_bearer_token_provider(AzureCliCredential(), "https://ai.azure.com/.default"),
)
_chat_model = OpenAIModel(
    AZURE_AI_DEPLOYMENT_NAME,
    provider=OpenAIProvider(openai_client=_openai_client),
)


# ── Pydantic-ai agent ─────────────────────────────────────────────────────────
# The orchestrator: per-turn ``AgentDeps`` state container, the system prompt
# that defines how the model picks and chains tools, and the ``image_agent``
# itself. ``end_strategy="exhaustive"`` keeps the loop running until the model
# stops calling tools.


@dataclass
class AgentDeps:
    """Per-run state passed to tools via ``RunContext[AgentDeps]``.

    *config* is the resolved ``CONFIG_PARAMETERS`` snapshot for this turn
    (``{"model": ..., "size": ..., "style": ...}``), recovered from prior
    ``/set`` commands by
    :func:`fast_a2a_app.server.commons.config.resolve_config_from_history`.
    *available_images* lists URLs the agent can pass to ``generate_image``
    as ``reference_image_url`` (current-turn uploads + the most recent
    conversation image, both pre-stored in image_store so the agent can
    refer to them by URL).
    *generated* collects the artifacts produced during the run; the
    outer ``invoke`` / ``stream_invoke`` yields them after agent.run().
    """
    config: dict[str, str]
    available_images: list[tuple[str, str]]   # [(url, label), ...]
    generated: list[Artifact] = field(default_factory=list)


SYSTEM_PROMPT = f"""You are an expert visual director helping the user create
and iterate on images. Use the tools available to plan and execute their
request.

Conversation context:
- The prior turns of this conversation are provided as message history.
- Read them. Carry forward style preferences, palette decisions, and
  any feedback the user has given ("keep it minimal", "we agreed on
  warm tones", "no people in the shot"). Do not start from scratch on
  every turn.
- Short follow-ups ("warmer", "now from above", "remove the background")
  are deltas on the most recent image — combine them with the
  established direction rather than treating them as standalone briefs.

Workflow:
1. Read the user's request together with the conversation history.
   If they reference an existing image ("the packshot", "make it
   warmer", "this image"), use the URL from the "Available references"
   list in the user's message as the ``reference_image_url`` argument
   to ``generate_image``.
2. If the request is vague about *style* (e.g. "more retro", "give it a
   cinematic feel"), call ``expand_intent`` first to turn the vague
   note into specific visual cues (palette, lighting, composition,
   mood). Pass the expanded text — not the original — to
   ``generate_image``. Reuse style cues from earlier turns when they
   still apply.
3. If the user's prompt is loose, contradictory, or under-specified,
   call ``rewrite_prompt`` to tighten it before generating.
4. For research-driven requests ("a 90s anime poster of a samurai cat"),
   you may call ``web_search`` to gather concrete visual cues, then
   feed the findings into the prompt.
5. For multi-step plans ("three variations" or "combine the packshot
   with my logo"), call ``generate_image`` multiple times in sequence.

{ACTIVE_SETTINGS_CONVENTION}
- The active ``model``, ``size``, and ``style`` are passed to
  ``generate_image`` via tool deps. Do NOT call ``rewrite_prompt`` /
  ``expand_intent`` to inject them — they are applied to every
  generated image regardless.

Reply guidelines:
- Be concise — the image speaks for itself; one or two sentences are
  plenty.
- Don't include image URLs or markdown image links in your reply. The
  chat UI renders generated images alongside your text automatically.
- If a tool returns an error, report it briefly and stop.
"""

image_agent: Agent[AgentDeps] = Agent(
    model=_chat_model,
    deps_type=AgentDeps,
    system_prompt=SYSTEM_PROMPT,
    end_strategy="exhaustive",
)


# ── Tools ─────────────────────────────────────────────────────────────────────
# The toolbox the orchestrator picks from. ``generate_image`` is the only tool
# that produces pixels; the others reshape text (intent expansion, prompt
# rewriting) or fetch references (web search, brand assets — mocks).
#
# Tool docstrings below double as the descriptions the chat model sees during
# tool selection. They are deliberately user/LLM-facing — keep developer-side
# rationale in the comment block ABOVE each tool, so the model isn't fed
# implementation context it doesn't need.


# Returns the image URL (not bytes) so the chat model can pass it back as
# ``reference_image_url`` in subsequent tool calls (multi-step plans).
# Bytes can't traverse the chat-completion text channel; the artifact is
# stashed in deps so the outer entry point can yield it on the wire.
@image_agent.tool
async def generate_image(
    ctx: RunContext[AgentDeps],
    prompt: str,
    reference_image_url: str | None = None,
) -> str:
    """Generate or edit an image.

    Args:
        prompt: Detailed visual prompt — subject, style, lighting,
            composition, mood. Specific is much better than abstract.
        reference_image_url: Optional URL of a reference image to edit
            (must come from the "Available references" list shown in the
            user's message). Omit for a fresh generation.

    Returns:
        The URL of the generated image. The chat UI renders it inline
        automatically — do NOT include this URL in your final reply.
    """
    references: list[tuple[bytes, str]] = []
    if reference_image_url:
        image_id = image_store.id_from_url(reference_image_url)
        if image_id:
            data = image_store.get(image_id)
            if data:
                references.append(data)

    model = ctx.deps.config["model"]
    size = ctx.deps.config["size"]
    style = ctx.deps.config["style"]

    # Inject the style directive only when it differs from the default — keeps
    # the prompt close to the user's intent when no style is explicitly set.
    if style != CONFIG_PARAMETERS["style"]["default"]:
        style_descr = CONFIG_PARAMETERS["style"]["values"].get(style, "")
        effective_prompt = f"{style_descr.rstrip('.')}. {prompt}" if style_descr else prompt
    else:
        effective_prompt = prompt

    if references:
        report_progress(f"Editing with `{model}` @ {size} (1 reference image)…")
    else:
        report_progress(f"Generating with `{model}` @ {size} ({style})…")

    try:
        image_bytes, mime = await _generate_pixels(
            model, effective_prompt, references, size,
        )
    except Exception as exc:
        log.exception("Image generation failed (model=%s, size=%s)", model, size)
        return f"ERROR: image generation failed — {exc}"

    if not image_bytes:
        return "ERROR: model returned no image — try rephrasing the prompt"

    image_id = image_store.put(image_bytes, media_type=mime)
    url = image_store.url_for(image_id)
    ctx.deps.generated.append(image_artifact(
        url=url,
        media_type=mime,
        caption=None,
        filename=f"image-{image_id}.png",
    ))
    return url


# Style nuance ("retro", "cinematic", "Wes Anderson vibe") doesn't reduce
# to keyword substitution — delegate to an LLM that can interpret vibe
# rather than templating the rewrite. Distinct from rewrite_prompt: this
# *adds* visual detail, that one strips ambiguity from a verbose prompt.
@image_agent.tool_plain
async def expand_intent(vague_request: str) -> str:
    """Turn a vague style note into a concrete visual prompt.

    Use when the user says things like "more retro", "cinematic feel",
    "minimal", "Wes Anderson vibe" — anywhere a literal interpretation
    would underspecify the image.

    Args:
        vague_request: The user's vague style instruction.

    Returns:
        A 3–5 sentence prompt with concrete visual cues (palette,
        lighting, lens, mood) ready to feed into ``generate_image``.
    """
    report_progress(f"Expanding intent: {vague_request!r}…")
    response = await _openai_client.chat.completions.create(
        model=AZURE_AI_DEPLOYMENT_NAME,
        max_completion_tokens=400,
        messages=[
            {"role": "system", "content": (
                "You are a senior art director. Turn the user's vague style "
                "note into 3-5 sentences of specific visual instructions: "
                "subject framing, colour palette, lighting, lens / texture, "
                "and mood. Do not narrate or preface — output the prompt only."
            )},
            {"role": "user", "content": vague_request},
        ],
    )
    return (response.choices[0].message.content or vague_request).strip()


# Counterpart to expand_intent: that one *adds* style detail to a vague
# brief; this one *removes* contradictions and looseness from an
# already-verbose prompt before it hits the image model.
@image_agent.tool_plain
async def rewrite_prompt(rough_prompt: str) -> str:
    """Tighten a rough prompt before sending it to the image model.

    Use when the user's instruction is grammatically loose, contradictory,
    or under-specified.

    Args:
        rough_prompt: The user's original prompt, as typed.

    Returns:
        A clean, single-paragraph prompt suitable for direct image
        generation.
    """
    report_progress("Rewriting prompt for clarity…")
    response = await _openai_client.chat.completions.create(
        model=AZURE_AI_DEPLOYMENT_NAME,
        max_completion_tokens=300,
        messages=[
            {"role": "system", "content": (
                "Rewrite the user's image prompt as one tight paragraph. "
                "Resolve contradictions, add concrete subject/style detail, "
                "and preserve their intent. Output only the rewritten prompt."
            )},
            {"role": "user", "content": rough_prompt},
        ],
    )
    return (response.choices[0].message.content or rough_prompt).strip()


# Surfaced in the tool palette even when stubbed so the chat model knows
# the capability is available — without it, the model wouldn't try to
# reach for external research and would over-rely on its own training.
# The integration is per-team (Brave / SerpAPI / OpenAI Responses-API
# web tool / etc.), so the body is intentionally a stub.
@image_agent.tool_plain
async def web_search(query: str) -> str:
    """Search the web for visual references and style cues.

    Use when the user references something specific you don't know about
    (a niche product, regional aesthetic, a movie / show / artist) and
    you want concrete visual guidance before generating.

    Args:
        query: A natural-language search query.

    Returns:
        A short summary of findings, suitable for feeding into a prompt.

    Note:
        This is a **mock** — replace with a real search backend
        (Brave Search, SerpAPI, the OpenAI Responses-API web tool, etc.)
        for production use.
    """
    report_progress(f"Searching the web: {query!r}…")
    await asyncio.sleep(0.2)
    return (
        f"(mock search) Visual references for '{query}':\n"
        "- common visual cues: warm tones, soft rim light, shallow depth of field\n"
        "- typical compositions: hero subject centered, 3/4 angle\n"
        "- mood: aspirational, clean, premium\n"
        "Wire web_search() to a real search API to replace this stub."
    )


# Same rationale as web_search: B2B / agency users routinely reference
# named internal assets ("the FY25 packshot"). Listing the tool keeps
# that pathway visible to the chat model; the actual DAM integration
# (S3 / Bynder / Frontify / etc.) is per-team, so this is a stub.
@image_agent.tool_plain
async def brand_asset_lookup(asset_name: str) -> str:
    """Look up an internal brand asset by name (logo, packshot, style guide).

    Useful for B2B / agency workflows where the user references known
    assets (e.g. "the FY25 packshot", "our primary logo lockup").

    Args:
        asset_name: The name or short description of the asset.

    Returns:
        Either an ``image_store`` URL the agent can pass to
        ``generate_image`` as ``reference_image_url``, or a description
        of why the asset wasn't found.

    Note:
        This is a **mock** — wire it to your DAM (S3 bucket, Bynder,
        Frontify, etc.) to enable real brand-asset references.
    """
    report_progress(f"Looking up brand asset: {asset_name!r}…")
    return (
        f"(mock) Asset '{asset_name}' not found. "
        "Wire brand_asset_lookup() to your DAM to enable internal-asset "
        "references."
    )


# ── Image-model backend (private to the generate_image tool) ──────────────────
# Thin wrapper around the OpenAI images SDK. Isolated so ``generate_image``
# stays focused on orchestration concerns (progress, deps, artifact stashing)
# while this layer just produces bytes.


async def _generate_pixels(
    model: str, prompt: str, references: list[tuple[bytes, str]], size: str,
) -> tuple[bytes | None, str]:
    """Call the OpenAI image API. Direct SDK call — same shape as before.

    Kept separate from :func:`generate_image` so the orchestration concerns
    (progress reporting, deps, artifact stashing, error formatting for the
    chat model) all live in one place — this layer just produces pixels.
    """
    if references:
        files: list[io.BytesIO] = []
        for i, (data, mime) in enumerate(references):
            f = io.BytesIO(data)
            f.name = f"ref_{i}.{_ext_for_mime(mime)}"
            files.append(f)
        result = await _openai_client.images.edit(
            model=model,
            image=files if len(files) > 1 else files[0],
            prompt=prompt,
            n=1,
            size=size,
        )
    else:
        result = await _openai_client.images.generate(
            model=model, prompt=prompt, n=1, size=size,
        )

    items = getattr(result, "data", None) or []
    if not items:
        return None, "image/png"
    b64 = getattr(items[0], "b64_json", None)
    if not b64:
        return None, "image/png"
    return base64.b64decode(b64), "image/png"


def _ext_for_mime(mime: str) -> str:
    """Map an image MIME to a filename extension for the SDK's ``image=`` arg."""
    if mime.endswith("/jpeg") or mime.endswith("/jpg"):
        return "jpg"
    if mime.endswith("/webp"):
        return "webp"
    return "png"


# ── Input parsing + history file lookup ───────────────────────────────────────
# Thin adapters that wire ``image_store`` and the "images only" filter into
# the framework-provided walkers from ``fast_a2a_app.server.commons.uploads``.
# ``_extract_input`` reads the current turn (slash-command detection sees the
# untouched user message); ``_latest_image_in_history`` walks prior turns so
# follow-ups like "make it warmer" pick up whatever is visually most recent.


_HISTORY_MAX_MESSAGES = 12  # ≈6 turns, enough for style preferences to land


def _resolve_image_url(url: str) -> tuple[bytes, str] | None:
    """Adapter from ``image_store`` to the commons.uploads ``Resolver`` shape."""
    image_id = image_store.id_from_url(url)
    return image_store.get(image_id) if image_id else None


def _is_image(_data: bytes, mime: str) -> bool:
    """Predicate keeping only image parts (raw uploads carry any MIME)."""
    return mime.startswith("image/")


def _extract_input(context: RequestContext) -> tuple[str, list[tuple[bytes, str]]]:
    """Current-turn user text + attached images, via commons.uploads."""
    return extract_current_turn(
        context, resolver=_resolve_image_url, predicate=_is_image,
    )


def _latest_image_in_history(context: RequestContext) -> tuple[bytes, str] | None:
    """Most recent image across prior turns (user uploads + agent artifacts)."""
    return latest_file_in_history(
        context, resolver=_resolve_image_url, predicate=_is_image,
    )


def _build_message_history(context: RequestContext) -> list[ModelMessage]:
    """Convert A2A related-task history into pydantic-ai ModelMessages.

    Without this the orchestrator only sees the current user turn and
    forgets earlier feedback ("I told you to keep it minimal", "we
    settled on the warm palette two turns ago"). Walking related_tasks
    in order yields one ``ModelRequest`` (user) + one ``ModelResponse``
    (agent text) per turn. Image bytes are intentionally not included —
    the orchestrator is text-only; current-turn references flow through
    the ``Available references:`` preamble instead.

    Capped at the most recent :data:`_HISTORY_MAX_MESSAGES` so token
    spend stays predictable on long sessions.

    Why not the library helpers (``fast_a2a_app.get_task_history`` /
    ``format_history``)? Two reasons:

    1. They produce **strings** for the default prompt builder's
       prefix-mode. pydantic-ai's ``message_history=`` parameter wants
       typed ``ModelMessage`` / ``ModelRequest`` / ``ModelResponse``
       objects, which the framework deliberately doesn't know about.
    2. ``get_task_history`` sources agent text from BOTH
       ``task.history`` and ``task.artifacts`` — i.e. it includes
       transient status messages emitted via ``report_progress`` as
       if they were agent replies. We only want the artifact-level
       text the user actually saw, so agent text is filtered to
       ``task.artifacts``.
    """
    messages: list[ModelMessage] = []
    for task in getattr(context, "related_tasks", None) or []:
        # User turn — first user-role message in the task's history.
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

        # Skip slash-command turns — they're UI affordances (welcome text,
        # help, model switcher), not real conversation. Feeding them into
        # the LLM as prior assistant replies caused the model to occasionally
        # regurgitate the welcome message verbatim on later turns.
        if user_text.startswith("/"):
            continue

        # Agent reply — combine all text parts across artifacts (transient
        # status messages in `task.history` like "Processing request..."
        # are deliberately skipped; the artifacts hold the real reply).
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


# ── Slash commands ────────────────────────────────────────────────────────────
# Deterministic UI shortcuts (``/hello``, ``/help``, ``/set``) that bypass the
# agent loop. ``/hello`` and ``/help`` are app-specific; ``/set`` delegates to
# :func:`fast_a2a_app.server.commons.config.handle_set_command`, which owns the
# two-step pill wizard backed by ``CONFIG_PARAMETERS``.


def _format_config_summary(config: dict[str, str]) -> str:
    """One-line ``key=value`` summary of the current parameter snapshot."""
    return " · ".join(f"`{name}`=`{config[name]}`" for name in CONFIG_PARAMETERS)


def _hello_text(config: dict[str, str]) -> str:
    """Markdown body for the welcome message (``/hello``)."""
    return (
        "👋 **Welcome to the Image Creator agent.**\n\n"
        "I'm an LLM-driven image director. Describe what you want, attach a "
        "reference, or ask me to expand a vague style ("
        "*\"make it more cinematic\"*, *\"a 90s anime poster of a samurai cat\"*"
        ") and I'll plan, generate, and iterate.\n\n"
        "Quick commands:\n"
        "- `/help` — what I can do\n"
        "- `/set` — change `model`, `size`, or `style` for this conversation\n\n"
        f"Current settings: {_format_config_summary(config)}.\n"
        f"Orchestration: `{AZURE_AI_DEPLOYMENT_NAME}`."
    )


def _help_text(config: dict[str, str]) -> str:
    """Markdown body for the capabilities cheatsheet (``/help``)."""
    return (
        "**Image Creator — capabilities**\n\n"
        "- **Generate from text** — *\"a moody product shot of a perfume bottle "
        "on wet stone, soft rim light\"*.\n"
        "- **Iterate on the last image** — *\"make the lighting warmer\"*.\n"
        "- **Edit a reference image** — attach a packshot, describe the change.\n"
        "- **Expand vague intent** — *\"give this a cinematic feel\"* triggers "
        "an art-direction expansion before pixels are generated.\n"
        "- **Multi-step plans** — *\"generate three variations and pick the most "
        "premium\"* — the agent loops through tools until the plan is done.\n\n"
        "**Slash commands**\n"
        "- `/hello` — welcome message\n"
        "- `/help` — this message\n"
        "- `/set` — change `model`, `size`, or `style` for this conversation\n\n"
        f"Current settings: {_format_config_summary(config)}.\n"
        f"Orchestration: `{AZURE_AI_DEPLOYMENT_NAME}`."
    )


def _handle_slash_command(user_text: str, config: dict[str, str]) -> Artifact | None:
    """Bypass the agent loop for deterministic UI shortcuts.

    Returns an Artifact for matched commands or ``None`` to fall through.
    Detection runs on the raw user text — the history-augmented prompt
    would drag earlier ``/help`` mentions into the current turn. Matching
    is case-insensitive but every reply uses lowercase command tokens.
    """
    lowered = user_text.lower()
    if lowered == "/hello":
        # Welcome text only — no starter pills. The starter prompts are
        # surfaced where the user actually needs a nudge (empty composer
        # state, no-prompt-with-image state); pinning them to /hello
        # crowded a fresh chat with buttons the user hadn't asked for.
        return text_artifact(_hello_text(config))
    if lowered == "/help":
        return text_artifact(_help_text(config))
    return handle_set_command(user_text, config, CONFIG_PARAMETERS)


# ── Prompt-suggestion lists ───────────────────────────────────────────────────
# Canned quick-reply buttons surfaced where the user typically stalls: empty
# composer (starter prompts), image-without-text (edit suggestions), and
# post-generation (refinement suggestions).


# Caption shown alongside edit suggestions when the user uploads an image
# without typing instructions.
_NEED_PROMPT_TEXT = "Got the image — what would you like me to do with it?"


# Curated starter prompts. Lower the activation energy for the first turn —
# the chat model can answer more sharply when the user picks something
# concrete than when they have to invent a prompt from scratch.
_STARTER_SUGGESTIONS: list[dict[str, str]] = [
    {
        "label": "Moody product shot",
        "prompt": "A moody product shot of a perfume bottle on wet stone, "
                  "soft rim light, shallow depth of field.",
    },
    {
        "label": "90s anime poster",
        "prompt": "A 90s anime poster of a samurai cat at dusk, hand-drawn "
                  "look, halftone shading, dramatic lighting.",
    },
    {
        "label": "Three style variations",
        "prompt": "Generate three variations of a minimalist coffee shop "
                  "logo: one playful, one premium, one industrial.",
    },
    {"label": "Show capabilities", "prompt": "/help"},
]


# Edits to suggest when the user uploads a reference image without
# instructions. Each is a sensible standalone edit that works with most
# product / portrait / scene inputs.
_IMAGE_EDIT_SUGGESTIONS: list[dict[str, str]] = [
    {"label": "Make the lighting warmer", "prompt": "Make the lighting warmer."},
    {"label": "Replace the background", "prompt": "Replace the background with a clean marble counter."},
    {"label": "Watercolour style", "prompt": "Render this in a soft watercolour style."},
    {"label": "Three variations", "prompt": "Generate three variations of this image with different moods."},
]


# Follow-ups shown after a successful generation. Refinement-flavoured
# rather than fresh-start so the user can iterate on the image they just
# saw without retyping everything.
_REFINEMENT_SUGGESTIONS: list[dict[str, str]] = [
    {"label": "Warmer lighting", "prompt": "Make the lighting warmer."},
    {"label": "Cooler / moodier", "prompt": "Cool the palette and add a moodier atmosphere."},
    {"label": "Different angle", "prompt": "Try a different camera angle."},
    {"label": "Three variations", "prompt": "Give me three more variations in different styles."},
]


# ── Run preparation + result assembly ─────────────────────────────────────────
# Glue between input parsing and the agent run: ``_prepare_run`` builds the
# ``AgentDeps`` and a reference-aware prompt; ``_assemble_artifact`` flattens
# the streaming items into a single Artifact for the one-shot path.


def _prepare_run(
    user_text: str,
    attached_images: list[tuple[bytes, str]],
    config: dict[str, str],
    context: RequestContext,
) -> tuple[AgentDeps, str]:
    """Build deps + a prompt containing reference URLs the agent can use.

    Pre-stores the current-turn upload (and the most-recent prior image
    if the user is iterating) in image_store and exposes them to the
    agent via a ``Available references:`` preamble in the user prompt.

    The orchestrator only sees text — it cannot reference image bytes
    directly. Pre-storing surfaces them as URLs the chat model can pass
    to ``generate_image`` as ``reference_image_url``. Without this step
    the agent would have no way to "use the packshot the user just
    uploaded".
    """
    available_images: list[tuple[str, str]] = []

    for i, (data, mime) in enumerate(attached_images):
        image_id = image_store.put(data, media_type=mime)
        available_images.append((
            image_store.url_for(image_id),
            f"reference uploaded just now (#{i + 1})",
        ))

    if not attached_images:
        previous = _latest_image_in_history(context)
        if previous:
            data, mime = previous
            image_id = image_store.put(data, media_type=mime)
            available_images.append((
                image_store.url_for(image_id),
                "most recent image in this conversation",
            ))

    deps = AgentDeps(
        config=config,
        available_images=available_images,
    )

    # The active /set parameters are appended to the user prompt so the
    # orchestrator LLM is aware of them — without this, the model has no
    # context that the user has explicitly chosen a style or size, and
    # produces replies that ignore the active settings. The values are
    # *also* passed directly to ``generate_image`` via ``ctx.deps.config``,
    # so the model never has to forward them as tool arguments.
    sections: list[str] = []
    if available_images:
        ref_lines = [f"- {url} ({label})" for url, label in available_images]
        sections.append("Available references:\n" + "\n".join(ref_lines))
    sections.append("User: " + user_text)
    sections.append(format_active_settings(config, CONFIG_PARAMETERS))
    prompt_with_refs = "\n\n".join(sections)

    return deps, prompt_with_refs


def _assemble_artifact(
    text: str,
    generated: list[Artifact],
    *,
    with_refinement_suggestions: bool = False,
) -> Artifact:
    """Combine an agent text reply + generated images into one Artifact.

    Used by the non-streaming ``invoke`` entry point; the streaming path
    yields each item separately instead. The A2A one-shot contract
    returns a single Artifact, so multi-image plans ("three variations")
    have to be flattened into one ``parts`` list — each tool's image
    becomes a sibling part alongside the chat model's final text.

    When ``with_refinement_suggestions`` is set the post-generation
    follow-up suggestions are appended as a final data part, matching
    the streaming path's behaviour.
    """
    parts: list[Part] = []
    if text:
        parts.append(Part(text=text))
    for art in generated:
        for part in art.parts:
            parts.append(part)
    if with_refinement_suggestions:
        for part in prompt_suggestions_artifact(_REFINEMENT_SUGGESTIONS).parts:
            parts.append(part)
    if not parts:
        return text_artifact("(no response)")
    return Artifact(
        artifact_id=str(uuid.uuid4()),
        name="result",
        parts=parts,
    )


# ── A2A invoke entry points ───────────────────────────────────────────────────
# Public functions ``main.py`` wires into ``build_a2a_app``. Both share the
# same early-exit ladder (slash command → empty input → image-without-text)
# before falling through to the agent loop; they differ only in how results
# reach the wire (one Artifact vs. streamed yields).


async def invoke(prompt: str, context: RequestContext) -> Artifact:
    """Non-streaming entry. Drives the pydantic-ai loop and returns a single
    Artifact bundling the agent's text reply with every generated image.

    Required by the A2A protocol's one-shot ``SendMessage`` path; the
    streaming variant below covers ``SendStreamingMessage``. Both share
    setup (slash-command bypass, deps prep, agent.run) and only differ
    in how they return the result — one Artifact vs. multiple yields.
    """  # noqa: ARG001 — prompt is part of the framework contract
    user_text, attached_images = _extract_input(context)
    config = resolve_config_from_history(context, CONFIG_PARAMETERS)

    if response := _handle_slash_command(user_text, config):
        return response

    if not user_text and not attached_images:
        return prompt_suggestions_artifact(
            _STARTER_SUGGESTIONS,
            text="Tell me what to create, or attach a reference image and "
                 "describe what to change.",
        )
    if attached_images and not user_text:
        return prompt_suggestions_artifact(
            _IMAGE_EDIT_SUGGESTIONS, text=_NEED_PROMPT_TEXT,
        )

    deps, prompt_with_refs = _prepare_run(
        user_text, attached_images, config, context,
    )
    result = await image_agent.run(
        prompt_with_refs,
        deps=deps,
        message_history=_build_message_history(context),
    )
    return _assemble_artifact(
        str(result.output or "").strip(),
        deps.generated,
        with_refinement_suggestions=bool(deps.generated),
    )


async def stream_invoke(
    prompt: str, context: RequestContext,
) -> AsyncIterable[str | Artifact]:
    """Streaming entry. Yields each generated image as its own Artifact so
    they appear inline as the agent loop progresses, then yields the agent's
    final text reply. Tool-internal status flows via ``report_progress()``.

    The per-image yields matter for multi-step plans ("three variations") —
    without them the user would stare at a spinner until every image
    finished, then see them all at once. Yielding incrementally gives
    immediate feedback as each ``generate_image`` call completes.
    """  # noqa: ARG001
    user_text, attached_images = _extract_input(context)
    config = resolve_config_from_history(context, CONFIG_PARAMETERS)

    if response := _handle_slash_command(user_text, config):
        yield response
        return

    if not user_text and not attached_images:
        yield prompt_suggestions_artifact(
            _STARTER_SUGGESTIONS,
            text="Tell me what to create, or attach a reference image and "
                 "describe what to change.",
        )
        return
    if attached_images and not user_text:
        yield prompt_suggestions_artifact(
            _IMAGE_EDIT_SUGGESTIONS, text=_NEED_PROMPT_TEXT,
        )
        return

    deps, prompt_with_refs = _prepare_run(
        user_text, attached_images, config, context,
    )
    result = await image_agent.run(
        prompt_with_refs,
        deps=deps,
        message_history=_build_message_history(context),
    )

    for art in deps.generated:
        yield art

    final_text = str(result.output or "").strip()
    if final_text:
        # When the turn produced an image, fold the refinement suggestions
        # into the same bubble as the closing text so they read as one
        # response rather than a stray follow-up message.
        if deps.generated:
            yield prompt_suggestions_artifact(
                _REFINEMENT_SUGGESTIONS, text=final_text,
            )
        else:
            yield text_artifact(final_text)
    elif deps.generated:
        # No final text, but we did produce an image — still offer refinements.
        yield prompt_suggestions_artifact(_REFINEMENT_SUGGESTIONS)
