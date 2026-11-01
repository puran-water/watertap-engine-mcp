'Nitrifier invasion test and branch reseed for the direct MBR rating path.'

from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Sequence, Tuple

SECONDS_PER_DAY = 86400.0
_AUTOTROPH = "X_AUT"
_AMMONIUM = "S_NH4"
_NITRATE = "S_NO3"


def _value(expr: Any) -> float:
    import pyomo.environ as pyo

    return float(pyo.value(expr))


def _implicit_rate_derivative(reaction_block: Any, reaction: Any, variable: Any) -> float:
    """d(reaction_rate[r]) / d(variable) implied by the rate_expression constraint (1/s)."""

    from pyomo.core.expr.calculus.derivatives import Modes, differentiate

    constraint = reaction_block.rate_expression[reaction]
    rate = reaction_block.reaction_rate[reaction]
    body = constraint.body
    d_var = float(differentiate(body, wrt=variable, mode=Modes.reverse_numeric))
    d_rate = float(differentiate(body, wrt=rate, mode=Modes.reverse_numeric))
    if d_rate == 0.0:
        raise ValueError(f"rate_expression[{reaction}] does not define reaction_rate[{reaction}]")
    return -d_var / d_rate


def net_specific_autotroph_growth(unit: Any, reaction_package: Any) -> Dict[str, Any]:
    """Linearised net specific X_AUT growth (1/d) in one reactor at its solved state."""

    reaction_block = unit.control_volume.reactions[0.0]
    variable = reaction_block.conc_mass_comp_ref[_AUTOTROPH]
    stoichiometry = reaction_package.rate_reaction_stoichiometry
    contributions: Dict[str, float] = {}
    for reaction in reaction_package.rate_reaction_idx:
        coefficient = stoichiometry.get((reaction, "Liq", _AUTOTROPH), 0)
        coefficient = _value(coefficient) if not isinstance(coefficient, (int, float)) else float(coefficient)
        if coefficient == 0.0:
            continue
        derivative = _implicit_rate_derivative(reaction_block, reaction, variable)
        contributions[str(reaction)] = coefficient * derivative * SECONDS_PER_DAY
    state = unit.control_volume.properties_out[0.0]
    return {
        "unit": unit.local_name,
        "net_specific_growth_per_d": sum(contributions.values()),
        "reaction_contributions_per_d": contributions,
        "state_mg_L": {
            component: _value(state.conc_mass_comp[component]) * 1000.0
            for component in ("S_O2", _AMMONIUM, "S_PO4", _NITRATE, _AUTOTROPH)
        },
    }


def _dominant_eigenvalue(growth: Sequence[float], volumes: Sequence[float], flows: Sequence[float],
                         recycle_return_fraction: float) -> float:
    """Dominant eigenvalue (1/d) of the linearised X_AUT balance over a CSTR train.

    dX_0/dt = (f Q_n X_n - Q_0 X_0)/V_0 + g_0 X_0
    dX_k/dt = (Q_{k-1} X_{k-1} - Q_k X_k)/V_k + g_k X_k     (k >= 1)

    where f is the fraction of the last reactor's X_AUT flow that returns to the
    first reactor (retentate x RAS split); the remainder leaves as permeate
    solids and WAS.
    """

    import numpy as np

    size = len(growth)
    matrix = np.zeros((size, size))
    for index in range(size):
        matrix[index, index] = growth[index] - flows[index] / volumes[index]
        if index:
            matrix[index, index - 1] = flows[index - 1] / volumes[index]
    matrix[0, size - 1] += recycle_return_fraction * flows[-1] / volumes[0]
    return float(max(np.linalg.eigvals(matrix).real))


