'Where the PAO invasion test (solver/pao_invasion.py) finds the pinned zero-PAO solution invaded (rate >= 0), the zero'

from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence

from watertap_engine_mcp.solver.pao_invasion import POOLS, SECONDS_PER_DAY, _Dynamics, _ReactorRates, _train, _transport_matrix

#: The ruling that built this route; carried on every record.
EBPR_ROUTE_RULING_ID = 'PUBLIC-RULE-187e44c36a83'
#: The explicit dynamics hold at the solved state to this, relative to each row's largest term; a row is allowed the
#: native solve's own relative resolution (1e-9, the strict polish's tol) x the largest row scale besides: below that a
#: row's terms are solver noise, not a balance.
DYNAMICS_RELATIVE_TOLERANCE = 1e-6
NATIVE_RELATIVE_RESOLUTION = 1e-9
#: The dominant eigenvalue is decided only beyond this x the Jacobian's infinity norm (1/d): its float resolution
#: (LAPACK's backward error is O(eps |J|); the margin covers the eigenvector conditioning).
EIGENVALUE_RELATIVE_RESOLUTION = 1e-9
#: The pool balances at the solved pools: |F(v)| within this x their largest term (the invasion test's ray tolerance).
BALANCED_GROWTH_RELATIVE_TOLERANCE = 1e-8
#: The trajectory: integrated in legs of the solved SRT, settled when every row's |dc/dt| is within
#: SETTLED_RELATIVE_TOLERANCE of its largest term (the start of the solve, not its answer: the solve and the polish own
#: the tolerance), refused past its horizon. The integration's tolerances set its pace only.
SETTLED_RELATIVE_TOLERANCE = 1e-6

#: goes on SLOWEST_MODE_TIME_CONSTANTS time constants of its slowest mode (the dominant eigenvalue of its Jacobian
#: there, `dominant_mode`, which must be stable), in legs of the longer of the SRT and one time constant. Near a neutral
#: invasion the stable state's dominant eigenvalue is near zero too and the trajectory settles at that mode's pace, not
#: the SRT's (c176: ASM2d as published at LB TEX DAP 22-30). A row's relative rate is at most 4 (four terms, each within
#: its scale), so the mode brings it below the tolerance within ln(4 / tolerance) time constants.
MAX_SRTS = 100
SLOWEST_MODE_TIME_CONSTANTS = math.log(4.0 / SETTLED_RELATIVE_TOLERANCE)
INTEGRATION_RTOL, INTEGRATION_ATOL_KG_M3 = 1e-6, 1e-10


class EbprStabilityError(ValueError):
    """The stability of the seeded solution has no answer; the message names the check that failed."""


class EbprTrajectoryError(ValueError):
    """The trajectory from the seed has no settled state; the message names why."""


def _value(expr: Any) -> float:
    import pyomo.environ as pyo

    return float(pyo.value(expr))


def _per_day(var: Any) -> float:
    import pyomo.environ as pyo

    return float(pyo.value(pyo.units.convert(var, to_units=1 / pyo.units.day)))


def _flux_kg_d(port: Any, component: str) -> float:
    return _value(port.flow_vol[0.0]) * SECONDS_PER_DAY * _value(port.conc_mass_comp[0.0, component])


