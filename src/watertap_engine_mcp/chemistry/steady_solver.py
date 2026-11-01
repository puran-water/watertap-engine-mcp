"""Branch-explicit fixed-dose solver for the shared steady chemistry block.

This is a reduced, property-package-neutral forward screen.  It deliberately
does not claim to be the canonical biological ``solve_flowsheet`` route and it
cannot recommend a dose: charge/speciation, surface binding, calibration, and
full biological/topology stability still have to be supplied by the host
flowsheet and the independent PHREEQC audit. Reduced-branch local stability is
reported from the shared continuous-time RHS.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
from idaes.core.solvers import get_solver
from puran_mcp_support.chemistry import UncertaintyRealization, convert_commercial_product_dose, reduced_chemistry_cstr_rhs, run_branch_aware_dose_grid_ensemble
from puran_mcp_support.schema import MADM1_FECL3_FE_S_P_KERNEL, AssumptionLedger, CommercialProductDefinition, ProductDose, mechanism_interpretation_from_ledger, validate_kernel_definition
from pyomo.environ import ConcreteModel, Constraint, Var, value
from pyomo.opt import SolverStatus, TerminationCondition

from .steady_state import (
    build_shared_chemistry_steady_state_block,
    fecl3_source_mol_m3_d,
    fix_forward_case,
    initialize_forward_case,
)

_PHASE_PARAMETER_NAMES = (
    "k_n",
    "k_g",
    "k_d",
    "si_critical",
    "n_nucleation",
    "n_growth",
    "n_dissolution",
    "area_per_inventory",
    "seed_surface_area",
)
_REDOX_PARAMETER_NAMES = ("k_red", "K_donor")
_OXIDATION_PARAMETER_NAMES = ("k_oxidation", "K_oxidant")
_AGING_PARAMETER_NAMES = ("k_aging",)
_SURFACE_PARAMETER_TO_BLOCK = {
    "site_capacity": "site_capacity",
    "k_adsorption": "k_surface_adsorption",
    "K_P": "K_surface_P",
    "K_ligand": "K_surface_ligand",
    "k_desorption": "k_surface_desorption",
}
_LIGAND_PARAMETER_TO_BLOCK = {
    "beta_fe_ii_l_mol": "beta_fe_ii_l_mol",
    "beta_fe_iii_l_mol": "beta_fe_iii_l_mol",
}
_RUNTIME_BALANCE_PROPERTIES = (
    "C",
    "N",
    "P",
    "S",
    "Fe",
    "Al",
    "Ca",
    "Mg",
    "Na",
    "K",
    "Cl",
    "charge",
    "electron_equivalent",
)


def _local_dynamic_stability_report(
    *,
    state_mol_m3: Mapping[str, float],
    inlet_mol_m3: Mapping[str, float],
    dose_source_mol_m3_d: Mapping[str, float],
    hydraulic_residence_time_d: float,
    solids_retention_time_d: float,
    temperature_c: float,
    phase_parameters: Mapping[str, Mapping[str, float]] | None,
    redox_parameters: Mapping[str, Mapping[str, float]] | None,
    oxidation_parameters: Mapping[str, Mapping[str, float]] | None,
    aging_parameters: Mapping[str, Mapping[str, float]] | None,
    surface_parameters: Mapping[str, Mapping[str, float]] | None,
    ligand_parameters: Mapping[str, float] | None,
    activity_coefficients: Mapping[str, float] | None,
    solvent_water_activity: float,
    eigenvalue_tolerance_per_d: float = 1e-7,
) -> dict[str, Any]:
    """Classify the local dynamic stability of a solved reduced branch.

    The Jacobian is a diagnostic finite-difference rendering of the *same*
    continuous QSDsan-facing residual used for steady/dynamic parity. A
    one-sided perturbation is used at a zero inventory so the diagnostic never
    creates an unphysical negative state merely to estimate a derivative.
    """

    state_ids = tuple(state_mol_m3)
    base = np.asarray([float(state_mol_m3[name]) for name in state_ids])
    try:
        def rhs(vector: np.ndarray) -> np.ndarray:
            derivatives, _ = reduced_chemistry_cstr_rhs(
                dict(zip(state_ids, vector)),
                inlet_mol_m3=inlet_mol_m3,
                dose_source_mol_m3_d=dose_source_mol_m3_d,
                hydraulic_residence_time_days=hydraulic_residence_time_d,
                solids_retention_time_days=solids_retention_time_d,
                temperature_k=float(temperature_c) + 273.15,
                phase_parameters=phase_parameters,
                redox_parameters=redox_parameters,
                oxidation_parameters=oxidation_parameters,
                aging_parameters=aging_parameters,
                surface_parameters=surface_parameters,
                ligand_parameters=ligand_parameters,
                activity_coefficients=activity_coefficients,
                solvent_water_activity=solvent_water_activity,
            )
            return np.asarray([float(derivatives[name]) for name in state_ids])

        base_rhs = rhs(base)
        jacobian = np.empty((len(state_ids), len(state_ids)), dtype=float)
        for column, value_ in enumerate(base):
            step = max(1e-9, 1e-6 * max(abs(float(value_)), 1.0))
            upper = base.copy()
            upper[column] += step
            if value_ > step:
                lower = base.copy()
                lower[column] -= step
                jacobian[:, column] = (rhs(upper) - rhs(lower)) / (2.0 * step)
            else:
                jacobian[:, column] = (rhs(upper) - base_rhs) / step
        eigenvalues = np.linalg.eigvals(jacobian)
        if not np.all(np.isfinite(eigenvalues)):
            raise ValueError("local dynamic Jacobian has nonfinite eigenvalues")
        maximum_real = float(np.max(np.real(eigenvalues)))
        tolerance = float(eigenvalue_tolerance_per_d)
        classification = (
            "locally_asymptotically_stable"
            if maximum_real < -tolerance
            else "locally_unstable"
            if maximum_real > tolerance
            else "marginal_or_unresolved"
        )
        return {
            "status": "completed",
            "classification": classification,
            "maximum_real_eigenvalue_per_d": maximum_real,
            "eigenvalue_tolerance_per_d": tolerance,
            "state_dimension": len(state_ids),
            "steady_rhs_norm_inf": float(np.max(np.abs(base_rhs))),
            "method": "finite_difference_jacobian_of_shared_continuous_rhs",
        }
    except Exception as exc:
        return {
            "status": "failed",
            "classification": "assessment_failed",
            "reason": str(exc),
            "method": "finite_difference_jacobian_of_shared_continuous_rhs",
        }


def _finite_nonnegative(value_: Any, *, field: str) -> float:
    result = float(value_)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field} must be finite and nonnegative")
    return result


def set_kinetic_profile(
    block,
    *,
    phase_parameters: Mapping[str, Mapping[str, float]] | None = None,
    redox_parameters: Mapping[str, Mapping[str, float]] | None = None,
    oxidation_parameters: Mapping[str, Mapping[str, float]] | None = None,
    aging_parameters: Mapping[str, Mapping[str, float]] | None = None,
    surface_parameters: Mapping[str, Mapping[str, float]] | None = None,
    ligand_parameters: Mapping[str, float] | None = None,
    scale: float = 1.0,
) -> None:
    """Apply an explicit kinetic profile; omitted rates remain zero.

    ``scale`` multiplies rate constants only and is used for continuation.
    Orders, thresholds, areas, and donor half-saturations retain their declared
    values throughout the continuation path.
    """

    scale = _finite_nonnegative(scale, field="kinetic continuation scale")
    phase_parameters = phase_parameters or {}
    redox_parameters = redox_parameters or {}
    oxidation_parameters = oxidation_parameters or {}
    aging_parameters = aging_parameters or {}
    surface_parameters = surface_parameters or {}
    ligand_parameters = ligand_parameters or {}
    unknown_phases = sorted(set(phase_parameters) - set(block.phases))
    unknown_redox = sorted(set(redox_parameters) - set(block.reduction_reactions))
    unknown_oxidation = sorted(set(oxidation_parameters) - set(block.oxidation_reactions))
    unknown_aging = sorted(set(aging_parameters) - set(block.aging_reactions))
    unknown_surface = sorted(set(surface_parameters) - set(block.surface_histories))
    unknown_ligand = sorted(set(ligand_parameters) - set(_LIGAND_PARAMETER_TO_BLOCK))
    if unknown_phases or unknown_redox or unknown_oxidation or unknown_aging or unknown_surface or unknown_ligand:
        raise KeyError(
            "unknown kinetic identifiers: "
            f"phases={unknown_phases}, redox={unknown_redox}, "
            f"oxidation={unknown_oxidation}, aging={unknown_aging}, "
            f"surface={unknown_surface}, ligand={unknown_ligand}"
        )

    for phase_id in block.phases:
        supplied = phase_parameters.get(phase_id, {})
        unknown = sorted(set(supplied) - set(_PHASE_PARAMETER_NAMES))
        if unknown:
            raise KeyError(f"phase {phase_id!r} has unknown kinetic fields {unknown}")
        for parameter_name in _PHASE_PARAMETER_NAMES:
            raw = supplied.get(parameter_name, getattr(block, parameter_name)[phase_id].value)
            number = _finite_nonnegative(raw, field=f"{phase_id}.{parameter_name}")
            if parameter_name in {"k_n", "k_g", "k_d"}:
                number *= scale
            getattr(block, parameter_name)[phase_id].set_value(number)

    for reaction_id in block.reduction_reactions:
        supplied = redox_parameters.get(reaction_id, {})
        unknown = sorted(set(supplied) - set(_REDOX_PARAMETER_NAMES))
        if unknown:
            raise KeyError(f"redox reaction {reaction_id!r} has unknown fields {unknown}")
        for parameter_name in _REDOX_PARAMETER_NAMES:
            raw = supplied.get(parameter_name, getattr(block, parameter_name)[reaction_id].value)
            number = _finite_nonnegative(raw, field=f"{reaction_id}.{parameter_name}")
            if parameter_name == "k_red":
                number *= scale
            getattr(block, parameter_name)[reaction_id].set_value(number)

    for reaction_id in block.oxidation_reactions:
        supplied = oxidation_parameters.get(reaction_id, {})
        unknown = sorted(set(supplied) - set(_OXIDATION_PARAMETER_NAMES))
        if unknown:
            raise KeyError(f"oxidation reaction {reaction_id!r} has unknown fields {unknown}")
        for parameter_name in _OXIDATION_PARAMETER_NAMES:
            raw = supplied.get(parameter_name, getattr(block, parameter_name)[reaction_id].value)
            number = _finite_nonnegative(raw, field=f"{reaction_id}.{parameter_name}")
            if parameter_name == "k_oxidation":
                number *= scale
            getattr(block, parameter_name)[reaction_id].set_value(number)

    for reaction_id in block.aging_reactions:
        supplied = aging_parameters.get(reaction_id, {})
        unknown = sorted(set(supplied) - set(_AGING_PARAMETER_NAMES))
        if unknown:
            raise KeyError(f"aging reaction {reaction_id!r} has unknown fields {unknown}")
        raw = supplied.get("k_aging", block.k_aging[reaction_id].value)
        block.k_aging[reaction_id].set_value(
            _finite_nonnegative(raw, field=f"{reaction_id}.k_aging") * scale
        )

    for history in block.surface_histories:
        supplied = surface_parameters.get(history, {})
        unknown = sorted(set(supplied) - set(_SURFACE_PARAMETER_TO_BLOCK))
        if unknown:
            raise KeyError(f"surface history {history!r} has unknown fields {unknown}")
        for public_name, block_name in _SURFACE_PARAMETER_TO_BLOCK.items():
            parameter = getattr(block, block_name)
            raw = supplied.get(public_name, parameter[history].value)
            number = _finite_nonnegative(raw, field=f"{history}.{public_name}")
            if public_name in {"k_adsorption", "k_desorption"}:
                number *= scale
            parameter[history].set_value(number)
        if value(block.K_surface_P[history]) <= 0.0 or value(block.K_surface_ligand[history]) <= 0.0:
            raise ValueError("surface half-saturation/inhibition constants must be positive")
        if value(block.site_capacity[history]) > 0.0:
            block.surface_capacity_constraint[history].activate()
        else:
            block.surface_capacity_constraint[history].deactivate()

    for public_name, block_name in _LIGAND_PARAMETER_TO_BLOCK.items():
        raw = ligand_parameters.get(public_name, getattr(block, block_name).value)
        getattr(block, block_name).set_value(
            _finite_nonnegative(raw, field=f"ligand.{public_name}")
        )


def _maximum_constraint_residual(model: ConcreteModel) -> float:
    maximum = 0.0
    for constraint in model.component_data_objects(Constraint, active=True):
        body = float(value(constraint.body))
        if constraint.equality:
            residual = abs(body - float(value(constraint.lower)))
        else:
            residual = 0.0
            if constraint.has_lb():
                residual = max(residual, float(value(constraint.lower)) - body)
            if constraint.has_ub():
                residual = max(residual, body - float(value(constraint.upper)))
        maximum = max(maximum, residual)
    return maximum


def _variable_value_snapshot(model: ConcreteModel) -> tuple[tuple[Any, float | None], ...]:
    """Capture a warm-start point before a nonlinear solve attempt.

    IPOPT may leave the model at its failed trial point.  Restoring the last
    converged continuation point is therefore essential before changing only
    numerical controls for a retry; otherwise the retry is not a controlled
    comparison and can jump branches.
    """

    return tuple(
        (variable, variable.value)
        for variable in model.component_data_objects(Var, descend_into=True)
    )


def _restore_variable_value_snapshot(
    snapshot: tuple[tuple[Any, float | None], ...],
) -> None:
    for variable, saved_value in snapshot:
        if saved_value is not None:
            variable.set_value(saved_value, skip_validation=True)


def _runtime_balance_report(
    block,
    *,
    absolute_tolerance: float = 1e-7,
    relative_tolerance: float = 1e-8,
) -> dict[str, Any]:
    """Audit steady external fluxes on the shared molar component basis.

    This is distinct from the nonlinear constraint residual. It independently
    verifies that inlet plus dose minus dissolved outlet minus retained-solid
    wasting closes each tracked element, charge flux and redox-equivalent flux.
    Hydrogen and oxygen are deliberately excluded from the pass gate because
    the reduced block declares water as an implicit solvent reservoir.
    """

    hrt = float(value(block.hydraulic_residence_time_d))
    component_by_id = {
        component.component_id: component for component in MADM1_FECL3_FE_S_P_KERNEL.components
    }
    external_flux: dict[str, float] = {}
    for component_id in block.aqueous_components:
        external_flux[component_id] = (
            float(value(block.inlet[component_id])) - float(value(block.outlet[component_id]))
        ) / hrt + float(value(block.dose_source[component_id]))
    for component_id in block.retained_components:
        external_flux[component_id] = -float(value(block.solid_wasting[component_id]))

    def coefficient(component_id: str, property_id: str) -> float:
        component = component_by_id[component_id]
        if property_id == "charge":
            return float(component.charge)
        if property_id == "electron_equivalent":
            return float(component.electron_equivalent_mol_e)
        return float(component.elemental_coefficients.get(property_id, 0.0))

    residuals: dict[str, float] = {}
    tolerances: dict[str, float] = {}
    passed: dict[str, bool] = {}
    for property_id in _RUNTIME_BALANCE_PROPERTIES:
        contributions = [
            coefficient(component_id, property_id) * flux
            for component_id, flux in external_flux.items()
        ]
        residual = float(sum(contributions))
        scale = float(sum(abs(item) for item in contributions))
        tolerance = absolute_tolerance + relative_tolerance * scale
        residuals[property_id] = residual
        tolerances[property_id] = tolerance
        passed[property_id] = math.isfinite(residual) and abs(residual) <= tolerance

    return {
        "basis": "mol_property_m3_d_external_flux",
        "residuals": residuals,
        "tolerances": tolerances,
        "property_passed": passed,
        "passed": all(passed.values()),
        "implicit_reservoirs": {
            "H": "solvent_water_and_proton_reservoir",
            "O": "solvent_water_reservoir",
        },
        "aqueous_electroneutrality_checked": False,
    }


def solve_fixed_dose_screen(
    *,
    inlet_mol_m3: Mapping[str, float],
    fecl3_fe_mol_m3_d: float,
    hydraulic_residence_time_d: float,
    solids_retention_time_d: float,
    temperature_c: float = 25.0,
    phase_parameters: Mapping[str, Mapping[str, float]] | None = None,
    redox_parameters: Mapping[str, Mapping[str, float]] | None = None,
    oxidation_parameters: Mapping[str, Mapping[str, float]] | None = None,
    aging_parameters: Mapping[str, Mapping[str, float]] | None = None,
    surface_parameters: Mapping[str, Mapping[str, float]] | None = None,
    ligand_parameters: Mapping[str, float] | None = None,
    activity_coefficients: Mapping[str, float] | None = None,
    solvent_water_activity: float = 1.0,
    continuation_path: Sequence[float] = (0.0, 0.1, 0.5, 1.0),
    target_orthophosphate_p_mg_l: float | None = None,
    solver_options: Mapping[str, Any] | None = None,
    additional_dose_source_mol_m3_d: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Solve one fixed FeCl3-as-Fe forward screen with explicit provenance.

    Inputs and dose sources are mol/m3 and mol/m3/d, respectively.  The result
    reports modeled orthophosphate only; it never labels that quantity plant TP.
    """

    validate_kernel_definition(MADM1_FECL3_FE_S_P_KERNEL)
    path = tuple(float(point) for point in continuation_path)
    if not path or path[0] != 0.0 or path[-1] != 1.0:
        raise ValueError("continuation_path must start at 0 and end at 1")
    if any(not math.isfinite(point) or point < 0 or point > 1 for point in path):
        raise ValueError("continuation_path values must be finite fractions in [0, 1]")
    if any(right <= left for left, right in zip(path, path[1:])):
        raise ValueError("continuation_path must be strictly increasing")
    if target_orthophosphate_p_mg_l is not None:
        target_orthophosphate_p_mg_l = _finite_nonnegative(
            target_orthophosphate_p_mg_l,
            field="target orthophosphate-P",
        )

    dose_source = fecl3_source_mol_m3_d(fecl3_fe_mol_m3_d)
    for component_id, raw_source in (additional_dose_source_mol_m3_d or {}).items():
        source = _finite_nonnegative(raw_source, field=f"additional dose source {component_id}")
        dose_source[str(component_id)] = dose_source.get(str(component_id), 0.0) + source

    model = ConcreteModel()
    model.chemistry = build_shared_chemistry_steady_state_block()
    block = model.chemistry
    fix_forward_case(
        block,
        inlet_mol_m3=dict(inlet_mol_m3),
        dose_source_mol_m3_d=dose_source,
        hydraulic_residence_time_d=hydraulic_residence_time_d,
        solids_retention_time_d=solids_retention_time_d,
        temperature_c=temperature_c,
        solvent_water_activity=solvent_water_activity,
    )
    for component_id, coefficient in (activity_coefficients or {}).items():
        if component_id not in block.aqueous_components:
            raise KeyError(f"unknown activity coefficient component {component_id!r}")
        coefficient = float(coefficient)
        if not math.isfinite(coefficient) or coefficient <= 0:
            raise ValueError("activity coefficients must be finite and positive")
        block.activity_coefficient[component_id].set_value(coefficient)

    set_kinetic_profile(
        block,
        phase_parameters=phase_parameters,
        redox_parameters=redox_parameters,
        oxidation_parameters=oxidation_parameters,
        aging_parameters=aging_parameters,
        surface_parameters=surface_parameters,
        ligand_parameters=ligand_parameters,
        scale=0.0,
    )
    initialize_forward_case(block)
    requested_solver_options = {
        str(option): option_value for option, option_value in (solver_options or {}).items()
    }

    def configured_solver(
        *,
        numerical_retry: bool = False,
        constraint_refinement: bool = False,
    ):
        configured = get_solver()
        configured.options.setdefault("tol", 1e-9)
        configured.options.setdefault("max_iter", 10_000)
        if numerical_retry:
            # This changes only IPOPT's barrier strategy and distance from
            # nonnegative bounds.  It does not alter chemistry, phase
            # selection, or the physical continuation point.
            configured.options.setdefault("mu_strategy", "adaptive")
            configured.options.setdefault("bound_push", 1e-12)
            configured.options.setdefault("bound_frac", 1e-12)
        if constraint_refinement:
            configured.options.setdefault("constr_viol_tol", 1e-10)
            configured.options.setdefault("acceptable_constr_viol_tol", 1e-10)
        for option, option_value in requested_solver_options.items():
            configured.options[option] = option_value
        return configured

    def solve_attempt(configured, *, attempt: str):
        try:
            result = configured.solve(model)
        except Exception as exc:  # Pyomo can raise instead of returning an error result.
            return None, False, {
                "attempt": attempt,
                "solver_status": "error",
                "termination_condition": "solver_exception",
                "converged": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        converged_ = (
            result.solver.status == SolverStatus.ok
            and result.solver.termination_condition
            in {TerminationCondition.optimal, TerminationCondition.locallyOptimal}
        )
        return result, converged_, {
            "attempt": attempt,
            "solver_status": str(result.solver.status),
            "termination_condition": str(result.solver.termination_condition),
            "converged": converged_,
        }

    solver = configured_solver()

    stages: list[dict[str, Any]] = []
    for scale in path:
        set_kinetic_profile(
            block,
            phase_parameters=phase_parameters,
            redox_parameters=redox_parameters,
            oxidation_parameters=oxidation_parameters,
            aging_parameters=aging_parameters,
            surface_parameters=surface_parameters,
            ligand_parameters=ligand_parameters,
            scale=scale,
        )
        converged_warm_start = _variable_value_snapshot(model)
        _, converged, primary_attempt = solve_attempt(
            solver,
            attempt="primary",
        )
        attempts = [primary_attempt]
        if not converged:
            # A numerical failure is not physical directionality.  Keep the
            # same kinetic scale and restore the prior converged point before
            # retrying with alternate IPOPT controls.
            _restore_variable_value_snapshot(converged_warm_start)
            retry_solver = configured_solver(numerical_retry=True)
            _, converged, retry_attempt = solve_attempt(
                retry_solver,
                attempt="adaptive_barrier_retry",
            )
            attempts.append(retry_attempt)
            if not converged:
                _restore_variable_value_snapshot(converged_warm_start)
        final_attempt = attempts[-1]
        stages.append(
            {
                "kinetic_scale": scale,
                "solver_status": final_attempt["solver_status"],
                "termination_condition": final_attempt["termination_condition"],
                "converged": converged,
                "attempts": attempts,
                "numerical_retry_used": len(attempts) > 1,
            }
        )
        if not converged:
            break

    solver_converged = bool(stages and stages[-1]["converged"] and len(stages) == len(path))
    residual = _maximum_constraint_residual(model)
    if solver_converged and residual > 1e-9:
        # IPOPT's scaled feasibility test can accept small raw residuals in
        # logarithmic SI equations.  Refine the already-converged endpoint
        # under an explicit raw constraint tolerance.  A failed refinement is
        # classified and rolled back; it never destroys a valid branch.
        converged_endpoint = _variable_value_snapshot(model)
        _, refinement_converged, refinement_attempt = solve_attempt(
            configured_solver(constraint_refinement=True),
            attempt="raw_constraint_refinement",
        )
        stages[-1]["attempts"].append(refinement_attempt)
        stages[-1]["constraint_refinement_used"] = True
        if not refinement_converged:
            _restore_variable_value_snapshot(converged_endpoint)
        residual = _maximum_constraint_residual(model)
    elif stages:
        stages[-1]["constraint_refinement_used"] = False
    runtime_balance = _runtime_balance_report(block)
    mass_balance_passed = solver_converged and residual <= 1e-7 and runtime_balance["passed"]
    outlet = {
        component_id: float(value(block.outlet[component_id]))
        for component_id in block.aqueous_components
    }
    solids = {
        component_id: float(value(block.solid_inventory[component_id]))
        for component_id in block.retained_components
    }
    phase_si = {
        phase_id: float(value(block.saturation_index[phase_id])) for phase_id in block.phases
    }
    outlet_activities = {
        component_id: float(value(block.activity[component_id]))
        for component_id in block.aqueous_components
    }
    free_ion_fractions = {
        component_id: float(value(block.free_ion_fraction[component_id]))
        for component_id in block.aqueous_components
    }
    temperature_c_result = float(value(block.temperature_k)) - 273.15
    phase_temperature_treatment = {
        phase_id: (
            block.phase_definition_by_id[phase_id].temperature_correction_id
            or (
                "reference_25C"
                if abs(temperature_c_result - 25.0) <= 0.1
                else "held_at_25C_no_sourced_temperature_correction"
            )
        )
        for phase_id in block.phases
    }
    phases_without_temperature_correction = [
        phase_id
        for phase_id, treatment in phase_temperature_treatment.items()
        if treatment == "held_at_25C_no_sourced_temperature_correction"
    ]
    residual_po4_p = outlet["PO4"] * 30.973761998
    target_met = (
        None
        if target_orthophosphate_p_mg_l is None
        else solver_converged and residual_po4_p <= target_orthophosphate_p_mg_l
    )
    active_phases = tuple(
        phase_id for phase_id in block.phases if solids.get(phase_id, 0.0) > 1e-12
    )
    if solver_converged and mass_balance_passed:
        stability_assessment = _local_dynamic_stability_report(
            state_mol_m3={
                **{
                    component_id: outlet[component_id]
                    for component_id in block.aqueous_components
                },
                **{
                    component_id: solids[component_id]
                    for component_id in block.retained_components
                },
            },
            inlet_mol_m3=inlet_mol_m3,
            dose_source_mol_m3_d=dose_source,
            hydraulic_residence_time_d=hydraulic_residence_time_d,
            solids_retention_time_d=solids_retention_time_d,
            temperature_c=temperature_c_result,
            phase_parameters=phase_parameters,
            redox_parameters=redox_parameters,
            oxidation_parameters=oxidation_parameters,
            aging_parameters=aging_parameters,
            surface_parameters=surface_parameters,
            ligand_parameters=ligand_parameters,
            activity_coefficients=activity_coefficients,
            solvent_water_activity=solvent_water_activity,
        )
    else:
        stability_assessment = {
            "status": "not_run",
            "classification": "not_assessed",
            "reason": "steady_solver_or_mass_balance_gate_failed",
            "method": "finite_difference_jacobian_of_shared_continuous_rhs",
        }
    # Publish an engine-neutral endpoint translation for the independent
    # PHREEQC audit. Concentrations and retained inventories are mol/m3 in this
    # reduced block, hence the 1e-3 conversion to mol/L. This translation is
    # intentionally not audit-ready: the reduced screen has no full pH/charge
    # closure or explicit gas boundary and CO3 is not a complete TIC total.
    phreeqc_endpoint_translation = {
        "kernel_id": MADM1_FECL3_FE_S_P_KERNEL.kernel_id,
        "kernel_hash": MADM1_FECL3_FE_S_P_KERNEL.sha256,
        "totals_mol_l": {
            "P": outlet["PO4"] * 1e-3,
            "S(6)": outlet["SO4"] * 1e-3,
            "S(-2)": outlet["HS_minus"] * 1e-3,
            "Fe(3)": outlet["Fe_III_aq"] * 1e-3,
            "Fe(2)": outlet["Fe_II_aq"] * 1e-3,
            "C(4)": outlet["CO3"] * 1e-3,
            "Cl": outlet["Cl_minus"] * 1e-3,
        },
        "kinetic_phase_inventory_mol_l": {
            phase_id: inventory * 1e-3
            for phase_id, inventory in solids.items()
            if phase_id != "HFO"
        },
        "hfo_history_inventory_mol_fe_l": {
            "unresolved_hfo_inventory": solids.get("HFO", 0.0) * 1e-3,
        },
        "p_total_basis": "reduced_block_PO4_only_not_plant_TP",
        "temperature_c": temperature_c_result,
        "ph": None,
        "gas_boundary_mode": "unsupplied",
        "gas_boundary": {},
        "analytical_totals_ready": True,
        "translation_ready": True,
        "audit_ready": False,
        "audit_readiness_blockers": [
            "pH_not_supplied",
            "gas_boundary_not_supplied",
            "carbonate_state_is_not_complete_TIC",
            "aqueous_complexation_and_ligands_not_closed",
        ],
        "mechanism_identified": False,
        "surface_dataset_id": MADM1_FECL3_FE_S_P_KERNEL.surfaces[0].surface_dataset_id,
        "oxide_history_id": "zero_inventory_branch_surface_history_unresolved",
        "formation_ph_basis": "not_modeled",
        "effective_active_fe_fraction": None,
        "parameter_contact": None,
        "extrapolation_status": "not_assessed",
        "mapping_limitations": [
            "the PO4 state is dissolved reactive phosphorus, not plant TP",
            "the CO3 state is not a complete analytical TIC inventory",
            "HFO oxide history and surface occupancy are unresolved",
        ],
    }
    canonical_input = json.dumps(
        {
            "inlet_mol_m3": dict(
                sorted((str(key), float(item)) for key, item in inlet_mol_m3.items())
            ),
            "fecl3_fe_mol_m3_d": float(fecl3_fe_mol_m3_d),
            "hydraulic_residence_time_d": float(hydraulic_residence_time_d),
            "solids_retention_time_d": float(solids_retention_time_d),
            "temperature_c": float(temperature_c),
            "phase_parameters": phase_parameters or {},
            "redox_parameters": redox_parameters or {},
            "oxidation_parameters": oxidation_parameters or {},
            "aging_parameters": aging_parameters or {},
            "surface_parameters": surface_parameters or {},
            "ligand_parameters": ligand_parameters or {},
            "activity_coefficients": activity_coefficients or {},
            "solvent_water_activity": float(solvent_water_activity),
            "continuation_path": list(path),
            "target_orthophosphate_p_mg_l": target_orthophosphate_p_mg_l,
            "additional_dose_source_mol_m3_d": dict(
                sorted(
                    (str(key), float(item))
                    for key, item in (additional_dose_source_mol_m3_d or {}).items()
                )
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    surface = MADM1_FECL3_FE_S_P_KERNEL.surfaces[0]
    assumption_ledger = AssumptionLedger(
        target_variable="steady_outlet_orthophosphate_p",
        thermodynamic_dataset_id=MADM1_FECL3_FE_S_P_KERNEL.thermodynamic_dataset_id,
        database_hash="unresolved_runtime_database_hash",
        phase_definition_set_id=MADM1_FECL3_FE_S_P_KERNEL.phase_definition_set_id,
        activity_model_id=(
            "externally_supplied_coefficients_or_ideal_with_shared_"
            "temperature_corrected_acid_base_fractions"
        ),
        redox_method=MADM1_FECL3_FE_S_P_KERNEL.redox_reaction_set_id,
        sulfide_source="fixed_inlet_HS_minus_total",
        iron_sulfide_phase_case="amorphous_and_mackinawite_competing",
        chemistry_treatment="zero_accumulation_kinetic_screen",
        solids_inventory_id="zero_inventory_continuation_branch",
        mbr_retention_profile_id="ideal_retention_with_explicit_SRT_wasting",
        calibration_domain_id=None,
        ligand_model_id=MADM1_FECL3_FE_S_P_KERNEL.ligand_model_id,
        model_commit=(
            os.environ.get("PURANOS_MODEL_COMMIT") or os.environ.get("GIT_COMMIT_SHA") or "unknown"
        ),
        input_hash=hashlib.sha256(canonical_input.encode("utf-8")).hexdigest(),
        surface_dataset_id=surface.surface_dataset_id,
        surface_parameter_interpretation=(
            "runtime_uncommissioned_history_profile"
            if surface_parameters
            else "compiled_but_inactive_zero_rate_profile"
        ),
        oxide_history_class="unresolved",
        oxide_history_id="zero_inventory_branch_surface_history_unresolved",
        oxide_formation_ph=None,
        oxide_formation_ph_basis="not_modeled",
        active_metal_fraction_basis="not_identified",
        effective_active_fe_fraction=None,
        parameter_contact=None,
        mechanism_identified=False,
        smp_or_doc_basis="not_supplied",
        extrapolation_status="not_assessed",
        extrapolation_reasons=(
            "surface/ligand parameters are uncommissioned and full biological coupling is not represented",
        ),
    ).public_dict()
    mechanism_interpretation = mechanism_interpretation_from_ledger(
        assumption_ledger
    )
    phreeqc_endpoint_translation["mechanism_interpretation"] = (
        mechanism_interpretation
    )
    for field in (
        "mechanism_identified",
        "surface_dataset_id",
        "oxide_history_id",
        "formation_ph_basis",
        "effective_active_fe_fraction",
        "parameter_contact",
        "extrapolation_status",
    ):
        phreeqc_endpoint_translation[field] = mechanism_interpretation[field]
    recommendation_blockers = [
        "commercial_product_not_characterized",
        "not_integrated_with_canonical_biological_property_package",
        "charge_and_full_aqueous_speciation_not_closed",
        "conditional_ligand_model_not_site_calibrated",
        "HFO_history_surface_model_not_site_calibrated",
        "kinetics_not_calibrated",
        "PHREEQC_audit_not_attached",
        "plant_TP_is_not_modeled_orthophosphate",
    ]
    if stability_assessment["classification"] != "locally_asymptotically_stable":
        recommendation_blockers.append(
            "dynamic_branch_not_locally_stable:"
            + str(stability_assessment["classification"])
        )
    if phases_without_temperature_correction:
        recommendation_blockers.append(
            "phase_logK_temperature_corrections_incomplete:"
            + ",".join(phases_without_temperature_correction)
        )

    final_termination = stages[-1]["termination_condition"] if stages else "not_run"
    status = (
        "converged_screen"
        if solver_converged
        else "solver_error"
        if final_termination == "solver_exception"
        else "infeasible"
        if "infeasible" in final_termination.lower()
        else "nonconverged"
    )

    return {
        "status": status,
        "dose_input_mode": "direct_molar_fecl3_as_fe",
        "commercial_product_characterized": False,
        "solver_converged": solver_converged,
        "mass_balance_passed": mass_balance_passed,
        "maximum_constraint_residual": residual,
        "runtime_balance": runtime_balance,
        "target_metric": "orthophosphate_p",
        "target_orthophosphate_p_mg_l": target_orthophosphate_p_mg_l,
        "target_met": target_met,
        "residual_orthophosphate_p_mg_l": residual_po4_p,
        "outlet_mol_m3": outlet,
        "analytical_state_basis": {
            "PO4": "total_inorganic_P",
            "HS_minus": "total_dissolved_S_minus_II",
            "CO3": "total_inorganic_carbon",
            "SO4": "total_sulfate",
            "Fe_III_aq": "total_aqueous_Fe_III_pending_complexation",
            "Fe_II_aq": "total_aqueous_Fe_II_pending_complexation",
            "Cl_minus": "total_chloride",
            "H2_aq": "dissolved_H2",
            "O2_aq": "dissolved_O2",
            "Ligand_sites": "transported_conditional_binding_site_total",
            "H_plus": "hydrogen_ion",
        },
        "retained_inventory_mol_m3": solids,
        "phase_saturation_index": phase_si,
        "outlet_activities": outlet_activities,
        "thermodynamic_free_ion_fractions": free_ion_fractions,
        "conditional_ligand_speciation": {
            "free_ligand_mol_l": float(value(block.free_ligand_mol_l)),
            "Fe_II_complexed_mol_l": float(value(block.fe_ii_l_complex_mol_l)),
            "Fe_III_complexed_mol_l": float(value(block.fe_iii_l_complex_mol_l)),
            "beta_fe_ii_l_mol": float(value(block.beta_fe_ii_l_mol)),
            "beta_fe_iii_l_mol": float(value(block.beta_fe_iii_l_mol)),
            "calibrated": False,
        },
        "phase_temperature_treatment": phase_temperature_treatment,
        "phases_without_temperature_correction": phases_without_temperature_correction,
        "phase_temperature_coverage_complete": not phases_without_temperature_correction,
        "phreeqc_endpoint_translation": phreeqc_endpoint_translation,
        "branch": {
            "branch_id": "zero_inventory_continuation_branch",
            "initial_inventory_id": "zero_retained_inventory",
            "continuation_path": list(path),
            "dynamic_stability": stability_assessment["classification"],
            "stability_assessment": stability_assessment,
            "steady_residual_norm": residual,
            "active_phase_set": list(active_phases),
        },
        "continuation_stages": stages,
        "kernel_id": MADM1_FECL3_FE_S_P_KERNEL.kernel_id,
        "kernel_hash": MADM1_FECL3_FE_S_P_KERNEL.sha256,
        "calculation_class": "uncommissioned_reduced_mechanistic_screen",
        "activity_model_id": (
            "externally_supplied_coefficients_or_ideal_with_shared_"
            "temperature_corrected_acid_base_fractions"
        ),
        "temperature_c": temperature_c_result,
        "solvent_water_activity": float(value(block.solvent_water_activity)),
        "activity_model_in_domain": False,
        "charge_balance_passed": False,
        "tracked_charge_flux_balance_passed": runtime_balance["property_passed"]["charge"],
        "electron_equivalent_flux_balance_passed": runtime_balance["property_passed"][
            "electron_equivalent"
        ],
        "assumption_ledger": assumption_ledger,
        "mechanism_interpretation": mechanism_interpretation,
        "recommendation_eligible": False,
        "recommendation_blockers": recommendation_blockers,
    }


def solve_commercial_fecl3_product_screen(
    *,
    product: CommercialProductDefinition,
    product_dose: ProductDose,
    plant_flow_m3_d: float,
    inlet_mol_m3: Mapping[str, float],
    hydraulic_residence_time_d: float,
    solids_retention_time_d: float,
    temperature_c: float = 25.0,
    phase_parameters: Mapping[str, Mapping[str, float]] | None = None,
    redox_parameters: Mapping[str, Mapping[str, float]] | None = None,
    oxidation_parameters: Mapping[str, Mapping[str, float]] | None = None,
    aging_parameters: Mapping[str, Mapping[str, float]] | None = None,
    surface_parameters: Mapping[str, Mapping[str, float]] | None = None,
    ligand_parameters: Mapping[str, float] | None = None,
    activity_coefficients: Mapping[str, float] | None = None,
    solvent_water_activity: float = 1.0,
    continuation_path: Sequence[float] = (0.0, 0.1, 0.5, 1.0),
    target_orthophosphate_p_mg_l: float | None = None,
    solver_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the fixed-dose screen from a fully characterized FeCl3 product."""

    flow = _finite_nonnegative(plant_flow_m3_d, field="plant_flow_m3_d")
    if flow <= 0:
        raise ValueError("plant_flow_m3_d must be positive")
    reagent = MADM1_FECL3_FE_S_P_KERNEL.reagents[0]
    conversion = convert_commercial_product_dose(
        product=product,
        dose=product_dose,
        reagent=reagent,
        require_resolved_free_acidity=True,
    )
    sources = {
        component_id: float(moles_per_day) / flow
        for component_id, moles_per_day in conversion.source_component_mol_d.items()
    }
    fe_source = sources.pop("Fe_III_aq", 0.0)
    # The base solver adds stoichiometric 3Cl per mole Fe. Retain only the
    # independently measured free-acid counterion increment here.
    sources["Cl_minus"] = sources.get("Cl_minus", 0.0) - 3.0 * fe_source
    if sources["Cl_minus"] < -1e-12:
        raise ValueError("commercial FeCl3 conversion supplied less than 3 mol Cl/mol Fe")
    sources["Cl_minus"] = max(sources["Cl_minus"], 0.0)
    result = solve_fixed_dose_screen(
        inlet_mol_m3=inlet_mol_m3,
        fecl3_fe_mol_m3_d=fe_source,
        hydraulic_residence_time_d=hydraulic_residence_time_d,
        solids_retention_time_d=solids_retention_time_d,
        temperature_c=temperature_c,
        phase_parameters=phase_parameters,
        redox_parameters=redox_parameters,
        oxidation_parameters=oxidation_parameters,
        aging_parameters=aging_parameters,
        surface_parameters=surface_parameters,
        ligand_parameters=ligand_parameters,
        activity_coefficients=activity_coefficients,
        solvent_water_activity=solvent_water_activity,
        continuation_path=continuation_path,
        target_orthophosphate_p_mg_l=target_orthophosphate_p_mg_l,
        solver_options=solver_options,
        additional_dose_source_mol_m3_d=sources,
    )
    result["dose_input_mode"] = "as_delivered_commercial_product"
    conversion_payload = conversion.public_dict()
    result["commercial_product_conversion"] = conversion_payload
    result["resolved_dose_source_mol_m3_d"] = {
        component_id: float(moles_per_day) / flow
        for component_id, moles_per_day in conversion.source_component_mol_d.items()
    }
    result["plant_flow_m3_d"] = flow
    result["commercial_product_characterized"] = True
    result["recommendation_blockers"] = [
        blocker
        for blocker in result["recommendation_blockers"]
        if blocker != "commercial_product_not_characterized"
    ]
    result["assumption_ledger"]["input_hash"] = hashlib.sha256(
        json.dumps(
            {
                "resolved_screen_input_hash": result["assumption_ledger"]["input_hash"],
                "commercial_product_conversion": conversion_payload,
                "plant_flow_m3_d": flow,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return result


def solve_fixed_dose_uncertainty_screen(
    *,
    inlet_mol_m3: Mapping[str, float],
    fecl3_fe_dose_grid_mol_m3_d: Sequence[float],
    uncertainty_realizations: Sequence[UncertaintyRealization],
    target_orthophosphate_p_mg_l: float,
    hydraulic_residence_time_d: float = 1.0,
    solids_retention_time_d: float = 30.0,
    temperature_c: float = 25.0,
    phase_parameters: Mapping[str, Mapping[str, float]] | None = None,
    redox_parameters: Mapping[str, Mapping[str, float]] | None = None,
    activity_coefficients: Mapping[str, float] | None = None,
    solvent_water_activity: float = 1.0,
    continuation_path: Sequence[float] = (0.0, 0.1, 0.5, 1.0),
    random_seed: int = 0,
) -> dict[str, object]:
    """Run a branch-aware diagnostic dose-grid ensemble on the reduced block.

    Realization parameter keys are deliberately narrow and explicit:
    ``inlet.<component>``, ``inlet_multiplier.<component>``,
    ``phase.<phase>.<parameter>``, ``redox.<reaction>.<parameter>``,
    ``activity_coefficient.<component>``, or one of
    ``temperature_c``, ``hydraulic_residence_time_d``,
    ``solids_retention_time_d`` and ``solvent_water_activity``. The result is
    a screening envelope outside a validated calibration domain and can never
    authorize a setpoint.
    """

    base_inlet = {str(key): float(value) for key, value in inlet_mol_m3.items()}
    base_phase = {
        str(phase_id): {str(key): float(value) for key, value in values.items()}
        for phase_id, values in (phase_parameters or {}).items()
    }
    base_redox = {
        str(reaction_id): {str(key): float(value) for key, value in values.items()}
        for reaction_id, values in (redox_parameters or {}).items()
    }
    base_activity = {
        str(component_id): float(value)
        for component_id, value in (activity_coefficients or {}).items()
    }
    scalar_defaults = {
        "temperature_c": float(temperature_c),
        "hydraulic_residence_time_d": float(hydraulic_residence_time_d),
        "solids_retention_time_d": float(solids_retention_time_d),
        "solvent_water_activity": float(solvent_water_activity),
    }

    def evaluate_dose(
        dose: float,
        realization_parameters: Mapping[str, float],
    ) -> Mapping[str, object]:
        inlet = dict(base_inlet)
        phase = {phase_id: dict(values) for phase_id, values in base_phase.items()}
        redox = {
            reaction_id: dict(values) for reaction_id, values in base_redox.items()
        }
        activity = dict(base_activity)
        scalars = dict(scalar_defaults)
        for path, realized_value in realization_parameters.items():
            pieces = path.split(".")
            if path in scalars:
                scalars[path] = float(realized_value)
            elif len(pieces) == 2 and pieces[0] == "inlet":
                if pieces[1] not in base_inlet:
                    raise KeyError(f"unknown uncertain inlet component {pieces[1]!r}")
                inlet[pieces[1]] = float(realized_value)
            elif len(pieces) == 2 and pieces[0] == "inlet_multiplier":
                if pieces[1] not in base_inlet:
                    raise KeyError(f"unknown uncertain inlet component {pieces[1]!r}")
                inlet[pieces[1]] = base_inlet[pieces[1]] * float(realized_value)
            elif len(pieces) == 3 and pieces[0] == "phase":
                phase.setdefault(pieces[1], {})[pieces[2]] = float(realized_value)
            elif len(pieces) == 3 and pieces[0] == "redox":
                redox.setdefault(pieces[1], {})[pieces[2]] = float(realized_value)
            elif len(pieces) == 2 and pieces[0] == "activity_coefficient":
                activity[pieces[1]] = float(realized_value)
            else:
                raise KeyError(f"unsupported uncertainty parameter path {path!r}")

        result = solve_fixed_dose_screen(
            inlet_mol_m3=inlet,
            fecl3_fe_mol_m3_d=dose,
            hydraulic_residence_time_d=scalars["hydraulic_residence_time_d"],
            solids_retention_time_d=scalars["solids_retention_time_d"],
            temperature_c=scalars["temperature_c"],
            phase_parameters=phase,
            redox_parameters=redox,
            activity_coefficients=activity,
            solvent_water_activity=scalars["solvent_water_activity"],
            continuation_path=continuation_path,
            target_orthophosphate_p_mg_l=target_orthophosphate_p_mg_l,
        )
        active_phases = tuple(result["branch"]["active_phase_set"])
        branch_id = result["branch"]["branch_id"] + "::" + (
            ",".join(active_phases) if active_phases else "no_active_phase"
        )
        return {
            "branch_id": branch_id,
            "solver_converged": bool(result["solver_converged"]),
            "mass_balance_passed": bool(result["mass_balance_passed"]),
            "target_met": bool(result["target_met"]),
            # No realization can be declared in-domain until site calibration
            # and holdout validation are attached to this surface.
            "within_calibration_domain": False,
        }

    canonical_input = json.dumps(
        {
            "inlet_mol_m3": base_inlet,
            "dose_grid": list(fecl3_fe_dose_grid_mol_m3_d),
            "realizations": [
                {
                    "realization_id": row.realization_id,
                    "parameters": dict(sorted(row.parameters.items())),
                }
                for row in uncertainty_realizations
            ],
            "target_orthophosphate_p_mg_l": float(
                target_orthophosphate_p_mg_l
            ),
            "phase_parameters": base_phase,
            "redox_parameters": base_redox,
            "activity_coefficients": base_activity,
            **scalar_defaults,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    envelope = run_branch_aware_dose_grid_ensemble(
        uncertainty_realizations,
        dose_grid=fecl3_fe_dose_grid_mol_m3_d,
        evaluator=evaluate_dose,
        random_seed=int(random_seed),
        input_hash=hashlib.sha256(canonical_input.encode("utf-8")).hexdigest(),
        model_hash=MADM1_FECL3_FE_S_P_KERNEL.sha256,
    )
    envelope["calculation_class"] = (
        "uncommissioned_reduced_mechanistic_uncertainty_screen"
    )
    envelope["target_metric"] = "orthophosphate_p_not_plant_TP"
    envelope["recommendation_eligible"] = False
    envelope["recommendation_blockers"] = sorted(
        set(envelope["recommendation_blockers"])
        | {
            "not_integrated_with_canonical_biological_property_package",
            "plant_TP_is_not_modeled_orthophosphate",
            "site_calibration_and_holdout_validation_absent",
            "uncertainty_realizations_are_caller_supplied_not_a_calibrated_posterior",
        }
    )
    return envelope


__all__ = [
    "set_kinetic_profile",
    "solve_commercial_fecl3_product_screen",
    "solve_fixed_dose_screen",
    "solve_fixed_dose_uncertainty_screen",
]
