"""Solver capability table for dispatch decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ProblemType(Enum):
    """Supported algebraic optimization problem classes."""

    LP = "LP"
    MILP = "MILP"
    NLP = "NLP"
    MINLP = "MINLP"


@dataclass(frozen=True)
class SolverCapability:
    """Declared solver compatibility."""

    name: str
    problem_types: frozenset[ProblemType]
    description: str


SOLVER_CAPABILITIES: dict[str, SolverCapability] = {
    "ipopt": SolverCapability(
        name="ipopt",
        problem_types=frozenset({ProblemType.LP, ProblemType.NLP}),
        description="Continuous linear/nonlinear programming solver.",
    ),
    "cbc": SolverCapability(
        name="cbc",
        problem_types=frozenset({ProblemType.MILP}),
        description="Linear mixed-integer programming solver.",
    ),
    "glpk": SolverCapability(
        name="glpk",
        problem_types=frozenset({ProblemType.LP, ProblemType.MILP}),
        description="Linear LP/MILP solver.",
    ),
    "bonmin": SolverCapability(
        name="bonmin",
        problem_types=frozenset({ProblemType.MINLP}),
        description="Mixed-integer nonlinear programming solver.",
    ),
    "couenne": SolverCapability(
        name="couenne",
        problem_types=frozenset({ProblemType.MINLP}),
        description="Global mixed-integer nonlinear programming solver.",
    ),
}


DEFAULT_SOLVER_BY_PROBLEM_TYPE: dict[ProblemType, str] = {
    ProblemType.LP: "ipopt",
    ProblemType.MILP: "cbc",
    ProblemType.NLP: "ipopt",
    ProblemType.MINLP: "bonmin",
}


def available_solvers_for_problem_type(problem_type: ProblemType) -> list[str]:
    """Return solver names that can handle a problem type."""

    return sorted(
        name
        for name, capability in SOLVER_CAPABILITIES.items()
        if problem_type in capability.problem_types
    )
