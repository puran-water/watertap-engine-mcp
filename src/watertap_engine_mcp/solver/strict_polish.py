"""Records for the direct ModifiedASM2d MBR rating's solves, and the row-resolution audit.

The strict native polish (``tol`` 1e-9, ``constr_viol_tol`` 1e-11, no bound
relaxation) asks IPOPT for an ABSOLUTE unscaled constraint violation of 1e-11.
That is only reachable on a row whose float64 resolution is finer than it. A
row whose largest additive term is ~2.9e6 has a resolution of 2**-31 = 4.66e-10,
so it can pass only when its evaluation cancels exactly: a lottery, not a
tolerance (LB TEX payload-08, 2026-09-18: IPOPT stalled at 4.66e-10, entered
restoration and exited "Restoration Failed").

This module does not relax anything. It measures each row's resolution against
the tolerance so a failure names the rows that cannot meet it, and it turns
IPOPT's own exit into a record the result carries.

It also carries the polish's warm start: the polish refines a converged point,
so it starts from that point's own multipliers rather than from IPOPT's cold
defaults (see ``WARM_START_OPTIONS``).
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List


def _numeric_constant(node: Any) -> float | None:
    """Value of a node that cannot vary during the solve, else None."""

    if isinstance(node, (int, float)):
        return float(node)
    import pyomo.environ as pyo

    try:
        if not node.is_potentially_variable() or node.is_fixed():
            return float(pyo.value(node))
    except AttributeError:
        return None
    return None


def additive_term_values(expr: Any, scale: float = 1.0) -> Iterator[float]:
    """Values of the additive terms of ``expr`` at the current point.

    Sums and negations are flattened, and a product with a constant factor
    carries the factor into its operand, so a row rescaled by
    ``constraint_scaling_transform`` (``body * v``) yields its terms times v.
    Any other node is one term.
    """

    import pyomo.environ as pyo
    from pyomo.core.expr import numeric_expr as ne

    if isinstance(expr, ne.SumExpression):
        for arg in expr.args:
            yield from additive_term_values(arg, scale)
        return
    if isinstance(expr, ne.NegationExpression):
        yield from additive_term_values(expr.args[0], -scale)
        return
    if isinstance(expr, ne.ProductExpression):
        left, right = expr.args
        left_constant = _numeric_constant(left)
        if left_constant is not None:
            yield from additive_term_values(right, scale * left_constant)
            return
        right_constant = _numeric_constant(right)
        if right_constant is not None:
            yield from additive_term_values(left, scale * right_constant)
            return
    yield scale * float(pyo.value(expr))


def row_float_resolution(constraint: Any) -> float | None:
    """One float64 ulp of the row's largest additive term: the finest nonzero residual it can hold.

    The residual of a sum is computed at the magnitude of its terms, so below
    this value the row is either exactly zero or not representable. It is a
    lower bound: accumulated rounding can make the attainable floor coarser.
    """

    try:
        largest = max((abs(value) for value in additive_term_values(constraint.body)), default=0.0)
    except (ValueError, ZeroDivisionError, TypeError, OverflowError):
        return None
    if not math.isfinite(largest):
        return None
    return math.ulp(largest) if largest > 0.0 else 0.0


def row_resolution_audit(model: Any, *, tolerance: float, top: int = 5) -> Dict[str, Any]:
    """Every active equality row's float resolution against an absolute tolerance.

    ``unresolvable`` lists the rows whose resolution exceeds the tolerance: they
    can satisfy it only by exact cancellation. ``coarsest`` lists the ``top``
    rows by resolution so the headroom of the rest is visible.
    """

    import pyomo.environ as pyo

    rows: List[Dict[str, Any]] = []
    for constraint in model.component_data_objects(pyo.Constraint, active=True, descend_into=True):
        if not constraint.equality:
            continue
        resolution = row_float_resolution(constraint)
        if resolution is None:
            continue
        try:
            residual = abs(float(pyo.value(constraint.body) - pyo.value(constraint.lower)))
        except (ValueError, ZeroDivisionError, TypeError):
            residual = None
        if residual is not None and not math.isfinite(residual):
            residual = None  # the record is JSON with allow_nan=False downstream
        rows.append({
            "constraint": constraint.name,
            "resolution": resolution,
            "headroom": (tolerance / resolution) if resolution > 0.0 else None,
            "residual": residual,
        })
    rows.sort(key=lambda row: (-row["resolution"], row["constraint"]))
    unresolvable = [row for row in rows if row["resolution"] > tolerance]
    return {
        "method": "one float64 ulp of the row's largest additive term at the warm start (a lower bound)",
        "tolerance": tolerance,
        "rows_audited": len(rows),
        "unresolvable_count": len(unresolvable),
        "unresolvable": unresolvable[:20],
        "coarsest": rows[:top],
    }


_SUMMARY_FIELDS = {
    "unscaled_constraint_violation": r"Constraint violation\.+:\s*\S+\s+(\S+)",
    "unscaled_dual_infeasibility": r"Dual infeasibility\.+:\s*\S+\s+(\S+)",
    "unscaled_complementarity": r"Complementarity\.+:\s*\S+\s+(\S+)",
}


def ipopt_log_summary(path: Path | None) -> Dict[str, Any]:
    """What IPOPT itself reported for the last solve written to ``path``."""

    if path is None or not Path(path).is_file():
        return {"ipopt_log": None}
    text = Path(path).read_text(errors="replace")
    lines = text.splitlines()
    exits = [line.strip() for line in lines if line.startswith("EXIT:")]
    iterations = re.findall(r"Number of Iterations\.+:\s*(\d+)", text)
    summary: Dict[str, Any] = {
        "ipopt_exit": exits[-1] if exits else None,
        "iterations": int(iterations[-1]) if iterations else None,
        "restoration_iterations": sum(1 for line in lines if re.match(r"^\s*\d+r\s", line)),
        "step_computation_failures": sum(
            1 for line in lines if "Problem in step computation" in line
        ),
        "abort_notes": [line.strip() for line in lines if "Abort" in line][:5],
    }
    for key, pattern in _SUMMARY_FIELDS.items():
        found = re.findall(pattern, text)
        try:
            value = float(found[-1]) if found else None
        except ValueError:
            value = None
        summary[key] = value if value is not None and math.isfinite(value) else None
    return summary


def solve_attempt_record(
    phase: str,
    *,
    options: Dict[str, Any],
    results: Any = None,
    exception: BaseException | None = None,
    log_path: Path | None = None,
    loaded: bool | None = None,
) -> Dict[str, Any]:
    """One solve attempt as the result carries it: options, Pyomo's verdict and IPOPT's exit."""

    record: Dict[str, Any] = {"phase": phase, "solver_options": dict(options)}
    if results is not None:
        record["solver_status"] = str(results.solver.status)
        record["termination_condition"] = str(results.solver.termination_condition)
        message = getattr(results.solver, "message", None)
        record["solver_message"] = str(message) if message is not None else None
    if exception is not None:
        record["exception"] = f"{type(exception).__name__}: {exception}"
    if loaded is not None:
        record["solution_loaded"] = loaded
    record.update(ipopt_log_summary(log_path))
    return record


# --- Warm start of the strict polish from the solve it refines ---------------
#
# The polish refines a CONVERGED point. Restarting IPOPT there cold (bound
# multipliers initialised to 1) under mu_strategy "adaptive" hands the barrier
# oracle a meaningless complementarity: LB TEX heated-replay request 14
# (2026-09-18) started at inf_pr 1.34e-9 with cmpl 2.67e6, the quality-function
# oracle chose mu = 2.54, the first step (||d|| 47) left the solution (inf_pr
# 3.63), and restoration ran 597 of 600 iterations into a PAO-populated state.
# Whether the cold oracle happens to pick a small mu depends on scaling (payload
# 08 picked lg(mu) -4.1; request 14 with the base solve's constraint scaling
# picked -4.1 too): luck, not robustness. Warm-starting from the refined solve's
# own multipliers -- exactly zero on these square solves, which IPOPT reports
# with zero dual infeasibility and complementarity -- gives the oracle the
# solution's complementarity: request 14 then chose lg(mu) -3.9, then -10, and
# converged in 2 iterations with no restoration. Not the tolerance (the cold
# restart fails identically at constr_viol_tol 1e-8) and not the zero-pool
# bounds (the first step is a full barrier step, alpha_pr 0.53, not a
# fraction-to-boundary cut).
WARM_START_OPTIONS: Dict[str, Any] = {
    "warm_start_init_point": "yes",
    # The polish's bound_push is 1e-12 so trace pools stay where the refined
    # solve left them; the warm-start pushes (default 1e-3) get the same value.
    "warm_start_bound_push": 1e-12,
    "warm_start_mult_bound_push": 1e-12,
}
_MULTIPLIER_IMPORTS = ("ipopt_zL_out", "ipopt_zU_out", "dual")
_WARM_START_EXPORTS = ("ipopt_zL_in", "ipopt_zU_in")


def declare_multiplier_import(model: Any) -> None:
    """Have every IPOPT solve on ``model`` return its bound and constraint multipliers.

    Import-only suffixes: nothing is sent to IPOPT, so no solve changes.
    """

    import pyomo.environ as pyo

    for name in _MULTIPLIER_IMPORTS:
        if model.component(name) is None:
            model.add_component(name, pyo.Suffix(direction=pyo.Suffix.IMPORT))


def capture_multipliers(model: Any, *, source: str) -> Dict[str, Any]:
    """The multipliers the last loaded solve returned, with each row's statement at capture."""

    import idaes.core.util.scaling as iscale
    from pyomo.common.collections import ComponentMap

    def finite(items):
        # ComponentMap: Pyomo components are keyed by identity (a scalar Var's
        # __eq__ builds an expression, so it cannot key a plain dict).
        kept = ComponentMap()
        for key, value in items:
            if value is not None and math.isfinite(float(value)):
                kept[key] = float(value)
        return kept

    duals = ComponentMap()
    for con, value in finite(model.dual.items()).items():
        duals[con] = (value, iscale.get_constraint_transform_applied_scaling_factor(con, default=1.0))
    return {
        "source": source,
        "zL": finite(model.ipopt_zL_out.items()),
        "zU": finite(model.ipopt_zU_out.items()),
        "dual": duals,
    }


