"""State translation utilities for converting between property package formats."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.property_registry import PropertyPackageType


# Molecular weights for common components (g/mol)
MOLECULAR_WEIGHTS = {
    "H2O": 18.015,
    "NaCl": 58.44,
    "Na": 22.99,
    "Na_+": 22.99,
    "Cl": 35.45,
    "Cl_-": 35.45,
    "TDS": 58.44,  # Approximate as NaCl
    "Ca": 40.08,
    "Ca_2+": 40.08,
    "Mg": 24.31,
    "Mg_2+": 24.31,
    "SO4": 96.06,
    "SO4_2-": 96.06,
    "HCO3": 61.02,
    "HCO3_-": 61.02,
    "K": 39.10,
    "K_+": 39.10,
}


@dataclass
class StateArgs:
    """State arguments for property package initialization."""
    flow_mass_phase_comp: Optional[Dict[Tuple[str, str], float]] = None
    flow_mol_phase_comp: Optional[Dict[Tuple[str, str], float]] = None
    flow_vol: Optional[float] = None
    temperature: float = 298.15  # K
    pressure: float = 101325.0  # Pa


class StateTranslator:
    """Translates simplified water state to property package state_args."""

    def __init__(self):
        """Initialize state translator."""
        pass

    def to_seawater_state(
        self,
        flow_vol_m3_s: float,
        tds_kg_m3: float,
        temperature_K: float = 298.15,
        pressure_Pa: float = 101325.0,
    ) -> Dict[str, Any]:
        """Convert to SEAWATER property package state_args.

        SEAWATER uses mass basis with H2O and TDS components.

        Args:
            flow_vol_m3_s: Volumetric flow rate (m3/s)
            tds_kg_m3: TDS concentration (kg/m3)
            temperature_K: Temperature in Kelvin
            pressure_Pa: Pressure in Pascals

        Returns:
            state_args dict for SEAWATER package
        """
        # Estimate density (simplified)
        density = 1000 + 0.7 * tds_kg_m3  # kg/m3

        mass_flow = flow_vol_m3_s * density  # kg/s
        tds_mass_flow = flow_vol_m3_s * tds_kg_m3  # kg/s
        water_mass_flow = mass_flow - tds_mass_flow

        return {
            "flow_mass_phase_comp": {
                ("Liq", "H2O"): water_mass_flow,
                ("Liq", "TDS"): tds_mass_flow,
            },
            "temperature": temperature_K,
            "pressure": pressure_Pa,
        }

    def to_nacl_state(
        self,
        flow_vol_m3_s: float,
        nacl_kg_m3: float,
        temperature_K: float = 298.15,
        pressure_Pa: float = 101325.0,
    ) -> Dict[str, Any]:
        """Convert to NaCl property package state_args.

        NaCl uses mass basis with H2O and NaCl components.

        Args:
            flow_vol_m3_s: Volumetric flow rate (m3/s)
            nacl_kg_m3: NaCl concentration (kg/m3)
            temperature_K: Temperature in Kelvin
            pressure_Pa: Pressure in Pascals

        Returns:
            state_args dict for NaCl package
        """
        density = 1000 + 0.7 * nacl_kg_m3
        mass_flow = flow_vol_m3_s * density
        nacl_mass_flow = flow_vol_m3_s * nacl_kg_m3
        water_mass_flow = mass_flow - nacl_mass_flow

        return {
            "flow_mass_phase_comp": {
                ("Liq", "H2O"): water_mass_flow,
                ("Liq", "NaCl"): nacl_mass_flow,
            },
            "temperature": temperature_K,
            "pressure": pressure_Pa,
        }

    def to_mcas_state(
        self,
        flow_vol_m3_s: float,
        components_mol_m3: Dict[str, float],
        temperature_K: float = 298.15,
        pressure_Pa: float = 101325.0,
    ) -> Dict[str, Any]:
        """Convert to MCAS property package state_args.

        MCAS uses molar basis with multiple ion components.

        Args:
            flow_vol_m3_s: Volumetric flow rate (m3/s)
            components_mol_m3: Dict of component -> molar concentration (mol/m3)
            temperature_K: Temperature in Kelvin
            pressure_Pa: Pressure in Pascals

        Returns:
            state_args dict for MCAS package
        """
        flow_mol_phase_comp = {}

        # Water flow (assuming ~55.5 mol/L = 55500 mol/m3)
        water_mol_m3 = 55500
        flow_mol_phase_comp[("Liq", "H2O")] = flow_vol_m3_s * water_mol_m3

        # Ion flows
        for comp, mol_m3 in components_mol_m3.items():
            flow_mol_phase_comp[("Liq", comp)] = flow_vol_m3_s * mol_m3

        return {
            "flow_mol_phase_comp": flow_mol_phase_comp,
            "temperature": temperature_K,
            "pressure": pressure_Pa,
        }

    def to_zero_order_state(
        self,
        flow_vol_m3_s: float,
        temperature_K: float = 298.15,
        pressure_Pa: float = 101325.0,
    ) -> Dict[str, Any]:
        """Convert to Zero-Order property package state_args.

        Zero-order uses volumetric flow basis.

        Args:
            flow_vol_m3_s: Volumetric flow rate (m3/s)
            temperature_K: Temperature in Kelvin
            pressure_Pa: Pressure in Pascals

        Returns:
            state_args dict for Zero-Order package
        """
        return {
            "flow_vol": flow_vol_m3_s,
            "temperature": temperature_K,
            "pressure": pressure_Pa,
        }

    def convert_mass_to_molar(
        self,
        mass_flow_kg_s: float,
        component: str,
    ) -> float:
        """Convert mass flow to molar flow.

        Args:
            mass_flow_kg_s: Mass flow rate (kg/s)
            component: Component name

        Returns:
            Molar flow rate (mol/s)
        """
        mw = MOLECULAR_WEIGHTS.get(component, 100.0)  # Default MW
        return mass_flow_kg_s * 1000 / mw  # kg/s -> g/s -> mol/s

    def convert_molar_to_mass(
        self,
        mol_flow_mol_s: float,
        component: str,
    ) -> float:
        """Convert molar flow to mass flow.

        Args:
            mol_flow_mol_s: Molar flow rate (mol/s)
            component: Component name

        Returns:
            Mass flow rate (kg/s)
        """
        mw = MOLECULAR_WEIGHTS.get(component, 100.0)
        return mol_flow_mol_s * mw / 1000  # mol/s -> g/s -> kg/s

    def translate_state(
        self,
        source_pkg: PropertyPackageType,
        dest_pkg: PropertyPackageType,
        state_args: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Translate state_args between property packages.

        Note: This is limited to packages with the same basis.
        Full translation requires ASM/ADM translator blocks.

        Args:
            source_pkg: Source property package type
            dest_pkg: Destination property package type
            state_args: Source state_args dict

        Returns:
            Translated state_args for destination package
        """
        if source_pkg == dest_pkg:
            return state_args.copy()

        # Same-basis translations
        mass_packages = {
            PropertyPackageType.SEAWATER,
            PropertyPackageType.NACL,
            PropertyPackageType.NACL_T_DEP,
            PropertyPackageType.WATER,
        }

        if source_pkg in mass_packages and dest_pkg in mass_packages:
            # Both mass basis - can translate with component mapping
            return self._translate_mass_basis(source_pkg, dest_pkg, state_args)

        # Different basis requires translator blocks (ASM/ADM)
        raise ValueError(
            f"Cannot directly translate from {source_pkg.name} to {dest_pkg.name}. "
            "Use a translator block for cross-basis translations."
        )

    def _translate_mass_basis(
        self,
        source_pkg: PropertyPackageType,
        dest_pkg: PropertyPackageType,
        state_args: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Translate between mass-basis packages."""
        result = {
            "temperature": state_args.get("temperature", 298.15),
            "pressure": state_args.get("pressure", 101325.0),
        }

        flow_mass = state_args.get("flow_mass_phase_comp", {})

        # Get total mass flow
        total_mass = sum(flow_mass.values())
        water_mass = flow_mass.get(("Liq", "H2O"), 0)
        solute_mass = total_mass - water_mass

        # Map to destination components
        if dest_pkg == PropertyPackageType.SEAWATER:
            result["flow_mass_phase_comp"] = {
                ("Liq", "H2O"): water_mass,
                ("Liq", "TDS"): solute_mass,
            }
        elif dest_pkg in (PropertyPackageType.NACL, PropertyPackageType.NACL_T_DEP):
            result["flow_mass_phase_comp"] = {
                ("Liq", "H2O"): water_mass,
                ("Liq", "NaCl"): solute_mass,
            }
        elif dest_pkg == PropertyPackageType.WATER:
            # Pure water - ignore solutes
            result["flow_mass_phase_comp"] = {
                ("Liq", "H2O"): water_mass,
            }

        return result


def create_state_args(
    property_package: PropertyPackageType,
    flow_vol_m3_hr: float,
    temperature_C: float = 25.0,
    pressure_bar: float = 1.0,
    tds_mg_L: Optional[float] = None,
    nacl_mg_L: Optional[float] = None,
    components_mg_L: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Create state_args from simplified inputs.

    Args:
        property_package: Target property package type
        flow_vol_m3_hr: Volumetric flow (m3/hr)
        temperature_C: Temperature (Celsius)
        pressure_bar: Pressure (bar)
        tds_mg_L: TDS concentration for SEAWATER (mg/L)
        nacl_mg_L: NaCl concentration for NACL packages (mg/L)
        components_mg_L: Component concentrations for MCAS (mg/L)

    Returns:
        state_args dict for the property package
    """
    translator = StateTranslator()

    # Convert units
    flow_m3_s = flow_vol_m3_hr / 3600
    temp_K = temperature_C + 273.15
    pressure_Pa = pressure_bar * 1e5

    if property_package == PropertyPackageType.SEAWATER:
        tds_kg_m3 = (tds_mg_L or 35000) / 1000  # mg/L -> kg/m3
        return translator.to_seawater_state(flow_m3_s, tds_kg_m3, temp_K, pressure_Pa)

    elif property_package in (PropertyPackageType.NACL, PropertyPackageType.NACL_T_DEP):
        nacl_kg_m3 = (nacl_mg_L or tds_mg_L or 35000) / 1000
        return translator.to_nacl_state(flow_m3_s, nacl_kg_m3, temp_K, pressure_Pa)

    elif property_package == PropertyPackageType.MCAS:
        if components_mg_L is None:
            raise ValueError("MCAS requires components_mg_L dict")
        # Convert mg/L to mol/m3
        components_mol_m3 = {}
        for comp, mg_L in components_mg_L.items():
            mw = MOLECULAR_WEIGHTS.get(comp, 100.0)
            components_mol_m3[comp] = mg_L / mw  # mg/L / (g/mol) = mmol/L = mol/m3
        return translator.to_mcas_state(flow_m3_s, components_mol_m3, temp_K, pressure_Pa)

    elif property_package == PropertyPackageType.ZERO_ORDER:
        return translator.to_zero_order_state(flow_m3_s, temp_K, pressure_Pa)

    else:
        # Default to volumetric flow for biological packages
        return {
            "flow_vol": flow_m3_s,
            "temperature": temp_K,
            "pressure": pressure_Pa,
        }
