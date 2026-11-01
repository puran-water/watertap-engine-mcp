"""Formatting helpers for diagnostics payloads."""

from __future__ import annotations


def dof_status_message(
    *,
    status: str,
    total_dof: int,
    feed_units_without_state: list[str] | None = None,
) -> str:
    """Return the public message for a live DOF status."""

    if status == "ready":
        return "Flowsheet is properly specified (DOF = 0)"
    if feed_units_without_state:
        return "Feed state missing for: " + ", ".join(feed_units_without_state)
    if total_dof > 0:
        return f"Need to fix {total_dof} more variable(s)"
    return f"Too many fixed variables ({abs(total_dof)} extra)"


def dof_completion_plan_from_preflight(
    session_id: str,
    payload: dict,
) -> dict:
    """Return DOF closure guidance from a precomputed readiness payload."""

    if not payload["build_success"]:
        return {
            "session_id": session_id,
            "status": "error",
            "issues": payload["issues"],
        }

    return {
        "session_id": session_id,
        "status": payload["status"],
        "total_dof": payload.get("total_dof"),
        "unit_dof": payload.get("unit_dof", {}),
        "missing_engineering_inputs": payload.get("missing_engineering_inputs", {}),
        "live_dof_requirements": payload.get("live_dof_requirements", {}),
        "unit_spec_sets": payload.get("unit_spec_sets", {}),
        "usage": {
            "preferred": "Set registered process-facing inputs with set_unit_engineering_inputs().",
            "dynamic_topology": "For units without curated engineering inputs, provide the listed target_variable paths under units[].fixed_vars in build_from_topology.",
            "after_sizing": "Run check_solve() again; solve() owns scaling, initialization, recovery, and persistence once DOF is closed.",
        },
        "warnings": payload.get("warnings", []),
        "issues": payload.get("issues", []),
    }
