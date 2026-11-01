"""Gas State Abstraction for the biogas-upgrading membrane path.

Analog of ``core/water_state.py`` for the gas-phase ``natural_gas_PR`` (Peng-Robinson,
Vap-only) property package used by ``Membrane1D``. That package uses an FTPx state
definition, so the state args are total ``flow_mol`` + ``mole_frac_comp`` + ``temperature``
+ ``pressure`` (NOT ``flow_mol_phase_comp``).

Vendor heat-and-mass-balance flows are quoted as Nm3/hr, but the *normal* reference
differs by vendor: Air Products uses 0 C / 1.013 bara; Evonik HMBs use 15 C / 0 barg
(1.013 bara). ``from_normal_flow`` makes that explicit so molar flows are converted on
the correct basis.
"""

from dataclasses import dataclass, field
from typing import Dict, Optional

from .property_registry import PropertyPackageType

R_GAS = 8.314462618  # J/mol/K
ATM_PA = 101325.0


def normal_molar_density(std_T_C: float, std_P_bar_abs: float) -> float:
    """Ideal-gas molar density [mol/m3] at a 'normal' reference condition."""
    return (std_P_bar_abs * 1e5) / (R_GAS * (std_T_C + 273.15))


@dataclass
class GasState:
    """Simplified gas feed-state specification for natural_gas_PR (Vap, PR).

    Attributes:
        flow_mol_s: Total molar flow [mol/s].
        mole_frac: Component mole fractions, e.g. {"CH4": 0.60, "CO2": 0.34, ...}.
            Must sum to ~1.0; trailing components may be dropped if not in the package.
        temperature_C: Temperature [C] (default 25).
        pressure_bar_abs: Absolute pressure [bar] (default 1.013). Use ``from_barg``
            or set explicitly; the model needs absolute pressure.
    """

    flow_mol_s: Optional[float] = None
    mole_frac: Dict[str, float] = field(default_factory=dict)
    temperature_C: float = 25.0
    pressure_bar_abs: float = 1.013

    @classmethod
    def from_normal_flow(
        cls,
        flow_nm3_hr: float,
        mole_frac: Dict[str, float],
        *,
        temperature_C: float = 25.0,
        pressure_barg: float = 0.0,
        std_T_C: float = 0.0,
        std_P_bar_abs: float = 1.013,
    ) -> "GasState":
        """Build a GasState from a vendor Nm3/hr flow at the stated normal basis.

        AP HMBs: std_T_C=0, std_P_bar_abs=1.013. Evonik HMBs: std_T_C=15.
        """
        rho_n = normal_molar_density(std_T_C, std_P_bar_abs)  # mol/m3
        flow_mol_s = flow_nm3_hr * rho_n / 3600.0
        return cls(
            flow_mol_s=flow_mol_s,
            mole_frac=dict(mole_frac),
            temperature_C=temperature_C,
            pressure_bar_abs=pressure_barg + 1.013,
        )

    def to_state_args(self, pkg_type: PropertyPackageType) -> Dict[str, object]:
        """Translate to the property-package state_args dict.

        Only NATURAL_GAS_PR (FTPx) is supported; the gas path does not touch the
        aqueous packages.
        """
        if pkg_type is not PropertyPackageType.NATURAL_GAS_PR:
            raise ValueError(
                f"GasState only supports NATURAL_GAS_PR, got {pkg_type}"
            )
        if self.flow_mol_s is None:
            raise ValueError("GasState.flow_mol_s must be set to build state args")
        total = sum(self.mole_frac.values())
        if total <= 0:
            raise ValueError("GasState.mole_frac must contain positive fractions")
        # Renormalize defensively so the FTPx sum-to-one constraint is satisfied.
        frac = {c: v / total for c, v in self.mole_frac.items()}
        return {
            "flow_mol": self.flow_mol_s,
            "mole_frac_comp": frac,
            "temperature": self.temperature_C + 273.15,
            "pressure": self.pressure_bar_abs * 1e5,
        }
