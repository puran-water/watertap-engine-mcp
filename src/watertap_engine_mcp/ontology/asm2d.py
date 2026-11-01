"""Explicit ASM2d component handoff policies.

Policy:
- ASM2D-to-mASM2d: ``X_TSS`` is computed from component concentrations using
  QSDsan/mASM2d ``i_TSS`` coefficients and carried as a sidecar field.
- ASM2D-to-mADM1: ``X_TSS`` is dropped because mADM1 has its own particulate
  fractions and does not accept a bulk TSS state variable.
- ASM2D-to-WaterTAP CSTR/RO/etc.: ``X_TSS`` is mapped to a TSS-equivalent
  aggregate while retaining the source concentration units.
"""

from __future__ import annotations

from typing import Mapping

from .components import (
    WATERTAP_ASM2D_COMPONENTS,
    WATERTAP_ASM2D_PARTICULATE_COMPONENTS,
    get_component_ids,
)


ASM2D_I_TSS: dict[str, float] = {
    "X_I": 0.75,
    "X_S": 0.75,
    "X_H": 0.9,
    "X_PAO": 0.9,
    "X_AUT": 0.9,
    "X_PHA": 0.55,
    "X_PP": 3.2251507977994662,
    "X_MeOH": 1.0,
    "X_MeP": 1.0,
}

ASM2D_TSS_COMPONENTS: tuple[str, ...] = tuple(ASM2D_I_TSS)


def _as_float(value: object, *, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def compute_x_tss_from_components(
    state: Mapping[str, object],
    *,
    passthrough_existing: bool = True,
) -> float:
    """Compute or preserve WaterTAP ASM2d ``X_TSS`` from component mass states."""

    if passthrough_existing and "X_TSS" in state and state["X_TSS"] is not None:
        return _as_float(state["X_TSS"])
    return sum(_as_float(state.get(component)) * coeff for component, coeff in ASM2D_I_TSS.items())


def asm2d_to_masm2d_state(
    state: Mapping[str, object],
    *,
    preserve_existing_x_tss: bool = True,
) -> dict[str, float]:
    """Translate WaterTAP/QSDsan ASM2d state to mASM2d plus explicit X_TSS sidecar."""

    output = {
        component: _as_float(state.get(component))
        for component in get_component_ids("mASM2d")
    }
    if "S_ALK" in state and "S_IC" in output and not output["S_IC"]:
        output["S_IC"] = _as_float(state.get("S_ALK"))
    output["X_TSS"] = compute_x_tss_from_components(
        state,
        passthrough_existing=preserve_existing_x_tss,
    )
    return output


def asm2d_to_madm1_state(state: Mapping[str, object]) -> dict[str, float]:
    """Translate ASM2d state to mADM1 and intentionally drop bulk ``X_TSS``."""

    output = {
        component: _as_float(state.get(component))
        for component in get_component_ids("mADM1")
    }
    if "S_ALK" in state and "S_IC" in output and not output["S_IC"]:
        output["S_IC"] = _as_float(state.get("S_ALK"))
    output.pop("X_TSS", None)
    return output


def asm2d_to_watertap_tss_equivalent(state: Mapping[str, object]) -> dict[str, float]:
    """Return a WaterTAP aggregate TSS-equivalent payload from an ASM2d state."""

    x_tss = compute_x_tss_from_components(state, passthrough_existing=True)
    return {"TSS": x_tss, "TSS_mg_L": x_tss}


__all__ = [
    "ASM2D_I_TSS",
    "ASM2D_TSS_COMPONENTS",
    "WATERTAP_ASM2D_COMPONENTS",
    "WATERTAP_ASM2D_PARTICULATE_COMPONENTS",
    "asm2d_to_madm1_state",
    "asm2d_to_masm2d_state",
    "asm2d_to_watertap_tss_equivalent",
    "compute_x_tss_from_components",
]