def ray_seed(model: Any, reactors: Sequence[Any], invasion: Dict[str, Any], *, srt_d: float,
             permeate_port: Any, waste_port: Any) -> Dict[str, Any]:
    "The pools to start from: the invading ray's per-reactor shape (``invasion``, the pin's ``pao_invasion`` record)"
    fs = model.fs
    rows = invasion["reactors"]
    if [row["unit"] for row in rows] != [unit.local_name for unit in reactors]:
        raise ValueError("the invasion record's reactors are not this train's")
    volumes = [_value(unit.volume[0.0]) for unit in reactors]
    shares = [[float(row["ray_share"][pool]) for pool in POOLS] for row in rows]
    pao_volume = math.fsum(v * share[0] for v, share in zip(volumes, shares))
    pp_volume = math.fsum(v * share[2] for v, share in zip(volumes, shares))
    if not (pao_volume > 0.0 and pp_volume > 0.0):
        raise ValueError(f"the invading ray carries no X_PAO or X_PP: {shares}")
    y_pao, b_pao = _value(fs.rxn.Y_PAO), _per_day(fs.rxn.b_PAO)
    first_out = reactors[0].control_volume.properties_out[0.0]
    acetate_kg_d = _value(first_out.flow_vol) * SECONDS_PER_DAY * _value(first_out.conc_mass_comp["S_A"])
    phosphate_kg_d = _flux_kg_d(permeate_port, "S_PO4") + _flux_kg_d(waste_port, "S_PO4")
    rho = pp_volume / pao_volume
    limits = {"acetate": y_pao * acetate_kg_d * srt_d / (1.0 + b_pao * srt_d),
              "phosphorus": phosphate_kg_d * srt_d / rho}
    binding = min(limits, key=limits.get)
    inventory = limits[binding]
    if not (math.isfinite(inventory) and inventory > 0.0):
        raise ValueError(f"the stated X_PAO inventory is not positive: {limits}")
    scale = inventory / pao_volume
    return {
        "basis": ("the invading ray's per-reactor X_PAO/X_PHA/X_PP shape scaled to the X_PAO inventory "
                  "min(Y_PAO A SRT / (1 + b_PAO SRT), P SRT / rho): A the acetate leaving the first reactor and P the "
                  "phosphate discharged (permeate + waste) at the pinned solution, rho the ray's inventory-weighted "
                  "X_PP/X_PAO; a start, not an answer"),
        "x_pao_inventory_kg_cod": inventory, "binding_limit": binding, "limits_kg_cod": limits,
        "Y_PAO": y_pao, "b_PAO_per_d": b_pao, "srt_d": srt_d,
        "first_reactor_acetate_kg_cod_d": acetate_kg_d, "discharged_phosphate_kg_p_d": phosphate_kg_d,
        "ray_x_pp_to_x_pao": rho, "reactor_volumes_m3": volumes,
        "seed_kg_m3": {unit.local_name: {pool: scale * share[j] for j, pool in enumerate(POOLS)}
                       for unit, share in zip(reactors, shares)},
    }