def clear_warm_start(model: Any) -> None:
    """Withdraw any exported initial multipliers (a cold solve sends none)."""

    import pyomo.environ as pyo

    for name in _WARM_START_EXPORTS:
        if model.component(name) is not None:
            model.del_component(name)
    if model.component("dual") is not None:
        model.dual.direction = pyo.Suffix.IMPORT


def export_warm_start(model: Any, multipliers: Dict[str, Any]) -> Dict[str, Any]:
    """Hand IPOPT the captured multipliers for a warm-started solve, in the rows' CURRENT statement.

    A row restated by s since capture (the polish's material/energy
    conditioning) has its multiplier divided by s, so the exported point is the
    same primal-dual point in the new statement.
    """

    import idaes.core.util.scaling as iscale
    import pyomo.environ as pyo

    clear_warm_start(model)
    model.ipopt_zL_in = pyo.Suffix(direction=pyo.Suffix.EXPORT)
    model.ipopt_zU_in = pyo.Suffix(direction=pyo.Suffix.EXPORT)
    for var, value in multipliers["zL"].items():
        model.ipopt_zL_in[var] = value
    for var, value in multipliers["zU"].items():
        model.ipopt_zU_in[var] = value
    model.dual.clear()
    model.dual.direction = pyo.Suffix.IMPORT_EXPORT
    restated = 0
    for con, (value, factor_at_capture) in multipliers["dual"].items():
        factor_now = iscale.get_constraint_transform_applied_scaling_factor(con, default=1.0)
        if factor_now != factor_at_capture:
            restated += 1
        model.dual[con] = value * factor_at_capture / factor_now
    bounds = [*multipliers["zL"].values(), *multipliers["zU"].values()]
    duals = [float(value) for value in model.dual.values()]
    return {
        "source": multipliers["source"],
        # IPOPT writes only NONZERO suffix entries (ASL convention), so an
        # absent bound multiplier is exactly zero. A square DOF-0 solve with a
        # zero objective reports all of them zero (its summary: dual
        # infeasibility 0, complementarity 0), and every constraint multiplier
        # 0.0: the warm start then hands IPOPT z = 0 (pushed to
        # warm_start_mult_bound_push) where a cold start would use z = 1.
        "nonzero_bound_multipliers": sum(1 for v in bounds if v != 0.0),
        "constraint_multipliers": len(duals),
        "nonzero_constraint_multipliers": sum(1 for v in duals if v != 0.0),
        "constraint_multipliers_rescaled_for_restated_rows": restated,
        "max_abs_bound_multiplier": max((abs(v) for v in bounds), default=0.0),
        "max_abs_constraint_multiplier": max((abs(v) for v in duals), default=0.0),
        "options": dict(WARM_START_OPTIONS),
    }
