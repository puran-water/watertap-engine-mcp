# WaterTAP Engine MCP - Utils Module
"""Utility modules for WaterTAP MCP server."""

from .job_manager import JobManager, Job, JobStatus

# Note: model_builder requires WaterTAP/IDAES, only import in worker.py
# from .model_builder import ModelBuilder, ModelBuildError

__all__ = [
    "JobManager",
    "Job",
    "JobStatus",
]
