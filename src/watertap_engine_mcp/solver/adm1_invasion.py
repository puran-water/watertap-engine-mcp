"""Guild invasion test and branch reseed for ADM1 / mADM1 digester ratings.

The steady-state ADM1 digester has a family of trivial roots: any biomass guild
X_i (X_su, X_aa, X_fa, X_c4, X_pro, X_ac, X_h2) may sit at X_i ~ 0 while its
substrate passes through. The feed carries the guilds only at a 1e-9 kg COD/m3
trace, so the guild balance Q (X_in - X) + V r_i(X) = 0 is met by X ~ 0 to well
inside IPOPT's tolerance whatever the growth rate, and IPOPT reports a strict
optimum on whichever root its warm start reaches. Which root that is depends on
numerically irrelevant detail: at 15 d HRT on the default feed the same intensive
UASB lands on four different partial-washout roots at 259.2, 1000 and 2000 m3/d
(engine-adm1 evidence, 2026-09-29), including acetoclastic washout at 2000 m3/d
with a hydrogenotroph-held 60 % CH4 headspace.

A washed-out guild is physical only if the washout is STABLE: a trace population
introduced at the solved state must die out. This module answers that from the
solved model itself, never from typed constants, exactly as the ASM2d nitrifier
test does (:mod:`solver.nitrifier_invasion`, whose implicit-derivative kernel it
reuses):

* each guild's net specific growth rate in each digester is the linearisation of
  the INSTALLED mADM1 rate expressions,
  g_i = sum_r nu(r, X_i) * d rate_r / d X_i  (uptake yields minus decay);
* the guild's loss rate L is the reactor's particulate turnover, read off the
  solved flows by walking the reactor's liquid outlet through the flowsheet's
  separators and mixers to its exits: L = (particulate mass flow leaving through
  the exits) / (reactor particulate inventory). Every mADM1 particulate shares
  one split in these separators, so L is the same for any particulate and equals
  V/Q for a UASB, the retained-solids turnover for EGSB/AnMBR/contact, and each
  stage's own V/Q for the two-stage cascade (a lower-triangular train, whose
  eigenvalues are the per-stage rates);
* the invasion rate is lambda_i = g_i - L. lambda_i > 0 at X_i ~ 0 means the
  washout is an unstable fixed point, not the physical digester.

The reseed moves the warm start to the neighbourhood of the viable branch, from
the solved model's own expressions: the guild's substrate is set to the level at
which its linearised balance is neutral (bisection on the installed rate
expression), and X_i is seeded from a steady-state balance on the substrate the
washout state discharges, X_i = Y_i * D * (S_w - S*) / (L + k_dec,i), with D the
liquid exit rate. Only initial values move: no geometry, kinetic parameter, feed
or design criterion is changed, and the re-solve is the same square rating.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from watertap_engine_mcp.solver.nitrifier_invasion import _implicit_rate_derivative

SECONDS_PER_DAY = 86400.0
TIME = 0.0

# The mADM1 active biomass guilds whose trivial X_i ~ 0 root the test examines.
ADM1_GUILDS = ("X_su", "X_aa", "X_fa", "X_c4", "X_pro", "X_ac", "X_h2")
# Organic soluble substrates a guild can be seeded against (inorganic nutrients
# S_IC / S_IN / S_IP and the strong ions are consumed by uptake too but are not
# the growth-limiting substrate the reseed moves).
ADM1_ORGANIC_SUBSTRATES = frozenset(
    {"S_su", "S_aa", "S_fa", "S_va", "S_bu", "S_pro", "S_ac", "S_h2"}
)
# A guild is examined only when it sits below this level (kg COD/m3). Single-sourced
# with the A4 oracle's washout floor in templates.adm1.
from watertap_engine_mcp.templates.adm1 import ADM1_ACTIVE_BIOMASS_FLOOR_KG_M3 as GUILD_WASHOUT_FLOOR_KG_M3  # noqa: E402

# Invasion-rate threshold (1/d). At a genuine steady state with X_i > 0 the guild
# balance gives lambda_i = -(inflow of X_i)/(V X_i) <= 0 exactly; a spurious trivial
# root on the default feed sits at +0.1 to +0.3 1/d. The threshold only keeps
# solver noise at a live guild's lambda ~ 0 from triggering a reseed.
INVASION_RATE_TOLERANCE_PER_D = 1e-4
# Reseed/re-solve rounds. Reseeding one guild moves its products (e.g. X_su makes
# acetate for X_ac), so a second round may expose another invadable washout.
MAX_RESEED_ROUNDS = 4

_PASS_THROUGH_TYPES = frozenset({"Mixer", "Separator"})


def _value(expr: Any) -> float:
    import pyomo.environ as pyo

    return float(pyo.value(expr))


def _reaction_block(unit: Any) -> Any:
    return unit.liquid_phase.reactions[TIME]


def _outlet(unit: Any) -> Any:
    return unit.liquid_phase.properties_out[TIME]


def _stoichiometry(reaction_block: Any) -> Mapping[Tuple[str, str, str], Any]:
    return reaction_block.params.rate_reaction_stoichiometry


def _coefficient(stoichiometry: Mapping[Any, Any], reaction: Any, component: str) -> float:
    raw = stoichiometry.get((reaction, "Liq", component), 0)
    return float(raw) if isinstance(raw, (int, float)) else _value(raw)


def guild_growth(unit: Any, guild: str) -> Dict[str, Any]:
    """Linearised growth, decay and substrate map of one guild in one digester (1/d)."""

    reaction_block = _reaction_block(unit)
    variable = reaction_block.conc_mass_comp_ref[guild]
    stoichiometry = _stoichiometry(reaction_block)
    growth = 0.0
    decay = 0.0
    yields: List[float] = []
    substrates: List[str] = []
    contributions: Dict[str, float] = {}
    for reaction in reaction_block.params.rate_reaction_idx:
        nu = _coefficient(stoichiometry, reaction, guild)
        if nu == 0.0:
            continue
        derivative = _implicit_rate_derivative(reaction_block, reaction, variable)
        term = nu * derivative * SECONDS_PER_DAY
        contributions[str(reaction)] = term
        if nu > 0.0:
            growth += term
            yields.append(nu)
            for component in ADM1_ORGANIC_SUBSTRATES:
                if _coefficient(stoichiometry, reaction, component) < 0.0 and component not in substrates:
                    substrates.append(component)
        else:
            decay -= term
    return {
        "growth_per_d": growth,
        "decay_per_d": decay,
        "net_specific_growth_per_d": growth - decay,
        "yield": max(yields) if yields else None,
        "substrates": sorted(substrates),
        "reaction_contributions_per_d": contributions,
    }


def _port_state(port: Any) -> Tuple[float, Dict[str, float]]:
    flow = _value(port.flow_vol[TIME])
    conc = {}
    for index in port.conc_mass_comp:
        component = index[-1] if isinstance(index, tuple) else index
        conc[str(component)] = _value(port.conc_mass_comp[index])
    return flow, conc


def reactor_loop(
    reactor_id: str,
    connections: Sequence[Mapping[str, Any]],
    unit_types: Mapping[str, str],
) -> Dict[str, Any]:
    """Walk a digester's liquid outlet to its exits through separators and mixers.

    Returns the exit arcs (a Product inlet or another digester's inlet), the
    pass-through units inside the reactor's recycle loop with the port they were
    reached by, and whether any path returns to the reactor (a recycle).
    """

    frontier: List[Tuple[str, Optional[str]]] = [(reactor_id, "liquid_outlet")]
    visited: set = set()
    exits: List[Tuple[str, str]] = []
    loop_units: List[Tuple[str, str]] = []
    recycles = False
    while frontier:
        unit_id, port = frontier.pop()
        for conn in connections:
            if conn["src_unit"] != unit_id:
                continue
            if port is not None and conn["src_port"] != port:
                continue
            dest, dest_port = conn["dest_unit"], conn["dest_port"]
            if dest == reactor_id:
                recycles = True
                continue
            if unit_types.get(dest) not in _PASS_THROUGH_TYPES:
                exits.append((dest, dest_port))
                continue
            loop_units.append((dest, dest_port))
            if dest not in visited:
                visited.add(dest)
                frontier.append((dest, None))
    return {"exits": exits, "loop_units": loop_units, "recycles": recycles}


def reactor_turnover(
    reactor_id: str,
    unit: Any,
    units: Mapping[str, Any],
    loop: Mapping[str, Any],
    particulates: Sequence[str],
) -> Dict[str, float]:
    """Particulate turnover L and liquid exit rate D (both 1/d) off the solved flows."""

    volume = _value(unit.volume_liquid[TIME])
    reactor_conc = _outlet(unit).conc_mass_comp
    inventory = volume * sum(_value(reactor_conc[c]) for c in particulates)
    particulate_out = 0.0
    liquid_out = 0.0
    for dest, dest_port in loop["exits"]:
        flow, conc = _port_state(getattr(units[dest], dest_port))
        liquid_out += flow
        particulate_out += flow * sum(conc.get(c, 0.0) for c in particulates)
    if inventory <= 0.0 or volume <= 0.0:
        raise ValueError(f"{reactor_id}: no particulate inventory to define a turnover")
    return {
        "particulate_turnover_per_d": particulate_out / inventory * SECONDS_PER_DAY,
        "liquid_exit_rate_per_d": liquid_out / volume * SECONDS_PER_DAY,
        "particulate_retention_time_d": inventory / particulate_out / SECONDS_PER_DAY
        if particulate_out > 0.0
        else None,
        "volume_liquid_m3": volume,
    }


def digester_invasion(
    reactors: Sequence[Tuple[str, Any]],
    units: Mapping[str, Any],
    connections: Sequence[Mapping[str, Any]],
    unit_types: Mapping[str, str],
    particulates: Sequence[str],
) -> Dict[str, Any]:
    """Every guild's invasion rate in every digester at the solved state."""

    rows: List[Dict[str, Any]] = []
    for reactor_id, unit in reactors:
        loop = reactor_loop(reactor_id, connections, unit_types)
        turnover = reactor_turnover(reactor_id, unit, units, loop, particulates)
        conc = _outlet(unit).conc_mass_comp
        guilds: Dict[str, Any] = {}
        for guild in ADM1_GUILDS:
            if guild not in conc:
                continue
            growth = guild_growth(unit, guild)
            level = _value(conc[guild])
            invasion = growth["net_specific_growth_per_d"] - turnover["particulate_turnover_per_d"]
            guilds[guild] = {
                "X_kg_cod_m3": level,
                "substrates": growth["substrates"],
                "substrate_kg_cod_m3": {s: _value(conc[s]) for s in growth["substrates"]},
                "net_specific_growth_per_d": growth["net_specific_growth_per_d"],
                "decay_per_d": growth["decay_per_d"],
                "yield": growth["yield"],
                "invasion_rate_per_d": invasion,
                "washed_out": level < GUILD_WASHOUT_FLOOR_KG_M3,
                "spurious_washout": level < GUILD_WASHOUT_FLOOR_KG_M3
                and invasion > INVASION_RATE_TOLERANCE_PER_D,
            }
        rows.append(
            {
                "reactor": reactor_id,
                "recycle_loop": loop["recycles"],
                "exits": [f"{d}.{p}" for d, p in loop["exits"]],
                **turnover,
                "guilds": guilds,
            }
        )
    spurious = [
        f"{row['reactor']}.{guild}"
        for row in rows
        for guild, entry in row["guilds"].items()
        if entry["spurious_washout"]
    ]
    return {
        "method": (
            "lambda_i = sum_r nu(r,X_i) d rate_r/d X_i (installed mADM1 rate expressions) "
            "minus the reactor particulate turnover L = exit particulate flow / reactor "
            "particulate inventory; lambda_i > 0 at X_i below the washout floor is an "
            "unstable (spurious) washout root"
        ),
        "washout_floor_kg_cod_m3": GUILD_WASHOUT_FLOOR_KG_M3,
        "invasion_rate_tolerance_per_d": INVASION_RATE_TOLERANCE_PER_D,
        "reactors": rows,
        "spurious_washout_guilds": spurious,
    }


def _state_blocks(block: Any) -> List[Any]:
    """Every state-block data object under ``block`` carrying conc_mass_comp."""

    import pyomo.environ as pyo

    found: List[Any] = []
    seen: set = set()
    for var in block.component_data_objects(pyo.Var, descend_into=True):
        if var.parent_component().local_name != "conc_mass_comp":
            continue
        parent = var.parent_block()
        if id(parent) in seen:
            continue
        seen.add(id(parent))
        found.append(parent)
    return found


def _loop_blocks(reactor_id: str, unit: Any, units: Mapping[str, Any], loop: Mapping[str, Any],
                 unit_types: Mapping[str, str]) -> List[Any]:
    """Liquid state blocks carrying the reactor's own contents (never the fresh feed)."""

    blocks: List[Any] = [_outlet(unit)]
    if loop["recycles"]:
        blocks.append(unit.liquid_phase.properties_in[TIME])
    for unit_id, reached_by in loop["loop_units"]:
        member = units[unit_id]
        for block in _state_blocks(member):
            name = block.parent_component().local_name
            if unit_types.get(unit_id) == "Mixer" and name != "mixed_state" and name != f"{reached_by}_state":
                continue  # a mixer inlet from outside the loop is a boundary, never reseeded
            blocks.append(block)
    for dest, _port in loop["exits"]:
        member = units[dest]
        if unit_types.get(dest) == "AD":
            blocks.append(member.liquid_phase.properties_in[TIME])
        else:
            blocks.extend(_state_blocks(member))
    unique: List[Any] = []
    seen: set = set()
    for block in blocks:
        if id(block) not in seen:
            seen.add(id(block))
            unique.append(block)
    return unique


def _neutral_fraction(unit: Any, guild: str, substrates: Sequence[str], turnover: float) -> float:
    """Fraction f of the washout substrate level at which lambda_guild = 0 (bisection)."""

    conc = _outlet(unit).conc_mass_comp
    saved = {s: _value(conc[s]) for s in substrates}

    def invasion_at(fraction: float) -> float:
        for s in substrates:
            conc[s].set_value(saved[s] * fraction, skip_validation=True)
        return guild_growth(unit, guild)["net_specific_growth_per_d"] - turnover

    try:
        if invasion_at(1.0) <= 0.0:
            raise ValueError(f"{guild}: not invasive at the solved substrate level")
        low, high = 0.0, 1.0
        for _ in range(60):
            middle = 0.5 * (low + high)
            if invasion_at(middle) > 0.0:
                high = middle
            else:
                low = middle
        return high
    finally:
        for s in substrates:
            conc[s].set_value(saved[s], skip_validation=True)


def _recompute_reaction_rates(unit: Any) -> None:
    from pyomo.util.calc_var_value import calculate_variable_from_constraint

    reaction_block = _reaction_block(unit)
    for reaction in reaction_block.rate_expression:
        try:
            calculate_variable_from_constraint(
                reaction_block.reaction_rate[reaction], reaction_block.rate_expression[reaction]
            )
        except Exception:
            continue  # an unresolvable guess is left for the solver, never forced


def reseed_viable_branch(
    invasion: Mapping[str, Any],
    reactors: Sequence[Tuple[str, Any]],
    units: Mapping[str, Any],
    connections: Sequence[Mapping[str, Any]],
    unit_types: Mapping[str, str],
    particulates: Sequence[str],
) -> List[Dict[str, Any]]:
    """Move every spurious-washout guild's warm start to its viable branch."""

    by_id = dict(reactors)
    seeds: List[Dict[str, Any]] = []
    for row in invasion["reactors"]:
        reactor_id = row["reactor"]
        unit = by_id[reactor_id]
        loop = reactor_loop(reactor_id, connections, unit_types)
        blocks = _loop_blocks(reactor_id, unit, units, loop, unit_types)
        reactor_conc = _outlet(unit).conc_mass_comp
        reactor_solids = sum(_value(reactor_conc[c]) for c in particulates)
        turnover = row["particulate_turnover_per_d"]
        exit_rate = row["liquid_exit_rate_per_d"]
        for guild, entry in row["guilds"].items():
            if not entry["spurious_washout"]:
                continue
            substrates = entry["substrates"]
            if not substrates or not entry["yield"]:
                seeds.append({"reactor": reactor_id, "guild": guild, "skipped": "no organic substrate"})
                continue
            washout_substrate = sum(entry["substrate_kg_cod_m3"].values())
            fraction = _neutral_fraction(unit, guild, substrates, turnover)
            removed = washout_substrate * (1.0 - fraction)
            seed = entry["yield"] * exit_rate * removed / (turnover + entry["decay_per_d"])
            if not (math.isfinite(seed) and seed > 0.0):
                seeds.append({"reactor": reactor_id, "guild": guild, "skipped": f"non-positive seed {seed!r}"})
                continue
            touched = 0
            for block in blocks:
                conc = block.conc_mass_comp
                if conc[guild].fixed:
                    continue
                solids = sum(_value(conc[c]) for c in particulates)
                share = solids / reactor_solids if reactor_solids > 0.0 else 1.0
                conc[guild].set_value(seed * share, skip_validation=True)
                for s in substrates:
                    if not conc[s].fixed:
                        conc[s].set_value(_value(conc[s]) * fraction, skip_validation=True)
                touched += 1
            seeds.append(
                {
                    "reactor": reactor_id,
                    "guild": guild,
                    "substrates": substrates,
                    "washout_substrate_kg_cod_m3": washout_substrate,
                    "neutral_substrate_fraction": fraction,
                    "neutral_substrate_kg_cod_m3": washout_substrate * fraction,
                    "seed_kg_cod_m3": seed,
                    "seed_basis": "Y * D_exit * (S_washout - S_neutral) / (L + k_dec), solids-proportional across the loop",
                    "particulate_turnover_per_d": turnover,
                    "liquid_exit_rate_per_d": exit_rate,
                    "state_blocks_reseeded": touched,
                }
            )
        _recompute_reaction_rates(unit)
    return seeds


def _brief(invasion: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "spurious_washout_guilds": list(invasion["spurious_washout_guilds"]),
        "reactors": [
            {
                "reactor": row["reactor"],
                "particulate_turnover_per_d": row["particulate_turnover_per_d"],
                "guilds": {
                    guild: {
                        "X_kg_cod_m3": entry["X_kg_cod_m3"],
                        "invasion_rate_per_d": entry["invasion_rate_per_d"],
                        "substrate_kg_cod_m3": entry["substrate_kg_cod_m3"],
                    }
                    for guild, entry in row["guilds"].items()
                },
            }
            for row in invasion["reactors"]
        ],
    }


def select_viable_digester_branch(
    model: Any,
    reactors: Sequence[Tuple[str, Any]],
    units: Mapping[str, Any],
    connections: Sequence[Mapping[str, Any]],
    unit_types: Mapping[str, str],
    particulates: Sequence[str],
    *,
    resolve: Callable[[], Any],
    is_optimal: Callable[[Any], bool],
    max_rounds: int = MAX_RESEED_ROUNDS,
    initial_point_optimal: bool = True,
) -> Tuple[Any, Dict[str, Any]]:
    """Reseed guilds and require an optimal resolve of the unchanged model.

    Returns ``(new_results, record)``. ``new_results`` is None when the model still
    holds the original solution: no guild is washed out where it could invade (the
    physical answer), or the invasion test itself failed, or the first reseed did
    not re-solve (the original point is restored). After a successful reseed the
    model holds the latest OPTIMAL point; a later round that fails restores it.

    With ``initial_point_optimal=False``, the caller supplies a finite nonoptimal
    cascade warm start. The initial growth calculation is only a seeding heuristic:
    it does not establish washout stability. None then means NO qualified solution
    was obtained; the unqualified original point is restored on first-round failure.
    """

    import pyomo.environ as pyo

    # A cascade can stop at an infeasible bound instead of a washout root:
    # upstream biomass enters the downstream reactor, so X=0 cannot satisfy
    # its material balance. Growth at that point is only a seed heuristic,
    # never a biological stability finding or an accepted solution.
    record: Dict[str, Any] = {
        "rounds": [],
        "initial_point_optimal": initial_point_optimal,
        "initial_growth_assessment": (
            "steady_state_invasion" if initial_point_optimal else "nonoptimal_warm_start_only"
        ),
    }
    try:
        invasion = digester_invasion(reactors, units, connections, unit_types, particulates)
    except Exception as exc:
        record["verdict"] = "invasion_test_failed"
        record["error"] = f"{type(exc).__name__}: {exc}"
        return None, record
    record["method"] = invasion["method"]
    record["first_solve" if initial_point_optimal else "initial_warm_start"] = invasion
    if not invasion["spurious_washout_guilds"]:
        record["verdict"] = "no_invadable_washout"
        return None, record

    def snapshot() -> List[Tuple[Any, Any]]:
        return [
            (var, var.value)
            for var in model.component_data_objects(pyo.Var, descend_into=True)
            if not var.fixed
        ]

    def restore(point: Iterable[Tuple[Any, Any]]) -> None:
        for var, number in point:
            var.set_value(number, skip_validation=True)

    last_point = snapshot()
    latest_results = None
    for round_index in range(1, max_rounds + 1):
        entry: Dict[str, Any] = {"round": round_index, "reseeded": invasion["spurious_washout_guilds"]}
        record["rounds"].append(entry)
        try:
            entry["seeds"] = reseed_viable_branch(
                invasion, reactors, units, connections, unit_types, particulates
            )
            results = resolve()
            entry["termination_condition"] = str(results.solver.termination_condition)
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            results = None
        if results is None or not is_optimal(results):
            restore(last_point)
            record["verdict"] = (
                "reseed_failed_restored_last_optimal"
                if initial_point_optimal or latest_results is not None
                else "reseed_failed_restored_nonoptimal_warm_start"
            )
            record["final"] = _brief(invasion)
            return latest_results, record
        latest_results = results
        last_point = snapshot()
        try:
            invasion = digester_invasion(reactors, units, connections, unit_types, particulates)
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            record["verdict"] = "invasion_test_failed_after_reseed"
            return latest_results, record
        entry["after"] = _brief(invasion)
        if not invasion["spurious_washout_guilds"]:
            record["verdict"] = "reseeded_viable_branch"
            record["final"] = _brief(invasion)
            return latest_results, record
    record["verdict"] = "invadable_washout_remains"
    record["final"] = _brief(invasion)
    return latest_results, record
