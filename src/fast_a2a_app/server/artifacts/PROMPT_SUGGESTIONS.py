"""PROMPT_SUGGESTIONS — clickable pill buttons for follow-up prompts.

Specialised artifact module. The package's autodiscover picks up the
two module-level attributes below:

  * ``tag``     — the value used as the ``_type`` discriminator.
  * ``builder`` — Python convenience wrapper around ``data_artifact``.
                  Re-exported at package level under its function name
                  (here, ``prompt_suggestions_artifact``).

The JS renderer that turns this artifact's payload into clickable pill
buttons lives separately in
``fast_a2a_app/ui/renderers/PROMPT_SUGGESTIONS.js`` — the two files
meet at the ``_type`` discriminator string only. If a specialised
module has no matching ``.js`` file, the chat UI falls back to the
generic key-value rendering for ``data_artifact``.

Each suggestion is a ``{label, prompt}`` pair: the label is the button
text, the prompt is the text sent as the user's next message when the
button is clicked.
"""
from __future__ import annotations

from a2a.types import Artifact

from ._core import data_artifact

tag = "PROMPT_SUGGESTIONS"


def prompt_suggestions_artifact(
    suggestions: list[dict[str, str]],
    *,
    text: str | None = None,
    name: str = "prompt_suggestions",
) -> Artifact:
    """Build an Artifact carrying clickable prompt suggestions for the chat UI.

    Each suggestion is a dict with ``label`` (display text) and ``prompt`` (the
    text sent as a user message when clicked). Wraps the suggestion list in a
    well-known data envelope (``{"_type": "PROMPT_SUGGESTIONS", ...}``); the
    bundled chat UI detects this envelope and renders the suggestions as
    clickable buttons instead of a key-value table.

    The ``_type`` discriminator follows the Pydantic convention — leading
    underscore on the *field name* avoids any clash with user-defined fields.

    Optional ``text`` is rendered as a markdown caption above the buttons.

    >>> prompt_suggestions_artifact(
    ...     [
    ...         {"label": "Use gpt-image-1-mini", "prompt": "/models gpt-image-1-mini"},
    ...         {"label": "Use gpt-image-1", "prompt": "/models gpt-image-1"},
    ...     ],
    ...     text="Pick a model:",
    ... )
    """
    return data_artifact(
        {"_type": tag, "suggestions": suggestions},
        name=name,
        text=text,
    )


builder = prompt_suggestions_artifact
