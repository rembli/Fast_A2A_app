"""_core — Embedded A2A artifact primitives.

The four "embedded" builders here wrap the A2A protocol Parts directly
— ``text`` / ``data`` / ``file`` / ``image`` — and every other
artifact in this package composes on top of one of them. They are
*not* registered with the typed-data renderer registry: they don't
carry a ``_type`` discriminator, the chat UI dispatches them via the
protobuf ``content`` oneof (``text`` / ``data`` / ``url`` / ``raw``)
and (for images) media-type sniffing inside ``renderFilePartEl``.

``image_artifact`` lives here rather than in a sibling ``IMAGE.py``
because it has no ``_type`` envelope and no specialised JS renderer
contract — it's just ``file_artifact`` with image-friendly defaults.

Specialised artifacts (``TABLE``, ``PROMPT_SUGGESTIONS``, custom
application types) live in dedicated sibling modules and register a
``_type`` tag + JS renderer with the package's data-type registry.
"""
from __future__ import annotations

import math
import uuid
from typing import Any

from a2a.types import Artifact, Part
from google.protobuf.struct_pb2 import Struct, Value


def _new_artifact_id() -> str:
    return str(uuid.uuid4())


def _scrub_non_finite(value: Any) -> Any:
    """Recursively replace ``NaN`` / ``±Inf`` with ``None``.

    The downstream A2A task store serialises tasks via
    ``MessageToJson``, which strictly rejects non-finite floats because
    they have no native JSON representation. pandas — and any agent
    handling missing data — routinely produces them, so we scrub once
    at the artifact-construction boundary instead of asking every agent
    to remember.

    Detection uses the IEEE identity ``v != v`` (true only for NaN)
    plus an explicit ``isinf`` check, so this catches Python ``float``,
    every ``numpy.floating`` subtype (including ``numpy.float32``,
    which is *not* a Python ``float`` subclass), and other duck-typed
    NaN values.
    """
    if isinstance(value, dict):
        return {k: _scrub_non_finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_non_finite(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub_non_finite(v) for v in value)
    if isinstance(value, bool):
        return value
    try:
        if value != value:           # NaN ≠ NaN, by IEEE
            return None
    except TypeError:
        return value
    try:
        if math.isinf(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _dict_to_value(data: dict[str, Any]) -> Value:
    """Wrap a JSON-compatible dict as a protobuf ``Value`` (struct).

    Non-finite floats (``NaN`` / ``±Inf``) are scrubbed to ``None``
    because the A2A task store serialises tasks via ``MessageToJson``
    and the protobuf JSON encoder refuses non-finite values — they have
    no representation in standard JSON. Agents that surface missing
    pandas cells don't have to think about this.
    """
    struct = Struct()
    struct.update(_scrub_non_finite(data))
    return Value(struct_value=struct)


def text_artifact(text: str, *, name: str = "result") -> Artifact:
    """Build a text-only Artifact, rendered as a markdown bubble.

    >>> text_artifact("Hello!")
    """
    return Artifact(
        artifact_id=_new_artifact_id(),
        name=name,
        parts=[Part(text=text)],
    )


def data_artifact(
    data: dict[str, Any],
    *,
    name: str = "data",
    text: str | None = None,
) -> Artifact:
    """Build a structured-data Artifact.

    Without a ``_type`` discriminator, the chat UI renders the dict as
    a labeled key-value block. With a registered ``_type`` (see
    :mod:`fast_a2a_app.server.artifacts.TABLE` and friends), the UI
    dispatches to a specialised renderer.

    Pass any JSON-compatible dict (nested dicts, lists, scalars all
    work). An optional ``text`` caption is rendered as a markdown
    bubble before the data widget.

    >>> data_artifact({"count": 42, "ok": True}, text="Run finished:")
    """
    parts: list[Part] = []
    if text:
        parts.append(Part(text=text))
    parts.append(Part(data=_dict_to_value(data)))
    return Artifact(
        artifact_id=_new_artifact_id(),
        name=name,
        parts=parts,
    )


def file_artifact(
    content: bytes | None = None,
    *,
    url: str | None = None,
    filename: str,
    media_type: str,
    name: str | None = None,
    text: str | None = None,
) -> Artifact:
    """Build a file Artifact, rendered as a download card.

    Provide exactly one of ``content`` (inline bytes) or ``url`` (reference
    to an externally-hosted resource). The URL form keeps the wire
    transcript compact when the bytes are already stored in an object
    store / CDN / sibling FastAPI endpoint. Image media types (``image/*``)
    are also rendered inline as an ``<img>`` preview by the chat UI.

    >>> file_artifact(b"...", filename="report.pdf", media_type="application/pdf")
    >>> file_artifact(url="/files/abc.pdf",
    ...               filename="abc.pdf", media_type="application/pdf")
    """
    if (content is None) == (url is None):
        raise ValueError("Provide exactly one of `content` or `url`.")

    parts: list[Part] = []
    if text:
        parts.append(Part(text=text))
    if content is not None:
        parts.append(Part(raw=content, filename=filename, media_type=media_type))
    else:
        parts.append(Part(url=url, filename=filename, media_type=media_type))
    return Artifact(
        artifact_id=_new_artifact_id(),
        name=name or filename,
        parts=parts,
    )


# Common media types → file extensions for image_artifact's auto-naming.
_IMAGE_EXT_BY_MIME: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/svg+xml": "svg",
    "image/avif": "avif",
}


def image_artifact(
    image_bytes: bytes | None = None,
    *,
    url: str | None = None,
    media_type: str = "image/png",
    caption: str | None = None,
    filename: str | None = None,
    name: str = "image",
) -> Artifact:
    """Build an image Artifact with an optional caption.

    An "embedded" primitive: no ``_type`` envelope, no specialised JS
    renderer. The chat UI's ``renderFilePartEl`` sniffs ``mediaType``
    and renders any ``image/*`` part inline as an ``<img>``.

    Provide exactly one of ``image_bytes`` (inline) or ``url`` (reference
    to a stored image). The URL form keeps the wire transcript and the
    browser's localStorage compact — the chat UI renders ``<img src=url>``
    directly.

    Convenience over :func:`file_artifact` with image-friendly defaults:
    filename is auto-derived from ``media_type`` if not supplied; caption
    is rendered as markdown above the image.

    >>> image_artifact(png_bytes, caption="Here's your image.")
    >>> image_artifact(url="/images/abc123", caption="Here's your image.")
    """
    if (image_bytes is None) == (url is None):
        raise ValueError("Provide exactly one of `image_bytes` or `url`.")

    if not filename:
        ext = _IMAGE_EXT_BY_MIME.get(media_type.lower(), "png")
        filename = f"image-{uuid.uuid4().hex[:8]}.{ext}"

    parts: list[Part] = []
    if caption:
        parts.append(Part(text=caption))
    if image_bytes is not None:
        parts.append(Part(raw=image_bytes, filename=filename, media_type=media_type))
    else:
        parts.append(Part(url=url, filename=filename, media_type=media_type))
    return Artifact(
        artifact_id=_new_artifact_id(),
        name=name,
        parts=parts,
    )
