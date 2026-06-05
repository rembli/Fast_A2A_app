"""DOCUMENT — single-card document viewer with thumbnail + paginated preview.

Specialised artifact module. The package's autodiscover picks up the
two module-level attributes below:

  * ``tag``     — the value used as the ``_type`` discriminator.
  * ``builder`` — Python convenience wrapper around ``data_artifact``.
                  Re-exported at package level as ``document_artifact``.

The JS renderer that turns this artifact's payload into a card with a
thumbnail viewer, prev/next chrome, download chip, and click-to-expand
fullscreen page reader lives separately in
``fast_a2a_app/ui/renderers/DOCUMENT.js``. The two files meet at the
``_type`` discriminator string only.

Data shape:

    {
        "_type": "DOCUMENT",
        "documents": [
            {
                "filename":     "quarterly-update.pptx",
                "downloadUrl":  "/download/<ctx>/quarterly-update.pptx",
                "thumbnailUrl"?: "/download/<ctx>/.previews/.../slide-1.png",
                "pages"?:       ["/download/<ctx>/.previews/.../slide-1.png", ...],
                "mediaType"?:   "application/vnd.openxmlformats-officedocument...",
                "sizeBytes"?:   31415,
            },
            ...
        ],
    }

``thumbnailUrl`` is the cover image shown in the card. ``pages`` is the
list of full-resolution page images shown in the fullscreen modal —
when non-empty the card becomes click-to-expand. Both are optional;
omit them for download-only cards.
"""
from __future__ import annotations

from typing import Mapping, Sequence

from a2a.types import Artifact

from ._core import data_artifact

tag = "DOCUMENT"


def _coerce_document(d: Mapping) -> dict | None:
    """Normalise one document entry; drop entries without a filename."""
    filename = d.get("filename")
    if not filename:
        return None
    out: dict = {"filename": str(filename)}
    if d.get("downloadUrl"):
        out["downloadUrl"] = str(d["downloadUrl"])
    if d.get("thumbnailUrl"):
        out["thumbnailUrl"] = str(d["thumbnailUrl"])
    if d.get("mediaType"):
        out["mediaType"] = str(d["mediaType"])
    size = d.get("sizeBytes")
    if isinstance(size, (int, float)) and size >= 0:
        out["sizeBytes"] = int(size)
    pages = d.get("pages")
    if isinstance(pages, Sequence) and not isinstance(pages, (str, bytes)):
        out["pages"] = [str(p) for p in pages if p]
    return out


def document_artifact(
    documents: Sequence[Mapping],
    *,
    caption: str | None = None,
    name: str = "document",
) -> Artifact:
    """Build a document-viewer Artifact rendered as a single inline card.

    Use this when the agent has produced one or more concrete Office /
    PDF documents the user should *look at right now* — the card shows
    a thumbnail, a filename caption, prev/next chrome (when more than
    one document is supplied), and a download chip. When ``pages`` is
    non-empty the thumbnail is clickable and opens a fullscreen modal
    that vertically stacks every page for native-scroll reading.

    Each ``documents`` entry is a dict with at least ``filename``.
    Optional fields:

      * ``downloadUrl`` — link the download chip points at.
      * ``thumbnailUrl`` — cover image shown in the card body.
      * ``pages`` — list of full-resolution page-image URLs surfaced in
        the fullscreen modal.
      * ``mediaType`` — RFC 6838 media type (informational).
      * ``sizeBytes`` — non-negative integer; formatted as B / KB / MB.

    Args:
        documents: Iterable of document dicts. Entries without a
            ``filename`` are dropped silently — partial agent output
            should still produce a useful card.
        caption: Optional one-line label rendered as a markdown bubble
            immediately above the card.
        name: Artifact name (used by clients filtering on artifact
            metadata; doesn't affect rendering).

    >>> document_artifact(
    ...     [
    ...         {
    ...             "filename": "quarterly-update.pptx",
    ...             "downloadUrl": "/download/abc/quarterly-update.pptx",
    ...             "thumbnailUrl": "/download/abc/.previews/qu/slide-1.png",
    ...             "pages": [
    ...                 "/download/abc/.previews/qu/slide-1.png",
    ...                 "/download/abc/.previews/qu/slide-2.png",
    ...             ],
    ...             "mediaType": (
    ...                 "application/vnd.openxmlformats-officedocument"
    ...                 ".presentationml.presentation"
    ...             ),
    ...             "sizeBytes": 31415,
    ...         },
    ...     ],
    ...     caption="Here's your deck.",
    ... )
    """
    cleaned = [d for d in (_coerce_document(d) for d in documents) if d is not None]
    return data_artifact(
        {"_type": tag, "documents": cleaned},
        name=name,
        text=caption,
    )


builder = document_artifact
