"""Failure Recovery Strategies for WaterTAP Flowsheets.

Provides automatic and guided recovery from common solver failures.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from watertap_engine_mcp.solver.scaler import apply_asm2d_species_scaling


class FailureType(Enum):
    """Types of solver failures."""
    INFEASIBLE = "infeasible"
    LOCALLY_INFEASIBLE = "locally_infeasible"
    MAX_ITERATIONS = "max_iterations"
    NUMERICAL_ERROR = "numerical_error"
    UNBOUNDED = "unbounded"
    OTHER = "other"


class RecoveryStrategy(Enum):
    """Available recovery strategies.

    BOUND_RELAXATION and INITIALIZATION_RETRY were REMOVED 2026-08-24:
    the former widened every bound by 10% of its span with no physical
    basis and no restore (a failed solve could return "recovered" on a
    mutated feasible set), and the latter was a no-op that reported a
    re-initialization it never performed. Solver-workaround audit,
    Wave 5a.
    """
    PENALTY_RELAXATION = "penalty_relaxation"
    CONSTRAINT_RELAXATION = "constraint_relaxation"
    SCALING_ADJUSTMENT = "scaling_adjustment"
    SOLVER_OPTIONS = "solver_options"
    MANUAL_INTERVENTION = "manual_intervention"


@dataclass
class RecoveryAction:
    """A specific recovery action to take."""
    strategy: RecoveryStrategy
    description: str
    target: Optional[str] = None  # Variable or constraint name
    value: Optional[Any] = None   # New value to apply
    priority: int = 0             # Higher = try first


@dataclass
class RecoveryResult:
    """Result of a recovery attempt."""
    success: bool
    strategy: RecoveryStrategy
    message: str
    solve_result: Optional[Dict[str, Any]] = None
    actions_taken: List[str] = field(default_factory=list)


@dataclass
class FailureAnalysis:
    """Analysis of a solver failure."""
    failure_type: FailureType
    likely_causes: List[str]
    suggested_strategies: List[RecoveryAction]
    related_constraints: List[str] = field(default_factory=list)
    related_variables: List[str] = field(default_factory=list)


class FailureAnalyzer:
    """Analyzes solver failures and suggests recovery strategies."""

    def __init__(self):
        """Initialize failure analyzer."""
        self._patterns = self._build_patterns()

    def _build_patterns(self) -> Dict[FailureType, Dict[str, Any]]:
        """Build failure pattern database."""
        return {
            FailureType.INFEASIBLE: {
                "indicators": ["infeasible", "no feasible solution"],
                "common_causes": [
                    "Constraint bounds too tight",
                    "Conflicting constraints",
                    "Physical impossibility (e.g., pressure below osmotic)",
                ],
                "default_strategies": [
                    RecoveryAction(
                        strategy=RecoveryStrategy.CONSTRAINT_RELAXATION,
                        description="Add slack variables to tight constraints",
                        priority=2,
                    ),
                ],
            },
            FailureType.MAX_ITERATIONS: {
                "indicators": ["iteration limit", "maxIterations"],
                "common_causes": [
                    "Poor scaling",
                    "Bad initial point",
                    "Highly nonlinear problem",
                ],
                "default_strategies": [
                    RecoveryAction(
                        strategy=RecoveryStrategy.SCALING_ADJUSTMENT,
                        description="Apply aggressive scaling to small/large variables",
                        priority=1,
                    ),
                    RecoveryAction(
                        strategy=RecoveryStrategy.SOLVER_OPTIONS,
                        description="Increase iteration limit",
                        value={"max_iter": 500},
                        priority=2,
                    ),
                ],
            },
            FailureType.LOCALLY_INFEASIBLE: {
                "indicators": ["locally infeasible", "locallyInfeasible"],
                "common_causes": [
                    "Starting point far from solution",
                    "Multiple local minima",
                    "Non-convex problem structure",
                ],
                "default_strategies": [
                    RecoveryAction(
                        strategy=RecoveryStrategy.PENALTY_RELAXATION,
                        description="Use penalty method for difficult constraints",
                        priority=2,
                    ),
                ],
            },
            FailureType.NUMERICAL_ERROR: {
                "indicators": ["numerical", "evaluation error", "overflow"],
                "common_causes": [
                    "Division by near-zero value",
                    "Log of negative number",
                    "Extreme variable values",
                ],
                "default_strategies": [
                    RecoveryAction(
                        strategy=RecoveryStrategy.SCALING_ADJUSTMENT,
                        description="Scale problematic variables",
                        priority=1,
                    ),
                ],
            },
        }

    def analyze_failure(
        self,
        termination_condition: str,
        constraint_residuals: Optional[List[Dict]] = None,
        bound_violations: Optional[List[Dict]] = None,
    ) -> FailureAnalysis:
        """Analyze a solver failure.

        Args:
            termination_condition: Solver's termination condition
            constraint_residuals: List of constraint residual info
            bound_violations: List of bound violation info

        Returns:
            FailureAnalysis with suggestions
        """
        # Determine failure type
        failure_type = self._classify_failure(termination_condition)
        pattern = self._patterns.get(failure_type, {})

        # Build cause list
        causes = list(pattern.get("common_causes", []))

        # Add specific causes from residuals/violations
        related_constraints = []
        related_variables = []

        if constraint_residuals:
            for res in constraint_residuals[:5]:
                name = res.get("name", "")
                related_constraints.append(name)
                causes.append(f"Large residual in constraint: {name}")

        if bound_violations:
            for viol in bound_violations[:5]:
                name = viol.get("name", "")
                related_variables.append(name)
                causes.append(f"Bound violation: {name}")

        # Get strategies
        strategies = list(pattern.get("default_strategies", []))

        # Add context-specific strategies
        strategies.extend(self._get_context_strategies(
            constraint_residuals, bound_violations
        ))

        # Sort by priority
        strategies.sort(key=lambda s: s.priority)

        return FailureAnalysis(
            failure_type=failure_type,
            likely_causes=causes,
            suggested_strategies=strategies,
            related_constraints=related_constraints,
            related_variables=related_variables,
        )

    def _classify_failure(self, termination_condition: str) -> FailureType:
        """Classify failure type from termination condition."""
        tc_lower = termination_condition.lower()

        for failure_type, pattern in self._patterns.items():
            for indicator in pattern.get("indicators", []):
                if indicator.lower() in tc_lower:
                    return failure_type

        return FailureType.OTHER

    def _get_context_strategies(
        self,
        residuals: Optional[List[Dict]],
        violations: Optional[List[Dict]],
    ) -> List[RecoveryAction]:
        """Get context-specific recovery strategies."""
        strategies = []

        if residuals:
            # Check for common WaterTAP patterns
            for res in residuals[:3]:
                name = res.get("name", "").lower()

                if "flux" in name or "permeate" in name:
                    strategies.append(RecoveryAction(
                        strategy=RecoveryStrategy.MANUAL_INTERVENTION,
                        description="RO flux constraint violated - increase feed pressure or membrane area",
                        target=name,
                        priority=0,
                    ))

                if "solubility" in name:
                    strategies.append(RecoveryAction(
                        strategy=RecoveryStrategy.MANUAL_INTERVENTION,
                        description="Solubility constraint violated - check crystallizer operating conditions",
                        target=name,
                        priority=0,
                    ))

        return strategies


class RecoveryExecutor:
    """Executes recovery strategies on a model."""

    def __init__(self, model: Any = None):
        """Initialize recovery executor.

        Args:
            model: Pyomo model
        """
        self._model = model
        self._analyzer = FailureAnalyzer()

    def attempt_recovery(
        self,
        termination_condition: str,
        constraint_residuals: Optional[List[Dict]] = None,
        bound_violations: Optional[List[Dict]] = None,
        max_attempts: int = 3,
    ) -> RecoveryResult:
        """Attempt automatic recovery from failure.

        Args:
            termination_condition: Failed solve's termination condition
            constraint_residuals: Constraint residual info
            bound_violations: Bound violation info
            max_attempts: Maximum recovery attempts

        Returns:
            RecoveryResult
        """
        if self._model is None:
            return RecoveryResult(
                success=False,
                strategy=RecoveryStrategy.MANUAL_INTERVENTION,
                message="No model set for recovery",
            )

        analysis = self._analyzer.analyze_failure(
            termination_condition,
            constraint_residuals,
            bound_violations,
        )

        actions_taken = []

        capped_attempts = min(max_attempts, 3)
        for attempt, action in enumerate(analysis.suggested_strategies[:capped_attempts]):
            actions_taken.append(f"Attempt {attempt + 1}: {action.description}")

            try:
                if action.strategy == RecoveryStrategy.SCALING_ADJUSTMENT:
                    self._apply_scaling_recovery()
                elif action.strategy == RecoveryStrategy.SOLVER_OPTIONS:
                    self._apply_solver_options(action.value or {})
                else:
                    continue  # Skip unsupported strategies

                # Retry solve
                solve_result = self._retry_solve()

                if solve_result.get("success"):
                    return RecoveryResult(
                        success=True,
                        strategy=action.strategy,
                        message=f"Recovery successful: {action.description}",
                        solve_result=solve_result,
                        actions_taken=actions_taken,
                    )

            except Exception as e:
                actions_taken.append(f"  Error: {e}")

        return RecoveryResult(
            success=False,
            strategy=RecoveryStrategy.MANUAL_INTERVENTION,
            message="Automatic recovery failed - manual intervention required",
            actions_taken=actions_taken,
        )

    def _apply_scaling_recovery(self):
        """Apply aggressive scaling for recovery."""
        try:
            import idaes.core.util.scaling as iscale
            apply_asm2d_species_scaling(self._model)
            iscale.calculate_scaling_factors(self._model)
            iscale.constraint_autoscale_large_jac(self._model)
        except ImportError:
            pass

    def _apply_solver_options(self, options: Dict):
        """Apply solver option changes."""
        # Options will be used in next solve
        self._solver_options = options



    def _retry_solve(self) -> Dict[str, Any]:
        """Retry solve after recovery action."""
        try:
            from watertap.core.solvers import get_solver

            solver = get_solver()

            if hasattr(self, "_solver_options"):
                for opt, val in self._solver_options.items():
                    solver.options[opt] = val

            old_stdout = sys.stdout
            old_stderr = sys.stderr
            try:
                with open(os.devnull, "w") as devnull:
                    sys.stdout = devnull
                    sys.stderr = devnull
                    results = solver.solve(self._model, tee=False)
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
            termination = str(results.solver.termination_condition)
            status = str(results.solver.status)

            return {
                "success": termination == "optimal",
                "termination_condition": termination,
                "solver_status": status,
            }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
            }


def analyze_and_suggest_recovery(
    termination_condition: str,
    constraint_residuals: Optional[List[Dict]] = None,
    bound_violations: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    """Analyze failure and suggest recovery without executing.

    Args:
        termination_condition: Solver's termination condition
        constraint_residuals: Constraint residual info
        bound_violations: Bound violation info

    Returns:
        Dict with analysis and suggestions
    """
    analyzer = FailureAnalyzer()
    analysis = analyzer.analyze_failure(
        termination_condition,
        constraint_residuals,
        bound_violations,
    )

    return {
        "failure_type": analysis.failure_type.name,
        "likely_causes": analysis.likely_causes,
        "suggested_strategies": [
            {
                "strategy": s.strategy.name,
                "description": s.description,
                "target": s.target,
                "priority": s.priority,
            }
            for s in analysis.suggested_strategies
        ],
        "related_constraints": analysis.related_constraints,
        "related_variables": analysis.related_variables,
    }
