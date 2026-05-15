"""artifacts — Convenience builders + typed-artifact registry.

The package is laid out in two tiers:

  * :mod:`._core` defines the **embedded** primitives — ``text_artifact``,
    ``data_artifact``, ``file_artifact``, ``image_artifact`` — that wrap
    A2A protocol Parts directly. They have no ``_type`` discriminator.
    The chat UI's standard renderers (markdown bubble, key-value block,
    file download card, inline image preview) live in ``index.html``
    and dispatch on the protobuf ``content`` oneof + media-type
    sniffing rather than the typed-artifact registry.
  * Each **specialised** artifact lives in its own ``CAPS_NAME.py``
    sibling module (see :mod:`.TABLE`, :mod:`.PROMPT_SUGGESTIONS`,
    :mod:`.MAP`). A specialised module exposes two module-level
    attributes:

        builder  — Python helper that returns an :class:`Artifact`.
        tag      — value used as the ``_type`` discriminator on the
                   data part. Registered with :data:`artifact_types`.

    The matching JS renderer (optional) lives separately as
    ``<TAG>.js`` under :mod:`fast_a2a_app.ui.renderers` and is inlined
    into the served HTML by :func:`build_a2a_ui`. A tag without a
    matching ``.js`` file falls through to the chat UI's generic
    key-value rendering of :func:`data_artifact`.

When this package is imported, autodiscover walks the directory,
imports every uppercase ``.py`` module, exposes its ``builder`` at the
package namespace under its function name, and registers ``(tag,
builder)`` with the artifact-type registry below.

Adding a new specialised artifact = drop a ``CAPS_NAME.py`` file (copy
:mod:`.TABLE` as a template) and the next process boot picks it up.
Add a matching ``ui/renderers/CAPS_NAME.js`` for a bespoke UI widget.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass
from typing import Callable

from a2a.types import Artifact

from ._core import data_artifact, file_artifact, image_artifact, text_artifact

log = logging.getLogger(__name__)


# ── Artifact-type registry ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ArtifactType:
    """A registered ``_type`` discriminator + its Python builder.

    Renderers live separately under ``fast_a2a_app/ui/renderers/``;
    the chat UI loads them at build time and dispatches data parts by
    ``_type``. A registered tag without a matching ``<TAG>.js`` file
    in the renderers directory still works — the UI falls back to the
    generic key-value rendering of :func:`data_artifact`.

    Attributes:
        tag: Value of ``_type`` in the data part. Convention:
            uppercase snake-case (e.g. ``TABLE``, ``PROMPT_SUGGESTIONS``,
            ``TIMELINE``). Pick a tag specific enough that it can't
            collide with another integration — short generic names like
            ``TABLE`` are owned by the framework, longer
            domain-prefixed names like ``CRM_OPPORTUNITY`` are safe for
            applications.
        builder: Optional Python helper that builds an Artifact for
            this tag. If omitted, callers construct artifacts
            themselves with ``data_artifact({"_type": tag, ...})``.
    """

    tag: str
    builder: Callable[..., Artifact] | None = None


class ArtifactTypeRegistry:
    """Process-wide registry of typed-artifact builders.

    Tracks the Python side of typed artifacts: ``(tag, builder)``
    pairs. The matching JavaScript renderers are a separate concern —
    they live as ``<TAG>.js`` files under ``fast_a2a_app/ui/renderers/``
    and are inlined into the served HTML by :func:`build_a2a_ui`.

    Use the module-level :data:`artifact_types` singleton; callers
    typically don't instantiate this class directly. Re-registering a
    tag overwrites the prior entry.
    """

    def __init__(self) -> None:
        self._types: dict[str, ArtifactType] = {}

    def register(
        self,
        tag: str,
        *,
        builder: Callable[..., Artifact] | None = None,
    ) -> ArtifactType:
        if not tag or not isinstance(tag, str):
            raise ValueError("artifact type tag must be a non-empty string")
        dt = ArtifactType(tag=tag, builder=builder)
        self._types[tag] = dt
        return dt

    def unregister(self, tag: str) -> None:
        self._types.pop(tag, None)

    def get(self, tag: str) -> ArtifactType | None:
        return self._types.get(tag)

    def builder(self, tag: str) -> Callable[..., Artifact] | None:
        dt = self._types.get(tag)
        return dt.builder if dt else None

    def all(self) -> list[ArtifactType]:
        return list(self._types.values())


artifact_types = ArtifactTypeRegistry()


# ── Autodiscover specialised artifact modules ─────────────────────────────────


def _autodiscover() -> None:
    """Import every sibling module and wire it up.

    Convention for sibling files:

      * Lowercase / leading-underscore filenames (``_core.py``,
        ``_helpers.py``, etc.) are **skipped** — they're internal
        utilities, not specialised artifacts.
      * Uppercase filenames (``TABLE.py``, ``PROMPT_SUGGESTIONS.py``,
        custom apps' ``MYAPP_TIMELINE.py``) are imported. Each is
        expected to expose:

            builder  — callable producing an ``Artifact``; re-exported
                       at package level under ``builder.__name__``
                       (e.g. ``table_artifact``).
            tag      — ``_type`` value to register with the
                       artifact-type registry.

    The matching JS renderer (optional) lives separately as
    ``<TAG>.js`` under :mod:`fast_a2a_app.ui.renderers`. If absent,
    the chat UI falls back to the generic key-value rendering of
    :func:`data_artifact`.

    A specialised module without a ``tag`` (no typed envelope) still
    has its ``builder`` re-exported — but at that point it usually
    belongs in ``_core.py`` alongside the other embedded primitives.
    """
    for module_info in pkgutil.iter_modules(__path__):
        name = module_info.name
        # Skip anything that doesn't follow the CAPS convention. This
        # keeps internal helpers (``_core``, etc.) out of the artifact
        # surface area without an explicit deny-list.
        if not name[:1].isupper():
            continue
        module = importlib.import_module(f"{__name__}.{name}")

        builder = getattr(module, "builder", None)
        if callable(builder):
            globals()[builder.__name__] = builder

        tag = getattr(module, "tag", None)
        if tag:
            artifact_types.register(
                tag,
                builder=builder if callable(builder) else None,
            )


_autodiscover()


__all__ = [
    # Embedded primitives (text / data / file / image) — always available.
    "text_artifact",
    "data_artifact",
    "file_artifact",
    "image_artifact",
    # Specialised builders auto-exported by the autodiscover above
    # (referenced by name here so static analysers / IDE autocompletion
    # still see them as exports of the package).
    "map_artifact",
    "table_artifact",
    "prompt_suggestions_artifact",
    # Artifact-type registry.
    "ArtifactType",
    "ArtifactTypeRegistry",
    "artifact_types",
]
