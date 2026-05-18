"""
fast_a2a_app.server.commons.uploads — file-attachment parsing helpers.

Every agent that accepts uploads ends up writing the same three things:

1. A resolver that turns a single A2A ``Part`` into ``(bytes, mime)``,
   handling both **inline** parts (``raw`` — a fresh upload's bytes
   ride along with the message) and **referenced** parts (``url`` — the
   chat UI re-sends a file from a previous turn by URL after first POSTing
   it to your upload endpoint).
2. A walker over ``context.message.parts`` to pull ``(text, files)`` from
   the *current* user turn — used for slash-command routing,
   image-without-text guards, and feeding fresh uploads into tools.
3. A walker over ``context.related_tasks`` to find files from *prior*
   turns — used for "make it warmer" / "edit the packshot from yesterday"
   follow-ups that reference an earlier artifact without re-uploading.

This module ships those three pieces. Storage stays user-owned (the
framework already makes ``POST /uploads`` opt-in via ``file_upload_api=``,
and the storage backend — S3, Bynder, local disk — is per-team). Hand the
helpers a ``resolver(url)`` callable that looks up bytes in your store,
and an optional ``predicate(bytes, mime)`` to filter to the file types
your agent cares about.

Typical wiring (image-only agent over a local ``image_store``)::

    from fast_a2a_app.server.commons.uploads import (
        extract_current_turn,
        latest_file_in_history,
    )

    def _resolve_url(url):
        image_id = image_store.id_from_url(url)
        return image_store.get(image_id) if image_id else None

    def _is_image(_data, mime):
        return mime.startswith("image/")

    async def stream_invoke(prompt, context):
        user_text, uploads = extract_current_turn(
            context, resolver=_resolve_url, predicate=_is_image,
        )
        previous = latest_file_in_history(
            context, resolver=_resolve_url, predicate=_is_image,
        )
        # … route on user_text + uploads, feed previous into the agent.
"""
from __future__ import annotations

from collections.abc import Callable

from a2a.server.agent_execution import RequestContext


# Caller-supplied callable that fetches bytes from URL refs. Returns
# ``(bytes, mime)`` when the URL is resolvable, ``None`` otherwise.
Resolver = Callable[[str], "tuple[bytes, str] | None"]

# Caller-supplied filter applied to every resolved file. Return ``True``
# to keep the file, ``False`` to skip it. Use cases: "images only",
# "documents only", size guards, MIME allow-listing.
Predicate = Callable[[bytes, str], bool]


def resolve_file_part(
    part: object,
    *,
    resolver: Resolver | None = None,
) -> tuple[bytes, str] | None:
    """Resolve a single A2A ``Part`` to ``(bytes, mime)``, or ``None``.

    * ``raw`` parts (fresh uploads, bytes inline) are decoded directly.
    * ``url`` parts (references back into your file store) are passed
      through ``resolver(url)``. If no resolver is provided, URL parts
      yield ``None`` — the bytes are unreachable without store-aware code.
    * ``text`` and ``data`` parts always yield ``None``.

    Single resolver so callers don't have to know whether an upload arrived
    inline or as a back-reference — relevant on follow-up turns where the
    chat UI may resend a prior upload as a URL part rather than re-uploading.
    """
    kind = part.WhichOneof("content")
    if kind == "raw":
        if part.raw:
            mime = part.media_type or "application/octet-stream"
            return bytes(part.raw), mime
    elif kind == "url" and part.url and resolver is not None:
        return resolver(part.url)
    return None


def extract_current_turn(
    context: RequestContext,
    *,
    resolver: Resolver | None = None,
    predicate: Predicate | None = None,
) -> tuple[str, list[tuple[bytes, str]]]:
    """Walk ``context.message.parts`` and return ``(user_text, files)``.

    Reads the **untouched current turn** — bypasses the prompt builder's
    history prefix so slash-command detection, file-without-text guards,
    and per-turn upload handling see what the user actually typed.

    ``predicate(bytes, mime)`` filters file matches when supplied. Common
    use is ``lambda _, mime: mime.startswith("image/")`` for an image
    agent; pass ``None`` to accept everything ``resolve_file_part`` returns.
    """
    text_chunks: list[str] = []
    files: list[tuple[bytes, str]] = []
    for part in getattr(context.message, "parts", None) or []:
        kind = part.WhichOneof("content")
        if kind == "text":
            if part.text:
                text_chunks.append(part.text)
        elif kind in ("raw", "url"):
            resolved = resolve_file_part(part, resolver=resolver)
            if resolved and (predicate is None or predicate(*resolved)):
                files.append(resolved)
    return ("\n".join(text_chunks)).strip(), files


def latest_file_in_history(
    context: RequestContext,
    *,
    resolver: Resolver | None = None,
    predicate: Predicate | None = None,
) -> tuple[bytes, str] | None:
    """Find the most recent file in ``context.related_tasks``, or ``None``.

    Walks both message ``history`` (user uploads) and ``artifacts``
    (agent-produced files) in chronological order; the last match wins.
    Use this to resolve follow-ups like "make it warmer" / "annotate this"
    where the user is referencing whatever file the conversation last
    surfaced, regardless of who produced it.

    ``predicate(bytes, mime)`` filters matches — pass an image / document /
    size predicate when your agent only cares about specific file kinds.
    """
    latest: tuple[bytes, str] | None = None
    for task in getattr(context, "related_tasks", None) or []:
        for msg in getattr(task, "history", None) or []:
            for part in getattr(msg, "parts", None) or []:
                resolved = resolve_file_part(part, resolver=resolver)
                if resolved and (predicate is None or predicate(*resolved)):
                    latest = resolved
        for artifact in getattr(task, "artifacts", None) or []:
            for part in getattr(artifact, "parts", None) or []:
                resolved = resolve_file_part(part, resolver=resolver)
                if resolved and (predicate is None or predicate(*resolved)):
                    latest = resolved
    return latest


__all__ = [
    "Resolver",
    "Predicate",
    "resolve_file_part",
    "extract_current_turn",
    "latest_file_in_history",
]
