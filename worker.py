#!/usr/bin/env python3
"""Background Worker for WaterTAP Solve Operations.

This script is executed as a subprocess by the JobManager to perform
long-running operations like flowsheet solving without blocking the
MCP server.

Usage:
    python worker.py <params_file>

The params file is a JSON file containing:
- job_id: Unique job identifier
- session_id: Associated flowsheet session
- job_type: Type of operation ("solve", "initialize", "diagnose")
- params: Operation-specific parameters
"""

import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from utils.job_manager import update_job_from_worker, JobStatus


def update_status(jobs_dir: Path, job_id: str, **kwargs):
    """Update job status."""
    update_job_from_worker(jobs_dir, job_id, **kwargs)


def run_full_pipeline(jobs_dir: Path, job_id: str, session_id: str, params: dict):
    """Execute full hygiene pipeline (DOF check → scaling → init → solve).

    Args:
        jobs_dir: Jobs directory for status updates
        job_id: Job identifier
        session_id: Flowsheet session to solve
        params: Pipeline parameters
    """
    try:
        update_status(jobs_dir, job_id, progress=5, message="Loading session...")

        from pyomo.environ import ConcreteModel
        from idaes.core import FlowsheetBlock

        # Load session
        from core.session import SessionManager
        session_manager = SessionManager(jobs_dir.parent / "flowsheets")
        session = session_manager.load(session_id)

        update_status(jobs_dir, job_id, progress=10, message="Building flowsheet model...")

        # Build model from session state
        from utils.model_builder import ModelBuilder, ModelBuildError

        try:
            builder = ModelBuilder(session)
            m = builder.build()
            units = builder.get_units()
        except ModelBuildError as e:
            # FAIL LOUDLY - no silent fallback to empty model
            update_status(
                jobs_dir, job_id,
                status=JobStatus.FAILED,
                error=f"Model build failed: {e}. WaterTAP must be properly installed.",
            )
            return

        # Import and run hygiene pipeline
        from solver.pipeline import HygienePipeline, PipelineConfig, PipelineState

        config = PipelineConfig(
            auto_scale=params.get("auto_scale", True),
            sequential_init=params.get("sequential_init", True),
            propagate_state=params.get("propagate_state", True),
            solver_options=params.get("solver_options", {}),
            enable_relaxed_solve=params.get("enable_relaxed_solve", True),
        )

        pipeline = HygienePipeline(m, config)

        def on_stage_complete(result):
            """Update job progress after each pipeline stage."""
            progress_map = {
                PipelineState.DOF_CHECK: 20,
                PipelineState.SCALING: 35,
                PipelineState.INITIALIZATION: 50,
                PipelineState.PRE_SOLVE_DIAGNOSTICS: 65,
                PipelineState.SOLVING: 80,
                PipelineState.POST_SOLVE_DIAGNOSTICS: 90,
                PipelineState.COMPLETED: 100,
                PipelineState.FAILED: 100,
            }
            progress = progress_map.get(result.state, 50)
            update_status(
                jobs_dir, job_id,
                progress=progress,
                message=f"Stage {result.state.value}: {result.message}",
            )

        update_status(jobs_dir, job_id, progress=15, message="Running hygiene pipeline...")
        result = pipeline.run_full_pipeline(on_stage_complete=on_stage_complete)

        if result.success:
            update_status(
                jobs_dir, job_id,
                status=JobStatus.COMPLETED,
                progress=100,
                message="Pipeline completed successfully",
                result={
                    "state": result.state.value,
                    "details": result.details,
                    "history": [
                        {"state": h.state.value, "success": h.success, "message": h.message}
                        for h in pipeline.history
                    ],
                },
            )
        else:
            update_status(
                jobs_dir, job_id,
                status=JobStatus.FAILED,
                progress=100,
                message=f"Pipeline failed: {result.message}",
                error=result.message,
                result={
                    "state": result.state.value,
                    "details": result.details,
                    "errors": result.errors,
                    "history": [
                        {"state": h.state.value, "success": h.success, "message": h.message}
                        for h in pipeline.history
                    ],
                },
            )

    except ImportError as e:
        update_status(
            jobs_dir, job_id,
            status=JobStatus.FAILED,
            error=f"Import error: {e}. Ensure WaterTAP is installed.",
        )
    except Exception as e:
        update_status(
            jobs_dir, job_id,
            status=JobStatus.FAILED,
            error=f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
        )


