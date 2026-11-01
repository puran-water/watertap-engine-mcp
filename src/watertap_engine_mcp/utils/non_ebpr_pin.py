"The qualified native route (engineering_utils ``process_balance.adapters.biology.GROUP_TOPOLOGIES``: MLE and\naerobic-only, neither with an anaerobic zone) is the non-EBPR domain: X_PAO, X_PHA and X_PP are fed at zero, and zero is\nan exact steady state of them. It is not the only one: the interior-point solve leaves them at its barrier and can land\non a self-sustaining PAO population at a feed a rounding away from one it did not (ZLR c155 W2; LB TEX 2026-09-20). The\npin makes the zero branch the only one the solve can reach:\n\n* every state block's ``conc_mass_comp[t, pool]`` is fixed at 0;\n* the reactions that move a pool (read off the installed stoichiometry, never typed: WaterTAP 1.8 R10-R17) have their\n  ``reaction_rate``, ``rate_reaction_extent`` and, for the pools, ``rate_reaction_generation`` fixed at 0. Each of\n  those rates carries a pool factor, so 0 is their value at the pin; their ``rate_expression`` rows are deactivated\n  explicitly, as they read 0/0 there (R10's X_PP/(KP_PP X_PAO + X_PP));\n* every active equality row that is then the CONSTANT 0 = 0 (its standard representation, the pinned values\n  substituted, has no variable term) is deactivated with them, so the Jacobian carries no zero row; one that is a\n  nonzero constant (a pool fed above zero) is refused. Degrees of freedom are unchanged, or the pin is refused.\n\n`verify_pinned_solution` re-reads every deactivated row but the rate rows at the solved point: each must hold, which is\nthe statement that the pinned answer is a steady state of the unpinned equations."

from __future__ import annotations

from typing import Any, Dict, Iterable, List

#: The ruling that built the pin; carried on every record.
PIN_RULING_ID = 'PUBLIC-RULE-74ee26cc55e2'
NON_EBPR_POOLS = ("X_PAO", "X_PHA", "X_PP")
#: A deactivated row's residual at the solved point, above which the pinned answer is not a steady state of the
#: unpinned equations: the strict polish's own constr_viol_tol (actions.py native_polish_options).
PINNED_ROW_RESIDUAL_TOLERANCE = 1e-11


class NonEbprPinError(ValueError):
    """The pin cannot be applied to this model as it stands; the message names what moved."""


def pool_reactions(reaction_package: Any, pools: Iterable[str] = NON_EBPR_POOLS) -> List[str]:
    """The reactions with a nonzero stoichiometric coefficient on any pool, in the package's own order."""
    import pyomo.environ as pyo

    pools = tuple(pools)
    stoichiometry = reaction_package.rate_reaction_stoichiometry
    moving = []
    for reaction in reaction_package.rate_reaction_idx:
        for pool in pools:
            coefficient = stoichiometry.get((reaction, "Liq", pool), 0)
            if float(pyo.value(coefficient)) != 0.0:
                moving.append(str(reaction))
                break
    return moving


def _index_parts(index: Any) -> tuple:
    return index if isinstance(index, tuple) else (index,)


def _constant_zero(constraint: Any) -> tuple[bool, float | None]:
    """(no variable term left, its constant residual) for an equality row, the fixed values substituted."""
    import pyomo.environ as pyo
    from pyomo.repn import generate_standard_repn

    repn = generate_standard_repn(constraint.body, compute_values=True, quadratic=True)
    if any(float(c) != 0.0 for c in repn.linear_coefs) or any(float(c) != 0.0 for c in repn.quadratic_coefs):
        return False, None
    if repn.nonlinear_expr is not None:
        return False, None
    return True, float(repn.constant) - float(pyo.value(constraint.upper))


