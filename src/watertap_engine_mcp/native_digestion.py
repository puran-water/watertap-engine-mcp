"""Native WaterTAP modified-ADM1 digester answering ONE walk node (a completely mixed CSTR, SRT = HRT).

The walk carries the ModifiedASM2d superset; WaterTAP's own BSM2-P interface answers the digester:

    ASM2d feed --[Translator_ASM2d_ADM1]--> mADM1 --[AD (adm1_uasb preset: single CSTR + headspace)]-->
    digestate (mADM1) --[Translator_ADM1_ASM2D]--> ASM2d digestate, and the vapour outlet as the biogas.

The three pieces are solved in series (the chain has no recycle, so the series solution IS the flowsheet solution):

1. ``translate_asm2d_to_adm1``: the Flores-Alsina interface as WaterTAP implements it, on a square translator
   block. Two things are the caller's, never defaulted here, and both are recorded:

   * the STRONG IONS. WaterTAP's translator sets ADM1 anions = S_IN/14 and cations = S_IC/12 (a BSM2 convention
     its own source marks "not clear how Flores-Alsina handles ions"). A real feed carries Na, Ca, Cl, SO4, so the
     caller supplies ``cations_kmol_m3`` / ``anions_kmol_m3`` as charge equivalents of the walk's carried ions and
     those two convention constraints are deactivated. The house reaction subclass counts dissolved K and 2 Mg
     explicitly; the cation lump carries only Na and 2 Ca. Sodium also feeds its separate inhibition term.
   * the ELEMENT CONTENTS of matter the translator passes through unbooked (``adm1_fixes``): the inert N and P
     (X_I/S_I COD crosses without re-booking its N/P) and the lipid P, applied alike to all three blocks.
   * the smooth-Heaviside sharpness ``heaviside_k``. WaterTAP's 20 is sized for BSM2's ~kg/m3 digester feeds; on a
     167 kg COD/m3 food-waste liquor exp(-20 * -40) overflows. It is a NUMERICAL CONDITIONING choice: the value is
     declared with its source and the answer's sensitivity to it is the caller's evidence (swept, never chosen).

   The interface is element-conserving only if the ASM2d carbon (and P) contents cover the ADM1 biochemistry it
   assigns; otherwise the translator needs S_IC (or S_IP) < 0 and IPOPT reports infeasible. That is refused here
   BY NAME with the deficit, never absorbed.

2. ``solve_digester``: the server's own adm1_uasb preset (a single mADM1 CSTR with a biogas headspace -- the
   completely mixed digester), built through ``apply_template`` / ``ModelBuilder`` and solved by the server's
   ``HygienePipeline`` at the STAGE-(b) rated geometry (the rating geometry contract refuses without its sizing
   reference). The steady-state ADM1 is multi-branch: every trophic group X_j = 0 satisfies its own balance, so a
   cold solve can land on a washout root that is UNSTABLE (the group would invade) -- Terminus 2026-09-29 landed
   on X_fa ~ 1e-18 with 28.5 kg COD/m3 LCFA and a 'sour' pH 6.2. The hygiene is CONTINUATION in feed strength:
   the whole feed (organics, nutrients, strong ions) is diluted by lambda0, solved from the server's healthy seed,
   and walked to lambda = 1 by warm-started solves; every step is recorded. A branch that folds before lambda = 1
   is the physical overload limit and is reported as such, never forced. Three numerical devices keep the path off
   the roots that are not the design answer, each recorded as an attempt and none present at the answer:
   a feed INOCULUM of every trophic group at lambda0, ramped to exactly zero at lambda = 1 (with biomass in the feed,
   X_j = 0 is not a root); EXCLUSION of a washout root (a lower bound on the washed-out group, solved, released,
   re-solved unbounded); and EXCLUSION of the acidified root (an upper bound under the sour root's own acetate,
   solved, released, re-solved). A device that finds no other root restores the solved state exactly and the basin
   is reported as solved (the adapter refuses a sour or washed-out digester).

3. ``translate_adm1_to_asm2d``: the bound-N/P reverse interface on a square translator block; live biomass
   leaves as X_H-equivalent particulate with an explicit X_S/X_I redistribution closing COD, N and P. On an S/Fe
   digestate the struvite first gives back its share of the mapping's phosphorus draw (``reverse_phosphate_draw``).

Reported beside the answer (screens WaterTAP's mADM1 does not model): free ammonia at the solved pH/T and the
methanogen pH window and sodium inhibition are read off the solved reaction block; potassium/calcium inhibition and CaCO3
precipitation are the engineering-utils adapter's (PHREEQC is not in this venv).
"""
from __future__ import annotations
import math
import time
import uuid
from typing import Any, Dict, List
SINGLE_CSTR_TEMPLATES = frozenset({'adm1_uasb', 'adm1_cstr'})
ADM1_BIOMASS = ('X_su', 'X_aa', 'X_fa', 'X_c4', 'X_pro', 'X_ac', 'X_h2')
SRB_BIOMASS = ('X_hSRB', 'X_aSRB', 'X_pSRB', 'X_c4SRB')
SRB_INOCULUM_SULFATE_KG_S_M3 = 1e-06
SFE_STATES = ('S_SO4', 'S_IS', 'X_hSRB', 'X_aSRB', 'X_pSRB', 'X_c4SRB', 'S_S0', 'S_Fe3', 'S_Fe2', 'X_HFO_H', 'X_HFO_L', 'X_HFO_old', 'X_HFO_HP', 'X_HFO_LP', 'X_HFO_HP_old', 'X_HFO_LP_old', 'X_FeS', 'X_Fe3PO42', 'X_Siderite', 'X_Mackinawite', 'X_struv')
SULFUR_IRON_FEED_STATES = tuple((k for k in SFE_STATES if k not in SRB_BIOMASS))
ORGANIC_SULFUR_STATES = ('X_Sorg',)
WALK_SULFUR_STATES = ('S_SO4', 'S_IS')
WALK_ELEMENTAL_SULFUR_STATE = 'S_S0'
MICROAERATION_STATES = ('S_O2',)
WALK_UNCARRIED_SULFUR_IRON = tuple((k for k in SULFUR_IRON_FEED_STATES if k not in WALK_SULFUR_STATES))
WALK_UNCARRIED_SULFUR_SHARE = 0.0001
WALK_IRON_FEED_STATES = ('X_HFO_H', 'S_Fe2', 'X_FeS', 'X_Fe3PO42', 'X_Siderite')
WALK_IRON_RETURNED_STATES = ('S_Fe2', 'X_FeS', 'X_Mackinawite', 'X_Fe3PO42', 'X_Siderite', 'X_HFO_H', 'X_HFO_L', 'X_HFO_old', 'X_HFO_HP', 'X_HFO_LP', 'X_HFO_HP_old', 'X_HFO_LP_old')
WALK_STRUVITE_STATE = 'X_struv'
HFO_BOUND_P_CONTENT = {'X_HFO_HP': 'iP_HFO_HP', 'X_HFO_HP_old': 'iP_HFO_HP', 'X_HFO_LP': 'iP_HFO_LP', 'X_HFO_LP_old': 'iP_HFO_LP'}
WALK_UNCARRIED_FERRIC_SHARE = 0.0001
SRB_REVERSE_CARRIER = 'X_ac'
DEFAULT_SULFUR_IRON_STEPS = (0.01, 0.02, 0.05, 0.1, 0.2, 0.3, 0.45, 0.6, 0.75, 0.9, 1.0)
SULFUR_IRON_MAX_HALVINGS = 6
STRUVITE_RATE_PARAMETER = 'k_cryst_struvite'
STRUVITE_RATE_SHARES = tuple((10.0 ** (i / 4.0) for i in range(-36, 1)))
HELD_SEED_MINERALS = ('mineral_seed_vivianite', 'mineral_seed_siderite', 'mineral_seed_struvite')
HELD_SEED_KG_M3 = 100.0
MINERAL_WALK_MAX_HALVINGS = 8
ORGANIC_WALK_MAX_HALVINGS = 4
DEFAULT_OXIDANT_DOSE_STEPS = (0.05, 0.15, 0.35, 0.65, 1.0)
OXIDANT_DOSE_MAX_HALVINGS = 6
OXIDANT_POLISH_OPTIONS = {'bound_push': 1e-10, 'mu_init': 1e-09, 'warm_start_init_point': 'yes', 'max_iter': 200, 'tol': 1e-11}
MICROAERATION_BRACKET_PARAMETERS = ('r_A_ref', 'n_h2s', 'K_pO2', 'phi_liq', 'f_SO4', 'kL_O2', 'K_O2_fac', 'KI_O2', 'k_S0_red')
MICROAERATION_BRACKET_MAX_HALVINGS = 5
MICROAERATION_BRACKET_METRICS = ('biogas_h2s_ppmv_dry', 'biogas_h2s_kg_s_d', 'biogas_h2s_removal_fraction', 'residual_o2_fraction_wet', 'liquor_sulfide_kg_s_m3', 'headspace_h2s_oxidised_kg_s_d', 's0_headspace_deposit_kg_s_d', 's0_to_liquid_kg_s_d', 'sulfate_to_liquid_kg_s_d', 'o2_to_liquid_kg_d', 'liquor_do_kg_m3', 's0_rereduction_extent_kg_s_d', 's0_rereduction_extra_o2_kg_d', 's0_rereduction_lost_cod_kg_d', 'pH', 'methane_kg_cod_d', 'headspace_oxidation_heat_w')
WETTED_AREA_FIELD = 'headspace_wetted_area_m2'
DEFAULT_SRB_INOCULUM_KG_COD_M3 = 0.01
WASHOUT_FLOOR_KG_M3 = 1e-06
EXCLUSION_FLOOR_KG_M3 = 0.0001
SOUR_EXCLUSION_ACETATE_KG_COD_M3 = 1.0
SOUR_EXCLUSION_SHARE = 0.25
PH_HOMOTOPY_SHIFTS = (1.0, 0.75, 0.5, 0.25, 0.1, 0.0)
SOUR_EXCLUSION_STRATEGIES = (('cold', (('max_iter', 3000),)), ('interior', (('max_iter', 3000), ('bound_push', 0.01), ('mu_init', 0.1))), ('interior_strong', (('max_iter', 5000), ('bound_push', 0.1), ('mu_init', 1.0), ('mu_strategy', 'adaptive'))))

def _solve(solver, model) -> str:
    """IPOPT's termination condition, or ``error: ...`` when the solve itself raised (an evaluation error loads a
    SolverResults with bad status and Pyomo raises): an exploratory start that errors is a failed attempt, recorded,
    never an exception that loses the solved state the caller can restore."""
    try:
        return str(solver.solve(model, tee=False).solver.termination_condition)
    except (ValueError, RuntimeError, ArithmeticError) as exc:
        return f'error: {exc}'
STAGED_WARM_OPTIONS = {'bound_push': 1e-10, 'mu_init': 1e-09, 'warm_start_init_point': 'yes', 'max_iter': 500}

def _copy_shared_values(core_model, model) -> int:
    """Every unfixed Var of ``model`` that ``core_model`` carries BY NAME, set to the core value; the count copied.
    ``core_model`` is a solved Pyomo model, or a ``{Var name: value}`` mapping of one (`_unfixed_values`): a warm
    start's previous answer. A fixed Var (a parameter, the feed, the dose) is never touched."""
    import pyomo.environ as pyo
    pairs = core_model.items() if isinstance(core_model, dict) else ((var.name, var.value) for var in core_model.component_data_objects(pyo.Var, descend_into=True))
    copied = 0
    for name, value in pairs:
        if value is None:
            continue
        target = model.find_component(name)
        if target is None or target.fixed:
            continue
        target.set_value(value, skip_validation=True)
        copied += 1
    return copied

def _unfixed_values(model) -> Dict[str, float]:
    """``{Var name: value}`` of every unfixed Var of a solved model: what a warm start copies back (`_copy_shared_values`
    skips a fixed Var, so the fixed ones -- the parameters, the feed, the dose -- are never carried)."""
    import pyomo.environ as pyo
    return {var.name: float(var.value) for var in model.component_data_objects(pyo.Var, descend_into=True) if not var.fixed and var.value is not None}
WARM_START_RULING_ID = 'HOUSE-NATIVE-SOLVE-WARM-START-FINAL-BRACKET-20261012'
WARM_STATE_SCHEMA = 'native_digestion_warm_state/v1'

def warm_structure(*, engine_template_name: str, feed_components, sulfur_iron: bool, organic_sulfur: bool, microaeration: bool, volume_liquid_m3: float, volume_vapor_m3: float, headspace_wetted_area_m2) -> Dict[str, Any]:
    """What a previous answer must share with this request for its values to be a warm start: the same preset and
    variant (S/Fe, organic sulfur, micro-aeration), the same feed components, and the same rated geometry."""
    return {'schema': WARM_STATE_SCHEMA, 'engine_template_name': str(engine_template_name), 'variant': {'sulfur_iron': bool(sulfur_iron), 'organic_sulfur': bool(organic_sulfur), 'microaeration': bool(microaeration)}, 'feed_components': sorted(feed_components), 'volume_liquid_m3': float(volume_liquid_m3), 'volume_vapor_m3': float(volume_vapor_m3), 'headspace_wetted_area_m2': None if headspace_wetted_area_m2 is None else float(headspace_wetted_area_m2)}

def _warm_mismatch(warm_start, structure: Dict[str, Any]) -> str | None:
    """Why ``warm_start`` cannot start this request (None when it can)."""
    if not isinstance(warm_start, dict) or not isinstance(warm_start.get('values'), dict) or (not warm_start['values']):
        return 'the warm start carries no solved values'
    previous = warm_start.get('structure')
    if previous != structure:
        differ = sorted((k for k in set(structure) | set(previous or {}) if (previous or {}).get(k) != structure.get(k)))
        return f"the model structure differs from the previous answer's ({', '.join(differ)})"
    return None

def _warm_from_values(pipeline, model, values) -> tuple:
    """A WARM START (`WARM_START_RULING_ID`): the hygiene pipeline's DOF check and scaling on ``model``, every unfixed
    Var then set from the previous answer's ``values``, ONE warm solve (``STAGED_WARM_OPTIONS``) and the same
    post-solve diagnostics the staged starts gate on; ``(ok, message)``.

    The pipeline's INITIALIZATION is run only where the previous answer does not cover every unfixed Var of the model:
    where it does, the copy overwrites every value the initialization would set (Napier 12 TPD, 2026-10-13: 546 of 546
    Vars, and the initialization took 96 of a warm start's 116 s; the warm IPOPT solve 4 s)."""
    import pyomo.environ as pyo
    from watertap.core.solvers import get_solver
    uncovered = [var.name for var in model.component_data_objects(pyo.Var, descend_into=True) if not var.fixed and var.name not in values]
    steps = (pipeline.run_dof_check, pipeline.run_scaling) + ((pipeline.run_initialization,) if uncovered else ())
    for step in steps:
        result = step()
        if not getattr(result, 'success', False):
            return (False, f'warm start: {step.__name__} failed: {getattr(result, 'message', '')}')
    copied = _copy_shared_values(values, model)
    start = f'warm start ({copied} values from the previous answer' + (f'; initialized first: {len(uncovered)} unfixed Vars it does not carry, e.g. {uncovered[:3]}' if uncovered else '; every unfixed Var carried, so no initialization') + ', STAGED_WARM_OPTIONS)'
    condition = _solve(get_solver(options=dict(STAGED_WARM_OPTIONS)), model)
    if condition != 'optimal':
        return (False, f'{start}: {condition}')
    result = pipeline.run_post_solve_diagnostics()
    return (bool(getattr(result, 'success', False)), f'{start}: {getattr(result, 'message', '')}')