def run_solve(jobs_dir: Path, job_id: str, session_id: str, params: dict):
    """Execute a solve operation.

    Args:
        jobs_dir: Jobs directory for status updates
        job_id: Job identifier
        session_id: Flowsheet session to solve
        params: Solve parameters (solver options, etc.)
    """
    # Check if full pipeline requested
    if params.get("run_full_pipeline", False):
        return run_full_pipeline(jobs_dir, job_id, session_id, params)

    try:
        update_status(jobs_dir, job_id, progress=10, message="Loading session...")

        # Import WaterTAP/IDAES (heavy imports done here, not in server)
        from pyomo.environ import ConcreteModel, value
        from idaes.core import FlowsheetBlock
        from idaes.core.util.model_statistics import degrees_of_freedom
        from watertap.core.solvers import get_solver
        import idaes.core.util.scaling as iscale

        update_status(jobs_dir, job_id, progress=20, message="Building flowsheet...")

        # Load session from disk
        from core.session import SessionManager
        session_manager = SessionManager(jobs_dir.parent / "flowsheets")
        session = session_manager.load(session_id)

        # Build the Pyomo model from session state
        from utils.model_builder import ModelBuilder, ModelBuildError

        try:
            builder = ModelBuilder(session)
            m = builder.build()
            units = builder.get_units()
            update_status(jobs_dir, job_id, progress=35, message="Model built successfully")
        except ModelBuildError as e:
            # FAIL LOUDLY - no silent fallback to empty model
            update_status(
                jobs_dir, job_id,
                status=JobStatus.FAILED,
                error=f"Model build failed: {e}. WaterTAP must be properly installed.",
            )
            return

        update_status(jobs_dir, job_id, progress=40, message="Checking DOF...")

        # Check degrees of freedom
        dof = degrees_of_freedom(m)
        if dof != 0:
            update_status(jobs_dir, job_id, progress=45,
                         message=f"DOF = {dof} (should be 0), continuing...")

        update_status(jobs_dir, job_id, progress=50, message="Calculating scaling factors...")

        # Apply scaling
        iscale.calculate_scaling_factors(m)

        update_status(jobs_dir, job_id, progress=60, message="Initializing flowsheet...")

        # Get initialization order using IDAES SequentialDecomposition
        from utils.topo_sort import compute_initialization_order, SequentialDecompositionError

        connections = [
            {
                "src_unit": conn.source_unit,
                "src_port": conn.source_port,
                "dest_unit": conn.dest_unit,
                "dest_port": conn.dest_port,
            }
            for conn in session.connections
        ]

        try:
            init_order = compute_initialization_order(
                units={uid: units.get(uid) for uid in session.units.keys()},
                connections=connections,
                tear_streams=None,
                model=m,
            )
        except SequentialDecompositionError as e:
            # FAIL LOUDLY - SequentialDecomposition is required
            update_status(
                jobs_dir, job_id,
                status=JobStatus.FAILED,
                error=f"IDAES SequentialDecomposition failed: {e}. Check flowsheet structure.",
            )
            return

        # Initialize units in SequentialDecomposition order
        for unit_id in init_order:
            if unit_id not in units:
                continue
            unit_block = units[unit_id]
            try:
                if hasattr(unit_block, 'initialize_build'):
                    unit_block.initialize_build()
                elif hasattr(unit_block, 'initialize'):
                    unit_block.initialize()
            except Exception as e:
                update_status(jobs_dir, job_id, progress=65,
                             message=f"Init warning for {unit_id}: {e}")

        update_status(jobs_dir, job_id, progress=70, message="Solving...")

        # Get solver and solve
        solver = get_solver()
        solver_options = params.get("solver_options", {})
        for opt, val in solver_options.items():
            solver.options[opt] = val

        # Solve the model
        results = solver.solve(m, tee=False)

        update_status(jobs_dir, job_id, progress=90, message="Extracting results...")

        # Extract results
        solve_status = str(results.solver.status)
        termination = str(results.solver.termination_condition)

        result = {
            "solver_status": solve_status,
            "termination_condition": termination,
            "solve_time": getattr(results.solver, "wallclock_time", None),
            "iterations": getattr(results.solver, "iterations", None),
        }

        if termination == "optimal":
            update_status(
                jobs_dir, job_id,
                status=JobStatus.COMPLETED,
                progress=100,
                message="Solve completed successfully",
                result=result,
            )
        else:
            update_status(
                jobs_dir, job_id,
                status=JobStatus.FAILED,
                progress=100,
                message=f"Solve failed: {termination}",
                error=f"Termination condition: {termination}",
                result=result,
            )

    except ImportError as e:
        update_status(
            jobs_dir, job_id,
            status=JobStatus.FAILED,
            error=f"Import error: {e}. Ensure WaterTAP is installed.",
        )
    except Exception as e:
        update_status(
            jobs_dir, job_id,
            status=JobStatus.FAILED,
            error=f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
        )


