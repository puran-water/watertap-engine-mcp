"""Shared action catalog for MCP and CLI adapters."""
from __future__ import annotations
import inspect
from dataclasses import dataclass
from typing import Any, Callable, Sequence
from puran_mcp_support.engineering.pydantic_base import StrictFiniteBaseModel
from pydantic import BaseModel, ConfigDict, create_model
import watertap_engine_mcp.actions as actions
from watertap_engine_mcp.api.tools import request_model_for_action, response_model_for_action

@dataclass(frozen=True)
class ActionSpec:
    """Metadata for a callable action exposed through adapters."""
    id: str
    handler: Callable
    cli_path: tuple[str, ...]
    summary: str
    request_model: type[BaseModel]
    response_type: Any
    surface: str = 'workflow'

class _ActionRequestModel(StrictFiniteBaseModel):
    """Strict base for catalog-generated public request models."""
    model_config = ConfigDict(allow_inf_nan=False, extra='forbid', validate_assignment=True, arbitrary_types_allowed=True)

def _summary(handler: Callable) -> str:
    doc = (handler.__doc__ or '').strip().splitlines()
    return doc[0] if doc else handler.__name__.replace('_', ' ')

def _request_model(action_id: str, handler: Callable) -> type[BaseModel]:
    """Build a Pydantic request model from a handler signature."""
    signature = inspect.signature(handler)
    fields: dict[str, tuple[Any, Any]] = {}
    for name, parameter in signature.parameters.items():
        annotation = parameter.annotation if parameter.annotation is not inspect._empty else Any
        default = parameter.default if parameter.default is not inspect._empty else ...
        fields[name] = (annotation, default)
    return create_model(f'{action_id.title().replace('_', '')}Request', __base__=_ActionRequestModel, **fields)

def _spec(action_id: str, *cli_path: str, surface: str='workflow') -> ActionSpec:
    handler = getattr(actions, action_id)
    request_model = request_model_for_action(action_id) or _request_model(action_id, handler)
    response_type = response_model_for_action(action_id) or inspect.signature(handler).return_annotation
    return ActionSpec(id=action_id, handler=handler, cli_path=tuple(cli_path), summary=_summary(handler), request_model=request_model, response_type=response_type, surface=surface)