def autotroph_invasion(model: Any, reactors: Sequence[Any], *, srt_d: float | None) -> Dict[str, Any]:
    """Evaluate whether an autotroph population invades the solved state."""

    fs = model.fs
    rows = [net_specific_autotroph_growth(unit, fs.rxn) for unit in reactors]
    volumes = [_value(unit.volume[0.0]) for unit in reactors]
    flows = [_value(unit.control_volume.properties_out[0.0].flow_vol) * SECONDS_PER_DAY for unit in reactors]
    permeate_fraction = _value(fs.MBR.split_fraction[0.0, "permeate", _AUTOTROPH])
    recycle_fraction = _value(fs.SludgeSplitter.split_fraction[0.0, "recycle"])
    returned = (1.0 - permeate_fraction) * recycle_fraction
    growth = [row["net_specific_growth_per_d"] for row in rows]
    eigenvalue = _dominant_eigenvalue(growth, volumes, flows, returned)
    wastage = 1.0 / srt_d if srt_d and srt_d > 0 else None
    for row in rows:
        row["invasion_rate_per_d"] = (
            row["net_specific_growth_per_d"] - wastage if wastage is not None else None
        )
    weighted = sum(g * v for g, v in zip(growth, volumes)) / sum(volumes)
    return {
        "method": (
            "dominant eigenvalue of the linearised X_AUT balance over the reactor train; "
            "net growth = sum_r nu(r,X_AUT) * d rate_r/d X_AUT from the installed rate expressions"
        ),
        "invasion_eigenvalue_per_d": eigenvalue,
        "invasive": eigenvalue > 0.0,
        "reactors": rows,
        "wastage_rate_per_d": wastage,
        "srt_d": srt_d,
        "volume_weighted_net_growth_minus_wastage_per_d": (
            weighted - wastage if wastage is not None else None
        ),
        "recycle_return_fraction": returned,
        "volumes_m3": volumes,
        "flows_m3_d": flows,
    }


def _stream_blocks(model: Any) -> List[Any]:
    """Every state-block data object carrying a free concentration vector."""

    import pyomo.environ as pyo

    blocks = []
    seen = set()
    for var in model.component_data_objects(pyo.Var, descend_into=True):
        if var.parent_component().local_name != "conc_mass_comp":
            continue
        block = var.parent_block()
        if id(block) in seen:
            continue
        seen.add(id(block))
        blocks.append(block)
    return blocks


def _neutral_ammonium(model: Any, reactors: Sequence[Any], aerobic_units: Sequence[Any],
                      *, srt_d: float, upper_kg_m3: float) -> float:
    """Aerobic ammonium (kg/m3) at which the linearised X_AUT balance is neutral."""

    saved = {
        unit.local_name: _value(unit.control_volume.properties_out[0.0].conc_mass_comp[_AMMONIUM])
        for unit in aerobic_units
    }

    def eigenvalue_at(level: float) -> float:
        for unit in aerobic_units:
            unit.control_volume.properties_out[0.0].conc_mass_comp[_AMMONIUM].set_value(level)
        return autotroph_invasion(model, reactors, srt_d=srt_d)["invasion_eigenvalue_per_d"]

    try:
        low, high = 0.0, upper_kg_m3
        if eigenvalue_at(high) <= 0.0:
            raise ValueError("no invasive ammonium level below the solved washout level")
        for _ in range(60):
            middle = 0.5 * (low + high)
            if eigenvalue_at(middle) > 0.0:
                high = middle
            else:
                low = middle
        return high
    finally:
        for unit in aerobic_units:
            unit.control_volume.properties_out[0.0].conc_mass_comp[_AMMONIUM].set_value(saved[unit.local_name])