def run_initialize(jobs_dir: Path, job_id: str, session_id: str, params: dict):
    """Execute an initialization operation.

    Args:
        jobs_dir: Jobs directory for status updates
        job_id: Job identifier
        session_id: Flowsheet session to initialize
        params: Initialization parameters
    """
    try:
        update_status(jobs_dir, job_id, progress=10, message="Loading session...")

        from pyomo.environ import ConcreteModel
        from idaes.core import FlowsheetBlock
        from idaes.core.util.initialization import propagate_state
        from idaes.core.util.model_statistics import degrees_of_freedom

        # Load session
        from core.session import SessionManager
        session_manager = SessionManager(jobs_dir.parent / "flowsheets")
        session = session_manager.load(session_id)

        update_status(jobs_dir, job_id, progress=30, message="Building model...")

        # Build model using ModelBuilder
        from utils.model_builder import ModelBuilder, ModelBuildError

        try:
            builder = ModelBuilder(session)
            m = builder.build()
            units = builder.get_units()
            update_status(jobs_dir, job_id, progress=40, message="Model built successfully")
        except ModelBuildError as e:
            update_status(
                jobs_dir, job_id,
                status=JobStatus.FAILED,
                error=f"Model build failed: {e}",
            )
            return

        update_status(jobs_dir, job_id, progress=50, message="Computing initialization order...")

        # Get initialization order using IDAES SequentialDecomposition
        from utils.topo_sort import compute_initialization_order, SequentialDecompositionError

        connections = [
            {
                "src_unit": conn.source_unit,
                "src_port": conn.source_port,
                "dest_unit": conn.dest_unit,
                "dest_port": conn.dest_port,
            }
            for conn in session.connections
        ]

        # Use params-provided order if given (for manual override), otherwise compute via SequentialDecomposition
        if "init_order" in params:
            init_order = params["init_order"]
        else:
            try:
                init_order = compute_initialization_order(
                    units={uid: units.get(uid) for uid in session.units.keys()},
                    connections=connections,
                    tear_streams=params.get("tear_streams"),
                    model=m,
                )
            except SequentialDecompositionError as e:
                update_status(
                    jobs_dir, job_id,
                    status=JobStatus.FAILED,
                    error=f"Failed to compute initialization order: {e}",
                )
                return

        update_status(jobs_dir, job_id, progress=55, message="Initializing units...")
        init_results = {}

        for i, unit_id in enumerate(init_order):
            progress = 50 + int(40 * (i + 1) / len(init_order))
            update_status(
                jobs_dir, job_id,
                progress=progress,
                message=f"Initializing {unit_id}..."
            )

            try:
                unit_block = units.get(unit_id)
                if unit_block is None:
                    init_results[unit_id] = {"status": "skipped", "error": "Unit not found in model"}
                    continue

                dof_before = degrees_of_freedom(unit_block)

                # Initialize unit using appropriate method
                if hasattr(unit_block, 'initialize_build'):
                    # RO, NF specific initialization
                    unit_block.initialize_build()
                elif hasattr(unit_block, 'initialize'):
                    unit_block.initialize()
                # Feed/Product units may not need initialization

                dof_after = degrees_of_freedom(unit_block)
                init_results[unit_id] = {
                    "status": "success",
                    "dof_before": dof_before,
                    "dof_after": dof_after,
                }
            except Exception as e:
                init_results[unit_id] = {"status": "failed", "error": str(e)}

        update_status(
            jobs_dir, job_id,
            status=JobStatus.COMPLETED,
            progress=100,
            message="Initialization completed",
            result={
                "initialized": True,
                "init_order": init_order,
                "unit_results": init_results,
            },
        )

    except ImportError as e:
        update_status(
            jobs_dir, job_id,
            status=JobStatus.FAILED,
            error=f"Import error: {e}. Ensure WaterTAP is installed.",
        )
    except Exception as e:
        update_status(
            jobs_dir, job_id,
            status=JobStatus.FAILED,
            error=f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
        )


