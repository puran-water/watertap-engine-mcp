"""Tests for solver modules (DOF, scaling, init, diagnostics, pipeline)."""

import pytest
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solver import (
    DOFResolver,
    DOFStatus,
    ScalingTools,
    ScalingIssue,
    FlowsheetInitializer,
    InitializationResult,
    DiagnosticsRunner,
    DiagnosticResult,
    HygienePipeline,
    PipelineState,
    PipelineConfig,
    FailureAnalyzer,
    RecoveryStrategy,
    FailureType,
    analyze_and_suggest_recovery,
)


class TestDOFResolver:
    """Tests for DOF resolver."""

    def test_dof_status_enum(self):
        """DOFStatus should have expected values."""
        assert DOFStatus.UNDERSPECIFIED.value == "underspecified"
        assert DOFStatus.READY.value == "ready"  # DOF=0, ready to solve
        assert DOFStatus.OVERSPECIFIED.value == "overspecified"

    def test_resolver_initialization(self):
        """DOFResolver should initialize without model."""
        resolver = DOFResolver()
        assert resolver is not None

    def test_resolver_with_none_model(self):
        """Resolver should handle None model gracefully."""
        resolver = DOFResolver(model=None)
        # Should not crash when model is None
        assert resolver._model is None


class TestScalingTools:
    """Tests for scaling tools."""

    def test_scaling_issue_dataclass(self):
        """ScalingIssue should store issue details."""
        from solver.scaler import ScalingIssueType
        issue = ScalingIssue(
            issue_type=ScalingIssueType.UNSCALED_VAR,
            component_name="test_var",
            current_magnitude=1e-10,
            suggested_factor=1e10,
            message="Too small",
        )
        assert issue.component_name == "test_var"
        assert issue.current_magnitude == 1e-10

    def test_scaling_tools_initialization(self):
        """ScalingTools should initialize without model."""
        tools = ScalingTools()
        assert tools is not None


class TestFlowsheetInitializer:
    """Tests for flowsheet initializer."""

    def test_init_result_dataclass(self):
        """InitializationResult should store results."""
        from solver.initializer import InitStatus
        result = InitializationResult(
            unit_id="RO",
            status=InitStatus.SUCCESS,
            dof_before=5,
            dof_after=0,
            message="OK",
        )
        assert result.unit_id == "RO"
        assert result.status == InitStatus.SUCCESS

    def test_initializer_creation(self):
        """FlowsheetInitializer should initialize without model."""
        init = FlowsheetInitializer()
        assert init is not None


class TestDiagnosticsRunner:
    """Tests for diagnostics runner."""

    def test_diagnostics_runner_creation(self):
        """DiagnosticsRunner should initialize."""
        runner = DiagnosticsRunner()
        assert runner is not None


class TestHygienePipeline:
    """Tests for hygiene pipeline."""

    def test_pipeline_states(self):
        """Pipeline should have all expected states."""
        states = [s.value for s in PipelineState]
        assert "idle" in states
        assert "dof_check" in states
        assert "scaling" in states
        assert "initialization" in states
        assert "solving" in states
        assert "completed" in states
        assert "failed" in states

    def test_pipeline_config_defaults(self):
        """PipelineConfig should have sensible defaults."""
        config = PipelineConfig()
        assert config.auto_scale is True
        assert config.sequential_init is True
        assert config.enable_relaxed_solve is True

    def test_pipeline_creation(self):
        """HygienePipeline should create with default config."""
        pipeline = HygienePipeline()
        assert pipeline.state == PipelineState.IDLE

    def test_pipeline_reset(self):
        """Pipeline reset should clear state and history."""
        pipeline = HygienePipeline()
        pipeline.reset()
        assert pipeline.state == PipelineState.IDLE
        assert len(pipeline.history) == 0


class TestFailureAnalyzer:
    """Tests for failure analyzer."""

    def test_failure_types(self):
        """FailureType should have expected values."""
        assert FailureType.INFEASIBLE.value == "infeasible"
        assert FailureType.MAX_ITERATIONS.value == "max_iterations"
        assert FailureType.NUMERICAL_ERROR.value == "numerical_error"

    def test_recovery_strategies(self):
        """RecoveryStrategy should have expected values."""
        assert RecoveryStrategy.BOUND_RELAXATION.value == "bound_relaxation"
        assert RecoveryStrategy.SCALING_ADJUSTMENT.value == "scaling_adjustment"
        assert RecoveryStrategy.MANUAL_INTERVENTION.value == "manual_intervention"

    def test_analyzer_creation(self):
        """FailureAnalyzer should create successfully."""
        analyzer = FailureAnalyzer()
        assert analyzer is not None

    def test_analyze_infeasible(self):
        """Analyzer should detect infeasible failure type."""
        analyzer = FailureAnalyzer()
        analysis = analyzer.analyze_failure("infeasible")
        assert analysis.failure_type == FailureType.INFEASIBLE

    def test_analyze_max_iterations(self):
        """Analyzer should detect max iterations failure."""
        analyzer = FailureAnalyzer()
        analysis = analyzer.analyze_failure("maxIterations")
        assert analysis.failure_type == FailureType.MAX_ITERATIONS

    def test_analyze_with_residuals(self):
        """Analyzer should include constraint info in analysis."""
        analyzer = FailureAnalyzer()
        residuals = [{"name": "fs.RO.flux_mass", "residual": 1e-3}]
        analysis = analyzer.analyze_failure("infeasible", constraint_residuals=residuals)
        assert len(analysis.related_constraints) > 0

    def test_analyze_and_suggest_recovery_function(self):
        """Top-level function should return dict."""
        result = analyze_and_suggest_recovery("infeasible")
        assert "failure_type" in result
        assert "likely_causes" in result
        assert "suggested_strategies" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
