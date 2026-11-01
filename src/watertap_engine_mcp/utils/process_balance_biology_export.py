"""Export solved native ModifiedASM2d ports and equation-owned source terms.

The exported reaction terms are stoichiometric coefficients multiplied by the
native rate-reaction extents. They are never calculated from inlet/outlet
residuals. This is the numerical handoff to the central process-balance engine,
not a second biological model or a persistence surface.
"""

from __future__ import annotations

import math


def native_biology_constraint_diagnostics(model):
    """Retain actual failed equations; do not turn their residuals into terms."""
    import pyomo.environ as pyo

    rows = []
    for constraint in model.component_data_objects(pyo.Constraint, active=True, descend_into=True):
        try:
            body = float(pyo.value(constraint.body))
            lower = float(pyo.value(constraint.lower)) if constraint.has_lb() else None
            upper = float(pyo.value(constraint.upper)) if constraint.has_ub() else None
            residual = max(0.0, (lower - body) if lower is not None else 0.,
                           (body - upper) if upper is not None else 0.)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if math.isfinite(residual):
            rows.append({"constraint": constraint.name, "absolute_residual": residual,
                         "body": body, "lower": lower, "upper": upper})
    return sorted(rows, key=lambda row: (-row["absolute_residual"], row["constraint"]))[:20]


def scale_modified_asm2d_material_equations(model, aerobic_units):
    """Condition native material equations for trace-component daily closure.

    Concentration/pressure/temperature equations retain native scaling. Material
    equations are kg/s; checking a tiny native residual as though it were
    kg/day leaves trace pools poorly closed. A moderate material row scale
    (100), together with the native constraint tolerance, resolves trace pools
    without the ill conditioning from multiplying every water row by 86400.
    This is a positive equation
    scaling only and changes neither the model equations nor design criteria.
    """
    import idaes.core.util.scaling as iscale

    constraints = [model.fs.MX1.material_mixing_equations,
                   model.fs.MBR.material_splitting_eqn,
                   model.fs.SludgeSplitter.material_splitting_eqn,
                   model.fs.RASPressure.control_volume.material_balances,
                   # a native side feed's mixer (actions.SIDE_FEEDS_TARGET), as the feed's
                   *(item["mixer"].material_mixing_equations for item in getattr(model, "native_side_feeds", ()))]
    # an aerobic-only MBR builds no anoxic reactor (actions._modified_asm2d_mbr_rating_direct_result)
    anoxic = getattr(model.fs, "Anoxic", None)
    for unit in ((anoxic,) if anoxic is not None else ()) + tuple(aerobic_units):
        constraints.extend((unit.control_volume.material_balances,
                            unit.control_volume.rate_reaction_stoichiometry_constraint,
                            unit.cstr_performance_eqn))
    for group in constraints:
        for constraint in group.values():
            iscale.constraint_scaling_transform(constraint, 100.0, overwrite=True)


# Energy rows of the ModifiedASM2d flowsheet: every CSTR / pressure-changer
# control-volume enthalpy balance and the mixer's enthalpy mixing equation.
ENERGY_ROW_COMPONENTS = ("enthalpy_balances", "enthalpy_mixing_equations")


