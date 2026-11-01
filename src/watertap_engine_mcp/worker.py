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
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from watertap_engine_mcp.utils.job_manager import update_job_from_worker, JobStatus
from watertap_engine_mcp.security.redaction import redact_public_payload

def update_status(jobs_dir: Path, job_id: str, **kwargs):
    """Update job status."""
    safe_kwargs = redact_public_payload(kwargs)
    if 'status' in kwargs:
        safe_kwargs['status'] = kwargs['status']
    update_job_from_worker(jobs_dir, job_id, **safe_kwargs)

def _session_storage_dir(jobs_dir: Path, params: dict | None=None) -> Path:
    """Resolve the flowsheet session storage directory for a worker job."""
    if params:
        override = params.get('session_storage_dir')
        if override:
            return Path(override)
    return jobs_dir.parent / 'flowsheets'

def _load_session_for_worker(jobs_dir: Path, session_id: str, params: dict | None=None):
    """Load a worker session, falling back to an embedded snapshot when needed."""
    from watertap_engine_mcp.core.session import FlowsheetSession, SessionManager
    session_manager = SessionManager(_session_storage_dir(jobs_dir, params))
    try:
        return session_manager.load(session_id)
    except FileNotFoundError:
        snapshot = (params or {}).get('session_snapshot')
        if snapshot is None:
            raise
        session = FlowsheetSession.from_dict(snapshot)
        try:
            session_manager.save(session)
        except Exception:
            pass
        return session

def _attach_runtime_diagnostics(result: dict, runtime_metadata: dict | None) -> None:
    """Copy build-time runtime diagnostics into the client-visible result."""
    if not isinstance(result, dict) or not isinstance(runtime_metadata, dict):
        return
    from watertap_engine_mcp.core.modified_asm2d_kinetics import MODIFIED_ASM2D_KINETICS_KEY
    from watertap_engine_mcp.core.modified_asm2d_reaction_config import BUILT_REACTION_CONFIG_KEY, REACTION_CONFIG_TARGET
    if isinstance(runtime_metadata.get(MODIFIED_ASM2D_KINETICS_KEY), dict):
        result[MODIFIED_ASM2D_KINETICS_KEY] = runtime_metadata[MODIFIED_ASM2D_KINETICS_KEY]
    if isinstance(runtime_metadata.get(BUILT_REACTION_CONFIG_KEY), dict):
        result[REACTION_CONFIG_TARGET] = runtime_metadata[BUILT_REACTION_CONFIG_KEY]
    pressure_reference = runtime_metadata.get('asm2d_pressure_reference')
    if isinstance(pressure_reference, dict):
        result['asm2d_pressure_reference'] = pressure_reference
        if not isinstance(result.get('runtime_metadata'), dict):
            result['runtime_metadata'] = {}
        result['runtime_metadata']['asm2d_pressure_reference'] = pressure_reference
        process_diagnostics = result.get('process_diagnostics')
        if isinstance(process_diagnostics, dict):
            process_diagnostics['asm2d_pressure_reference'] = pressure_reference

def persist_results_to_session(jobs_dir: Path, session_id: str, result: dict, success: bool, message: str='', params: dict | None=None):
    """Persist job results to the session state.

    Args:
        jobs_dir: Jobs directory (used to locate flowsheets dir)
        session_id: Session to update
        result: Result dict from job
        success: Whether solve was successful
        message: Optional failure message
    """
    try:
        from watertap_engine_mcp.core.session import SessionManager
        session_manager = SessionManager(_session_storage_dir(jobs_dir, params))
        session = _load_session_for_worker(jobs_dir, session_id, params)
        metadata_updates = result.get('runtime_metadata') if isinstance(result, dict) else None
        if isinstance(metadata_updates, dict):
            session.config.runtime_metadata.update(metadata_updates)
        _attach_runtime_diagnostics(result, session.config.runtime_metadata)
        if success:
            session.set_solved(result)
        else:
            session.set_failed(message or 'Solve failed')
            session.results = result
        session_manager.save(session)
    except Exception as e:
        print(f'Warning: Failed to persist results to session: {e}', file=sys.stderr)

