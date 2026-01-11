"""Solver Hygiene Pipeline for WaterTAP Flowsheets.

Implements the state machine for systematic flowsheet solving:
IDLE -> DOF_CHECK -> SCALING -> INITIALIZATION -> PRE_SOLVE_DIAGNOSTICS -> SOLVING
                                                                              |
COMPLETED <-- POST_SOLVE_DIAGNOSTICS <----------------------------------------+
    ^              | (if failed)                                              |
    +------- RELAXED_SOLVE ---------------------------------------------------+
                   | (if still fails)
                FAILED
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Callable

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .dof_resolver import DOFResolver, DOFStatus, FlowsheetDOFAnalysis
from .scaler import ScalingTools
from .initializer import FlowsheetInitializer
from .diagnostics import DiagnosticsRunner
from .recovery import RecoveryExecutor, analyze_and_suggest_recovery


class PipelineState(Enum):
    """States in the solver hygiene pipeline."""
    IDLE = "idle"
    DOF_CHECK = "dof_check"
    SCALING = "scaling"
    INITIALIZATION = "initialization"
    PRE_SOLVE_DIAGNOSTICS = "pre_solve_diagnostics"
    SOLVING = "solving"
    POST_SOLVE_DIAGNOSTICS = "post_solve_diagnostics"
    RELAXED_SOLVE = "relaxed_solve"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class PipelineResult:
    """Result from a pipeline stage."""
    success: bool
    state: PipelineState
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass
class PipelineConfig:
    """Configuration for the hygiene pipeline."""

    # DOF check settings
    allow_overspecified: bool = False
    auto_fix_dof: bool = False

    # Scaling settings
    auto_scale: bool = True
    report_scaling_issues: bool = True

    # Initialization settings
    sequential_init: bool = True
    propagate_state: bool = True
    tear_streams: Optional[List[tuple]] = None

    # Solve settings
    solver_options: Dict[str, Any] = field(default_factory=dict)
    max_iterations: int = 100
    tolerance: float = 1e-8

    # Recovery settings
    enable_relaxed_solve: bool = True
    relaxation_factor: float = 0.1


class HygienePipeline:
    """Solver hygiene pipeline for systematic flowsheet solving."""

    def __init__(
        self,
        model: Any = None,
        config: Optional[PipelineConfig] = None,
        units: Optional[Dict[str, Any]] = None,
    ):
        """Initialize pipeline.

        Args:
            model: Pyomo model
            config: Pipeline configuration
            units: Optional dict of unit_id -> unit_block. If not provided,
                   units are discovered from model.fs (IDAES convention).
        """
        self._model = model
        self._config = config or PipelineConfig()
        self._state = PipelineState.IDLE
        self._history: List[PipelineResult] = []
        self._units = units  # Can be None, will be discovered on demand

        # Sub-components
        self._dof_resolver = DOFResolver(model)
        self._scaler = ScalingTools(model)
        self._initializer = FlowsheetInitializer(flowsheet=model, model=model)
        self._diagnostics = DiagnosticsRunner(model)
        self._recovery = RecoveryExecutor(model)

    def _discover_units(self) -> Dict[str, Any]:
        """Discover units under m.fs (IDAES convention), fallback to m.

        Returns:
            Dict mapping unit_id to unit block
        """
        if self._model is None:
            return {}

        from pyomo.core.base.block import Block

        # Prefer m.fs (IDAES FlowsheetBlock pattern)
        fs = getattr(self._model, 'fs', self._model)

        units = {}
        for name in dir(fs):
            if name.startswith('_'):
                continue
            obj = getattr(fs, name, None)
            if obj is None:
                continue
            # Type check + port presence (avoid picking up non-units)
            if isinstance(obj, Block) and (
                hasattr(obj, 'inlet') or hasattr(obj, 'outlet') or
                hasattr(obj, 'initialize')
            ):
                units[name] = obj
        return units

    def get_units(self) -> Dict[str, Any]:
        """Get units dict, discovering if not already set."""
        if self._units is None:
            self._units = self._discover_units()
        return self._units

    @property
    def state(self) -> PipelineState:
        """Get current pipeline state."""
        return self._state

    @property
    def history(self) -> List[PipelineResult]:
        """Get pipeline execution history."""
        return self._history

    def _transition(
        self,
        new_state: PipelineState,
        success: bool,
        message: str,
        **kwargs
    ) -> PipelineResult:
        """Transition to new state and record result."""
        result = PipelineResult(
            success=success,
            state=new_state,
            message=message,
            **kwargs
        )
        self._history.append(result)
        self._state = new_state
        return result

    def run_dof_check(self) -> PipelineResult:
        """Run DOF check stage.

        Returns:
            PipelineResult
        """
        if self._model is None:
            return self._transition(
                PipelineState.FAILED,
                False,
                "No model set",
            )

        # Pass the flowsheet block (m.fs), not the whole model
        fs = getattr(self._model, 'fs', self._model)
        analysis = self._dof_resolver.analyze_flowsheet(fs)

        if analysis.overall_status == DOFStatus.UNDERSPECIFIED:
            return self._transition(
                PipelineState.DOF_CHECK,
                False,
                f"Model is underspecified: {analysis.total_dof} DOF remaining",
                details={
                    "total_dof": analysis.total_dof,
                    "unit_dof": {u: d.dof for u, d in analysis.unit_analyses.items()},
                    "suggestions": [
                        {"unit": s.unit_id, "var": s.var_name, "value": s.suggested_value}
                        for s in analysis.suggestions
                    ],
                },
            )

        if analysis.overall_status == DOFStatus.OVERSPECIFIED:
            if not self._config.allow_overspecified:
                return self._transition(
                    PipelineState.DOF_CHECK,
                    False,
                    f"Model is overspecified: {analysis.total_dof} DOF (should be 0)",
                    details={"total_dof": analysis.total_dof},
                )

        return self._transition(
            PipelineState.DOF_CHECK,
            True,
            f"DOF check passed: {analysis.total_dof} DOF",
            details={"total_dof": analysis.total_dof},
        )

    def run_scaling(self) -> PipelineResult:
        """Run scaling stage.

        Returns:
            PipelineResult
        """
        if self._model is None:
            return self._transition(
                PipelineState.FAILED,
                False,
                "No model set",
            )

        try:
            if self._config.auto_scale:
                self._scaler.calculate_scaling_factors(self._model)

            if self._config.report_scaling_issues:
                report = self._scaler.get_scaling_report(self._model)
                if report.unscaled_vars > 0 or report.unscaled_constraints > 0:
                    return self._transition(
                        PipelineState.SCALING,
                        True,  # Proceed with warning
                        "Scaling applied with warnings",
                        warnings=[
                            f"Unscaled variables: {report.unscaled_vars}",
                            f"Unscaled constraints: {report.unscaled_constraints}",
                        ],
                        details={
                            "issues": [
                                {"type": i.issue_type.value, "name": i.component_name}
                                for i in report.issues[:10]
                            ],
                        },
                    )

            return self._transition(
                PipelineState.SCALING,
                True,
                "Scaling applied successfully",
            )

        except Exception as e:
            return self._transition(
                PipelineState.SCALING,
                False,
                f"Scaling failed: {e}",
                errors=[str(e)],
            )

    def run_initialization(self) -> PipelineResult:
        """Run initialization stage.

        Returns:
            PipelineResult
        """
        if self._model is None:
            return self._transition(
                PipelineState.FAILED,
                False,
                "No model set",
            )

        try:
            # Use units from constructor or discover from m.fs
            units = self.get_units()

            # Get connections from arcs if available
            # Search in model.fs (IDAES convention) first, then model
            connections = []
            fs = getattr(self._model, 'fs', self._model)

            from pyomo.network import Arc as PyomoArc
            for arc in fs.component_objects(PyomoArc, active=True, descend_into=False):
                src_port = arc.source
                dst_port = arc.destination
                if src_port and dst_port:
                    connections.append({
                        "source_unit": str(src_port.parent_block().name),
                        "source_port": str(src_port.name),
                        "dest_unit": str(dst_port.parent_block().name),
                        "dest_port": str(dst_port.name),
                    })

            result = self._initializer.initialize_flowsheet(
                self._model,
                units=units,
                connections=connections,
                tear_streams=self._config.tear_streams,
            )

            if result.success:
                return self._transition(
                    PipelineState.INITIALIZATION,
                    True,
                    "Initialization completed",
                    details={
                        "units_initialized": result.units_initialized,
                        "warnings": result.warnings,
                    },
                )
            else:
                return self._transition(
                    PipelineState.INITIALIZATION,
                    False,
                    f"Initialization failed: {result.message}",
                    errors=result.errors,
                    details={"failed_unit": result.failed_unit},
                )

        except Exception as e:
            return self._transition(
                PipelineState.INITIALIZATION,
                False,
                f"Initialization error: {e}",
                errors=[str(e)],
            )

    def run_pre_solve_diagnostics(self) -> PipelineResult:
        """Run pre-solve diagnostics.

        Returns:
            PipelineResult
        """
        if self._model is None:
            return self._transition(
                PipelineState.FAILED,
                False,
                "No model set",
            )

        try:
            structural = self._diagnostics.run_structural_diagnostics(self._model)
            numerical = self._diagnostics.run_numerical_diagnostics(self._model)

            issues = []
            if structural.issues_found > 0:
                issues.extend(structural.details)
            if numerical.issues_found > 0:
                issues.extend(numerical.details)

            if issues:
                return self._transition(
                    PipelineState.PRE_SOLVE_DIAGNOSTICS,
                    True,  # Continue with warnings
                    f"Pre-solve diagnostics found {len(issues)} potential issues",
                    warnings=issues[:10],
                    details={
                        "structural_issues": structural.issues_found,
                        "numerical_issues": numerical.issues_found,
                    },
                )

            return self._transition(
                PipelineState.PRE_SOLVE_DIAGNOSTICS,
                True,
                "Pre-solve diagnostics passed",
            )

        except Exception as e:
            return self._transition(
                PipelineState.PRE_SOLVE_DIAGNOSTICS,
                False,
                f"Diagnostics error: {e}",
                errors=[str(e)],
            )

    def run_solve(self) -> PipelineResult:
        """Run solve stage.

        Returns:
            PipelineResult
        """
        if self._model is None:
            return self._transition(
                PipelineState.FAILED,
                False,
                "No model set",
            )

        try:
            from watertap.core.solvers import get_solver

            solver = get_solver()
            for opt, val in self._config.solver_options.items():
                solver.options[opt] = val

            results = solver.solve(self._model, tee=False)

            status = str(results.solver.status)
            termination = str(results.solver.termination_condition)

            if termination == "optimal":
                return self._transition(
                    PipelineState.SOLVING,
                    True,
                    "Solve completed successfully",
                    details={
                        "solver_status": status,
                        "termination_condition": termination,
                        "solve_time": getattr(results.solver, "wallclock_time", None),
                        "iterations": getattr(results.solver, "iterations", None),
                    },
                )
            else:
                return self._transition(
                    PipelineState.SOLVING,
                    False,
                    f"Solve failed: {termination}",
                    details={
                        "solver_status": status,
                        "termination_condition": termination,
                    },
                )

        except ImportError:
            return self._transition(
                PipelineState.SOLVING,
                False,
                "WaterTAP not available",
                errors=["Could not import watertap.core.solvers"],
            )
        except Exception as e:
            return self._transition(
                PipelineState.SOLVING,
                False,
                f"Solve error: {e}",
                errors=[str(e)],
            )

    def run_post_solve_diagnostics(self) -> PipelineResult:
        """Run post-solve diagnostics.

        Returns:
            PipelineResult
        """
        if self._model is None:
            return self._transition(
                PipelineState.FAILED,
                False,
                "No model set",
            )

        residuals = self._diagnostics.get_constraint_residuals(self._model)
        violations = self._diagnostics.get_bound_violations(self._model)

        if residuals.issues_found > 0 or violations.issues_found > 0:
            return self._transition(
                PipelineState.POST_SOLVE_DIAGNOSTICS,
                False,
                f"Post-solve issues: {residuals.issues_found} residuals, {violations.issues_found} violations",
                details={
                    "constraint_residuals": [
                        {"name": r.constraint_name, "residual": r.residual}
                        for r in residuals.details[:10]
                    ],
                    "bound_violations": [
                        {"name": v.variable_name, "value": v.value, "type": v.violation_type}
                        for v in violations.details[:10]
                    ],
                },
            )

        return self._transition(
            PipelineState.POST_SOLVE_DIAGNOSTICS,
            True,
            "Post-solve diagnostics passed",
        )

    def run_full_pipeline(
        self,
        on_stage_complete: Optional[Callable[[PipelineResult], None]] = None,
    ) -> PipelineResult:
        """Run the full hygiene pipeline.

        Args:
            on_stage_complete: Optional callback after each stage

        Returns:
            Final PipelineResult
        """
        stages = [
            ("DOF Check", self.run_dof_check),
            ("Scaling", self.run_scaling),
            ("Initialization", self.run_initialization),
            ("Pre-solve Diagnostics", self.run_pre_solve_diagnostics),
            ("Solve", self.run_solve),
            ("Post-solve Diagnostics", self.run_post_solve_diagnostics),
        ]

        for stage_name, stage_func in stages:
            result = stage_func()

            if on_stage_complete:
                on_stage_complete(result)

            if not result.success:
                # Check if we should try relaxed solve
                if (
                    self._config.enable_relaxed_solve
                    and result.state == PipelineState.SOLVING
                ):
                    # Attempt recovery using RecoveryExecutor
                    recovery_result = self._recovery.attempt_recovery(
                        termination_condition=result.details.get("termination_condition", "unknown"),
                        max_attempts=3,
                    )

                    if recovery_result.success:
                        self._transition(
                            PipelineState.RELAXED_SOLVE,
                            True,
                            f"Recovery succeeded: {recovery_result.message}",
                            details={
                                "strategy": recovery_result.strategy.value,
                                "actions_taken": recovery_result.actions_taken,
                            },
                        )
                        # Continue to post-solve diagnostics after successful recovery
                        return self.run_post_solve_diagnostics()
                    else:
                        # Recovery failed - include recovery details in failure
                        result.details["recovery_attempted"] = True
                        result.details["recovery_message"] = recovery_result.message
                        result.details["recovery_actions"] = recovery_result.actions_taken

                return self._transition(
                    PipelineState.FAILED,
                    False,
                    f"Pipeline failed at {stage_name}: {result.message}",
                    details={"failed_stage": stage_name, "stage_result": result.details},
                    errors=result.errors,
                )

        return self._transition(
            PipelineState.COMPLETED,
            True,
            "Pipeline completed successfully",
            details={
                "stages_completed": len(stages),
                "solve_result": self._history[-2].details if len(self._history) > 1 else {},
            },
        )

    def reset(self):
        """Reset pipeline to initial state."""
        self._state = PipelineState.IDLE
        self._history = []
