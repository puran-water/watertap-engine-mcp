"""Per-session active solve job leases."""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .idempotency import IdempotencyCache
from .state import ACTIVE_JOB_STATUSES


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(data, indent=2, default=str)
    try:
        with open(tmp_path, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class SessionJobLease:
    """Active job lease for one session."""

    session_id: str
    job_id: str
    idempotency_key: str | None
    created_at: str
    expires_at: str


@dataclass(frozen=True)
class LeaseDecision:
    """Result of checking an active job lease."""

    action: str
    job_id: str | None = None
    status: str | None = None
    message: str = ""

    @property
    def available(self) -> bool:
        return self.action == "available"

    @property
    def replay(self) -> bool:
        return self.action == "replay"

    @property
    def conflict(self) -> bool:
        return self.action == "conflict"


JobStatusLookup = Callable[[str], str | None]


class SessionJobLeaseStore:
    """Filesystem-backed active-job lease store."""

    def __init__(
        self,
        storage_dir: Path,
        *,
        idempotency_cache: IdempotencyCache | None = None,
        lease_seconds: int = 3600,
    ):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.idempotency_cache = idempotency_cache or IdempotencyCache(
            self.storage_dir / "idempotency"
        )
        self.lease_seconds = lease_seconds
        self._lock = threading.Lock()

    def _lease_path(self, session_id: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in session_id)
        return self.storage_dir / f"{safe}.json"

    def _load(self, session_id: str) -> SessionJobLease | None:
        path = self._lease_path(session_id)
        if not path.exists():
            return None
        try:
            with open(path) as f:
                return SessionJobLease(**json.load(f))
        except (OSError, json.JSONDecodeError, TypeError):
            return None

    def _save(self, lease: SessionJobLease) -> None:
        _write_json_atomic(self._lease_path(lease.session_id), asdict(lease))

    def clear(self, session_id: str) -> None:
        """Clear the active job lease for a session."""

        self._lease_path(session_id).unlink(missing_ok=True)

    def check(
        self,
        *,
        session_id: str,
        idempotency_key: str | None,
        job_status_lookup: JobStatusLookup,
    ) -> LeaseDecision:
        """Check whether a solve submission can proceed."""

        with self._lock:
            if idempotency_key:
                cached = self.idempotency_cache.get(session_id, idempotency_key)
                if cached is not None:
                    cached_status = job_status_lookup(cached.job_id)
                    if cached_status in ACTIVE_JOB_STATUSES:
                        return LeaseDecision(
                            action="replay",
                            job_id=cached.job_id,
                            status=cached_status,
                            message="Existing active solve returned for idempotency key.",
                        )
                    self.idempotency_cache.delete(session_id, idempotency_key)

            lease = self._load(session_id)
            if lease is None:
                return LeaseDecision(action="available")

            try:
                if _parse_ts(lease.expires_at) <= _utc_now():
                    self.clear(session_id)
                    return LeaseDecision(action="available")
            except ValueError:
                self.clear(session_id)
                return LeaseDecision(action="available")

            status = job_status_lookup(lease.job_id)
            if status not in ACTIVE_JOB_STATUSES:
                self.clear(session_id)
                return LeaseDecision(action="available")

            if idempotency_key and lease.idempotency_key == idempotency_key:
                return LeaseDecision(
                    action="replay",
                    job_id=lease.job_id,
                    status=status,
                    message="Existing active solve returned for idempotency key.",
                )

            return LeaseDecision(
                action="conflict",
                job_id=lease.job_id,
                status=status,
                message=(
                    f"Session '{session_id}' already has active solve job "
                    f"'{lease.job_id}'."
                ),
            )

    def record(
        self,
        *,
        session_id: str,
        job_id: str,
        idempotency_key: str | None,
    ) -> SessionJobLease:
        """Record an active job lease."""

        with self._lock:
            created_at = _utc_now()
            expires_at = created_at + timedelta(seconds=self.lease_seconds)
            lease = SessionJobLease(
                session_id=session_id,
                job_id=job_id,
                idempotency_key=idempotency_key,
                created_at=created_at.isoformat(),
                expires_at=expires_at.isoformat(),
            )
            self._save(lease)
            if idempotency_key:
                self.idempotency_cache.put(
                    session_id=session_id,
                    idempotency_key=idempotency_key,
                    job_id=job_id,
                    created_at=lease.created_at,
                    expires_at=lease.expires_at,
                )
            return lease

