"""
fast_a2a_app.server.commons.config — conversation-scoped parameters via ``/set``.

Lets users switch runtime knobs (which model, output size, output style,
verbosity, …) at chat time without redeploying. Schema is user-owned data;
this module provides the four reusable pieces every implementation needs.

The schema shape (a plain ``dict`` — no new type)::

    {
        "model": {
            "description": "Image deployment used for generation.",
            "default": "gpt-image-1-mini",
            "values": {
                "gpt-image-1-mini": "Faster, lower-cost.",
                "gpt-image-1": "Higher fidelity, slower.",
            },
        },
        # … more parameters
    }

Wire-up in ``stream_invoke`` is five visible lines::

    from fast_a2a_app.server.commons.config import (
        resolve_config_from_history,
        handle_set_command,
        format_active_settings,
        ACTIVE_SETTINGS_CONVENTION,
    )

    async def stream_invoke(prompt, context):
        user_text = context.get_user_input()
        config = resolve_config_from_history(context, SCHEMA)
        if reply := handle_set_command(user_text, config, SCHEMA):
            yield reply
            return
        prompt_ext = f"{prompt}\\n\\n{format_active_settings(config, SCHEMA)}"
        deps = AgentDeps(config=config)
        result = await my_agent.run(prompt_ext, deps=deps)
        yield text_artifact(str(result.output))

And append :data:`ACTIVE_SETTINGS_CONVENTION` to your system prompt so the
LLM knows what the trailing block means.
"""
from __future__ import annotations

import re

from a2a.server.agent_execution import RequestContext
from a2a.types import Artifact, Role

from ..artifacts import prompt_suggestions_artifact, text_artifact


# ── Public API ────────────────────────────────────────────────────────────────

ACTIVE_SETTINGS_CONVENTION = """\
Active settings:
- Every user message ends with an "Active settings:" block listing the
  conversation-scoped parameters the user has selected via `/set`. These
  values are forwarded to your tools via deps — do NOT restate them
  inside tool prompt arguments.
- When a non-default value is in effect, briefly acknowledge it in your
  reply ("rendered in the active anime style").
- If the user explicitly asks for a one-off override in their message,
  follow the request for that turn without changing the persistent
  setting — and end your reply by suggesting `/set <param> <value>` if
  they want it to stick.
"""
"""Drop-in preamble for your system prompt explaining the ``Active settings:``
convention to the orchestrator LLM. Append it (or paraphrase it) so the model
knows the trailing block is automatic context — not something to restate."""


def resolve_config_from_history(
    context: RequestContext,
    schema: dict[str, dict],
) -> dict[str, str]:
    """Walk related-task history for ``/set <param> <value>`` commands.

    Latest valid assignment wins per parameter. Unset parameters fall back to
    ``schema[<name>]["default"]``. Matching is case-insensitive — the parameter
    name is normalised to lowercase, the value is preserved as-typed.

    A2A history is already persisted by ``fast_a2a_app`` (in MemoryTaskStore /
    Redis / Mongo / Postgres). Deriving config from past ``/set`` commands
    avoids a second persistence layer for what is effectively cached user
    input — and survives reloads and process restarts for free.
    """
    config = {name: spec["default"] for name, spec in schema.items()}
    for task in getattr(context, "related_tasks", None) or []:
        for msg in getattr(task, "history", None) or []:
            if getattr(msg, "role", 0) != Role.ROLE_USER:
                continue
            for part in getattr(msg, "parts", None) or []:
                if part.WhichOneof("content") != "text":
                    continue
                match = _SET_CMD.match(part.text.strip())
                if not match:
                    continue
                param = (match.group(1) or "").lower()
                value = match.group(2) or ""
                if (
                    param in schema
                    and value
                    and value in schema[param]["values"]
                ):
                    config[param] = value
    return config


