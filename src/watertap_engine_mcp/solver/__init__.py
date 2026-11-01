# WaterTAP Engine MCP - Solver Module
"""Solver hygiene pipeline modules for WaterTAP flowsheet building."""

from .diagnostics import DiagnosticResult, DiagnosticsRunner
from .dof_resolver import DOFResolver, DOFStatus
from .initializer import FlowsheetInitializer, InitializationResult
from .pipeline import HygienePipeline, PipelineConfig, PipelineResult, PipelineState
from .recovery import (
    FailureAnalyzer,
    FailureType,
    RecoveryExecutor,
    RecoveryStrategy,
    analyze_and_suggest_recovery,
)
from .scaler import ScalingApplication, ScalingIssue, ScalingTools

__all__ = [
    "DOFResolver",
    "DOFStatus",
    "ScalingTools",
    "ScalingApplication",
    "ScalingIssue",
    "FlowsheetInitializer",
    "InitializationResult",
    "DiagnosticsRunner",
    "DiagnosticResult",
    "HygienePipeline",
    "PipelineState",
    "PipelineResult",
    "PipelineConfig",
    "FailureAnalyzer",
    "RecoveryExecutor",
    "RecoveryStrategy",
    "FailureType",
    "analyze_and_suggest_recovery",
]