def pin_non_ebpr_pools(model: Any, reactors: Iterable[Any], reaction_package: Any,
                       pools: Iterable[str] = NON_EBPR_POOLS, *,
                       newly_fixed: List[Any] | None = None) -> tuple[Dict[str, Any], List[Any]]:
    """Pin ``pools`` at zero on ``model`` (built, with every design variable fixed, before initialisation). Returns
    ``(record, rows)``: what was fixed and deactivated, by family, with the degrees of freedom; and the deactivated
    rows themselves, for `verify_pinned_solution`. ``newly_fixed`` collects the variables the pin fixed that were free
    before it (the feed's own pools were fixed already), for `unpin_non_ebpr_pools`."""
    import pyomo.environ as pyo
    from idaes.core.util.model_statistics import degrees_of_freedom
    from pyomo.common.collections import ComponentSet

    pools = tuple(pools)
    reactors = list(reactors)
    dof_before = degrees_of_freedom(model)
    reactions = pool_reactions(reaction_package, pools)
    fixed: Dict[str, int] = {}
    pinned = ComponentSet()

    def pin(var: Any, family: str) -> None:
        if var in pinned:
            return
        if var.fixed and float(pyo.value(var)) != 0.0:
            raise NonEbprPinError(f"{var.name} is fixed at {pyo.value(var)!r}: the qualified non-EBPR route feeds "
                                  "the PAO pools at exactly zero")
        if newly_fixed is not None and not var.fixed:
            newly_fixed.append(var)
        var.fix(0.0)
        pinned.add(var)
        fixed[family] = fixed.get(family, 0) + 1

    for var in model.component_data_objects(pyo.Var, descend_into=True):
        component = var.parent_component()
        if component.local_name == "conc_mass_comp" and _index_parts(var.index())[-1] in pools:
            pin(var, "conc_mass_comp")
    deactivated: Dict[str, int] = {}
    rows: List[Any] = []
    for unit in reactors:
        control_volume = unit.control_volume
        for time in model.fs.time:
            block = control_volume.reactions[time]
            for reaction in reactions:
                pin(block.reaction_rate[reaction], "reaction_rate")
                block.rate_expression[reaction].deactivate()
                rows.append(block.rate_expression[reaction])
                deactivated["rate_expression"] = deactivated.get("rate_expression", 0) + 1
                pin(control_volume.rate_reaction_extent[time, reaction], "rate_reaction_extent")
            for pool in pools:
                pin(control_volume.rate_reaction_generation[time, "Liq", pool], "rate_reaction_generation")
    for constraint in list(model.component_data_objects(pyo.Constraint, active=True, descend_into=True)):
        if not constraint.equality:
            continue
        constant, residual = _constant_zero(constraint)
        if not constant:
            continue
        if residual != 0.0:
            raise NonEbprPinError(f"{constraint.name} reads {residual!r} = 0 at the pin: a pinned pool is fed or held "
                                  "above zero, outside the qualified non-EBPR domain")
        constraint.deactivate()
        rows.append(constraint)
        family = constraint.parent_component().local_name
        deactivated[family] = deactivated.get(family, 0) + 1
    dof_after = degrees_of_freedom(model)
    if dof_after != dof_before:
        raise NonEbprPinError(f"pinning the PAO pools moved the degrees of freedom from {dof_before} to {dof_after}: "
                              f"fixed {fixed}, deactivated {deactivated}")
    return ({"ruling": PIN_RULING_ID, "pools": list(pools), "pool_reactions": reactions,
             "fixed": dict(sorted(fixed.items())), "deactivated": dict(sorted(deactivated.items())),
             "dof": dof_after, "fixed_total": sum(fixed.values()), "deactivated_total": sum(deactivated.values())},
            rows)


def unpin_non_ebpr_pools(model: Any, rows: List[Any], newly_fixed: List[Any], *, dof: int) -> Dict[str, Any]:
    """Undo `pin_non_ebpr_pools` in place on its solved model: the variables it fixed free again and the rows it
    deactivated active again (the unpinned equations, solved values kept as the start). Refused if the degrees of
    freedom then differ from the pinned model's ``dof`` (the pin's own count)."""
    from idaes.core.util.model_statistics import degrees_of_freedom

    for var in newly_fixed:
        var.unfix()
    for row in rows:
        row.activate()
    after = degrees_of_freedom(model)
    if after != dof:
        raise NonEbprPinError(f"unpinning the PAO pools moved the degrees of freedom from {dof} to {after}")
    return {"freed_total": len(newly_fixed), "reactivated_total": len(rows), "dof": after}


def zero_jacobian_rows(model: Any) -> List[str]:
    """Names of the active equality rows whose Jacobian is all zero at the current point (PyNumero): none after the
    pin. A check, never an input to the solve."""
    import idaes  # noqa: F401 - registers IDAES's binary directory, where PyNumero finds pynumero_ASL
    import numpy as np
    import pyomo.environ as pyo
    from pyomo.contrib.pynumero.interfaces.pyomo_nlp import PyomoNLP

    objectives = list(model.component_data_objects(pyo.Objective, active=True))
    added = None
    if not objectives:
        model._non_ebpr_pin_probe_objective = added = pyo.Objective(expr=0.0)
    try:
        nlp = PyomoNLP(model)
        jacobian = nlp.evaluate_jacobian_eq().tocsr()
        norms = np.asarray(abs(jacobian).sum(axis=1)).ravel()
        names = nlp.get_pyomo_equality_constraints()
        return [names[i].name for i in np.flatnonzero(norms == 0.0)]
    finally:
        if added is not None:
            model.del_component(model._non_ebpr_pin_probe_objective)


def verify_pinned_solution(model: Any, record: Dict[str, Any], rows: List[Any]) -> Dict[str, Any]:
    """The pinned rows at the solved point: every pinned pool exactly zero and every row the pin deactivated but the
    rate rows holding to ``PINNED_ROW_RESIDUAL_TOLERANCE`` in its own (transformed) units. Refused otherwise."""
    import pyomo.environ as pyo

    pools = tuple(record["pools"])
    worst, checked = 0.0, 0
    for constraint in rows:
        if constraint.parent_component().local_name == "rate_expression":
            continue
        residual = abs(float(pyo.value(constraint.body)) - float(pyo.value(constraint.upper)))
        checked += 1
        worst = max(worst, residual)
    nonzero = [var.name for var in model.component_data_objects(pyo.Var, descend_into=True)
               if var.parent_component().local_name == "conc_mass_comp"
               and _index_parts(var.index())[-1] in pools and float(pyo.value(var)) != 0.0]
    if nonzero or worst > PINNED_ROW_RESIDUAL_TOLERANCE:
        raise NonEbprPinError(f"the pinned solve is not a steady state of the unpinned equations: pools off zero "
                              f"{nonzero[:5]}, largest deactivated-row residual {worst!r}")
    return {"deactivated_rows_checked": checked, "largest_residual": worst,
            "tolerance": PINNED_ROW_RESIDUAL_TOLERANCE}
