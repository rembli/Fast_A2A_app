"""Lightweight A2A chat UI — mount at "/" in your FastAPI app.

The default ``a2a_ui`` app serves the chat with no upload affordance.
Use ``build_a2a_ui(file_upload_api=...)`` to configure features
programmatically — for example, to wire the chat's paperclip button to
your application's upload endpoint::

    app.mount("/", build_a2a_ui(file_upload_api="/images"))

To narrow what the file picker accepts, pass ``accepted_file_types`` —
a list (or comma-separated string) of extensions, MIME types, or MIME
wildcards in the same format as the HTML ``<input accept>`` attribute::

    app.mount("/", build_a2a_ui(
        file_upload_api="/uploads",
        accepted_file_types=[".csv", ".xlsx", "text/csv"],
    ))

Configuration is captured at build time and inlined as ``window.UI_CONFIG``
in the served HTML so the bundled JS can branch on it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .._version import __version__ as _LIBRARY_VERSION

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

_UI_DIR = Path(__file__).parent
_HTML_PATH = _UI_DIR / "index.html"
_CSS_PATH = _UI_DIR / "styles.css"
_JS_PATH = _UI_DIR / "app.js"
_LOGO_PATH = _UI_DIR / "a2a-ui.png"
_RENDERERS_DIR = _UI_DIR / "renderers"
_CONFIG_PLACEHOLDER = "/* UI_CONFIG */"
_RENDERERS_PLACEHOLDER = "/* DATA_TYPE_RENDERERS */"

# Default kept narrow on purpose: previous releases hard-coded these in
# the HTML, so unspecified callers (image_creator, etc.) see the same
# behaviour they did before. Callers that need different formats opt in
# via ``accepted_file_types``.
_DEFAULT_ACCEPTED = "image/png,image/jpeg,image/webp,image/gif"


def _normalise_accepted_file_types(
    value: Iterable[str] | str | None,
) -> str:
    """Normalise ``accepted_file_types`` to the comma-separated form the
    HTML ``<input accept>`` attribute expects.

    ``None`` → built-in default (images). A string passes through (trim
    only) so callers with a pre-formatted list don't have to split it.
    Any other iterable is joined with commas; entries are individually
    stripped, empties dropped, but otherwise untouched — file extensions
    (``".csv"``), MIME types (``"text/csv"``) and wildcards
    (``"image/*"``) are all valid and pass through as-is.
    """
    if value is None:
        return _DEFAULT_ACCEPTED
    if isinstance(value, str):
        return value.strip()
    cleaned = [entry.strip() for entry in value if entry and entry.strip()]
    return ",".join(cleaned)


def _collect_renderer_scripts() -> str:
    """Concatenate every ``<TAG>.js`` file in ``ui/renderers/``.

    Each renderer is a small self-registering script that assigns into
    ``window.A2A_RENDERERS``. Concatenating them at build time inlines
    the bodies into the served HTML so the source ``index.html`` stays
    small while specialised renderers live in dedicated, version-
    controllable ``.js`` files.

    Files are emitted in sorted order (deterministic across boots).
    Missing renderers are fine — a typed artifact whose ``<TAG>.js``
    file isn't present falls through to the chat UI's generic
    key-value rendering for ``data_artifact``.
    """
    if not _RENDERERS_DIR.is_dir():
        return ""
    chunks: list[str] = []
    for js_file in sorted(_RENDERERS_DIR.glob("*.js")):
        chunks.append(f"// ─── {js_file.name} " + "─" * 50)
        chunks.append(js_file.read_text(encoding="utf-8").rstrip())
    return "\n".join(chunks)


def build_a2a_ui(
    *,
    file_upload_api: str | None = None,
    accepted_file_types: Iterable[str] | str | None = None,
) -> Starlette:
    """Build a chat UI Starlette app with the given configuration.

    Args:
        file_upload_api: URL the chat UI should ``POST`` user-attached
            files to before sending the message. The endpoint must
            accept ``multipart/form-data`` and return
            ``{id, url, mediaType, filename}``. When ``None``, the
            attach button is hidden and uploads are disabled.
        accepted_file_types: What the paperclip's file picker will let
            the user select. Same format as the HTML ``<input accept>``
            attribute — a list (or comma-separated string) of file
            extensions (``".csv"``), MIME types (``"text/csv"``), or
            MIME wildcards (``"image/*"``). Defaults to images only
            (``image/png,image/jpeg,image/webp,image/gif``) so existing
            callers keep their behaviour. Ignored when
            ``file_upload_api`` is ``None``.
    """
    config = {
        "fileUploadApi": file_upload_api,
        "acceptedFileTypes": _normalise_accepted_file_types(accepted_file_types),
        "libraryVersion": _LIBRARY_VERSION,
    }
    config_js = f"window.UI_CONFIG = {json.dumps(config)};"
    # Snapshot the renderers directory **at build time** — dropping a
    # new ``<TAG>.js`` file after the UI is built won't hot-reload;
    # restart the app (or call ``build_a2a_ui()`` again) to pick it up.
    renderers_js = _collect_renderer_scripts()
    html = (
        _HTML_PATH.read_text()
        .replace(_CONFIG_PLACEHOLDER, config_js)
        .replace(_RENDERERS_PLACEHOLDER, renderers_js)
    )
    # Static asset bytes are snapshotted here so a request handler can't
    # be tricked into serving the wrong path via traversal (we never
    # consult the request path for static paths — each route returns a
    # fixed payload).
    css_bytes = _CSS_PATH.read_bytes()
    js_bytes = _JS_PATH.read_bytes()
    logo_bytes = _LOGO_PATH.read_bytes()

    async def _index(_: Request) -> HTMLResponse:
        return HTMLResponse(html)

    async def _styles(_: Request) -> Response:
        return Response(
            content=css_bytes,
            media_type="text/css; charset=utf-8",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    async def _app_js(_: Request) -> Response:
        return Response(
            content=js_bytes,
            media_type="text/javascript; charset=utf-8",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    async def _logo(_: Request) -> Response:
        return Response(
            content=logo_bytes,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    return Starlette(routes=[
        Route("/", _index),
        Route("/styles.css", _styles),
        Route("/app.js", _app_js),
        Route("/a2a-ui.png", _logo),
    ])


a2a_ui = build_a2a_ui()