def _auto_persist_successful_outputs(jobs_dir: Path, session_id: str, params: dict | None) -> dict:
    """Persist canonical artifacts after a successful server-owned solve."""
    try:
        from watertap_engine_mcp.core.session import SessionManager
        import watertap_engine_mcp.actions as actions
        session_manager = SessionManager(_session_storage_dir(jobs_dir, params))
        session = _load_session_for_worker(jobs_dir, session_id, params)
        metadata = session.config.runtime_metadata or {}
        project_ref = (params or {}).get('project_ref') or metadata.get('project_ref')
        if not isinstance(project_ref, str) or not project_ref.strip():
            return {'skipped': 'project_ref not provided'}
        plant_slug = (params or {}).get('plant_slug') or metadata.get('plant_slug')
        workflow_run_id = (params or {}).get('workflow_run_id') or metadata.get('workflow_run_id')
        mcp_outputs_dir = (params or {}).get('mcp_outputs_dir') or metadata.get('mcp_outputs_dir')
        original_session_manager = actions.session_manager
        actions.session_manager = session_manager
        try:
            return actions.persist_simulation_artifacts(session_id=session_id, project_ref=project_ref, plant_slug=plant_slug if isinstance(plant_slug, str) else None, workflow_run_id=workflow_run_id if isinstance(workflow_run_id, str) else None, mcp_outputs_dir=mcp_outputs_dir if isinstance(mcp_outputs_dir, str) else None)
        finally:
            actions.session_manager = original_session_manager
    except Exception as exc:
        return {'error': f'Auto-persistence failed: {exc}'}

def _extract_solver_infeasibility_report(result: dict | None) -> dict | None:
    """Return the nested solver infeasibility report from a pipeline payload."""
    if not isinstance(result, dict):
        return None
    details = result.get('details') if isinstance(result.get('details'), dict) else {}
    report = details.get('solver_infeasibility_report')
    if isinstance(report, dict):
        return report
    stage_result = details.get('stage_result')
    if isinstance(stage_result, dict):
        report = stage_result.get('solver_infeasibility_report')
        if isinstance(report, dict):
            return report
    return None

def _auto_persist_solver_infeasibility_report(jobs_dir: Path, session_id: str, result: dict, params: dict | None) -> dict:
    """Persist solver infeasibility report after a diagnostic solve failure."""
    report = _extract_solver_infeasibility_report(result)
    if report is None:
        return {'skipped': 'solver_infeasibility_report not present'}
    try:
        from watertap_engine_mcp.core.session import SessionManager
        import watertap_engine_mcp.actions as actions
        session_manager = SessionManager(_session_storage_dir(jobs_dir, params))
        session = _load_session_for_worker(jobs_dir, session_id, params)
        metadata = session.config.runtime_metadata or {}
        project_ref = (params or {}).get('project_ref') or metadata.get('project_ref')
        if not isinstance(project_ref, str) or not project_ref.strip():
            return {'skipped': 'project_ref not provided'}
        plant_slug = (params or {}).get('plant_slug') or metadata.get('plant_slug')
        original_session_manager = actions.session_manager
        actions.session_manager = session_manager
        try:
            return actions.persist_solver_infeasibility_report(report, project_ref=project_ref, plant_slug=plant_slug if isinstance(plant_slug, str) else None, session_id=session_id)
        finally:
            actions.session_manager = original_session_manager
    except Exception as exc:
        return {'error': f'Solver infeasibility report persistence failed: {exc}'}

def _solve_attached_costing(builder, model, solver_options: dict | None=None) -> dict:
    """Attach costing to an already-solved model and solve the costing equations."""
    from pyomo.opt import TerminationCondition
    from watertap.core.solvers import get_solver

    def _costing_metrics_resolved(payload: dict) -> bool:
        if payload.get('status') != 'complete':
            return False
        if payload.get('aggregate_capital_cost') is None:
            return False
        if payload.get('total_capital_cost') is None or payload.get('total_operating_cost') is None:
            return False
        if payload.get('product_stream', {}).get('resolved') and payload.get('LCOW') is None:
            return False
        for unit_payload in (payload.get('unit_costs') or {}).values():
            if any((value is None for value in unit_payload.values())):
                return False
        return True
    report = builder.attach_costing()
    requested_units = report.get('requested_units', [])
    if not requested_units:
        return {}
    if report.get('status') != 'complete':
        raise RuntimeError(f'Costing attach failed: {report}')
    builder.initialize_costing_values()
    initialized_payload = builder.extract_costing_results(source='solved_model')
    if _costing_metrics_resolved(initialized_payload):
        return initialized_payload
    solver = get_solver()
    for key, value in (solver_options or {}).items():
        solver.options[key] = value
    results = solver.solve(model, tee=False)
    if results.solver.termination_condition != TerminationCondition.optimal:
        raise RuntimeError(f'Costed solve failed: {results.solver.status} / {results.solver.termination_condition}')
    return builder.extract_costing_results(source='solved_model')

