"""Live Pyomo/IDAES degrees-of-freedom diagnostics."""

from __future__ import annotations

from typing import Any


def _dof_target(model: Any) -> Any:
    """Return the flowsheet block when present."""

    return getattr(model, "fs", model)


def compute_live_dof(model: Any) -> int:
    """Return IDAES degrees of freedom for the actual built Pyomo model."""

    from idaes.core.util.model_statistics import degrees_of_freedom

    return int(degrees_of_freedom(_dof_target(model)))


def compute_unit_dof(unit_block: Any) -> int:
    """Return IDAES degrees of freedom for one live unit block."""

    from idaes.core.util.model_statistics import degrees_of_freedom

    return int(degrees_of_freedom(unit_block))


def active_unfixed_variables(block: Any) -> list[str]:
    """Return live unfixed variables that participate in active constraints."""

    try:
        from idaes.core.util.model_statistics import (
            unfixed_variables_set,
            variables_in_activated_constraints_set,
        )

        active_vars = {
            var.name for var in variables_in_activated_constraints_set(block)
        }
        return sorted(
            var.name
            for var in unfixed_variables_set(block)
            if var.name in active_vars
        )
    except Exception:
        return []


def compute_dof_report(
    model: Any,
    units: dict[str, Any],
    *,
    verbose: bool = False,
) -> dict[str, Any]:
    """Build a live DOF report for the flowsheet and unit blocks."""

    total_dof = compute_live_dof(model)
    unit_dof = {
        unit_id: compute_unit_dof(unit_block)
        for unit_id, unit_block in units.items()
    }
    report: dict[str, Any] = {
        "total_dof": total_dof,
        "unit_dof": unit_dof,
        "status": dof_status(total_dof),
    }
    if verbose:
        report["raw_block_dof"] = {
            unit_id: {
                "block_name": getattr(unit_block, "name", unit_id),
                "dof": unit_dof[unit_id],
            }
            for unit_id, unit_block in units.items()
        }
    return report


def dof_status(total_dof: int) -> str:
    """Map a DOF integer to the public status string."""

    if total_dof == 0:
        return "ready"
    if total_dof > 0:
        return "underspecified"
    return "overspecified"
