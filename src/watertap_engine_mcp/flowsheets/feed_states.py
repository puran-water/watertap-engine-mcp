"""Property-package-specific feed-state mapping.

This module owns the public feed-state contract for flowsheet feeds.  The
actions layer validates the target session/unit and delegates all state-args
construction here so each property package has one explicit mapping.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from watertap_engine_mcp.core.property_registry import PropertyPackageType
from watertap_engine_mcp.core.water_state import WaterTAPState


MASS_PHASE_PACKAGES = {
    PropertyPackageType.SEAWATER,
    PropertyPackageType.NACL,
    PropertyPackageType.NACL_T_DEP,
    PropertyPackageType.NDMA,
    PropertyPackageType.CRYST_NACL,
}


def _temperature_pressure_to_si(
    *,
    temperature_C: Optional[float] = None,
    pressure_bar: Optional[float] = None,
    temperature_K: Optional[float] = None,
    pressure_Pa: Optional[float] = None,
) -> tuple[float, float]:
    """Return temperature in K and pressure in Pa from public units."""

    temp_k = float(temperature_K) if temperature_K is not None else float(temperature_C or 25.0) + 273.15
    pressure = float(pressure_Pa) if pressure_Pa is not None else float(pressure_bar or 1.0) * 1e5
    return temp_k, pressure


def _canonical_mass_flows(
    pkg: PropertyPackageType,
    component_mass_flow_kg_s: Dict[str, float],
) -> Dict[str, float]:
    """Normalize mass-flow component aliases to the target package."""

    flows = dict(component_mass_flow_kg_s)
    if pkg == PropertyPackageType.SEAWATER:
        if "TDS" not in flows and "NaCl" in flows:
            flows["TDS"] = flows.pop("NaCl")
        return flows

    if pkg in {
        PropertyPackageType.NACL,
        PropertyPackageType.NACL_T_DEP,
        PropertyPackageType.CRYST_NACL,
    }:
        if "NaCl" not in flows and "TDS" in flows:
            flows["NaCl"] = flows.pop("TDS")
        return flows

    return flows


def build_exact_feed_state_args(
    *,
    pkg: PropertyPackageType,
    component_mass_flow_kg_s: Optional[Dict[str, float]],
    component_mol_flow_mol_s: Optional[Dict[str, float]],
    temperature_C: Optional[float] = None,
    pressure_bar: Optional[float] = None,
    temperature_K: Optional[float] = None,
    pressure_Pa: Optional[float] = None,
    material_flow_basis: Any = None,
) -> Dict[str, Any]:
    """Build exact state arguments from user-supplied component flows."""

    temp_k, pressure = _temperature_pressure_to_si(
        temperature_C=temperature_C,
        pressure_bar=pressure_bar,
        temperature_K=temperature_K,
        pressure_Pa=pressure_Pa,
    )
    basis_name = getattr(material_flow_basis, "name", material_flow_basis)

    if component_mass_flow_kg_s is not None:
        if not component_mass_flow_kg_s:
            raise ValueError("component_mass_flow_kg_s must not be empty")
        negative = sorted(
            component
            for component, flow in component_mass_flow_kg_s.items()
            if flow < 0
        )
        if negative:
            raise ValueError(
                "component_mass_flow_kg_s must be non-negative for components: "
                + ", ".join(negative)
            )

        if pkg == PropertyPackageType.MCAS and str(basis_name).lower().endswith("mass"):
            return {
                "flow_mass_phase_comp": {
                    ("Liq", component): flow
                    for component, flow in component_mass_flow_kg_s.items()
                },
                "temperature": temp_k,
                "pressure": pressure,
            }

        if pkg in MASS_PHASE_PACKAGES:
            flows = _canonical_mass_flows(pkg, component_mass_flow_kg_s)
            return {
                "flow_mass_phase_comp": {
                    ("Liq", component): flow
                    for component, flow in flows.items()
                },
                "temperature": temp_k,
                "pressure": pressure,
            }

        raise ValueError(
            f"Exact component_mass_flow_kg_s is not supported for property package {pkg.name}"
        )

    if component_mol_flow_mol_s is not None:
        if not component_mol_flow_mol_s:
            raise ValueError("component_mol_flow_mol_s must not be empty")
        negative = sorted(
            component
            for component, flow in component_mol_flow_mol_s.items()
            if flow < 0
        )
        if negative:
            raise ValueError(
                "component_mol_flow_mol_s must be non-negative for components: "
                + ", ".join(negative)
            )
        if pkg == PropertyPackageType.NATURAL_GAS_PR:
            # natural_gas_PR uses an FTPx state: total flow_mol + mole_frac_comp.
            total = sum(component_mol_flow_mol_s.values())
            if total <= 0:
                raise ValueError("component_mol_flow_mol_s must sum to a positive flow")
            return {
                "flow_mol": total,
                "mole_frac_comp": {
                    component: flow / total
                    for component, flow in component_mol_flow_mol_s.items()
                },
                "temperature": temp_k,
                "pressure": pressure,
            }
        if pkg != PropertyPackageType.MCAS:
            raise ValueError(
                f"Exact component_mol_flow_mol_s is only supported for MCAS and "
                f"NATURAL_GAS_PR, not {pkg.name}"
            )
        return {
            "flow_mol_phase_comp": {
                ("Liq", component): flow
                for component, flow in component_mol_flow_mol_s.items()
            },
            "temperature": temp_k,
            "pressure": pressure,
        }

    raise ValueError("Exact feed state requires component_mass_flow_kg_s or component_mol_flow_mol_s")


def build_feed_state_payload(
    *,
    unit_id: str,
    pkg: PropertyPackageType,
    flow_vol_m3_hr: Optional[float] = None,
    tds_mg_L: Optional[float] = None,
    nacl_mg_L: Optional[float] = None,
    components: Optional[Dict[str, float]] = None,
    temperature_C: float = 25.0,
    pressure_bar: float = 1.0,
    concentration_units: str = "mg/L",
    concentration_basis: str = "mass",
    component_charges: Optional[Dict[str, int]] = None,
    electroneutrality_species: Optional[str] = None,
    phase_mass_flow_kg_s: Optional[Dict[str, float]] = None,
    component_mass_flow_kg_s: Optional[Dict[str, float]] = None,
    component_mol_flow_mol_s: Optional[Dict[str, float]] = None,
    property_package_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the persisted feed-state payload for a unit property package."""

    package_config = property_package_config or {}
    comps = dict(components or {})
    if tds_mg_L is not None:
        comps["TDS"] = tds_mg_L
    if nacl_mg_L is not None:
        comps["NaCl"] = nacl_mg_L

    exact_mass_state = dict(component_mass_flow_kg_s or {}) or None
    exact_molar_state = dict(component_mol_flow_mol_s or {}) or None
    if exact_mass_state and exact_molar_state:
        raise ValueError("Specify only one of component_mass_flow_kg_s or component_mol_flow_mol_s")

    if exact_mass_state is None and exact_molar_state is None:
        if pkg == PropertyPackageType.MCAS:
            package_charge_map = package_config.get("charge", {})
            component_charges = component_charges or package_charge_map or None
            if not component_charges and package_config.get("ignore_neutral_charge"):
                component_charges = {}
            if component_charges is None:
                raise ValueError(
                    "MCAS property package requires component_charges dict "
                    "(e.g., {'Na_+': 1, 'Cl_-': -1})"
                )
            if not comps:
                raise ValueError("MCAS property package requires components dict with concentrations")
        elif pkg != PropertyPackageType.WATER and flow_vol_m3_hr is None:
            raise ValueError(f"{pkg.name} feed state requires flow_vol_m3_hr")
        elif pkg == PropertyPackageType.WATER and flow_vol_m3_hr is None and phase_mass_flow_kg_s is None:
            raise ValueError("WATER feed state requires flow_vol_m3_hr or phase_mass_flow_kg_s")

    if exact_mass_state is not None or exact_molar_state is not None:
        state_args = build_exact_feed_state_args(
            pkg=pkg,
            component_mass_flow_kg_s=exact_mass_state,
            component_mol_flow_mol_s=exact_molar_state,
            temperature_C=temperature_C,
            pressure_bar=pressure_bar,
            material_flow_basis=package_config.get("material_flow_basis"),
        )
    else:
        state = WaterTAPState(
            flow_vol_m3_hr=flow_vol_m3_hr,
            temperature_C=temperature_C,
            pressure_bar=pressure_bar,
            phase_mass_flow_kg_s=phase_mass_flow_kg_s,
            components=comps,
            concentration_units=concentration_units,
            concentration_basis=concentration_basis,
            component_charges=component_charges,
            electroneutrality_species=electroneutrality_species,
        )
        material_flow_basis = None
        if pkg == PropertyPackageType.MCAS:
            basis = package_config.get("material_flow_basis")
            material_flow_basis = getattr(basis, "name", basis)
        state_args = state.to_state_args(pkg, material_flow_basis=material_flow_basis)
        electroneutrality_species = state.electroneutrality_species

    return {
        "unit_id": unit_id,
        "property_package": pkg.name,
        "flow_vol_m3_hr": flow_vol_m3_hr,
        "temperature_C": temperature_C,
        "pressure_bar": pressure_bar,
        "phase_mass_flow_kg_s": phase_mass_flow_kg_s,
        "component_mass_flow_kg_s": exact_mass_state,
        "component_mol_flow_mol_s": exact_molar_state,
        "components": comps,
        "concentration_units": concentration_units,
        "concentration_basis": concentration_basis,
        "component_charges": component_charges,
        "electroneutrality_species": electroneutrality_species,
        "state_args": state_args,
    }


