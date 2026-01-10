"""Diagnostics for WaterTAP Flowsheets.

Provides tools for diagnosing solver failures and model issues.
Wraps IDAES DiagnosticsToolbox and provides failure pattern matching.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class DiagnosticType(Enum):
    """Type of diagnostic check."""
    STRUCTURAL = "structural"
    NUMERICAL = "numerical"
    CONSTRAINT_RESIDUALS = "constraint_residuals"
    BOUND_VIOLATIONS = "bound_violations"
    JACOBIAN_ANALYSIS = "jacobian_analysis"


@dataclass
class ConstraintResidual:
    """Constraint residual information."""
    constraint_name: str
    residual: float
    body_value: float
    bound: Optional[float] = None
    is_equality: bool = True


@dataclass
class BoundViolation:
    """Variable bound violation."""
    variable_name: str
    value: float
    lower_bound: Optional[float] = None
    upper_bound: Optional[float] = None
    violation_type: str = ""  # "below_lower", "above_upper"


@dataclass
class DiagnosticResult:
    """Result of diagnostic analysis."""
    diagnostic_type: DiagnosticType
    issues_found: int
    details: List[Any] = field(default_factory=list)
    summary: str = ""
    suggestions: List[str] = field(default_factory=list)


class DiagnosticsRunner:
    """Runner for model diagnostics.

    Wraps IDAES DiagnosticsToolbox and provides additional
    WaterTAP-specific diagnostic patterns.
    """

    def __init__(self, model: Any = None):
        """Initialize diagnostics runner.

        Args:
            model: Pyomo model
        """
        self._model = model
        self._toolbox = None

    def _get_toolbox(self, model: Any) -> Any:
        """Get or create DiagnosticsToolbox."""
        try:
            from idaes.core.util.model_diagnostics import DiagnosticsToolbox
            return DiagnosticsToolbox(model)
        except ImportError:
            return None

    def run_structural_diagnostics(self, model: Any) -> DiagnosticResult:
        """Run structural diagnostics on model.

        Checks for:
        - Structural singularities
        - Redundant constraints
        - Variables not in constraints

        Args:
            model: Pyomo model

        Returns:
            DiagnosticResult
        """
        try:
            toolbox = self._get_toolbox(model)
            if toolbox is None:
                return DiagnosticResult(
                    diagnostic_type=DiagnosticType.STRUCTURAL,
                    issues_found=-1,
                    summary="IDAES DiagnosticsToolbox not available",
                )

            # Run structural checks
            issues = []

            # Check for structural singularities
            try:
                sing_result = toolbox.report_structural_issues()
                # Parse output - this is text-based
                issues.append(str(sing_result) if sing_result else "No structural issues")
            except Exception as e:
                issues.append(f"Structural check error: {e}")

            return DiagnosticResult(
                diagnostic_type=DiagnosticType.STRUCTURAL,
                issues_found=len(issues) if issues else 0,
                details=issues,
                summary="Structural diagnostics complete",
            )

        except Exception as e:
            return DiagnosticResult(
                diagnostic_type=DiagnosticType.STRUCTURAL,
                issues_found=-1,
                summary=f"Structural diagnostics failed: {e}",
            )

    def run_numerical_diagnostics(self, model: Any) -> DiagnosticResult:
        """Run numerical diagnostics on model.

        Checks for:
        - Large residuals
        - Bound violations
        - Numerical singularities

        Args:
            model: Pyomo model

        Returns:
            DiagnosticResult
        """
        try:
            toolbox = self._get_toolbox(model)
            if toolbox is None:
                return DiagnosticResult(
                    diagnostic_type=DiagnosticType.NUMERICAL,
                    issues_found=-1,
                    summary="IDAES DiagnosticsToolbox not available",
                )

            issues = []

            try:
                num_result = toolbox.report_numerical_issues()
                issues.append(str(num_result) if num_result else "No numerical issues")
            except Exception as e:
                issues.append(f"Numerical check error: {e}")

            return DiagnosticResult(
                diagnostic_type=DiagnosticType.NUMERICAL,
                issues_found=len(issues) if issues else 0,
                details=issues,
                summary="Numerical diagnostics complete",
            )

        except Exception as e:
            return DiagnosticResult(
                diagnostic_type=DiagnosticType.NUMERICAL,
                issues_found=-1,
                summary=f"Numerical diagnostics failed: {e}",
            )

    def get_constraint_residuals(
        self,
        model: Any,
        threshold: float = 1e-6,
        max_results: int = 20,
    ) -> DiagnosticResult:
        """Get largest constraint residuals.

        Args:
            model: Pyomo model
            threshold: Only report residuals above this
            max_results: Maximum number to return

        Returns:
            DiagnosticResult with ConstraintResidual details
        """
        try:
            from pyomo.environ import Constraint, value

            residuals = []

            for c in model.component_data_objects(Constraint, active=True, descend_into=True):
                try:
                    body = value(c.body, exception=False)
                    if body is None:
                        continue

                    if c.equality:
                        # Equality constraint: residual = |body - bound|
                        bound = value(c.lower, exception=False)
                        if bound is not None:
                            residual = abs(body - bound)
                            if residual > threshold:
                                residuals.append(ConstraintResidual(
                                    constraint_name=str(c),
                                    residual=residual,
                                    body_value=body,
                                    bound=bound,
                                    is_equality=True,
                                ))
                    else:
                        # Inequality - check both bounds
                        lower = value(c.lower, exception=False)
                        upper = value(c.upper, exception=False)

                        if lower is not None and body < lower:
                            residual = lower - body
                            if residual > threshold:
                                residuals.append(ConstraintResidual(
                                    constraint_name=str(c),
                                    residual=residual,
                                    body_value=body,
                                    bound=lower,
                                    is_equality=False,
                                ))
                        elif upper is not None and body > upper:
                            residual = body - upper
                            if residual > threshold:
                                residuals.append(ConstraintResidual(
                                    constraint_name=str(c),
                                    residual=residual,
                                    body_value=body,
                                    bound=upper,
                                    is_equality=False,
                                ))
                except Exception:
                    continue

            # Sort by residual magnitude
            residuals.sort(key=lambda r: r.residual, reverse=True)
            residuals = residuals[:max_results]

            return DiagnosticResult(
                diagnostic_type=DiagnosticType.CONSTRAINT_RESIDUALS,
                issues_found=len(residuals),
                details=residuals,
                summary=f"Found {len(residuals)} constraints with residual > {threshold}",
            )

        except Exception as e:
            return DiagnosticResult(
                diagnostic_type=DiagnosticType.CONSTRAINT_RESIDUALS,
                issues_found=-1,
                summary=f"Failed to get residuals: {e}",
            )

    def get_bound_violations(
        self,
        model: Any,
        tolerance: float = 1e-8,
        max_results: int = 20,
    ) -> DiagnosticResult:
        """Get variables violating their bounds.

        Args:
            model: Pyomo model
            tolerance: Tolerance for bound violations
            max_results: Maximum number to return

        Returns:
            DiagnosticResult with BoundViolation details
        """
        try:
            from pyomo.environ import Var, value

            violations = []

            for v in model.component_data_objects(Var, active=True, descend_into=True):
                try:
                    val = value(v, exception=False)
                    if val is None:
                        continue

                    lb = v.lb
                    ub = v.ub

                    if lb is not None and val < lb - tolerance:
                        violations.append(BoundViolation(
                            variable_name=str(v),
                            value=val,
                            lower_bound=lb,
                            upper_bound=ub,
                            violation_type="below_lower",
                        ))
                    elif ub is not None and val > ub + tolerance:
                        violations.append(BoundViolation(
                            variable_name=str(v),
                            value=val,
                            lower_bound=lb,
                            upper_bound=ub,
                            violation_type="above_upper",
                        ))
                except Exception:
                    continue

            # Sort by violation magnitude
            def violation_magnitude(v):
                if v.violation_type == "below_lower":
                    return v.lower_bound - v.value
                else:
                    return v.value - v.upper_bound

            violations.sort(key=violation_magnitude, reverse=True)
            violations = violations[:max_results]

            return DiagnosticResult(
                diagnostic_type=DiagnosticType.BOUND_VIOLATIONS,
                issues_found=len(violations),
                details=violations,
                summary=f"Found {len(violations)} bound violations",
            )

        except Exception as e:
            return DiagnosticResult(
                diagnostic_type=DiagnosticType.BOUND_VIOLATIONS,
                issues_found=-1,
                summary=f"Failed to check bounds: {e}",
            )

    def diagnose_failure(
        self,
        model: Any,
        termination_condition: str,
    ) -> Dict[str, Any]:
        """Diagnose a solver failure.

        Args:
            model: Pyomo model that failed to solve
            termination_condition: The solver's termination condition

        Returns:
            Dict with diagnosis and suggestions
        """
        results = {
            "termination_condition": termination_condition,
            "constraint_residuals": [],
            "bound_violations": [],
            "likely_causes": [],
            "suggested_fixes": [],
        }

        # Get residuals and violations
        residuals = self.get_constraint_residuals(model)
        violations = self.get_bound_violations(model)

        results["constraint_residuals"] = [
            {"name": r.constraint_name, "residual": r.residual}
            for r in residuals.details[:10]
        ]
        results["bound_violations"] = [
            {"name": v.variable_name, "value": v.value, "type": v.violation_type}
            for v in violations.details[:10]
        ]

        # Pattern matching for common failures
        if termination_condition == "infeasible":
            if any("flux_mass" in str(r.constraint_name) for r in residuals.details):
                results["likely_causes"].append(
                    "RO membrane flux constraint violated - likely insufficient feed pressure"
                )
                results["suggested_fixes"].append(
                    "Increase feed pressure to at least 1.5x osmotic pressure"
                )

            if any("solubility" in str(r.constraint_name).lower() for r in residuals.details):
                results["likely_causes"].append(
                    "Crystallizer solubility constraint violated"
                )
                results["suggested_fixes"].append(
                    "Check that feed concentration can reach saturation at operating temperature"
                )

        elif termination_condition == "maxIterations":
            results["likely_causes"].append(
                "Solver hit iteration limit - likely poor scaling or bad initialization"
            )
            results["suggested_fixes"].extend([
                "Check scaling with report_scaling_issues",
                "Scale small parameters (A_comp: 1e12, B_comp: 1e8)",
                "Initialize units sequentially with propagate_state",
            ])

        elif termination_condition == "locallyInfeasible":
            results["likely_causes"].append(
                "Local minimum is infeasible - bad initial point"
            )
            results["suggested_fixes"].extend([
                "Try different initial values",
                "Initialize from a known feasible solution",
            ])

        return results


def run_diagnostics(model: Any) -> Dict[str, Any]:
    """Run comprehensive diagnostics on a model.

    Args:
        model: Pyomo model

    Returns:
        Dict with all diagnostic results
    """
    runner = DiagnosticsRunner(model)

    return {
        "structural": runner.run_structural_diagnostics(model).__dict__,
        "numerical": runner.run_numerical_diagnostics(model).__dict__,
        "constraint_residuals": runner.get_constraint_residuals(model).__dict__,
        "bound_violations": runner.get_bound_violations(model).__dict__,
    }
