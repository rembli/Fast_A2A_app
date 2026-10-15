"""
config.py — settings for the Secure Agent example.

The one knob that matters here is where the SQLite task-store file lives.
In a container ``DATA_DIR=/data`` (a mounted volume); locally that path
isn't writable, so if it can't be created *and written to* we fall back to
``./data`` under the current working directory — no .env needed to run
locally.
"""
from __future__ import annotations

import os
from pathlib import Path

DEBUG = os.getenv("DEBUG", "true").lower() in ("1", "true", "yes")

# ── Model (Azure OpenAI via AzureCliCredential) ───────────────────────────────
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")
AZURE_AI_BASE_URL = os.environ.get("AZURE_AI_BASE_URL", "").strip().rstrip("/")
AZURE_AI_DEPLOYMENT_NAME = (
    os.environ.get("AZURE_AI_DEPLOYMENT_NAME", "").strip() or "gpt-5.4-mini"
)


def _usable_dir(path: str) -> bool:
    """True iff *path* can be created and a file written inside it.

    ``mkdir(exist_ok=True)`` alone is not enough: a pre-existing but
    unwritable directory wouldn't raise, leaving SQLite to fail later with a
    cryptic ``disk I/O error``. An explicit write probe catches that up front.
    """
    try:
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write_probe"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


DATA_DIR = os.environ.get("DATA_DIR", "/data")
if not _usable_dir(DATA_DIR):
    DATA_DIR = str(Path("./data").resolve())
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

TASK_DB_PATH = str(Path(DATA_DIR).resolve() / "tasks.db")