def reseed_nitrifying_branch(model: Any, reactors: Sequence[Any], aerobic_units: Sequence[Any], *,
                             srt_d: float, reactor_volume_m3: float, discharged_ammonium_kg_d: float,
                             ammonium_margin: float = 1.0, particulates: Sequence[str],
                             boundary_blocks: Sequence[Any] = ()) -> Dict[str, Any]:
    """Move the warm start from the washout state to the nitrifying branch.

    ``boundary_blocks`` are state blocks equal to the frozen feed (the feed
    unit's own state and the mixer's feed inlet); they are never reseeded.

    Returns the seed record. Every value is computed from the solved model:
    ``Y_A`` and ``b_AUT`` from the reaction package, the neutral ammonium level
    from the linearised balance, the X_AUT inventory from a steady-state balance
    on the ammonium the washout state discharges:

        X_AUT = Y_A * N_nitrifiable * SRT / (V * (1 + b_AUT * SRT))
    """

    import pyomo.environ as pyo

    fs = model.fs
    last = aerobic_units[-1].control_volume.properties_out[0.0]
    washout_ammonium = _value(last.conc_mass_comp[_AMMONIUM])
    neutral = _neutral_ammonium(model, reactors, aerobic_units, srt_d=srt_d, upper_kg_m3=washout_ammonium)
    target_ammonium = min(washout_ammonium, neutral * ammonium_margin)
    shift = washout_ammonium - target_ammonium

    yield_a = _value(fs.rxn.Y_A)
    decay = _value(pyo.units.convert(fs.rxn.b_AUT, to_units=1 / pyo.units.day))
    seed = yield_a * discharged_ammonium_kg_d * srt_d / (reactor_volume_m3 * (1.0 + decay * srt_d))
    if not (math.isfinite(seed) and seed > 0.0):
        raise ValueError(f"nitrifier reseed estimate is not positive: {seed!r}")

    reference_solids = sum(
        _value(last.conc_mass_comp[component]) for component in particulates if component != _AUTOTROPH
    )
    frozen = {id(block) for block in boundary_blocks}
    touched = 0
    for block in _stream_blocks(model):
        concentration = block.conc_mass_comp
        autotroph = concentration[_AUTOTROPH]
        if autotroph.fixed or id(block) in frozen:
            continue  # the feed and its mixer inlet are a frozen boundary; never reseeded
        solids = sum(_value(concentration[c]) for c in particulates if c != _AUTOTROPH)
        autotroph.set_value(seed * solids / reference_solids if reference_solids > 0 else seed)
        ammonium, nitrate = concentration[_AMMONIUM], concentration[_NITRATE]
        if not ammonium.fixed and not nitrate.fixed:
            old = _value(ammonium)
            moved = min(shift, max(old - target_ammonium, 0.0))
            ammonium.set_value(old - moved)
            nitrate.set_value(_value(nitrate) + moved)
        touched += 1

    _recompute_reaction_rates(reactors)
    return {
        "x_aut_seed_mg_L": seed * 1000.0,
        "x_aut_seed_basis": "Y_A * N_nitrifiable * SRT / (V * (1 + b_AUT * SRT)), solids-proportional across streams",
        "Y_A": yield_a,
        "b_AUT_per_d": decay,
        "srt_d": srt_d,
        "reactor_volume_m3": reactor_volume_m3,
        "nitrifiable_ammonium_kg_N_d": discharged_ammonium_kg_d,
        "washout_aerobic_ammonium_mg_L": washout_ammonium * 1000.0,
        "neutral_aerobic_ammonium_mg_L": neutral * 1000.0,
        "seeded_aerobic_ammonium_mg_L": target_ammonium * 1000.0,
        "ammonium_margin": ammonium_margin,
        "state_blocks_reseeded": touched,
    }


def _recompute_reaction_rates(reactors: Sequence[Any]) -> None:
    """Make each reactor's reaction_rate consistent with its reseeded state."""

    from pyomo.util.calc_var_value import calculate_variable_from_constraint

    for unit in reactors:
        reaction_block = unit.control_volume.reactions[0.0]
        for reaction in reaction_block.rate_expression:
            try:
                calculate_variable_from_constraint(
                    reaction_block.reaction_rate[reaction], reaction_block.rate_expression[reaction]
                )
            except Exception:
                continue  # an unresolvable guess is left for the solver, never forced


def solved_srt_d(reactors: Sequence[Any], waste_port: Any, particulates: Sequence[str]) -> float | None:
    """Reactor solids inventory / WAS solids flow (d), read off the solved state.

    The same definition the rating result reports as ``SRT_actual_d``. The port's ``flow_vol`` is m3/s (the
    ModifiedASM2d state), so the waste flow is taken per day (until c166 this returned seconds).
    """

    inventory = sum(
        _value(unit.volume[0.0])
        * sum(_value(unit.control_volume.properties_out[0.0].conc_mass_comp[c]) for c in particulates)
        for unit in reactors
    )
    wasted = _value(waste_port.flow_vol[0.0]) * SECONDS_PER_DAY * sum(
        _value(waste_port.conc_mass_comp[0.0, c]) for c in particulates
    )
    return inventory / wasted if wasted > 0 else None


