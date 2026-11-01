"""Pydantic envelopes for result-tool responses."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .streams import normalize_job_result_streams


class StreamResultsEnvelope(BaseModel):
    """Structured envelope for get_stream_results."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    source: str
    streams: dict[str, Any] = Field(default_factory=dict)
    stream_roles: dict[str, Any] = Field(default_factory=dict)
    count: int | None = None
    warning: str | None = None
    error: str | None = None


class JobResultsEnvelope(BaseModel):
    """Structured envelope for get_job_results."""

    model_config = ConfigDict(extra="allow")

    job_id: str
    status: str
    result: dict[str, Any] | None = None
    message: str | None = None
    progress: float | None = None
    failure_reason: str | None = None


def completed_job_results_envelope(job_id: str, result: Any) -> dict[str, Any]:
    """Return a normalized completed-job response."""

    envelope = JobResultsEnvelope(
        job_id=job_id,
        status="completed",
        result=normalize_job_result_streams(result),
    )
    return envelope.model_dump(exclude_none=True)


__all__ = [
    "JobResultsEnvelope",
    "StreamResultsEnvelope",
    "completed_job_results_envelope",
]
