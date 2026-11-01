"""Shared session lifecycle state constants."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

ACTIVE_JOB_STATUSES = frozenset({"pending", "running"})
TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled"})


def utc_now_iso() -> str:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ActiveJobReference:
    """Reference to an active solve job for one session."""

    session_id: str
    job_id: str
    idempotency_key: str | None = None
    created_at: str = ""
    expires_at: str = ""