def select_nitrifying_branch(
    model: Any,
    reactors: Sequence[Any],
    aerobic_units: Sequence[Any],
    *,
    resolve: Callable[[], Any],
    is_optimal: Callable[[Any], bool],
    waste_port: Any,
    permeate_port: Any,
    particulates: Sequence[str],
    boundary_blocks: Sequence[Any],
    ammonium_margins: Sequence[float],
    viability_floor_mg_l: float,
) -> Tuple[Any, Dict[str, Any] | None]:
    """Keep or replace an optimal base solve according to nitrifier stability.

    Returns ``(new_results, record)``. ``new_results`` is None when the model
    still holds the original base solution (not a washout; a STABLE washout,
    which is the physical answer and is left for the physicality gate to
    refuse; or every bounded reseed failed, in which case the washout point is
    restored). ``record`` is None only when the base solve is not a washout.
    """

    last = aerobic_units[-1].control_volume.properties_out[0.0].conc_mass_comp
    x_aut_mg_l = _value(last[_AUTOTROPH]) * 1000.0
    if x_aut_mg_l >= viability_floor_mg_l:
        return None, None

    srt = solved_srt_d(reactors, waste_port, particulates)
    record: Dict[str, Any] = {
        "trigger": "optimal base solve on the X_AUT washout root",
        "viability_floor_mg_L": viability_floor_mg_l,
        "first_solve": {
            "X_AUT_mg_L": x_aut_mg_l,
            "aerobic_NH4_N_mg_L": _value(last[_AMMONIUM]) * 1000.0,
            "permeate_NH4_N_mg_L": _value(permeate_port.conc_mass_comp[0.0, _AMMONIUM]) * 1000.0,
            "SRT_actual_d": srt,
        },
        "attempts": [],
    }
    try:
        invasion = autotroph_invasion(model, reactors, srt_d=srt)
    except Exception as exc:
        record["base_verdict"] = "invasion_test_failed"
        record["error"] = f"{type(exc).__name__}: {exc}"
        return None, record
    record["washout_invasion"] = invasion
    if not invasion["invasive"]:
        record["base_verdict"] = "washout_stable"
        return None, record

    import pyomo.environ as pyo

    washout_point = [
        (var, var.value) for var in model.component_data_objects(pyo.Var, descend_into=True) if not var.fixed
    ]

    def restore() -> None:
        for var, number in washout_point:
            var.set_value(number, skip_validation=True)

    discharged = SECONDS_PER_DAY * (
        _value(permeate_port.flow_vol[0.0]) * _value(permeate_port.conc_mass_comp[0.0, _AMMONIUM])
        + _value(waste_port.flow_vol[0.0]) * _value(waste_port.conc_mass_comp[0.0, _AMMONIUM])
    )
    volume = sum(_value(unit.volume[0.0]) for unit in reactors)
    for attempt, margin in enumerate(ammonium_margins, start=1):
        restore()
        entry: Dict[str, Any] = {"attempt": attempt}
        record["attempts"].append(entry)
        try:
            entry["seed"] = reseed_nitrifying_branch(
                model, reactors, aerobic_units, srt_d=srt, reactor_volume_m3=volume,
                discharged_ammonium_kg_d=discharged, ammonium_margin=margin,
                particulates=particulates, boundary_blocks=boundary_blocks,
            )
            results = resolve()
            entry["termination_condition"] = str(results.solver.termination_condition)
            if not is_optimal(results):
                continue
            entry["X_AUT_mg_L"] = _value(last[_AUTOTROPH]) * 1000.0
            entry["aerobic_NH4_N_mg_L"] = _value(last[_AMMONIUM]) * 1000.0
            entry["permeate_NH4_N_mg_L"] = _value(permeate_port.conc_mass_comp[0.0, _AMMONIUM]) * 1000.0
            entry["SRT_actual_d"] = solved_srt_d(reactors, waste_port, particulates)
            entry["invasion_eigenvalue_after_per_d"] = autotroph_invasion(
                model, reactors, srt_d=entry["SRT_actual_d"]
            )["invasion_eigenvalue_per_d"]
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            continue
        if entry["X_AUT_mg_L"] >= viability_floor_mg_l:
            record["base_verdict"] = "reseeded_nitrifying_branch"
            return results, record
    restore()
    record["base_verdict"] = "reseed_failed"
    return None, record
