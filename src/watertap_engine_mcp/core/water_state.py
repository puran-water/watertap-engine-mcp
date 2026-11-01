"""Water State Abstraction for WaterTAP.

Provides a simplified interface for specifying feed water conditions
that can be translated to different WaterTAP property package formats.

Supports:
- Mass-based concentrations (mg/L) for desalination packages
- Molar-based concentrations with charges for MCAS
- Volumetric flow with concentrations for ASM/ADM
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .property_registry import PropertyPackageType


# Molecular weights for common ions (g/mol)
MOLECULAR_WEIGHTS = {
    "H2O": 18.015,
    "Na": 22.990,
    "Na_+": 22.990,
    "Cl": 35.453,
    "Cl_-": 35.453,
    "NaCl": 58.443,
    "TDS": 58.443,  # Approximate as NaCl
    "Ca": 40.078,
    "Ca_2+": 40.078,
    "Mg": 24.305,
    "Mg_2+": 24.305,
    "SO4": 96.06,
    "SO4_2-": 96.06,
    "HCO3": 61.017,
    "HCO3_-": 61.017,
    "K": 39.098,
    "K_+": 39.098,
    "NO3": 62.005,
    "NO3_-": 62.005,
}

# Ion charges
ION_CHARGES = {
    "Na_+": 1,
    "K_+": 1,
    "Ca_2+": 2,
    "Mg_2+": 2,
    "Fe_2+": 2,
    "Fe_3+": 3,
    "Mn_2+": 2,
    "Ba_2+": 2,
    "Sr_2+": 2,
    "Cl_-": -1,
    "SO4_2-": -2,
    "HCO3_-": -1,
    "NO3_-": -1,
    "CO3_2-": -2,
}


@dataclass
class WaterTAPState:
    """Simplified feed water state specification.

    This class provides a user-friendly interface for specifying feed water
    conditions. It can translate to the specific state_args format required
    by different WaterTAP property packages.

    Attributes:
    flow_vol_m3_hr: Volumetric flow rate in m³/hr
        temperature_C: Temperature in Celsius (default 25)
        pressure_bar: Pressure in bar (default 1.0)
        components: Dict of component concentrations
        concentration_units: Units for concentrations ("mg/L", "mol/L", "kg/m3")
        concentration_basis: Basis for concentrations ("mass" or "molar")
        component_charges: Ion charges for MCAS (e.g., {"Na_+": 1, "Cl_-": -1})
        electroneutrality_species: Species to adjust for charge balance
        target_property_package: Hint for target package (optional)
    """

    # Basic flow properties
    flow_vol_m3_hr: Optional[float] = None
    temperature_C: float = 25.0
    pressure_bar: float = 1.0
    phase_mass_flow_kg_s: Optional[Dict[str, float]] = None

    # Component specification
    components: Dict[str, float] = field(default_factory=dict)
    concentration_units: str = "mg/L"  # "mg/L", "mol/L", "kg/m3"
    concentration_basis: str = "mass"  # "mass" or "molar"

    # For charged species (MCAS, ASM, ADM)
    component_charges: Optional[Dict[str, int]] = None
    electroneutrality_species: Optional[str] = None

    # Target property package hint
    target_property_package: Optional[str] = None

    def __post_init__(self):
        """Validate inputs after initialization."""
        if self.flow_vol_m3_hr is None and not self.phase_mass_flow_kg_s:
            raise ValueError("Either flow_vol_m3_hr or phase_mass_flow_kg_s must be provided")
        if self.flow_vol_m3_hr is not None and self.flow_vol_m3_hr <= 0:
            raise ValueError("flow_vol_m3_hr must be positive")
        if self.phase_mass_flow_kg_s is not None:
            if not self.phase_mass_flow_kg_s:
                raise ValueError("phase_mass_flow_kg_s must not be empty")
            invalid = sorted(
                phase for phase in self.phase_mass_flow_kg_s
                if phase not in {"Liq", "Vap"}
            )
            if invalid:
                raise ValueError(
                    "phase_mass_flow_kg_s only supports Liq/Vap phases, got: "
                    + ", ".join(invalid)
                )
            negative = sorted(
                phase for phase, flow in self.phase_mass_flow_kg_s.items() if flow < 0
            )
            if negative:
                raise ValueError(
                    "phase_mass_flow_kg_s must be non-negative for phases: "
                    + ", ".join(negative)
                )
        if self.concentration_units not in ("mg/L", "mol/L", "kg/m3"):
            raise ValueError(f"Invalid concentration_units: {self.concentration_units}")
        if self.concentration_basis not in ("mass", "molar"):
            raise ValueError(f"Invalid concentration_basis: {self.concentration_basis}")

    @property
    def flow_vol_m3_s(self) -> float:
        """Flow rate in m³/s."""
        if self.flow_vol_m3_hr is None:
            raise ValueError("flow_vol_m3_hr is required for this property package")
        return self.flow_vol_m3_hr / 3600

    @property
    def temperature_K(self) -> float:
        """Temperature in Kelvin."""
        return self.temperature_C + 273.15

    @property
    def pressure_Pa(self) -> float:
        """Pressure in Pascals."""
        return self.pressure_bar * 1e5

    def get_mass_concentration_kg_m3(self, component: str) -> float:
        """Get mass concentration in kg/m³.

        Args:
            component: Component name

        Returns:
            Concentration in kg/m³
        """
        conc = self.components.get(component, 0.0)

        if self.concentration_units == "mg/L":
            return conc / 1000  # mg/L = g/m³ → kg/m³
        elif self.concentration_units == "kg/m3":
            return conc
        elif self.concentration_units == "mol/L":
            # Convert molar to mass
            mw = MOLECULAR_WEIGHTS.get(component, 100.0)  # Default MW
            return conc * mw / 1000  # mol/L * g/mol → kg/m³

        return conc

    def get_molar_concentration_mol_m3(self, component: str) -> float:
        """Get molar concentration in mol/m³.

        Args:
            component: Component name

        Returns:
            Concentration in mol/m³
        """
        conc = self.components.get(component, 0.0)

        if self.concentration_units == "mol/L":
            return conc * 1000  # mol/L → mol/m³
        elif self.concentration_units == "mg/L":
            mw = MOLECULAR_WEIGHTS.get(component, 100.0)
            return conc / mw  # mg/L / (g/mol) = mmol/L → mol/m³
        elif self.concentration_units == "kg/m3":
            mw = MOLECULAR_WEIGHTS.get(component, 100.0)
            return conc * 1000 / mw  # kg/m³ → mol/m³

        return conc

    def to_state_args(
        self,
        pkg_type: PropertyPackageType,
        *,
        material_flow_basis: Optional[str] = None,
    ) -> Dict:
        """Convert to WaterTAP property package state_args format.

        Args:
            pkg_type: Target property package type
            material_flow_basis: Optional MCAS material-flow basis override

        Returns:
            Dict suitable for state_args parameter

        Raises:
            ValueError: If conversion not supported or data missing
        """
        if pkg_type == PropertyPackageType.SEAWATER:
            return self._to_seawater_state_args()
        elif pkg_type in (PropertyPackageType.NACL, PropertyPackageType.NACL_T_DEP):
            return self._to_nacl_state_args()
        elif pkg_type == PropertyPackageType.NDMA:
            return self._to_ndma_state_args()
        elif pkg_type == PropertyPackageType.CRYST_NACL:
            return self._to_cryst_nacl_state_args()
        elif pkg_type == PropertyPackageType.WATER:
            return self._to_water_state_args()
        elif pkg_type == PropertyPackageType.MCAS:
            if material_flow_basis and str(material_flow_basis).lower().endswith("mass"):
                return self._to_mcas_mass_state_args()
            return self._to_mcas_state_args()
        elif pkg_type == PropertyPackageType.ZERO_ORDER:
            return self._to_zero_order_state_args()
        elif pkg_type in (PropertyPackageType.ASM1, PropertyPackageType.ASM2D,
                          PropertyPackageType.ASM3, PropertyPackageType.MODIFIED_ASM2D):
            return self._to_asm_state_args()
        elif pkg_type in (PropertyPackageType.ADM1, PropertyPackageType.MODIFIED_ADM1,
                          PropertyPackageType.MODIFIED_ADM1_SFE, PropertyPackageType.MODIFIED_ADM1_SFE_ORGANIC_S,
                          PropertyPackageType.MODIFIED_ADM1_SFE_MICROAERATION):
            return self._to_adm_state_args()
        else:
            raise ValueError(f"Unsupported property package: {pkg_type}")

    def _to_seawater_state_args(self) -> Dict:
        """Convert to Seawater property package format."""
        # Get TDS concentration
        tds_conc = self.get_mass_concentration_kg_m3("TDS")
        if tds_conc == 0 and "NaCl" in self.components:
            tds_conc = self.get_mass_concentration_kg_m3("NaCl")

        from watertap_engine_mcp.core.seawater_feed import density_from_concentration

        density = density_from_concentration(self.temperature_C, tds_conc)
        total_mass_flow = self.flow_vol_m3_s * density

        # Calculate mass flows
        tds_mass_flow = self.flow_vol_m3_s * tds_conc
        water_mass_flow = total_mass_flow - tds_mass_flow

        return {
            "flow_mass_phase_comp": {
                ("Liq", "H2O"): water_mass_flow,
                ("Liq", "TDS"): tds_mass_flow,
            },
            "temperature": self.temperature_K,
            "pressure": self.pressure_Pa,
        }

    def _to_nacl_state_args(self) -> Dict:
        """Convert to NaCl property package format."""
        density = 1000
        total_mass_flow = self.flow_vol_m3_s * density

        # Get NaCl concentration
        nacl_conc = self.get_mass_concentration_kg_m3("NaCl")
        if nacl_conc == 0 and "TDS" in self.components:
            nacl_conc = self.get_mass_concentration_kg_m3("TDS")

        nacl_mass_flow = self.flow_vol_m3_s * nacl_conc
        water_mass_flow = total_mass_flow - nacl_mass_flow

        return {
            "flow_mass_phase_comp": {
                ("Liq", "H2O"): water_mass_flow,
                ("Liq", "NaCl"): nacl_mass_flow,
            },
            "temperature": self.temperature_K,
            "pressure": self.pressure_Pa,
        }

    def _to_water_state_args(self) -> Dict:
        """Convert to pure Water property package format."""
        if self.phase_mass_flow_kg_s is not None:
            flow_mass_phase_comp = {}
            if "Liq" in self.phase_mass_flow_kg_s:
                flow_mass_phase_comp[("Liq", "H2O")] = self.phase_mass_flow_kg_s["Liq"]
            if "Vap" in self.phase_mass_flow_kg_s:
                flow_mass_phase_comp[("Vap", "H2O")] = self.phase_mass_flow_kg_s["Vap"]
        else:
            density = 1000
            water_mass_flow = self.flow_vol_m3_s * density
            flow_mass_phase_comp = {
                ("Liq", "H2O"): water_mass_flow,
                ("Vap", "H2O"): 0.0,
            }

        return {
            "flow_mass_phase_comp": flow_mass_phase_comp,
            "temperature": self.temperature_K,
            "pressure": self.pressure_Pa,
        }

    def _to_ndma_state_args(self) -> Dict:
        """Convert to the NDMA trace-organic property package format."""

        density = 1000
        total_mass_flow = self.flow_vol_m3_s * density
        ndma_conc = self.get_mass_concentration_kg_m3("NDMA")
        ndma_mass_flow = self.flow_vol_m3_s * ndma_conc
        water_mass_flow = total_mass_flow - ndma_mass_flow

        return {
            "flow_mass_phase_comp": {
                ("Liq", "H2O"): water_mass_flow,
                ("Liq", "NDMA"): ndma_mass_flow,
            },
            "temperature": self.temperature_K,
            "pressure": self.pressure_Pa,
        }

    def _to_cryst_nacl_state_args(self) -> Dict:
        """Convert to the crystallizer NaCl package format."""

        density = 1000
        total_mass_flow = self.flow_vol_m3_s * density
        nacl_conc = self.get_mass_concentration_kg_m3("NaCl")
        if nacl_conc == 0 and "TDS" in self.components:
            nacl_conc = self.get_mass_concentration_kg_m3("TDS")

        nacl_mass_flow = self.flow_vol_m3_s * nacl_conc
        water_mass_flow = total_mass_flow - nacl_mass_flow
        eps = 1e-6

        return {
            "flow_mass_phase_comp": {
                ("Liq", "H2O"): water_mass_flow,
                ("Liq", "NaCl"): nacl_mass_flow,
                ("Sol", "NaCl"): eps,
                ("Vap", "H2O"): eps,
            },
            "temperature": self.temperature_K,
            "pressure": self.pressure_Pa,
        }

    def _to_mcas_state_args(self) -> Dict:
        """Convert to MCAS (multi-component aqueous solution) format.

        MCAS uses molar basis and requires charge balance.
        """
        if self.component_charges is None:
            raise ValueError(
                "MCAS property package requires component_charges for electroneutrality. "
                "Provide charges like: {'Na_+': 1, 'Cl_-': -1, ...}"
            )

        # Build molar flows
        flow_mol_phase_comp = {}

        # Water flow
        water_mass_flow = self.flow_vol_m3_s * 1000  # kg/s
        water_mol_flow = water_mass_flow / MOLECULAR_WEIGHTS["H2O"] * 1000  # mol/s
        flow_mol_phase_comp[("Liq", "H2O")] = water_mol_flow

        # Ion flows
        for component, conc in self.components.items():
            if component == "H2O":
                continue
            mol_conc = self.get_molar_concentration_mol_m3(component)
            mol_flow = self.flow_vol_m3_s * mol_conc  # mol/s
            flow_mol_phase_comp[("Liq", component)] = mol_flow

        # Check electroneutrality
        total_charge = 0.0
        for comp, flow in flow_mol_phase_comp.items():
            if comp[1] in self.component_charges:
                total_charge += flow * self.component_charges[comp[1]]

        if abs(total_charge) > 1e-6 and self.electroneutrality_species is None:
            self.electroneutrality_species = self._infer_electroneutrality_species(
                total_charge=total_charge,
                flow_mol_phase_comp=flow_mol_phase_comp,
            )

        if abs(total_charge) > 1e-6 and self.electroneutrality_species:
            # Adjust electroneutrality species
            adjust_comp = self.electroneutrality_species
            charge = self.component_charges.get(adjust_comp, 1)
            adjustment = -total_charge / charge
            key = ("Liq", adjust_comp)
            if key in flow_mol_phase_comp:
                flow_mol_phase_comp[key] += adjustment
            else:
                flow_mol_phase_comp[key] = adjustment

        return {
            "flow_mol_phase_comp": flow_mol_phase_comp,
            "temperature": self.temperature_K,
            "pressure": self.pressure_Pa,
        }

    def _infer_electroneutrality_species(
        self,
        *,
        total_charge: float,
        flow_mol_phase_comp: Dict[Tuple[str, str], float],
    ) -> Optional[str]:
        """Choose a deterministic balancing species for MCAS feeds.

        The public process surface should not require clients to choose the
        bookkeeping ion used to enforce electroneutrality. Prefer chloride for
        net positive feeds and sodium for net negative feeds when available,
        then fall back to the largest-opposing-charge species present.
        """

        if not self.component_charges:
            return None

        needed_sign = -1 if total_charge > 0 else 1
        candidates = []
        for component, charge in self.component_charges.items():
            if charge == 0 or charge * needed_sign <= 0:
                continue
            key = ("Liq", component)
            if key not in flow_mol_phase_comp:
                continue
            candidates.append((component, abs(flow_mol_phase_comp[key]), abs(charge)))

        if not candidates:
            return None

        preferred = "Cl_-" if needed_sign < 0 else "Na_+"
        for component, _, _ in candidates:
            if component == preferred:
                return component

        candidates.sort(key=lambda item: (item[1], item[2]), reverse=True)
        return candidates[0][0]

    def _to_mcas_mass_state_args(self) -> Dict:
        """Convert to MCAS mass-basis state args."""

        density = 1000
        total_mass_flow = self.flow_vol_m3_s * density
        flow_mass_phase_comp = {("Liq", "H2O"): total_mass_flow}

        for component in self.components:
            if component == "H2O":
                continue
            mass_conc = self.get_mass_concentration_kg_m3(component)
            mass_flow = self.flow_vol_m3_s * mass_conc
            flow_mass_phase_comp[("Liq", component)] = mass_flow
            flow_mass_phase_comp[("Liq", "H2O")] -= mass_flow

        return {
            "flow_mass_phase_comp": flow_mass_phase_comp,
            "temperature": self.temperature_K,
            "pressure": self.pressure_Pa,
        }

    def _to_zero_order_state_args(self) -> Dict:
        """Convert to zero-order property package format."""
        conc_mass_comp = {}
        for component, conc in self.components.items():
            if component != "H2O":
                conc_mass_comp[component] = self.get_mass_concentration_kg_m3(component)

        return {
            "flow_vol": self.flow_vol_m3_s,
            "conc_mass_comp": conc_mass_comp,
            "temperature": self.temperature_K,
            "pressure": self.pressure_Pa,
        }

    def _to_asm_state_args(self) -> Dict:
        """Convert to ASM (Activated Sludge Model) format."""
        conc_mass_comp = {}
        for component, conc in self.components.items():
            if component != "H2O":
                conc_mass_comp[component] = self.get_mass_concentration_kg_m3(component)

        return {
            "flow_vol": self.flow_vol_m3_s,
            "conc_mass_comp": conc_mass_comp,
            "temperature": self.temperature_K,
            "pressure": self.pressure_Pa,
        }

    def _to_adm_state_args(self) -> Dict:
        """Convert to ADM (Anaerobic Digestion Model) format."""
        return self._to_asm_state_args()  # Same format

    @classmethod
    def from_tds(
        cls,
        flow_vol_m3_hr: float,
        tds_mg_L: float,
        temperature_C: float = 25.0,
        pressure_bar: float = 1.0,
    ) -> "WaterTAPState":
        """Create state from simple TDS specification.

        Args:
            flow_vol_m3_hr: Flow rate in m³/hr
            tds_mg_L: Total dissolved solids in mg/L
            temperature_C: Temperature in Celsius
            pressure_bar: Pressure in bar

        Returns:
            WaterTAPState instance
        """
        return cls(
            flow_vol_m3_hr=flow_vol_m3_hr,
            temperature_C=temperature_C,
            pressure_bar=pressure_bar,
            components={"TDS": tds_mg_L},
            concentration_units="mg/L",
            concentration_basis="mass",
            target_property_package="SEAWATER",
        )

    @classmethod
    def from_nacl(
        cls,
        flow_vol_m3_hr: float,
        nacl_mg_L: float,
        temperature_C: float = 25.0,
        pressure_bar: float = 1.0,
    ) -> "WaterTAPState":
        """Create state from NaCl concentration.

        Args:
            flow_vol_m3_hr: Flow rate in m³/hr
            nacl_mg_L: NaCl concentration in mg/L
            temperature_C: Temperature in Celsius
            pressure_bar: Pressure in bar

        Returns:
            WaterTAPState instance
        """
        return cls(
            flow_vol_m3_hr=flow_vol_m3_hr,
            temperature_C=temperature_C,
            pressure_bar=pressure_bar,
            components={"NaCl": nacl_mg_L},
            concentration_units="mg/L",
            concentration_basis="mass",
            target_property_package="NACL",
        )

    @classmethod
    def seawater_standard(cls, flow_vol_m3_hr: float) -> "WaterTAPState":
        """Create standard seawater composition.

        Args:
            flow_vol_m3_hr: Flow rate in m³/hr

        Returns:
            WaterTAPState with typical seawater composition (35,000 mg/L TDS)
        """
        return cls.from_tds(
            flow_vol_m3_hr=flow_vol_m3_hr,
            tds_mg_L=35000,
            temperature_C=25.0,
            pressure_bar=1.0,
        )

    @classmethod
    def brackish_water(cls, flow_vol_m3_hr: float) -> "WaterTAPState":
        """Create brackish water composition.

        Args:
            flow_vol_m3_hr: Flow rate in m³/hr

        Returns:
            WaterTAPState with typical brackish water (5,000 mg/L TDS)
        """
        return cls.from_tds(
            flow_vol_m3_hr=flow_vol_m3_hr,
            tds_mg_L=5000,
            temperature_C=25.0,
            pressure_bar=1.0,
        )