def _staged_from_core(pipeline, model, core_model) -> tuple:
    """The hygiene pipeline's own stages on ``model`` (the S/Fe preset), with every unfixed Var it shares BY NAME with
    the solved ``core_model`` set to the core value after initialization and before the solve; ``(ok, message)``
    from the same post-solve diagnostics the full pipeline gates on. The S/Fe-only states keep their initialization
    (the floor). Where the pipeline's solve fails from that point, the core values are copied again and solved warm
    (`STAGED_WARM_OPTIONS`), and the answer is gated on the same post-solve diagnostics; both outcomes are reported."""
    from watertap.core.solvers import get_solver
    for step in (pipeline.run_dof_check, pipeline.run_scaling, pipeline.run_initialization):
        result = step()
        if not getattr(result, 'success', False):
            return (False, f'staged start: {step.__name__} failed: {getattr(result, 'message', '')}')
    copied = _copy_shared_values(core_model, model)
    result = pipeline.run_solve()
    if not getattr(result, 'success', False):
        failed = f'staged start ({copied} values from the core start): solve failed: {getattr(result, 'message', '')}'
        copied = _copy_shared_values(core_model, model)
        condition = _solve(get_solver(options=dict(STAGED_WARM_OPTIONS)), model)
        if condition != 'optimal':
            return (False, f'{failed}; warm re-solve from the core answer: {condition}')
        result = pipeline.run_post_solve_diagnostics()
        return (bool(getattr(result, 'success', False)), f'{failed}; warm re-solve from the core answer ({copied} values, STAGED_WARM_OPTIONS): {getattr(result, 'message', '')}')
    result = pipeline.run_post_solve_diagnostics()
    return (bool(getattr(result, 'success', False)), f'staged start ({copied} values from the core start): {getattr(result, 'message', '')}')

def final_equation_residual(model) -> Dict[str, Any]:
    'Public engineering calculation interface.'
    import pyomo.environ as pyo
    worst, name, count = (0.0, None, 0)
    for constraint in model.component_data_objects(pyo.Constraint, active=True, descend_into=True):
        if not constraint.equality:
            continue
        count += 1
        try:
            residual = abs(float(pyo.value(constraint.body)) - float(pyo.value(constraint.upper)))
        except (ValueError, ZeroDivisionError, TypeError):
            residual = float('inf')
        if residual > worst or name is None:
            worst, name = (residual, constraint.name)
    return {'max_abs': worst, 'constraint': name, 'equalities': count}

def _snapshot(model):
    """Every Var value of a solved model, so a failed exploratory solve can be undone exactly (no re-solve)."""
    import pyomo.environ as pyo
    return [(v, v.value) for v in model.component_data_objects(pyo.Var, descend_into=True)]

def _restore(saved) -> None:
    for var, value in saved:
        var.set_value(value, skip_validation=True)
DEFAULT_CONTINUATION = {'lambda0': 0.1, 'steps': [0.2, 0.35, 0.5, 0.65, 0.8, 0.9, 1.0], 'inoculum_kg_cod_m3': 0.01, 'buffer_share': 0.5, 'source': "[CODE] native_digestion.DEFAULT_CONTINUATION: whole-feed dilution continuation onto the methanogenic branch, with a feed inoculum of every trophic group and half the feed's net strong-cation excess kept at the dilute start, both ramped to exactly zero at lambda = 1 (Terminus 2026-09-29: without the buffer the dilute start lands on the acidified root at pH 6.2 and, on the lipid-rich split, never leaves it)"}

class NativeDigestionRefusal(ValueError):
    """A request the native digester cannot answer honestly; the message names what would close it."""

class DiluteStartRefusal(NativeDigestionRefusal):
    """No start converged at the continuation's dilute start (every preset and buffer share tried)."""
WARM_STATE_MAP_RULE = "a warm state is applied by name; the S/Fe Vars it lacks start at zero and are recorded as filled; any other gap, or a name today's model lacks, refuses"
WARM_STATE_FILL = 0.0

class WarmStateMapRefusal(NativeDigestionRefusal):
    """A warm state that cannot be mapped onto today's model: a non-S/Fe Var it lacks, or a Var the model no longer has."""

def sfe_family(reaction_package) -> frozenset:
    """The index members that put a Var in the S/Fe family: the S/Fe states and the reactions the S/Fe block added."""
    return frozenset(SFE_STATES) | frozenset((str(r) for r in getattr(reaction_package, 'sfe_reaction_idx', None) or ()))

def map_warm_values(model, values: Dict[str, float], family) -> tuple:
    """``values`` (a warm state's ``{Var name: value}``) mapped onto ``model`` under `WARM_STATE_MAP_RULE`:
    ``(mapped values, record or None)``; the record names every filled Var, its value and why."""
    import pyomo.environ as pyo
    present = {var.name: var for var in model.component_data_objects(pyo.Var, descend_into=True)}
    stale = sorted((name for name in values if name not in present))
    gaps = [var for name, var in present.items() if not var.fixed and name not in values]

    def in_family(var) -> bool:
        index = var.index()
        return any((str(member) in family for member in (index if isinstance(index, tuple) else (index,))))
    outside = sorted((var.name for var in gaps if not in_family(var)))
    if stale or outside:
        raise WarmStateMapRefusal("the warm state cannot be mapped onto today's model (" + '; '.join((part for part in (f'it lacks {len(outside)} unfixed Vars outside the S/Fe family: {outside}' if outside else '', f"it names {len(stale)} Vars today's model does not have: {stale}" if stale else '') if part)) + '); only an S/Fe Var it lacks is filled')
    filled = {var.name: WARM_STATE_FILL for var in gaps}
    if not filled:
        return (dict(values), None)
    return ({**values, **filled}, {'rule': WARM_STATE_MAP_RULE, 'carried': len(values), 'filled_count': len(filled), 'filled': filled, 'why': f"the warm state predates these S/Fe Vars (an older model's variable set); each starts at {WARM_STATE_FILL:g}, the ruled start of an S/Fe state a record lacks: a start, never a record value (the solve answers it)"})
DILUTE_START_FALLBACKS = (0.15, 0.2)
WALK_BUFFER_FALLBACK_FRACTION = 0.2

