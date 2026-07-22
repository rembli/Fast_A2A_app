"""
config.py — environment-driven settings for the Image Creator.

Every ``os.environ`` / ``os.getenv`` lookup the agent makes lives here,
exposed as plain module-level constants. The rest of the codebase
imports the names instead of repeating ``os.environ.get(...)`` calls so
the override surface is one file, not a grep across multiple modules.

Per-conversation parameters that the user can switch at runtime
(``model`` / ``size`` / ``style``) are *not* here — they live in
``agent.py`` as ``CONFIG_PARAMETERS`` so the schema, defaults, and the
behaviour that reads them all sit next to each other.

Override any value at runtime by setting the matching env var (or
adding it to ``examples/.env``, which ``main.py`` loads via
``python-dotenv`` before this module is first imported).
"""
from __future__ import annotations

import os

# ── App / infra ───────────────────────────────────────────────────────────────

APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")
REDIS_URL = os.getenv("REDIS_URL")
DEBUG = os.getenv("DEBUG", "true").lower() in ("1", "true", "yes")

# ── Azure OpenAI ──────────────────────────────────────────────────────────────

AZURE_AI_BASE_URL = os.environ.get("AZURE_AI_BASE_URL", "").strip().rstrip("/")
AZURE_AI_DEPLOYMENT_NAME = (
    os.environ.get("AZURE_AI_DEPLOYMENT_NAME", "").strip() or "gpt-5.4-mini"
)

# ── Google Vertex AI (Gemini image) ───────────────────────────────────────────
# Alternative image backend. Auth is via managed identity / Application Default
# Credentials (``google.auth.default()``) — Workload Identity in a cluster,
# ``gcloud auth application-default login`` locally. No API keys.
#
# The Gemini image *model id* (``gemini-3.1-flash-image-preview`` /
# ``gemini-2.5-flash-image``, both of which support editing) is selected
# per-conversation via ``/set model`` (see ``CONFIG_PARAMETERS`` in
# ``agent.py``). ``GCP_LOCATION`` is the Vertex AI region the image client is
# pinned to.

GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "hmcp-dev-dxtinsnd-de-prj-a04").strip()
#GCP_LOCATION = os.getenv("GCP_LOCATION", "us-central1").strip()
GCP_LOCATION = os.getenv("GCP_LOCATION", "global").strip()

# ── Uploads ───────────────────────────────────────────────────────────────────

MAX_UPLOAD_BYTES = 10 * 1024 * 1024   # 10 MB cap per upload
ALLOWED_UPLOAD_TYPES = frozenset({
    "image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif",
})
