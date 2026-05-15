"""
agent.py — Holiday planning agent

A pydantic-ai agent that helps users plan their perfect holiday.
The agent gathers preferences through conversation and uses its tools to
build destination recommendations, day-by-day itineraries, and budget estimates.

All tools call ``report_progress()`` so the A2A chat UI shows a live status
indicator while the agent is working through each step.

Authentication uses AzureCliCredential (managed identity, CLI login, or
environment credentials — whatever is available in the environment).
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterable
from dataclasses import dataclass, field

from a2a.server.agent_execution import RequestContext
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Artifact,
    Role,
)
from google.protobuf.json_format import MessageToDict
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
from fast_a2a_app.server import report_progress
from fast_a2a_app.server.artifacts import (
    map_artifact,
    prompt_suggestions_artifact,
    text_artifact,
)

log = logging.getLogger(__name__)

# ── Agent card ────────────────────────────────────────────────────────────────

agent_card = AgentCard(
    name="Holiday Planner",
    description=(
        "AI-powered travel assistant that crafts personalised holiday itineraries, "
        "destination recommendations, budget estimates, and travel essentials."
    ),
    version="0.1.0",
    supported_interfaces=[AgentInterface(url=f"{APP_BASE_URL}/a2a/", protocol_binding="JSONRPC")],
    capabilities=AgentCapabilities(streaming=True),
    default_input_modes=["text"],
    default_output_modes=["text", "data"],   # "data" covers the MAP artifact
    skills=[
        AgentSkill(
            id="recommend_destinations",
            name="Recommend destinations",
            description="Suggests 2-3 tailored holiday destinations with pros, cons, and highlights.",
            tags=[],
        ),
        AgentSkill(
            id="create_itinerary",
            name="Create itinerary",
            description="Builds a day-by-day plan with restaurant picks and local tips.",
            tags=[],
        ),
        AgentSkill(
            id="estimate_budget",
            name="Estimate budget",
            description="Provides a cost breakdown table per person per day.",
            tags=[],
        ),
        AgentSkill(
            id="travel_essentials",
            name="Travel essentials",
            description="Covers visa requirements, health advice, weather, and packing tips.",
            tags=[],
        ),
    ],
)


# ── Azure OpenAI client ───────────────────────────────────────────────────────

_client = AsyncOpenAI(
    base_url=f"{AZURE_AI_BASE_URL}/openai/v1",
    api_key=get_bearer_token_provider(AzureCliCredential(), "https://ai.azure.com/.default"),
)
_model = OpenAIModel(AZURE_AI_DEPLOYMENT_NAME, provider=OpenAIProvider(openai_client=_client))

# ── Agent definition ──────────────────────────────────────────────────────────


@dataclass
class AgentDeps:
    """Per-turn state passed to tools via ``RunContext[AgentDeps]``.

    ``generated`` collects rich artifacts (maps, etc.) the tools want
    to surface alongside the LLM's narrative reply. ``main.py`` drains
    it after ``agent.run()`` returns and yields each artifact into the
    A2A stream.
    """

    generated: list[Artifact] = field(default_factory=list)


holiday_agent: Agent[AgentDeps] = Agent(
    model=_model,
    deps_type=AgentDeps,
    system_prompt="""You are an expert holiday planning assistant with deep knowledge
of travel destinations worldwide. You help users plan their perfect holiday by
understanding their preferences and creating personalised itineraries.

Your workflow:
1. Greet the user warmly and ask about their holiday vision — destination ideas,
   travel dates, duration, budget range, number of travellers, and interests
   (culture, nature, food, adventure, relaxation, etc.).
2. Once you have enough information, use recommend_destinations to suggest 2-3
   destinations that match their preferences, with pros and cons for each.
   The tool also drops a map onto the chat with a pin for each suggestion, so
   refer to "the map above" in your reply rather than re-listing coordinates.
3. When the user picks a destination, use create_itinerary to build a
   day-by-day plan tailored to their interests and duration.
4. Use estimate_budget to give them a realistic cost breakdown.
5. Answer follow-up questions about visas, packing, local tips, etc.

Be enthusiastic, specific, and practical. Include local gems, not just tourist traps.
If the user changes their mind or asks for alternatives, adapt gracefully.

