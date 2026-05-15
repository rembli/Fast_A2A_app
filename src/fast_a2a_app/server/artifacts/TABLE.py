"""TABLE — Tabular data, rendered as an HTML ``<table>``.

Specialised artifact module. The package's autodiscover picks up the
two module-level attributes below:

  * ``tag``     — the value used as the ``_type`` discriminator.
  * ``builder`` — Python convenience wrapper around ``data_artifact``.
                  Re-exported at package level under its function name
                  (here, ``table_artifact``).

The JS renderer that turns this artifact's payload into a ``<table>``
lives separately in
``fast_a2a_app/ui/renderers/TABLE.js`` — the two files meet at the
``_type`` discriminator string only. If a specialised module has no
matching ``.js`` file, the chat UI falls back to the generic
key-value rendering for ``data_artifact``.

Adding your own type? Copy this file as ``YOURTAG.py`` and (optionally)
add ``ui/renderers/YOURTAG.js`` for a bespoke renderer.
"""
from __future__ import annotations

from typing import Any

from a2a.types import Artifact

from ._core import data_artifact

tag = "TABLE"


def table_artifact(
    rows: list[list[Any]],
    *,
    columns: list[str] | None = None,
    caption: str | None = None,
    name: str = "table",
) -> Artifact:
    """Build a tabular Artifact rendered by the chat UI as a real HTML ``<table>``.

    Distinct from a generic :func:`data_artifact`: a table has a fixed
    ``(rows, columns)`` shape, the UI renders it as ``<table>`` with
    column headers and proper cell typography (right-aligned numbers,
    monospace identifiers) instead of as a key-value block. The
    ``_type`` discriminator is enforced as ``"TABLE"`` so the UI can
    branch on it deterministically.

    Args:
        rows: List of rows. Each row is a list of cells. Cell values
            should be JSON-friendly scalars (``str | int | float |
            bool | None``); non-finite floats (``NaN`` / ``±Inf``) are
            scrubbed to ``None`` automatically.
        columns: Column headers. If omitted, headers are auto-derived
            as ``["col_1", "col_2", …]`` from the row width.
        caption: Optional one-line label rendered immediately above
            the table. Keep it short — a sentence at most. The agent's
            narrative reply belongs in a separate :func:`text_artifact`
            (or in the agent's natural reply channel), not here.
        name: Artifact name (used by clients filtering on artifact
            metadata; doesn't affect rendering).

    >>> table_artifact(
    ...     rows=[["APAC", 38400], ["EMEA", 22000]],
    ...     columns=["region", "revenue"],
    ...     caption="Top 2 regions by revenue",
    ... )
    """
    inferred_cols = columns
    if inferred_cols is None:
        width = max((len(r) for r in rows), default=0)
        inferred_cols = [f"col_{i + 1}" for i in range(width)]
    return data_artifact(
        {"_type": tag, "columns": list(inferred_cols), "rows": list(rows)},
        name=name,
        text=caption,
    )


builder = table_artifact
