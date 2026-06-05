"""DOCUMENTS — always-on workspace file panel.

Specialised artifact module. The package's autodiscover picks up the
two module-level attributes below:

  * ``tag``     — the value used as the ``_type`` discriminator.
  * ``builder`` — Python convenience wrapper around ``data_artifact``.
                  Re-exported at package level as ``documents_artifact``.

The JS renderer that turns this artifact's payload into a fixed
right-edge side panel lives separately in
``fast_a2a_app/ui/renderers/DOCUMENTS.js``. Instead of inserting a
card into the chat scroll, the renderer maintains a collapsible
workspace panel and silently updates it every time the agent emits a
``DOCUMENTS`` artifact — emit it at the end of every turn to keep the
panel mirrored to current workspace state.

Data shape:

    {
        "_type": "DOCUMENTS",
        "documents": [
            {
                "filename":    "quarterly-update.pptx",
                "downloadUrl": "/download/<ctx>/quarterly-update.pptx",
                "mediaType"?:  "application/vnd.openxmlformats-officedocument...",
                "sizeBytes"?:  31415,
                "modifiedAt"?: "2026-05-22T23:04:50Z",
                "viewable"?:   true,
                "versions"?:   [                # newest-first
                    {
                        "filename":    "quarterly-update.<ts>.pptx",
                        "downloadUrl": "/download/<ctx>/.versions/...",
                        "sizeBytes"?:  30912,
                        "modifiedAt"?: "2026-05-22T23:04:50Z",
                        "viewable"?:   true,
                    },
                    ...
                ],
            },
            ...
        ],
    }

``viewable`` defaults to ``true`` — set it to ``false`` for files the
agent only knows how to hand back as a download (no in-chat preview).
``versions`` is the optional per-file history surfaced behind a small
chevron in each row; it stays a transient inspection affordance, so
older versions can be elided when the workspace gets large.
"""
from __future__ import annotations

from typing import Mapping, Sequence

from a2a.types import Artifact

from ._core import data_artifact

tag = "DOCUMENTS"


def _coerce_entry(d: Mapping) -> dict | None:
    """Normalise one workspace entry; drop entries without a filename."""
    filename = d.get("filename")
    if not filename:
        return None
    out: dict = {"filename": str(filename)}
    if d.get("downloadUrl"):
        out["downloadUrl"] = str(d["downloadUrl"])
    if d.get("mediaType"):
        out["mediaType"] = str(d["mediaType"])
    size = d.get("sizeBytes")
    if isinstance(size, (int, float)) and size >= 0:
        out["sizeBytes"] = int(size)
    if d.get("modifiedAt"):
        out["modifiedAt"] = str(d["modifiedAt"])
    if "viewable" in d:
        out["viewable"] = bool(d["viewable"])
    return out


def _coerce_document(d: Mapping) -> dict | None:
    """Top-level document, plus optional newest-first ``versions`` list."""
    entry = _coerce_entry(d)
    if entry is None:
        return None
    versions = d.get("versions")
    if isinstance(versions, Sequence) and not isinstance(versions, (str, bytes)):
        cleaned = [
            v for v in (_coerce_entry(v) for v in versions) if v is not None
        ]
        if cleaned:
            entry["versions"] = cleaned
    return entry


def documents_artifact(
    documents: Sequence[Mapping],
    *,
    name: str = "documents",
) -> Artifact:
    """Build a workspace-panel Artifact rendered as a fixed right-edge file list.

    Unlike :func:`document_artifact` — which drops a viewer card into
    the chat scroll for files the user should look at *now* — this
    artifact maintains a persistent side panel reflecting the current
    workspace contents. Emit it at the end of every turn (after any
    file mutation) so the panel stays mirrored to reality without the
    user having to ask.

    Each ``documents`` entry is a dict with at least ``filename``.
    Optional fields:

      * ``downloadUrl`` — link the row's download chip points at.
      * ``mediaType`` — RFC 6838 media type (informational; the
        renderer picks a file icon from the extension instead).
      * ``sizeBytes`` — non-negative integer; formatted as B / KB / MB.
      * ``modifiedAt`` — ISO-8601 timestamp; rendered as a relative
        clock-time today, falling back to ``Mon DD`` for older dates.
      * ``viewable`` — ``False`` hides the in-chat preview affordance
        and labels the row "download only". Defaults to ``True``.
      * ``versions`` — newest-first list of prior versions (same entry
        shape as the top-level document). Surfaced behind a small
        chevron toggle on the row.

    Args:
        documents: Iterable of document dicts. Entries without a
            ``filename`` are dropped silently.
        name: Artifact name (used by clients filtering on artifact
            metadata; doesn't affect rendering).

    >>> documents_artifact(
    ...     [
    ...         {
    ...             "filename": "quarterly-update.pptx",
    ...             "downloadUrl": "/download/abc/quarterly-update.pptx",
    ...             "sizeBytes": 31415,
    ...             "modifiedAt": "2026-05-22T23:04:50Z",
    ...             "versions": [
    ...                 {
    ...                     "filename":
    ...                         "quarterly-update.20260522T230450123456.pptx",
    ...                     "downloadUrl":
    ...                         "/download/abc/.versions/"
    ...                         "quarterly-update.20260522T230450123456.pptx",
    ...                     "sizeBytes": 30912,
    ...                     "modifiedAt": "2026-05-22T23:04:50Z",
    ...                 },
    ...             ],
    ...         },
    ...     ],
    ... )
    """
    cleaned = [d for d in (_coerce_document(d) for d in documents) if d is not None]
    return data_artifact(
        {"_type": tag, "documents": cleaned},
        name=name,
    )


builder = documents_artifact