def _apply_state_value(target: Any, value: Any) -> bool:
    """Fix a state variable or indexed state variable on a live unit block."""

    if isinstance(value, dict):
        applied = False
        for idx, entry in value.items():
            candidates = [idx]
            if isinstance(idx, tuple):
                candidates.extend([(0, *idx), (0.0, *idx)])
            candidates.extend([(0, idx), (0.0, idx)])
            for candidate_idx in candidates:
                try:
                    if candidate_idx in target and hasattr(target[candidate_idx], "fix"):
                        target[candidate_idx].fix(entry)
                        applied = True
                        break
                except Exception:
                    continue
        return applied

    if hasattr(target, "fix"):
        target.fix(value)
        return True

    return False


def set_feed_state(unit: Any, feed_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a feed-state payload directly to a live Feed-like Pyomo block."""

    state_args = feed_dict.get("state_args") if isinstance(feed_dict, dict) else None
    if not isinstance(state_args, dict):
        raise ValueError("feed_dict must include a state_args dictionary")

    state_block = getattr(unit, "properties", None)
    if state_block is not None:
        state_block = state_block[0]
    port_block = getattr(unit, "outlet", None) or getattr(unit, "inlet", None)

    for key, value in state_args.items():
        candidates = []
        if hasattr(unit, key):
            candidates.append(getattr(unit, key))
        if state_block is not None and hasattr(state_block, key):
            candidates.append(getattr(state_block, key))
        if port_block is not None and hasattr(port_block, key):
            candidates.append(getattr(port_block, key))

        for candidate in candidates:
            if _apply_state_value(candidate, value):
                break

    return state_args
