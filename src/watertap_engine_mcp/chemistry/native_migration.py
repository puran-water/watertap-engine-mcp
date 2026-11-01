"""Declared WaterTAP ModifiedADM1 native-state migration boundary."""

from __future__ import annotations

from collections.abc import Iterable

from puran_mcp_support.schema import MADM1_FECL3_FE_S_P_KERNEL, NativeStateMigrationContract, NativeStateRequirement

WATERTAP_MADM1_FE_S_P_NATIVE_MIGRATION = NativeStateMigrationContract(
    migration_id="watertap-modified-adm1-shared-fe-s-p-native-v1",
    engine_id="watertap-engine-mcp",
    model_variant="ModifiedADM1",
    kernel_id=MADM1_FECL3_FE_S_P_KERNEL.kernel_id,
    kernel_hash=MADM1_FECL3_FE_S_P_KERNEL.sha256,
    source_layout_version="watertap-upstream-modified-adm1",
    target_layout_version="watertap-modified-adm1-shared-fe-s-p/v1",
    checkpoint_policy="cold_reseed",
    requirements=(
        NativeStateRequirement(
            "existing_adm1_chemical_totals",
            ("H2_aq", "PO4", "CO3"),
            "differential_state",
            "partial_native",
            ("S_h2", "S_IP", "S_IC"),
            "native COD/P/C bases exist but shared chemistry source terms are not coupled",
            "existing_ADM1_totals_do_not_receive_shared_chemistry_source_terms",
        ),
        NativeStateRequirement(
            "iron_and_sulfur_aqueous_totals",
            ("Fe_III_aq", "Fe_II_aq", "HS_minus", "SO4", "S_zero", "Cl_minus"),
            "differential_state",
            "missing",
            (),
            "not present in the upstream ModifiedADM1 property package",
            "native_WaterTAP_ModifiedADM1_lacks_Fe_S_and_chloride_states",
        ),
        NativeStateRequirement(
            "selected_mineral_inventories",
            ("FeS_amorphous", "Mackinawite", "Vivianite", "Siderite"),
            "differential_state",
            "missing",
            (),
            "no retained Fe-S-P mineral states in the canonical property package",
            "native_WaterTAP_ModifiedADM1_lacks_retained_mineral_states",
        ),
        NativeStateRequirement(
            "oxide_history_hosts",
            ("HFO", "HFO_reoxidized", "HFO_aged"),
            "differential_state",
            "missing",
            (),
            "available only in the reduced post-solve chemistry block",
            "HFO_history_is_not_in_the_native_WaterTAP_state",
        ),
        NativeStateRequirement(
            "history_specific_surface_p",
            (
                "HFO_fresh_surface_P",
                "HFO_reoxidized_surface_P",
                "HFO_aged_surface_P",
            ),
            "differential_state",
            "missing",
            (),
            "available only in the reduced post-solve chemistry block",
            "surface_P_history_is_not_in_the_native_WaterTAP_state",
        ),
        NativeStateRequirement(
            "oxic_history_driver",
            ("O2_aq",),
            "differential_state",
            "missing",
            (),
            "requires an explicit downstream oxidation/aerobic property-package boundary",
            "native_ADM1_train_has_no_coupled_oxic_history_state",
        ),
        NativeStateRequirement(
            "conditional_ligand_inventory",
            ("Ligand_sites",),
            "differential_state",
            "missing",
            (),
            "conditional ligand sites exist only in the reduced chemistry screen",
            "conditional_ligand_inventory_is_not_in_the_native_property_package",
        ),
        NativeStateRequirement(
            "proton_charge_closure",
            ("H_plus",),
            "algebraic_state",
            "partial_native",
            ("ModifiedADM1ReactionBlock.pH",),
            "native reaction pH is solved without the shared Fe-S-P source terms",
            "shared_chemistry_is_not_in_native_charge_and_acid_base_residuals",
        ),
        NativeStateRequirement(
            "solvent_reservoir",
            ("H2O",),
            "implicit_reservoir",
            "implicit_reservoir",
            (),
            "declared implicit solvent reservoir",
        ),
    ),
    shared_material_source_terms_coupled=False,
    shared_charge_acid_base_coupled=False,
    serial_zone_topology_coupled=False,
    initialization_and_scaling_complete=False,
    restart_migration_implemented=False,
)


def watertap_madm1_native_migration_report(
    native_component_ids: Iterable[str] | None = None,
) -> dict[str, object]:
    """Return the target migration contract and optional runtime catalog check."""

    report = WATERTAP_MADM1_FE_S_P_NATIVE_MIGRATION.public_dict()
    if native_component_ids is None:
        report["native_component_catalog_checked"] = False
        report["catalog_missing_declared_states"] = []
        return report

    available = {str(component_id) for component_id in native_component_ids}
    declared = {"S_h2", "S_IP", "S_IC"}
    missing = sorted(declared - available)
    report["native_component_catalog_checked"] = True
    report["catalog_missing_declared_states"] = missing
    if missing:
        report["blockers"] = list(
            dict.fromkeys(
                [*report["blockers"], "declared_native_component_missing_from_runtime_catalog"]
            )
        )
    return report


__all__ = [
    "WATERTAP_MADM1_FE_S_P_NATIVE_MIGRATION",
    "watertap_madm1_native_migration_report",
]
