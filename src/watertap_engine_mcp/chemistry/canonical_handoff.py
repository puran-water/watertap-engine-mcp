"""Canonical ``solve_flowsheet`` handoff to the shared mADM1 Fe-S-P screen.

This is an intentionally one-way, post-biological-solve coupling. The solved
mADM1 soluble inorganic-P state supplies the PO4-P inventory to the shared
steady chemistry block; explicitly observed/supplied Fe, S, carbonate and
counterion states supply the balance of the reduced chemistry boundary. The
chemistry result does not feed back into the biological property package yet.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from puran_mcp_support.chemistry import build_endpoint_thermodynamic_audit_request, coerce_commercial_product_definition, coerce_product_dose, convert_commercial_product_dose, selected_phase_activities
from puran_mcp_support.schema import MADM1_FECL3_FE_S_P_KERNEL, coerce_serial_chemistry_train

from .serial_steady import solve_serial_history_steady_state
from .steady_solver import (
    solve_commercial_fecl3_product_screen,
    solve_fixed_dose_screen,
)

_SUPPORTED_MODES = frozenset(
    {
        "fixed_dose_forward_screen",
        "serial_fixed_dose_forward_screen",
    }
)
_SUPPORTED_REAGENT = "FeCl3"
_SUPPORTED_SOURCE_PORT = "methanogenic_reactor_liquid_outlet"
_RESULT_PORT_BY_MODE = {
    "fixed_dose_forward_screen": "shared_chemistry_screen_outlet",
    "serial_fixed_dose_forward_screen": "post_chemistry_mbr_permeate",
}
_SUPPORTED_AUDIT_GASES = frozenset(
    {
        "ch4",
        "ch4(g)",
        "co2",
        "co2(g)",
        "h2",
        "h2(g)",
        "h2s",
        "h2s(g)",
        "n2",
        "n2(g)",
        "nh3",
        "nh3(g)",
        "o2",
        "o2(g)",
    }
)
_REQUIRED_NON_P_ANALYTICAL_TOTALS = frozenset(
    {
        "Fe_III_total",
        "Fe_II_total",
        "H2_aq",
        "sulfide_total",
        "sulfate_total",
        "TIC_total",
        "chloride_total",
    }
)
_ANALYTICAL_TO_REDUCED_STATE = {
    "Fe_III_total": "Fe_III_aq",
    "Fe_II_total": "Fe_II_aq",
    "H2_aq": "H2_aq",
    "sulfide_total": "HS_minus",
    "sulfate_total": "SO4",
    "TIC_total": "CO3",
    "chloride_total": "Cl_minus",
}


class CanonicalChemistryHandoffError(RuntimeError):
    """Fail-closed error carrying any partial chemistry result for diagnosis."""

    def __init__(
        self,
        message: str,
        *,
        chemistry_result: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.chemistry_result = (
            dict(chemistry_result) if isinstance(chemistry_result, Mapping) else None
        )


def require_valid_forward_chemistry_result(
    chemistry_result: Mapping[str, Any],
) -> None:
    """Require convergence and balance closure before exposing chemistry output.

    A converged, balanced forward calculation remains valid when its requested
    residual target is not met; ``target_met`` is a performance result, not a
    solver-success surrogate.  Missing status fields fail closed rather than
    being coerced to ``False`` and quietly attached to an otherwise optimal
    biological solve.
    """

    missing = [
        field
        for field in ("solver_converged", "mass_balance_passed")
        if field not in chemistry_result
    ]
    if missing:
        raise CanonicalChemistryHandoffError(
            "canonical chemistry result omitted required validity fields: "
            + ", ".join(missing),
            chemistry_result=chemistry_result,
        )

    invalid_types = [
        field
        for field in ("solver_converged", "mass_balance_passed")
        if not isinstance(chemistry_result.get(field), bool)
    ]
    if invalid_types:
        raise CanonicalChemistryHandoffError(
            "canonical chemistry validity fields must be explicit booleans: "
            + ", ".join(invalid_types),
            chemistry_result=chemistry_result,
        )

    failures = []
    if chemistry_result["solver_converged"] is not True:
        failures.append("solver_not_converged")
    if chemistry_result["mass_balance_passed"] is not True:
        failures.append("mass_balance_not_closed")
    if failures:
        raise CanonicalChemistryHandoffError(
            "canonical chemistry forward result is invalid: " + ", ".join(failures),
            chemistry_result=chemistry_result,
        )


_ALLOWED_KEYS = frozenset(
    {
        "mode",
        "reagent_id",
        "source_port",
        "result_port",
        "inlet_non_p_analytical_totals_mol_m3",
        "inlet_ph",
        "temperature_c",
        "fecl3_fe_mol_m3_d",
        "commercial_product",
        "product_dose",
        "plant_flow_m3_d",
        "hydraulic_residence_time_d",
        "solids_retention_time_d",
        "phase_parameters",
        "redox_parameters",
        "oxidation_parameters",
        "aging_parameters",
        "surface_parameters",
        "ligand_parameters",
        "activity_coefficients",
        "solvent_water_activity",
        "continuation_path",
        "target_orthophosphate_p_mg_l",
        "gas_boundary_mode",
        "gas_boundary",
        "gas_volume_l",
        "gas_total_pressure_atm",
        "species_coverage_fraction",
        "serial_train",
        "feed_retained_mol_m3",
    }
)


def _finite(value: Any, *, field: str, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric, not bool")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    if minimum is not None and number < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    return number


def normalize_canonical_madm1_chemistry_case(
    case: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the explicit one-way mADM1 chemistry-boundary contract."""

    if not isinstance(case, Mapping):
        raise ValueError("mineral_chemistry_case must be a mapping")
    unknown = sorted(set(case) - _ALLOWED_KEYS)
    if unknown:
        raise ValueError(f"mineral_chemistry_case has unknown fields {unknown}")
    mode = str(case.get("mode", "")).strip()
    if mode not in _SUPPORTED_MODES:
        raise ValueError(f"mineral_chemistry_case.mode must be one of {sorted(_SUPPORTED_MODES)}")
    reagent = str(case.get("reagent_id", "")).strip()
    if reagent != _SUPPORTED_REAGENT:
        raise ValueError("the first canonical chemistry handoff supports FeCl3 only")
    source_port = str(case.get("source_port", "")).strip()
    if source_port != _SUPPORTED_SOURCE_PORT:
        raise ValueError(
            "the first canonical chemistry handoff requires source_port="
            f"{_SUPPORTED_SOURCE_PORT!r}; direct use of an already-treated "
            "MBR-permeate source is not implemented"
        )
    expected_result_port = _RESULT_PORT_BY_MODE[mode]
    result_port_supplied = "result_port" in case
    result_port = str(case.get("result_port", expected_result_port)).strip()
    if result_port != expected_result_port:
        raise ValueError(
            f"mineral_chemistry_case.result_port must be {expected_result_port!r} "
            f"for mode={mode!r}"
        )

    raw_non_p = case.get("inlet_non_p_analytical_totals_mol_m3")
    if not isinstance(raw_non_p, Mapping):
        raise ValueError("inlet_non_p_analytical_totals_mol_m3 must be a mapping")
    supplied = set(raw_non_p)
    if supplied != _REQUIRED_NON_P_ANALYTICAL_TOTALS:
        missing = sorted(_REQUIRED_NON_P_ANALYTICAL_TOTALS - supplied)
        extra = sorted(supplied - _REQUIRED_NON_P_ANALYTICAL_TOTALS)
        raise ValueError(
            "inlet_non_p_analytical_totals_mol_m3 must contain the exact "
            "reduced Fe/S/C/Cl/H2 boundary; "
            f"missing={missing}, extra={extra}. Total inorganic P is linked "
            "from solved S_IP."
        )
    inlet_non_p = {
        str(component_id): _finite(
            value,
            field=f"inlet_non_p_analytical_totals_mol_m3.{component_id}",
            minimum=0.0,
        )
        for component_id, value in raw_non_p.items()
    }
    inlet_ph = case.get("inlet_ph")
    if inlet_ph is not None:
        inlet_ph = _finite(inlet_ph, field="inlet_ph", minimum=0.0)
        if inlet_ph > 14.0:
            raise ValueError("inlet_ph must be in [0, 14]")
    temperature_c = case.get("temperature_c")
    if temperature_c is not None:
        temperature_c = _finite(temperature_c, field="temperature_c")
        if temperature_c <= -273.15:
            raise ValueError("temperature_c must be above absolute zero")

    gas_mode = str(case.get("gas_boundary_mode", "closed")).strip()
    if gas_mode not in {"closed", "fixed_partial_pressure", "finite_headspace"}:
        raise ValueError("gas_boundary_mode is invalid")
    raw_gas = case.get("gas_boundary") or {}
    if not isinstance(raw_gas, Mapping):
        raise ValueError("gas_boundary must be a mapping")
    gas = {
        str(component_id): _finite(
            value,
            field=f"gas_boundary.{component_id}",
            minimum=0.0,
        )
        for component_id, value in raw_gas.items()
    }
    unsupported_gases = sorted(
        component_id for component_id in gas if component_id.strip().lower() not in _SUPPORTED_AUDIT_GASES
    )
    if unsupported_gases:
        raise ValueError(
            f"gas_boundary contains components unsupported by the PHREEQC endpoint audit: {unsupported_gases}"
        )
    if gas_mode == "closed" and gas:
        raise ValueError("closed gas boundary must not specify gas components")
    if gas_mode != "closed" and not gas:
        raise ValueError(f"{gas_mode} gas boundary requires gas components")
    if gas_mode == "fixed_partial_pressure" and any(value <= 0.0 for value in gas.values()):
        raise ValueError("fixed gas partial pressures must be positive")
    gas_volume = case.get("gas_volume_l")
    gas_pressure = case.get("gas_total_pressure_atm")
    if gas_mode == "finite_headspace":
        gas_volume = _finite(gas_volume, field="gas_volume_l", minimum=0.0)
        if gas_volume <= 0.0:
            raise ValueError("gas_volume_l must be positive")
        if gas_pressure is not None:
            gas_pressure = _finite(
                gas_pressure,
                field="gas_total_pressure_atm",
                minimum=0.0,
            )
            if gas_pressure <= 0.0:
                raise ValueError("gas_total_pressure_atm must be positive")
        if not any(value > 0.0 for value in gas.values()):
            raise ValueError("finite headspace requires a positive component partial pressure")
    elif gas_volume is not None or gas_pressure is not None:
        raise ValueError("gas volume/total pressure are valid only for finite_headspace")

    coverage = _finite(
        case.get("species_coverage_fraction", 0.0),
        field="species_coverage_fraction",
        minimum=0.0,
    )
    if coverage > 1.0:
        raise ValueError("species_coverage_fraction must be in [0, 1]")

    continuation = tuple(
        _finite(value, field="continuation_path", minimum=0.0)
        for value in case.get("continuation_path", (0.0, 0.1, 0.5, 1.0))
    )
    product_fields = {"commercial_product", "product_dose", "plant_flow_m3_d"}
    supplied_product_fields = product_fields & set(case)
    direct_dose_supplied = "fecl3_fe_mol_m3_d" in case
    if supplied_product_fields and supplied_product_fields != product_fields:
        raise ValueError(
            "commercial-product dose mode requires commercial_product, product_dose, and plant_flow_m3_d together"
        )
    if supplied_product_fields and direct_dose_supplied:
        raise ValueError("supply either fecl3_fe_mol_m3_d or the commercial-product dose, not both")
    commercial_product = (
        coerce_commercial_product_definition(case["commercial_product"]) if supplied_product_fields else None
    )
    product_dose = coerce_product_dose(case["product_dose"]) if supplied_product_fields else None
    plant_flow = (
        _finite(case["plant_flow_m3_d"], field="plant_flow_m3_d", minimum=0.0) if supplied_product_fields else None
    )
    if plant_flow is not None and plant_flow <= 0:
        raise ValueError("plant_flow_m3_d must be positive")
    serial_train = None
    if mode == "serial_fixed_dose_forward_screen":
        if case.get("serial_train") is None:
            raise ValueError("serial_train is required for serial chemistry mode")
        serial_train = coerce_serial_chemistry_train(case["serial_train"])
        if plant_flow is not None and not math.isclose(
            plant_flow,
            serial_train.hydraulic_flow_m3_d,
            rel_tol=1e-6,
            abs_tol=1e-9,
        ):
            raise ValueError("commercial-product plant_flow_m3_d conflicts with the serial train hydraulic flow")
    elif case.get("serial_train") is not None:
        raise ValueError("serial_train is valid only in serial chemistry mode")

    normalized = {
        "mode": mode,
        "reagent_id": reagent,
        "source_port": source_port,
        "result_port": result_port,
        "result_port_source": (
            "caller_declared" if result_port_supplied else "mode_canonical_default"
        ),
        "inlet_non_p_analytical_totals_mol_m3": inlet_non_p,
        "inlet_ph": inlet_ph,
        "temperature_c": temperature_c,
        "fecl3_fe_mol_m3_d": _finite(
            case.get("fecl3_fe_mol_m3_d", 0.0),
            field="fecl3_fe_mol_m3_d",
            minimum=0.0,
        ),
        "dose_input_mode": (
            "as_delivered_commercial_product" if supplied_product_fields else "direct_molar_fecl3_as_fe"
        ),
        "commercial_product": commercial_product,
        "product_dose": product_dose,
        "plant_flow_m3_d": plant_flow,
        "serial_train": serial_train,
        "feed_retained_mol_m3": _mapping_copy(case.get("feed_retained_mol_m3"), field="feed_retained_mol_m3"),
        "hydraulic_residence_time_d": _finite(
            case.get("hydraulic_residence_time_d", 1.0),
            field="hydraulic_residence_time_d",
            minimum=0.0,
        ),
        "solids_retention_time_d": _finite(
            case.get("solids_retention_time_d", 30.0),
            field="solids_retention_time_d",
            minimum=0.0,
        ),
        "phase_parameters": _mapping_copy(case.get("phase_parameters"), field="phase_parameters"),
        "redox_parameters": _mapping_copy(case.get("redox_parameters"), field="redox_parameters"),
        "oxidation_parameters": _mapping_copy(case.get("oxidation_parameters"), field="oxidation_parameters"),
        "aging_parameters": _mapping_copy(case.get("aging_parameters"), field="aging_parameters"),
        "surface_parameters": _mapping_copy(case.get("surface_parameters"), field="surface_parameters"),
        "ligand_parameters": _mapping_copy(case.get("ligand_parameters"), field="ligand_parameters"),
        "activity_coefficients": _mapping_copy(case.get("activity_coefficients"), field="activity_coefficients"),
        "solvent_water_activity": _finite(
            case.get("solvent_water_activity", 1.0),
            field="solvent_water_activity",
            minimum=0.0,
        ),
        "continuation_path": continuation,
        "target_orthophosphate_p_mg_l": (
            None
            if case.get("target_orthophosphate_p_mg_l") is None
            else _finite(
                case["target_orthophosphate_p_mg_l"],
                field="target_orthophosphate_p_mg_l",
                minimum=0.0,
            )
        ),
        "gas_boundary_mode": gas_mode,
        "gas_boundary": gas,
        "gas_volume_l": gas_volume,
        "gas_total_pressure_atm": gas_pressure,
        "species_coverage_fraction": coverage,
    }
    if normalized["hydraulic_residence_time_d"] <= 0.0:
        raise ValueError("hydraulic_residence_time_d must be positive")
    if normalized["solids_retention_time_d"] <= 0.0:
        raise ValueError("solids_retention_time_d must be positive")
    if normalized["solvent_water_activity"] <= 0.0:
        raise ValueError("solvent_water_activity must be positive")
    return normalized