class TrainDynamics:
    """The reactor train's explicit dynamics dc/dt (kg/m3/d) over every free outlet concentration of every reactor:
    CSTR transport, the feed and the membrane-retentate x RAS return into the first reactor (the solved flows and splits,
    which no concentration moves), and N^T r with each installed rate read implicitly from its row at the state set.
    A held concentration (the aerobic DO set point) is a constant input, never a state. A reactor aerated at a stated
    KLa (the reacting membrane tank, actions.MEMBRANE_TANK_TARGET) adds its O2 transfer KLa (S_O_eq - S_O2) to its
    free S_O2 state, in the generation term."""

    def __init__(self, model: Any, reactors: Sequence[Any]) -> None:
        import numpy as np
        from pyomo.common.collections import ComponentSet
        from pyomo.core.expr.calculus.derivatives import Modes, differentiate
        from pyomo.core.expr.visitor import identify_variables

        self._differentiate, self._mode = differentiate, Modes.reverse_numeric
        fs = model.fs
        self.components = [str(c) for c in fs.props.solute_set]
        self.reactors = list(reactors)
        self.reactions = [str(r) for r in fs.rxn.rate_reaction_idx]
        stoichiometry = fs.rxn.rate_reaction_stoichiometry
        nu = np.array([[_value(stoichiometry.get((r, "Liq", c), 0)) for c in self.components] for r in self.reactions])
        self.states: List[tuple] = []
        for unit in self.reactors:
            out = unit.control_volume.properties_out[0.0]
            self.states.extend((unit.local_name, c) for c in self.components if not out.conc_mass_comp[c].fixed)
        self.index = {state: i for i, state in enumerate(self.states)}
        size = len(self.states)
        volumes = [_value(unit.volume[0.0]) for unit in self.reactors]
        flows = [_value(unit.control_volume.properties_out[0.0].flow_vol) * SECONDS_PER_DAY for unit in self.reactors]
        recycle = _value(fs.SludgeSplitter.split_fraction[0.0, "recycle"])
        feed = fs.MX1.feed_water_state[0.0]
        feed_flow = _value(feed.flow_vol) * SECONDS_PER_DAY
        self.transport = np.zeros((size, size))
        self.inflow = np.zeros(size)          # constant inflows (the feed, a held upstream concentration), kg/m3/d
        for k, unit in enumerate(self.reactors):
            for c in self.components:
                i = self.index.get((unit.local_name, c))
                if i is None:
                    continue
                self.transport[i, i] -= flows[k] / volumes[k]
                if k:
                    upstream, coefficient = self.reactors[k - 1], flows[k - 1] / volumes[k]
                else:
                    upstream = self.reactors[-1]
                    returned = (1.0 - _value(fs.MBR.split_fraction[0.0, "permeate", c])) * recycle
                    coefficient = returned * flows[-1] / volumes[0]
                    self.inflow[i] += feed_flow * _value(feed.conc_mass_comp[c]) / volumes[0]
                j = self.index.get((upstream.local_name, c))
                if j is None:
                    self.inflow[i] += coefficient * _value(upstream.control_volume.properties_out[0.0].conc_mass_comp[c])
                else:
                    self.transport[i, j] += coefficient
        # a native side feed (actions.SIDE_FEEDS_TARGET) is a constant inflow into the reactor it enters; entering the
        # membrane split, its returned retentate share enters the first reactor with the return
        for item in getattr(model, "native_side_feeds", ()):
            side = item["feed"].properties[0.0]
            side_flow = _value(side.flow_vol) * SECONDS_PER_DAY
            k = 0 if item["into"] is None else next((k for k, unit in enumerate(self.reactors) if unit is item["into"]),
                                                    None)
            if k is None:
                raise EbprStabilityError(f"side feed {item['role']} enters {item['into'].local_name}, outside the train")
            for c in self.components:
                i = self.index.get((self.reactors[k].local_name, c))
                if i is None:
                    continue
                share = 1.0 if item["into"] is not None else (
                    1.0 - _value(fs.MBR.split_fraction[0.0, "permeate", c])) * recycle
                self.inflow[i] += share * side_flow * _value(side.conc_mass_comp[c]) / volumes[k]
        self._blocks = []
        for unit in self.reactors:
            block = unit.control_volume.reactions[0.0]
            out = unit.control_volume.properties_out[0.0]
            free = [(c, out.conc_mass_comp[c]) for c in self.components if (unit.local_name, c) in self.index]
            allowed = ComponentSet(var for _, var in free)
            rows, rates, coefficients = [], [], []
            for r in self.reactions:
                row, rate = block.rate_expression[r], block.reaction_rate[r]
                stray = [var.name for var in identify_variables(row.body, include_fixed=False)
                         if var is not rate and var not in allowed]
                if stray:
                    raise EbprStabilityError(f"rate_expression[{r}] of {unit.local_name} reads free variables other "
                                             f"than its rate and the reactor's concentrations: {stray[:5]}")
                coefficient = float(differentiate(row.body, wrt=rate, mode=self._mode))
                if coefficient == 0.0:
                    raise EbprStabilityError(f"rate_expression[{r}] does not define reaction_rate[{r}]")
                rows.append(row), rates.append(rate), coefficients.append(coefficient)
            self._blocks.append({
                "vars": [var for _, var in free], "index": [self.index[(unit.local_name, c)] for c, _ in free],
                "rows": rows, "rates": rates, "coefficients": coefficients,
                "nu": nu[:, [self.components.index(c) for c, _ in free]],
            })
        # (state index, KLa in 1/d, S_O_eq in kg/m3) of every reactor whose O2 crosses at a fixed KLa
        self.aeration = []
        for unit in self.reactors:
            if getattr(getattr(unit, "config", None), "has_aeration", False):
                i = self.index.get((unit.local_name, "S_O2"))
                if i is not None:
                    self.aeration.append((i, _value(unit.KLa) * 24.0, _value(unit.S_O_eq)))
        self.initial = self.state()

    def state(self):
        """The current values of the states."""
        import numpy as np

        values = np.zeros(len(self.states))
        for block in self._blocks:
            values[block["index"]] = [_value(var) for var in block["vars"]]
        return values

    def set_state(self, values) -> None:
        for block in self._blocks:
            for var, value in zip(block["vars"], values[block["index"]]):
                var.set_value(float(value), skip_validation=True)

    def _rates_per_d(self, block) -> List[float]:
        return [(_value(rate) - (_value(row.body) - _value(row.upper)) / coefficient) * SECONDS_PER_DAY
                for row, rate, coefficient in zip(block["rows"], block["rates"], block["coefficients"])]

    def generation(self, values):
        """N^T r at ``values`` (kg/m3/d); the model's concentrations are left at ``values``."""
        import numpy as np

        self.set_state(values)
        out = np.zeros(len(self.states))
        for block in self._blocks:
            out[block["index"]] = block["nu"].T @ np.array(self._rates_per_d(block))
        for i, kla_per_d, saturation in self.aeration:
            out[i] += kla_per_d * (saturation - values[i])
        return out

    def rhs(self, values):
        return self.inflow + self.transport @ values + self.generation(values)

    def jacobian(self, values):
        """d(dc/dt)/dc at ``values`` (1/d), each rate's derivative implied by its row."""
        import numpy as np

        self.set_state(values)
        out = self.transport.copy()
        for block in self._blocks:
            d_rates = np.array([[-float(d) / coefficient * SECONDS_PER_DAY for d in
                                 self._differentiate(row.body, wrt_list=block["vars"], mode=self._mode)]
                                for row, coefficient in zip(block["rows"], block["coefficients"])])
            out[np.ix_(block["index"], block["index"])] += block["nu"].T @ d_rates
        for i, kla_per_d, _ in self.aeration:
            out[i, i] -= kla_per_d
        return out

    def relative_residual(self, values):
        """``(|dc/dt| / allowed scale per row, dc/dt)``: each row against its largest term, floored at the native
        resolution x the largest row scale."""
        import numpy as np

        transported = self.transport @ values
        diagonal = np.diag(self.transport) * values
        generated = self.generation(values)
        value = self.inflow + transported + generated
        scale = np.maximum.reduce([np.abs(self.inflow), np.abs(diagonal), np.abs(transported - diagonal),
                                   np.abs(generated)])
        allowed = np.maximum(scale, NATIVE_RELATIVE_RESOLUTION / DYNAMICS_RELATIVE_TOLERANCE * float(np.max(scale)))
        return np.abs(value) / allowed, value


