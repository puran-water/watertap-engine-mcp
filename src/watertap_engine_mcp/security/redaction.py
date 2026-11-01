"""Central redaction for public WaterTAP Engine payloads."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from puran_mcp_support.engineering.safe_paths import redact_paths as _redact_paths


_ANGLE_HOME_PATH = re.compile(r"<home>/[^\s'\"]+/([^\s'\"/]+)")
_REPO_ROOT = re.compile(
    'Public source package; internal evidence not distributed.'
)
_PROCESSENG_ROOT = re.compile(
    'Public source package; internal evidence not distributed.'
)


def redact_text(value: str) -> str:
    """Redact developer filesystem paths from public strings."""

    if not isinstance(value, str):
        return value
    text = _redact_paths(value)
    text = _REPO_ROOT.sub("<watertap-engine-mcp>", text)
    text = _PROCESSENG_ROOT.sub("<processeng-workspace>", text)
    text = _ANGLE_HOME_PATH.sub(r"<path>/\1", text)
    return text


def redact_public_payload(payload: Any) -> Any:
    """Recursively redact public JSON-like payloads."""

    if isinstance(payload, str):
        return redact_text(payload)
    if isinstance(payload, dict):
        return {
            _redact_mapping_key(key): redact_public_payload(value)
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redact_public_payload(value) for value in payload]
    if isinstance(payload, tuple):
        return tuple(redact_public_payload(value) for value in payload)
    if isinstance(payload, set):
        return [redact_public_payload(value) for value in payload]
    if payload is None or isinstance(payload, (bool, int, float)):
        return payload
    # Pydantic models (e.g. SolveResult) -> dump to a JSON-able dict and recurse,
    # so a structured result is preserved instead of being flattened to a string
    # by the fallback (which broke the MCP structured_content contract, OBS-001).
    if isinstance(payload, BaseModel):
        return redact_public_payload(payload.model_dump(mode="json"))
    return redact_text(str(payload))


def _redact_mapping_key(key: Any) -> str:
    """Return a redacted, JSON-object-safe mapping key."""

    if isinstance(key, str):
        return redact_text(key)
    return redact_text(str(redact_public_payload(key)))


def redact_exception(exc: BaseException) -> str:
    """Return a public-safe exception message."""

    return redact_text(f"{type(exc).__name__}: {exc}")
