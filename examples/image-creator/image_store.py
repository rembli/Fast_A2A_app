"""
image_store.py — Per-agent image storage for image_creator

Keeps generated and uploaded image bytes out of the wire transcript and the
browser's localStorage. The agent stores into this service and returns
URL-based artifacts; a sibling FastAPI endpoint reads back from the same
store. URLs flow over the wire (compact); bytes never leave the server.

The store is **independent of fast_a2a_app and the chat UI** — both treat it
as an opaque HTTP endpoint behind a URL.

Implementation: filesystem-backed under a ``tmp/`` directory next to this
module, with TTL eviction by file mtime. Adequate for a single-process demo
and survives a process restart. For multi-replica deployments swap in a
Redis/S3-backed implementation behind the same ``put`` / ``get`` contract.
"""
from __future__ import annotations

import re
import secrets
import time
from pathlib import Path

DEFAULT_TTL_SECONDS = 24 * 60 * 60  # 24 h — matches the A2A task TTL
URL_PREFIX = "/images/"
DEFAULT_BASE_DIR = Path(__file__).parent / "tmp"

# Common image MIME types the chat UI emits.
_MIME_TO_EXT: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}
_EXT_TO_MIME: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}

# Image IDs come from secrets.token_urlsafe — restrict to that alphabet plus
# length to prevent path traversal via the URL endpoint.
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _is_safe_id(image_id: str) -> bool:
    return bool(image_id) and bool(_ID_PATTERN.fullmatch(image_id))


class ImageStore:
    """Filesystem-backed image store with TTL eviction.

    Files are written as ``{base_dir}/{id}.{ext}``; the extension encodes the
    media type so a separate metadata sidecar isn't needed.

    >>> store = ImageStore()
    >>> image_id = store.put(b"\\x89PNG...", media_type="image/png")
    >>> data, mime = store.get(image_id)
    """

    def __init__(
        self,
        base_dir: Path | str | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._base = Path(base_dir) if base_dir else DEFAULT_BASE_DIR
        self._base.mkdir(parents=True, exist_ok=True)
        self._ttl = ttl_seconds

    def put(self, content: bytes, *, media_type: str = "image/png") -> str:
        """Persist *content* under a fresh URL-safe id and return that id."""
        token = secrets.token_urlsafe(12)
        ext = _MIME_TO_EXT.get(media_type.lower(), "bin")
        (self._base / f"{token}.{ext}").write_bytes(content)
        # Best-effort opportunistic eviction so the directory doesn't grow forever.
        self._evict_expired()
        return token

    def get(self, image_id: str) -> tuple[bytes, str] | None:
        """Return ``(content, media_type)`` or ``None`` if missing/expired."""
        if not _is_safe_id(image_id):
            return None
        for path in self._base.glob(f"{image_id}.*"):
            try:
                if time.time() - path.stat().st_mtime > self._ttl:
                    path.unlink(missing_ok=True)
                    return None
                ext = path.suffix.lstrip(".").lower()
                mime = _EXT_TO_MIME.get(ext, "application/octet-stream")
                return path.read_bytes(), mime
            except (FileNotFoundError, OSError):
                continue
        return None

    def url_for(self, image_id: str) -> str:
        """Return the relative URL the UI uses to fetch this image."""
        return f"{URL_PREFIX}{image_id}"

    def id_from_url(self, url: str) -> str | None:
        """Extract the image id from a URL produced by :meth:`url_for`.

        Tolerant of absolute URLs, query strings, and fragments — any URL
        whose path ends with ``/images/<id>`` resolves correctly.
        """
        if not url or URL_PREFIX not in url:
            return None
        tail = url.rsplit(URL_PREFIX, 1)[-1]
        for sep in ("?", "#", "/"):
            if sep in tail:
                tail = tail.split(sep, 1)[0]
        return tail if _is_safe_id(tail) else None

    def _evict_expired(self) -> None:
        cutoff = time.time() - self._ttl
        try:
            entries = list(self._base.iterdir())
        except FileNotFoundError:
            return
        for f in entries:
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
            except OSError:
                continue


# Module-level singleton — sufficient for the single-process demo.
store = ImageStore()