def _mapping_copy(value: Any, *, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _resolved_boundary_value(
    *,
    solved: Any,
    declared: float | None,
    field: str,
    tolerance: float,
) -> tuple[float, str]:
    """Prefer a solved boundary and reject a conflicting caller assertion."""

    solved_value = None
    if solved is not None:
        solved_value = _finite(solved, field=f"solved.{field}")
    if solved_value is None and declared is None:
        raise ValueError(f"{field} is unavailable from the solved mADM1 endpoint and was not supplied")
    if solved_value is None:
        return float(declared), "caller_supplied_fallback"
    if declared is not None and abs(solved_value - declared) > tolerance:
        raise ValueError(
            f"declared {field}={declared} conflicts with solved endpoint {field}={solved_value} (tolerance {tolerance})"
        )
    return solved_value, "solved_madm1_endpoint"


def _audit_activity_subset(
    *,
    phase_ids: tuple[str, ...],
    activities: Mapping[str, float],
    solvent_water_activity: float,
) -> dict[str, float]:
    return selected_phase_activities(
        phase_ids,
        activities,
        solvent_water_activity=solvent_water_activity,
        kernel=MADM1_FECL3_FE_S_P_KERNEL,
    )


def run_canonical_madm1_chemistry_handoff(
    *,
    effluent_payload: Mapping[str, Any],
    case: Mapping[str, Any],
) -> dict[str, Any]:
    """Run the shared screen from one solved WaterTAP mADM1 endpoint."""

    normalized = normalize_canonical_madm1_chemistry_case(case)
    liquid = effluent_payload.get("liquid_effluent")
    if not isinstance(liquid, Mapping):
        raise ValueError("solved mADM1 liquid-effluent payload is missing")
    concentrations = liquid.get("conc_mass_comp_kg_m3")
    if not isinstance(concentrations, Mapping) or concentrations.get("S_IP") is None:
        raise ValueError("solved mADM1 endpoint omitted S_IP")
    s_ip_kg_p_m3 = _finite(concentrations["S_IP"], field="solved.S_IP_kg_P_m3", minimum=0.0)
    solved_temperature_k = liquid.get("temperature_K")
    solved_temperature_c = (
        None
        if solved_temperature_k is None
        else _finite(
            solved_temperature_k,
            field="solved.temperature_K",
            minimum=0.0,
        )
        - 273.15
    )
    boundary_ph, ph_source = _resolved_boundary_value(
        solved=liquid.get("pH"),
        declared=normalized["inlet_ph"],
        field="pH",
        tolerance=0.05,
    )
    if not 0.0 <= boundary_ph <= 14.0:
        raise ValueError("resolved pH must be in [0, 14]")
    boundary_temperature_c, temperature_source = _resolved_boundary_value(
        solved=solved_temperature_c,
        declared=normalized["temperature_c"],
        field="temperature_c",
        tolerance=0.1,
    )
    if boundary_temperature_c <= -273.15:
        raise ValueError("resolved temperature_c must be above absolute zero")

    inlet = {
        _ANALYTICAL_TO_REDUCED_STATE[analytical_id]: value
        for analytical_id, value in normalized["inlet_non_p_analytical_totals_mol_m3"].items()
    }
    inlet["PO4"] = s_ip_kg_p_m3 / 0.030973761998
    # This reduced block carries H+ explicitly. The analytical pH belongs to
    # the stated inlet boundary; any reaction-generated H+ changes the solved
    # endpoint activity and therefore the emitted endpoint pH.
    inlet["H_plus"] = 10.0 ** (-boundary_ph) * 1000.0
    shared_inputs = {
        "inlet_mol_m3": inlet,
        "hydraulic_residence_time_d": normalized["hydraulic_residence_time_d"],
        "solids_retention_time_d": normalized["solids_retention_time_d"],
        "temperature_c": boundary_temperature_c,
        "phase_parameters": normalized["phase_parameters"],
        "redox_parameters": normalized["redox_parameters"],
        "oxidation_parameters": normalized["oxidation_parameters"],
        "aging_parameters": normalized["aging_parameters"],
        "surface_parameters": normalized["surface_parameters"],
        "ligand_parameters": normalized["ligand_parameters"],
        "activity_coefficients": normalized["activity_coefficients"],
        "solvent_water_activity": normalized["solvent_water_activity"],
        "continuation_path": normalized["continuation_path"],
        "target_orthophosphate_p_mg_l": normalized["target_orthophosphate_p_mg_l"],
    }
    serial_mode = normalized["mode"] == "serial_fixed_dose_forward_screen"
    if serial_mode:
        train = normalized["serial_train"]
        flow = train.hydraulic_flow_m3_d
        conversion_payload = None
        if normalized["dose_input_mode"] == "as_delivered_commercial_product":
            conversion = convert_commercial_product_dose(
                product=normalized["commercial_product"],
                dose=normalized["product_dose"],
                reagent=MADM1_FECL3_FE_S_P_KERNEL.reagents[0],
                require_resolved_free_acidity=True,
            )
            dose_mol_d = dict(conversion.source_component_mol_d)
            conversion_payload = conversion.public_dict()
        else:
            fe_mol_d = normalized["fecl3_fe_mol_m3_d"] * flow
            dose_mol_d = {
                "Fe_III_aq": fe_mol_d,
                "Cl_minus": 3.0 * fe_mol_d,
            }
        screen = solve_serial_history_steady_state(
            train,
            feed_mol_m3=inlet,
            inlet_ph=boundary_ph,
            dose_mol_d=dose_mol_d,
            temperature_c=boundary_temperature_c,
            phase_parameters=normalized["phase_parameters"],
            redox_parameters=normalized["redox_parameters"],
            oxidation_parameters=normalized["oxidation_parameters"],
            aging_parameters=normalized["aging_parameters"],
            surface_parameters=normalized["surface_parameters"],
            ligand_parameters=normalized["ligand_parameters"],
            activity_coefficients=normalized["activity_coefficients"],
            solvent_water_activity=normalized["solvent_water_activity"],
            feed_retained_mol_m3=normalized["feed_retained_mol_m3"],
            continuation_path=normalized["continuation_path"],
        )
        target = normalized["target_orthophosphate_p_mg_l"]
        screen["target_met"] = (
            None
            if target is None
            else bool(screen["solver_converged"]) and screen["permeate_orthophosphate_mg_p_l"] <= target
        )
        screen["target_orthophosphate_p_mg_l"] = target
        screen["dose_input_mode"] = normalized["dose_input_mode"]
        screen["resolved_dose_source_mol_d"] = dose_mol_d
        screen["commercial_product_conversion"] = conversion_payload
        screen["commercial_product_characterized"] = conversion_payload is not None
    elif normalized["dose_input_mode"] == "as_delivered_commercial_product":
        screen = solve_commercial_fecl3_product_screen(
            product=normalized["commercial_product"],
            product_dose=normalized["product_dose"],
            plant_flow_m3_d=normalized["plant_flow_m3_d"],
            **shared_inputs,
        )
    else:
        screen = solve_fixed_dose_screen(
            fecl3_fe_mol_m3_d=normalized["fecl3_fe_mol_m3_d"],
            **shared_inputs,
        )
    require_valid_forward_chemistry_result(screen)
    activities = dict(screen["outlet_activities"])
    endpoint_ph = -math.log10(activities["H_plus"])
    endpoint = dict(screen["phreeqc_endpoint_translation"])
    endpoint.update(
        {
            "temperature_c": boundary_temperature_c,
            "ph": endpoint_ph,
            "gas_boundary_mode": normalized["gas_boundary_mode"],
            "gas_boundary": normalized["gas_boundary"],
        }
    )
    screen["phreeqc_endpoint_translation"] = endpoint
    screen["canonical_handoff"] = {
        "attached_to_solve_flowsheet": True,
        "coupling_class": (
            "one_way_post_biological_solve_serial_history_no_feedback"
            if serial_mode
            else "one_way_post_biological_solve_no_feedback"
        ),
        "source_model_variant": "WaterTAP_ModifiedADM1",
        "source_port": normalized["source_port"],
        "result_port": normalized["result_port"],
        "result_port_source": normalized["result_port_source"],
        "source_component": "S_IP",
        "source_component_basis": "kg_P_m3",
        "source_s_ip_kg_p_m3": s_ip_kg_p_m3,
        "dose_input_mode": normalized["dose_input_mode"],
        "mapped_po4_mol_m3": inlet["PO4"],
        "analytical_total_to_reduced_state_map": dict(_ANALYTICAL_TO_REDUCED_STATE),
        "reduced_state_semantics": {
            "PO4": "analytical_total_inorganic_P_with_free_PO4_activity_speciated",
            "HS_minus": "analytical_total_S_minus_II_with_free_HS_activity_speciated",
            "CO3": "analytical_TIC_with_free_CO3_activity_speciated",
            "Fe_III_aq": "analytical_Fe_III_treated_as_free_pending_complexation_closure",
            "Fe_II_aq": "analytical_Fe_II_treated_as_free_pending_complexation_closure",
        },
        "inlet_ph": boundary_ph,
        "inlet_ph_source": ph_source,
        "temperature_c": boundary_temperature_c,
        "temperature_source": temperature_source,
        "chemistry_target_metric": "orthophosphate_p",
        "plant_tp_modeled": False,
        "biological_feedback_applied": False,
        "serial_zone_history_modeled": serial_mode,
        "serial_train_id": (normalized["serial_train"].train_id if serial_mode else None),
        "gas_boundary_applied_to_reduced_screen": False,
        "chemistry_output_available": True,
        "recommendation_eligible": False,
    }
    if serial_mode:
        mbr_zone_ids = [
            zone.zone_id
            for zone in normalized["serial_train"].zones
            if zone.zone_kind == "mbr_separation"
        ]
        staged_output = {
            "schema_version": "watertap_staged_treatment_port/v1",
            "available": True,
            "port_id": normalized["result_port"],
            "port_role": "configured_shared_chemistry_train_terminal_liquid",
            "source_port_id": normalized["source_port"],
            "chemistry_train_id": normalized["serial_train"].train_id,
            "terminal_separator_zone_id": mbr_zone_ids[-1],
            "aqueous_state_mol_m3": dict(screen.get("outlet_mol_m3") or {}),
            "orthophosphate_mg_p_l": screen["permeate_orthophosphate_mg_p_l"],
            "reactive_p_mg_p_l": screen.get("permeate_reactive_p_mg_p_l"),
            "total_p_mg_p_l": None,
            "measurement_basis": (
                screen.get("p_metric_basis")
                or "modeled_inorganic_and_surface_reactive_P_not_plant_TP"
            ),
            "target_orthophosphate_mg_p_l": screen.get(
                "target_orthophosphate_p_mg_l"
            ),
            "target_met": screen.get("target_met"),
            "solver_converged": bool(screen.get("solver_converged")),
            "mass_balance_passed": bool(screen.get("mass_balance_passed")),
            "is_final_for_configured_chemistry_train": True,
            "is_final_plant_effluent": False,
            "plant_tp_modeled": False,
            "upstream_biological_feedback_applied": False,
            "downstream_unit_operations_modeled": False,
            "eligible_for_plant_tp_compliance": False,
            "recommendation_eligible": False,
        }
        screen["staged_treatment_output"] = staged_output
        screen["canonical_handoff"].update(
            {
                "result_port_role": staged_output["port_role"],
                "result_port_is_configured_train_terminal": True,
                "result_port_is_plant_final_effluent": False,
            }
        )
    audit_phase_ids = tuple(screen["phase_saturation_index"])
    screen["phreeqc_audit_request"] = build_endpoint_thermodynamic_audit_request(
        totals_mol_l=endpoint["totals_mol_l"],
        temperature_c=boundary_temperature_c,
        ph=endpoint_ph,
        gas_boundary_mode=normalized["gas_boundary_mode"],
        gas_boundary=normalized["gas_boundary"],
        gas_volume_l=normalized["gas_volume_l"],
        gas_total_pressure_atm=normalized["gas_total_pressure_atm"],
        phase_saturation_log10_si=screen["phase_saturation_index"],
        kinetic_phase_ids=audit_phase_ids,
        activities=activities,
        solvent_water_activity=normalized["solvent_water_activity"],
        source_mass_balance_passed=screen["mass_balance_passed"],
        species_coverage_fraction=normalized["species_coverage_fraction"],
        reduced_model_database_hash=None,
        database="minteq.v4.dat",
        mechanism_interpretation=screen["mechanism_interpretation"],
        kernel=MADM1_FECL3_FE_S_P_KERNEL,
    )
    screen["phreeqc_audit_dispatch"] = {
        "tool": "water-chemistry-mcp.audit_process_chemistry_endpoint",
        "request_ready": True,
        "executed_by_watertap": False,
        "request_kernel_hash": MADM1_FECL3_FE_S_P_KERNEL.sha256,
    }
    screen["recommendation_eligible"] = False
    blockers = list(screen.get("recommendation_blockers") or [])
    blockers.extend(
        [
            "canonical_handoff_is_one_way_without_biological_feedback",
            (
                "serial_history_is_zero_accumulation_without_native_state_feedback"
                if serial_mode
                else "serial_injection_anaerobic_oxidation_MBR_history_not_modeled"
            ),
            "PHREEQC_audit_request_emitted_but_not_executed_by_WaterTAP",
            "aqueous_Fe_complexation_not_closed_total_Fe_treated_as_free",
        ]
    )
    if normalized["gas_boundary_mode"] != "closed":
        blockers.append("declared_gas_boundary_not_coupled_to_reduced_screen")
    screen["recommendation_blockers"] = list(dict.fromkeys(blockers))
    return screen


def attach_staged_treatment_port(
    effluent_payload: Mapping[str, Any],
    chemistry_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach a validated serial-chemistry terminal port to a SolveResult payload.

    The native ADM1 liquid outlet is preserved.  The additional port is a
    separate modeled stage, never an overwrite of the biological endpoint.
    """

    payload = dict(effluent_payload)
    staged = chemistry_result.get("staged_treatment_output")
    if staged is None:
        return payload
    if not isinstance(staged, Mapping) or staged.get("available") is not True:
        raise ValueError("staged treatment output is malformed or unavailable")
    require_valid_forward_chemistry_result(chemistry_result)
    if staged.get("solver_converged") is not True:
        raise ValueError("staged treatment output is not solver-converged")
    if staged.get("mass_balance_passed") is not True:
        raise ValueError("staged treatment output failed chemistry mass balance")
    port_id = str(staged.get("port_id", "")).strip()
    if port_id != "post_chemistry_mbr_permeate":
        raise ValueError("serial chemistry staged output has a noncanonical port_id")
    if staged.get("plant_tp_modeled") is not False:
        raise ValueError("staged chemistry output cannot be labeled as plant TP")
    if staged.get("upstream_biological_feedback_applied") is not False:
        raise ValueError("one-way WaterTAP chemistry output cannot claim biological feedback")
    ports = dict(payload.get("staged_treatment_ports") or {})
    if port_id in ports:
        raise ValueError(f"duplicate staged treatment port {port_id!r}")
    ports[port_id] = dict(staged)
    payload["staged_treatment_ports"] = ports
    return payload


__all__ = [
    "CanonicalChemistryHandoffError",
    "attach_staged_treatment_port",
    "normalize_canonical_madm1_chemistry_case",
    "require_valid_forward_chemistry_result",
    "run_canonical_madm1_chemistry_handoff",
]
