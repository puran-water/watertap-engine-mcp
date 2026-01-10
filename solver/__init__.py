# WaterTAP Engine MCP - Solver Module
"""Solver hygiene pipeline modules for WaterTAP flowsheet building."""

from .dof_resolver import DOFResolver, DOFStatus
from .scaler import ScalingTools, ScalingIssue
from .initializer import FlowsheetInitializer, InitializationResult
from .diagnostics import DiagnosticsRunner, DiagnosticResult
from .pipeline import HygienePipeline, PipelineState, PipelineResult, PipelineConfig
from .recovery import (
    FailureAnalyzer,
    RecoveryExecutor,
    RecoveryStrategy,
    FailureType,
    analyze_and_suggest_recovery,
)

__all__ = [
    "DOFResolver",
    "DOFStatus",
    "ScalingTools",
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
