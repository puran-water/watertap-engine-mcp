"""Branch-explicit WaterTAP/Pyomo steady compiler for the serial Fe-S-P IR."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from idaes.core.solvers import get_solver
from puran_mcp_support.chemistry import build_endpoint_thermodynamic_audit_request
from puran_mcp_support.schema import MADM1_FECL3_FE_S_P_KERNEL, ChemistryKernelDefinition, SerialChemistryTrainDefinition, mechanism_interpretation_from_ledger
from pyomo.environ import ConcreteModel, Constraint, value
from pyomo.opt import SolverStatus, TerminationCondition

from .steady_solver import (
    _maximum_constraint_residual,
    _restore_variable_value_snapshot,
    _variable_value_snapshot,
    set_kinetic_profile,
)
from .steady_state import (
    build_shared_chemistry_steady_state_block,
    configure_relative_charge_balance,
    initialize_forward_case,
)

_GOVERNED_PROPERTIES = (
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
    "electron_equivalent",
)


def _finite_nonnegative(value_: Any, *, field: str) -> float:
    if isinstance(value_, bool):
        raise ValueError(f"{field} must be numeric, not bool")
    converted = float(value_)
    if not math.isfinite(converted) or converted < 0.0:
        raise ValueError(f"{field} must be finite and nonnegative")
    return converted


def _safe_component_name(cell_id: str) -> str:
    return "chem_" + "".join(character if character.isalnum() else "_" for character in cell_id)


def _steady_whole_train_balance(
    *,
    blocks: Sequence[Any],
    train: SerialChemistryTrainDefinition,
    feed_mol_m3: Mapping[str, float],
    dose_mol_d: Mapping[str, float],
    kernel: ChemistryKernelDefinition,
    absolute_tolerance: float = 1e-7,
    relative_tolerance: float = 1e-7,
) -> dict[str, Any]:
    component_by_id = {component.component_id: component for component in kernel.components}
    terminal = blocks[-1]
    flow = train.hydraulic_flow_m3_d
    external_flux: dict[str, float] = {}
    for component_id in terminal.aqueous_components:
        external_flux[component_id] = (
            float(dose_mol_d.get(component_id, 0.0))
            + flow * float(feed_mol_m3.get(component_id, 0.0))
            - flow * float(value(terminal.outlet[component_id]))
        )
    external_flux["O2_aq"] = external_flux.get("O2_aq", 0.0) + sum(
        zone.oxygen_source_mol_m3_d * zone.volume_m3 for zone in train.zones
    )
    final_rejection = train.zones[-1].particulate_rejection_fraction
    for component_id in terminal.retained_components:
        permeate_loss = flow * (1.0 - final_rejection) * float(value(terminal.solid_inventory[component_id]))
        wasting_loss = sum(
            float(value(block.solid_wasting[component_id])) * float(value(block.zone_volume)) for block in blocks
        )
        external_flux[component_id] = flow * float(feed_mol_m3.get(component_id, 0.0)) - permeate_loss - wasting_loss

    def coefficient(component_id: str, property_id: str) -> float:
        component = component_by_id[component_id]
        if property_id == "electron_equivalent":
            return float(component.electron_equivalent_mol_e)
        return float(component.elemental_coefficients.get(property_id, 0.0))

    rows: dict[str, dict[str, float | bool]] = {}
    for property_id in _GOVERNED_PROPERTIES:
        contributions = [coefficient(component_id, property_id) * flux for component_id, flux in external_flux.items()]
        residual = float(sum(contributions))
        scale = max(float(sum(abs(item) for item in contributions)), 1.0)
        tolerance = absolute_tolerance + relative_tolerance * scale
        rows[property_id] = {
            "residual_mol_property_d": residual,
            "tolerance_mol_property_d": tolerance,
            "passed": math.isfinite(residual) and abs(residual) <= tolerance,
        }
    return {
        "basis": "whole_train_external_molar_flux",
        "properties": rows,
        "passed": all(bool(row["passed"]) for row in rows.values()),
        "H_O_charge_policy": "implicit_water_proton_and_transported_background_charge_reservoirs",
    }


def solve_serial_history_steady_state(
    train: SerialChemistryTrainDefinition,
    *,
    feed_mol_m3: Mapping[str, float],
    inlet_ph: float,
    dose_mol_d: Mapping[str, float] | None = None,
    temperature_c: float = 25.0,
    kernel: ChemistryKernelDefinition = MADM1_FECL3_FE_S_P_KERNEL,
    phase_parameters: Mapping[str, Mapping[str, float]] | None = None,
    redox_parameters: Mapping[str, Mapping[str, float]] | None = None,
    oxidation_parameters: Mapping[str, Mapping[str, float]] | None = None,
    aging_parameters: Mapping[str, Mapping[str, float]] | None = None,
    surface_parameters: Mapping[str, Mapping[str, float]] | None = None,
    ligand_parameters: Mapping[str, float] | None = None,
    activity_coefficients: Mapping[str, float] | None = None,
    solvent_water_activity: float = 1.0,
    species_coverage_fraction: float = 0.0,
    feed_retained_mol_m3: Mapping[str, float] | None = None,
    continuation_path: Sequence[float] = (0.0, 0.1, 0.5, 1.0),
    solver_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Solve the zero-accumulation counterpart of the shared serial train."""

    ph = float(inlet_ph)
    temperature = float(temperature_c)
    if not math.isfinite(ph) or not 0.0 <= ph <= 14.0:
        raise ValueError("inlet_ph must be finite in [0, 14]")
    if not math.isfinite(temperature) or temperature <= -273.15:
        raise ValueError("temperature_c must be finite and above absolute zero")
    if any(zone.gas_boundary_mode != "closed" for zone in train.zones):
        raise ValueError(
            "WaterTAP serial mineral steady solve requires an explicit liquid boundary "
            "from native ADM1 gas closure; nonclosed gas modes are not silently ignored"
        )
    water_activity = _finite_nonnegative(
        solvent_water_activity,
        field="solvent_water_activity",
    )
    species_coverage = _finite_nonnegative(
        species_coverage_fraction,
        field="species_coverage_fraction",
    )
    if water_activity <= 0.0:
        raise ValueError("solvent_water_activity must be positive")
    if species_coverage > 1.0:
        raise ValueError("species_coverage_fraction must be in [0, 1]")
    path = tuple(float(point) for point in continuation_path)
    if (
        not path
        or path[0] != 0.0
        or path[-1] != 1.0
        or any(not math.isfinite(point) or not 0.0 <= point <= 1.0 for point in path)
        or any(right <= left for left, right in zip(path, path[1:]))
    ):
        raise ValueError("continuation_path must increase from 0 to 1")

    dose = {
        str(component_id): _finite_nonnegative(value_, field=f"dose.{component_id}")
        for component_id, value_ in (dose_mol_d or {}).items()
    }
    retained_feed = {
        str(component_id): _finite_nonnegative(value_, field=f"feed_retained.{component_id}")
        for component_id, value_ in (feed_retained_mol_m3 or {}).items()
    }
    model = ConcreteModel()
    blocks: list[Any] = []
    cells: list[tuple[Any, int, str, Any]] = []
    previous = None
    background_charge: float | None = None

    for zone in train.zones:
        for cell_index in range(zone.tanks_in_series):
            cell_id = f"{zone.zone_id}:{cell_index + 1}"
            block = build_shared_chemistry_steady_state_block(kernel=kernel)
            model.add_component(_safe_component_name(cell_id), block)
            block.hydraulic_residence_time_d.set_value(zone.hydraulic_residence_time_d / zone.tanks_in_series)
            block.solids_retention_time_d.set_value(zone.solids_retention_time_d)
            block.zone_volume.set_value(zone.volume_m3 / zone.tanks_in_series)
            block.temperature_k.set_value(temperature + 273.15)
            block.solvent_water_activity.set_value(water_activity)
            for component_id in block.aqueous_components:
                block.inlet[component_id].fix(
                    _finite_nonnegative(
                        feed_mol_m3.get(
                            component_id,
                            10.0 ** (-ph) * 1000.0 if component_id == "H_plus" else 0.0,
                        ),
                        field=f"feed.{component_id}",
                    )
                )
                source = 0.0
                if zone.zone_kind == "injection_micromixing" and cell_index == 0:
                    source += dose.get(component_id, 0.0) * zone.dose_fraction / float(value(block.zone_volume))
                if component_id == "O2_aq":
                    source += zone.oxygen_source_mol_m3_d
                block.dose_source[component_id].fix(source)
                coefficient = float((activity_coefficients or {}).get(component_id, 1.0))
                if not math.isfinite(coefficient) or coefficient <= 0.0:
                    raise ValueError("activity coefficients must be finite and positive")
                block.activity_coefficient[component_id].set_value(coefficient)

            for component_id in block.retained_components:
                block.solid_inlet[component_id].fix(retained_feed.get(component_id, 0.0))
                block.solid_inventory[component_id].set_value(0.0)
                block.solid_wasting[component_id].set_value(0.0)
                block.solid_outlet_fraction[component_id].set_value(
                    1.0 - zone.particulate_rejection_fraction if zone.zone_kind == "mbr_separation" else 1.0
                )

            if previous is not None:
                for component_id in block.aqueous_components:
                    block.inlet[component_id].unfix()
                    block.inlet[component_id].set_value(float(value(previous.outlet[component_id])))
                    model.add_component(
                        f"link_aq_{_safe_component_name(cell_id)}_{component_id}",
                        Constraint(expr=block.inlet[component_id] == previous.outlet[component_id]),
                    )
                for component_id in block.retained_components:
                    block.solid_inlet[component_id].unfix()
                    block.solid_inlet[component_id].set_value(
                        float(value(previous.solid_outlet_fraction[component_id]))
                        * float(value(previous.solid_inventory[component_id]))
                    )
                    model.add_component(
                        f"link_solid_{_safe_component_name(cell_id)}_{component_id}",
                        Constraint(
                            expr=block.solid_inlet[component_id]
                            == previous.solid_outlet_fraction[component_id] * previous.solid_inventory[component_id]
                        ),
                    )

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
            if background_charge is None:
                background_charge = configure_relative_charge_balance(
                    block,
                    inlet_ph=ph,
                )
            else:
                configure_relative_charge_balance(
                    block,
                    inlet_ph=ph,
                    fixed_background_charge_eq_m3=background_charge,
                )
            initialize_forward_case(block)

            blocks.append(block)
            cells.append((zone, cell_index, cell_id, block))
            previous = block

    requested_solver_options = {str(option): option_value for option, option_value in (solver_options or {}).items()}

    def configured_solver(*, numerical_retry: bool = False):
        configured = get_solver()
        configured.options.setdefault("tol", 1e-9)
        configured.options.setdefault("max_iter", 5000)
        configured.options.setdefault("bound_push", 1e-12)
        configured.options.setdefault("bound_frac", 1e-12)
        if numerical_retry:
            configured.options.setdefault("mu_strategy", "adaptive")
        else:
            configured.options.setdefault("constr_viol_tol", 1e-10)
            configured.options.setdefault("acceptable_constr_viol_tol", 1e-10)
        for option, option_value in requested_solver_options.items():
            configured.options[option] = option_value
        return configured

    def solve_attempt(configured, *, label: str):
        try:
            result = configured.solve(model)
        except Exception as exc:
            return False, {
                "attempt": label,
                "solver_status": "error",
                "termination_condition": "solver_exception",
                "converged": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        converged_ = result.solver.status == SolverStatus.ok and result.solver.termination_condition in {
            TerminationCondition.optimal,
            TerminationCondition.locallyOptimal,
        }
        return converged_, {
            "attempt": label,
            "solver_status": str(result.solver.status),
            "termination_condition": str(result.solver.termination_condition),
            "converged": converged_,
        }

    solver = configured_solver()
    stages: list[dict[str, Any]] = []
    for scale in path:
        for block in blocks:
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
        warm_start = _variable_value_snapshot(model)
        converged, primary = solve_attempt(solver, label="primary")
        attempts = [primary]
        if not converged:
            _restore_variable_value_snapshot(warm_start)
            converged, retry = solve_attempt(
                configured_solver(numerical_retry=True),
                label="adaptive_barrier_retry",
            )
            attempts.append(retry)
            if not converged:
                _restore_variable_value_snapshot(warm_start)
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

    solver_converged = bool(stages and len(stages) == len(path) and stages[-1]["converged"])
    residual = _maximum_constraint_residual(model)
    balance = _steady_whole_train_balance(
        blocks=blocks,
        train=train,
        feed_mol_m3={**dict(feed_mol_m3), **retained_feed},
        dose_mol_d=dose,
        kernel=kernel,
    )
    terminal = blocks[-1]
    outlet = {component_id: float(value(terminal.outlet[component_id])) for component_id in terminal.aqueous_components}
    solids = {
        component_id: float(value(terminal.solid_inventory[component_id]))
        for component_id in terminal.retained_components
    }
    rejection = train.zones[-1].particulate_rejection_fraction
    permeate_reactive_p = outlet["PO4"] + (1.0 - rejection) * (
        2.0 * solids.get("Vivianite", 0.0)
        + solids.get("HFO_fresh_surface_P", 0.0)
        + solids.get("HFO_reoxidized_surface_P", 0.0)
        + solids.get("HFO_aged_surface_P", 0.0)
    )
    zone_results = {
        cell_id: {
            "zone_kind": zone.zone_kind,
            "outlet_mol_m3": {
                component_id: float(value(block.outlet[component_id])) for component_id in block.aqueous_components
            },
            "retained_inventory_mol_m3": {
                component_id: float(value(block.solid_inventory[component_id]))
                for component_id in block.retained_components
            },
            "pH": -math.log10(float(value(block.activity["H_plus"]))),
            "charge_residual_eq_m3": float(value(block.reduced_charge_residual_eq_m3)),
        }
        for zone, _, cell_id, block in cells
    }
    active_phases = sorted(
        phase_id for phase_id in terminal.phases if float(value(terminal.solid_inventory[phase_id])) > 1e-12
    )
    final_termination = stages[-1]["termination_condition"] if stages else "not_run"
    status = (
        "converged_screen"
        if solver_converged
        else "solver_error"
        if final_termination == "solver_exception"
        else "nonconverged"
    )
    activities = {
        component_id: float(value(terminal.activity[component_id])) for component_id in terminal.aqueous_components
    }
    phase_saturation = {phase_id: float(value(terminal.saturation_index[phase_id])) for phase_id in terminal.phases}
    terminal_ph = -math.log10(activities["H_plus"])
    mechanism_interpretation = mechanism_interpretation_from_ledger(
        {
            "surface_dataset_id": (kernel.surfaces[0].surface_dataset_id if kernel.surfaces else None),
            "surface_parameter_interpretation": ("history_specific_uncommissioned_serial_profiles"),
            "oxide_history_class": "serial_mixed_history",
            "oxide_history_id": ("fresh_fes_reoxidized_and_aged_inventories_explicit_uncommissioned"),
            "oxide_formation_ph_basis": "zone_charge_or_imposed_pH",
            "active_metal_fraction_basis": "not_identified",
            "effective_active_fe_fraction": None,
            "parameter_contact": None,
            "parameter_contact_evidence_id": None,
            "mechanism_identified": False,
            "mechanism_evidence_id": None,
            "extrapolation_status": "not_assessed",
            "extrapolation_reasons": (),
        }
    )
    phreeqc_endpoint_translation = {
        "kernel_id": kernel.kernel_id,
        "kernel_hash": kernel.sha256,
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
            phase_id: solids.get(phase_id, 0.0) * 1e-3 for phase_id in terminal.phases if phase_id != "HFO"
        },
        "hfo_history_inventory_mol_fe_l": {
            "fresh": solids.get("HFO", 0.0) * 1e-3,
            "fes_reoxidized": solids.get("HFO_reoxidized", 0.0) * 1e-3,
            "aged": solids.get("HFO_aged", 0.0) * 1e-3,
        },
        "p_total_basis": "serial_reduced_PO4_only_not_plant_TP",
        "temperature_c": temperature,
        "ph": terminal_ph,
        "gas_boundary_mode": "closed",
        "gas_boundary": {},
        "analytical_totals_ready": True,
        "translation_ready": True,
        "audit_ready": True,
        "audit_readiness_blockers": [],
        "mechanism_interpretation": mechanism_interpretation,
        **{
            key: mechanism_interpretation[key]
            for key in (
                "mechanism_identified",
                "surface_dataset_id",
                "oxide_history_id",
                "formation_ph_basis",
                "effective_active_fe_fraction",
                "parameter_contact",
                "extrapolation_status",
            )
        },
        "mapping_limitations": [
            "serial PO4 is modeled dissolved reactive P, not plant TP",
            "conditional ligand chemistry is reduced rather than molecule-specific",
            "kinetic retained phases are not equilibrium-phase assertions",
        ],
    }
    mass_balance_passed = solver_converged and residual <= 1e-7 and balance["passed"]
    phreeqc_audit_request = build_endpoint_thermodynamic_audit_request(
        totals_mol_l=phreeqc_endpoint_translation["totals_mol_l"],
        temperature_c=temperature,
        ph=terminal_ph,
        phase_saturation_log10_si=phase_saturation,
        activities=activities,
        solvent_water_activity=water_activity,
        source_mass_balance_passed=mass_balance_passed,
        species_coverage_fraction=species_coverage,
        mechanism_interpretation=mechanism_interpretation,
        kernel=kernel,
    )
    return {
        "status": status,
        "calculation_class": "uncommissioned_serial_zero_accumulation_history_screen",
        "solver_converged": solver_converged,
        "maximum_constraint_residual": residual,
        "mass_balance": balance,
        "mass_balance_passed": mass_balance_passed,
        "ph_charge_solve_passed": solver_converged
        and all(abs(result["charge_residual_eq_m3"]) <= 1e-7 for result in zone_results.values()),
        "train_id": train.train_id,
        "kernel_id": kernel.kernel_id,
        "kernel_hash": kernel.sha256,
        "hydraulic_flow_m3_d": train.hydraulic_flow_m3_d,
        "outlet_mol_m3": outlet,
        "outlet_activities": activities,
        "phase_saturation_index": phase_saturation,
        "retained_inventory_mol_m3": solids,
        "zone_results": zone_results,
        "permeate_orthophosphate_mg_p_l": outlet["PO4"] * 30.973761998,
        "permeate_reactive_p_mg_p_l": permeate_reactive_p * 30.973761998,
        "p_metric_basis": "modeled_inorganic_and_surface_reactive_P_not_plant_TP",
        "branch": {
            "branch_id": "serial_zero_inventory_continuation_branch",
            "initial_inventory_id": "zero_retained_inventory",
            "continuation_path": list(path),
            "dynamic_stability": "not_assessed",
            "steady_residual_norm": residual,
            "active_phase_set": active_phases,
        },
        "continuation_stages": stages,
        "charge_background_eq_m3": background_charge,
        "gas_coupling_class": "native_ADM1_liquid_boundary_required",
        "phreeqc_endpoint_translation": phreeqc_endpoint_translation,
        "phreeqc_audit_request": phreeqc_audit_request,
        "phreeqc_audit_dispatch": {
            "tool": "water-chemistry-mcp.audit_process_chemistry_endpoint",
            "request_ready": True,
            "executed_by_watertap": False,
            "request_kernel_hash": kernel.sha256,
        },
        "mechanism_interpretation": mechanism_interpretation,
        "recommendation_eligible": False,
        "recommendation_blockers": [
            "kinetics_not_calibrated_or_independently_validated",
            "serial_branch_dynamic_stability_not_assessed",
            "native_ADM1_biological_feedback_not_coupled",
            "PHREEQC_endpoint_audit_not_attached",
            "plant_TP_is_not_modeled_reactive_P",
        ],
    }


__all__ = ["solve_serial_history_steady_state"]
