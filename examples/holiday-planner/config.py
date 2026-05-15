"""
config.py — environment-driven settings for the Holiday Planner.

Every ``os.environ`` / ``os.getenv`` lookup the agent makes lives here,
exposed as plain module-level constants. The rest of the codebase
imports the names instead of repeating ``os.environ.get(...)`` calls so
the override surface is one file, not a grep across multiple modules.

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
    os.environ.get("AZURE_AI_DEPLOYMENT_NAME", "").strip() or "gpt-4o"
)