def _extract_solved_kpis(model, units: dict) -> dict:
    """Extract key performance indicators from solved model.

    Args:
        model: Solved Pyomo model
        units: Dict of unit_id -> unit_block

    Returns:
        JSON-serializable dict with streams and unit KPIs
    """
    from watertap_engine_mcp.results.kpis import extract_solved_kpis
    return extract_solved_kpis(model, units)
from watertap_engine_mcp.results.solved_state import _extract_process_diagnostics, _extract_solved_state

def run_full_pipeline(jobs_dir: Path, job_id: str, session_id: str, params: dict):
    """Execute full hygiene pipeline (DOF check → scaling → init → solve).

    Args:
        jobs_dir: Jobs directory for status updates
        job_id: Job identifier
        session_id: Flowsheet session to solve
        params: Pipeline parameters
    """
    try:
        update_status(jobs_dir, job_id, progress=5, message='Loading session...')
        session = _load_session_for_worker(jobs_dir, session_id, params)
        update_status(jobs_dir, job_id, progress=10, message='Building flowsheet model...')
        from watertap_engine_mcp.utils.model_builder import ModelBuilder, ModelBuildError
        try:
            costing_requested = bool(session.costing_config and session.costing_config.get('enabled'))
            builder = ModelBuilder(session, include_costing=not costing_requested)
            m = builder.build()
            units = builder.get_units()
        except ModelBuildError as e:
            update_status(jobs_dir, job_id, status=JobStatus.FAILED, error=f'Model build failed: {e}. WaterTAP must be properly installed.')
            return
        from watertap_engine_mcp.api.errors import SolverDispatchError
        from watertap_engine_mcp.solvers.dispatch import detect_problem_type, select_solver
        problem_type = detect_problem_type(m)
        try:
            selected_solver = select_solver(params.get('solver'), problem_type)
        except SolverDispatchError as e:
            error = e.to_error()
            update_status(jobs_dir, job_id, status=JobStatus.FAILED, progress=100, message=error.message, error=error.message, result={'state': 'failed', 'details': {'solver_dispatch': error.model_dump(mode='python')}, 'errors': [error.message]})
            return
        params['solver'] = selected_solver
        params['detected_problem_type'] = problem_type.value
        from watertap_engine_mcp.solver.pipeline import HygienePipeline, PipelineConfig, PipelineState
        config = PipelineConfig(auto_scale=params.get('auto_scale', True), interval_initialize=params.get('interval_initialize', True), sequential_init=params.get('sequential_init', True), propagate_state=params.get('propagate_state', True), solver=params.get('solver', 'ipopt'), solver_options=params.get('solver_options', {}), enable_relaxed_solve=params.get('enable_relaxed_solve', True), enable_progressive_tightening=params.get('enable_progressive_tightening', True), allow_near_feasible_asm2d=params.get('allow_near_feasible_asm2d', False), max_wall_time_s=int(params.get('max_wall_time_s', 300)))
        pipeline = HygienePipeline(m, config, units=units, session=session, builder=builder)

        def on_stage_complete(result):
            """Update job progress after each pipeline stage."""
            progress_map = {PipelineState.DOF_CHECK: 20, PipelineState.SCALING: 35, PipelineState.INITIALIZATION: 50, PipelineState.PRE_SOLVE_DIAGNOSTICS: 65, PipelineState.PROGRESSIVE_TIGHTENING: 72, PipelineState.SOLVING: 80, PipelineState.RELAXED_SOLVE: 85, PipelineState.POST_SOLVE_DIAGNOSTICS: 90, PipelineState.OPTIMIZATION_SOLVE: 95, PipelineState.COMPLETED: 100, PipelineState.FAILED: 100}
            progress = progress_map.get(result.state, 50)
            update_status(jobs_dir, job_id, progress=progress, message=f'Stage {result.state.value}: {result.message}')
        update_status(jobs_dir, job_id, progress=15, message='Running hygiene pipeline...')
        result = pipeline.run_all(on_stage_complete=on_stage_complete)
        if result.success:
            result_dict = {'state': result.state.value, 'details': result.details, 'history': [{'state': h.state.value, 'success': h.success, 'message': h.message, 'details': h.details, 'warnings': h.warnings, 'errors': h.errors} for h in pipeline.history], 'recovered': bool(result.details.get('recovered', False)), 'initial_solve_result': result.details.get('initial_solve_result'), 'recovery': result.details.get('recovery')}
            try:
                kpis = _extract_solved_kpis(m, units)
                result_dict['kpis'] = kpis
            except Exception as e:
                print(f'Warning: Failed to extract KPIs: {e}', file=sys.stderr)
                result_dict['kpis'] = {}
            try:
                solved_state = _extract_solved_state(units, session.units, model=m)
                result_dict['solved_state'] = solved_state
                result_dict['process_diagnostics'] = _extract_process_diagnostics(m, units, session.units, solved_state, session.config.runtime_metadata)
            except Exception as e:
                print(f'Warning: Failed to extract solved_state/process diagnostics: {e}', file=sys.stderr)
                result_dict['solved_state'] = {}
                result_dict['process_diagnostics'] = {'schema_version': 'watertap_process_diagnostics/v1', 'error': str(e)}
            _attach_runtime_diagnostics(result_dict, session.config.runtime_metadata)
            try:
                costing = _solve_attached_costing(builder, m, params.get('solver_options', {}))
                if costing:
                    result_dict['costing'] = costing
            except Exception as e:
                update_status(jobs_dir, job_id, status=JobStatus.FAILED, progress=100, message=f'Pipeline failed: costing stage failed: {e}', error=f'Costing stage failed: {e}')
                persist_results_to_session(jobs_dir, session_id, result_dict, success=False, message=f'Costing stage failed: {e}', params=params)
                return
            try:
                from watertap_engine_mcp.actions import _flowsheet_certification
                certification = _flowsheet_certification(session, builder=builder)
                if isinstance(result_dict.get('costing'), dict):
                    certification['product_stream'] = result_dict['costing'].get('product_stream')
                    certification['costing_blocked_by_missing_product_stream'] = not bool((result_dict['costing'].get('product_stream') or {}).get('resolved'))
                    certification['cost_certified'] = result_dict['costing'].get('status') == 'complete'
                result_dict['certification'] = certification
            except Exception as e:
                print(f'Warning: Failed to extract certification: {e}', file=sys.stderr)
            from watertap_engine_mcp.core.rating_geometry import verify_live_geometry
            verify_live_geometry(session.config.runtime_metadata.get('rating_geometry'), units)
            persist_results_to_session(jobs_dir, session_id, result_dict, success=True, params=params)
            artifact_persistence = _auto_persist_successful_outputs(jobs_dir, session_id, params)
            if artifact_persistence:
                result_dict['artifact_persistence'] = artifact_persistence
                persist_results_to_session(jobs_dir, session_id, result_dict, success=True, params=params)
            update_status(jobs_dir, job_id, status=JobStatus.COMPLETED, progress=100, message='Pipeline completed successfully', result=result_dict)
        else:
            result_dict = {'state': result.state.value, 'details': result.details, 'errors': result.errors, 'history': [{'state': h.state.value, 'success': h.success, 'message': h.message, 'details': h.details, 'warnings': h.warnings, 'errors': h.errors} for h in pipeline.history], 'recovered': bool(result.details.get('recovered', False)), 'initial_solve_result': result.details.get('initial_solve_result'), 'recovery': result.details.get('recovery')}
            if result.details.get('convergence_status'):
                result_dict['convergence_status'] = result.details['convergence_status']
            _attach_runtime_diagnostics(result_dict, session.config.runtime_metadata)
            report_persistence = _auto_persist_solver_infeasibility_report(jobs_dir, session_id, result_dict, params)
            if report_persistence:
                result_dict['solver_infeasibility_report_persistence'] = report_persistence
            persist_results_to_session(jobs_dir, session_id, result_dict, success=False, message=result.message, params=params)
            if result_dict.get('convergence_status') == 'solver_infeasible_with_diagnostic':
                update_status(jobs_dir, job_id, status=JobStatus.COMPLETED, progress=100, message='Pipeline produced solver infeasibility diagnostic', result=result_dict)
                return
            update_status(jobs_dir, job_id, status=JobStatus.FAILED, progress=100, message=f'Pipeline failed: {result.message}', error=result.message, result=result_dict)
    except ImportError as e:
        update_status(jobs_dir, job_id, status=JobStatus.FAILED, error=f'WaterTAP/IDAES import failed: {e}')
    except Exception as e:
        update_status(jobs_dir, job_id, status=JobStatus.FAILED, error=f'{type(e).__name__}: {e}\n{traceback.format_exc()}')

