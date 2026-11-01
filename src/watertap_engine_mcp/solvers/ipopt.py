"""IPOPT solver helpers."""

from __future__ import annotations

from typing import Any


def get_ipopt_solver(options: dict[str, Any] | None = None) -> Any:
    """Return WaterTAP's configured IPOPT solver with options applied."""

    from watertap.core.solvers import get_solver

    solver = get_solver()
    for key, value in (options or {}).items():
        solver.options[key] = value
    return solver


def solve_ipopt(
    model: Any,
    *,
    options: dict[str, Any] | None = None,
    tee: bool = False,
    load_solutions: bool = False,
) -> Any:
    """Solve a model with IPOPT."""

    solver = get_ipopt_solver(options)
    return solver.solve(model, tee=tee, load_solutions=load_solutions)

