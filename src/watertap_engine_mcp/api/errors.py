"""Typed API error envelopes and redaction helpers."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from pydantic import ConfigDict, Field
from puran_mcp_support.engineering.pydantic_base import StrictFiniteBaseModel

from watertap_engine_mcp.security.redaction import redact_public_payload, redact_text


JsonDict = dict[str, Any]


def _redact_value(value: Any) -> Any:
    """Redact paths from nested JSON-like values."""

    return redact_public_payload(value)


class ApiErrorEnvelope(StrictFiniteBaseModel):
    """Structured error response returned by typed MCP adapters."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: JsonDict = Field(default_factory=dict)
    correlation_id: str = Field(default_factory=lambda: uuid4().hex)


class SolverDispatchError(ValueError):
    """Base class for solver dispatch errors with public API metadata."""

    code = "SOLVER_DISPATCH_ERROR"

    def __init__(
        self,
        *,
        requested: str,
        detected_problem_type: str,
        available_solvers_for_problem_type: list[str],
        message: str,
    ):
        super().__init__(message)
        self.requested = requested
        self.detected_problem_type = detected_problem_type
        self.available_solvers_for_problem_type = available_solvers_for_problem_type
        self.message = message

    @property
    def details(self) -> JsonDict:
        """Structured solver-dispatch details."""

        return {
            "requested": self.requested,
            "detected_problem_type": self.detected_problem_type,
            "available_solvers_for_problem_type": self.available_solvers_for_problem_type,
        }

    def to_error(self, correlation_id: str | None = None) -> ApiErrorEnvelope:
        """Convert to the public API error envelope."""

        return make_error(
            self.code,
            self.message,
            details=self.details,
            correlation_id=correlation_id,
        )


class SolverNotApplicableError(SolverDispatchError):
    """Raised when a known solver cannot handle the detected problem type."""

    code = "SOLVER_NOT_APPLICABLE"


class SolverUnknownError(SolverDispatchError):
    """Raised when a requested solver name is not registered."""

    code = "SOLVER_UNKNOWN"

    def __init__(
        self,
        *,
        requested: str,
        detected_problem_type: str,
        available_solvers_for_problem_type: list[str],
        known_solvers: list[str],
        message: str,
    ):
        super().__init__(
            requested=requested,
            detected_problem_type=detected_problem_type,
            available_solvers_for_problem_type=available_solvers_for_problem_type,
            message=message,
        )
        self.known_solvers = known_solvers

    @property
    def details(self) -> JsonDict:
        details = super().details
        details["known_solvers"] = self.known_solvers
        return details


def make_error(
    code: str,
    message: str,
    *,
    details: JsonDict | None = None,
    correlation_id: str | None = None,
) -> ApiErrorEnvelope:
    """Build an API error envelope with redacted strings."""

    safe_details = _redact_value(details or {})
    return ApiErrorEnvelope(
        code=code,
        message=redact_text(message),
        details=safe_details if isinstance(safe_details, dict) else {"value": safe_details},
        correlation_id=correlation_id or uuid4().hex,
    )


def exception_to_error(
    exc: BaseException,
    *,
    code: str = "INTERNAL_ERROR",
    details: JsonDict | None = None,
    correlation_id: str | None = None,
) -> ApiErrorEnvelope:
    """Convert an exception into a redacted API error envelope."""

    merged_details: JsonDict = {"exception_type": type(exc).__name__}
    if details:
        merged_details.update(details)
    return make_error(
        code,
        str(exc),
        details=merged_details,
        correlation_id=correlation_id,
    )


def is_error_envelope_payload(payload: Any) -> bool:
    """Return True when a payload already has the public error-envelope shape."""

    if not isinstance(payload, dict):
        return False
    return (
        "error" not in payload
        and isinstance(payload.get("code"), str)
        and isinstance(payload.get("message"), str)
        and isinstance(payload.get("details"), dict)
        and isinstance(payload.get("correlation_id"), str)
    )


def _derive_legacy_error_code(payload: JsonDict, fallback_code: str) -> str:
    code = payload.get("code")
    if code:
        return str(code)

    message = str(payload.get("error") or payload.get("message") or "")
    normalized = message.lower()
    if "not found" in normalized:
        return "NOT_FOUND"
    if any(
        marker in normalized
        for marker in (
            "invalid",
            "must",
            "requires",
            "unknown",
            "validation",
        )
    ):
        return "VALIDATION_ERROR"
    return fallback_code


def _legacy_error_message(payload: JsonDict) -> str:
    raw_error = payload.get("error")
    raw_code = payload.get("code")
    raw_message = payload.get("message")
    if raw_message and raw_error == raw_code:
        return str(raw_message)
    if raw_error is not None:
        return str(raw_error)
    if raw_message is not None:
        return str(raw_message)
    return "Unknown error"


def error_from_legacy_payload(
    payload: JsonDict,
    *,
    code: str | None = None,
    fallback_code: str = "INTERNAL_ERROR",
    correlation_id: str | None = None,
) -> ApiErrorEnvelope:
    """Convert legacy ``{"error": ...}`` action payloads to the typed envelope."""

    raw_message = _legacy_error_message(payload)
    legacy_details = payload.get("details")
    if isinstance(legacy_details, dict):
        details = dict(legacy_details)
    else:
        details = {}

    details.update(
        {
            key: value
            for key, value in payload.items()
            if key not in {"error", "code", "message", "details", "correlation_id"}
        }
    )
    try:
        json.dumps(details, default=str)
    except TypeError:
        details = {"legacy_payload": str(details)}
    legacy_correlation_id = payload.get("correlation_id")
    return make_error(
        code or _derive_legacy_error_code(payload, fallback_code),
        str(raw_message),
        details=details,
        correlation_id=correlation_id
        or (str(legacy_correlation_id) if legacy_correlation_id is not None else None),
    )


def normalize_error_payload(
    payload: JsonDict,
    *,
    fallback_code: str = "INTERNAL_ERROR",
) -> ApiErrorEnvelope | None:
    """Normalize a legacy or already-canonical error payload."""

    if is_error_envelope_payload(payload):
        return make_error(
            payload["code"],
            payload["message"],
            details=payload["details"],
            correlation_id=payload["correlation_id"],
        )
    if "error" in payload:
        return error_from_legacy_payload(payload, fallback_code=fallback_code)
    return None