def settle(dynamics: TrainDynamics, start, *, srt_d: float) -> Dict[str, Any]:
    """Integrate the train dynamics from ``start`` in legs of ``srt_d`` until every row is settled (its |dc/dt| within
    SETTLED_RELATIVE_TOLERANCE of `TrainDynamics.relative_residual`'s scale), within the horizon (MAX_SRTS, then the
    slowest mode's SLOWEST_MODE_TIME_CONSTANTS); the settled state and the record (``horizon`` once the SRT budget is
    spent). Raises `EbprTrajectoryError` if the integration fails, the slowest mode where the SRT budget ends is not
    stable, or nothing settles within the horizon."""
    import numpy as np
    from scipy.integrate import solve_ivp

    values = np.clip(np.asarray(start, dtype=float), 0.0, None)
    legs, days, leg_days, horizon_days, horizon = 0, 0.0, srt_d, MAX_SRTS * srt_d, None
    pools = [i for i, (_, c) in enumerate(dynamics.states) if c in POOLS]
    history = []
    while True:
        if days >= horizon_days:
            if horizon is not None:
                break
            mode = dominant_mode(dynamics.jacobian(values), [f"{unit}.{c}" for unit, c in dynamics.states])
            horizon = {key: mode[key] for key in ("dominant_eigenvalue_per_d", "dominant_eigenvalue_imag_per_d",
                                                  "resolution_per_d", "verdict", "dominant_mode_states")}
            if mode["verdict"] != "stable":
                raise EbprTrajectoryError(
                    f"the trajectory from the seed did not settle in {MAX_SRTS} SRTs ({days:g} d) and its slowest "
                    f"mode there is {mode['verdict']} ({mode['dominant_eigenvalue_per_d']!r} per d): no horizon; "
                    f"largest relative rate {history[-1]['largest_relative_rate']!r}, last pools "
                    f"{history[-1]['pools_kg_m3']}")
            time_constant_d = -1.0 / mode["dominant_eigenvalue_per_d"]
            leg_days, horizon_days = max(srt_d, time_constant_d), days + SLOWEST_MODE_TIME_CONSTANTS * time_constant_d
            horizon.update({"srt_budget_days": days, "time_constant_d": time_constant_d,
                            "time_constants": SLOWEST_MODE_TIME_CONSTANTS, "horizon_days": horizon_days,
                            "leg_days": leg_days})
        legs += 1
        solution = solve_ivp(lambda _t, y: dynamics.rhs(np.clip(y, 0.0, None)), (0.0, leg_days), values, method="BDF",
                             jac=lambda _t, y: dynamics.jacobian(np.clip(y, 0.0, None)),
                             rtol=INTEGRATION_RTOL, atol=INTEGRATION_ATOL_KG_M3)
        if not solution.success:
            raise EbprTrajectoryError(f"the trajectory's integration failed on leg {legs}: {solution.message}")
        values = np.clip(solution.y[:, -1], 0.0, None)
        days += leg_days
        relative, _ = dynamics.relative_residual(values)
        worst = float(np.max(relative))
        history.append({"day": days, "largest_relative_rate": worst,
                        "pools_kg_m3": {f"{dynamics.states[i][0]}.{dynamics.states[i][1]}": float(values[i])
                                        for i in pools}})
        if worst <= SETTLED_RELATIVE_TOLERANCE:
            return {"values": values, "legs": legs, "days": days, "largest_relative_rate": worst,
                    "history": history[-3:], "leg_days": srt_d, **({"horizon": horizon} if horizon else {})}
    raise EbprTrajectoryError(f"the trajectory from the seed did not settle within its horizon ({days:g} d: {MAX_SRTS} "
                              f"SRTs, then {SLOWEST_MODE_TIME_CONSTANTS:.3g} time constants of its slowest mode, "
                              f"{horizon['dominant_eigenvalue_per_d']!r} per d): largest relative rate "
                              f"{history[-1]['largest_relative_rate']!r}, last pools {history[-1]['pools_kg_m3']}")


