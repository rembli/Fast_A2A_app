"""MAP — Geographic markers rendered as a Leaflet/OSM map.

Specialised artifact module. The package's autodiscover picks up the
two module-level attributes below:

  * ``tag``     — the value used as the ``_type`` discriminator.
  * ``builder`` — Python convenience wrapper around ``data_artifact``.
                  Re-exported at package level as ``map_artifact``.

The JS renderer that turns this artifact's payload into an interactive
map lives separately in ``fast_a2a_app/ui/renderers/MAP.js``. It
lazy-loads Leaflet from a public CDN on first use, so the chat UI
pays no Leaflet cost until an agent actually emits a ``MAP`` part.

Data shape:

    {
        "_type": "MAP",
        "markers": [
            {"lat": float, "lng": float,
             "label"?: str, "popup"?: str},
            ...
        ],
        "center"?: [lat, lng],   # optional; auto-fits to markers when omitted
        "zoom"?: int,            # optional default zoom
    }

``label`` is shown in the marker tooltip and as fallback popup text;
``popup`` (plain text, no HTML — the renderer escapes it) is shown
when the marker is clicked.
"""
from __future__ import annotations

from typing import Mapping, Sequence

from a2a.types import Artifact

from ._core import data_artifact

tag = "MAP"


def _coerce_marker(m: Mapping) -> dict | None:
    """Validate one marker and normalise its shape.

    Tolerates floats / ints, ignores entries without numeric coords
    (so a partial LLM JSON response that misses a city's coordinates
    just drops that pin rather than failing the whole map).
    """
    try:
        lat = float(m["lat"])
        lng = float(m["lng"])
    except (KeyError, TypeError, ValueError):
        return None
    out: dict = {"lat": lat, "lng": lng}
    if m.get("label"):
        out["label"] = str(m["label"])
    if m.get("popup"):
        out["popup"] = str(m["popup"])
    return out


def map_artifact(
    markers: Sequence[Mapping],
    *,
    center: tuple[float, float] | list[float] | None = None,
    zoom: int | None = None,
    caption: str | None = None,
    name: str = "map",
    html_url: str | None = None,
) -> Artifact:
    """Build a map Artifact rendered by the chat UI as a Leaflet map.

    Args:
        markers: Iterable of dicts with at least ``lat`` and ``lng``.
            Optional ``label`` and ``popup`` strings annotate the
            marker. Entries missing valid numeric coordinates are
            dropped silently — partial LLM output should still
            produce a useful map.
        center: Optional ``(lat, lng)`` pair to center the view on.
            When omitted, the renderer auto-fits to the markers'
            bounding box (with light padding).
        zoom: Optional initial zoom level (Leaflet scale: 0 = whole
            world, ~10 = city, ~15 = streets). Ignored when ``center``
            is not also set, since auto-fit derives its own zoom.
        caption: Optional one-line label rendered above the map.
        name: Artifact name (used by clients filtering on artifact
            metadata; doesn't affect rendering).
        html_url: URL of a self-contained HTML map (e.g. Folium
            output). When provided the renderer displays an iframe
            instead of building a client-side Leaflet map, and
            ``markers`` / ``center`` / ``zoom`` are ignored.

    >>> map_artifact(
    ...     [
    ...         {"lat": 41.9028, "lng": 12.4964, "label": "Rome"},
    ...         {"lat": 37.9755, "lng": 23.7348, "label": "Athens"},
    ...     ],
    ...     caption="Suggested destinations",
    ... )
    """
    if html_url:
        payload: dict = {"_type": tag, "html_url": html_url}
        return data_artifact(payload, name=name, text=caption)

    cleaned = [m for m in (_coerce_marker(m) for m in markers) if m is not None]
    payload = {"_type": tag, "markers": cleaned}
    if center is not None:
        try:
            payload["center"] = [float(center[0]), float(center[1])]
        except (TypeError, ValueError, IndexError):
            pass
    if zoom is not None:
        try:
            payload["zoom"] = int(zoom)
        except (TypeError, ValueError):
            pass
    return data_artifact(payload, name=name, text=caption)


builder = map_artifact
