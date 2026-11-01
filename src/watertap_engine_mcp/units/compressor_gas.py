'Gas feed compressor for the biogas-upgrading train (Vilter/Emerson single-screw).\n\nDEPRECATED for the recycle-loop saturated closure — DO NOT REUSE ``pin_actual_inlet_capacity``.\nIt FREES the suction pressure, which finds the WRONG low-recycle equilibrium (the suction floats\ndown and the compressor starves). The corrected production unit is\n``units.screw_compressor_unit.OilFloodedScrewCompressor`` (registered as ``GasScrewCompressor``):\n``activate_saturated_capacity(free_discharge=True)`` HOLDS the suction at the field setpoint, pins\nmixed flow to the displacement, and frees the DISCHARGE. This isentropic ``PressureChanger`` helper\nis retained only as a simpler design/standalone reference (no oil-cooled discharge T, no\nhold-suction closure); use the screw unit for any recycle / saturated-slide work.\n\nBuilder helpers around IDAES ``PressureChanger`` configured as an isentropic compressor\non the ``natural_gas_PR`` (Peng-Robinson, Vap-only) package — the same package the\n``GasMembraneStage1D`` stages use, so the compressor drops straight into the membrane\nrecycle loop.'

from __future__ import annotations

from typing import Dict

DEFAULT_ISENTROPIC_EFFICIENCY = 0.75


def build_gas_compressor(
    parent_block,
    name: str,
    property_package,
    *,
    isentropic_efficiency: float = DEFAULT_ISENTROPIC_EFFICIENCY,
):
    """Attach an isentropic gas ``PressureChanger`` (compressor) to ``parent_block``.

    Returns the constructed unit. The caller must still close the DOF: fix the inlet
    state (or connect it), then pick ONE discharge spec — ``pin_outlet_pressure`` /
    ``pin_pressure_ratio`` (design mode) or ``pin_actual_inlet_capacity`` (saturated-slide
    mode, frees suction pressure). ``efficiency_isentropic`` is fixed here.
    """
    from idaes.models.unit_models.pressure_changer import (
        PressureChanger,
        ThermodynamicAssumption,
    )

    setattr(
        parent_block,
        name,
        PressureChanger(
            property_package=property_package,
            thermodynamic_assumption=ThermodynamicAssumption.isentropic,
            compressor=True,
        ),
    )
    cmp = getattr(parent_block, name)
    for t in cmp.flowsheet().time:
        cmp.efficiency_isentropic[t].fix(isentropic_efficiency)
    return cmp


def pin_outlet_pressure(cmp, pressure_bara: float) -> None:
    """Design mode: fix the discharge pressure [bar abs]."""
    for t in cmp.flowsheet().time:
        cmp.outlet.pressure[t].fix(pressure_bara * 1e5)


def pin_pressure_ratio(cmp, ratio: float) -> None:
    """Design mode: fix the pressure ratio (discharge/suction)."""
    for t in cmp.flowsheet().time:
        cmp.ratioP[t].fix(ratio)


def pin_actual_inlet_capacity(cmp, q_actual_m3_s: float):
    """Saturated-slide mode: pin the ACTUAL inlet volumetric flow to the compressor
    displacement at 100% slide, and free the suction pressure to close against it.

    Adds ``flow_mol_in == q_actual_m3_s * dens_mol_in`` so the molar suction flow tracks
    the (free) suction density: the machine moves the same swept volume regardless of how
    much gas the membrane recycle delivers, which is the saturated-compressor physics.
    This constraint REPLACES the suction-pressure DOF, so the inlet pressure is unfixed
    here. Returns the created Constraint.

    Usage: fix the inlet (incl. a guessed pressure) + outlet spec, run ``initialize()`` in
    design mode, THEN call this (the standard square-block initializer needs the inlet
    pressure fixed; this switches to saturated mode afterwards).
    """
    from pyomo.environ import Constraint

    for t in cmp.flowsheet().time:
        cmp.inlet.pressure[t].unfix()

    def _rule(b, t):
        return (
            b.control_volume.properties_in[t].flow_mol
            == q_actual_m3_s * b.control_volume.properties_in[t].dens_mol
        )

    con = Constraint(cmp.flowsheet().time, rule=_rule)
    cmp.add_component("actual_inlet_capacity_eqn", con)
    return con


def actual_inlet_volumetric_flow_m3_s(cmp, t=0) -> float:
    """Read the actual inlet volumetric flow [m3/s] (flow_mol / dens_mol at inlet)."""
    from pyomo.environ import value

    p = cmp.control_volume.properties_in[t]
    return value(p.flow_mol) / value(p.dens_mol)


def compressor_report(cmp, t=0) -> Dict[str, float]:
    """Solve-state summary for reporting / twin diagnostics.

    ``discharge_temperature_C`` is the DRY-adiabatic isentropic value — over-predicts the
    real oil-flooded-screw discharge; see the module docstring before using it as a
    diagnostic.
    """
    from pyomo.environ import value

    suction_p = value(cmp.inlet.pressure[t])
    discharge_p = value(cmp.outlet.pressure[t])
    return {
        "suction_pressure_bara": suction_p / 1e5,
        "discharge_pressure_bara": discharge_p / 1e5,
        "pressure_ratio": discharge_p / suction_p,
        "suction_temperature_C": value(cmp.inlet.temperature[t]) - 273.15,
        "discharge_temperature_C_dry_adiabatic": value(cmp.outlet.temperature[t]) - 273.15,
        "work_mechanical_kW": value(cmp.work_mechanical[t]) / 1000.0,
        "work_isentropic_kW": value(cmp.work_isentropic[t]) / 1000.0,
        "actual_inlet_vol_m3_h": actual_inlet_volumetric_flow_m3_s(cmp, t) * 3600.0,
    }