def restate_modified_asm2d_energy_equations(model, *, constr_viol_tol):
    """Restate each energy row per unit rho*cp so its float resolution can meet the polish tolerance.

    ModifiedASM2d writes enthalpy flow as ``Q * rho * cp * (T - T_ref)`` in W.
    At LB TEX flows the mixer and aerobic rows carry about 2.9e6 W, and one
    float64 ulp of that is 2**-31 = 4.66e-10 W. The strict polish's absolute
    ``constr_viol_tol`` (1e-11, checked by IPOPT on the unscaled row) is about
    1/47 of one ulp: every term lies in [2**19, 2**22) W, so any nonzero
    residual is a multiple of 2**-33 = 1.16e-10 > 1e-11 and a W-stated row
    passes only when IPOPT's evaluation cancels exactly. LB TEX iterates 01-07
    passed that way; 08 did not (2026-09-18: the rows stuck at 4.66e-10, IPOPT
    entered restoration and exited "Restoration Failed").

    ``rho`` and ``cp`` are constant parameters of this property package, so
    dividing a row by them is an exact restatement: ``sum Q_i (T_i - T_ref) = 0``
    (+ work / (rho cp) on a pressure changer), in m3*K/s. At LB TEX that is about
    0.68 m3*K/s with a resolution near 1e-16, and the solver tolerance is
    unchanged. The record states what the unchanged tolerance means in the
    row's original W, so the restatement cannot hide as an unexplained loosening.
    Positive constant equation scaling only; it changes no model equation
    solution, criterion or bound. It is idempotent: IDAES applies the factor to
    the row as originally written.
    """
    import pyomo.environ as pyo
    import idaes.core.util.scaling as iscale

    rho = float(pyo.value(model.fs.props.dens_mass))
    cp = float(pyo.value(model.fs.props.cp_mass))
    factor = 1.0 / (rho * cp)
    rows = []
    for constraint in model.component_data_objects(pyo.Constraint, active=True, descend_into=True):
        if constraint.parent_component().local_name not in ENERGY_ROW_COMPONENTS:
            continue
        # A unit may already have transformed its own row (IDAES PressureChanger
        # applies 1e-6 to its enthalpy balance); IDAES applies the new factor to
        # the row as ORIGINALLY written, so it replaces that one exactly.
        previous = iscale.get_constraint_transform_applied_scaling_factor(constraint, default=1.0)
        residual_w = float(pyo.value(constraint.body) - pyo.value(constraint.lower)) / previous
        iscale.constraint_scaling_transform(constraint, factor, overwrite=True)
        rows.append({"constraint": constraint.name, "previous_factor": previous,
                     "residual_before_W": residual_w,
                     "residual_after_m3K_s": float(pyo.value(constraint.body) - pyo.value(constraint.lower))})
    if not rows:
        raise ValueError("ModifiedASM2d flowsheet has no energy rows to restate")
    return {
        "rows": rows,
        "factor": factor,
        "factor_basis": "1 / (props.dens_mass * props.cp_mass), read off the solved property package",
        "dens_mass_kg_m3": rho,
        "cp_mass_J_kg_K": cp,
        "original_unit": "W",
        "restated_unit": "m3*K/s",
        "constr_viol_tol": constr_viol_tol,
        "constr_viol_tol_in_original_unit_W": constr_viol_tol / factor,
    }


