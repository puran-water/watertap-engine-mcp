"""Idempotency key cache for solve submissions."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
class IdempotencyEntry:
    """One cached idempotency mapping."""

    session_id: str
    idempotency_key: str
    job_id: str
    created_at: str
    expires_at: str


class IdempotencyCache:
    """In-memory and filesystem-backed idempotency key cache."""

    def __init__(self, storage_dir: Path):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._entries: dict[str, IdempotencyEntry] = {}

    def _cache_key(self, session_id: str, idempotency_key: str) -> str:
        raw = f"{session_id}\0{idempotency_key}".encode()
        return hashlib.sha256(raw).hexdigest()

    def _entry_path(self, session_id: str, idempotency_key: str) -> Path:
        return self.storage_dir / f"{self._cache_key(session_id, idempotency_key)}.json"

    def get(self, session_id: str, idempotency_key: str) -> IdempotencyEntry | None:
        """Return a live idempotency entry, pruning expired entries."""

        cache_key = self._cache_key(session_id, idempotency_key)
        entry = self._entries.get(cache_key)
        path = self._entry_path(session_id, idempotency_key)
        if entry is None and path.exists():
            try:
                with open(path) as f:
                    entry = IdempotencyEntry(**json.load(f))
                self._entries[cache_key] = entry
            except (OSError, json.JSONDecodeError, TypeError):
                return None

        if entry is None:
            return None

        try:
            if _parse_ts(entry.expires_at) <= _utc_now():
                self.delete(session_id, idempotency_key)
                return None
        except ValueError:
            self.delete(session_id, idempotency_key)
            return None
        return entry

    def put(
        self,
        *,
        session_id: str,
        idempotency_key: str,
        job_id: str,
        created_at: str,
        expires_at: str,
    ) -> IdempotencyEntry:
        """Persist an idempotency mapping."""

        entry = IdempotencyEntry(
            session_id=session_id,
            idempotency_key=idempotency_key,
            job_id=job_id,
            created_at=created_at,
            expires_at=expires_at,
        )
        cache_key = self._cache_key(session_id, idempotency_key)
        self._entries[cache_key] = entry
        _write_json_atomic(self._entry_path(session_id, idempotency_key), asdict(entry))
        return entry

    def delete(self, session_id: str, idempotency_key: str) -> None:
        """Remove one idempotency entry."""

        cache_key = self._cache_key(session_id, idempotency_key)
        self._entries.pop(cache_key, None)
        self._entry_path(session_id, idempotency_key).unlink(missing_ok=True)

