"""WaterTAP ingress validation for QSDsan downstream handoffs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .property_registry import PropertyPackageType
from .session_presets import get_session_preset


_REQUIRED_TOP_LEVEL_FIELDS = (
    "target_profile",
    "property_package",
    "property_package_config",
    "state_payload",
    "completeness",
    "missing_or_estimated_species",
    "assumptions",
)
_MCAS_STATE_FIELDS = (
    "flow_vol_m3_hr",
    "temperature_C",
    "pressure_bar",
    "components",
    "concentration_units",
    "concentration_basis",
    "component_charges",
)
_MASS_BASIS_STATE_FIELDS = (
    "flow_vol_m3_hr",
    "temperature_C",
    "pressure_bar",
    "components",
    "concentration_units",
    "concentration_basis",
)
_MCAS_BRACKISH_CONFIG_FIELDS = (
    "solute_list",
    "charge",
    "mw_data",
    "stokes_radius_data",
    "diffusivity_data",
    "activity_coefficient_model",
    "density_calculation",
)
_ZERO_ORDER_CONFIG_FIELDS = ("water_source", "solute_list")
_ED_PAIR_CONFIG_FIELDS = ("solute_list", "charge", "mw_data", "elec_mobility_data")


@dataclass(frozen=True)
class DownstreamIngressProfile:
    """Public QSDsan -> WaterTAP ingress contract."""

    name: str
    session_preset: str
    property_package: PropertyPackageType
    description: str
    strict_contract: bool = True
    estimated_by_design: bool = False
    public_supported: bool = True
    public_reason: Optional[str] = None
    required_state_fields: Tuple[str, ...] = _MASS_BASIS_STATE_FIELDS
    required_config_fields: Tuple[str, ...] = ()


INGRESS_PROFILES: Dict[str, DownstreamIngressProfile] = {
    "MCAS_BRACKISH_5ION": DownstreamIngressProfile(
        name="MCAS_BRACKISH_5ION",
        session_preset="MCAS_BRACKISH_5ION",
        property_package=PropertyPackageType.MCAS,
        description="Exact five-ion QSDsan -> WaterTAP MCAS ingress contract.",
        required_state_fields=_MCAS_STATE_FIELDS,
        required_config_fields=_MCAS_BRACKISH_CONFIG_FIELDS,
    ),
    "ZERO_ORDER_WATER": DownstreamIngressProfile(
        name="ZERO_ORDER_WATER",
        session_preset="ZERO_ORDER_WATER",
        property_package=PropertyPackageType.ZERO_ORDER,
        description="Approximate zero-order wastewater ingress contract.",
        strict_contract=False,
        estimated_by_design=True,
        required_state_fields=_MASS_BASIS_STATE_FIELDS,
        required_config_fields=_ZERO_ORDER_CONFIG_FIELDS,
        public_reason=(
            "This ingress is approximate by design because biological plant states are "
            "collapsed to a bulk zero-order wastewater constituent set."
        ),
    ),
    "NACL_BRINE_THERMAL": DownstreamIngressProfile(
        name="NACL_BRINE_THERMAL",
        session_preset="NACL_BRINE_THERMAL",
        property_package=PropertyPackageType.CRYST_NACL,
        description="Approximate NaCl thermal-brine ingress contract.",
        strict_contract=False,
        estimated_by_design=True,
        required_state_fields=_MASS_BASIS_STATE_FIELDS,
        public_reason=(
            "This ingress is approximate by design because upstream chemistry is collapsed "
            "to a NaCl-equivalent thermal-brine proxy."
        ),
    ),
    "MD_THERMAL": DownstreamIngressProfile(
        name="MD_THERMAL",
        session_preset="MD_THERMAL",
        property_package=PropertyPackageType.SEAWATER,
        description="Approximate membrane-distillation thermal ingress contract.",
        strict_contract=False,
        estimated_by_design=True,
        required_state_fields=_MASS_BASIS_STATE_FIELDS,
        public_reason=(
            "This ingress is approximate by design because upstream chemistry is collapsed "
            "to a TDS-equivalent membrane-distillation proxy."
        ),
    ),
    "ED_NACL_PAIR": DownstreamIngressProfile(
        name="ED_NACL_PAIR",
        session_preset="ED_NACL_PAIR",
        property_package=PropertyPackageType.MCAS,
        description=(
            "Approximate sodium/chloride-pair electrodialysis ingress contract for "
            "reduced-chemistry ED workflows."
        ),
        strict_contract=False,
        estimated_by_design=True,
        required_state_fields=_MCAS_STATE_FIELDS,
        required_config_fields=_ED_PAIR_CONFIG_FIELDS,
        public_reason=(
            "This ingress is approximate by design because broader chemistry is reduced "
            "to an Na+/Cl- electrodialysis pair."
        ),
    ),
}


def list_downstream_handoff_profiles(public_only: bool = False) -> List[Dict[str, Any]]:
    """List supported downstream ingress profiles."""

    profiles = []
    for profile in INGRESS_PROFILES.values():
        if public_only and not profile.public_supported:
            continue
        profiles.append(
            {
                "name": profile.name,
                "target_profile": profile.name,
                "session_preset": profile.session_preset,
                "property_package": profile.property_package.name,
                "description": profile.description,
                "public_supported": profile.public_supported,
                "public_reason": profile.public_reason,
                "strict_contract": profile.strict_contract,
                "estimated_by_design": profile.estimated_by_design,
                "ingress_mode": "exact" if profile.strict_contract else "approximate",
            }
        )
    return sorted(profiles, key=lambda item: item["name"])


def _profile(name: str) -> DownstreamIngressProfile:
    key = name.upper()
    if key not in INGRESS_PROFILES:
        valid = ", ".join(sorted(INGRESS_PROFILES))
        raise ValueError(f"Unknown downstream handoff profile '{name}'. Valid profiles: {valid}")
    return INGRESS_PROFILES[key]


def _require_dict(value: Any, name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Downstream handoff {name} must be an object")
    return value


def _require_list(value: Any, name: str) -> List[Any]:
    if not isinstance(value, list):
        raise ValueError(f"Downstream handoff {name} must be a list")
    return value


def _validate_state_payload(
    profile: DownstreamIngressProfile,
    state_payload: Dict[str, Any],
) -> None:
    missing_state_fields = [
        field for field in profile.required_state_fields if field not in state_payload
    ]
    if missing_state_fields:
        raise ValueError(
            f"Downstream handoff state_payload missing required fields: {missing_state_fields}"
        )


def _validate_property_package_config(
    profile: DownstreamIngressProfile,
    payload_config: Dict[str, Any],
) -> None:
    missing_config = [key for key in profile.required_config_fields if key not in payload_config]
    if missing_config:
        raise ValueError(
            f"Downstream handoff property_package_config missing required fields: {missing_config}"
        )

    preset = get_session_preset(profile.session_preset)
    if profile.name == "ZERO_ORDER_WATER":
        solute_list = payload_config.get("solute_list")
        if not isinstance(solute_list, list) or not solute_list:
            raise ValueError(
                "Profile ZERO_ORDER_WATER requires property_package_config.solute_list to be a non-empty list"
            )
        expected = preset.merged_package_config({"solute_list": solute_list})
        if payload_config != expected:
            raise ValueError(
                f"Profile {profile.name} requires property_package_config values that match the "
                f"public {profile.session_preset} ingress contract"
            )
        return

    expected = preset.merged_package_config()
    if any(expected.get(key) != value for key, value in payload_config.items()):
        raise ValueError(
            f"Profile {profile.name} requires property_package_config values that match the "
            f"public {profile.session_preset} ingress contract"
        )


def validate_downstream_handoff_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate one QSDsan downstream handoff payload against the WaterTAP ingress contract."""

    missing_fields = [field for field in _REQUIRED_TOP_LEVEL_FIELDS if field not in payload]
    if missing_fields:
        raise ValueError(f"Downstream handoff payload missing required fields: {missing_fields}")

    profile = _profile(str(payload["target_profile"]))
    if not profile.public_supported:
        raise ValueError(
            f"Profile {profile.name} is not public in this phase. {profile.public_reason}"
        )

    if payload.get("target_engine") not in {None, "watertap"}:
        raise ValueError("Downstream handoff target_engine must be 'watertap'")
    if payload.get("session_preset", profile.session_preset) != profile.session_preset:
        raise ValueError(
            f"Profile {profile.name} requires session_preset={profile.session_preset!r}"
        )
    if str(payload["property_package"]).upper() != profile.property_package.name:
        raise ValueError(
            f"Profile {profile.name} requires property_package={profile.property_package.name!r}"
        )

    strict_contract = payload.get("strict_contract")
    if profile.strict_contract:
        if strict_contract is False:
            raise ValueError("Downstream handoff strict_contract must be true for exact ingress")
    elif strict_contract is not False:
        raise ValueError(
            f"Profile {profile.name} is approximate and must declare strict_contract=false"
        )

    completeness = _require_dict(payload["completeness"], "completeness")
    if completeness.get("machine_ready") is False:
        raise ValueError("Downstream handoff completeness.machine_ready must be true")

    missing_or_estimated_species = _require_list(
        payload["missing_or_estimated_species"], "missing_or_estimated_species"
    )
    if profile.strict_contract and missing_or_estimated_species:
        raise ValueError(
            "Downstream handoff must not contain missing_or_estimated_species for the exact ingress path"
        )

    assumptions = _require_list(payload["assumptions"], "assumptions")
    if not assumptions:
        raise ValueError("Downstream handoff assumptions must be a non-empty list")

    state_payload = _require_dict(payload["state_payload"], "state_payload")
    _validate_state_payload(profile, state_payload)

    payload_config = _require_dict(payload["property_package_config"], "property_package_config")
    _validate_property_package_config(profile, payload_config)

    return {
        "valid": True,
        "profile": {
            "name": profile.name,
            "session_preset": profile.session_preset,
            "property_package": profile.property_package.name,
            "description": profile.description,
            "strict_contract": profile.strict_contract,
            "estimated_by_design": profile.estimated_by_design,
            "ingress_mode": "exact" if profile.strict_contract else "approximate",
        },
        "normalized_payload": {
            **payload,
            "session_preset": profile.session_preset,
            "property_package": profile.property_package.name,
            "target_profile": profile.name,
            "strict_contract": profile.strict_contract,
            "estimated_by_design": profile.estimated_by_design,
        },
    }
