"""Model-aware biological-effluent measurement aggregation.

WaterTAP's ASM2d and modified-ADM1 property packages do not share a native
component basis.  The maps below are the installed package's own ``Pi`` / ``Ni``
values, never a generic sum: until WaterTAP 1.8.0 modified ADM1 ``X_PP`` carried
``1 kmol P / kg native state`` (31 kg P per native kg) while ASM2d ``X_PP`` was on
a kg-P basis, and a single sum gave a plausible-looking but materially wrong TP.
WaterTAP 1.8.0 puts modified ADM1 ``X_PP`` on kg P as well (``Pi["X_PP"] = 1/31``).

This module keeps the native-basis maps explicit and returns measurements for
an already selected stream/port.  Membrane or clarifier transmission must have
been applied by the flowsheet before this function is called.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from puran_mcp_support.engineering.heuristics.asm2d_influent_fractionation import asm2d_cod_kg_m3, asm2d_effluent_tn_kg_m3
from puran_mcp_support.schema import MeasurementMapEntry, PhosphorusMeasurementAggregator, PortSeparationProfile

_KG_P_PER_KMOL_P = 31.0
_KG_N_PER_KMOL_N = 14.0

# WaterTAP modified_adm1_reactions.py ``Pi`` values, as installed (1.8.0; a test
# re-reads the installed parameter block).  The explicit multiplication by the
# package's 31 kg-P/kmol-P constant converts each native COD-basis component to
# kg P per kg native component.  S_IP is already kg P.  ``X_PP`` is 1/31 kmol P
# per kg since 1.8.0 (line 198), i.e. 1 kg P per kg of state; it was 1 in 1.7.0.
WATERTAP_MODIFIED_ADM1_P_COEFFICIENTS: dict[str, float] = {
    "S_IP": 1.0,
    "S_I": 0.0002093322 * _KG_P_PER_KMOL_P,
    "X_li": 0.0003440808 * _KG_P_PER_KMOL_P,
    "X_su": 0.0006947201 * _KG_P_PER_KMOL_P,
    "X_aa": 0.0006947201 * _KG_P_PER_KMOL_P,
    "X_fa": 0.0006947201 * _KG_P_PER_KMOL_P,
    "X_c4": 0.0006947201 * _KG_P_PER_KMOL_P,
    "X_pro": 0.0006947201 * _KG_P_PER_KMOL_P,
    "X_ac": 0.0006947201 * _KG_P_PER_KMOL_P,
    "X_h2": 0.0006947201 * _KG_P_PER_KMOL_P,
    "X_I": 0.0002093322 * _KG_P_PER_KMOL_P,
    "X_PP": (1.0 / 31.0) * _KG_P_PER_KMOL_P,
    "X_PAO": 0.0006947201 * _KG_P_PER_KMOL_P,
}

# WaterTAP 1.6 modified-ADM1 ``Ni`` values, converted from kmol N/kg native
# component to kg N/kg native component.  S_IN is already kg N.
WATERTAP_MODIFIED_ADM1_N_COEFFICIENTS: dict[str, float] = {
    "S_IN": 1.0,
    "S_aa": 0.0079034 * _KG_N_PER_KMOL_N,
    "S_I": 0.0042876 * _KG_N_PER_KMOL_N,
    "X_pr": 0.0079034 * _KG_N_PER_KMOL_N,
    "X_su": 0.0061532 * _KG_N_PER_KMOL_N,
    "X_aa": 0.0061532 * _KG_N_PER_KMOL_N,
    "X_fa": 0.0061532 * _KG_N_PER_KMOL_N,
    "X_c4": 0.0061532 * _KG_N_PER_KMOL_N,
    "X_pro": 0.0061532 * _KG_N_PER_KMOL_N,
    "X_ac": 0.0061532 * _KG_N_PER_KMOL_N,
    "X_h2": 0.0061532 * _KG_N_PER_KMOL_N,
    "X_I": 0.0042876 * _KG_N_PER_KMOL_N,
    "X_PAO": 0.0061532 * _KG_N_PER_KMOL_N,
}

# Modified ADM1 property-package components carried on kg-COD/m3.  Elemental
# inorganics, X_PP, K and Mg are intentionally excluded.  Dissolved H2/CH4 are
# included because this is the modeled COD inventory basis, not an assertion
# about an open-vial laboratory COD preparation.
WATERTAP_MODIFIED_ADM1_COD_COMPONENTS = (
    "S_su",
    "S_aa",
    "S_fa",
    "S_va",
    "S_bu",
    "S_pro",
    "S_ac",
    "S_h2",
    "S_ch4",
    "S_I",
    "X_ch",
    "X_pr",
    "X_li",
    "X_su",
    "X_aa",
    "X_fa",
    "X_c4",
    "X_pro",
    "X_ac",
    "X_h2",
    "X_I",
    "X_PHA",
    "X_PAO",
)

_ASM2D_P_ENTRIES = (
    MeasurementMapEntry("S_PO4", "orthophosphate", 1.0),
    MeasurementMapEntry("X_PP", "polyphosphate", 1.0),
    MeasurementMapEntry("X_H", "biomass_p", 0.02),
    MeasurementMapEntry("X_PAO", "biomass_p", 0.02),
    MeasurementMapEntry("X_AUT", "biomass_p", 0.02),
    MeasurementMapEntry("X_I", "particulate_organic_p", 0.01),
    MeasurementMapEntry("X_S", "particulate_organic_p", 0.01),
    MeasurementMapEntry("X_MeP", "mineral_p", 1.0 / 4.87),
)


def _modified_adm1_p_pool(component_id: str) -> str:
    if component_id == "S_IP":
        return "orthophosphate"
    if component_id == "S_I":
        return "soluble_organic_or_unreactive_p"
    if component_id == "X_PP":
        return "polyphosphate"
    if component_id in {"X_su", "X_aa", "X_fa", "X_c4", "X_pro", "X_ac", "X_h2", "X_PAO"}:
        return "biomass_p"
    return "particulate_organic_p"


_MODIFIED_ADM1_P_ENTRIES = tuple(
    MeasurementMapEntry(component_id, _modified_adm1_p_pool(component_id), coefficient)
    for component_id, coefficient in WATERTAP_MODIFIED_ADM1_P_COEFFICIENTS.items()
)


def infer_biological_state_family(components_kg_m3: Mapping[str, float]) -> str | None:
    """Infer the native biological state basis from discriminating components."""

    component_ids = set(components_kg_m3)
    if "S_IP" in component_ids and {"S_IC", "S_IN"} <= component_ids:
        return "watertap_modified_adm1_v1_6"
    if "S_PO4" in component_ids and {"S_NH4", "S_NO3"} <= component_ids:
        return "watertap_asm2d_family"
    return None


def _finite_state(components_kg_m3: Mapping[str, float]) -> dict[str, float]:
    state = {str(component): float(value) for component, value in components_kg_m3.items()}
    invalid = sorted(component for component, value in state.items() if not math.isfinite(value))
    if invalid:
        raise ValueError(f"effluent state contains nonfinite components: {invalid}")
    materially_negative = sorted(component for component, value in state.items() if value < -1e-12)
    if materially_negative:
        raise ValueError(
            "effluent state contains materially negative components: "
            f"{materially_negative}"
        )
    return {component: max(value, 0.0) for component, value in state.items()}


def _aggregate_phosphorus(
    state: Mapping[str, float],
    *,
    entries: tuple[MeasurementMapEntry, ...],
    port_id: str,
) -> dict[str, float]:
    present_entries = tuple(entry for entry in entries if entry.component_id in state)
    transmission = {entry.component_id: 1.0 for entry in present_entries}
    return PhosphorusMeasurementAggregator(present_entries).aggregate(
        state,
        separation_profile=PortSeparationProfile(port_id, transmission),
    )


def aggregate_biological_effluent_metrics(
    components_kg_m3: Mapping[str, float],
    *,
    port_id: str,
) -> dict[str, Any]:
    """Return model-aware aggregate measurements for an already separated port.

    Concentrations enter on the native WaterTAP kg/m3 basis and aggregate
    values leave on kg/m3.  Callers may convert the latter to mg/L by
    multiplying by 1000.  An unsupported state returns an explicit status and
    never manufactures a TP value.
    """

    state = _finite_state(components_kg_m3)
    family = infer_biological_state_family(state)
    if family is None:
        return {
            "status": "unsupported_state_basis",
            "model_family": None,
            "port_id": port_id,
            "recommendation_eligible": False,
            "reason": "no registered biological native-basis measurement map",
        }

    if family == "watertap_asm2d_family":
        p_pools = _aggregate_phosphorus(state, entries=_ASM2D_P_ENTRIES, port_id=port_id)
        total_n = asm2d_effluent_tn_kg_m3(state)
        metrics = {
            "COD": asm2d_cod_kg_m3(state),
            "BOD5": (
                0.60 * state.get("S_F", 0.0)
                + 0.40 * state.get("S_A", 0.0)
                + 0.25 * state.get("X_S", 0.0)
            ),
            "TN": total_n,
            "TKN": total_n - state.get("S_NO3", 0.0),
            "TP": p_pools["total_p"],
            "orthophosphate_P": p_pools["orthophosphate"],
            "dissolved_P": p_pools["orthophosphate"],
        }
        phosphorus_basis = (
            "ASM2d native component-P map; terminal stream already reflects "
            "the configured separator"
        )
    else:
        p_pools = _aggregate_phosphorus(
            state,
            entries=_MODIFIED_ADM1_P_ENTRIES,
            port_id=port_id,
        )
        total_n = sum(
            state.get(component_id, 0.0) * coefficient
            for component_id, coefficient in WATERTAP_MODIFIED_ADM1_N_COEFFICIENTS.items()
        )
        dissolved_p = p_pools.get("orthophosphate", 0.0) + p_pools.get(
            "soluble_organic_or_unreactive_p", 0.0
        )
        metrics = {
            "COD": sum(
                state.get(component_id, 0.0)
                for component_id in WATERTAP_MODIFIED_ADM1_COD_COMPONENTS
            ),
            "TN": total_n,
            "TKN": total_n,
            "TP": p_pools["total_p"],
            "orthophosphate_P": p_pools["orthophosphate"],
            "dissolved_P": dissolved_p,
            "modeled_soluble_inorganic_P": state.get("S_IP", 0.0),
        }
        phosphorus_basis = (
            "WaterTAP 1.6 modified-ADM1 Pi map (31 kg P/kmol P); terminal "
            "stream already reflects the configured separator"
        )

    return {
        "status": "complete",
        "model_family": family,
        "port_id": port_id,
        "recommendation_eligible": False,
        "recommendation_blockers": [
            "aggregate_reporting_does_not_establish_a_calibrated_dose_model"
        ],
        "metrics_kg_m3": metrics,
        "phosphorus_pools_kg_m3": p_pools,
        "phosphorus_basis": phosphorus_basis,
    }


__all__ = [
    "WATERTAP_MODIFIED_ADM1_COD_COMPONENTS",
    "WATERTAP_MODIFIED_ADM1_N_COEFFICIENTS",
    "WATERTAP_MODIFIED_ADM1_P_COEFFICIENTS",
    "aggregate_biological_effluent_metrics",
    "infer_biological_state_family",
]
