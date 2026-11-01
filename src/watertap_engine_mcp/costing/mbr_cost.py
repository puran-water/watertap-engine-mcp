"""Costing for the MCP ASM2D algebraic MBR separator surrogate."""

from __future__ import annotations

import pyomo.environ as pyo
from watertap.costing.util import make_capital_cost_var


def _state_flow_vol(unit_model):
    """Return the permeate volumetric flow reference for an IDAES Separator."""

    for attr_name in ("properties_permeate", "mixed_permeate"):
        state = getattr(unit_model, attr_name, None)
        if state is None:
            continue
        for index in (0, 0.0):
            try:
                return state[index].flow_vol
            except Exception:
                continue
        flow = getattr(state, "flow_vol", None)
        if flow is not None:
            return flow
    port = getattr(unit_model, "permeate", None)
    flow = getattr(port, "flow_vol", None) if port is not None else None
    if flow is not None:
        for index in (0, 0.0):
            try:
                return flow[index]
            except Exception:
                continue
    raise AttributeError("MBRSeparator has no permeate flow_vol reference")


def cost_mbr_separator(blk, cost_electricity_flow=True):
    """Cost an algebraic MBR separator from permeate flow.

    The ASM2D MCP template represents the MBR as an ideal component separator,
    so it has no upstream membrane-area variable. This costing method derives
    membrane area from permeate flow using a conservative hollow-fiber MF/UF
    design flux of 15 LMH (0.36 m3/m2/d) and applies an installed membrane
    capital cost of 100 USD_2018/m2. The installed-cost range for municipal and
    industrial hollow-fiber MBR membrane systems is commonly reported around
    50-150 USD/m2; the midpoint is used here for proposal optimization. Scour
    aeration OPEX is represented as 0.20 kWh/m3 of permeate, consistent with
    literature MBR air-scour energy ranges for immersed hollow-fiber systems.
    Citations: Judd, The MBR Book, 2e, Ch. 5-6; WEF MOP 8, membrane bioreactor
    design guidance; Metcalf & Eddy, Wastewater Engineering, 5e, MBR sections.
    """

    make_capital_cost_var(blk)
    blk.costing_package.add_cost_factor(blk, "TIC")

    permeate_flow = _state_flow_vol(blk.unit_model)
    design_flux = 0.36 * pyo.units.m**3 / pyo.units.m**2 / pyo.units.day
    installed_membrane_cost = 100.0 * blk.costing_package.base_currency / pyo.units.m**2

    blk.membrane_area = pyo.Expression(
        expr=pyo.units.convert(permeate_flow, to_units=pyo.units.m**3 / pyo.units.day)
        / design_flux
    )
    blk.capital_cost_constraint = pyo.Constraint(
        expr=blk.capital_cost
        == blk.cost_factor
        * pyo.units.convert(
            installed_membrane_cost * blk.membrane_area,
            to_units=blk.costing_package.base_currency,
        )
    )

    if cost_electricity_flow:
        specific_scour_energy = 0.20 * pyo.units.kWh / pyo.units.m**3
        scour_power = pyo.units.convert(
            specific_scour_energy
            * pyo.units.convert(permeate_flow, to_units=pyo.units.m**3 / pyo.units.hour),
            to_units=pyo.units.kW,
        )
        blk.costing_package.cost_flow(scour_power, "electricity")

