"""Solver job state containers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .capabilities import ProblemType


@dataclass
class SolverJobState:
    """Solver dispatch metadata attached to a solve job."""

    requested_solver: str | None
    selected_solver: str
    problem_type: ProblemType
    details: dict[str, Any] = field(default_factory=dict)

