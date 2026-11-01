"""Infeasibility diagnostic helpers."""

from __future__ import annotations

from typing import Any


def infeasibility_summary(payload: Any) -> dict[str, Any]:
    """Normalize an infeasibility payload into a compact diagnostic summary."""

    if not isinstance(payload, dict):
        return {"available": False, "payload_type": type(payload).__name__}
    return {
        "available": True,
        "convergence_status": payload.get("convergence_status"),
        "termination_condition": payload.get("termination_condition"),
        "message": payload.get("message"),
        "most_likely_cause": payload.get("most_likely_cause"),
    }

