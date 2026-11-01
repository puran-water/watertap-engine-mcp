"""Volumetric feed conversion using the installed SEAWATER property kernel.

Concentration in kg/m3 is not a mass fraction.  These small property-only
calculations evaluate WaterTAP's own constraints; they do not copy a density
correlation or initialize/solve a flowsheet.  The parameter block is built
through the registry factory, so the enthalpy carries the server's
correction of the WaterTAP 1.7.0 Nayar coefficients (core.watertap_corrections).
"""

from functools import lru_cache

from pyomo.environ import ConcreteModel, value
from pyomo.util.calc_var_value import calculate_variable_from_constraint
from scipy.optimize import brentq

from watertap_engine_mcp.core.property_registry import PropertyPackageType, build_property_package


def _state(temperature_c: float, pressure_pa: float = 101325.0):
    model = ConcreteModel()
    params = build_property_package(model, "params", PropertyPackageType.SEAWATER)
    model.state = params.build_state_block([0], defined_state=True)
    state = model.state[0]
    state.temperature.set_value(temperature_c + 273.15)
    state.pressure.set_value(pressure_pa)
    return model, state


@lru_cache(maxsize=256)
def density_from_concentration(temperature_c: float, tds_kg_m3: float) -> float:
    """Return kg/m3 within the density correlation's 0–150 g/kg domain."""
    if not 0 <= temperature_c <= 180 or tds_kg_m3 < 0:
        raise ValueError("SEAWATER density requires 0–180 C and non-negative TDS")
    model, state = _state(temperature_c)
    density_var = state.dens_mass_phase["Liq"]
    calculate_variable_from_constraint(state.dens_mass_solvent, state.eq_dens_mass_solvent)

    def density_at(salinity):
        state.mass_frac_phase_comp["Liq", "TDS"].set_value(salinity)
        calculate_variable_from_constraint(density_var, state.eq_dens_mass_phase["Liq"])
        return value(density_var)

    if tds_kg_m3 > 0.15 * density_at(0.15):
        raise ValueError("SEAWATER feed exceeds density validity limit of 150 g/kg solution")
    salinity = brentq(lambda s: s * density_at(s) - tds_kg_m3, 0.0, 0.15)
    return density_at(salinity)


def specific_enthalpy(temperature_c: float, mass_fraction: float, pressure_pa: float = 1e5) -> float:
    """Return J/kg, bounded by WaterTAP's 10–120 C, 0–120 g/kg correlation."""
    if not 10 <= temperature_c <= 120 or not 0 <= mass_fraction <= 0.12:
        raise ValueError("SEAWATER enthalpy requires 10–120 C and 0–120 g/kg solution")
    model, state = _state(temperature_c, pressure_pa)
    enthalpy_var = state.enth_mass_phase["Liq"]
    state.mass_frac_phase_comp["Liq", "TDS"].set_value(mass_fraction)
    calculate_variable_from_constraint(enthalpy_var, state.eq_enth_mass_phase["Liq"])
    return value(enthalpy_var)