def run_solve(jobs_dir: Path, job_id: str, session_id: str, params: dict):
    """Execute solve through the canonical hygiene pipeline."""
    return run_full_pipeline(jobs_dir, job_id, session_id, params or {})

def run_initialize(jobs_dir: Path, job_id: str, session_id: str, params: dict):
    """Execute an initialization operation.

    Args:
        jobs_dir: Jobs directory for status updates
        job_id: Job identifier
        session_id: Flowsheet session to initialize
        params: Initialization parameters
    """
    try:
        update_status(jobs_dir, job_id, progress=10, message='Loading session...')
        from idaes.core.util.model_statistics import degrees_of_freedom
        session = _load_session_for_worker(jobs_dir, session_id, params)
        update_status(jobs_dir, job_id, progress=30, message='Building model...')
        from watertap_engine_mcp.utils.model_builder import ModelBuilder, ModelBuildError
        try:
            builder = ModelBuilder(session)
            m = builder.build()
            units = builder.get_units()
            update_status(jobs_dir, job_id, progress=40, message='Model built successfully')
        except ModelBuildError as e:
            update_status(jobs_dir, job_id, status=JobStatus.FAILED, error=f'Model build failed: {e}')
            return
        update_status(jobs_dir, job_id, progress=50, message='Computing initialization order...')
        from watertap_engine_mcp.utils.topo_sort import compute_initialization_order, SequentialDecompositionError
        connections = [{'src_unit': conn.source_unit, 'src_port': conn.source_port, 'dest_unit': conn.dest_unit, 'dest_port': conn.dest_port} for conn in session.connections]
        if 'init_order' in params:
            init_order = params['init_order']
        else:
            try:
                init_order = compute_initialization_order(units={uid: units.get(uid) for uid in session.units.keys()}, connections=connections, tear_streams=params.get('tear_streams'), model=m)
            except SequentialDecompositionError as e:
                update_status(jobs_dir, job_id, status=JobStatus.FAILED, error=f'Failed to compute initialization order: {e}')
                return
        update_status(jobs_dir, job_id, progress=55, message='Initializing units...')
        init_results = {}
        for i, unit_id in enumerate(init_order):
            progress = 50 + int(40 * (i + 1) / len(init_order))
            update_status(jobs_dir, job_id, progress=progress, message=f'Initializing {unit_id}...')
            try:
                unit_block = units.get(unit_id)
                if unit_block is None:
                    init_results[unit_id] = {'status': 'skipped', 'error': 'Unit not found in model'}
                    continue
                dof_before = degrees_of_freedom(unit_block)
                if hasattr(unit_block, 'initialize_build'):
                    unit_block.initialize_build()
                elif hasattr(unit_block, 'initialize'):
                    unit_block.initialize()
                dof_after = degrees_of_freedom(unit_block)
                init_results[unit_id] = {'status': 'success', 'dof_before': dof_before, 'dof_after': dof_after}
            except Exception as e:
                init_results[unit_id] = {'status': 'failed', 'error': str(e)}
        update_status(jobs_dir, job_id, status=JobStatus.COMPLETED, progress=100, message='Initialization completed', result={'initialized': True, 'init_order': init_order, 'unit_results': init_results})
    except ImportError as e:
        update_status(jobs_dir, job_id, status=JobStatus.FAILED, error=f'WaterTAP/IDAES import failed: {e}')
    except Exception as e:
        update_status(jobs_dir, job_id, status=JobStatus.FAILED, error=f'{type(e).__name__}: {e}\n{traceback.format_exc()}')