The chat UI automatically appends 3 quick-reply buttons to every reply you
send, derived from your message text. Write your reply so 3 plausible
short user responses are obvious from it — that means: end with a clear
question or decision point whenever possible.""",
    end_strategy="exhaustive",
)


SUGGESTION_COUNT = 3


# ── Conversation history ──────────────────────────────────────────────────────


_HISTORY_MAX_MESSAGES = 16  # ≈8 turns — long enough to follow a planning thread


def build_message_history(context: RequestContext) -> list[ModelMessage]:
    """Convert A2A related-task history into pydantic-ai ModelMessages.

    Without this, ``holiday_agent.run()`` only sees the current user turn
    and replays the same opening greeting + suggestion set every time —
    it has no memory of what the user already said. Walking
    ``related_tasks`` chronologically yields one ``ModelRequest`` (user)
    + one ``ModelResponse`` (agent text) per turn so the chat model can
    actually advance the conversation.

    Capped at the most recent :data:`_HISTORY_MAX_MESSAGES` so token
    spend stays predictable on long planning sessions.

    Why not the library helpers (``fast_a2a_app.get_task_history`` /
    ``format_history``)? Two reasons:

    1. They produce **strings** for the default prompt builder's
       prefix-mode. pydantic-ai's ``message_history=`` parameter wants
       typed ``ModelMessage`` / ``ModelRequest`` / ``ModelResponse``
       objects, which the framework deliberately doesn't know about.
    2. ``get_task_history`` sources agent text from BOTH
       ``task.history`` and ``task.artifacts`` — i.e. it includes
       transient status messages emitted via ``report_progress``
       ("Searching destinations…") as if they were agent replies. We
       only want the artifact-level text the user actually saw, so
       agent text is filtered to ``task.artifacts``.
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

        # Agent reply — combine all text parts across artifacts.
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


# ── Tools ─────────────────────────────────────────────────────────────────────


@holiday_agent.tool
async def recommend_destinations(
    ctx: RunContext[AgentDeps],
    interests: str,
    duration_days: int,
    budget_level: str,
    travel_month: str,
) -> str:
    """Generate 2-3 tailored destination recommendations.

    Side-effect: appends a ``MAP`` artifact to ``ctx.deps.generated``
    with a pin per recommendation so the user sees where each
    destination is on a real map.

    Args:
        interests: Comma-separated list of interests (culture, food, beach, hiking, etc.)
        duration_days: Total trip length in days
        budget_level: 'budget', 'moderate', or 'luxury'
        travel_month: Month of travel (e.g. 'July', 'December')
    """
    report_progress(f"Finding destinations for {duration_days}-day {budget_level} trip in {travel_month}…")

    prompt = f"""Generate exactly 2-3 holiday destination recommendations as a JSON array.

Context:
- Interests: {interests}
- Duration: {duration_days} days
- Budget level: {budget_level}
- Travel month: {travel_month}

For each destination return:
{{
  "name": "City, Country",
  "lat": <decimal latitude of the city centre, e.g. 41.9028>,
  "lng": <decimal longitude of the city centre, e.g. 12.4964>,
  "tagline": "One-line hook",
  "why_it_fits": "2-3 sentences matching their interests/budget/season",
  "highlight": "The single must-do experience",
  "consideration": "One honest trade-off or challenge",
  "best_for": "Type of traveller this suits most"
}}

``lat`` and ``lng`` MUST be numeric (not strings). They are used to drop
pins on a real map for the user — if you don't know the coordinates for
a destination, pick a different one rather than guessing.

Return ONLY the JSON array, no markdown."""

    response = await _client.chat.completions.create(
        model=AZURE_AI_DEPLOYMENT_NAME,
        max_completion_tokens=1024,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    'Respond with a JSON object {"destinations": [...]} '
                    "where the array follows the schema in the user's prompt."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    )
    raw = (response.choices[0].message.content or "").strip()

    try:
        parsed = json.loads(raw)
        destinations = (
            parsed["destinations"] if isinstance(parsed, dict) and "destinations" in parsed
            else parsed
        )
        if not isinstance(destinations, list):
            raise ValueError("destinations payload is not a list")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        log.warning("recommend_destinations: model returned non-JSON; surfacing raw text")
        return raw

    # Map artifact — drop pins for the destinations the model gave us
    # coordinates for. ``map_artifact`` silently drops markers with
    # missing/invalid coords, so a partial response still produces a
    # useful map rather than failing the whole tool.
    markers = [
        {
            "lat": d.get("lat"),
            "lng": d.get("lng"),
            "label": d.get("name"),
            "popup": (
                f"{d.get('name', '')}\n{d.get('tagline', '')}\n\n{d.get('why_it_fits', '')}"
            ).strip(),
        }
        for d in destinations
        if isinstance(d, dict)
    ]
    if markers:
        ctx.deps.generated.append(map_artifact(
            markers,
            caption=(
                f"Suggested destinations for a {duration_days}-day "
                f"{budget_level} trip in {travel_month}:"
            ),
        ))

    # Markdown summary for the LLM's reply context.
    lines = [f"**Destination Recommendations for a {duration_days}-day {budget_level} trip:**\n"]
    for i, d in enumerate(destinations, 1):
        if not isinstance(d, dict):
            continue
        lines.append(f"### {i}. {d.get('name', 'Unknown')}")
        if d.get("tagline"):
            lines.append(f"*{d['tagline']}*\n")
        if d.get("why_it_fits"):
            lines.append(f"**Why it fits:** {d['why_it_fits']}")
        if d.get("highlight"):
            lines.append(f"**Must-do:** {d['highlight']}")
        if d.get("consideration"):
            lines.append(f"**Consider:** {d['consideration']}")
        if d.get("best_for"):
            lines.append(f"**Best for:** {d['best_for']}\n")
    return "\n".join(lines)


