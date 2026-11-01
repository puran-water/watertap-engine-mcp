"""Problem-class detection and solver selection."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from typing import Any

from watertap_engine_mcp.api.errors import SolverDispatchError, SolverNotApplicableError, SolverUnknownError

from .capabilities import (
    DEFAULT_SOLVER_BY_PROBLEM_TYPE,
    SOLVER_CAPABILITIES,
    ProblemType,
    available_solvers_for_problem_type,
)


def _component_is_nonlinear(expr: Any) -> bool:
    """Return True if a Pyomo expression is nonlinear."""

    if expr is None:
        return False
    degree_fn = getattr(expr, "polynomial_degree", None)
    if degree_fn is None:
        return True
    try:
        degree = degree_fn()
    except Exception:
        return True
    return degree not in (0, 1)


def detect_problem_type(model: Any) -> ProblemType:
    """Detect LP/MILP/NLP/MINLP from a Pyomo model."""

    from pyomo.environ import Constraint, Objective, Var

    has_integers = False
    has_nonlinear = False

    for var in model.component_data_objects(Var, descend_into=True):
        try:
            if var.is_binary() or var.is_integer():
                has_integers = True
                break
        except Exception:
            domain = getattr(var, "domain", None)
            domain_name = str(domain)
            if "Integer" in domain_name or "Boolean" in domain_name or "Binary" in domain_name:
                has_integers = True
                break

    for constraint in model.component_data_objects(Constraint, active=True, descend_into=True):
        if _component_is_nonlinear(constraint.body):
            has_nonlinear = True
            break

    if not has_nonlinear:
        for objective in model.component_data_objects(Objective, active=True, descend_into=True):
            if _component_is_nonlinear(objective.expr):
                has_nonlinear = True
                break

    if has_integers and has_nonlinear:
        return ProblemType.MINLP
    if has_integers:
        return ProblemType.MILP
    if has_nonlinear:
        return ProblemType.NLP
    return ProblemType.LP


def select_solver(requested_solver: str | None, problem_type: ProblemType) -> str:
    """Select or validate a solver for the detected problem type."""

    if requested_solver is None or not str(requested_solver).strip():
        return DEFAULT_SOLVER_BY_PROBLEM_TYPE[problem_type]

    requested = str(requested_solver).strip().lower()
    capability = SOLVER_CAPABILITIES.get(requested)
    available = available_solvers_for_problem_type(problem_type)
    if capability is None:
        raise SolverUnknownError(
            requested=requested,
            detected_problem_type=problem_type.value,
            available_solvers_for_problem_type=available,
            known_solvers=sorted(SOLVER_CAPABILITIES),
            message=f"Unknown solver '{requested}'.",
        )

    if problem_type in capability.problem_types:
        return requested

    if requested == "glpk" and problem_type == ProblemType.NLP:
        message = (
            "glpk is LP-only; this model has nonlinear constraints/objective. "
            "Use ipopt for NLP."
        )
    else:
        message = (
            f"Solver '{requested}' cannot solve {problem_type.value} models. "
            f"Available solvers: {available}."
        )
    raise SolverNotApplicableError(
        requested=requested,
        detected_problem_type=problem_type.value,
        available_solvers_for_problem_type=available,
        message=message,
    )


def solver_not_applicable_payload(exc: SolverDispatchError) -> dict[str, Any]:
    """Convert a solver dispatch exception to the legacy action error shape."""

    error = exc.to_error()
    return {
        "error": error.message,
        "code": error.code,
        "details": error.details,
        "correlation_id": error.correlation_id,
        "status": "rejected",
    }


def dispatch_solver_for_session(
    session: Any,
    requested_solver: str | None,
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """Build a model for problem-type detection and select a compatible solver."""

    if requested_solver is None or not str(requested_solver).strip():
        return None, None, None

    try:
        from watertap_engine_mcp.utils.model_builder import ModelBuildError, ModelBuilder

        dispatch_output = StringIO()
        with redirect_stdout(dispatch_output), redirect_stderr(dispatch_output):
            builder = ModelBuilder(session)
            model = builder.build()
    except ModelBuildError as exc:
        return None, None, {"error": f"Model build failed during solver dispatch: {exc}"}
    except Exception as exc:
        return None, None, {"error": f"Solver dispatch model build failed: {exc}"}

    problem_type = detect_problem_type(model)
    try:
        selected_solver = select_solver(requested_solver, problem_type)
    except SolverDispatchError as exc:
        return None, problem_type.value, solver_not_applicable_payload(exc)
    return selected_solver, problem_type.value, None
