"""Shared dataclasses for the task-store backends.

Lives in a leaf module so each backend can import it without triggering a
circular import via ``task_stores/__init__.py`` (which imports the backends).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProgressEntry:
    """One persisted ``report_progress(...)`` event for crash-resilient replay."""

    seq: int
    message: str
    ts: float  # unix seconds (utc)
