"""Thin MCP adapter helpers for typed tool responses."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .errors import (
    ApiErrorEnvelope,
    is_error_envelope_payload,
    normalize_error_payload,
    exception_to_error,
)
from .schemas import UNIT_INTROSPECTION_SCHEMAS


def to_json_compatible(value: Any) -> Any:
    """Convert nested action payloads into values Pydantic can JSON-serialize."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(to_json_compatible(key)): to_json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_json_compatible(item) for item in value]
    if isinstance(value, BaseModel):
        return to_json_compatible(value.model_dump(mode="json"))
    return str(value)


def output_schema_for_action(action_id: str) -> dict[str, Any] | None:
    """Return the explicit MCP output schema for typed pilot tools."""

    schema_pair = UNIT_INTROSPECTION_SCHEMAS.get(action_id)
    if schema_pair is None:
        return None
    return schema_pair[1].model_json_schema()


def request_model_for_action(action_id: str) -> type[BaseModel] | None:
    """Return the explicit request model for typed pilot tools."""

    schema_pair = UNIT_INTROSPECTION_SCHEMAS.get(action_id)
    if schema_pair is None:
        return None
    return schema_pair[0]


def response_model_for_action(action_id: str) -> type[BaseModel] | None:
    """Return the explicit response model for typed pilot tools."""

    schema_pair = UNIT_INTROSPECTION_SCHEMAS.get(action_id)
    if schema_pair is None:
        return None
    return schema_pair[1]


def normalize_action_result(action_id: str, payload: Any) -> Any:
    """Normalize legacy public action errors to the canonical envelope."""

    if isinstance(payload, dict):
        error = normalize_error_payload(
            payload,
            fallback_code=f"{action_id.upper()}_FAILED",
        )
        if error is not None:
            return error.model_dump(mode="python")
    if (
        isinstance(payload, list)
        and len(payload) == 1
        and isinstance(payload[0], dict)
    ):
        error = normalize_error_payload(
            payload[0],
            fallback_code=f"{action_id.upper()}_FAILED",
        )
        if error is not None:
            return error.model_dump(mode="python")
    return payload


def typed_action_wrapper(action_id: str, handler: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a legacy action with typed success/error envelope conversion."""

    response_model = response_model_for_action(action_id)
    legacy_handler = getattr(handler, "__wrapped__", handler)
    if response_model is None:
        @wraps(handler)
        def untyped_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                payload = normalize_action_result(action_id, handler(*args, **kwargs))
                if is_error_envelope_payload(payload):
                    return payload
                return to_json_compatible(payload)
            except Exception as exc:  # pragma: no cover - defensive adapter path
                return exception_to_error(
                    exc,
                    code=f"{action_id.upper()}_FAILED",
                    details={"tool": action_id},
                ).model_dump(mode="python")

        untyped_wrapper.__wrapped__ = legacy_handler
        return untyped_wrapper

    @wraps(handler)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            payload = handler(*args, **kwargs)
            payload = normalize_action_result(action_id, payload)
            if is_error_envelope_payload(payload):
                return to_json_compatible(response_model(
                    ok=False,
                    error=ApiErrorEnvelope.model_validate(payload),
                ))
            return to_json_compatible(response_model(ok=True, data=to_json_compatible(payload)))
        except Exception as exc:  # pragma: no cover - defensive adapter path
            return to_json_compatible(response_model(
                ok=False,
                error=exception_to_error(
                    exc,
                    code=f"{action_id.upper()}_FAILED",
                    details={"tool": action_id},
                ),
            ))

    wrapper.__annotations__ = dict(getattr(handler, "__annotations__", {}))
    wrapper.__annotations__["return"] = dict[str, Any]
    wrapper.__wrapped__ = legacy_handler
    return wrapper