def load_train_state(model: Any, reactors: Sequence[Any], dynamics: TrainDynamics, values, *,
                     boundary_blocks: Sequence[Any]) -> int:
    """Put a train state into every free stream block of the model before a solve: each reactor's outlet at ``values``
    (``dynamics``' states), the next reactor's inlet at its upstream outlet, the mixer outlet and the first reactor's
    inlet as the feed and the recycle mixed, and every other block (membrane, splitter, RAS, products, the mixer's
    recycle inlet) at the last reactor's outlet times its own ratio to it at the pinned solution, per component: the
    splits are fixed, so that ratio is a constant of the hydraulics (for a pool, zero at the pin, the ratio of the
    block's own largest particulate). The pinned last-reactor state is read off the membrane's inlet block, which the
    trajectory never moves (it moves the reactor outlets). The feed and its mixer inlet are a frozen boundary. The
    reactors' rates, extents, generations and free injections (the DO set point's oxygen) follow. Returns the count of
    blocks set."""
    from pyomo.util.calc_var_value import calculate_variable_from_constraint
    from watertap_engine_mcp.solver.nitrifier_invasion import _stream_blocks

    fs = model.fs
    components = dynamics.components
    last = reactors[-1].control_volume.properties_out[0.0]
    membrane_inlet = fs.MBR.inlet.conc_mass_comp[0.0, components[0]].parent_block()
    before_last = {c: _value(membrane_inlet.conc_mass_comp[c]) for c in components}
    blocks = [block for block in _stream_blocks(model) if id(block) not in {id(b) for b in boundary_blocks}]
    before = {id(block): {c: _value(block.conc_mass_comp[c]) for c in components} for block in blocks}
    dynamics.set_state(values)
    after_last = {c: _value(last.conc_mass_comp[c]) for c in components}
    reactor_outlets = {id(unit.control_volume.properties_out[0.0]) for unit in reactors}
    inlets = {id(unit.control_volume.properties_in[0.0]): reactors[k - 1].control_volume.properties_out[0.0]
              for k, unit in enumerate(reactors) if k}
    mixed = {id(fs.MX1.mixed_state[0.0]), id(reactors[0].control_volume.properties_in[0.0])}
    # a native side feed (actions.SIDE_FEEDS_TARGET): its mixer's upstream inlet at the upstream outlet, its outlet and
    # the inlet it feeds as the two mixed; entering the membrane split, the membrane inlet the scaled blocks are read
    # against is that mix
    side_mixes = []     # (blocks set to the mix, upstream outlet, side feed state)
    for item in getattr(model, "native_side_feeds", ()):
        mixer, side = item["mixer"], item["feed"].properties[0.0]
        into = item["into"]
        upstream = last if into is None else inlets.pop(id(into.control_volume.properties_in[0.0]))
        inlets[id(mixer.upstream_state[0.0])] = upstream
        targets = [mixer.mixed_state[0.0]] + ([] if into is None else [into.control_volume.properties_in[0.0]])
        side_mixes.append((targets, upstream, side))
        mixed.update(id(block) for block in targets)
        if into is None:
            q_up, q_side = _value(upstream.flow_vol), _value(side.flow_vol)
            after_last = {c: (q_up * after_last[c] + q_side * _value(side.conc_mass_comp[c])) / (q_up + q_side)
                          for c in components}

    def put(block, component, value):
        var = block.conc_mass_comp[component]
        if not var.fixed:
            var.set_value(max(float(value), 0.0), skip_validation=True)

    touched = 0
    downstream = [block for block in blocks if id(block) not in reactor_outlets and id(block) not in inlets
                  and id(block) not in mixed]
    for block in downstream:
        old = before[id(block)]
        reference = max((c for c in components if c.startswith("X_") and c not in POOLS and before_last[c] > 0.0),
                        key=lambda c: old[c])
        for c in components:
            ratio = old[c] / before_last[c] if before_last[c] > 0.0 else old[reference] / before_last[reference]
            put(block, c, after_last[c] * ratio)
        touched += 1
    for block in blocks:
        upstream = inlets.get(id(block))
        if upstream is not None:
            for c in components:
                put(block, c, _value(upstream.conc_mass_comp[c]))
            touched += 1
    for targets, upstream, side in side_mixes:
        q_up, q_side = _value(upstream.flow_vol), _value(side.flow_vol)
        for block in targets:
            for c in components:
                put(block, c, (q_up * _value(upstream.conc_mass_comp[c]) + q_side * _value(side.conc_mass_comp[c]))
                    / (q_up + q_side))
            touched += 1
    feed, returned = fs.MX1.feed_water_state[0.0], fs.MX1.recycle_state[0.0]
    q_feed, q_return = _value(feed.flow_vol), _value(returned.flow_vol)
    for block in (fs.MX1.mixed_state[0.0], reactors[0].control_volume.properties_in[0.0]):
        for c in components:
            put(block, c, (q_feed * _value(feed.conc_mass_comp[c]) + q_return * _value(returned.conc_mass_comp[c]))
                / (q_feed + q_return))
        touched += 1
    for unit in reactors:
        reactions = unit.control_volume.reactions[0.0]
        for reaction in reactions.rate_expression:
            calculate_variable_from_constraint(reactions.reaction_rate[reaction], reactions.rate_expression[reaction])
        for index, row in unit.cstr_performance_eqn.items():
            calculate_variable_from_constraint(unit.control_volume.rate_reaction_extent[index], row)
        for index, row in unit.control_volume.rate_reaction_stoichiometry_constraint.items():
            if not unit.control_volume.rate_reaction_generation[index].fixed:
                calculate_variable_from_constraint(unit.control_volume.rate_reaction_generation[index], row)
        injection = getattr(unit, "injection", None)
        for index, var in (injection.items() if injection is not None else ()):
            if not var.fixed:    # the DO set point's oxygen supply follows the state's demand
                calculate_variable_from_constraint(var, unit.control_volume.material_balances[index])
    return touched + len(reactors)


