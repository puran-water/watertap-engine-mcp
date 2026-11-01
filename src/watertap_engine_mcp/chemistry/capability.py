"""Machine-readable status of the shared mineral-chemistry integration."""

from __future__ import annotations

from puran_mcp_support.schema import MADM1_FECL3_FE_S_P_KERNEL

from .native_migration import watertap_madm1_native_migration_report


def shared_mineral_chemistry_capability() -> dict[str, object]:
    """Describe implemented WaterTAP chemistry without overstating coupling.

    This manifest travels with canonical flowsheet diagnostics so an agent
    cannot infer that the presence of the reduced development screen means the
    same block is already coupled to the production ADM1/ASM property package.
    """

    return {
        "schema_version": "shared_mineral_chemistry_capability/v1",
        "kernel_id": MADM1_FECL3_FE_S_P_KERNEL.kernel_id,
        "kernel_hash": MADM1_FECL3_FE_S_P_KERNEL.sha256,
        "kernel_commissioning_status": (MADM1_FECL3_FE_S_P_KERNEL.commissioning_status),
        "fixed_dose_screen_action": "screen_madm1_fe_s_p_steady_state",
        "fixed_dose_screen_available": True,
        "serial_history_steady_screen_available": True,
        "serial_history_steady_screen_action": (
            "screen_madm1_fe_s_p_serial_steady_state"
        ),
        "serial_history_steady_screen_entrypoint": (
            'watertap_engine_mcp.chemistry.solve_serial_history_steady_state'
        ),
        "serial_history_states": [
            "amorphous_FeS",
            "mackinawite",
            "fresh_HFO",
            "FeS_reoxidized_HFO",
            "aged_HFO",
            "history_specific_surface_P",
        ],
        "relative_charge_balance_available": True,
        "finite_conditional_ligand_balance_available": True,
        "host_linked_surface_capacity_constraint_available": True,
        "diagnostic_uncertainty_envelope_action": (
            "screen_madm1_fe_s_p_dose_envelope"
        ),
        "diagnostic_uncertainty_envelope_available": True,
        "reduced_branch_local_stability_assessment_available": True,
        "canonical_full_flowsheet_stability_assessment_available": False,
        "uncertainty_envelope_calibration_status": (
            "caller_supplied_realizations_not_calibrated_posterior"
        ),
        "commercial_product_dose_conversion_available": True,
        "commercial_product_supported_dose_units": [
            "kg_product_d",
            "kg_product_h",
            "l_product_d",
            "l_product_h",
            "us_gal_product_d",
            "us_gal_product_h",
        ],
        "commercial_product_unknown_free_acidity_policy": "fail_closed",
        "independent_endpoint_audit_action": (
            "water-chemistry-mcp.audit_process_chemistry_endpoint"
        ),
        "independent_endpoint_audit_available": True,
        "canonical_solve_audit_handoff_attached": False,
        "canonical_solve_audit_handoff_available_when_requested": True,
        "canonical_solve_audit_request_ready_when_requested": True,
        "canonical_solve_handoff_modes": [
            "fixed_dose_forward_screen",
            "serial_fixed_dose_forward_screen",
        ],
        "canonical_solve_handoff_mode": (
            "one_way_post_biological_solve_no_feedback"
        ),
        "canonical_serial_staged_treatment_port_available": True,
        "canonical_serial_staged_treatment_port_id": (
            "post_chemistry_mbr_permeate"
        ),
        "canonical_serial_staged_treatment_port_semantics": {
            "final_for_configured_chemistry_train": True,
            "final_plant_effluent": False,
            "plant_tp_modeled": False,
            "upstream_biological_feedback_applied": False,
        },
        "canonical_requested_chemistry_failure_policy": (
            "demote_combined_result_preserve_biological_endpoint_for_diagnosis"
        ),
        "canonical_chemistry_output_validity_gate": [
            "solver_converged",
            "mass_balance_passed",
        ],
        "native_state_migration": watertap_madm1_native_migration_report(),
        "canonical_solve_flowsheet_coupled": False,
        "canonical_supported_model_variants": [],
        "canonical_supported_reagents": [],
        "canonical_diagnostic_handoff_model_variants": ["mADM1"],
        "canonical_diagnostic_handoff_reagents": ["FeCl3"],
        "planned_first_model_variant": "mADM1",
        "planned_first_reagent": "FeCl3",
        "recommendation_eligible": False,
        "operational_use": (
            "diagnostic_fixed_dose_and_screening_uncertainty_envelopes_only"
        ),
        "recommendation_blockers": [
            "shared_kernel_not_feedback_coupled_to_canonical_solve_flowsheet",
            "canonical_handoff_is_one_way_without_biological_feedback",
            "native_ADM1_Fe_S_mineral_property_states_not_implemented",
            "kinetics_not_site_calibrated_or_holdout_validated",
            "full_flowsheet_dynamic_branch_stability_not_established",
            "PHREEQC_endpoint_audit_request_emitted_but_cross_server_audit_not_executed",
        ],
    }


__all__ = ["shared_mineral_chemistry_capability"]