def run_diagnose(jobs_dir: Path, job_id: str, session_id: str, params: dict):
    """Execute a diagnostics operation.

    Args:
        jobs_dir: Jobs directory for status updates
        job_id: Job identifier
        session_id: Flowsheet session to diagnose
        params: Diagnostics parameters
    """
    try:
        update_status(jobs_dir, job_id, progress=10, message='Loading session...')
        from pyomo.environ import Var, Constraint, value
        from watertap_engine_mcp.core.session import SessionManager
        session_manager = SessionManager(_session_storage_dir(jobs_dir, params))
        session = session_manager.load(session_id)
        update_status(jobs_dir, job_id, progress=30, message='Building model...')
        from watertap_engine_mcp.utils.model_builder import ModelBuilder, ModelBuildError
        try:
            builder = ModelBuilder(session)
            m = builder.build()
            update_status(jobs_dir, job_id, progress=40, message='Model built successfully')
        except ModelBuildError as e:
            update_status(jobs_dir, job_id, status=JobStatus.FAILED, error=f'Model build failed: {e}')
            return
        update_status(jobs_dir, job_id, progress=50, message='Running diagnostics...')
        structural_issues = []
        numerical_issues = []
        constraint_residuals = []
        bound_violations = []
        try:
            from idaes.core.util.diagnostics_tools.diagnostics_toolbox import DiagnosticsToolbox
            dt = DiagnosticsToolbox(m)
            update_status(jobs_dir, job_id, progress=60, message='Checking structural issues...')
            try:
                dt.report_structural_issues()
            except Exception as e:
                structural_issues.append(f'Structural analysis error: {e}')
            update_status(jobs_dir, job_id, progress=75, message='Checking numerical issues...')
            try:
                dt.report_numerical_issues()
            except Exception as e:
                numerical_issues.append(f'Numerical analysis error: {e}')
        except ImportError:
            structural_issues.append('DiagnosticsToolbox not available')
        threshold = params.get('threshold', 1e-06)
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
                            constraint_residuals.append({'name': str(c), 'residual': residual})
            except Exception:
                pass
        for v in m.component_data_objects(Var, active=True, descend_into=True):
            try:
                val = value(v, exception=False)
                if val is None:
                    continue
                if v.lb is not None and val < v.lb - 1e-08:
                    bound_violations.append({'name': str(v), 'value': val, 'bound': v.lb, 'type': 'below_lower'})
                if v.ub is not None and val > v.ub + 1e-08:
                    bound_violations.append({'name': str(v), 'value': val, 'bound': v.ub, 'type': 'above_upper'})
            except Exception:
                pass
        update_status(jobs_dir, job_id, status=JobStatus.COMPLETED, progress=100, message='Diagnostics completed', result={'structural_issues': structural_issues, 'numerical_issues': numerical_issues, 'constraint_residuals': constraint_residuals[:20], 'bound_violations': bound_violations[:20]})
    except ImportError as e:
        update_status(jobs_dir, job_id, status=JobStatus.FAILED, error=f'WaterTAP/IDAES import failed: {e}')
    except Exception as e:
        update_status(jobs_dir, job_id, status=JobStatus.FAILED, error=f'{type(e).__name__}: {e}\n{traceback.format_exc()}')

def main():
    """Main entry point for worker."""
    if len(sys.argv) != 2:
        print('Usage: python worker.py <params_file>', file=sys.stderr)
        sys.exit(1)
    params_file = Path(sys.argv[1])
    if not params_file.exists():
        print(f'Params file not found: {params_file}', file=sys.stderr)
        sys.exit(1)
    with open(params_file) as f:
        data = json.load(f)
    job_id = data['job_id']
    session_id = data['session_id']
    job_type = data['job_type']
    params = data.get('params', {})
    jobs_dir = params_file.parent
    if job_type == 'solve':
        run_solve(jobs_dir, job_id, session_id, params)
    elif job_type == 'initialize':
        run_initialize(jobs_dir, job_id, session_id, params)
    elif job_type == 'diagnose':
        run_diagnose(jobs_dir, job_id, session_id, params)
    else:
        update_status(jobs_dir, job_id, status=JobStatus.FAILED, error=f'Unknown job type: {job_type}')
if __name__ == '__main__':
    main()