ACTIONS: tuple[ActionSpec, ...] = (_spec('create_session', 'session', 'create'), _spec('create_watertap_session', 'session', 'create-watertap'), _spec('list_session_presets', 'session', 'presets'), _spec('get_session_preset', 'session', 'preset'), _spec('list_downstream_handoff_profiles', 'handoff', 'profiles'), _spec('validate_downstream_handoff', 'handoff', 'validate'), _spec('get_session', 'session', 'get'), _spec('list_sessions', 'session', 'list'), _spec('delete_session', 'session', 'delete'), _spec('list_units', 'units', 'list'), _spec('get_unit_spec', 'units', 'spec'), _spec('get_unit_requirements', 'units', 'requirements'), _spec('get_upstream_unit_coverage_matrix', 'units', 'coverage-matrix'), _spec('get_unit_config_schema', 'units', 'config-schema', surface='internal'), _spec('preview_unit_build', 'units', 'preview-build', surface='internal'), _spec('list_property_packages', 'packages', 'list'), _spec('get_property_package_specification', 'packages', 'spec'), _spec('list_translators', 'translators', 'list'), _spec('set_feed_state', 'flowsheet', 'set-feed-state'), _spec('set_unit_engineering_inputs', 'flowsheet', 'set-engineering-inputs', surface='internal'), _spec('create_unit', 'flowsheet', 'create-unit', surface='internal'), _spec('create_translator', 'flowsheet', 'create-translator', surface='internal'), _spec('connect_ports', 'flowsheet', 'connect', surface='internal'), _spec('connect_units', 'flowsheet', 'connect-auto', surface='internal'), _spec('get_flowsheet_diagram', 'flowsheet', 'diagram'), _spec('update_unit', 'flowsheet', 'update-unit', surface='internal'), _spec('delete_unit', 'flowsheet', 'delete-unit', surface='internal'), _spec('validate_flowsheet', 'flowsheet', 'validate'), _spec('get_dof_status', 'dof', 'status', surface='internal'), _spec('check_dof', 'dof', 'check', surface='internal'), _spec('get_dof_completion_plan', 'dof', 'completion-plan'), _spec('fix_variable', 'dof', 'fix', surface='internal'), _spec('unfix_variable', 'dof', 'unfix', surface='internal'), _spec('list_unfixed_vars', 'dof', 'unfixed', surface='internal'), _spec('get_scaling_status', 'scaling', 'status', surface='internal'), _spec('set_scaling_factor', 'scaling', 'set', surface='internal'), _spec('apply_scaling', 'scaling', 'apply', surface='internal'), _spec('calculate_scaling_factors', 'scaling', 'calculate', surface='internal'), _spec('report_scaling_issues', 'scaling', 'report', surface='internal'), _spec('autoscale_large_jac', 'scaling', 'autoscale', surface='internal'), _spec('initialize_unit', 'init', 'unit', surface='internal'), _spec('get_initialization_order', 'init', 'order', surface='internal'), _spec('initialize_flowsheet', 'init', 'flowsheet', surface='internal'), _spec('propagate_state', 'init', 'propagate', surface='internal'), _spec('check_solve', 'solve', 'check'), _spec('screen_madm1_fe_s_p_steady_state', 'chemistry', 'screen-madm1-fe-s-p'), _spec('screen_madm1_fe_s_p_serial_steady_state', 'chemistry', 'screen-madm1-fe-s-p-serial'), _spec('screen_madm1_fe_s_p_dose_envelope', 'chemistry', 'screen-madm1-fe-s-p-envelope'), _spec('solve_flowsheet', 'solve', 'flowsheet'), _spec('design_flowsheet', 'solve', 'design-flowsheet'), _spec('apply_design_template', 'templates', 'apply-design'), _spec('solve_designed_graph', 'solve', 'designed-graph'), _spec('solve', 'solve', 'run'), _spec('build_and_solve', 'solve', 'pipeline', surface='internal'), _spec('solve_for', 'solve', 'for'), _spec('list_solve_for_capabilities', 'solve', 'for-capabilities'), _spec('clone_session', 'session', 'clone'), _spec('export_twin_bundle', 'twin', 'export'), _spec('import_twin_bundle', 'twin', 'import'), _spec('evaluate_flowsheet', 'solve', 'evaluate'), _spec('run_global_sensitivity', 'sensitivity', 'run'), _spec('get_sensitivity_study', 'sensitivity', 'status'), _spec('get_solve_status', 'solve', 'status'), _spec('run_diagnostics', 'diagnostics', 'run', surface='internal'), _spec('get_process_diagnostics', 'diagnostics', 'process'), _spec('get_constraint_residuals', 'diagnostics', 'residuals', surface='internal'), _spec('get_bound_violations', 'diagnostics', 'bound-violations', surface='internal'), _spec('diagnose_failure', 'diagnostics', 'diagnose-failure', surface='internal'), _spec('get_last_failure_diagnostics', 'diagnostics', 'last-failure', surface='internal'), _spec('load_zo_parameters', 'zo', 'load-parameters'), _spec('list_zo_databases', 'zo', 'databases'), _spec('list_zo_process_subtypes', 'zo', 'process-subtypes'), _spec('get_zo_unit_parameters', 'zo', 'unit-parameters'), _spec('enable_costing', 'costing', 'enable'), _spec('add_unit_costing', 'costing', 'add-unit'), _spec('disable_unit_costing', 'costing', 'disable-unit'), _spec('set_costing_parameters', 'costing', 'set-parameters'), _spec('list_costed_units', 'costing', 'list'), _spec('compute_costing', 'costing', 'compute'), _spec('get_costing', 'costing', 'get'), _spec('cost_equipment_list', 'costing', 'equipment-list'), _spec('get_results', 'results', 'get'), _spec('get_solved_state', 'results', 'state'), _spec('get_stream_results', 'results', 'streams'), _spec('get_unit_results', 'results', 'units'), _spec('list_templates', 'templates', 'list'), _spec('apply_template', 'templates', 'apply'), _spec('build_from_simulation_seeds', 'simulation-seeds', 'build'), _spec('plan_topology', 'topology', 'plan'), _spec('build_from_topology', 'topology', 'build'), _spec('build_simulation_report_payload', 'artifacts', 'simulation-report'), _spec('get_job_status', 'jobs', 'status'), _spec('get_job_results', 'jobs', 'results'), _spec('get_solve_results', 'solve', 'result'))
ACTION_BY_ID = {action.id: action for action in ACTIONS}
ACTION_BY_CLI_PATH = {action.cli_path: action for action in ACTIONS}
EXTERNAL_ACTION_DENYLIST = frozenset({'persist_simulation_artifacts'})

def list_actions(surface: str='all') -> Sequence[ActionSpec]:
    """Return the shared action catalog."""
    if surface == 'all':
        return ACTIONS
    if surface == 'external':
        return tuple((action for action in ACTIONS if action.surface == 'workflow' and action.id not in EXTERNAL_ACTION_DENYLIST))
    if surface not in {'workflow', 'internal'}:
        raise ValueError(f'Unknown action surface {surface!r}; expected all, workflow, internal, or external')
    return tuple((action for action in ACTIONS if action.surface == surface))