def _solve_with_start_fallbacks(solve, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """``solve(**kwargs)``; where its dilute start refuses, the same solve from each `DILUTE_START_FALLBACKS` start
    (the plan's steps above it). The answer records every start tried; a refusal names them all."""
    try:
        return solve(**kwargs)
    except DiluteStartRefusal as first:
        plan = {**DEFAULT_CONTINUATION, **(kwargs.get('continuation') or {})}
        if plan.get('blend_from') is not None:
            raise
        tried = [{'lambda0': float(plan['lambda0']), 'refusal': str(first)[:800]}]
        for lam0 in DILUTE_START_FALLBACKS:
            if math.isclose(lam0, float(plan['lambda0'])):
                continue
            continuation = {**(kwargs.get('continuation') or {}), 'lambda0': lam0, 'steps': [float(x) for x in plan['steps'] if float(x) > lam0]}
            try:
                answer = solve(**{**kwargs, 'continuation': continuation})
            except DiluteStartRefusal as exc:
                tried.append({'lambda0': lam0, 'refusal': str(exc)[:800]})
                continue
            answer['dilute_start_fallback'] = {'tried': tried, 'solved_from_lambda0': lam0, 'source': "[CODE] native_digestion.DILUTE_START_FALLBACKS: the plan's dilute start refused; the same solve from another dilute start (a numerical path; the answer at lambda = 1 is the real feed's)"}
            return answer
        raise DiluteStartRefusal(f'{first}; and from the fallback starts {[t['lambda0'] for t in tried[1:]]}: {[t['refusal'][-300:] for t in tried[1:]]}')

def _asm2d_blocks():
    import pyomo.environ as pyo
    from idaes.core import FlowsheetBlock
    from watertap.property_models.unit_specific.activated_sludge.modified_asm2d_properties import ModifiedASM2dParameterBlock
    from watertap.property_models.unit_specific.activated_sludge.modified_asm2d_reactions import ModifiedASM2dReactionParameterBlock
    from watertap.property_models.unit_specific.anaerobic_digestion.modified_adm1_properties import ModifiedADM1ParameterBlock
    from watertap_engine_mcp.chemistry.modified_adm1_davies import DaviesADM1ReactionParameterBlock
    m = pyo.ConcreteModel()
    m.fs = FlowsheetBlock(dynamic=False)
    m.fs.pa = ModifiedASM2dParameterBlock()
    m.fs.ra = ModifiedASM2dReactionParameterBlock(property_package=m.fs.pa)
    m.fs.pd = ModifiedADM1ParameterBlock()
    m.fs.rd = DaviesADM1ReactionParameterBlock(property_package=m.fs.pd)
    return m

def _apply_asm2d_fixes(m, fixes: Dict[str, float]) -> Dict[str, Dict[str, Any]]:
    """Fix ModifiedASM2d composition parameters on whichever block declares them (reaction first)."""
    import pyomo.environ as pyo
    applied = {}
    for name, value in (fixes or {}).items():
        target = None
        for block in (m.fs.ra, m.fs.pa):
            candidate = getattr(block, name, None)
            if candidate is not None and hasattr(candidate, 'fix') and (not candidate.is_indexed()):
                target = candidate
                break
        if target is None:
            raise NativeDigestionRefusal(f'unsupported ModifiedASM2d parameter fix {name!r}')
        target.fix(float(value))
        applied[name] = {'value': pyo.value(target), 'unit': str(pyo.units.get_units(target))}
    return applied
ADM1_CONTENT_GROUPS = ('Ni', 'Pi', 'Ci')

def _apply_adm1_fixes(rp, fixes: Dict[str, Any] | None) -> Dict[str, Dict[str, float]]:
    """Fix mADM1 element contents on one reaction parameter block (``{"Ni": {"X_I": v}, "Pi": {...}, ...}``).

    The SAME table is applied to the forward translator, the digester and the reverse translator: the three blocks
    are separate Pyomo objects, and one that kept WaterTAP's default would create or destroy the element at its edge.
    """
    import pyomo.environ as pyo
    applied: Dict[str, Dict[str, float]] = {}
    for group, table in (fixes or {}).items():
        if group == 'source':
            continue
        if group not in ADM1_CONTENT_GROUPS:
            raise NativeDigestionRefusal(f'unsupported mADM1 content group {group!r}; use {ADM1_CONTENT_GROUPS}')
        var = getattr(rp, group)
        for key, value in table.items():
            if key not in var:
                raise NativeDigestionRefusal(f'mADM1 {group} has no entry {key!r}')
            value = float(value)
            if not math.isfinite(value) or value < 0:
                raise NativeDigestionRefusal(f'mADM1 {group}[{key}] must be a finite non-negative content, not {value}')
            if value == 0.0:
                var[key].domain = pyo.NonNegativeReals
            var[key].fix(value)
            applied.setdefault(group, {})[key] = value
    return applied

def interface_element_demand(m, feed_kg_m3: Dict[str, float]) -> Dict[str, float]:
    """Carbon and phosphorus the ASM2d->ADM1 interface assigns beyond what the ASM2d feed carries (kg/m3; > 0 is a
    deficit the translator would take from S_IC / S_IP). Read off the SAME parameter blocks the translator uses, on
    the translator's own bio_P = False mapping (S_F -> S_aa by N then S_su; X_S -> X_pr by N, then 40 % X_ch /
    60 % X_li of the rest; WaterTAP 1.7 translator_asm2d_adm1 Ssu/Saa/Xch/Xpr/Xli_mapping)."""
    import pyomo.environ as pyo
    ra, rd = (m.fs.ra, m.fs.rd)
    v = lambda p: float(pyo.value(p))
    s_f, x_s = (feed_kg_m3.get('S_F', 0.0), feed_kg_m3.get('X_S', 0.0))
    s_aa = min(s_f * v(ra.i_NSF) / (v(rd.Ni['S_aa']) * 14), s_f)
    s_su = max(s_f - s_aa, 0.0)
    x_pr = min(x_s * v(ra.i_NXS) / (v(rd.Ni['X_pr']) * 14), x_s)
    rest = max(x_s - x_pr, 0.0)
    x_ch, x_li = (0.4 * rest, 0.6 * rest)
    carbon_adm1 = 12 * (s_su * v(rd.Ci['S_su']) + s_aa * v(rd.Ci['S_aa']) + x_ch * v(rd.Ci['X_ch']) + x_pr * v(rd.Ci['X_pr']) + x_li * v(rd.Ci['X_li']))
    carbon_asm2d = s_f * v(ra.i_CSF) + x_s * v(ra.i_CXS)
    p_adm1 = 31 * (x_ch * v(rd.P_ch) + x_li * v(rd.Pi['X_li']))
    p_asm2d = s_f * v(ra.i_PSF) + x_s * v(ra.i_PXS)
    return {'carbon_deficit_kg_m3': carbon_adm1 - carbon_asm2d, 'carbon_available_kg_m3': feed_kg_m3.get('S_IC', 0.0), 'phosphorus_deficit_kg_m3': p_adm1 - p_asm2d, 'phosphorus_available_kg_m3': feed_kg_m3.get('S_PO4', 0.0), 'carbon_consistent_i_CSF': 12 * ((s_su * v(rd.Ci['S_su']) + s_aa * v(rd.Ci['S_aa'])) / s_f) if s_f > 0 else None, 'carbon_consistent_i_CXS': 12 * (x_ch * v(rd.Ci['X_ch']) + x_pr * v(rd.Ci['X_pr']) + x_li * v(rd.Ci['X_li'])) / x_s if x_s > 0 else None}
SPLIT_GROUPS = {'soluble_nonprotein': (('S_su',), ('S_su', 'X_li')), 'particulate_nonprotein': (('X_ch', 'X_li'), ('X_ch', 'X_li'))}

def apply_biochemical_split(t1: Dict[str, Any], split: Dict[str, Any], *, walk_fixes: Dict[str, float], t1_fixes: Dict[str, float], asm2d_kg_m3: Dict[str, float], adm1_fixes: Dict[str, Any] | None) -> Dict[str, Any]:
    """Re-partition the translator's non-protein substrate by a DECLARED biochemical split (sourced from the feed
    characterisation), closing C, N and P exactly and reporting every residue.

    WaterTAP's translator maps an ASM2d feed onto mADM1 by fixed rules (non-protein S_F -> monosaccharide, non-protein
    X_S -> 40/60 carbohydrate/lipid): an ASM2d state carries no biochemical class, so a lipid-rich feed arrives as a
    sugar feed and the biogas CO2 share is overstated (Terminus 2026-09-29: 54.7 % CH4 on a feed whose COD is 41.6 %
    lipid). The split moves COD between S_su / X_ch / X_li only: COD is conserved by construction; protein (and so N)
    is untouched; P moves with X_li at its declared content (booked from S_IP, refused if short); carbon: the translator
    ran on its OWN carbon-consistent contents (``t1_fixes``) and the walk counts the split-consistent ones
    (``walk_fixes``), so the split's organic-carbon change must equal the difference between the two -- the residue,
    booked onto S_IC and reported, is that identity's error (zero to round-off when the walk table is derived from the
    same split)."""
    import pyomo.environ as pyo
    m = _asm2d_blocks()
    _apply_adm1_fixes(m.fs.rd, adm1_fixes)
    ci = {k: float(pyo.value(m.fs.rd.Ci[k])) * 12.0 for k in ('S_su', 'X_ch', 'X_li')}
    pi_li = float(pyo.value(m.fs.rd.Pi['X_li'])) * 31.0
    conc = dict(t1['conc_kg_m3'])
    before = {k: conc[k] for k in ('S_su', 'X_ch', 'X_li')}
    moved = {'S_su': 0.0, 'X_ch': 0.0, 'X_li': 0.0}
    for group, (sources, targets) in SPLIT_GROUPS.items():
        shares = split[group]
        if set(shares) != set(targets) or not math.isclose(sum(shares.values()), 1.0, abs_tol=1e-09) or any((v < 0 for v in shares.values())):
            raise NativeDigestionRefusal(f'biochemical_split.{group} must give non-negative shares of {targets} summing to 1')
        pool = sum((before[s] for s in sources))
        for s in sources:
            moved[s] -= before[s]
        for t, share in shares.items():
            moved[t] += share * pool
    after = {k: before[k] + moved[k] for k in before}
    if any((v < -1e-12 for v in after.values())):
        raise NativeDigestionRefusal(f'biochemical_split drove a state negative: {after}')
    carbon_change = sum((moved[k] * ci[k] for k in moved))
    walk_carbon_difference = sum((asm2d_kg_m3.get(state, 0.0) * (walk_fixes[param] - t1_fixes[param]) for state, param in (('S_F', 'i_CSF'), ('X_S', 'i_CXS'))))
    carbon_residue = walk_carbon_difference - carbon_change
    phosphorus_needed = moved['X_li'] * pi_li
    conc.update({k: max(v, 0.0) for k, v in after.items()})
    conc['S_IC'] = conc['S_IC'] + carbon_residue
    conc['S_IP'] = conc['S_IP'] - phosphorus_needed
    roundoff = 1e-09 * max(1.0, sum(before.values()))
    for key in ('S_IC', 'S_IP'):
        if -roundoff < conc[key] < 0:
            conc[key] = 0.0
    if conc['S_IC'] < 0 or conc['S_IP'] < 0:
        raise NativeDigestionRefusal(f'biochemical_split cannot close: S_IC {conc['S_IC']:.6g}, S_IP {conc['S_IP']:.6g} kg/m3 after booking its carbon residue and the lipid P it moves')
    record = {'shares': {g: dict(split[g]) for g in SPLIT_GROUPS}, 'source': split.get('source'), 'before_kg_cod_m3': before, 'after_kg_cod_m3': after, 'cod_change_kg_m3': sum(moved.values()), 'carbon_change_organic_kg_m3': carbon_change, 'walk_minus_translator_carbon_kg_m3': walk_carbon_difference, 'carbon_residue_booked_to_S_IC_kg_m3': carbon_residue, 'phosphorus_booked_from_S_IP_kg_m3': phosphorus_needed, 'nitrogen_change_kg_m3': 0.0, 'translator_working_contents': {k: t1_fixes[k] for k in ('i_CSF', 'i_CXS')}, 'walk_contents': {k: walk_fixes[k] for k in ('i_CSF', 'i_CXS')}}
    return {**t1, 'conc_kg_m3': conc, 'biochemical_split': record}

def _solve_square(block, label: str) -> None:
    import pyomo.environ as pyo
    from idaes.core.util.model_statistics import degrees_of_freedom
    from watertap.core.solvers import get_solver
    dof = degrees_of_freedom(block)
    if dof != 0:
        raise NativeDigestionRefusal(f'{label}: degrees of freedom {dof}, expected a square block')
    result = get_solver().solve(block, tee=False)
    if str(result.solver.termination_condition) != 'optimal':
        raise NativeDigestionRefusal(f'{label}: IPOPT {result.solver.termination_condition}')
    return pyo

def _exact_mappings_on_a_fixed_inlet(translator) -> list[str]:
    """Bind each of the interface's smoothed mappings to its exact value; return their names.

    WaterTAP's ASM2d->ADM1 interface writes its max/min mappings (S_su, S_aa, X_ch, X_li) as idaes smooth_max/smooth_min
    with ``eps_smooth`` (1e-4 kg/m3) so a flowsheet can move the inlet through the kink. Here every inlet concentration
    is FIXED, so each mapping is a number: it is evaluated exactly (the smoothing at eps = 0, where smooth_abs is |a|)
    and bound as that number, and no eps term remains in any outlet. At a kink the smoothing was not a rounding: a
    library feed with no readily biodegradable COD (S_F = 0, Napier 12 TPD, 2026-10-11) gave smooth_min(0, 0) = -eps/2,
    so S_aa = -5e-5 kg/m3 below its bound, and the same eps/2 terms put S_IC about 3e-7 below its bound on a carbon-free
    feed: IPOPT infeasible. An exactly-zero substrate now maps to zero. Away from a kink the exact value differs from
    the smoothed one by at most eps^2/(4|a - b|). An expression that reads any unfixed variable is left as WaterTAP
    wrote it."""
    import pyomo.environ as pyo
    from pyomo.core.expr.visitor import identify_mutable_parameters, identify_variables
    eps = translator.eps_smooth

    def smoothed(data) -> bool:
        params = identify_mutable_parameters(data.expr)
        return any((p is eps or getattr(p, 'parent_component', lambda: None)() is eps for p in params)) and all((v.fixed for v in identify_variables(data.expr)))
    targets = [data for comp in translator.component_objects(pyo.Expression, descend_into=False) for data in comp.values() if smoothed(data)]
    held = pyo.value(eps)
    eps.set_value(0.0)
    try:
        exact = [(data, pyo.value(data), pyo.units.get_units(data)) for data in targets]
    finally:
        eps.set_value(held)
    for data, value, units in exact:
        data.set_value(value * units if units is not None else value)
    return sorted((data.name for data, _value, _units in exact))

def translate_asm2d_to_adm1(*, flow_m3_d: float, temperature_k: float, asm2d_kg_m3: Dict[str, float], cations_kmol_m3: float, anions_kmol_m3: float, asm2d_fixes: Dict[str, float], heaviside_k: float, adm1_fixes: Dict[str, Any] | None=None) -> Dict[str, Any]:
    import pyomo.environ as pyo
    from watertap.unit_models.translators.translator_asm2d_adm1 import Translator_ASM2d_ADM1
    m = _asm2d_blocks()
    applied = _apply_asm2d_fixes(m, asm2d_fixes)
    applied_adm1 = _apply_adm1_fixes(m.fs.rd, adm1_fixes)
    demand = interface_element_demand(m, asm2d_kg_m3)
    tol = 1e-09 * max(1.0, sum(asm2d_kg_m3.values()))
    if demand['carbon_deficit_kg_m3'] - demand['carbon_available_kg_m3'] > tol:
        raise NativeDigestionRefusal("the ASM2d->ADM1 interface assigns {:.6g} kg C/m3 more organic carbon than the feed's ModifiedASM2d carbon contents carry, and the feed's S_IC ({:.6g} kg C/m3) cannot cover it (S_IC would go negative). The feed characterisation must state carbon contents consistent with the biochemistry the interface assigns: i_CSF = {} and i_CXS = {} kg C/kg COD on this N content (walk property_params, sourced), or measure TOC.".format(demand['carbon_deficit_kg_m3'], demand['carbon_available_kg_m3'], demand['carbon_consistent_i_CSF'], demand['carbon_consistent_i_CXS']))
    if demand['phosphorus_deficit_kg_m3'] - demand['phosphorus_available_kg_m3'] > tol:
        raise NativeDigestionRefusal(f"the ASM2d->ADM1 interface assigns {demand['phosphorus_deficit_kg_m3']:.6g} kg P/m3 of carbohydrate/lipid phosphorus the feed's X_S/S_F P content does not carry and its S_PO4 ({demand['phosphorus_available_kg_m3']:.6g}) cannot cover: phosphorus-limited digestion. Dose phosphorus to the digester inlet (a folded inlet reagent) or state the feed's organic P content; never zero-fill")
    m.fs.t1 = Translator_ASM2d_ADM1(inlet_property_package=m.fs.pa, outlet_property_package=m.fs.pd, inlet_reaction_package=m.fs.ra, outlet_reaction_package=m.fs.rd, has_phase_equilibrium=False, outlet_state_defined=True, bio_P=False)
    t = m.fs.t1
    t.heaviside_k = float(heaviside_k)
    t.inlet.flow_vol.fix(flow_m3_d / 86400.0)
    t.inlet.temperature.fix(temperature_k)
    t.inlet.pressure.fix(101325.0)
    for name, value in asm2d_kg_m3.items():
        t.inlet.conc_mass_comp[0, name].fix(max(float(value), 0.0))
    t.return_an.deactivate()
    t.return_cat.deactivate()
    t.properties_out[0].anions.fix(float(anions_kmol_m3))
    t.properties_out[0].cations.fix(float(cations_kmol_m3))
    exact = _exact_mappings_on_a_fixed_inlet(t)
    _solve_square(m, 'Translator_ASM2d_ADM1')
    out = t.properties_out[0]
    return {'conc_kg_m3': {j: float(pyo.value(out.conc_mass_comp[j])) for j in out.conc_mass_comp}, 'cations_kmol_m3': float(cations_kmol_m3), 'anions_kmol_m3': float(anions_kmol_m3), 'asm2d_parameters_applied': applied, 'adm1_contents_applied': applied_adm1, 'interface_element_demand': demand, 'heaviside_k': float(heaviside_k), 'exact_mappings': {'expressions': exact, 'basis': 'the inlet is fixed, so each smoothed max/min mapping is bound to its exact value (eps_smooth evaluated at 0): no eps term in any outlet'}}

def _reverse_interface(adm1_kg_m3: Dict[str, float], asm2d_fixes: Dict[str, float], adm1_fixes: Dict[str, Any] | None):
    """The bound-nutrient ModifiedADM1 -> ASM2d interface on its own parameter blocks, its inlet concentrations fixed."""
    from watertap_engine_mcp.units.adm1_bound_translator import BoundNutrientADM1ASM2D
    m = _asm2d_blocks()
    _apply_asm2d_fixes(m, asm2d_fixes)
    _apply_adm1_fixes(m.fs.rd, adm1_fixes)
    m.fs.t2 = BoundNutrientADM1ASM2D(inlet_property_package=m.fs.pd, outlet_property_package=m.fs.pa, inlet_reaction_package=m.fs.rd, outlet_reaction_package=m.fs.ra, has_phase_equilibrium=False, outlet_state_defined=True)
    for name, value in adm1_kg_m3.items():
        m.fs.t2.inlet.conc_mass_comp[0, name].fix(max(float(value), 0.0))
    return (m, m.fs.t2)

def reverse_phosphate_draw(*, adm1_kg_m3: Dict[str, float], struvite_kg_m3: float, asm2d_fixes: Dict[str, float], adm1_fixes: Dict[str, Any] | None) -> tuple[Dict[str, float], Dict[str, Any]]:
    """The S/Fe digestate's liquor for the reverse interface with the struvite's share of the mapping's phosphorus draw
    folded back onto S_Mg / S_IN / S_IP (``units.adm1_bound_translator.PHOSPHATE_DRAW_RULE``), and the record: the
    draw, the struvite dissolved and the struvite left (kg MgNH4PO4/m3, the kernel's ``WALK_STRUVITE_STATE``)."""
    import pyomo.environ as pyo
    from puran_mcp_support.schema.madm1_iron_minerals import FORMULA_MASS, MADM1_IRON_MINERAL_EXTENSION, MW_Mg, MW_N, MW_P
    from watertap_engine_mcp.units.adm1_bound_translator import PHOSPHATE_DRAW_RULE, mapped_inorganic_kg_m3, struvite_dissolved_for_draw
    m, t = _reverse_interface(adm1_kg_m3, asm2d_fixes, adm1_fixes)
    for name, var in t.inlet.conc_mass_comp.items():
        if not var.fixed:
            var.fix(0.0)
    mapped = mapped_inorganic_kg_m3(t, 0)
    liquor = {pool: float(pyo.value(t.inlet.conc_mass_comp[0, pool])) for pool in ('S_Mg', 'S_IN', 'S_IP')}
    draw = {'P': liquor['S_IP'] - mapped['S_PO4'], 'N': liquor['S_IN'] - mapped['S_NH4']}
    mass = float(FORMULA_MASS[WALK_STRUVITE_STATE])
    held = max(float(struvite_kg_m3), 0.0)
    kmol = struvite_dissolved_for_draw(draw_p=draw['P'] / MW_P, draw_n=draw['N'] / MW_N, mg=liquor['S_Mg'] / MW_Mg, n=liquor['S_IN'] / MW_N, p=liquor['S_IP'] / MW_P, available=held / mass)
    dissolved = held if kmol >= held / mass else kmol * mass
    content = MADM1_IRON_MINERAL_EXTENSION.content(WALK_STRUVITE_STATE)
    folded = dict(adm1_kg_m3)
    for element, pool in (('Mg', 'S_Mg'), ('N', 'S_IN'), ('P', 'S_IP')):
        if dissolved:
            folded[pool] = float(folded.get(pool, 0.0)) + dissolved * content.content(element)
    after = {'S_Mg': folded.get('S_Mg', 0.0), 'S_IN': folded.get('S_IN', 0.0) - draw['N'], 'S_IP': folded.get('S_IP', 0.0) - draw['P']}
    product = liquor['S_Mg'] * liquor['S_IN'] * liquor['S_IP']
    return (folded, {'rule': PHOSPHATE_DRAW_RULE, 'draw_kg_p_m3': draw['P'], 'draw_kg_n_m3': draw['N'], 'liquor_kg_m3': liquor, 'struvite_kernel_kg_m3': held, 'struvite_dissolved_kg_m3': dissolved, 'struvite_kg_m3': held - dissolved, 'after_mapping_kg_m3': after, 'ion_product_ratio': after['S_Mg'] * after['S_IN'] * after['S_IP'] / product if product > 0 else None})

def translate_adm1_to_asm2d(*, flow_m3_d: float, temperature_k: float, adm1_kg_m3: Dict[str, float], cations_kmol_m3: float, anions_kmol_m3: float, asm2d_fixes: Dict[str, float], adm1_fixes: Dict[str, Any] | None=None) -> Dict[str, Any]:
    import pyomo.environ as pyo
    from watertap_engine_mcp.units.adm1_bound_translator import mapped_inorganic_kg_m3
    m, t = _reverse_interface(adm1_kg_m3, asm2d_fixes, adm1_fixes)
    t.inlet.flow_vol.fix(flow_m3_d / 86400.0)
    t.inlet.temperature.fix(temperature_k)
    t.inlet.pressure.fix(101325.0)
    t.inlet.cations.fix(float(cations_kmol_m3))
    t.inlet.anions.fix(float(anions_kmol_m3))
    try:
        _solve_square(m, 'BoundNutrientADM1ASM2D')
    except NativeDigestionRefusal as exc:
        short = {k: v for k, v in mapped_inorganic_kg_m3(t, 0).items() if v < 0.0}
        if short:
            raise NativeDigestionRefusal("the reverse interface's mapping takes more than the liquor carries: " + ', '.join((f'{k} maps to {v:.6g} kg/m3' for k, v in short.items())) + " (ASM2d X_S/S_F contents beyond their mADM1 matter's, from S_IP/S_IN; units.adm1_bound_translator.PHOSPHATE_DRAW_RULE): " + str(exc)) from exc
        raise NativeDigestionRefusal('bound-biomass interface cannot represent this liquor with nonnegative X_H/X_S/X_I on the declared component contents; no N/P release fallback: ' + str(exc)) from exc
    out = t.properties_out[0]
    record = {'carrier': 'X_H with COD-conserving X_S/X_I redistribution', 'live_biomass_kg_cod_m3': float(pyo.value(t.live_biomass[0])), 'live_N_kg_m3': float(pyo.value(t.live_elements[0, 'N'])), 'live_P_kg_m3': float(pyo.value(t.live_elements[0, 'P'])), 'X_H_kg_cod_m3': float(pyo.value(out.conc_mass_comp['X_H'])), 'X_S_redistribution_kg_cod_m3': float(pyo.value(out.conc_mass_comp['X_S'] - t.Xch_AD1[0] - t.Xpr_AD1[0] - t.Xli_AD1[0])), 'X_I_redistribution_kg_cod_m3': float(pyo.value(out.conc_mass_comp['X_I'] - t.XI_AD1[0]))}
    return {'conc_kg_m3': {j: float(pyo.value(out.conc_mass_comp[j])) for j in out.conc_mass_comp}, 'bound_biomass_mapping': record}

def _digester_report(digester, pyo) -> Dict[str, Any]:
    out = digester.liquid_phase.properties_out[0]
    rx = digester.liquid_phase.reactions[0]
    vap = digester.vapor_phase[0]
    liquid = {j: float(pyo.value(out.conc_mass_comp[j])) for j in out.conc_mass_comp}
    vapor_conc = {j: float(pyo.value(vap.conc_mass_comp[j])) for j in vap.conc_mass_comp}
    psat = {j: float(pyo.value(vap.pressure_sat[j])) for j in vap.pressure_sat}
    flow_vap_m3_d = float(pyo.value(vap.flow_vol)) * 86400.0
    t_k = float(pyo.value(vap.temperature))
    p_pa = float(pyo.value(vap.pressure))
    report = {'liquid': {'flow_m3_d': float(pyo.value(out.flow_vol)) * 86400.0, 'temperature_k': float(pyo.value(out.temperature)), 'conc_kg_m3': liquid, 'cations_kmol_m3': float(pyo.value(out.cations)), 'anions_kmol_m3': float(pyo.value(out.anions))}, 'pH': float(pyo.value(rx.pH)), 'vapor': {'flow_m3_d': flow_vap_m3_d, 'temperature_k': t_k, 'pressure_pa': p_pa, 'conc_kg_m3': vapor_conc, 'partial_pressures_pa': psat}}
    for name in ('S_H', 'S_nh3', 'S_hco3', 'S_ac_ion', 'S_pro_ion', 'S_bu_ion', 'S_va_ion', 'S_co2', 'S_nh4'):
        for block in (rx, out):
            var = getattr(block, name, None)
            if var is not None:
                try:
                    report.setdefault('speciation', {})[name] = float(pyo.value(var))
                except Exception:
                    pass
                break
    report['speciation'] = {name: float(pyo.value(getattr(rx, name))) for name in ('S_H', 'pKW', 'pK_a_co2', 'pK_a_IN', 'pK_a_IP', 'pK_a_co3', 'conc_mol_co2', 'conc_mol_hco3', 'conc_mol_co3', 'conc_mol_nh4', 'conc_mol_nh3', 'conc_mol_h2po4', 'conc_mol_hpo4', 'conc_mol_K', 'conc_mol_Mg', 'sodium_mol_l', 'I_Na', 'conc_mol_oh', 'conc_mass_ac', 'conc_mass_pro', 'conc_mass_bu', 'conc_mass_va')}
    report['methanogen_inhibition'] = {r: float(pyo.value(rx.I[r])) for r in ('R10', 'R11')}
    from watertap_engine_mcp.chemistry.modified_adm1_phase1 import NA_INHIBITION_SOURCE
    report['sodium_inhibition_source'] = NA_INHIBITION_SOURCE
    report['chemistry_scope'] = 'phase2_davies_activity_acid_base_ion_pairs_no_minerals' if hasattr(rx, 'conc_mol_pair') else 'phase2_davies_activity_acid_base_no_ion_pairs_no_minerals'
    report['activity_model'] = rx.activity_report()
    if hasattr(rx, 'conc_mol_h2s'):
        report['chemistry_scope'] = 'phase3a_davies_sulfide_iron_fes_kinetic_no_other_minerals'
        report['sulfur_iron'] = _sulfur_iron_report(digester, report, pyo)
    return report

def _sulfur_iron_report(digester, report: Dict[str, Any], pyo) -> Dict[str, Any]:
    """The S/Fe digester's sulfur and iron answer: H2S in the dry biogas, the liquor's sulfur and iron pools (with the
    organic sulfur when the organic-sulfur variant answered), the SRB guilds, FeS and its saturation, and the S / Fe /
    charge closure (``chemistry.modified_adm1_sfe``)."""
    from watertap_engine_mcp.chemistry.modified_adm1_sfe import MW_S, hfo_reduction_kg_fe_s, sulfur_iron_closure
    rx = digester.liquid_phase.reactions[0]
    liquid = report['liquid']['conc_kg_m3']
    pools = SFE_STATES + tuple((k for k in ORGANIC_SULFUR_STATES if k in liquid))
    partials = report['vapor']['partial_pressures_pa']
    dry = sum((float(v) for k, v in partials.items() if k != 'H2O'))
    return {'biogas_h2s_ppmv_dry': float(partials.get('S_h2s', 0.0)) / dry * 1000000.0 if dry > 0 else None, 'free_h2s_kg_S_m3': float(pyo.value(rx.conc_mol_h2s)) * MW_S, 'liquor_kg_m3': {k: liquid[k] for k in pools}, 'srb_kg_cod_m3': {k: liquid[k] for k in SRB_BIOMASS}, 'pK_a_h2s': float(pyo.value(rx.pK_a_h2s)), 'saturation_index_FeS': float(pyo.value(rx.saturation_index_FeS)), 'saturation_index_vivianite': float(pyo.value(rx.saturation_index_vivianite)), 'saturation_index_siderite': float(pyo.value(rx.saturation_index_siderite)), 'saturation_index_mackinawite': float(pyo.value(rx.saturation_index_mackinawite)), 'saturation_index_struvite': float(pyo.value(rx.saturation_index_struvite)), 'srb_inhibition': {r: float(pyo.value(rx.I[r])) for r in ('R26', 'R28', 'R30', 'R32', 'R33')}, 'closure': sulfur_iron_closure(digester), 'hfo_reduced_kg_fe_d': {k: v * 86400.0 for k, v in hfo_reduction_kg_fe_s(digester).items()}, 'scope': 'the shared S/Fe kernel (SRB, HFO, FeS, H2S transfer), its minerals (vivianite, siderite, mackinawite, struvite) and the iron reduction by acetate; the Ca/K/Al phases and the other Mg phases are twin-only (owner ruling scope)'}
FEED_HYDROLYSIS_RULING_ID = 'PUBLIC-RULE-63222d93a0a5'
FEED_HYDROLYSIS_PARAMETERS = ('q_ch_hyd', 'q_pr_hyd', 'q_li_hyd')

def _feed_hydrolysis(request: Dict[str, Any] | None, engine_template_name: str) -> Dict[str, Any] | None:
    """The request's ``hydrolysis_rates_d`` ({q_ch_hyd, q_pr_hyd, q_li_hyd: 1/d, "source"}), validated, or None."""
    if request is None:
        return None
    if engine_template_name != 'adm1_cstr':
        raise NativeDigestionRefusal(f"{engine_template_name} is not the shared kernel: feed hydrolysis rates rebind the kernel's q_ch_hyd / q_pr_hyd / q_li_hyd (adm1_cstr)")
    if not isinstance(request, dict) or not str(request.get('source') or '').strip():
        raise NativeDigestionRefusal('hydrolysis_rates_d names its source')
    rates = {k: v for k, v in request.items() if k != 'source'}
    if set(rates) != set(FEED_HYDROLYSIS_PARAMETERS):
        raise NativeDigestionRefusal(f'hydrolysis_rates_d states exactly {list(FEED_HYDROLYSIS_PARAMETERS)}; got {sorted(rates)}')
    bad = {k: v for k, v in rates.items() if isinstance(v, bool) or not isinstance(v, (int, float)) or (not math.isfinite(v)) or (not v > 0)}
    if bad:
        raise NativeDigestionRefusal(f'hydrolysis rates are finite and positive (1/d): {bad}')
    return {'rates_d': {k: float(v) for k, v in rates.items()}, 'source': str(request['source']), 'ruling': FEED_HYDROLYSIS_RULING_ID}
FEED_HYDROLYSIS_HOLD = "the feed's own hydrolysis rates (hydrolysis_rates_d, FEED_HYDROLYSIS_RULING_ID) are measured at their own temperature, which the request does not state: held, never moved from 35 C"

def _kinetic_bases(rate_temperature_basis: str | None, free_ammonia_basis: str | None, temperature_k: float, engine_template_name: str, feed_hydrolysis: Dict[str, Any] | None) -> Dict[str, Any] | None:
    """The named kernel bases (``process_model_schema.madm1_rate_temperature`` / ``madm1_ammonia_acclimation``),
    validated before anything is built (an unknown basis, or a temperature outside the basis's range, refuses); None
    where neither is named, and the kernel's own set answers."""
    if rate_temperature_basis is None and free_ammonia_basis is None:
        return None
    if engine_template_name != 'adm1_cstr':
        raise NativeDigestionRefusal(f"{engine_template_name} is not the shared kernel: a rate-temperature or free-ammonia basis moves the kernel's constants (adm1_cstr)")
    from puran_mcp_support.schema.madm1_ammonia_acclimation import FreeAmmoniaBasisError, free_ammonia_basis as _fa
    from puran_mcp_support.schema.madm1_rate_temperature import RateTemperatureError, rate_temperature_record
    hold = FEED_HYDROLYSIS_PARAMETERS if feed_hydrolysis is not None else ()
    try:
        if rate_temperature_basis is not None:
            rate_temperature_record(rate_temperature_basis, temperature_k, hold=hold, hold_reason=FEED_HYDROLYSIS_HOLD)
        if free_ammonia_basis is not None:
            _fa(free_ammonia_basis)
    except (RateTemperatureError, FreeAmmoniaBasisError) as exc:
        raise NativeDigestionRefusal(str(exc)) from exc
    return {'rate_temperature': rate_temperature_basis, 'free_ammonia': free_ammonia_basis, 'temperature_k': float(temperature_k), 'hold': hold}

def _apply_kinetic_bases(rp, bases: Dict[str, Any] | None) -> None:
    """Every built model carries the named bases before any solve."""
    if bases is None:
        return
    if bases['rate_temperature'] is not None:
        rp.apply_rate_temperature(bases['rate_temperature'], bases['temperature_k'], hold=bases['hold'], hold_reason=FEED_HYDROLYSIS_HOLD if bases['hold'] else '')
    if bases['free_ammonia'] is not None:
        rp.apply_free_ammonia_basis(bases['free_ammonia'])

def solve_digester(*, flow_m3_d: float, temperature_k: float, adm1_feed: Dict[str, Any], volume_liquid_m3: float, volume_vapor_m3: float, evidence: Dict[str, Any], continuation: Dict[str, Any] | None=None, budget_seconds: int=600, adm1_fixes: Dict[str, Any] | None=None, sodium_mol_l: float=0.0, calcium_mol_l: float=0.0, sulfate_mol_l: float=0.0, engine_template_name: str='adm1_uasb', oxidant_inlet: Dict[str, Any] | None=None, headspace_wetted_area_m2: float | None=None, microaeration_bracket: Dict[str, Any] | None=None, warm_start: Dict[str, Any] | None=None, hydrolysis_rates_d: Dict[str, Any] | None=None, rate_temperature_basis: str | None=None, free_ammonia_basis: str | None=None) -> Dict[str, Any]:
    """A single-CSTR mADM1 preset (with headspace) at the rated geometry, walked onto the methanogenic branch by
    whole-feed dilution continuation. ``engine_template_name`` is ``adm1_uasb`` (the house Davies block on WaterTAP's
    library kinetics) or ``adm1_cstr`` (the shared biokinetic kernel, Wave 2 item 6); both are one completely mixed
    reactor, so the continuation and the root-exclusion devices apply unchanged.

    ``continuation["blend_from"]`` ({"conc_kg_m3", "cations_kmol_m3", "anions_kmol_m3", optional "sodium_mol_l" /
    "calcium_mol_l" / "sulfate_mol_l", "source"}) makes the continuation a COMPOSITION homotopy instead of a
    dilution: at lambda the feed is lambda x the real feed + (1 - lambda) x the stated start feed, so a feed whose
    dilute version is unbuffered (diluting it removes the buffer its own digestion builds) is approached from a feed
    that is known to converge. lambda0 may then be 0 (the start feed itself). It is a numerical path only: the answer
    at lambda = 1 is the real feed's, and the start feed and every step are recorded.

    ``oxidant_inlet`` ({"kmol_d": {O2, N2, Ar, CO2}, "temperature_K", "pressure_Pa", "source"}) with
    ``headspace_wetted_area_m2`` (stage (b)'s, bound in ``evidence`` like the volumes) builds the adm1_cstr S/Fe digester's
    MICRO-AERATION variant (``chemistry.modified_adm1_microaeration``; a NON-governing bracket model). The dose is held
    at zero through the organic and sulfur walks and then stepped in (``DEFAULT_OXIDANT_DOSE_STEPS``, or
    ``continuation['oxidant_dose_steps']``); the answer carries ``microaeration`` (the O2, N2 and S balances, the S0
    split, the residual O2, the reaction heat, the solved zero-dose reference and the dose path). The dose is fixed:
    nothing here re-doses to reach a removal.

    ``microaeration_bracket`` ({} or {"wetted_area_m2": {"low", "high", "source"}}) also runs the BRACKET on the solved
    dosed digester (``MICROAERATION_BRACKET_PARAMETERS``, one constant at a time at its band edges, sizing and dose
    fixed) and adds ``microaeration['bracket']``: every point, and the envelope of each reported quantity.

    ``warm_start`` ({"structure", "values", "source"}: a previous answer's ``warm_state``, `WARM_START_RULING_ID`) starts
    the solve from that answer at the full feed instead of the continuation; the dose walk and the bracket follow
    unchanged. Where it cannot answer (`_warm_mismatch`, or the warm solve is refused) the cold staged path answers,
    and ``native_start`` says which path did and why. The answer always carries ``warm_state`` (its own solved values
    before any oxidant dose: the next request's warm start).

    ``rate_temperature_basis`` / ``free_ammonia_basis`` (adm1_cstr only) name a kernel basis that moves the rate
    constants to ``temperature_k`` or rebinds the acetoclasts' KI_nh3 (`_kinetic_bases`); every built model carries
    them, the feed's own hydrolysis is held (`FEED_HYDROLYSIS_HOLD`), and ``kinetics`` records what answered (the
    kernel's unmoved set where none is named). A solved temperature off the basis's is refused."""
    if engine_template_name not in SINGLE_CSTR_TEMPLATES:
        raise NativeDigestionRefusal(f'{engine_template_name!r} is not a single-CSTR digester preset ({sorted(SINGLE_CSTR_TEMPLATES)})')
    feed_hydrolysis = _feed_hydrolysis(hydrolysis_rates_d, engine_template_name)
    kinetic_bases = _kinetic_bases(rate_temperature_basis, free_ammonia_basis, temperature_k, engine_template_name, feed_hydrolysis)
    import pyomo.environ as pyo
    import watertap_engine_mcp.actions as actions
    from watertap_engine_mcp.actions import _universal_template_config, apply_template, session_manager
    from watertap_engine_mcp.solver.digester_diagnostics import classify_digester_basin
    from watertap_engine_mcp.solver.pipeline import HygienePipeline, PipelineConfig
    from watertap_engine_mcp.utils.model_builder import ModelBuilder
    from watertap.core.solvers import get_solver
    plan = {**DEFAULT_CONTINUATION, **(continuation or {})}
    lam0 = float(plan['lambda0'])
    steps = [float(x) for x in plan['steps']]
    blend = plan.get('blend_from')
    if blend is not None and (not isinstance(blend, dict) or not str(blend.get('source') or '').strip() or (not isinstance(blend.get('conc_kg_m3'), dict))):
        raise NativeDigestionRefusal('continuation blend_from needs conc_kg_m3, its strong-ion lumps and a source')
    lam0_ok = 0 <= lam0 <= 1 if blend is not None else 0 < lam0 <= 1
    if not steps or steps[-1] != 1.0 or (not lam0_ok) or any((b <= a for a, b in zip([lam0, *steps], steps))):
        raise NativeDigestionRefusal('continuation must rise strictly from lambda0 in (0, 1] and end at lambda = 1.0')
    full = {k: max(float(v), 1e-09) for k, v in adm1_feed['conc_kg_m3'].items()}
    sfe_named = sorted((k for k in full if (k in SFE_STATES or k in ORGANIC_SULFUR_STATES) and full[k] > 1e-09))
    if sfe_named and engine_template_name != 'adm1_cstr':
        raise NativeDigestionRefusal(f'{engine_template_name} carries no sulfur/iron state; the feed names {sfe_named} (the S/Fe digester is adm1_cstr)')
    if full.get('S_SO4', 0.0) > 1e-09 and float(sulfate_mol_l) > 0:
        raise NativeDigestionRefusal('the feed carries sulfate twice: as the S_SO4 state AND in the anion lump (sulfate_mol_l); an S/Fe feed states it once, as S_SO4')
    organic_fed = [k for k in ORGANIC_SULFUR_STATES if full.get(k, 0.0) > 1e-09]
    if organic_fed and float(sulfate_mol_l) > 0:
        raise NativeDigestionRefusal('the feed carries organic sulfur twice: as the X_Sorg state AND (with all its sulfur) in the anion lump (sulfate_mol_l); an organic-S feed states its sulfur once, X_Sorg and S_SO4')
    ramped = [k for k in SULFUR_IRON_FEED_STATES + ORGANIC_SULFUR_STATES if full.get(k, 0.0) > 1e-09]
    sigma_steps = [float(x) for x in plan.get('sulfur_iron_steps') or DEFAULT_SULFUR_IRON_STEPS] if ramped else []
    if sigma_steps and (sigma_steps[-1] != 1.0 or any((not 0 < x <= 1 for x in sigma_steps)) or any((b <= a for a, b in zip(sigma_steps, sigma_steps[1:])))):
        raise NativeDigestionRefusal('sulfur_iron_steps must rise strictly within (0, 1] and end at 1.0')
    sigma = {'value': 0.0 if sigma_steps else 1.0}
    oxidant_state = None
    dose_steps: List[float] = []
    if oxidant_inlet is None and headspace_wetted_area_m2 is not None:
        raise NativeDigestionRefusal("headspace_wetted_area_m2 is stated without an oxidant_inlet; the wetted area is the micro-aeration variant's geometry and would be dropped")
    if microaeration_bracket is not None:
        if oxidant_inlet is None:
            raise NativeDigestionRefusal('a micro-aeration bracket is asked for without an oxidant_inlet; there is no dosed digester to bracket')
        _bracket_area_band(microaeration_bracket, headspace_wetted_area_m2)
    if oxidant_inlet is not None:
        from watertap_engine_mcp.chemistry.modified_adm1_microaeration import OxidantInletError, oxidant_inlet_state
        if engine_template_name != 'adm1_cstr':
            raise NativeDigestionRefusal(f"{engine_template_name} takes no oxidant inlet (digester micro-aeration is the adm1_cstr S/Fe digester's)")
        if not sigma_steps:
            raise NativeDigestionRefusal("an oxidant inlet on a feed that carries no sulfur or iron: micro-aeration oxidises the S/Fe digester's headspace H2S, and that digester is built only for a feed with sulfur (S_SO4, S_IS or X_Sorg)")
        try:
            oxidant_state = oxidant_inlet_state(oxidant_inlet)
        except OxidantInletError as exc:
            raise NativeDigestionRefusal(str(exc)) from exc
        try:
            area_ok = headspace_wetted_area_m2 is not None and (not isinstance(headspace_wetted_area_m2, bool)) and math.isfinite(float(headspace_wetted_area_m2)) and (float(headspace_wetted_area_m2) > 0)
        except (TypeError, ValueError):
            area_ok = False
        if not area_ok:
            raise NativeDigestionRefusal(f"an oxidant inlet needs {WETTED_AREA_FIELD} [m2] from stage (b) (the anaerobic sizing's rated headspace wetted area, per train), bound in the evidence like the volumes; got {headspace_wetted_area_m2!r}. A rating never invents it")
        dose_steps = [float(x) for x in plan.get('oxidant_dose_steps') or DEFAULT_OXIDANT_DOSE_STEPS]
        if dose_steps[-1] != 1.0 or any((not 0 < x <= 1 for x in dose_steps)) or any((b <= a for a, b in zip(dose_steps, dose_steps[1:]))):
            raise NativeDigestionRefusal('oxidant_dose_steps must rise strictly within (0, 1] and end at 1.0')
    srb_inoculum = float(plan.get('srb_inoculum_kg_cod_m3', DEFAULT_SRB_INOCULUM_KG_COD_M3)) if sigma_steps else 0.0
    if not math.isfinite(srb_inoculum) or srb_inoculum < 0:
        raise NativeDigestionRefusal('srb_inoculum_kg_cod_m3 is a finite non-negative feed biomass')
    inoculated = ADM1_BIOMASS + (SRB_BIOMASS if full.get('S_SO4', 0.0) > SRB_INOCULUM_SULFATE_KG_S_M3 else ())
    for k in inoculated:
        if k in SRB_BIOMASS:
            full.setdefault(k, 1e-09)
    cations, anions = (float(adm1_feed['cations_kmol_m3']), float(adm1_feed['anions_kmol_m3']))
    inoculum = float(plan.get('inoculum_kg_cod_m3') or 0.0)
    if not math.isfinite(inoculum) or inoculum < 0:
        raise NativeDigestionRefusal('continuation inoculum_kg_cod_m3 is a finite non-negative feed biomass')

    def ramp(lam):
        """1 at lambda0, falling linearly to EXACTLY 0 at lambda = 1."""
        return 0.0 if lam >= 1.0 or lam0 >= 1.0 else (1.0 - lam) / (1.0 - lam0)

    def scaled(lam):
        """The feed at continuation parameter lam: the real feed x lam, plus an inoculum of every trophic group that
        falls linearly from its full value at lambda0 to exactly zero at lambda = 1. With biomass in the feed, X_j = 0
        is not a root, so the dilute start cannot stop on a washout root; the answer at lambda = 1 is the real feed's."""
        share = inoculum * ramp(lam)
        start = (blend or {}).get('conc_kg_m3') or {}
        srb_share = srb_inoculum * (1.0 - sigma['value'])
        return {k: max(v * (sigma['value'] if k in ramped else 1.0) * lam + (1.0 - lam) * float(start.get(k, 0.0)) + (share if k in inoculated else 0.0) + (srb_share if k in SRB_BIOMASS else 0.0), 1e-09) for k, v in full.items()}
    per_s = float(flow_m3_d) / 86400.0

    def lumps_at(lam, buffer):
        """(cations, anions) at lam in kmol/m3 and the (Na, Ca, SO4) molar flows in kmol/s: the real feed's x lam
        (the dilution path's own arithmetic, unchanged), plus the buffer ramp, plus (1 - lam) of a blend start's (a
        composition homotopy, exactly zero at lambda = 1; absent without a blend)."""
        cat, an = (cations * lam + buffer * ramp(lam), anions * lam)
        flows = [c * float(flow_m3_d) / 86400.0 * lam for c in (sodium_mol_l, calcium_mol_l, sulfate_mol_l)]
        if blend is not None:
            cat += (1.0 - lam) * float(blend.get('cations_kmol_m3', 0.0))
            an += (1.0 - lam) * float(blend.get('anions_kmol_m3', 0.0))
            flows = [f + (1.0 - lam) * float(blend.get(k, 0.0)) * per_s for f, k in zip(flows, ('sodium_mol_l', 'calcium_mol_l', 'sulfate_mol_l'))]
        return (cat, an, *flows)
    if not math.isfinite(sodium_mol_l) or sodium_mol_l < 0:
        raise NativeDigestionRefusal('sodium_mol_l must be finite and nonnegative')
    for name, concentration, equivalents in (('calcium_mol_l', calcium_mol_l, cations), ('sulfate_mol_l', sulfate_mol_l, anions)):
        if not math.isfinite(concentration) or concentration < 0 or 2 * concentration > equivalents:
            raise NativeDigestionRefusal(f'{name} must be finite, nonnegative and already contained in its strong-ion lump')
    net_excess = max(cations + full['S_K'] / 39.1 + 2 * full['S_Mg'] / 24.3 - anions, 0.0)
    attempts: List[Dict[str, Any]] = []

    def start(buffer_share, *, sulfur_iron_build=None, warm_from=None, lam=None, warm_values=None):
        """Build the preset at the dilute start with ``buffer_share`` of the net strong-cation excess, and run the
        server's hygiene pipeline on it; (model, builder, units, digester, applied contents) or None.

        ``sulfur_iron_build=False`` builds the CORE preset on the feed's core states (the staged start's first half);
        ``warm_from`` (a solved core start's model) runs the pipeline's own stages with every value the two models
        share copied from it before the solve, accepted by the same post-solve diagnostics (``_staged_from_core``);
        ``warm_values`` (a previous answer's values, `WARM_START_RULING_ID`) is one warm solve from them, accepted by
        the same diagnostics (``_warm_from_values``)."""
        lam_at = lam0 if lam is None else float(lam)
        buffer = buffer_share * net_excess
        cat0, an0, na0, ca0, so40 = lumps_at(lam_at, buffer)
        sfe_build = engine_template_name == 'adm1_cstr' and bool(sigma_steps) if sulfur_iron_build is None else bool(sulfur_iron_build)
        feed0 = scaled(lam_at) if sfe_build else {k: v for k, v in scaled(lam_at).items() if k not in SFE_STATES and k not in ORGANIC_SULFUR_STATES}
        targets = {'feed_conc_kg_m3': feed0, 'feed_cations_kmol_m3': cat0, **({'sulfur_iron': True} if sfe_build else {}), **({'organic_sulfur': True} if sfe_build and organic_fed else {}), 'feed_anions_kmol_m3': an0, 'digester_volume_liquid_m3': float(volume_liquid_m3), 'digester_volume_vapor_m3': float(volume_vapor_m3), 'feed_temperature_K': float(temperature_k), 'feed_flow_m3_per_day': float(flow_m3_d), **{k: evidence[k] for k in ('sizing_reference', 'seed_bindings', 'sizing_outputs')}}
        oxidant_build = sfe_build and oxidant_state is not None
        if oxidant_build:
            targets.update({'oxidant_inlet': dict(oxidant_inlet), WETTED_AREA_FIELD: float(headspace_wetted_area_m2)})
        elif oxidant_state is not None and isinstance(targets['seed_bindings'], dict):
            targets['seed_bindings'] = {k: v for k, v in targets['seed_bindings'].items() if k != WETTED_AREA_FIELD}
        config = _universal_template_config(engine_template_name=engine_template_name, basis=None, fractionation=None, design_targets=targets)
        session_id = f'native-digestion-{uuid.uuid4()}'
        created = apply_template(engine_template_name, session_id=session_id, template_config=config, name='native_digestion')
        if 'error' in created:
            raise NativeDigestionRefusal(f'{engine_template_name} preset refused the rated request: {created['error']}')
        session = session_manager.load(session_id)
        builder = ModelBuilder(session)
        model = builder.build()
        units = builder.get_units()
        digester = actions._adm1_methanogenic_digester(builder, units)
        applied = _apply_adm1_fixes(digester.config.reaction_package, adm1_fixes)
        if feed_hydrolysis is not None:
            digester.config.reaction_package.apply_parameter_values(feed_hydrolysis['rates_d'])
        _apply_kinetic_bases(digester.config.reaction_package, kinetic_bases)
        if sfe_build and warm_values is None and hasattr(digester.config.reaction_package, 'iron_mineral_parameter'):
            held = digester.config.reaction_package.iron_mineral_parameter
            held[STRUVITE_RATE_PARAMETER].set_value(0.0)
            for seed in HELD_SEED_MINERALS:
                held[seed].set_value(HELD_SEED_KG_M3)
        if oxidant_build:
            digester.vapor_inlet_state[0].flow_vol.fix(0.0)
        digester.liquid_phase.reactions[0].sodium_molar_flow.set_value(na0)
        for name, molar_flow in (('calcium', ca0), ('sulfate', so40)):
            getattr(digester.liquid_phase.reactions[0], name + '_molar_flow').set_value(molar_flow)
        started = time.monotonic()
        options = session.config.runtime_metadata.get('solver_options') or {}
        pipeline = HygienePipeline(model, PipelineConfig(solver='ipopt', solver_options=options, max_wall_time_s=int(budget_seconds), allow_near_feasible_asm2d=False), units=units, session=session)
        warm_map = None
        if warm_values is not None:
            mapped, warm_map = map_warm_values(model, warm_values, sfe_family(digester.config.reaction_package))
            ok, message = _warm_from_values(pipeline, model, mapped)
            stage = 'warm_start_from_previous_answer'
        elif warm_from is None:
            base = pipeline.run_all()
            ok, message = (bool(getattr(base, 'success', False)), str(getattr(base, 'message', '')))
            stage = 'hygiene_pipeline'
        else:
            ok, message = _staged_from_core(pipeline, model, warm_from)
            stage = 'staged_from_core_start'
        attempts.append({'lambda': lam_at, 'stage': stage, 'buffer_share': buffer_share, 'success': ok, 'message': message, 'sulfur_iron_build': sfe_build, 'seconds': round(time.monotonic() - started, 2), 'session_id': session_id, **({'warm_state_map': warm_map} if warm_map is not None else {})})
        return (model, builder, units, digester, applied, session_id, buffer) if ok else None
    shares = tuple(dict.fromkeys((float(plan.get('buffer_share') or 0.0), WALK_BUFFER_FALLBACK_FRACTION * float(plan.get('buffer_share') or 0.0), 0.0)))
    sfe_feed = bool(sigma_steps) and engine_template_name == 'adm1_cstr'
    model = builder = units = digester = applied_adm1 = session_id = buffer = feed_block = None
    path: List[Dict[str, Any]] = []
    mineral_path: List[Dict[str, Any]] = []

    def bind(built_start):
        nonlocal model, builder, units, digester, applied_adm1, session_id, buffer, feed_block
        model, builder, units, digester, applied_adm1, session_id, buffer = built_start
        feed_block = next((u for u in units.values() if hasattr(u, 'conc_mass_comp') and hasattr(u, 'flow_vol') and (not hasattr(u, 'liquid_phase'))))

    def start_refused(what):
        return DiluteStartRefusal(f'the digester did not converge at the dilute continuation start lambda={lam0} {what}: {[a.get('message', a.get('termination')) for a in attempts]}')
    warm = get_solver(options={'bound_push': 1e-08, 'mu_init': 1e-06, 'warm_start_init_point': 'yes', 'max_iter': 500})

    def branch(lam):
        rep = _digester_report(digester, pyo)
        return {'lambda': lam, 'pH': rep['pH'], 'vfa_kg_cod_m3': {k: rep['liquid']['conc_kg_m3'][k] for k in ('S_ac', 'S_pro', 'S_bu', 'S_va', 'S_fa')}, 'biomass_kg_cod_m3': {k: rep['liquid']['conc_kg_m3'][k] for k in ADM1_BIOMASS}}

    def reseed_invaders(lam):
        """A trophic group at zero satisfies its own balance whatever its substrate (X_j = 0 is always a root), so a
        solve can stop on a washout root that is UNSTABLE -- the group would invade from any inoculum (Terminus
        2026-09-29: X_fa = 0 beside 1.4 kg COD/m3 of LCFA at lambda 0.1, where its net growth rate is ~7x the
        dilution rate). A warm re-solve from a re-seeded point falls straight back onto that root, so the trivial root
        is EXCLUDED instead: each washed-out group gets a lower bound EXCLUSION_FLOOR_KG_M3 and the square system is
        solved; a nontrivial root exists iff that solve is optimal. The bound is then released and the state re-solved
        unbounded (still a root: the bound is inactive). No root above the floor = a real washout, reported as such.
        Every attempt is recorded."""
        out = digester.liquid_phase.properties_out[0]
        washed = [k for k in ADM1_BIOMASS if pyo.value(out.conc_mass_comp[k]) < WASHOUT_FLOOR_KG_M3]
        if not washed:
            return
        living = [pyo.value(out.conc_mass_comp[k]) for k in ADM1_BIOMASS if k not in washed]
        seed = max(sum(living) / len(living), 0.001) if living else 0.1 * lam
        original_lb = {k: out.conc_mass_comp[k].lb for k in washed}
        for k in washed:
            out.conc_mass_comp[k].set_value(seed)
            out.conc_mass_comp[k].setlb(EXCLUSION_FLOOR_KG_M3)
        t0 = time.monotonic()
        condition = 'not_run'
        for label, solver in (('warm', warm), ('cold', get_solver(options={'max_iter': 3000}))):
            condition = _solve(solver, model)
            attempts.append({'lambda': lam, 'stage': f'exclude_washout_root_{label}', 'groups': washed, 'seed_kg_cod_m3': seed, 'floor_kg_cod_m3': EXCLUSION_FLOOR_KG_M3, 'termination': condition, 'seconds': round(time.monotonic() - t0, 2)})
            if condition == 'optimal':
                break
            for k in washed:
                out.conc_mass_comp[k].set_value(seed)
        for k in washed:
            out.conc_mass_comp[k].setlb(original_lb[k])
        if condition != 'optimal':
            raise NativeDigestionRefusal(f'IPOPT found no steady state with {washed} above {EXCLUSION_FLOOR_KG_M3:g} kg COD/m3 at lambda={lam} from the re-seeded point ({condition}); a LOCAL result that does not prove the washout is real -- the digester is not presented on a washout root either way')
        released = _solve(warm, model)
        attempts.append({'lambda': lam, 'stage': 'release_exclusion_bound', 'termination': released, 'groups': {k: pyo.value(out.conc_mass_comp[k]) for k in washed}})
        if released != 'optimal':
            raise NativeDigestionRefusal(f'the nontrivial root at lambda={lam} did not re-solve with the exclusion bound released (IPOPT {released})')

    def exclude_sour_root(lam):
        """The acidified root (acetate accumulated until the pH inhibits the acetoclastic methanogens that would remove
        it) is self-sustaining, and a solve from the template's seed can stop on it even where a methanogenic root
        exists (Terminus 2026-09-29: pH 6.23 with 0.9 kg COD/m3 acetate at lambda 0.1, where a pH 6.60 / 0.05 kg COD/m3
        root exists). Same device as the washout exclusion: an upper bound SOUR_EXCLUSION_ACETATE_KG_COD_M3 on acetate
        excludes the sour root, the square system is solved, the bound is released and the state re-solved unbounded.
        No root under the bound is reported as a local result, and the sour state is then what the adapter refuses."""
        basin = classify_digester_basin(digester)
        if not basin.get('is_sour'):
            return
        out = digester.liquid_phase.properties_out[0]
        s_ac = out.conc_mass_comp['S_ac']
        original_ub, before = (s_ac.ub, pyo.value(s_ac))
        bound = min(SOUR_EXCLUSION_ACETATE_KG_COD_M3, SOUR_EXCLUSION_SHARE * before)
        saved = _snapshot(model)
        condition = 'not_run'
        for label, options in SOUR_EXCLUSION_STRATEGIES:
            _restore(saved)
            s_ac.setub(bound)
            s_ac.set_value(0.5 * bound)
            t0 = time.monotonic()
            condition = _solve(get_solver(options=dict(options)), model)
            attempts.append({'lambda': lam, 'stage': f'exclude_sour_root_{label}', 'acetate_before_kg_cod_m3': before, 'acetate_bound_kg_cod_m3': bound, 'termination': condition, 'seconds': round(time.monotonic() - t0, 2)})
            if condition == 'optimal':
                break
        s_ac.setub(original_ub)
        if condition == 'optimal':
            released = _solve(warm, model)
            attempts.append({'lambda': lam, 'stage': 'release_sour_bound', 'termination': released, 'acetate_kg_cod_m3': pyo.value(s_ac), 'pH': pyo.value(digester.liquid_phase.reactions[0].pH)})
            if released == 'optimal' and (not classify_digester_basin(digester).get('is_sour')):
                return
        if relax_ph_inhibition_homotopy(lam, saved):
            return
        _restore(saved)
        attempts.append({'lambda': lam, 'stage': 'restore_sour_root', 'acetate_kg_cod_m3': pyo.value(s_ac)})

    def relax_ph_inhibition_homotopy(lam, saved) -> bool:
        """Homotopy on the methanogens' pH-inhibition window, the mechanism that SUSTAINS the sour root (acetate held
        where the pH inhibits its own consumers): the windows of the acetoclastic and hydrogenotrophic groups are
        shifted down by PH_HOMOTOPY_SHIFT, the model solved (the sour root is not self-sustaining there), and the
        shift walked back to exactly zero by warm-started solves. The answer is a root of the TRUE model (the shift is
        zero at the end, the parameters restored); every step is recorded; a step that fails restores everything."""
        rp = digester.config.reaction_package
        names = ('pH_UL_ac', 'pH_LL_ac', 'pH_UL_h2', 'pH_LL_h2')
        kernel = getattr(rp, 'kernel_parameter', None)

        def window(n):
            return kernel[n] if kernel is not None else getattr(rp, n)

        def set_window(n, v):
            window(n).set_value(v) if kernel is not None else window(n).fix(v)
        original = {n: pyo.value(window(n)) for n in names}
        path_ok = True
        for shift in PH_HOMOTOPY_SHIFTS:
            for n in names:
                set_window(n, original[n] - shift)
            t0 = time.monotonic()
            solver = get_solver(options={'max_iter': 3000}) if shift == PH_HOMOTOPY_SHIFTS[0] else warm
            condition = _solve(solver, model)
            attempts.append({'lambda': lam, 'stage': 'ph_inhibition_homotopy', 'shift_ph': shift, 'termination': condition, 'pH': pyo.value(digester.liquid_phase.reactions[0].pH), 'acetate_kg_cod_m3': pyo.value(digester.liquid_phase.properties_out[0].conc_mass_comp['S_ac']), 'seconds': round(time.monotonic() - t0, 2)})
            if condition != 'optimal':
                path_ok = False
                break
        for n in names:
            set_window(n, original[n])
        if path_ok and (not classify_digester_basin(digester).get('is_sour')):
            return True
        _restore(saved)
        return False

    def set_feed(conc):
        """The feed at a continuation point, on the components THIS preset carries (the core preset carries no S/Fe)."""
        carried = set(feed_block.config.property_package.component_list)
        for name, value in conc.items():
            if name in carried:
                feed_block.conc_mass_comp[0, name].fix(value)

    def start_and_walk(**kwargs):
        """Each buffer share in turn: the dilute start AND the organic walk from it. True when a share walked to
        lambda = 1; False when no start converged; a share whose walk loses the branch is recorded and the next one is
        tried (WALK_BUFFER_FALLBACK_FRACTION), and the last loss is raised when every share's walk lost it."""
        lost = None
        for share in shares:
            built_start = start(share, **kwargs)
            if built_start is None:
                continue
            bind(built_start)
            try:
                walk_organics()
                return True
            except NativeDigestionRefusal as exc:
                attempts.append({'stage': 'organic_walk_lost', 'buffer_share': share, 'message': str(exc)[:400]})
                lost = exc
        if lost is not None:
            raise lost
        return False

    def walk_organics():
        """The organic continuation lambda0 -> 1 on the bound preset (the exclusions at every step)."""
        nonlocal path
        reseed_invaders(lam0)
        exclude_sour_root(lam0)
        path = [branch(lam0)]
        for lam in steps:
            walk_to(lam)

    def walk_to(target):
        """One scheduled step: taken at once where it converges; else undone and halved from the last converged
        lambda (ORGANIC_WALK_MAX_HALVINGS), each halved point a recorded step of the path."""
        trial, halvings = (target, 0)
        while True:
            saved = _snapshot(model)
            try:
                walk_step(trial)
            except NativeDigestionRefusal as exc:
                if not str(exc).startswith('the methanogenic branch was lost'):
                    raise
                _restore(saved)
                halvings += 1
                if halvings > ORGANIC_WALK_MAX_HALVINGS:
                    raise NativeDigestionRefusal(f'{exc} (after {ORGANIC_WALK_MAX_HALVINGS} halvings of the step to lambda={target:g})') from exc
                trial = path[-1]['lambda'] + 0.5 * (trial - path[-1]['lambda'])
                continue
            if trial == target:
                return
            trial = target

    def walk_step(lam):
        set_feed(scaled(lam))
        cat, an, na, ca, so4 = lumps_at(lam, buffer)
        feed_block.cations.fix(cat)
        feed_block.anions.fix(an)
        digester.liquid_phase.reactions[0].sodium_molar_flow.set_value(na)
        for name, molar_flow in (('calcium', ca), ('sulfate', so4)):
            getattr(digester.liquid_phase.reactions[0], name + '_molar_flow').set_value(molar_flow)
        t0 = time.monotonic()
        condition = _solve(warm, model)
        attempts.append({'lambda': lam, 'stage': 'continuation', 'termination': condition, 'seconds': round(time.monotonic() - t0, 2)})
        if condition != 'optimal':
            raise NativeDigestionRefusal(f'the methanogenic branch was lost at feed strength lambda={lam} (IPOPT {condition}) after converging at lambda={path[-1]['lambda']}: the digester at this geometry is overloaded or inhibited before the full feed; last healthy state pH {path[-1]['pH']:.3f}, VFA {path[-1]['vfa_kg_cod_m3']}')
        reseed_invaders(lam)
        exclude_sour_root(lam)
        path.append(branch(lam))

    def cold_path():
        """The full cold staged path (every request before WARM_START_RULING_ID; the fallback of a warm start): the
        organic continuation, the core-first route where the S/Fe one fails, then the sulfur/iron walk; (route,
        sulfur_path)."""
        route = 'core' if not sfe_feed else 'sulfur_iron_build'
        organic_error = None
        try:
            walked = start_and_walk()
        except NativeDigestionRefusal as exc:
            if not sfe_feed:
                raise
            walked, organic_error = (False, exc)
        if not walked and (not sfe_feed):
            raise start_refused('with any buffer share tried')
        if sfe_feed and (not walked):
            attempts.append({'stage': 'sulfur_iron_build_route_failed', 'message': str(organic_error)[:400] if organic_error is not None else 'no S/Fe start'})
            if not start_and_walk(sulfur_iron_build=False):
                raise start_refused('on the S/Fe preset, nor on the core preset of the core-first route')
            core_model = model
            sfe_start = None
            for share in shares:
                sfe_start = start(share, warm_from=core_model, lam=1.0)
                if sfe_start is not None:
                    break
            if sfe_start is None:
                raise NativeDigestionRefusal(f"the S/Fe digester did not re-solve at the full organic feed and sigma 0 from the core preset's answer (the two models coincide there): {[a.get('message') for a in attempts[-len(shares):]]}")
            bind(sfe_start)
            reseed_invaders(1.0)
            exclude_sour_root(1.0)
            path.append({**branch(1.0), 'route': 'S/Fe preset from the core answer, sigma 0'})
            route = 'core_first'
        sulfur_path = []
        last = 0.0
        for target in sigma_steps:
            trial, halvings = (target, 0)
            while last < target:
                saved = _snapshot(model)
                sigma['value'] = trial
                set_feed(scaled(1.0))
                t0 = time.monotonic()
                condition = _solve(warm, model)
                attempts.append({'lambda': 1.0, 'stage': 'sulfur_iron_continuation', 'sigma': trial, 'termination': condition, 'seconds': round(time.monotonic() - t0, 2)})
                if condition == 'optimal':
                    last, trial = (trial, target)
                    continue
                _restore(saved)
                halvings += 1
                if halvings > SULFUR_IRON_MAX_HALVINGS:
                    raise NativeDigestionRefusal(f"the feed's sulfur and iron could not be walked past sigma={last:g} of their real values (IPOPT {condition} at sigma={trial:g} after {SULFUR_IRON_MAX_HALVINGS} halvings) on the methanogenic branch of the full organic feed; states ramped {ramped}")
                trial = last + 0.5 * (trial - last)
            reseed_invaders(1.0)
            exclude_sour_root(1.0)
            sulfur_path.append({'sigma': last, **branch(1.0)})
        walk_minerals()
        return (route, sulfur_path)

    def walk_minerals():
        """The k-then-seed walk at the full feed after the sulfur/iron walk (`HELD_SEED_MINERALS`): struvite's rate
        constant from zero to the extension's on the held seeds, then the held seeds down to the extension's; nothing
        where the bound preset carries no minerals (the core digester)."""
        from watertap_engine_mcp.chemistry.modified_adm1_sfe import IRON_MINERALS
        params = getattr(digester.config.reaction_package, 'iron_mineral_parameter', None)
        if params is None:
            return
        values = IRON_MINERALS.parameter_values()
        k = float(values[STRUVITE_RATE_PARAMETER])
        seeds = {name: float(values[name]) for name in HELD_SEED_MINERALS}
        seed = seeds[HELD_SEED_MINERALS[0]]
        if set(seeds.values()) != {seed}:
            raise NativeDigestionRefusal(f"the held minerals' seeds differ ({seeds}); the seed walk takes them together")
        quarters = round(4 * math.log10(HELD_SEED_KG_M3 / seed))
        walk_parameter(params, (STRUVITE_RATE_PARAMETER,), [share * k for share in STRUVITE_RATE_SHARES], 0.0, 'struvite_rate_continuation')
        walk_parameter(params, HELD_SEED_MINERALS, [HELD_SEED_KG_M3 * (seed / HELD_SEED_KG_M3) ** (i / quarters) for i in range(1, quarters)] + [seed], HELD_SEED_KG_M3, 'mineral_seed_continuation')
        if pyo.value(params[STRUVITE_RATE_PARAMETER]) != k or any((pyo.value(params[n]) != seed for n in seeds)):
            raise NativeDigestionRefusal("the mineral walk ended off the extension's constants")
        reseed_invaders(1.0)
        exclude_sour_root(1.0)
        mineral_path.append(branch(1.0))

    def walk_parameter(params, names, targets, start, stage):
        """Walk mutable kernel parameters ``names`` (one value for all) through ``targets`` (monotone, each the next
        step's goal) from ``start``: each a warm solve; a failed one undone and bisected in log value from the last
        converged one (arithmetically from zero), at most MINERAL_WALK_MAX_HALVINGS times."""
        last = start
        for target in targets:
            trial, halvings = (target, 0)
            while last != target:
                saved = _snapshot(model)
                for name in names:
                    params[name].set_value(trial)
                t0 = time.monotonic()
                condition = _solve(warm, model)
                attempts.append({'lambda': 1.0, 'stage': stage, 'value': trial, 'termination': condition, 'seconds': round(time.monotonic() - t0, 2)})
                if condition == 'optimal':
                    last, trial = (trial, target)
                    continue
                _restore(saved)
                halvings += 1
                if halvings > MINERAL_WALK_MAX_HALVINGS:
                    raise NativeDigestionRefusal(f'{', '.join(names)} could not be walked past {last:g} toward {target:g} (IPOPT {condition} at {trial:g} after {MINERAL_WALK_MAX_HALVINGS} halvings) on the methanogenic branch of the full feed')
                trial = (last * trial) ** 0.5 if last > 0 else 0.5 * trial
    structure = warm_structure(engine_template_name=engine_template_name, feed_components=full, sulfur_iron=sfe_feed, organic_sulfur=bool(sfe_feed and organic_fed), microaeration=oxidant_state is not None, volume_liquid_m3=volume_liquid_m3, volume_vapor_m3=volume_vapor_m3, headspace_wetted_area_m2=headspace_wetted_area_m2 if oxidant_state is not None else None)

    def warm_path():
        """The WARM START (`WARM_START_RULING_ID`): the preset built at the full feed (sulfur and iron in force, the
        dose held at zero as on the cold path) and solved from the previous answer's values; None when it answered,
        else why it did not. Only a healthy methanogenic basin with no trophic group washed out is accepted: a warm
        solve that lands anywhere else is not argued with -- the cold path answers instead."""
        why = _warm_mismatch(warm_start, structure)
        if why is not None:
            return why
        sigma['value'] = 1.0
        built_warm = start(0.0, warm_values=warm_start['values'], lam=1.0)
        if built_warm is None:
            return f'the warm solve was refused: {attempts[-1].get('message')}'
        bind(built_warm)
        out = digester.liquid_phase.properties_out[0]
        washed_now = [k for k in ADM1_BIOMASS if pyo.value(out.conc_mass_comp[k]) < WASHOUT_FLOOR_KG_M3]
        if washed_now:
            return f'the warm solve landed with {washed_now} washed out'
        landed = classify_digester_basin(digester)
        if not landed.get('is_healthy', False):
            return f'the warm solve landed on a {landed.get('classification')} basin'
        path.append({**branch(1.0), 'route': "warm start from this node's previous answer in this walk"})
        return None
    native_start = {'ruling': WARM_START_RULING_ID, 'path': 'cold', 'fallback': None, 'warm_start_source': None if warm_start is None else warm_start.get('source')}
    t_base = time.monotonic()
    if warm_start is None:
        native_start['fallback'] = 'no previous answer of this node in this walk'
        route, sulfur_path = cold_path()
    else:
        first_attempt = len(attempts)
        why_not = warm_path()
        native_start['warm_attempts'] = [dict(a) for a in attempts[first_attempt:]]
        native_start['warm_seconds'] = round(time.monotonic() - t_base, 2)
        native_start['warm_state_map'] = next((a['warm_state_map'] for a in native_start['warm_attempts'] if a.get('warm_state_map')), None)
        if why_not is None:
            native_start['path'] = 'warm'
            route, sulfur_path = ('warm_start', [])
        else:
            native_start['fallback'] = why_not
            sigma['value'] = 0.0 if sigma_steps else 1.0
            route, sulfur_path = cold_path()
    native_start['base_seconds'] = round(time.monotonic() - t_base, 2)
    warm_state = {'structure': structure, 'values': _unfixed_values(model)}
    microaeration = None
    if oxidant_state is not None:
        microaeration = _walk_oxidant_dose(digester, model, warm, oxidant_state, dose_steps, attempts, pyo, after_step=lambda: (reseed_invaders(1.0), exclude_sour_root(1.0)), branch=lambda: branch(1.0))
        if microaeration_bracket is not None:
            microaeration['bracket'] = _microaeration_bracket(digester, model, warm, attempts, pyo, microaeration, srt_d=float(volume_liquid_m3) / float(flow_m3_d), area_band=_bracket_area_band(microaeration_bracket, headspace_wetted_area_m2))
    report = _digester_report(digester, pyo)
    if microaeration is not None:
        report['microaeration'] = microaeration
    report['final_equation_residual_max'] = final_equation_residual(model)
    basin = classify_digester_basin(digester)
    washed = [k for k in ADM1_BIOMASS if report['liquid']['conc_kg_m3'][k] < WASHOUT_FLOOR_KG_M3]
    rp = digester.config.reaction_package
    report['engine_template_name'] = engine_template_name
    report['kinetics'] = dict(rp.kernel_identity) if getattr(rp, 'kernel_identity', None) is not None else {'kernel_id': None, 'source': 'watertap ModifiedADM1 library kinetics (house Davies block)'}
    for key, attribute in (('rate_temperature', 'kernel_rate_temperature'), ('free_ammonia', 'kernel_free_ammonia')):
        if getattr(rp, attribute, None) is not None:
            report['kinetics'][key] = dict(getattr(rp, attribute))
    if kinetic_bases is not None and kinetic_bases['rate_temperature'] is not None:
        solved_k = float(report['liquid']['temperature_k'])
        if abs(solved_k - kinetic_bases['temperature_k']) > 1e-06:
            raise NativeDigestionRefusal(f'the digester solved at {solved_k} K; its rate-temperature factors are for {kinetic_bases['temperature_k']} K')
    if feed_hydrolysis is not None:
        report['kinetics']['feed_hydrolysis'] = {**feed_hydrolysis, 'solved_rates_d': {k: float(pyo.value(rp.kernel_parameter[k])) for k in feed_hydrolysis['rates_d']}}
    report.update({'basin': basin, 'continuation': {'plan': plan, 'path': path, 'sulfur_iron': {'ramped_states': ramped, 'steps': sigma_steps, 'path': sulfur_path, 'route': route}, 'minerals': {'held_seeds': list(HELD_SEED_MINERALS), 'path': mineral_path}}, 'attempts': attempts, 'washed_out_groups': washed, 'session_id': session_id, 'adm1_contents_applied': applied_adm1, 'hydraulic_retention_time_d': float(volume_liquid_m3) / float(flow_m3_d), 'native_start': native_start, 'warm_state': warm_state})
    return report

def _oxidant_view(digester, pyo) -> Dict[str, Any]:
    """What a micro-aeration reader compares between the zero-dose reference and the dosed answer (one solved state)."""
    rep = _digester_report(digester, pyo)
    vap, sulfur = (rep['vapor'], rep['sulfur_iron'])
    day_flow = vap['flow_m3_d']
    return {'pH': rep['pH'], 'biogas_flow_m3_d': day_flow, 'biogas_pressure_pa': vap['pressure_pa'], 'biogas_h2s_ppmv_dry': sulfur['biogas_h2s_ppmv_dry'], 'biogas_kg_d': {k: float(v) * day_flow for k, v in vap['conc_kg_m3'].items()}, 'partial_pressures_pa': dict(vap['partial_pressures_pa']), 'free_h2s_kg_S_m3': sulfur['free_h2s_kg_S_m3'], 'liquor_sulfur_kg_m3': {k: sulfur['liquor_kg_m3'][k] for k in ('S_SO4', 'S_IS', 'S_S0')}, 'sulfur_closure_relative_residual': sulfur['closure']['sulfur_kg_s']['relative_residual']}

def _walk_oxidant_dose(digester, model, warm, oxidant_state, dose_steps, attempts, pyo, *, after_step, branch):
    """Step the stated oxidant dose in from zero on the solved digester (module constants) and return the answer's
    ``microaeration`` block: the unit's own report, the stated oxidant, the solved ZERO-DOSE reference and the path."""
    from watertap_engine_mcp.chemistry.modified_adm1_microaeration import microaeration_report
    flow = digester.vapor_inlet_state[0].flow_vol
    stated = float(oxidant_state['flow_vol_m3_s'])
    if pyo.value(flow) != 0.0 or not flow.fixed:
        raise NativeDigestionRefusal('the oxidant dose was not held at zero through the organic and sulfur walks')
    zero_dose = _oxidant_view(digester, pyo)
    path, last = ([], 0.0)
    for target in dose_steps:
        trial, halvings = (target, 0)
        while last < target:
            saved = _snapshot(model)
            flow.fix(trial * stated)
            t0 = time.monotonic()
            condition = _solve(warm, model)
            attempts.append({'lambda': 1.0, 'stage': 'oxidant_dose_continuation', 'dose_share': trial, 'termination': condition, 'seconds': round(time.monotonic() - t0, 2)})
            if condition == 'optimal':
                last, trial = (trial, target)
                continue
            _restore(saved)
            flow.fix(last * stated)
            halvings += 1
            if halvings > OXIDANT_DOSE_MAX_HALVINGS:
                raise NativeDigestionRefusal(f"the oxidant dose could not be stepped past {last:g} of the stated flow (IPOPT {condition} at {trial:g} after {OXIDANT_DOSE_MAX_HALVINGS} halvings) on the methanogenic branch of the full feed; the dose is the caller's and is not reduced to fit")
            trial = last + 0.5 * (trial - last)
        after_step()
        view = _oxidant_view(digester, pyo)
        path.append({'dose_share': last, **{k: view[k] for k in ('pH', 'biogas_h2s_ppmv_dry')}, 'residual_o2_fraction_wet': float(pyo.value(digester.residual_o2_fraction[0])), 'biomass_kg_cod_m3': branch()['biomass_kg_cod_m3']})
    if pyo.value(flow) != stated:
        raise NativeDigestionRefusal('the oxidant dose walk did not end on the stated flow')
    from watertap.core.solvers import get_solver
    saved = _snapshot(model)
    t0 = time.monotonic()
    condition = _solve(get_solver(options={**OXIDANT_POLISH_OPTIONS}), model)
    attempts.append({'lambda': 1.0, 'stage': 'oxidant_dose_polish', 'dose_share': 1.0, 'termination': condition, 'seconds': round(time.monotonic() - t0, 2)})
    if condition != 'optimal':
        _restore(saved)
    dosed = _oxidant_view(digester, pyo)
    h2s0, h2s1 = (zero_dose['biogas_kg_d']['S_h2s'], dosed['biogas_kg_d']['S_h2s'])
    return {**microaeration_report(digester), 'standing': "NON-governing bracket model until calibrated on a dosed digester (PUBLIC-RULE-6dce80fc160d); the dose is the caller's and is never adjusted", 'oxidant_inlet': dict(oxidant_state), 'zero_dose': zero_dose, 'dosed': dosed, 'biogas_h2s_removal_fraction': 1.0 - h2s1 / h2s0 if h2s0 > 0 else None, 'dose_path': {'steps': list(dose_steps), 'path': path}}

def _bracket_area_band(spec: Dict[str, Any], area_m2) -> Dict[str, Any] | None:
    """The wetted-area band a bracket states ({"wetted_area_m2": {"low", "high", "source"}}), validated; None where the
    bracket states none (the area is then not bracketed). The rated area must lie within its own band."""
    if not isinstance(spec, dict) or set(spec) - {'wetted_area_m2'}:
        raise NativeDigestionRefusal(f'microaeration_bracket reads only an optional wetted_area_m2 band, not {(sorted(spec) if isinstance(spec, dict) else spec)!r}')
    band = spec.get('wetted_area_m2')
    if band is None:
        return None
    if not isinstance(band, dict) or set(band) != {'low', 'high', 'source'} or (not str(band['source']).strip()):
        raise NativeDigestionRefusal('microaeration_bracket.wetted_area_m2 is {low, high, source} (m2 per train: the liquid surface alone to the surface plus the wetted wall band, from stage (b))')
    low, high = (float(band['low']), float(band['high']))
    if not (math.isfinite(low) and math.isfinite(high) and (0 < low <= high)) or not low <= float(area_m2) <= high:
        raise NativeDigestionRefusal(f'the wetted-area band {low!r}-{high!r} m2 must be positive, ordered and hold the rated area {area_m2!r} m2')
    return {'low': low, 'high': high, 'source': str(band['source'])}

def _bracket_view(digester, pyo, zero_dose) -> Dict[str, float]:
    """One bracket point: the quantities a micro-aeration reader compares (``MICROAERATION_BRACKET_METRICS``)."""
    from watertap_engine_mcp.chemistry.modified_adm1_microaeration import microaeration_report
    view, unit = (_oxidant_view(digester, pyo), microaeration_report(digester))
    h2s0 = float(zero_dose['biogas_kg_d']['S_h2s'])
    rereduced = unit['s0_rereduction']
    return {'biogas_h2s_ppmv_dry': view['biogas_h2s_ppmv_dry'], 'biogas_h2s_kg_s_d': view['biogas_kg_d']['S_h2s'], 'biogas_h2s_removal_fraction': 1.0 - view['biogas_kg_d']['S_h2s'] / h2s0 if h2s0 > 0 else None, 'residual_o2_fraction_wet': unit['residual_o2_fraction_wet'], 'liquor_sulfide_kg_s_m3': view['liquor_sulfur_kg_m3']['S_IS'], 'headspace_h2s_oxidised_kg_s_d': unit['headspace_h2s_oxidised_kg_s_d'], 's0_headspace_deposit_kg_s_d': unit['s0_headspace_deposit_kg_s_d'], 's0_to_liquid_kg_s_d': unit['s0_to_liquid_kg_s_d'], 'sulfate_to_liquid_kg_s_d': unit['sulfate_to_liquid_kg_s_d'], 'o2_to_liquid_kg_d': unit['o2_to_liquid_kg_d'], 'liquor_do_kg_m3': unit['liquor_do_kg_m3'], 's0_rereduction_extent_kg_s_d': rereduced['extent_kg_s_d'], 's0_rereduction_extra_o2_kg_d': rereduced['extra_o2_kg_d'], 's0_rereduction_lost_cod_kg_d': rereduced['lost_cod_kg_d'], 'pH': view['pH'], 'methane_kg_cod_d': view['biogas_kg_d']['S_ch4'], 'headspace_oxidation_heat_w': unit['headspace_oxidation_heat_w']}

def _microaeration_bracket(digester, model, warm, attempts, pyo, microaeration, *, srt_d, area_band):
    """Run the bracket on the solved dosed digester (module constants) and return its record: the base point, every
    edge point with how it was reached, and the envelope (min / max and where) of every reported quantity. Each edge is
    reached from the SOLVED base by a warm re-solve (the move halved where it does not converge, every attempt
    recorded), and the base state and value are restored exactly before the next one: the answer the caller gets is the
    base answer, untouched. An edge that cannot be reached is recorded as such and never replaced by a nearer point."""
    from watertap_engine_mcp.chemistry.modified_adm1_microaeration import EXT
    rp = digester.config.reaction_package
    zero_dose = microaeration['zero_dose']
    base_values = rp.microaeration_values()
    area_var = digester.headspace_wetted_area[0]
    base_area = float(pyo.value(area_var))
    base_state = _snapshot(model)
    base_view = _bracket_view(digester, pyo, zero_dose)

    def walk(setter, start, target, label):
        """Move one quantity from ``start`` to ``target`` by warm solves; True when the edge is reached."""
        last, halvings = (0.0, 0)
        trial = 1.0
        while last < 1.0:
            saved = _snapshot(model)
            setter(start + trial * (target - start))
            t0 = time.monotonic()
            condition = _solve(warm, model)
            attempts.append({'lambda': 1.0, 'stage': 'microaeration_bracket', 'edge': label, 'share': trial, 'termination': condition, 'seconds': round(time.monotonic() - t0, 2)})
            if condition == 'optimal':
                last, trial = (trial, 1.0)
                continue
            _restore(saved)
            halvings += 1
            if halvings > MICROAERATION_BRACKET_MAX_HALVINGS:
                return False
            trial = last + 0.5 * (trial - last)
        return True
    points = []

    def edge(parameter, which, value, base, setter, source):
        label = f'{parameter}:{which}'
        if value == base:
            points.append({'parameter': parameter, 'edge': which, 'value': value, 'base_value': base, 'source': source, 'status': 'at_base', **base_view})
            return
        reached = walk(setter, base, value, label)
        record = {'parameter': parameter, 'edge': which, 'value': value, 'base_value': base, 'source': source, 'status': 'solved' if reached else 'not_reached'}
        if reached:
            record.update(_bracket_view(digester, pyo, zero_dose))
        points.append(record)
        _restore(base_state)
        setter(base)
    for name in MICROAERATION_BRACKET_PARAMETERS:
        parameter = EXT.parameter(name)
        low, high = (parameter.low, parameter.high)
        source = f'{parameter.tag} band of process_model_schema.madm1_microaeration'
        if name == 'k_S0_red':
            low, high = (0.0, 1.0 / float(srt_d))
            source = f"HOUSE-MICROAERATION-S0-REREDUCTION-ZERO-BASE-20261010: zero in the base case, the bracket to k = 1/SRT = {high:.6g} 1/d at this digester's {float(srt_d):.6g} d"
        if low is None or high is None:
            raise NativeDigestionRefusal(f'bracket parameter {name} carries no band in the kernel')
        base = float(base_values[name])
        for which, value in (('low', float(low)), ('high', float(high))):
            edge(name, which, value, base, lambda v, n=name: rp.apply_parameter_values({n: v}), source)
    if area_band is not None:
        for which in ('low', 'high'):
            edge('headspace_wetted_area_m2', which, area_band[which], base_area, lambda v: area_var.fix(v), area_band['source'])
    if float(pyo.value(area_var)) != base_area or rp.microaeration_values() != base_values:
        raise NativeDigestionRefusal('the micro-aeration bracket did not put the digester back on its base values')
    solved = [p for p in points if p['status'] in ('solved', 'at_base')]
    envelope = {}
    for metric in MICROAERATION_BRACKET_METRICS:
        values = [(p[metric], f'{p['parameter']}:{p['edge']}') for p in solved if p.get(metric) is not None]
        low_point, high_point = (min(values), max(values))
        envelope[metric] = {'base': base_view[metric], 'min': low_point[0], 'min_at': low_point[1], 'max': high_point[0], 'max_at': high_point[1]}
    return {'basis': 'one constant at a time at the low and the high edge of its band, on the solved dosed digester; the geometry and the stated dose are FIXED; nothing is fitted', 'standing': microaeration['standing'], 'srt_d': float(srt_d), 'base': base_view, 'points': points, 'envelope': envelope, 'not_reached': [f'{p['parameter']}:{p['edge']}' for p in points if p['status'] == 'not_reached']}

def _walk_sulfur(payload: Dict[str, Any]) -> Dict[str, float]:
    """The walk's sulfur (``sulfur_iron_kg_m3``: S_SO4 and S_IS, and the opt-in organic sulfur X_Sorg, kg S/m3 on the
    native inlet volume), validated; empty when the walk carries none, so a sulfur-free node's request is unchanged."""
    sulfur = payload.get('sulfur_iron_kg_m3')
    if sulfur is None:
        return {}
    carried = WALK_SULFUR_STATES + ORGANIC_SULFUR_STATES + (WALK_ELEMENTAL_SULFUR_STATE,)
    if not isinstance(sulfur, dict) or set(sulfur) - set(carried):
        raise NativeDigestionRefusal(f'sulfur_iron_kg_m3 carries {list(carried)} only (kg S/m3; the walk maps no iron), not {(sorted(sulfur) if isinstance(sulfur, dict) else sulfur)!r}')
    values = {k: float(v) for k, v in sulfur.items()}
    if any((not math.isfinite(v) or v < 0 for v in values.values())):
        raise NativeDigestionRefusal(f'sulfur_iron_kg_m3 values are finite and nonnegative, not {values}')
    if float(payload.get('sulfate_mol_l', 0.0) or 0.0) > 0 and any(values.values()):
        raise NativeDigestionRefusal('the feed carries sulfur explicitly (sulfur_iron_kg_m3) AND sulfate in the anion lump (sulfate_mol_l); a walk states its sulfate once, as S_SO4')
    return {k: v for k, v in values.items() if v > 0}

def _walk_iron(payload: Dict[str, Any], sulfur: Dict[str, float]) -> Dict[str, float]:
    """The walk's iron (``walk_iron_kg_m3``: X_HFO_H and S_Fe2 kg Fe/m3, X_FeS kg FeS/m3, X_Fe3PO42 and X_Siderite kg of
    the mineral per m3, on the native inlet volume),
    validated; empty when the walk feeds none, so an iron-free request is unchanged. Iron is the S/Fe digester's, which
    the walk's sulfur builds: iron without sulfur refuses."""
    iron = payload.get('walk_iron_kg_m3')
    if iron is None:
        return {}
    if not isinstance(iron, dict) or set(iron) - set(WALK_IRON_FEED_STATES):
        raise NativeDigestionRefusal(f'walk_iron_kg_m3 carries {list(WALK_IRON_FEED_STATES)} only (kg Fe/m3; X_FeS, X_Fe3PO42, X_Siderite kg of the mineral/m3), not {(sorted(iron) if isinstance(iron, dict) else iron)!r}')
    values = {k: float(v) for k, v in iron.items()}
    if any((not math.isfinite(v) or v < 0 for v in values.values())):
        raise NativeDigestionRefusal(f'walk_iron_kg_m3 values are finite and nonnegative, not {values}')
    values = {k: v for k, v in values.items() if v > 0}
    if values and (not sulfur):
        raise NativeDigestionRefusal("the walk feeds iron to a digester whose feed carries no sulfur: the iron is the S/Fe digester's, built only for a feed with sulfur (S_SO4, S_IS or X_Sorg)")
    return values

def _walk_struvite(payload: Dict[str, Any], sulfur: Dict[str, float]) -> Dict[str, float]:
    """The walk's recycled struvite (``walk_struvite_kg_m3``: X_struv, kg anhydrous MgNH4PO4/m3 on the native inlet
    volume), validated; empty when the walk feeds none. Struvite is the S/Fe digester's: fed without sulfur, refused."""
    struvite = payload.get('walk_struvite_kg_m3')
    if struvite is None:
        return {}
    if not isinstance(struvite, dict) or set(struvite) - {WALK_STRUVITE_STATE}:
        raise NativeDigestionRefusal(f'walk_struvite_kg_m3 carries {WALK_STRUVITE_STATE} only (kg MgNH4PO4/m3), not {(sorted(struvite) if isinstance(struvite, dict) else struvite)!r}')
    values = {k: float(v) for k, v in struvite.items()}
    if any((not math.isfinite(v) or v < 0 for v in values.values())):
        raise NativeDigestionRefusal(f'walk_struvite_kg_m3 values are finite and nonnegative, not {values}')
    values = {k: v for k, v in values.items() if v > 0}
    if values and (not sulfur):
        raise NativeDigestionRefusal("the walk feeds struvite to a digester whose feed carries no sulfur: struvite is the S/Fe digester's, built only for a feed with sulfur (S_SO4, S_IS or X_Sorg)")
    return values

def solve_native_digestion(payload: Dict[str, Any]) -> Dict[str, Any]:
    """One digester node, per train: translate, digest, translate back. ``payload``:

    ``flow_m3_d`` (per train), ``temperature_k`` (digester), ``asm2d_kg_m3`` (the native feed), ``cations_kmol_m3``,
    ``anions_kmol_m3`` (charge equivalents of the carried strong ions), ``asm2d_fixes`` ({param: value}),
    ``translator`` ({heaviside_k, source}), ``geometry`` ({volume_liquid_m3, volume_vapor_m3}) and ``evidence``
    ({sizing_reference, seed_bindings, sizing_outputs}), optional ``continuation``, ``budget_seconds`` and
    ``sodium_mol_l`` (carried feed Na on the native inlet-volume basis, inhibition only; Na charge stays in the lump),
    ``calcium_mol_l`` / ``sulfate_mol_l`` (carried feed ions already in the lumps; their z-squared correction for I),
    ``adm1_fixes`` ({"Ni"|"Pi"|"Ci": {state: kmol/kg COD}, "source": ...}: mADM1 element contents applied to all three
    blocks alike -- the inert N/P and lipid P of THIS feed, which the forward translator passes through unbooked),
    and ``engine_template_name`` (``adm1_uasb``, the default: WaterTAP's library kinetics on the house Davies block;
    ``adm1_cstr``: the shared biokinetic kernel, the digester design model -- Wave 2 item 6). The translators read the
    element contents of the house Davies block either way; the kernel block closes its carriers on the same Vars.
    Optional ``sulfur_iron_kg_m3`` ({S_SO4, S_IS, X_Sorg}, kg S/m3 on the native inlet volume): the walk's sulfate,
    sulfide and (opt-in) particulate organic sulfur, joined to the translated feed (the adm1_cstr S/Fe build, its
    organic-sulfur variant when X_Sorg is fed; sulfur then never also in ``sulfate_mol_l``); the digestate's sulfate and
    sulfide come back as ``digestate_asm2d.digestate_sulfur_kg_m3`` (with ``X_Sorg``, kg S/m3, only when the
    organic-sulfur variant answered) and the SRB guilds cross the reverse interface as ``SRB_REVERSE_CARRIER``.
    Optional ``walk_iron_kg_m3`` ({X_HFO_H, S_Fe2 kg Fe/m3, X_FeS kg FeS/m3}; with sulfur only): the walk's dosed and
    recycled iron (``WALK_IRON_FEED_STATES``); the digestate's iron comes back as ``digestate_asm2d.digestate_iron_kg_m3``
    (``WALK_IRON_RETURNED_STATES``) with the P its P-bound HFO holds (``digestate_hfo_bound_p_kg_m3``), its S0 in
    ``digestate_sulfur_kg_m3``.
    Optional ``walk_struvite_kg_m3`` ({X_struv: kg MgNH4PO4/m3}; with sulfur only): the walk's recycled struvite. Every
    S/Fe answer returns the digestate's as ``digestate_asm2d.digestate_struvite_kg_m3`` (``WALK_STRUVITE_STATE``), after
    its share of the reverse mapping's phosphorus draw (``digestate_asm2d.reverse_phosphate_draw``).
    Optional ``oxidant_inlet`` ({"kmol_d": {O2, N2, Ar, CO2}, "temperature_K", "pressure_Pa", "source"}, PER TRAIN) with
    ``headspace_wetted_area_m2`` (per train, bound in ``evidence`` like the volumes): digester micro-aeration, the
    adm1_cstr S/Fe digester's opt-in variant (a NON-governing bracket model). The answer's ``digester.microaeration``
    carries the balances and the zero-dose reference; the liquid S0 comes back as ``digestate_sulfur_kg_m3['S_S0']`` (as
    it does where the walk itself feeds ``S_S0``), and the liquor's dissolved O2 is reported, never translated.
    Optional ``warm_start`` (a previous answer's ``warm_state`` with its ``source``; `WARM_START_RULING_ID`): the
    digester starts from it (``solve_digester``); ``digester.native_start`` says which path answered. The answer
    carries ``warm_state``, its own solved values, for the caller to send back as the next request's warm start.
    Optional ``hydrolysis_rates_d`` ({q_ch_hyd, q_pr_hyd, q_li_hyd: 1/d, "source"}; adm1_cstr only): the library
    feeds' own first-order hydrolysis (`FEED_HYDROLYSIS_RULING_ID`), rebound on the built kernel before any solve; the
    answer's ``digester.kinetics.feed_hydrolysis`` records the rates sent and the rates the solved block held.
    Optional ``rate_temperature_basis`` / ``free_ammonia_basis`` (adm1_cstr only): a named kernel basis
    (``solve_digester``); ``digester.kinetics.rate_temperature`` / ``free_ammonia`` record what answered.
    """
    for key in ('flow_m3_d', 'temperature_k', 'asm2d_kg_m3', 'cations_kmol_m3', 'anions_kmol_m3', 'translator', 'geometry', 'evidence'):
        if key not in payload:
            raise NativeDigestionRefusal(f'native digestion request lacks {key}')
    template = str(payload.get('engine_template_name') or 'adm1_uasb')
    if template not in SINGLE_CSTR_TEMPLATES:
        raise NativeDigestionRefusal(f'{template!r} is not a single-CSTR digester preset ({sorted(SINGLE_CSTR_TEMPLATES)})')
    heaviside = payload['translator']
    if not str(heaviside.get('source') or '').strip() or not float(heaviside.get('heaviside_k') or 0) > 0:
        raise NativeDigestionRefusal("the translator's heaviside_k is a declared numerical conditioning with its source")
    flow, temperature = (float(payload['flow_m3_d']), float(payload['temperature_k']))
    sulfur = _walk_sulfur(payload)
    iron = _walk_iron(payload, sulfur)
    struvite = _walk_struvite(payload, sulfur)
    fixes = dict(payload.get('asm2d_fixes') or {})
    adm1_fixes = payload.get('adm1_fixes')
    if adm1_fixes and (not str(adm1_fixes.get('source') or '').strip()):
        raise NativeDigestionRefusal("adm1_fixes override WaterTAP's mADM1 element contents and must name their source")
    split = payload.get('biochemical_split')
    t1_fixes = fixes
    if split:
        if not str(split.get('source') or '').strip():
            raise NativeDigestionRefusal("a biochemical_split re-partitions the translator's output and names its source")
        m = _asm2d_blocks()
        _apply_asm2d_fixes(m, fixes)
        _apply_adm1_fixes(m.fs.rd, adm1_fixes)
        own = interface_element_demand(m, payload['asm2d_kg_m3'])
        t1_fixes = {**fixes, **{k: v for k, v in (('i_CSF', own['carbon_consistent_i_CSF']), ('i_CXS', own['carbon_consistent_i_CXS'])) if v is not None}}
    t1 = translate_asm2d_to_adm1(flow_m3_d=flow, temperature_k=temperature, asm2d_kg_m3=payload['asm2d_kg_m3'], cations_kmol_m3=payload['cations_kmol_m3'], anions_kmol_m3=payload['anions_kmol_m3'], asm2d_fixes=t1_fixes, heaviside_k=float(heaviside['heaviside_k']), adm1_fixes=adm1_fixes)
    if split:
        t1 = apply_biochemical_split(t1, split, walk_fixes=fixes, t1_fixes=t1_fixes, asm2d_kg_m3=payload['asm2d_kg_m3'], adm1_fixes=adm1_fixes)
    if sulfur:
        t1 = {**t1, 'conc_kg_m3': {**t1['conc_kg_m3'], **sulfur}, 'walk_sulfur_kg_m3': dict(sulfur)}
    if iron:
        t1 = {**t1, 'conc_kg_m3': {**t1['conc_kg_m3'], **iron}, 'walk_iron_kg_m3': dict(iron)}
    if struvite:
        t1 = {**t1, 'conc_kg_m3': {**t1['conc_kg_m3'], **struvite}, 'walk_struvite_kg_m3': dict(struvite)}
    ad = _solve_with_start_fallbacks(solve_digester, dict(flow_m3_d=flow, temperature_k=temperature, adm1_feed=t1, volume_liquid_m3=payload['geometry']['volume_liquid_m3'], volume_vapor_m3=payload['geometry']['volume_vapor_m3'], evidence=payload['evidence'], continuation=payload.get('continuation'), budget_seconds=int(payload.get('budget_seconds', 600)), adm1_fixes=adm1_fixes, sodium_mol_l=float(payload.get('sodium_mol_l', 0.0)), calcium_mol_l=float(payload.get('calcium_mol_l', 0.0)), sulfate_mol_l=float(payload.get('sulfate_mol_l', 0.0)), engine_template_name=template, oxidant_inlet=payload.get('oxidant_inlet'), headspace_wetted_area_m2=payload.get('headspace_wetted_area_m2'), microaeration_bracket=payload.get('microaeration_bracket'), warm_start=payload.get('warm_start'), hydrolysis_rates_d=payload.get('hydrolysis_rates_d'), rate_temperature_basis=payload.get('rate_temperature_basis'), free_ammonia_basis=payload.get('free_ammonia_basis')))
    warm_state = ad.pop('warm_state', None)
    dosed = payload.get('oxidant_inlet') is not None
    carried_s0 = dosed or WALK_ELEMENTAL_SULFUR_STATE in sulfur or bool(iron)
    liquor = ad['liquid']['conc_kg_m3']
    core = {k: v for k, v in liquor.items() if k not in SFE_STATES and k not in ORGANIC_SULFUR_STATES and (k not in MICROAERATION_STATES)}
    srb = math.fsum((float(liquor.get(k, 0.0)) for k in SRB_BIOMASS)) if sulfur else 0.0
    if srb:
        core[SRB_REVERSE_CARRIER] = float(core[SRB_REVERSE_CARRIER]) + srb
    phosphate_draw = None
    if sulfur:
        core, phosphate_draw = reverse_phosphate_draw(adm1_kg_m3=core, struvite_kg_m3=float(liquor.get(WALK_STRUVITE_STATE, 0.0)), asm2d_fixes=fixes, adm1_fixes=adm1_fixes)
    t2 = translate_adm1_to_asm2d(flow_m3_d=ad['liquid']['flow_m3_d'], temperature_k=ad['liquid']['temperature_k'], adm1_kg_m3=core, cations_kmol_m3=ad['liquid']['cations_kmol_m3'], anions_kmol_m3=ad['liquid']['anions_kmol_m3'], asm2d_fixes=fixes, adm1_fixes=adm1_fixes)
    if sulfur:
        from watertap_engine_mcp.chemistry.modified_adm1_sfe import MW_FE, MW_S
        fed_s = sum(sulfur.values())
        uncarried_s = (0.0 if carried_s0 else float(liquor.get('S_S0', 0.0))) + (0.0 if iron else MW_S / (MW_S + MW_FE) * (float(liquor.get('X_FeS', 0.0)) + float(liquor.get('X_Mackinawite', 0.0))))
        if uncarried_s > WALK_UNCARRIED_SULFUR_SHARE * fed_s:
            raise NativeDigestionRefusal(f'the digestate holds {uncarried_s:.3g} kg S/m3 as S0/FeS ({uncarried_s / fed_s:.2%} of the {fed_s:.3g} kg S/m3 fed), which the walk has no state for; refused rather than dropped')
        t2['digestate_sulfur_kg_m3'] = {k: float(liquor[k]) for k in WALK_SULFUR_STATES}
        t2['digestate_sulfur_kg_m3'].update({k: float(liquor[k]) for k in ORGANIC_SULFUR_STATES if k in liquor})
        if carried_s0:
            t2['digestate_sulfur_kg_m3'][WALK_ELEMENTAL_SULFUR_STATE] = float(liquor[WALK_ELEMENTAL_SULFUR_STATE])
        t2['srb_biomass_to_carrier_kg_cod_m3'] = {'carrier': SRB_REVERSE_CARRIER, 'value': srb}
        t2['digestate_struvite_kg_m3'] = {WALK_STRUVITE_STATE: phosphate_draw['struvite_kg_m3']}
        t2['reverse_phosphate_draw'] = phosphate_draw
    if iron:
        from watertap_engine_mcp.chemistry.modified_adm1_sfe import IRON_MINERAL_FE_SHARE, MW_FE, MW_S, sfe_design_parameter_values
        fed_fe = float(iron.get('X_HFO_H', 0.0)) + float(iron.get('S_Fe2', 0.0)) + MW_FE / (MW_S + MW_FE) * float(iron.get('X_FeS', 0.0)) + math.fsum((share * float(iron.get(k, 0.0)) for k, share in IRON_MINERAL_FE_SHARE.items()))
        ferric = float(liquor.get('S_Fe3', 0.0))
        if ferric > WALK_UNCARRIED_FERRIC_SHARE * fed_fe:
            raise NativeDigestionRefusal(f'the digestate holds {ferric:.3g} kg Fe/m3 as dissolved ferric iron ({ferric / fed_fe:.2%} of the {fed_fe:.3g} kg Fe/m3 fed), which the walk has no state for; refused rather than dropped')
        values, _ = sfe_design_parameter_values()
        t2['digestate_iron_kg_m3'] = {k: float(liquor.get(k, 0.0)) for k in WALK_IRON_RETURNED_STATES}
        t2['digestate_hfo_bound_p_kg_m3'] = {k: float(liquor.get(k, 0.0)) * float(values[parameter]) for k, parameter in HFO_BOUND_P_CONTENT.items()}
    t2['sulfur_iron_not_translated_kg_m3'] = {k: float(v) for k, v in liquor.items() if k in SFE_STATES and float(v) > 1e-06 and (not (sulfur and (k in WALK_SULFUR_STATES or k in SRB_BIOMASS or k == WALK_STRUVITE_STATE))) and (not (carried_s0 and k == WALK_ELEMENTAL_SULFUR_STATE)) and (not (iron and k in WALK_IRON_RETURNED_STATES))}
    if dosed:
        t2['microaeration_not_translated_kg_m3'] = {k: float(liquor[k]) for k in MICROAERATION_STATES if k in liquor}
    return {'schema_version': 'native_digestion_result/v1', 'translated_feed': t1, 'digester': ad, 'digestate_asm2d': t2, 'translator': {'heaviside_k': float(heaviside['heaviside_k']), 'source': heaviside['source']}, 'adm1_fixes': adm1_fixes, 'warm_state': warm_state}