@holiday_agent.tool_plain
async def create_itinerary(
    destination: str,
    duration_days: int,
    interests: str,
    pace: str = "moderate",
) -> str:
    """Build a detailed day-by-day itinerary for the chosen destination.

    Args:
        destination: City and country (e.g. 'Lisbon, Portugal')
        duration_days: Number of days at this destination
        interests: Comma-separated interests to prioritise
        pace: 'relaxed', 'moderate', or 'packed'
    """
    report_progress(f"Building {duration_days}-day itinerary for {destination}…")

    prompt = f"""Create a {duration_days}-day holiday itinerary for {destination}.

Traveller interests: {interests}
Pace preference: {pace}

Format as markdown with:
- A brief intro paragraph (2 sentences)
- One section per day: "## Day N: [Theme]"
  - Morning, Afternoon, Evening subsections
  - 2-3 specific activities per time slot with vivid 1-line descriptions
  - One restaurant recommendation per day with a dish to try
  - One local tip per day (something most tourists miss)
- A "Getting Around" tip at the end

Be specific — use real place names, real restaurants, real neighbourhoods.
Keep it exciting and practical."""

    response = await _client.chat.completions.create(
        model=AZURE_AI_DEPLOYMENT_NAME,
        max_completion_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return (response.choices[0].message.content or "").strip()


@holiday_agent.tool_plain
async def estimate_budget(
    destination: str,
    duration_days: int,
    travellers: int,
    budget_level: str,
) -> str:
    """Provide a realistic cost breakdown for the trip.

    Args:
        destination: City and country
        duration_days: Length of stay
        travellers: Number of people travelling
        budget_level: 'budget', 'moderate', or 'luxury'
    """
    report_progress(f"Calculating budget for {travellers} traveller(s) in {destination}…")

    prompt = f"""Create a realistic budget estimate for:
- Destination: {destination}
- Duration: {duration_days} days
- Travellers: {travellers} person(s)
- Budget style: {budget_level}

Return a markdown table with these categories and per-person daily costs:
| Category | Per Person / Day | {duration_days}-Day Total (per person) | Notes |
|----------|-----------------|----------------------------------------|-------|
| Accommodation | | | |
| Food & drink | | | |
| Local transport | | | |
| Activities & entrance fees | | | |
| Miscellaneous | | | |
| **TOTAL** | | | |

After the table, add:
- A "Money-saving tip" specific to this destination
- A "Splurge on this" recommendation worth the extra cost
- Currency and current exchange rate note

Use realistic local prices, not tourist-trap prices."""

    response = await _client.chat.completions.create(
        model=AZURE_AI_DEPLOYMENT_NAME,
        max_completion_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return (response.choices[0].message.content or "").strip()


@holiday_agent.tool_plain
async def get_travel_essentials(
    destination: str,
    nationality: str,
    travel_month: str,
) -> str:
    """Return visa, health, weather, and packing essentials for the destination.

    Args:
        destination: City and country
        nationality: Traveller's passport nationality (e.g. 'German', 'British')
        travel_month: Month of travel
    """
    report_progress(f"Looking up travel essentials for {destination}…")

    prompt = f"""Provide practical travel essentials for a {nationality} citizen visiting {destination} in {travel_month}.

Format as markdown with these sections:

## Visa & Entry
- Visa requirement for {nationality} citizens
- How to apply (if needed), cost, processing time

## Health & Safety
- Recommended vaccinations
- Any health precautions
- Emergency number

## Weather in {travel_month}
- Typical temperature range
- What to expect (rain, sun, humidity)
- Any weather warnings

## Packing Essentials
- 5 items specific to this destination and season
- What NOT to bring (local customs or restrictions)

## Practical Tips
- Local currency and payment habits
- Tipping culture
- One cultural do and one cultural don't

Keep it concise and actionable."""

    response = await _client.chat.completions.create(
        model=AZURE_AI_DEPLOYMENT_NAME,
        max_completion_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return (response.choices[0].message.content or "").strip()


# ── Quick-reply suggestion generator ──────────────────────────────────────────


_SUGGESTIONS_SYSTEM = (
    "You generate exactly 3 quick-reply buttons that fit as the user's "
    "next message in a holiday-planning chat. Read the assistant's most "
    "recent reply AND the structured artifacts the assistant rendered "
    "alongside it (maps with destination pins, tables of options, …).\n\n"
    "If the artifacts give the user a concrete, enumerable set of "
    "choices (e.g. 3 destination pins on a map), produce one button "
    "per choice so the user can advance with a single click. Use the "
    "exact destination / option name in the label and a natural "
    "first-person sentence in the prompt, e.g.\n"
    '  {"label": "Pick Rome",   "prompt": "Let\'s go with Rome."}\n'
    '  {"label": "Pick Athens", "prompt": "Let\'s go with Athens."}\n\n'
    "Otherwise propose 3 short, distinct, plausible responses to the "
    "assistant's question or offer.\n\n"
    "Output strictly JSON with this shape:\n"
    '  {"suggestions": [{"label": "...", "prompt": "..."}, ...]}\n'
    "Labels are 2-5 words (button caption); prompts are the full reply "
    "text the user would send back. No prose, no markdown, JSON only."
)


def _summarise_artifacts(generated: list[Artifact]) -> str:
    """Render a compact textual summary of the rich artifacts the agent
    just emitted, so the suggestion-generator can ground its buttons in
    the concrete choices the user can see on-screen.

    Recognises the framework's typed envelopes by ``_type``:

      * ``MAP`` — lists the pin labels so suggestions can be
        "Pick <city>" one per destination.
      * ``TABLE`` — names the columns + row count so the LLM can offer
        per-row drill-downs.
      * Other typed data — bare ``_type`` tag, just enough for the LLM
        to know there's structured context above the chat text.

    Returns an empty string when nothing meaningful is on-screen — the
    suggestion-generator then falls back to plain text grounding.
    """
    if not generated:
        return ""

    lines: list[str] = []
    for art in generated:
        for part in (art.parts or []):
            if part.WhichOneof("content") != "data":
                continue
            try:
                data = MessageToDict(part).get("data", {})
            except Exception:  # noqa: BLE001 — surface what we can, skip what we can't
                continue
            tag = data.get("_type")
            if tag == "MAP":
                markers = data.get("markers") or []
                labels = [
                    str(m.get("label") or f"{m.get('lat')},{m.get('lng')}")
                    for m in markers
                    if isinstance(m, dict)
                ]
                if labels:
                    lines.append(
                        f"MAP with {len(labels)} pin(s): " + "; ".join(labels)
                    )
            elif tag == "TABLE":
                cols = data.get("columns") or []
                rows = data.get("rows") or []
                lines.append(
                    f"TABLE ({len(rows)} rows × {len(cols)} cols): "
                    + ", ".join(str(c) for c in cols)
                )
            elif tag:
                lines.append(f"data artifact (_type={tag})")
    return "\n".join(lines)


async def generate_suggestions(
    agent_reply: str,
    generated: list[Artifact] | None = None,
) -> list[dict[str, str]]:
    """Generate exactly 3 quick-reply buttons grounded in the agent's
    actual reply AND any rich artifacts the agent rendered alongside it.

    When ``generated`` carries a MAP / TABLE / typed data artifact, the
    suggestions become one-click picks over the concrete options the
    user can see (e.g. 3 destination pins → "Pick Rome" / "Pick Athens"
    / "Pick Lisbon"). When only text is on-screen, the buttons revert
    to plausible free-form responses to the assistant's question.

    One small LLM round-trip per turn — running this *after*
    ``agent.run()`` avoids the agent having to predict its own reply.
    Returns ``[]`` on parse / API failure so a flaky generator can't
    break the turn (the UI then renders the agent's text bubble alone).
    """
    if not agent_reply.strip():
        return []

    artifact_summary = _summarise_artifacts(generated or [])
    user_msg = f"ASSISTANT REPLY:\n{agent_reply.strip()}"
    if artifact_summary:
        user_msg += (
            "\n\nARTIFACTS RENDERED ALONGSIDE THE REPLY (use these to "
            "ground the buttons in concrete user-visible choices):\n"
            + artifact_summary
        )

    try:
        completion = await _client.chat.completions.create(
            model=AZURE_AI_DEPLOYMENT_NAME,
            max_completion_tokens=400,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SUGGESTIONS_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        )
        raw = (completion.choices[0].message.content or "").strip()
        data = json.loads(raw)
        items = data.get("suggestions") or []
        out: list[dict[str, str]] = []
        for item in items[:3]:
            label = str(item.get("label", "")).strip()
            prompt = str(item.get("prompt", label)).strip()
            if label and prompt:
                out.append({"label": label, "prompt": prompt})
        return out if len(out) == 3 else []
    except Exception:  # noqa: BLE001 — generator must not break the turn
        return []


# ── A2A invoke entry points ───────────────────────────────────────────────────


async def invoke(prompt: str, context: RequestContext) -> Artifact:
    """Non-streaming entry — one-shot ``SendMessage`` path.

    The streaming path is where the per-artifact UX really lands (maps
    appear above the narrative). For the one-shot path, callers only
    see the primary reply; rich artifacts stored in ``deps.generated``
    are silently dropped because A2A's one-shot contract returns a
    single Artifact.
    """
    deps = AgentDeps()
    result = await holiday_agent.run(
        prompt, deps=deps, message_history=build_message_history(context),
    )
    text = str(result.output or "").strip()
    suggestions = await generate_suggestions(text, deps.generated)
    if suggestions:
        return prompt_suggestions_artifact(suggestions, text=text or None)
    return text_artifact(text or "(no response)")


async def stream_invoke(
    prompt: str, context: RequestContext,
) -> AsyncIterable[Artifact]:
    """Streaming entry — yields each rich artifact in order, then the
    closing text + suggestions.

    ``run()`` (not ``run_stream()``): executes the full tool-call chain
    before returning. ``run_stream()`` stops at the first text output
    and would skip tools like ``recommend_destinations``.

    Tools push rich artifacts (e.g. ``recommend_destinations`` dropping
    a map of suggestions) into ``deps.generated``. We yield them in
    order before the closing text + suggestions so the map appears
    above the narrative.
    """
    deps = AgentDeps()
    result = await holiday_agent.run(
        prompt, deps=deps, message_history=build_message_history(context),
    )
    for art in deps.generated:
        yield art

    text = str(result.output or "").strip()
    suggestions = await generate_suggestions(text, deps.generated)
    if suggestions:
        yield prompt_suggestions_artifact(suggestions, text=text or None)
    elif text:
        yield text_artifact(text)