def seeded_start(model: Any, reactors: Sequence[Any], seed: Dict[str, Any]):
    """The trajectory's dynamics and start: the pinned solution's state with each reactor's pools at ``seed`` (set on
    the reactor outlets first: the pool rate rows read 0/0 at the pin)."""
    for unit in reactors:
        state = unit.control_volume.properties_out[0.0]
        for pool, value in seed["seed_kg_m3"][unit.local_name].items():
            state.conc_mass_comp[pool].set_value(value, skip_validation=True)
    dynamics = TrainDynamics(model, reactors)
    return dynamics, dynamics.initial.copy()


def _balanced_growth(model: Any, reactors: Sequence[Any], pool_reactions: Sequence[str]) -> Dict[str, Any]:
    """The invasion test's pool machinery read at the solved pools (the rest of the state as solved); the pools are
    restored to their solved values either way."""
    import numpy as np

    saved = [[_value(unit.control_volume.reactions[0.0].conc_mass_comp_ref[pool]) for pool in POOLS]
             for unit in reactors]
    try:
        rates = [_ReactorRates(unit, model.fs.rxn, pool_reactions) for unit in reactors]
        transport = _transport_matrix(_train(model, reactors))
        dynamics = _Dynamics(rates, transport)
        v = np.array([x for row in saved for x in row])
        f = dynamics.f(v)
        transported = transport @ v
        scale = float(max(np.max(np.abs(transported)), np.max(np.abs(f - transported))))
        jac = dynamics.jacobian(v)
        euler = float(np.max(np.abs(jac @ v - f)))
        eigenvalues = np.linalg.eigvals(jac)
    finally:
        for unit, row in zip(reactors, saved):
            for pool, value in zip(POOLS, row):
                unit.control_volume.reactions[0.0].conc_mass_comp_ref[pool].set_value(value, skip_validation=True)
    total = float(v.sum())
    return {
        "method": ("pao_invasion's pool balances F(v) (transport + N^T r of " + ", ".join(pool_reactions)
                   + ") at the solved pools, every other state as solved"),
        "relative_residual": float(np.max(np.abs(f))) / scale if scale > 0 else None,
        "rate_per_d": float(f.sum()) / total if total > 0 else None,
        "euler_relative_residual": euler / scale if scale > 0 else None,
        "pool_block_dominant_eigenvalue_per_d": float(max(eigenvalues.real)),
        "tolerance": BALANCED_GROWTH_RELATIVE_TOLERANCE,
        "balanced": bool(scale > 0 and float(np.max(np.abs(f))) <= BALANCED_GROWTH_RELATIVE_TOLERANCE * scale),
    }


