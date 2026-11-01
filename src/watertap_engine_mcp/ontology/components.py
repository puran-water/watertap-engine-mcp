"""Component metadata contracts used by WaterTAP/QSDsan handoffs.

QSDsan exposes its catalog from a top-level ``core`` package, which collides
with WaterTAP's own top-level ``core`` package inside this workspace. Until the
catalog is moved into a neutral package, this module intentionally mirrors the
public model component lists and the test suite contract-checks them against
the QSDsan source file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ComponentBasis = Literal["mass", "COD", "N", "P", "C", "S"]


@dataclass(frozen=True)
class ComponentSpec:
    """Public metadata and contract basis for one model component."""

    id: str
    name: str
    category: str
    basis: ComponentBasis = "mass"
    description: str | None = None
    typical_domestic_mg_L: float | None = None
    typical_range: tuple[float, float] | None = None
    required: bool = True

    def to_tool_dict(self, *, include_typical_values: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "basis": self.basis,
        }
        if include_typical_values and self.typical_domestic_mg_L is not None:
            payload["typical_domestic_mg_L"] = self.typical_domestic_mg_L
        return payload


MODEL_COMPONENTS: dict[str, tuple[str, ...]] = {
    # Mirrors qsdsan-engine-mcp core/components/catalog.py MODEL_COMPONENTS
    # exactly (order = state-vector positions). Synced 2026-08-25 with the
    # mADM1 micronutrient extension (X_Mackinawite, X_Siderite, S_Co, S_Ni,
    # S_Se) — the mirror had gone stale at 63 components.
    "mADM1": (
        "S_su", "S_aa", "S_fa", "S_va", "S_bu", "S_pro", "S_ac", "S_h2",
        "S_ch4", "S_IC", "S_IN", "S_IP", "S_I", "X_ch", "X_pr", "X_li",
        "X_su", "X_aa", "X_fa", "X_c4", "X_pro", "X_ac", "X_h2", "X_I",
        "X_PHA", "X_PP", "X_PAO", "S_K", "S_Mg", "S_SO4", "S_IS",
        "X_hSRB", "X_aSRB", "X_pSRB", "X_c4SRB", "S_S0", "S_Fe3", "S_Fe2",
        "X_HFO_H", "X_HFO_L", "X_HFO_old", "X_HFO_HP", "X_HFO_LP",
        "X_HFO_HP_old", "X_HFO_LP_old", "S_Ca", "S_Al", "X_CCM", "X_ACC",
        "X_ACP", "X_HAP", "X_DCPD", "X_OCP", "X_struv", "X_newb",
        "X_magn", "X_kstruv", "X_FeS", "X_Mackinawite", "X_Fe3PO42",
        "X_Siderite", "X_AlPO4", "S_Na", "S_Cl", "S_Co", "S_Ni", "S_Se",
        "H2O",
    ),
    "ASM2d": (
        "S_O2", "S_F", "S_A", "S_I", "S_NH4", "S_N2", "S_NO3", "S_PO4",
        "S_ALK", "X_I", "X_S", "X_H", "X_PAO", "X_PP", "X_PHA", "X_AUT",
        "X_MeOH", "X_MeP", "H2O",
    ),
    "ASM1": (
        "S_I", "S_S", "X_I", "X_S", "X_BH", "X_BA", "X_P", "S_O",
        "S_NO", "S_NH", "S_ND", "X_ND", "S_ALK",
    ),
    "mASM2d": (
        "S_O2", "S_N2", "S_NH4", "S_NO3", "S_PO4", "S_F", "S_A", "S_I",
        "S_IC", "S_K", "S_Mg", "X_I", "X_S", "X_H", "X_PAO", "X_PP",
        "X_PHA", "X_AUT", "S_Ca", "S_Na", "S_Cl", "X_CaCO3", "X_struv",
        "X_newb", "X_ACP", "X_MgCO3", "X_AlOH", "X_AlPO4", "X_FeOH",
        "X_FePO4", "H2O",
    ),
}

WATERTAP_ASM2D_COMPONENTS: tuple[str, ...] = (
    "H2O", "S_A", "S_F", "S_I", "S_N2", "S_NH4", "S_NO3", "S_O2", "S_PO4",
    "S_ALK", "X_AUT", "X_H", "X_I", "X_MeOH", "X_MeP", "X_PAO", "X_PHA",
    "X_PP", "X_S", "X_TSS",
)

WATERTAP_ASM2D_PARTICULATE_COMPONENTS: tuple[str, ...] = (
    "X_AUT", "X_H", "X_I", "X_MeOH", "X_MeP", "X_PAO", "X_PHA", "X_PP",
    "X_S", "X_TSS",
)

MODEL_ALIASES: dict[str, str] = {
    "madm1": "mADM1",
    "adm1p": "mADM1",
    "adm1_p_extension": "mADM1",
    "adm1": "ADM1",
    "asm2d": "ASM2d",
    "masm2d": "mASM2d",
    "asm1": "ASM1",
    "watertap_asm2d": "WaterTAP_ASM2D",
}


def normalize_model_name(model_type: str) -> str:
    """Normalize known model aliases used by cross-engine handoffs."""

    token = str(model_type).strip()
    return MODEL_ALIASES.get(token.lower(), token)


def get_component_ids(model_type: str) -> tuple[str, ...]:
    """Return component IDs for a known model contract."""

    normalized = normalize_model_name(model_type)
    if normalized == "WaterTAP_ASM2D":
        return WATERTAP_ASM2D_COMPONENTS
    try:
        return MODEL_COMPONENTS[normalized]
    except KeyError as exc:
        valid = ", ".join(sorted((*MODEL_COMPONENTS, "WaterTAP_ASM2D")))
        raise ValueError(f"Unknown component model {model_type!r}. Valid: {valid}") from exc


def component_set(model_type: str) -> set[str]:
    """Return component IDs as a set for validation and contract tests."""

    return set(get_component_ids(model_type))


__all__ = [
    "ComponentBasis",
    "ComponentSpec",
    "MODEL_ALIASES",
    "MODEL_COMPONENTS",
    "WATERTAP_ASM2D_COMPONENTS",
    "WATERTAP_ASM2D_PARTICULATE_COMPONENTS",
    "component_set",
    "get_component_ids",
    "normalize_model_name",
]