def export_modified_asm2d_balance(model, aerobic_units, *, membrane_tank=None):
    """The solved ports, reaction extents and injections by role. ``membrane_tank``: the reacting membrane tank
    (actions.MEMBRANE_TANK_TARGET), exported under role ``mbr`` (its port ``membrane_tank_out``); None, the algebraic
    split, exports no mbr reactions."""
    import pyomo.environ as pyo

    fs = model.fs
    components = tuple(str(c) for c in fs.props.component_list if c != "H2O")
    density = float(pyo.value(fs.props.dens_mass))

    def number(value):
        result = float(pyo.value(value))
        if not math.isfinite(result):
            raise ValueError("native biological export contains a nonfinite value")
        # PostgreSQL JSONB serializes -0.0 as +0.0. Canonicalize exact zero
        # before downstream hashes are created; every nonzero trace and
        # residual keeps its actual native value.
        return 0.0 if result == 0.0 else result

    def port_state(port, name):
        flow = number(port.flow_vol[0]) * 86400.0
        # Port references resolve to the native property state block. Read its
        # actual measurement expressions with its fixed composition parameters;
        # never refractionate the solved stream or reuse its influent bulk assay.
        state = port.flow_vol[0].parent_block()
        return {
            "flow_m3_d": flow,
            "temperature_k": number(port.temperature[0]),
            "pressure_kpa": number(port.pressure[0]) / 1000.0,
            "water_mass_kg_d": flow * density,
            "component_mass_kg_d": {
                component: flow * number(port.conc_mass_comp[0, component])
                for component in components
            },
            "aggregate_parameters": {
                "COD_total_mg_L": number(state.COD) * 1000.,
                "TSS_mg_L": number(state.TSS) * 1000.,
                "VSS_mg_L": number(state.VSS) * 1000.,
                "TKN_mg_L": number(state.TKN) * 1000.,
                "TN_mg_L": number(state.TKN + state.SNOX) * 1000.,
                "TP_mg_L": number(state.SP_organic + state.SP_inorganic) * 1000.,
                "BOD5_mg_L": number(state.BOD5["raw" if name == "feed" or name.startswith("side_feed_") else "effluent"]) * 1000.,
            },
        }

    # an aerobic-only MBR builds no anoxic reactor: it exports no anoxic port and no anoxic reactions
    anoxic = getattr(fs, "Anoxic", None)
    ports = {
        "feed": fs.FeedWater.outlet,
        "mixed": fs.MX1.outlet,
        **({"anoxic_out": anoxic.outlet} if anoxic is not None else {}),
        "aerobic_out": aerobic_units[-1].outlet,
        "permeate": fs.MBR.permeate,
        "retentate": fs.MBR.retentate,
        "waste": fs.SludgeSplitter.waste,
        "recycle": fs.RASPressure.outlet,
    }
    ports.update({f"aerobic_stage_{index}_out": unit.outlet
                  for index, unit in enumerate(aerobic_units, 1)})
    if membrane_tank is not None:
        ports["membrane_tank_out"] = membrane_tank.outlet
    # each native side feed (actions.SIDE_FEEDS_TARGET) as fed, under its role: the caller checks its frozen loads
    ports.update({f"side_feed_{item['role']}": item["feed"].outlet for item in getattr(model, "native_side_feeds", ())})
    reactors = {**({"anoxic": [anoxic]} if anoxic is not None else {}), "aerobic": list(aerobic_units),
                **({"mbr": [membrane_tank]} if membrane_tank is not None else {})}
    reactions, transfers = {}, {}
    for role, units in reactors.items():
        rows, injection_rows = [], []
        for index, unit in enumerate(units, 1):
            cv = unit.control_volume
            for reaction in fs.rxn.rate_reaction_idx:
                extent = pyo.units.convert(cv.rate_reaction_extent[0, reaction], to_units=pyo.units.kg / pyo.units.day)
                extent_kg_d = number(extent)
                coefficients = {
                    component: number(fs.rxn.rate_reaction_stoichiometry[reaction, "Liq", component])
                    for component in (*components, "H2O")
                }
                rows.append({
                    "stage": index, "reaction_id": str(reaction),
                    "extent_kg_d": extent_kg_d,
                    "stoichiometry": coefficients,
                    "component_generation_kg_d": {
                        component: extent_kg_d * coefficient
                        for component, coefficient in coefficients.items()
                    },
                })
            if hasattr(unit, "injection"):
                for component in (*components, "H2O"):
                    value = number(pyo.units.convert(unit.injection[0, "Liq", component],
                                                    to_units=pyo.units.kg / pyo.units.day))
                    injection_rows.append({"stage": index, "component_id": component,
                                           "value_kg_d": value})
        reactions[role] = rows
        transfers[role] = injection_rows
    return {
        "schema_version": "watertap_modified_asm2d_balance/v1",
        "component_namespace": "watertap.modified_asm2d/v1",
        "component_ids": [*components, "H2O"],
        "density_kg_m3": density,
        "model_parameters": {
            "kinetic_temperature_k": {"value": number(fs.FeedWater.outlet.temperature[0]), "unit": "K"},
            **{f"{group}.{variable.local_name}": {
                "value": number(variable), "unit": str(pyo.units.get_units(variable))}
               for group, block in (("property_parameter_fixes", fs.props), ("reaction_parameter_fixes", fs.rxn))
               for variable in block.component_objects(pyo.Var, descend_into=False)
               if not variable.is_indexed() and variable.fixed},
        },
        "ports": {name: port_state(port, name) for name, port in ports.items()},
        "reactions": reactions,
        "injections": transfers,
        "assumptions": [
            "Native ModifiedASM2d pseudo-component bases are retained (COD, N, P, C and ionic mass).",
            "Water uses the native constant-density material-flow convention; biochemical water is not modeled.",
            "Oxygen injection is the native dissolved-phase source; air nitrogen and off-gas are outside this model.",
            "S_N2 is retained as dissolved model nitrogen; no unmodeled stripping is inferred.",
            "S_IC is retained in the native liquid inorganic-carbon pool; no CO2 gas partition is solved.",
            "Aggregate TN uses native TKN + SNOX; dissolved S_N2 remains in the component balance.",
            "Native BOD5 uses the raw-feed coefficient at the feed and the effluent coefficient at solved process outlets.",
            "Alkalinity, pH, TDS and conductivity are not inferred from the incomplete native ionic inventory.",
            "The MBR has component-flow splits, no membrane fouling or hydraulic pressure-rating model.",
            *(("The membrane tank is a reactor at its stage-(b) volume, its liquor the retentate (represented by a "
               "CSTR at the split's inlet, actions.MEMBRANE_TANK_LIQUOR_EQUIVALENCE); its scour air transfers O2 at "
               "KLa V (S_O_eq - S_O2) and its DO is solved, not set.",) if membrane_tank is not None else ()),
            *((f"A side feed at the {item['role']} role ({item['flow_m3_d']:.6g} m3/d) enters "
               f"{'the membrane split' if item['into'] is None else item['into'].local_name} through its own mixer at "
               "the feed's temperature and pressure; the RAS ratio is to the group's whole liquid inflow "
               "(actions.SIDE_FEEDS_TARGET).") for item in getattr(model, "native_side_feeds", ())),
        ],
    }