def dominant_mode(jacobian, names: Sequence[str]) -> Dict[str, Any]:
    """The dominant eigenvalue of ``jacobian`` (1/d), its resolution (EIGENVALUE_RELATIVE_RESOLUTION x the infinity
    norm), the verdict (``stable`` below minus the resolution, ``unstable`` above it, else ``undecided``), the leading
    eigenvalues and the states ``names`` the dominant mode moves most."""
    import numpy as np

    eigenvalues, vectors = np.linalg.eig(jacobian)
    order = np.argsort(-eigenvalues.real)
    dominant = eigenvalues[order[0]]
    resolution = EIGENVALUE_RELATIVE_RESOLUTION * float(np.max(np.sum(np.abs(jacobian), axis=1)))
    vector = np.abs(vectors[:, order[0]])
    return {
        "dominant_eigenvalue_per_d": float(dominant.real),
        "dominant_eigenvalue_imag_per_d": float(dominant.imag),
        "leading_eigenvalues_per_d": [[float(eigenvalues[i].real), float(eigenvalues[i].imag)] for i in order[:6]],
        "dominant_mode_states": [[names[i], float(vector[i])] for i in np.argsort(-vector)[:5]],
        "resolution_per_d": resolution,
        "verdict": ("stable" if dominant.real < -resolution else "unstable" if dominant.real > resolution
                    else "undecided"),
    }


def ebpr_stability(model: Any, reactors: Sequence[Any], *, pool_reactions: Sequence[str]) -> Dict[str, Any]:
    """The stability record of the solved (unpinned) state: the explicit train dynamics checked against the solved
    balances, the dominant eigenvalue of their plain Jacobian with its resolution and verdict, and the balanced-growth
    read. Raises `EbprStabilityError` naming the check with no answer (the dynamics do not hold at the solved state)."""
    import numpy as np

    dynamics = TrainDynamics(model, reactors)
    relative, value = dynamics.relative_residual(dynamics.initial)
    worst = int(np.argmax(relative))
    if relative[worst] > DYNAMICS_RELATIVE_TOLERANCE:
        raise EbprStabilityError(f"the explicit train dynamics do not hold at the solved state: "
                                 f"{dynamics.states[worst]} dc/dt {float(value[worst])!r} kg/m3/d (relative "
                                 f"{float(relative[worst])!r} > {DYNAMICS_RELATIVE_TOLERANCE})")
    return {
        "ruling": EBPR_ROUTE_RULING_ID,
        "method": ("plain Jacobian of the reactor-train dynamics dc/dt over every free outlet concentration of every "
                   "reactor (CSTR transport, membrane split, RAS split, N^T r from every installed rate row read "
                   "implicitly), at the solved state; dominant eigenvalue"),
        "states": len(dynamics.states),
        "dynamics_relative_residual": float(relative[worst]),
        **dominant_mode(dynamics.jacobian(dynamics.initial),
                        [f"{unit}.{component}" for unit, component in dynamics.states]),
        "balanced_growth": _balanced_growth(model, reactors, pool_reactions),
        "pools_kg_m3": {unit.local_name: {pool: _value(unit.control_volume.properties_out[0.0].conc_mass_comp[pool])
                                          for pool in POOLS} for unit in reactors},
    }