def handle_set_command(
    user_text: str,
    config: dict[str, str],
    schema: dict[str, dict],
) -> Artifact | None:
    """Return the pill-wizard reply for ``/set`` commands, or ``None``.

    Matches:

    * ``/set`` → step 1: pills, one per parameter name + cancel.
    * ``/set <param>`` → step 2: pills, one per allowed value + cancel.
    * ``/set <param> <value>`` → confirmation (the assignment is recovered
      from history on the next turn by :func:`resolve_config_from_history`).
    * ``/set cancel`` → acknowledged at either step, no state change.

    Returns ``None`` when ``user_text`` is not a ``/set`` command; the caller
    falls through to its own routing (other slash commands, agent loop, …).

    The wizard reply is a single ``Artifact`` ready to ``yield`` from
    streaming ``stream_invoke`` (or ``return`` from non-streaming ``invoke``).
    """
    cmd = _SET_CMD.match(user_text)
    if not cmd:
        return None

    param = (cmd.group(1) or "").lower()
    value = cmd.group(2) or ""

    # /set — step 1: pick a parameter.
    if not param:
        return _set_step1_artifact(config, schema)

    # cancel pill clicked at either step.
    if param == "cancel":
        return text_artifact("Cancelled — no parameters changed.")

    if param not in schema:
        allowed = ", ".join(f"`{p}`" for p in schema)
        return text_artifact(
            f"Unknown parameter `{param}`. Valid parameters: {allowed}.\n\n"
            "Type `/set` to pick one."
        )

    # /set <param> — step 2: pick a value.
    if not value:
        return _set_step2_artifact(param, config, schema)

    # /set <param> <value> — assignment.
    if value not in schema[param]["values"]:
        allowed = ", ".join(f"`{v}`" for v in schema[param]["values"])
        return text_artifact(
            f"Unknown value `{value}` for `{param}`. Valid values: {allowed}.\n\n"
            f"Type `/set {param}` to pick one."
        )
    return text_artifact(
        f"✅ `{param}` set to `{value}` for this conversation."
    )


def format_active_settings(
    config: dict[str, str],
    schema: dict[str, dict],
    *,
    header: str = "Active settings:",
) -> str:
    """Render the resolved config as an LLM-facing block.

    Append the result to your user prompt (and append
    :data:`ACTIVE_SETTINGS_CONVENTION` to your system prompt) so the
    orchestrator LLM knows which conversation-scoped settings are active.
    Values that equal ``schema[<name>]["default"]`` are tagged ``(default)``.
    """
    lines = [
        f"- {name}: {config[name]}"
        + (" (default)" if config[name] == schema[name]["default"] else "")
        for name in schema
    ]
    return header + "\n" + "\n".join(lines)


# ── Private helpers ───────────────────────────────────────────────────────────

# Matches ``/set``, ``/set <param>``, and ``/set <param> <value>``. Input is
# case-insensitive; the handler lowercases the captured parameter name.
_SET_CMD = re.compile(r"^/set(?:\s+(\S+))?(?:\s+(\S+))?\s*$", re.IGNORECASE)


def _set_step1_artifact(
    config: dict[str, str], schema: dict[str, dict],
) -> Artifact:
    """``/set`` reply: one pill per parameter name + cancel."""
    suggestions = [
        {
            "label": f"{name} (now: {config[name]})",
            "prompt": f"/set {name}",
        }
        for name in schema
    ]
    suggestions.append({"label": "cancel", "prompt": "/set cancel"})
    return prompt_suggestions_artifact(
        suggestions,
        text="**Which parameter do you want to change?**",
    )


def _set_step2_artifact(
    param: str, config: dict[str, str], schema: dict[str, dict],
) -> Artifact:
    """``/set <param>`` reply: one pill per allowed value + cancel."""
    spec = schema[param]
    current = config[param]
    suggestions = [
        {
            "label": f"{value}{' ✓' if value == current else ''}",
            "prompt": f"/set {param} {value}",
        }
        for value in spec["values"]
    ]
    suggestions.append({"label": "cancel", "prompt": "/set cancel"})

    lines = [f"**Choose a value for `{param}`** — {spec['description']}", ""]
    for value, blurb in spec["values"].items():
        marker = "✅" if value == current else "•"
        suffix = "  *(default)*" if value == spec["default"] else ""
        lines.append(f"{marker} `{value}`{suffix} — {blurb}")
    return prompt_suggestions_artifact(suggestions, text="\n".join(lines))


__all__ = [
    "ACTIVE_SETTINGS_CONVENTION",
    "resolve_config_from_history",
    "handle_set_command",
    "format_active_settings",
]