def run_diagnose(jobs_dir: Path, job_id: str, session_id: str, params: dict):
    """Execute a diagnostics operation.

    Args:
        jobs_dir: Jobs directory for status updates
        job_id: Job identifier
        session_id: Flowsheet session to diagnose
        params: Diagnostics parameters
    """
    try:
        update_status(jobs_dir, job_id, progress=10, message="Loading session...")

        from pyomo.environ import ConcreteModel, Var, Constraint, value
        from idaes.core import FlowsheetBlock

        # Load session
        from core.session import SessionManager
        session_manager = SessionManager(jobs_dir.parent / "flowsheets")
        session = session_manager.load(session_id)

        update_status(jobs_dir, job_id, progress=30, message="Building model...")

        # Build model using ModelBuilder
        from utils.model_builder import ModelBuilder, ModelBuildError

        try:
            builder = ModelBuilder(session)
            m = builder.build()
            units = builder.get_units()
            update_status(jobs_dir, job_id, progress=40, message="Model built successfully")
        except ModelBuildError as e:
            update_status(
                jobs_dir, job_id,
                status=JobStatus.FAILED,
                error=f"Model build failed: {e}",
            )
            return

        update_status(jobs_dir, job_id, progress=50, message="Running diagnostics...")

        structural_issues = []
        numerical_issues = []
        constraint_residuals = []
        bound_violations = []

        # Try to use IDAES DiagnosticsToolbox
        try:
            from idaes.core.util.model_diagnostics import DiagnosticsToolbox

            dt = DiagnosticsToolbox(m)

            update_status(jobs_dir, job_id, progress=60, message="Checking structural issues...")

            # Run structural diagnostics
            try:
                dt.report_structural_issues()
                # Capture any warnings/issues from the report
            except Exception as e:
                structural_issues.append(f"Structural analysis error: {e}")

            update_status(jobs_dir, job_id, progress=75, message="Checking numerical issues...")

            # Run numerical diagnostics
            try:
                dt.report_numerical_issues()
                # Capture any warnings/issues from the report
            except Exception as e:
                numerical_issues.append(f"Numerical analysis error: {e}")

        except ImportError:
            structural_issues.append("DiagnosticsToolbox not available")

        # Manual residual check
        threshold = params.get("threshold", 1e-6)
        for c in m.component_data_objects(Constraint, active=True, descend_into=True):
            try:
                body = value(c.body, exception=False)
                if body is None:
                    continue
                if c.equality:
                    bound = value(c.lower, exception=False)
                    if bound is not None:
                        residual = abs(body - bound)
                        if residual > threshold:
                            constraint_residuals.append({
                                "name": str(c),
                                "residual": residual,
                            })
            except Exception:
                pass

        # Manual bound check
        for v in m.component_data_objects(Var, active=True, descend_into=True):
            try:
                val = value(v, exception=False)
                if val is None:
                    continue
                if v.lb is not None and val < v.lb - 1e-8:
                    bound_violations.append({
                        "name": str(v),
                        "value": val,
                        "bound": v.lb,
                        "type": "below_lower",
                    })
                if v.ub is not None and val > v.ub + 1e-8:
                    bound_violations.append({
                        "name": str(v),
                        "value": val,
                        "bound": v.ub,
                        "type": "above_upper",
                    })
            except Exception:
                pass

        update_status(
            jobs_dir, job_id,
            status=JobStatus.COMPLETED,
            progress=100,
            message="Diagnostics completed",
            result={
                "structural_issues": structural_issues,
                "numerical_issues": numerical_issues,
                "constraint_residuals": constraint_residuals[:20],
                "bound_violations": bound_violations[:20],
            },
        )

    except ImportError as e:
        update_status(
            jobs_dir, job_id,
            status=JobStatus.FAILED,
            error=f"Import error: {e}. Ensure WaterTAP is installed.",
        )
    except Exception as e:
        update_status(
            jobs_dir, job_id,
            status=JobStatus.FAILED,
            error=f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
        )


def main():
    """Main entry point for worker."""
    if len(sys.argv) != 2:
        print("Usage: python worker.py <params_file>", file=sys.stderr)
        sys.exit(1)

    params_file = Path(sys.argv[1])
    if not params_file.exists():
        print(f"Params file not found: {params_file}", file=sys.stderr)
        sys.exit(1)

    with open(params_file) as f:
        data = json.load(f)

    job_id = data["job_id"]
    session_id = data["session_id"]
    job_type = data["job_type"]
    params = data.get("params", {})

    jobs_dir = params_file.parent

    # Dispatch based on job type
    if job_type == "solve":
        run_solve(jobs_dir, job_id, session_id, params)
    elif job_type == "initialize":
        run_initialize(jobs_dir, job_id, session_id, params)
    elif job_type == "diagnose":
        run_diagnose(jobs_dir, job_id, session_id, params)
    else:
        update_status(
            jobs_dir, job_id,
            status=JobStatus.FAILED,
            error=f"Unknown job type: {job_type}",
        )


if __name__ == "__main__":
    main()
