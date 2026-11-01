"""Shared WaterTAP engine actions used by the MCP and CLI adapters."""
from collections.abc import Mapping, Sequence
from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
from functools import wraps
from html import escape
import importlib.util
from io import StringIO
import json
import inspect
import math
import os
import re
import sys
import threading
import time
import uuid
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic import Field as PydField
from pydantic import model_validator as pyd_model_validator
from watertap_engine_mcp.engineering_utils_bridge import ensure_engineering_utils_importable
ensure_engineering_utils_importable()
from watertap_engine_mcp.core import PropertyPackageType, PROPERTY_PACKAGES, get_property_package_spec, get_session_preset as get_session_preset_from_registry, list_session_presets as list_session_presets_registry, TRANSLATORS, get_translator, check_compatibility, UnitCategory, UNITS, get_unit_spec as get_unit_spec_from_registry, list_units as list_units_registry, SessionConfig, FlowsheetSession
from watertap_engine_mcp.core.roles import RoleResolver, normalize_role
from watertap_engine_mcp.core.session import SessionStatus
from watertap_engine_mcp.core.unit_registry import compile_engineering_inputs, effective_engineering_inputs, effective_required_fixes, normalize_profile_package_config, resolve_engineering_input, resolve_unit_support, supported_profiles as supported_unit_profiles, costing_supported_units as registry_costing_supported_units, unit_input_contract_summary, unit_supports_costing_package
from watertap_engine_mcp.core.downstream_handoff_contract import list_downstream_handoff_profiles as list_downstream_handoff_profiles_registry, validate_downstream_handoff_payload
from watertap_engine_mcp.core.modified_asm2d_kinetics import MODIFIED_ASM2D_KINETICS_KEY, ROUTES_RULING_ID as MODIFIED_ASM2D_ROUTES_RULING_ID, TEMPLATE_KINETICS_RULING_ID, ModifiedAsm2dKineticsRefused, built_reaction_parameters, fix_parameters, fix_reaction_parameters, stated_feed_temperature_c
from watertap_engine_mcp.core.modified_asm2d_reaction_config import REACTION_CONFIG_TARGET, ReactionConfigRefused, apply_reaction_config, reaction_config_target, reaction_package_options, session_reaction_config
from watertap_engine_mcp.utils import JobManager, JobStatus
from watertap_engine_mcp.templates import build_template, list_template_specs, serialize_template_spec
from puran_mcp_support.engineering.models.flowsheet_solve_result import SolveResult
from puran_mcp_support.engineering import run_timing as _run_timing
from puran_mcp_support.engineering.pydantic_base import validate_finite_recursive
from puran_mcp_support.engineering.pydantic_base import StrictFiniteBaseModel as _StrictFiniteBaseModel
from watertap_engine_mcp.diagnostics.dof import active_unfixed_variables, compute_dof_report, compute_live_dof, compute_unit_dof
from watertap_engine_mcp.diagnostics.messages import dof_completion_plan_from_preflight, dof_status_message
from watertap_engine_mcp.diagnostics.readiness import build_readiness_report, evaluate_unit_spec_set
from watertap_engine_mcp.flowsheets.feed_states import build_feed_state_payload
from watertap_engine_mcp.flowsheets.builder import build_model_context
from watertap_engine_mcp.ontology.asm2d import asm2d_to_watertap_tss_equivalent
from watertap_engine_mcp.results.serialization import StreamResultsEnvelope, completed_job_results_envelope
from watertap_engine_mcp.results.solved_state import _extract_process_diagnostics, _extract_solved_state
from watertap_engine_mcp.results.streams import extract_flat_streams, filter_solved_state_streams
from watertap_engine_mcp.security.redaction import redact_public_payload
from watertap_engine_mcp.sessions.lifecycle import SessionLifecycle
from watertap_engine_mcp.sessions.locks import SessionJobLeaseStore
from watertap_engine_mcp.sessions.store import SessionStore
from watertap_engine_mcp.solvers.dispatch import dispatch_solver_for_session
from watertap_engine_mcp.api.schemas import CreateUnitInput, ROSpecSet, SetFeedStateInput, SetScalingFactorInput, SetUnitEngineeringInputsInput
MINIMUM_VIABLE_FLOWSHEET_NOT_MET = 'MINIMUM_VIABLE_FLOWSHEET_NOT_MET'
STORAGE_DIR = Path(os.environ.get('WATERTAP_ENGINE_STORAGE_DIR', Path.home() / '.local' / 'share' / 'watertap-engine-mcp'))
FLOWSHEETS_DIR = STORAGE_DIR / 'flowsheets'

def _positive_int_env(name: str, default: int) -> int:
    """Read a strictly positive integer from the environment."""
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f'{name} must be a positive integer, got {raw!r}') from exc
    if value < 1:
        raise RuntimeError(f'{name} must be a positive integer, got {raw!r}')
    return value
session_manager = SessionStore(FLOWSHEETS_DIR)
job_manager = JobManager(STORAGE_DIR / 'jobs', max_concurrent_jobs=_positive_int_env('WATERTAP_ENGINE_MAX_CONCURRENT_JOBS', 3))
_session_job_lease_store: Optional[SessionJobLeaseStore] = None
_session_job_lease_base: Optional[Path] = None
_solve_submission_lock = threading.Lock()

def _load_session(session_id: str) -> FlowsheetSession:
    """Load a session or raise FileNotFoundError."""
    return session_manager.load(session_id)

def _input_validation_error(model_cls: type[BaseModel], **payload: Any) -> Dict[str, Any] | None:
    """Return a public validation error payload when a finite-input model rejects data."""
    try:
        model_cls.model_validate(payload)
    except ValidationError as exc:
        return redact_public_payload({'error': str(exc), 'code': 'VALIDATION_ERROR'})
    return None

def _finite_validation_error(context: str, payload: Any) -> Dict[str, Any] | None:
    """Return a public validation error when recursive finite validation fails."""
    try:
        validate_finite_recursive(payload)
    except ValueError as exc:
        return redact_public_payload({'error': f'{context} input validation failed: {exc}', 'code': 'VALIDATION_ERROR'})
    return None

def _raise_on_non_finite(context: str, payload: Any) -> None:
    """Raise ValueError with context when recursive finite validation fails."""
    try:
        validate_finite_recursive(payload)
    except ValueError as exc:
        raise ValueError(f'{context} input validation failed: {exc}') from exc

def _contains_finite_numeric_value(value: Any) -> bool:
    """Return True when a nested payload contains at least one finite number."""
    if isinstance(value, bool):
        return False
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, int):
        return True
    if isinstance(value, Decimal):
        return value.is_finite()
    if isinstance(value, BaseModel):
        return _contains_finite_numeric_value(value.model_dump(mode='python'))
    if isinstance(value, Mapping):
        return any((_contains_finite_numeric_value(item) for item in value.values()))
    if isinstance(value, (str, bytes, bytearray)):
        return False
    if isinstance(value, Sequence):
        return any((_contains_finite_numeric_value(item) for item in value))
    return False

def _lease_store_base() -> Path:
    """Resolve lease storage from the currently injected job manager."""
    jobs_dir = Path(getattr(job_manager, 'jobs_dir', STORAGE_DIR / 'jobs'))
    return jobs_dir.parent / 'session_locks'

def _session_job_leases() -> SessionJobLeaseStore:
    """Return a lease store aligned with the current job manager."""
    global _session_job_lease_store, _session_job_lease_base
    base = _lease_store_base()
    if _session_job_lease_store is None or _session_job_lease_base != base:
        _session_job_lease_store = SessionJobLeaseStore(base)
        _session_job_lease_base = base
    return _session_job_lease_store

def _job_status_value(job_id: str) -> Optional[str]:
    """Return a persisted job status value, if available."""
    job = job_manager.get_status(job_id)
    return job.status.value if job else None

def _guard_solver_mutation_allowed(session: FlowsheetSession, tool_name: str) -> Optional[Dict[str, Any]]:
    """Reject manual solver mutations once the server-owned pipeline has started."""
    status = getattr(session.status, 'value', str(session.status))
    if status in {'solving', 'solved'}:
        return {'session_id': session.config.session_id, 'status': 'blocked', 'error': f"{tool_name} is only allowed before the server-owned solve pipeline starts. Current session status is '{status}'. Create a new session or rebuild from topology instead of mutating initialization/scaling state mid-pipeline."}
    if isinstance(session.results, dict) and session.results.get('history'):
        return {'session_id': session.config.session_id, 'status': 'blocked', 'error': f'{tool_name} is blocked because this session already has pipeline history. Create a new session or rebuild from topology for another solve attempt.'}
    return None

def _normalize_property_package(property_package: str) -> PropertyPackageType:
    """Normalize a package name to the enum value."""
    try:
        return PropertyPackageType[property_package.upper()]
    except KeyError as exc:
        valid = ', '.join((p.name for p in PropertyPackageType))
        raise ValueError(f"Invalid property_package '{property_package}'. Valid: {valid}") from exc

def _unit_package_type(session: FlowsheetSession, unit_id: str) -> PropertyPackageType:
    """Resolve the primary property package configured for a unit instance."""
    unit_inst = session.units.get(unit_id)
    if unit_inst is None:
        return session.config.default_property_package
    config = unit_inst.config or {}
    if unit_inst.unit_type == 'Evaporator':
        package_name = config.get('property_package_feed') or config.get('property_package')
        if package_name:
            return _normalize_property_package(package_name)
        return session.config.default_property_package
    if unit_inst.unit_type == 'SurrogateCrystallizer':
        package_name = config.get('property_package')
        if package_name:
            return _normalize_property_package(package_name)
        return session.config.default_property_package
    package_name = config.get('property_package')
    if package_name:
        return _normalize_property_package(package_name)
    return session.config.default_property_package

def _coerce_package_ref(package_ref: Any, fallback: PropertyPackageType) -> PropertyPackageType:
    """Normalize an optional package reference to a PropertyPackageType."""
    if package_ref is None:
        return fallback
    if isinstance(package_ref, PropertyPackageType):
        return package_ref
    if isinstance(package_ref, str):
        return _normalize_property_package(package_ref)
    raise ValueError(f'Unsupported property package reference: {package_ref}')

def _unit_port_package_type(session: FlowsheetSession, unit_id: str, port_name: str) -> PropertyPackageType:
    """Resolve the actual property package used by one unit port."""
    unit_inst = session.units.get(unit_id)
    if unit_inst is None:
        return session.config.default_property_package
    config = unit_inst.config or {}
    default_pkg = session.config.default_property_package
    if unit_inst.unit_type == 'Evaporator':
        feed_pkg = _coerce_package_ref(config.get('property_package_feed') or config.get('property_package'), default_pkg)
        vapor_pkg = _coerce_package_ref(config.get('property_package_vapor'), PropertyPackageType.WATER)
        if port_name == 'outlet_vapor':
            return vapor_pkg
        return feed_pkg
    if unit_inst.unit_type == 'SteamHeater0D':
        hot_pkg = _coerce_package_ref((config.get('hot') or {}).get('property_package'), default_pkg)
        cold_pkg = _coerce_package_ref((config.get('cold') or {}).get('property_package'), default_pkg)
        if port_name.startswith('hot_side_'):
            return hot_pkg
        if port_name.startswith('cold_side_'):
            return cold_pkg
    if unit_inst.unit_type in {'MembraneDistillation0D', 'MembraneDistillation1D'}:
        hot_pkg = _coerce_package_ref((config.get('hot_ch') or {}).get('property_package'), default_pkg)
        cold_pkg = _coerce_package_ref((config.get('cold_ch') or {}).get('property_package'), default_pkg)
        if port_name.startswith('hot_'):
            return hot_pkg
        if port_name.startswith('cold_'):
            return cold_pkg
    if unit_inst.unit_type == 'SurrogateCrystallizer':
        vapor_pkg = _coerce_package_ref(config.get('vapor_property_package'), PropertyPackageType.WATER)
        if 'vapor' in port_name.lower():
            return vapor_pkg
    if unit_inst.unit_type == 'AD':
        if port_name == 'vapor_outlet':
            return _coerce_package_ref(config.get('vapor_property_package'), PropertyPackageType.ADM1_VAPOR)
        return _coerce_package_ref(config.get('liquid_property_package') or config.get('property_package'), default_pkg)
    return _unit_package_type(session, unit_id)

def _normalize_supported_unit(session: FlowsheetSession, unit_type: str, config: Optional[Dict[str, Any]]=None) -> tuple[Dict[str, Any], Optional[Any]]:
    """Apply support-profile defaults and validate session package requirements."""
    spec = get_unit_spec_from_registry(unit_type)
    normalized_config, _, profile = resolve_unit_support(spec, session.config.default_property_package, config, session.config.property_package_config)
    if profile is not None:
        normalize_profile_package_config(profile, session.config.property_package_config)
    return (normalized_config, profile)

def _active_costing_package(session: FlowsheetSession) -> Optional[str]:
    """Return the configured costing package for a session, if any."""
    if not session.costing_config or not session.costing_config.get('enabled'):
        return None
    return session.costing_config.get('package', 'watertap')

def _configured_product_stream(session: FlowsheetSession) -> tuple[Optional[str], str]:
    """Return the configured default product stream for costing, if any."""
    if not session.costing_config or not session.costing_config.get('enabled'):
        return (None, 'permeate')
    return (session.costing_config.get('product_stream_unit'), session.costing_config.get('product_stream_port', 'permeate'))

def _unit_certification(spec: Any, costing_package: Optional[str]=None) -> Dict[str, Any]:
    """Serialize the solve/cost certification contract for a unit spec."""
    return {'solve_supported': bool(getattr(spec, 'solve_supported', True)), 'calculation_class': str(getattr(spec, 'calculation_class', 'mechanistic')), 'recommendation_eligible': bool(getattr(spec, 'recommendation_eligible', True)), 'costing_supported': unit_supports_costing_package(spec, costing_package), 'costing_supported_packages': list(spec.costing.supported_packages), 'costing_source': spec.costing.source.value}

def _flowsheet_certification(session: FlowsheetSession, *, builder: Optional[Any]=None) -> Dict[str, Any]:
    """Build a flowsheet-level certification report for solve and costing."""
    costing_package = _active_costing_package(session)
    units = []
    cost_supported_units = []
    non_cost_supported_units = []
    structural_non_cost_supported_units = []
    blocking_non_cost_supported_units = []
    recommendation_blocking_units = []
    requested_costing_units = []
    valid_requested_costing_units = []
    invalid_requested_costing_units = []
    for unit_id, unit_inst in session.units.items():
        spec = get_unit_spec_from_registry(unit_inst.unit_type)
        certification = _unit_certification(spec, costing_package)
        unit_payload = {'unit_id': unit_id, 'unit_type': unit_inst.unit_type, 'costing_enabled': bool(unit_inst.costing_enabled), **certification}
        units.append(unit_payload)
        if not certification['recommendation_eligible']:
            recommendation_blocking_units.append(unit_id)
        if certification['costing_supported']:
            cost_supported_units.append(unit_id)
        else:
            non_cost_supported_units.append(unit_id)
            if unit_inst.unit_type in {'Feed', 'FeedZO', 'Product'}:
                structural_non_cost_supported_units.append(unit_id)
            else:
                blocking_non_cost_supported_units.append(unit_id)
        if unit_inst.costing_enabled:
            requested_costing_units.append(unit_id)
            if certification['costing_supported']:
                valid_requested_costing_units.append(unit_id)
            else:
                invalid_requested_costing_units.append(unit_id)
    certification: Dict[str, Any] = {'solve_certified': all((unit['solve_supported'] for unit in units)), 'recommendation_eligible': not recommendation_blocking_units, 'recommendation_blocking_units': sorted(recommendation_blocking_units), 'calculation_classes': sorted({str(unit['calculation_class']) for unit in units}), 'cost_eligible': len(blocking_non_cost_supported_units) == 0, 'cost_certified': False, 'costing_package': costing_package, 'flowsheet_costing_enabled': bool(session.costing_config and session.costing_config.get('enabled')), 'requested_costing_units': requested_costing_units, 'valid_requested_costing_units': valid_requested_costing_units, 'invalid_requested_costing_units': invalid_requested_costing_units, 'cost_supported_units': sorted(cost_supported_units), 'non_cost_supported_units': sorted(non_cost_supported_units), 'structural_non_cost_supported_units': sorted(structural_non_cost_supported_units), 'blocking_non_cost_supported_units': sorted(blocking_non_cost_supported_units), 'costing_blocked_by_non_costable_units': bool(blocking_non_cost_supported_units), 'costing_blocked_by_missing_product_stream': False, 'product_stream': None, 'units': units, 'registry_cost_supported_units': registry_costing_supported_units(costing_package)}
    if builder is not None and certification['flowsheet_costing_enabled']:
        configured_unit, configured_port = _configured_product_stream(session)
        try:
            product_stream = builder.ensure_lcow(product_stream_unit=configured_unit, product_stream_port=configured_port)
        except Exception as exc:
            product_stream = {'resolved': False, 'reason': str(exc)}
        certification['product_stream'] = product_stream
        certification['costing_blocked_by_missing_product_stream'] = not bool(product_stream.get('resolved'))
    certification['cost_certified'] = bool(certification['flowsheet_costing_enabled'] and certification['cost_eligible'] and (not certification['invalid_requested_costing_units']) and certification['requested_costing_units'] and (not certification['costing_blocked_by_missing_product_stream']))
    return certification

def _preflight_error_payload(session_id: str, *, issues: List[str], warnings: Optional[List[str]]=None, certification: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
    """Return a structured preflight/build error payload."""
    return {'session_id': session_id, 'build_success': False, 'ready_to_solve': False, 'status': 'error', 'issues': issues, 'issue_codes': [], 'warnings': warnings or [], 'unit_dof': {}, 'actual_unfixed_variables': {}, 'required_fixes': {}, 'unresolved_required_fixes': {}, 'unresolved_scaling_paths': {}, 'unit_ports': {}, 'feed_units_without_state': [], 'costing': None, 'certification': certification}

def _feed_state_has_fixed_process_spec(feed_state: Any) -> bool:
    """Return True when a persisted feed state fixes a process state variable."""
    state_args = feed_state.get('state_args') if isinstance(feed_state, dict) else None
    if not isinstance(state_args, dict):
        return False
    process_state_keys = {'flow_vol', 'flow_mass_phase_comp', 'flow_mol_phase_comp', 'flow_mass_comp', 'flow_mol_comp', 'conc_mass_comp'}
    for key in process_state_keys:
        if key not in state_args:
            continue
        try:
            validate_finite_recursive(state_args[key])
        except ValueError:
            continue
        if _contains_finite_numeric_value(state_args[key]):
            return True
    return False

def _minimum_viable_flowsheet_payload(session: FlowsheetSession) -> Dict[str, Any]:
    """Return minimum solve-admissibility checks independent of DOF closure."""
    feed_state_units = [unit_id for unit_id in session.feed_states if unit_id in session.units and _feed_state_has_fixed_process_spec(session.feed_states[unit_id])]
    issues: list[str] = []
    if not session.units:
        issues.append('Minimum viable flowsheet requires at least one unit')
    if not feed_state_units:
        issues.append('Minimum viable flowsheet requires at least one feed stream with a fixed feed-state spec')
    return {'met': not issues, 'has_units': bool(session.units), 'feed_state_units': feed_state_units, 'issues': issues, 'code': MINIMUM_VIABLE_FLOWSHEET_NOT_MET if issues else None}

def _build_unit_config_schema(unit_type: str) -> Dict[str, Any]:
    """Inspect the live unit CONFIG block and serialize its public fields."""
    spec = get_unit_spec_from_registry(unit_type)
    schema: Dict[str, Any] = {}
    try:
        import importlib
        module = importlib.import_module(spec.module_path)
        unit_class = getattr(module, spec.class_name)
        config_block = getattr(unit_class, 'CONFIG', None)
        if config_block is None:
            data_class = getattr(unit_class, '_ComponentDataClass', None)
            config_block = getattr(data_class, 'CONFIG', None)
        if config_block is None:
            return {'fields': schema, 'source': 'unit_class_missing_CONFIG'}
        for name in config_block:
            entry = config_block.get(name)
            if entry is None:
                continue
            field_info = {'default': getattr(entry, '_default', None), 'description': getattr(entry, '_description', None)}
            domain = getattr(entry, '_domain', None)
            if domain is not None:
                field_info['domain'] = getattr(domain, '__name__', str(domain))
                enum_type = None
                if inspect.isclass(domain) and issubclass(domain, Enum):
                    enum_type = domain
                elif hasattr(domain, '_domain') and inspect.isclass(domain._domain) and issubclass(domain._domain, Enum):
                    enum_type = domain._domain
                if enum_type is not None:
                    field_info['choices'] = [{'name': member.name, 'value': member.value} for member in enum_type]
            schema[name] = field_info
        return {'fields': schema, 'source': 'unit_class_CONFIG'}
    except Exception as exc:
        return {'fields': schema, 'source': 'registry_fallback', 'warning': f'Could not inspect live CONFIG: {type(exc).__name__}: {exc}'}

def _product_port_matches(session: FlowsheetSession, unit_id: str, requested_port: str, actual_port: Optional[str]) -> bool:
    """Return True when a requested product port is equivalent to the persisted port."""
    if actual_port is None:
        return False
    if requested_port == actual_port:
        return True
    unit_inst = session.units.get(unit_id)
    if unit_inst is not None and unit_inst.unit_type == 'Product':
        sink_aliases = {'inlet', 'outlet', 'product', 'treated', 'permeate'}
        if requested_port in sink_aliases and actual_port in sink_aliases:
            return True
    return False

def _persisted_costing_payload(session: FlowsheetSession, product_stream_unit: Optional[str]=None, product_stream_port: str='permeate') -> Optional[Dict[str, Any]]:
    """Return solved costing persisted on the session, if available."""
    if not isinstance(session.results, dict):
        return None
    costing = session.results.get('costing')
    if not isinstance(costing, dict):
        return None
    payload = deepcopy(costing)
    payload['session_id'] = session.config.session_id
    payload['source'] = 'solved_session'
    certification = _flowsheet_certification(session)
    certification['product_stream'] = payload.get('product_stream')
    certification['costing_blocked_by_missing_product_stream'] = not bool((payload.get('product_stream') or {}).get('resolved'))
    certification['cost_certified'] = payload.get('status') == 'complete'
    payload['certification'] = certification
    if product_stream_unit is not None:
        product_stream = payload.get('product_stream') or {}
        unit_matches = product_stream.get('unit_id') == product_stream_unit
        port_matches = _product_port_matches(session, product_stream_unit, product_stream_port, product_stream.get('port_name'))
        if not (unit_matches and port_matches):
            requested_stream = f'{product_stream_unit}.{product_stream_port}'
            solved_stream = product_stream.get('canonical_stream') or 'unknown'
            return {'session_id': session.config.session_id, 'source': 'solved_session', 'status': 'error', 'error': f"Requested product stream '{requested_stream}' does not match the solved costing stream '{solved_stream}'. Re-run solve with the requested product stream.", 'product_stream': product_stream, 'certification': _flowsheet_certification(session), 'warnings': list(payload.get('warnings', [])), 'supported_units': payload.get('supported_units', []), 'skipped_units': payload.get('skipped_units', [])}
    return payload

def _persisted_solved_state_payload(session: FlowsheetSession, *, unit_id: Optional[str]=None) -> Optional[Dict[str, Any]]:
    """Return persisted solved-state export, optionally filtered to one unit."""
    if not isinstance(session.results, dict):
        return None
    solved_state = session.results.get('solved_state')
    if not isinstance(solved_state, dict):
        return None
    payload = deepcopy(solved_state)
    payload['session_id'] = session.config.session_id
    payload['source'] = 'solved'
    if unit_id is None:
        return payload
    unit_payload = payload.get('units', {}).get(unit_id)
    if unit_payload is None:
        return {'session_id': session.config.session_id, 'source': 'solved', 'error': f"Unit '{unit_id}' not found in solved_state export"}
    filtered_streams = {stream_key: stream_payload for stream_key, stream_payload in payload.get('streams', {}).items() if stream_key.startswith(f'{unit_id}.')}
    filtered_stream_roles = {stream_key: role for stream_key, role in payload.get('stream_roles', {}).items() if stream_key.startswith(f'{unit_id}.')}
    return {'session_id': session.config.session_id, 'source': 'solved', 'unit_id': unit_id, 'unit': unit_payload, 'streams': filtered_streams, 'stream_roles': filtered_stream_roles}

def _solved_stream_payload(session: FlowsheetSession, streams: Optional[List[str]]=None) -> Optional[Dict[str, Any]]:
    """Return solved stream export when available."""
    solved_state = _persisted_solved_state_payload(session)
    if solved_state is None:
        return None
    stream_payload, stream_roles = filter_solved_state_streams(solved_state, stream_filter=streams)
    return StreamResultsEnvelope(session_id=session.config.session_id, source='solved', streams=stream_payload, stream_roles=stream_roles).model_dump(exclude_none=True)

def _solved_unit_results_payload(session: FlowsheetSession, unit_id: str) -> Optional[Dict[str, Any]]:
    """Return solved unit export and KPI summaries when available."""
    solved_state = _persisted_solved_state_payload(session, unit_id=unit_id)
    if solved_state is None or solved_state.get('error'):
        return solved_state
    unit_inst = session.units[unit_id]
    results = {'session_id': session.config.session_id, 'unit_id': unit_id, 'unit_type': unit_inst.unit_type, 'source': 'solved', 'variables': solved_state['unit'].get('variables', {}), 'streams': solved_state['unit'].get('streams', {}), 'state_blocks': solved_state['unit'].get('state_blocks', {}), 'port_roles': solved_state['unit'].get('port_roles', {})}
    if isinstance(session.results, dict):
        kpis = session.results.get('kpis', {})
        results['performance'] = kpis.get('units', {}).get(unit_id, {})
    return results

def _sync_costing_payload_certification(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Align costing certification with the returned costing payload."""
    certification = payload.get('certification')
    if not isinstance(certification, dict):
        return payload
    product_stream = payload.get('product_stream')
    if isinstance(product_stream, dict):
        certification['product_stream'] = product_stream
        certification['costing_blocked_by_missing_product_stream'] = not bool(product_stream.get('resolved'))
    certification['cost_certified'] = payload.get('status') == 'complete'
    return payload

def _extract_costing_from_rebuilt_model(session: FlowsheetSession, product_stream_unit: Optional[str]=None, product_stream_port: str='permeate') -> Dict[str, Any]:
    """Build the current model state and extract costing from it."""
    from watertap_engine_mcp.utils.model_builder import ModelBuilder
    builder = ModelBuilder(session)
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        builder.build()
    results = builder.extract_costing_results(product_stream_unit=product_stream_unit, product_stream_port=product_stream_port, source='rebuilt_model')
    results['certification'] = _flowsheet_certification(session, builder=builder)
    return _sync_costing_payload_certification(results)

def _component_path_status(builder: Any, unit_block: Any, path: str, satisfaction_mode: str='all') -> Dict[str, Any]:
    """Resolve a registry path against a live unit block."""

    def _required_fixed_matches(match_count: int) -> int:
        if satisfaction_mode == 'all':
            return match_count
        if satisfaction_mode == 'all_but_one':
            return max(1, match_count - 1)
        raise ValueError(f'Unsupported satisfaction_mode: {satisfaction_mode}')
    target, index = builder._resolve_variable_path(unit_block, path)
    if target is None:
        return {'path': path, 'resolved': False, 'satisfaction_mode': satisfaction_mode, 'match_count': 0, 'fixed_match_count': 0, 'required_fixed_match_count': 0, 'fixed': False}
    resolved_to = None
    match_count = 0
    fixed_match_count = 0
    fully_fixed = False
    if isinstance(target, list):
        resolved_to = []
        for var, index_ in target:
            if index_ in var:
                component = var[index_]
                resolved_to.append(component.name)
                match_count += 1
                if getattr(component, 'fixed', False):
                    fixed_match_count += 1
        fully_fixed = match_count > 0 and fixed_match_count == match_count
    elif index is None and hasattr(target, 'is_indexed') and target.is_indexed():
        resolved_to = [target[idx].name for idx in target]
        match_count = len(resolved_to)
        fixed_match_count = sum((1 for idx in target if getattr(target[idx], 'fixed', False)))
        fully_fixed = match_count > 0 and fixed_match_count == match_count
    else:
        component = target if index is None else target[index]
        resolved_to = component.name
        match_count = 1
        fixed_match_count = 1 if getattr(component, 'fixed', False) else 0
        fully_fixed = fixed_match_count == 1
    required_fixed_match_count = _required_fixed_matches(match_count)
    fixed = fixed_match_count >= required_fixed_match_count
    return {'path': path, 'resolved': True, 'fixed': fixed, 'fully_fixed': fully_fixed, 'resolved_to': resolved_to, 'satisfaction_mode': satisfaction_mode, 'match_count': match_count, 'fixed_match_count': fixed_match_count, 'required_fixed_match_count': required_fixed_match_count}

def _unit_port_names(unit_block: Any) -> Dict[str, List[str]]:
    """Collect live port names for a unit block."""
    ports = {'inlets': [], 'outlets': []}
    for name in dir(unit_block):
        if name.startswith('_'):
            continue
        candidate = getattr(unit_block, name, None)
        if candidate is None:
            continue
        if candidate.__class__.__name__ != 'ScalarPort':
            continue
        if 'inlet' in name or name == 'feed':
            ports['inlets'].append(name)
        elif 'outlet' in name or name in {'permeate', 'retentate', 'treated', 'byproduct', 'brine'}:
            ports['outlets'].append(name)
        else:
            ports['outlets'].append(name)
    ports['inlets'].sort()
    ports['outlets'].sort()
    return ports

def _live_unfixed_variables(unit_block: Any) -> List[str]:
    """Return unfixed variables participating in active constraints."""
    return active_unfixed_variables(unit_block)

def _relative_variable_path(unit_block: Any, variable_name: str) -> str:
    """Return the variable path relative to a unit block, when possible."""
    prefix = f'{getattr(unit_block, 'name', '')}.'
    if prefix != '.' and variable_name.startswith(prefix):
        return variable_name[len(prefix):]
    return variable_name

def _live_requirement_key(relative_path: str) -> str:
    """Create a stable JSON-safe key for live DOF guidance."""
    key = relative_path
    key = re.sub('\\[[^\\]]+\\]', '', key)
    key = key.replace('.', '_')
    key = re.sub('[^0-9A-Za-z_]+', '_', key)
    key = re.sub('_+', '_', key).strip('_')
    return key or 'variable'

def _live_requirement_kind(relative_path: str) -> str:
    """Classify an unfixed live variable into a process-facing guidance bucket."""
    path = relative_path.lower()
    if any((token in path for token in ('properties_in', '.inlet', '_inlet'))):
        return 'inlet_state'
    if any((token in path for token in ('properties_out', '.outlet', '_outlet', 'treated', 'byproduct'))):
        return 'outlet_state'
    if any((token in path for token in ('area', 'volume', 'length', 'width', 'height', 'diameter', 'depth', 'cell_pair'))):
        return 'design_size'
    if any((token in path for token in ('pressure', 'temperature', 'flow', 'current', 'voltage', 'dose', 'dosage'))):
        return 'operating_condition'
    if any((token in path for token in ('split_fraction', 'removal', 'recovery', 'conversion', 'efficiency'))):
        return 'performance_assumption'
    if any((token in path for token in ('a_comp', 'b_comp', 'permeability', 'conductivity', 'porosity', 'capacity'))):
        return 'material_or_equipment_property'
    return 'model_parameter'

def _live_dof_requirements(unit_block: Any, *, max_items: int=200) -> List[Dict[str, Any]]:
    """Return public DOF closure guidance from live unfixed model variables."""
    requirements = []
    for variable_name in _live_unfixed_variables(unit_block)[:max_items]:
        relative_path = _relative_variable_path(unit_block, variable_name)
        requirements.append({'key': _live_requirement_key(relative_path), 'target_variable': relative_path, 'resolved_to': variable_name, 'kind': _live_requirement_kind(relative_path), 'set_via': 'fixed_vars', 'topology_payload_hint': {'units[].fixed_vars': {relative_path: '<value>'}}})
    return requirements

def _effective_required_fixes(unit_inst: Any, spec: Any, default_property_package: PropertyPackageType, session: Optional[FlowsheetSession]=None, unit_id: Optional[str]=None) -> List[Any]:
    """Filter registry required-fix metadata based on the matched profile."""
    fixes = effective_required_fixes(spec, default_property_package, unit_inst.config or {})
    if session is None or unit_id is None:
        return fixes

    def _downstream(start_unit: str, source_port: Optional[str]=None) -> List[str]:
        return [conn.dest_unit for conn in session.connections if conn.source_unit == start_unit and (source_port is None or conn.source_port == source_port)]

    def _upstream(dest_unit: str) -> List[str]:
        return [conn.source_unit for conn in session.connections if conn.dest_unit == dest_unit]
    explicit = getattr(session, 'thermal_couplings', {})
    if unit_inst.unit_type == 'Evaporator':
        is_coupled = False
        if explicit:
            is_coupled = unit_id in explicit
        else:
            downstream_units = _downstream(unit_id, 'outlet_vapor')
            for candidate_id in downstream_units:
                candidate = session.units.get(candidate_id)
                if candidate is None:
                    continue
                if candidate.unit_type == 'Condenser':
                    is_coupled = True
                    break
                if candidate.unit_type == 'Compressor':
                    next_units = _downstream(candidate_id)
                    if any((session.units.get(next_id) and session.units[next_id].unit_type == 'Condenser' for next_id in next_units)):
                        is_coupled = True
                        break
        if is_coupled:
            return [fix for fix in fixes if fix.name not in {'delta_temperature_in', 'delta_temperature_out'}]
    if unit_inst.unit_type == 'Condenser':
        is_coupled = False
        if explicit:
            is_coupled = unit_id in explicit.values()
        else:
            upstream_units = _upstream(unit_id)
            for candidate_id in upstream_units:
                candidate = session.units.get(candidate_id)
                if candidate is None:
                    continue
                if candidate.unit_type == 'Evaporator':
                    is_coupled = True
                    break
                if candidate.unit_type == 'Compressor':
                    prior_units = _upstream(candidate_id)
                    if any((session.units.get(prev_id) and session.units[prev_id].unit_type == 'Evaporator' for prev_id in prior_units)):
                        is_coupled = True
                        break
        if is_coupled:
            return [fix for fix in fixes if fix.name != 'control_volume.properties_out[0].temperature']
    return fixes
_SPECIAL_REQUIRED_FIX_UNIT_LITERALS = frozenset({'-', '%', 'NTU', 'kg additive/kg feed'})

def _required_fix_unit_lookup() -> Dict[str, tuple[Any, Any]]:
    """Return supported unit conversions for registry required-fix values."""
    from pyomo.environ import units as pyunits
    return {'1/h': (pyunits.hr ** (-1), pyunits.hr ** (-1)), '1/s': (pyunits.s ** (-1), pyunits.s ** (-1)), 'K': (pyunits.K, pyunits.K), 'Pa': (pyunits.Pa, pyunits.Pa), 'V': (pyunits.V, pyunits.V), 'W': (pyunits.W, pyunits.W), 'dimensionless': (pyunits.dimensionless, pyunits.dimensionless), 'cm': (pyunits.cm, pyunits.m), 'cm^2': (pyunits.cm ** 2, pyunits.m ** 2), 'kmol/m^3': (pyunits.kmol / pyunits.m ** 3, pyunits.kmol / pyunits.m ** 3), 'A': (pyunits.ampere, pyunits.ampere), 'A/m^2': (pyunits.ampere / pyunits.m ** 2, pyunits.ampere / pyunits.m ** 2), 'W/m/K': (pyunits.W / pyunits.m / pyunits.K, pyunits.W / pyunits.m / pyunits.K), 'W/m^2/K': (pyunits.W / pyunits.m ** 2 / pyunits.K, pyunits.W / pyunits.m ** 2 / pyunits.K), 'cm^2/J': (pyunits.cm ** 2 / pyunits.J, pyunits.m ** 2 / pyunits.J), 'eq/L': (pyunits.mol / pyunits.L, pyunits.mol / pyunits.L), 'kWh/kg': (pyunits.kWh / pyunits.kg, pyunits.J / pyunits.kg), 'kWh/m^3': (pyunits.kWh / pyunits.m ** 3, pyunits.J / pyunits.m ** 3), 'kg/m^3': (pyunits.kg / pyunits.m ** 3, pyunits.kg / pyunits.m ** 3), 'kg/s': (pyunits.kg / pyunits.s, pyunits.kg / pyunits.s), 'm': (pyunits.m, pyunits.m), 'm/s': (pyunits.m / pyunits.s, pyunits.m / pyunits.s), 'm/s/Pa': (pyunits.m / pyunits.s / pyunits.Pa, pyunits.m / pyunits.s / pyunits.Pa), 'mW/cm^2': (pyunits.mW / pyunits.cm ** 2, pyunits.W / pyunits.m ** 2), 'm^2': (pyunits.m ** 2, pyunits.m ** 2), 'm^2/s': (pyunits.m ** 2 / pyunits.s, pyunits.m ** 2 / pyunits.s), 'm^3': (pyunits.m ** 3, pyunits.m ** 3), 'mg/L': (pyunits.mg / pyunits.L, pyunits.kg / pyunits.m ** 3), 'mg/L/NTU': (pyunits.mg / pyunits.L, pyunits.kg / pyunits.m ** 3), 'mol/m^3': (pyunits.mol / pyunits.m ** 3, pyunits.mol / pyunits.m ** 3), 'mol/s': (pyunits.mol / pyunits.s, pyunits.mol / pyunits.s), 'ohm*m^2': (pyunits.ohm * pyunits.m ** 2, pyunits.ohm * pyunits.m ** 2), 's': (pyunits.s, pyunits.s), 'min': (pyunits.minute, pyunits.minute), 'g/L': (pyunits.g / pyunits.L, pyunits.g / pyunits.L), 'kg/kg': (pyunits.kg / pyunits.kg, pyunits.kg / pyunits.kg)}

def _converter_supported_required_fix_unit_literals() -> set[str]:
    """Return the unit literals the converter itself can handle.

    This is the cheap set the per-conversion path validates against — it does
    NOT walk the unit registry. Keeping the registry-wide completeness audit
    out of the hot path avoids the footgun where one newly-advertised but
    unsupported literal on an unrelated unit blocks fix_variable for ALL units.
    """
    return set(_required_fix_unit_lookup()) | set(_SPECIAL_REQUIRED_FIX_UNIT_LITERALS)

def supported_required_fix_unit_literals() -> set[str]:
    """Return registry-advertised unit literals, asserting converter coverage.

    Registry-wide completeness invariant — intended for startup/CI (exercised by
    the required-fix converter coverage test), NOT the per-conversion hot path.
    Raises if the registry advertises a literal the converter cannot handle.
    """
    advertised = _registry_advertised_required_fix_unit_literals()
    converter_units = _converter_supported_required_fix_unit_literals()
    missing = advertised - converter_units
    if missing:
        raise RuntimeError('Required-fix converter missing advertised unit literals: ' + ', '.join(sorted(missing)))
    return advertised

def _registry_advertised_required_fix_unit_literals() -> set[str]:
    """Return unit literals advertised by required-fix and engineering-input specs."""
    advertised: set[str] = set()
    for unit_spec in UNITS.values():
        advertised.update((item.units for item in unit_spec.required_fixes))
        advertised.update((item.units for item in unit_spec.engineering_inputs))
        for profile in unit_spec.profiles:
            advertised.update((item.units for item in profile.required_fixes or []))
            advertised.update((item.units for item in profile.engineering_inputs))
        packages = unit_spec.compatible_property_packages or [PropertyPackageType.WATER]
        for package in packages:
            advertised.update((item.units for item in effective_engineering_inputs(unit_spec, package, {})))
            advertised.update((item.units for item in effective_required_fixes(unit_spec, package, {})))
    return {unit for unit in advertised if unit is not None}

def _percent_literal_to_canonical(raw_value: float, target_variable: Optional[str]) -> float:
    target = (target_variable or '').lower()
    if 'percent' in target:
        return raw_value
    return raw_value / 100.0

def _convert_required_fix_input_value(raw_value: float, units_literal: str, target_variable: Optional[str]=None) -> float:
    """Convert a user-facing required-fix value into the model's canonical units."""
    normalized_units = '' if units_literal is None else units_literal
    if normalized_units not in _converter_supported_required_fix_unit_literals():
        raise ValueError(f'Unsupported required-fix units literal: {normalized_units}')
    if normalized_units == '-':
        return raw_value
    if normalized_units == '%':
        return _percent_literal_to_canonical(raw_value, target_variable)
    if normalized_units in {'NTU', 'kg additive/kg feed'}:
        return raw_value
    from pyomo.environ import units as pyunits
    from_units, to_units = _required_fix_unit_lookup()[normalized_units]
    return pyunits.convert_value(raw_value, from_units=from_units, to_units=to_units)
_RO_SPEC_ADAPTER = TypeAdapter(ROSpecSet)

def _validate_ro_recovery_input(raw_value: float) -> float:
    """Validate RO recovery through the public discriminated spec-set contract."""
    try:
        parsed = _RO_SPEC_ADAPTER.validate_python({'mode': 'recovery_fixed', 'recovery': raw_value})
    except ValidationError as exc:
        raise ValueError('ReverseOsmosis0D recovery must be greater than 0 and less than 1') from exc
    return float(parsed.recovery)

def _engineering_input_payload(input_spec: Any) -> Dict[str, Any]:
    """Serialize an EngineeringInputSpec for public responses."""
    return {'key': input_spec.key, 'target_variable': input_spec.target_variable, 'description': input_spec.description, 'units': input_spec.units, 'kind': input_spec.kind, 'typical_min': input_spec.typical_min, 'typical_max': input_spec.typical_max, 'typical_default': input_spec.typical_default, 'indexed': input_spec.indexed, 'satisfaction_mode': input_spec.satisfaction_mode}

def _effective_engineering_input_specs(unit_inst: Any, spec: Any, default_property_package: PropertyPackageType, session: Optional[FlowsheetSession]=None, unit_id: Optional[str]=None) -> List[Any]:
    """Resolve the public engineering-input contract for one unit instance."""
    engineering_inputs = effective_engineering_inputs(spec, default_property_package, unit_inst.config or {})
    if session is None or unit_id is None:
        return engineering_inputs

    def _downstream(start_unit: str, source_port: Optional[str]=None) -> List[str]:
        return [conn.dest_unit for conn in session.connections if conn.source_unit == start_unit and (source_port is None or conn.source_port == source_port)]

    def _upstream(dest_unit: str) -> List[str]:
        return [conn.source_unit for conn in session.connections if conn.dest_unit == dest_unit]
    explicit = getattr(session, 'thermal_couplings', {})
    if unit_inst.unit_type == 'Evaporator':
        is_coupled = False
        if explicit:
            is_coupled = unit_id in explicit
        else:
            downstream_units = _downstream(unit_id, 'outlet_vapor')
            for candidate_id in downstream_units:
                candidate = session.units.get(candidate_id)
                if candidate is None:
                    continue
                if candidate.unit_type == 'Condenser':
                    is_coupled = True
                    break
                if candidate.unit_type == 'Compressor':
                    next_units = _downstream(candidate_id)
                    if any((session.units.get(next_id) and session.units[next_id].unit_type == 'Condenser' for next_id in next_units)):
                        is_coupled = True
                        break
        if is_coupled:
            engineering_inputs = [input_spec for input_spec in engineering_inputs if input_spec.target_variable not in {'delta_temperature_in', 'delta_temperature_out'}]
    if unit_inst.unit_type == 'Condenser':
        is_coupled = False
        if explicit:
            is_coupled = unit_id in explicit.values()
        else:
            upstream_units = _upstream(unit_id)
            for candidate_id in upstream_units:
                candidate = session.units.get(candidate_id)
                if candidate is None:
                    continue
                if candidate.unit_type == 'Evaporator':
                    is_coupled = True
                    break
                if candidate.unit_type == 'Compressor':
                    prior_units = _upstream(candidate_id)
                    if any((session.units.get(prev_id) and session.units[prev_id].unit_type == 'Evaporator' for prev_id in prior_units)):
                        is_coupled = True
                        break
        if is_coupled:
            engineering_inputs = [input_spec for input_spec in engineering_inputs if input_spec.target_variable != 'control_volume.properties_out[0].temperature']
    return engineering_inputs

def _missing_engineering_input_statuses(unit_inst: Any, spec: Any, default_property_package: PropertyPackageType, session: Optional[FlowsheetSession]=None, unit_id: Optional[str]=None, builder: Any | None=None, unit_block: Any | None=None) -> List[Dict[str, Any]]:
    """Return unresolved public engineering inputs for one unit."""
    missing = []
    engineering_inputs = _effective_engineering_input_specs(unit_inst, spec, default_property_package, session=session, unit_id=unit_id)
    for input_spec in engineering_inputs:
        provided = input_spec.key in unit_inst.engineering_inputs or input_spec.target_variable in unit_inst.engineering_inputs or input_spec.target_variable in unit_inst.fixed_vars
        if not provided and builder is not None and (unit_block is not None):
            status = _component_path_status(builder, unit_block, input_spec.target_variable, satisfaction_mode=input_spec.satisfaction_mode)
            provided = status.get('resolved', False) and status.get('fixed', False)
        if provided:
            continue
        missing.append(_engineering_input_payload(input_spec))
    return missing

def _internal_hygiene_summary() -> Dict[str, Any]:
    """Summarize server-owned solver hygiene for the public process surface."""
    return {'server_owned': True, 'automatic_scaling': True, 'sequential_decomposition_ordering': True, 'tear_stream_inference': True, 'translator_aware_initialization': True, 'recovery_enabled': True}

def _required_fix_status(builder: Any, unit_block: Any, variable_spec: Any) -> Dict[str, Any]:
    """Resolve and evaluate a required-fix contract item."""
    status = _component_path_status(builder, unit_block, variable_spec.name, satisfaction_mode=getattr(variable_spec, 'satisfaction_mode', 'all'))
    status['description'] = variable_spec.description
    status['units'] = variable_spec.units
    status['indexed'] = variable_spec.indexed
    return status

def _session_connection_specs(session: FlowsheetSession) -> List[Dict[str, Any]]:
    """Return session connections in the shape expected by SD helpers."""
    return [{'src_unit': conn.source_unit, 'src_port': conn.source_port, 'dest_unit': conn.dest_unit, 'dest_port': conn.dest_port, 'translator_id': conn.translator_id} for conn in session.connections]

def _tear_stream_payload(tear_streams: List[Any]) -> List[Dict[str, str]]:
    """Format tear-stream tuples for process-facing responses."""
    formatted: List[Dict[str, str]] = []
    for item in tear_streams or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        src, dst = (str(item[0]), str(item[1]))
        formatted.append({'source_unit': src, 'dest_unit': dst, 'label': f'{src}->{dst}'})
    return formatted

def _initialization_playbook_payload(session: FlowsheetSession, builder: Any, model: Any, units: Dict[str, Any]) -> Dict[str, Any]:
    """Classify a built topology and expose the server-owned SD playbook."""
    try:
        from watertap_engine_mcp.solver.initializer import FlowsheetInitializer
        unit_types = {unit_id: unit_inst.unit_type for unit_id, unit_inst in session.units.items()}
        connections = _session_connection_specs(session)
        initializer = FlowsheetInitializer(flowsheet=getattr(model, 'fs', model), model=model, thermal_couplings=session.thermal_couplings, runtime_metadata=session.config.runtime_metadata, unit_types=unit_types)
        topology = initializer.classify_topology(units, connections)
        raw_tears = [tuple(item) for item in topology.get('tear_streams', []) if isinstance(item, (list, tuple)) and len(item) == 2]
        payload: Dict[str, Any] = {'status': 'classified', 'family': topology.get('family', 'generic'), 'recipe_name': topology.get('recipe_name', 'generic_sequential'), 'confidence': topology.get('confidence', 0.0), 'template_name': topology.get('template_name'), 'unit_types': topology.get('unit_types', {}), 'tear_streams': _tear_stream_payload(raw_tears), 'notes': topology.get('notes', []), 'initialization_order': [], 'initialization_order_source': 'IDAES_SequentialDecomposition', 'warnings': []}
        try:
            payload['initialization_order'] = initializer.get_initialization_order(units, connections, tear_streams=raw_tears or None)
        except Exception as exc:
            payload['initialization_order_source'] = 'unavailable'
            payload['warnings'].append(f'Could not compute initialization order before sizing is complete: {exc}')
        return payload
    except Exception as exc:
        return {'status': 'unavailable', 'error': f'Could not classify topology initialization playbook: {exc}', 'warnings': []}

def _build_preflight_payload(session_id: str, session: FlowsheetSession, unit_id: Optional[str]=None, *, include_expert: bool=False, model_context: Optional[tuple[Any, Any, Dict[str, Any]]]=None) -> Dict[str, Any]:
    """Build a live readiness/preflight report for a session."""
    payload: Dict[str, Any] = {'session_id': session_id, 'build_success': False, 'ready_to_solve': False, 'status': 'error', 'issues': [], 'issue_codes': [], 'warnings': [], 'unit_dof': {}, 'engineering_requirements': {}, 'missing_engineering_inputs': {}, 'live_dof_requirements': {}, 'unit_spec_sets': {}, 'applied_engineering_defaults': {}, 'engineering_readiness': False, 'readiness_report': None, 'readiness_stages': [], 'internal_hygiene_summary': _internal_hygiene_summary(), 'unit_ports': {}, 'feed_units_without_state': [], 'minimum_viable_flowsheet': None, 'costing': None, 'certification': _flowsheet_certification(session)}
    if include_expert:
        payload['actual_unfixed_variables'] = {}
        payload['required_fixes'] = {}
        payload['unresolved_required_fixes'] = {}
        payload['unresolved_scaling_paths'] = {}
    feed_units = [uid for uid, unit in session.units.items() if unit.unit_type in {'Feed', 'FeedZO'}]
    for feed_unit in feed_units:
        if feed_unit not in session.feed_states:
            payload['feed_units_without_state'].append(feed_unit)
    if not feed_units and (not session.feed_states):
        payload['warnings'].append('No explicit feed units are present')
    minimum_viable = _minimum_viable_flowsheet_payload(session)
    payload['minimum_viable_flowsheet'] = minimum_viable
    if not minimum_viable['met']:
        payload['issue_codes'].append(MINIMUM_VIABLE_FLOWSHEET_NOT_MET)
        payload['issues'].extend(minimum_viable['issues'])
    if model_context is None:
        try:
            builder, model, units = build_model_context(session)
        except ImportError as exc:
            payload['issues'].append(f'WaterTAP/IDAES not available: {exc}')
            return payload
        except Exception as exc:
            payload['issues'].append(f'Model build failed: {exc}')
            return payload
    else:
        builder, model, units = model_context
    try:
        fs = getattr(model, 'fs', model)
        total_dof = compute_live_dof(fs)
        payload['total_dof'] = total_dof
        payload['build_success'] = True
        payload['certification'] = _flowsheet_certification(session, builder=builder)
        costing_report = builder.get_costing_report()
        if costing_report.get('enabled'):
            payload['costing'] = costing_report
            if costing_report.get('skipped_units'):
                skipped = ', '.join((item['unit_id'] for item in costing_report['skipped_units']))
                payload['warnings'].append(f'Costing is partial for this flowsheet. Skipped units: {skipped}')
        selected_units = units
        if unit_id:
            selected = units.get(unit_id)
            if selected is None:
                payload['issues'].append(f"Unit '{unit_id}' not found in built model")
                return payload
            selected_units = {unit_id: selected}
        for current_unit_id, unit_block in selected_units.items():
            unit_inst = session.units.get(current_unit_id)
            if unit_inst is None:
                continue
            try:
                payload['unit_dof'][current_unit_id] = compute_unit_dof(unit_block)
            except Exception as exc:
                payload['unit_dof'][current_unit_id] = f'error: {exc}'
            payload['unit_ports'][current_unit_id] = _unit_port_names(unit_block)
            if include_expert:
                payload['actual_unfixed_variables'][current_unit_id] = _live_unfixed_variables(unit_block)
            spec = get_unit_spec_from_registry(unit_inst.unit_type)
            payload['engineering_requirements'][current_unit_id] = [_engineering_input_payload(item) for item in _effective_engineering_input_specs(unit_inst, spec, session.config.default_property_package, session=session, unit_id=current_unit_id)]
            payload['missing_engineering_inputs'][current_unit_id] = _missing_engineering_input_statuses(unit_inst, spec, session.config.default_property_package, session=session, unit_id=current_unit_id, builder=builder, unit_block=unit_block)
            spec_set_status = evaluate_unit_spec_set(unit_id=current_unit_id, unit_inst=unit_inst, builder=builder, unit_block=unit_block)
            if spec_set_status is not None:
                payload['unit_spec_sets'][current_unit_id] = spec_set_status.model_dump(mode='python')
                if spec_set_status.valid and spec_set_status.mode == 'recovery_fixed':
                    payload['missing_engineering_inputs'][current_unit_id] = [item for item in payload['missing_engineering_inputs'][current_unit_id] if item.get('target_variable') != 'area' and item.get('key') not in {'area', 'area_m2'}]
            current_dof = payload['unit_dof'].get(current_unit_id)
            if total_dof != 0 and isinstance(current_dof, (int, float)) and (current_dof > 0):
                live_requirements = _live_dof_requirements(unit_block)
                registered_targets = {item['target_variable'] for item in payload['engineering_requirements'][current_unit_id]}
                payload['live_dof_requirements'][current_unit_id] = [item for item in live_requirements if item['target_variable'] not in registered_targets]
            else:
                payload['live_dof_requirements'][current_unit_id] = []
            payload['applied_engineering_defaults'][current_unit_id] = []
            if include_expert:
                payload['required_fixes'][current_unit_id] = []
                payload['unresolved_required_fixes'][current_unit_id] = []
                payload['unresolved_scaling_paths'][current_unit_id] = []
                for variable_spec in _effective_required_fixes(unit_inst, spec, session.config.default_property_package, session=session, unit_id=current_unit_id):
                    status = _required_fix_status(builder, unit_block, variable_spec)
                    payload['required_fixes'][current_unit_id].append(status)
                    if not status['resolved'] or not status.get('fixed', False):
                        payload['unresolved_required_fixes'][current_unit_id].append(status)
                for scaling_path in spec.default_scaling:
                    status = _component_path_status(builder, unit_block, scaling_path)
                    if not status['resolved']:
                        payload['unresolved_scaling_paths'][current_unit_id].append(status)
        if total_dof == 0:
            if payload['feed_units_without_state'] or not minimum_viable['met']:
                payload['status'] = 'underspecified'
            else:
                payload['status'] = 'ready'
                payload['ready_to_solve'] = True
        elif total_dof > 0:
            payload['status'] = 'underspecified'
            payload['issues'].append(f'Total DOF = {total_dof} (should be 0)')
        else:
            payload['status'] = 'overspecified'
            payload['issues'].append(f'Total DOF = {total_dof} (should be 0)')
        invalid_spec_sets = [item for item in payload['unit_spec_sets'].values() if not item.get('valid', False)]
        for item in invalid_spec_sets:
            issue = item.get('issue')
            if issue:
                payload['issues'].append(f"Unit '{item['unit_id']}' spec set invalid: {issue}")
        if payload['feed_units_without_state']:
            payload['issues'].append('Feed state missing for: ' + ', '.join(payload['feed_units_without_state']))
        missing_engineering_units = [unit for unit, items in payload['missing_engineering_inputs'].items() if items]
        if missing_engineering_units:
            payload['warnings'].append('Engineering inputs remain unresolved for: ' + ', '.join(sorted(missing_engineering_units)))
        live_dof_units = [unit for unit, items in payload['live_dof_requirements'].items() if items]
        if live_dof_units:
            payload['warnings'].append('Live DOF closure variables remain unresolved for: ' + ', '.join(sorted(live_dof_units)) + '. Provide these under units[].fixed_vars in build_from_topology or via fix_variable().')
        certification = payload['certification']
        if certification['flowsheet_costing_enabled'] and certification['costing_blocked_by_non_costable_units']:
            payload['warnings'].append('Flowsheet is not cost-certified because it contains non-costable units: ' + ', '.join(certification['blocking_non_cost_supported_units']))
        if certification['flowsheet_costing_enabled'] and certification['costing_blocked_by_missing_product_stream']:
            payload['warnings'].append('Flowsheet costing is enabled but no product stream could be resolved for LCOW.')
        payload['engineering_readiness'] = minimum_viable['met'] and (not payload['issue_codes']) and (not payload['feed_units_without_state']) and (not any(payload['missing_engineering_inputs'].values())) and payload['ready_to_solve'] and (not invalid_spec_sets)
        readiness_report = build_readiness_report(session_id=session_id, status=payload['status'], build_success=payload['build_success'], total_dof=payload.get('total_dof'), feed_units_without_state=payload['feed_units_without_state'], unit_spec_sets={unit_id: evaluate_unit_spec_set(unit_id=unit_id, unit_inst=session.units[unit_id], builder=builder, unit_block=units.get(unit_id)) for unit_id in payload['unit_spec_sets']}, issues=payload['issues'], warnings=payload['warnings'], issue_codes=payload['issue_codes'])
        payload['readiness_report'] = readiness_report.model_dump(mode='python')
        payload['readiness_stages'] = payload['readiness_report']['stages']
        return payload
    except Exception as exc:
        payload['issues'].append(f'Preflight failed: {exc}')
        return payload

def create_session(name: str='', description: str='', session_preset: Optional[str]=None, property_package: str='SEAWATER', property_package_config: Optional[Dict[str, Any]]=None, session_id: Optional[str]=None, reaction_config: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
    """Create a new WaterTAP flowsheet session.

    Returns:
        Dict with session_id and configuration details"""
    validation_error = _finite_validation_error('create_session', {'name': name, 'description': description, 'session_preset': session_preset, 'property_package': property_package, 'property_package_config': property_package_config, 'session_id': session_id})
    if validation_error is not None:
        return validation_error
    try:
        stated_reaction_config = session_reaction_config(reaction_config)
    except ReactionConfigRefused as exc:
        return redact_public_payload({'error': str(exc), 'termination_condition': 'reaction_config_refused'})
    created = SessionLifecycle(session_manager).create(name=name, description=description, session_preset=session_preset, property_package=property_package, property_package_config=property_package_config, session_id=session_id)
    if stated_reaction_config is not None and 'error' not in created:
        session = session_manager.load(created['session_id'])
        session.config.runtime_metadata[REACTION_CONFIG_TARGET] = stated_reaction_config
        session_manager.save(session)
        created[REACTION_CONFIG_TARGET] = stated_reaction_config
    return created

def list_session_presets() -> List[Dict[str, Any]]:
    """List curated session presets for public wastewater/reuse/ZLD workflows."""
    return [{'name': preset.name, 'description': preset.description, 'property_package': preset.property_package.name, 'property_package_config_defaults': preset.property_package_config_defaults, 'required_override_keys': preset.required_override_keys} for preset in list_session_presets_registry()]

def get_session_preset(name: str) -> Dict[str, Any]:
    """Get one curated session preset."""
    try:
        preset = get_session_preset_from_registry(name)
    except KeyError as exc:
        return {'error': str(exc)}
    return {'name': preset.name, 'description': preset.description, 'property_package': preset.property_package.name, 'property_package_config_defaults': preset.property_package_config_defaults, 'required_override_keys': preset.required_override_keys}

def list_downstream_handoff_profiles(public_only: bool=False) -> Dict[str, Any]:
    """List strict QSDsan -> WaterTAP downstream ingress profiles."""
    profiles = list_downstream_handoff_profiles_registry(public_only=public_only)
    return {'profiles': profiles, 'total': len(profiles), 'public_only': public_only}

def validate_downstream_handoff(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate one QSDsan downstream handoff payload for WaterTAP ingress."""
    try:
        return validate_downstream_handoff_payload(payload)
    except ValueError as exc:
        return {'error': str(exc)}

def get_session(session_id: str) -> Dict[str, Any]:
    """Get details of an existing session.

    Args:
        session_id: Session identifier

    Returns:
        Full session state including units, connections, and DOF status
    """
    return SessionLifecycle(session_manager).get(session_id)

def list_sessions() -> List[Dict[str, Any]]:
    """List all flowsheet sessions.

    Returns:
        List of session summaries (id, name, status, timestamps)
    """
    return SessionLifecycle(session_manager).list()

def delete_session(session_id: str) -> Dict[str, Any]:
    """Delete a flowsheet session.

    Args:
        session_id: Session to delete

    Returns:
        Confirmation or error message
    """
    return SessionLifecycle(session_manager).delete(session_id)

def list_units(category: Optional[str]=None, property_package: Optional[str]=None, is_idaes: Optional[bool]=None) -> List[Dict[str, Any]]:
    """List available unit types with optional filtering.

    Args:
        category: Filter by category (membrane, thermal, pump, etc.)
        property_package: Filter by compatible property package
        is_idaes: Filter IDAES vs WaterTAP units

    Returns:
        List of unit specifications
    """
    cat = None
    if category:
        try:
            cat = UnitCategory(category.lower())
        except ValueError:
            valid = [c.value for c in UnitCategory]
            return {'error': f'Invalid category. Valid: {valid}'}
    pkg = None
    if property_package:
        try:
            pkg = PropertyPackageType[property_package.upper()]
        except KeyError:
            valid = [p.name for p in PropertyPackageType]
            return {'error': f'Invalid property_package. Valid: {valid}'}
    units = list_units_registry(category=cat, property_package=pkg, is_idaes=is_idaes)
    return [{'unit_type': u.unit_type, 'category': u.category.value, 'module_path': u.module_path, 'is_idaes_unit': u.is_idaes_unit, 'n_inlets': u.n_inlets, 'n_outlets': u.n_outlets, 'port_roles': u.port_roles, 'description': u.description, 'solve_supported': u.solve_supported, 'calculation_class': u.calculation_class, 'recommendation_eligible': u.recommendation_eligible, 'costing_supported': u.costing.supported, 'costing_supported_packages': list(u.costing.supported_packages), 'costing_source': u.costing.source.value, 'canonical_unit_op_type': u.canonical_unit_op_type.value} for u in units]

def list_property_packages() -> List[Dict[str, Any]]:
    """List all available property packages with their specifications.

    Returns:
        List of property package details including module paths,
        components, flow basis, and compatibility info.

    Note: Some class names are duplicated across modules (NaClParameterBlock,
    WaterParameterBlock). Always use the full module_path for imports.
    """
    return [{'name': spec.pkg_type.name, 'class_name': spec.class_name, 'module_path': spec.module_path, 'phases': list(spec.phases), 'required_components': spec.required_components, 'flow_basis': spec.flow_basis, 'requires_reaction_package': spec.requires_reaction_package, 'charge_balance_required': spec.charge_balance_required, 'database_required': spec.database_required, 'requires_config': spec.requires_config, 'config_fields': spec.config_fields, 'config_help': spec.config_kwargs} for spec in PROPERTY_PACKAGES.values()]

def get_property_package_specification(property_package: str) -> Dict[str, Any]:
    """Get the full configuration/discovery schema for one property package."""
    try:
        pkg_type = _normalize_property_package(property_package)
    except ValueError as exc:
        return {'error': str(exc)}
    spec = get_property_package_spec(pkg_type)
    return {'name': spec.pkg_type.name, 'class_name': spec.class_name, 'module_path': spec.module_path, 'phases': sorted(spec.phases), 'required_components': spec.required_components, 'optional_components': spec.optional_components, 'state_vars': spec.state_vars, 'flow_basis': spec.flow_basis, 'requires_reaction_package': spec.requires_reaction_package, 'compatible_reaction_packages': spec.compatible_reaction_packages, 'charge_balance_required': spec.charge_balance_required, 'database_required': spec.database_required, 'default_scaling': spec.default_scaling, 'requires_config': spec.requires_config, 'config_fields': spec.config_fields, 'config_help': spec.config_kwargs}

def list_translators() -> List[Dict[str, Any]]:
    """List available property package translators.

    Includes named biological translators plus the curated generic mixed-package
    translator patterns exposed on the public MCP surface.

    Returns:
        List of translator specifications with source/destination packages
    """
    return [{'name': t.name, 'source': t.source_pkg.name, 'destination': t.dest_pkg.name, 'module_path': t.module_path, 'requires_reaction_packages': t.requires_reaction_packages, 'description': t.description} for t in TRANSLATORS.values()]

def get_unit_spec(unit_type: str) -> Dict[str, Any]:
    """Get full specification for a unit type.

    Args:
        unit_type: Unit type name (e.g., "ReverseOsmosis0D")

    Returns:
        Complete unit specification including DOF requirements,
        typical values, scaling defaults, and initialization hints
    """
    try:
        spec = get_unit_spec_from_registry(unit_type)
        support_profiles = []
        for profile in supported_unit_profiles(spec):
            support_profiles.append({'name': profile.name, 'description': profile.description, 'property_package': profile.property_package.name if profile.property_package is not None else None, 'require_session_default_package': profile.require_session_default_package, 'unit_config_defaults': profile.config_defaults, 'unit_config_constraints': profile.config_constraints, 'allowed_config_keys': sorted(profile.allowed_config_keys), 'required_session_package_config': profile.required_session_package_config, 'required_solutes': profile.required_solutes, 'session_package_config_constraints': profile.session_package_config_constraints, 'package_config_normalizer': profile.package_config_normalizer, 'engineering_inputs': [_engineering_input_payload(item) for item in profile.engineering_inputs or [effective_input for effective_input in effective_engineering_inputs(spec, profile.property_package or spec.compatible_property_packages[0], profile.config_defaults or profile.config_constraints)]], 'auto_fixed_variables': [{'name': auto_fix.name, 'value': auto_fix.value, 'use_existing_value': auto_fix.use_existing_value, 'description': auto_fix.description} for auto_fix in profile.auto_fixed_variables]})
        return {'unit_type': spec.unit_type, 'class_name': spec.class_name, 'module_path': spec.module_path, 'category': spec.category.value, 'compatible_property_packages': [p.name for p in spec.compatible_property_packages], 'engineering_inputs': [_engineering_input_payload(item) for item in effective_engineering_inputs(spec, spec.compatible_property_packages[0], None)], 'input_contract': unit_input_contract_summary(spec, spec.compatible_property_packages[0], None), 'typical_values': spec.typical_values, 'n_inlets': spec.n_inlets, 'n_outlets': spec.n_outlets, 'inlet_names': spec.inlet_names, 'outlet_names': spec.outlet_names, 'port_roles': spec.port_roles, 'config_schema': _build_unit_config_schema(unit_type), 'is_idaes_unit': spec.is_idaes_unit, 'description': spec.description, 'support_profiles': support_profiles, 'solve_supported': spec.solve_supported, 'calculation_class': spec.calculation_class, 'recommendation_eligible': spec.recommendation_eligible, 'costing_supported': spec.costing.supported, 'costing_supported_packages': list(spec.costing.supported_packages), 'costing_source': spec.costing.source.value}
    except KeyError:
        return {'error': f'Unknown unit type: {unit_type}'}

def get_unit_config_schema(unit_type: str) -> Dict[str, Any]:
    """Get the live constructor/config schema for a unit type."""
    try:
        spec = get_unit_spec_from_registry(unit_type)
    except KeyError:
        return {'error': f'Unknown unit type: {unit_type}'}
    return {'unit_type': unit_type, 'module_path': spec.module_path, 'class_name': spec.class_name, 'config_schema': _build_unit_config_schema(unit_type)}

def preview_unit_build(unit_type: str, property_package: str='SEAWATER', property_package_config: Optional[Dict[str, Any]]=None, config: Optional[Dict[str, Any]]=None, fixed_vars: Optional[Dict[str, float]]=None) -> Dict[str, Any]:
    """Build an unqualified unit preview; geometry must be explicitly supplied.

    fixed_vars names native variables such as crystal_median_length [m]. A
    preview is discovery only; rating still requires the sizing reference.
    """
    try:
        pkg_type = _normalize_property_package(property_package)
    except ValueError as exc:
        return {'error': str(exc)}
    try:
        session = FlowsheetSession(config=SessionConfig(session_id='preview', id_strategy='preview', default_property_package=pkg_type, property_package_config=property_package_config or {}))
        normalized_config, _ = _normalize_supported_unit(session, unit_type, config or {})
        session.add_unit('preview_unit', unit_type, normalized_config)
        for variable, value in (fixed_vars or {}).items():
            session.fix_variable('preview_unit', variable, value)
    except Exception as exc:
        payload = _preflight_error_payload('preview', issues=[f'Preview contract failed: {exc}'])
        payload['unit_type'] = unit_type
        payload['property_package'] = property_package
        payload['config_schema'] = _build_unit_config_schema(unit_type)
        return payload
    payload = _build_preflight_payload('preview', session, unit_id='preview_unit')
    payload['unit_type'] = unit_type
    payload['property_package'] = property_package
    payload['config_schema'] = _build_unit_config_schema(unit_type)
    return payload

def set_feed_state(session_id: str, unit_id: str, flow_vol_m3_hr: Optional[float]=None, tds_mg_L: Optional[float]=None, nacl_mg_L: Optional[float]=None, components: Optional[Dict[str, float]]=None, temperature_C: Optional[float]=None, pressure_bar: float=1.0, concentration_units: str='mg/L', concentration_basis: str='mass', component_charges: Optional[Dict[str, int]]=None, electroneutrality_species: Optional[str]=None, phase_mass_flow_kg_s: Optional[Dict[str, float]]=None, component_mass_flow_kg_s: Optional[Dict[str, float]]=None, component_mol_flow_mol_s: Optional[Dict[str, float]]=None, H2O_mass_kg_s: Optional[float]=None, NaCl_mass_kg_s: Optional[float]=None, temperature_K: Optional[float]=None, pressure_Pa: Optional[float]=None) -> Dict[str, Any]:
    """Set feed state for an explicit Feed/FeedZO unit.

    Args:
        session_id: Session containing the feed unit
        unit_id: Explicit feed unit identifier
        flow_vol_m3_hr: Volumetric flow rate in m³/hr
        tds_mg_L: TDS concentration (for SEAWATER package)
        nacl_mg_L: NaCl concentration (for NACL package)
        components: Dict of component concentrations (for MCAS, etc.)
        temperature_C: Temperature in Celsius. Unstated (with no temperature_K): 25 C, except on a
            MODIFIED_ASM2D feed, which is refused (it fixes the kinetic set's temperature)
        pressure_bar: Pressure in bar
        concentration_units: Units for concentrations ("mg/L", "mol/L", "kg/m3")
        concentration_basis: "mass" or "molar" basis for concentrations
        component_charges: Dict of component charges for MCAS (e.g., {"Na_+": 1, "Cl_-": -1})
        electroneutrality_species: Species to adjust for electroneutrality balance
        phase_mass_flow_kg_s: Optional explicit WATER feed phase flows
            (e.g., {"Vap": 1.0, "Liq": 1e-8})
        component_mass_flow_kg_s: Optional exact component mass-flow state for
            liquid-phase packages. Keys are component names, values are kg/s.
        component_mol_flow_mol_s: Optional exact component molar-flow state for
            MCAS liquid-phase packages. Keys are component names, values are mol/s.
        H2O_mass_kg_s: Convenience alias for exact H2O mass flow
        NaCl_mass_kg_s: Convenience alias for exact NaCl mass flow
        temperature_K: Convenience alias for absolute temperature
        pressure_Pa: Convenience alias for absolute pressure

    Returns:
        Feed state details and state_args for the unit property package
    """
    validation_error = _input_validation_error(SetFeedStateInput, session_id=session_id, unit_id=unit_id, flow_vol_m3_hr=flow_vol_m3_hr, tds_mg_L=tds_mg_L, nacl_mg_L=nacl_mg_L, components=components, temperature_C=temperature_C, pressure_bar=pressure_bar, concentration_units=concentration_units, concentration_basis=concentration_basis, component_charges=component_charges, electroneutrality_species=electroneutrality_species, phase_mass_flow_kg_s=phase_mass_flow_kg_s, component_mass_flow_kg_s=component_mass_flow_kg_s, component_mol_flow_mol_s=component_mol_flow_mol_s, H2O_mass_kg_s=H2O_mass_kg_s, NaCl_mass_kg_s=NaCl_mass_kg_s, temperature_K=temperature_K, pressure_Pa=pressure_Pa)
    if validation_error is not None:
        return validation_error
    try:
        session = _load_session(session_id)
    except FileNotFoundError:
        return redact_public_payload({'error': f"Session '{session_id}' not found"})
    unit_inst = session.units.get(unit_id)
    if unit_inst is None:
        return redact_public_payload({'error': f"Unit '{unit_id}' not found"})
    if unit_inst.unit_type not in {'Feed', 'FeedZO'}:
        return redact_public_payload({'error': f"Unit '{unit_id}' is not a feed unit (type: {unit_inst.unit_type})"})
    pkg = _unit_package_type(session, unit_id)
    exact_mass_state = dict(component_mass_flow_kg_s or {})
    if H2O_mass_kg_s is not None:
        exact_mass_state['H2O'] = H2O_mass_kg_s
    if NaCl_mass_kg_s is not None:
        exact_mass_state['NaCl'] = NaCl_mass_kg_s
    if not exact_mass_state:
        exact_mass_state = None
    try:
        temperature_C = stated_feed_temperature_c(modified_asm2d=pkg == PropertyPackageType.MODIFIED_ASM2D, temperature_c=temperature_C, temperature_k=temperature_K, where=f'set_feed_state on {unit_id} ({pkg.name})')
    except ModifiedAsm2dKineticsRefused as exc:
        return redact_public_payload({'error': str(exc), 'termination_condition': 'modified_asm2d_kinetics_refused'})
    if pressure_Pa is not None:
        pressure_bar = float(pressure_Pa) / 100000.0
    try:
        feed_state = build_feed_state_payload(unit_id=unit_id, pkg=pkg, flow_vol_m3_hr=flow_vol_m3_hr, tds_mg_L=tds_mg_L, nacl_mg_L=nacl_mg_L, components=components, temperature_C=temperature_C, pressure_bar=pressure_bar, concentration_units=concentration_units, concentration_basis=concentration_basis, component_charges=component_charges, electroneutrality_species=electroneutrality_species, phase_mass_flow_kg_s=phase_mass_flow_kg_s, component_mass_flow_kg_s=exact_mass_state, component_mol_flow_mol_s=component_mol_flow_mol_s, property_package_config=session.config.property_package_config)
    except ValueError as e:
        return redact_public_payload({'error': str(e)})
    session.set_feed_state(unit_id, feed_state)
    session_manager.save(session)
    return redact_public_payload({'session_id': session_id, 'unit_id': unit_id, 'feed_state': feed_state, 'all_feed_states': session.feed_states})

def set_unit_engineering_inputs(session_id: str, unit_id: str, inputs: Dict[str, float]) -> Dict[str, Any]:
    """Set process-facing engineering inputs for one unit."""
    validation_error = _input_validation_error(SetUnitEngineeringInputsInput, session_id=session_id, unit_id=unit_id, inputs=inputs)
    if validation_error is not None:
        return validation_error
    try:
        session = _load_session(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    unit_inst = session.units.get(unit_id)
    if unit_inst is None:
        return {'error': f"Unit '{unit_id}' not found"}
    try:
        normalized_inputs, compiled = _normalize_compiled_engineering_inputs(session, unit_id, unit_inst, inputs)
        session.set_engineering_inputs(unit_id, normalized_inputs)
    except (TypeError, ValueError) as exc:
        return {'error': str(exc)}
    session_manager.save(session)
    return {'session_id': session_id, 'unit_id': unit_id, 'engineering_inputs': session.units[unit_id].engineering_inputs, 'compiled_internal_fixes': compiled}

def _normalize_compiled_engineering_inputs(session: FlowsheetSession, unit_id: str, unit_inst: Any, inputs: Dict[str, Any]) -> tuple[Dict[str, float], Dict[str, float]]:
    """Validate, normalize, and compile process-facing engineering inputs."""
    spec = get_unit_spec_from_registry(unit_inst.unit_type)
    engineering_specs = _effective_engineering_input_specs(unit_inst, spec, session.config.default_property_package, session=session, unit_id=unit_id)
    normalized_inputs: Dict[str, float] = {}
    for raw_key, raw_value in (inputs or {}).items():
        input_spec, storage_key, _ = resolve_engineering_input(engineering_specs, raw_key)
        if input_spec is None:
            if unit_inst.unit_type == 'ReverseOsmosis0D' and raw_key in {'recovery', 'recovery_vol_phase[0,Liq]', 'recovery_vol_phase[0.0,Liq]'}:
                normalized_inputs['recovery'] = _validate_ro_recovery_input(raw_value)
                continue
            valid = sorted((item.key for item in engineering_specs))
            if unit_inst.unit_type == 'ReverseOsmosis0D':
                valid.append('recovery')
            raise ValueError(f"Unknown engineering input '{raw_key}' for unit '{unit_id}'. Valid engineering inputs: {valid}")
        normalized_inputs[storage_key or input_spec.key] = _convert_required_fix_input_value(raw_value, input_spec.units, target_variable=input_spec.target_variable)
    compiled = compile_engineering_inputs(spec, session.config.default_property_package, unit_inst.config, normalized_inputs)
    return (normalized_inputs, compiled)

def create_unit(session_id: str, unit_id: str, unit_type: str, config: Optional[Dict[str, Any]]=None, engineering_inputs: Optional[Dict[str, Any]]=None, role: Optional[str]=None, role_stage_index: Optional[int]=None, role_group: Optional[str]=None) -> Dict[str, Any]:
    """Create a unit in the flowsheet.

    Args:
        session_id: Session to add unit to
        unit_id: Unique identifier for this unit instance
        unit_type: Type of unit (e.g., "ReverseOsmosis0D")
        config: Unit-specific configuration options
        engineering_inputs: Optional process-facing engineering inputs to
            validate and persist on the new unit in the same call. Uses the
            same public engineering-input surface as
            ``set_unit_engineering_inputs()``.
        role: Optional structural role (see ``core.roles.UnitRole``) used by the
            role-based convergence priors so from-scratch flowsheets inherit the
            same initialization/recycle/SRT seeding as the canonical templates.
        role_stage_index: Optional 0-based position within a multi-stage role
            sequence (None — the default — for the single-winner case). Surfaced
            through ``core.roles.RoleResolver``'s stage-aware multiplicity API.
        role_group: Optional partition label scoping role multiplicity queries
            (e.g. a parallel-train id).

    Returns:
        Unit details and DOF requirements
    """
    validation_error = _input_validation_error(CreateUnitInput, session_id=session_id, unit_id=unit_id, unit_type=unit_type, config=config, engineering_inputs=engineering_inputs, role=role, role_stage_index=role_stage_index, role_group=role_group)
    if validation_error is not None:
        return validation_error
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    spec = get_unit_spec(unit_type)
    if 'error' in spec:
        return spec
    try:
        normalized_role = normalize_role(role)
    except ValueError as exc:
        return {'error': str(exc)}
    compiled_inputs: Dict[str, float] = {}
    try:
        normalized_config, profile = _normalize_supported_unit(session, unit_type, config or {})
        unit = session.add_unit(unit_id, unit_type, normalized_config, role=normalized_role, role_stage_index=role_stage_index, role_group=role_group)
        if engineering_inputs:
            normalized_inputs, compiled_inputs = _normalize_compiled_engineering_inputs(session, unit_id, unit, engineering_inputs)
            session.set_engineering_inputs(unit_id, normalized_inputs)
    except (TypeError, ValueError) as e:
        return {'error': str(e)}
    except Exception as e:
        return {'error': str(e)}
    session_manager.save(session)
    engineering_requirements = spec.get('engineering_inputs', [])
    typical_values = spec.get('typical_values', {})
    if profile is not None and profile.typical_values:
        typical_values = profile.typical_values
    if profile is not None:
        engineering_requirements = [_engineering_input_payload(item) for item in profile.engineering_inputs or _effective_engineering_input_specs(unit, get_unit_spec_from_registry(unit_type), session.config.default_property_package, session=session, unit_id=unit_id)]
    payload = {'session_id': session_id, 'unit_id': unit_id, 'unit_type': unit_type, 'engineering_requirements': engineering_requirements, 'typical_values': typical_values, 'config': unit.config, 'role': unit.role, 'role_stage_index': unit.role_stage_index, 'role_group': unit.role_group}
    if engineering_inputs:
        payload['engineering_inputs'] = unit.engineering_inputs
        payload['compiled_internal_fixes'] = compiled_inputs
    return payload

def create_translator(session_id: str, translator_id: str, source_package: str, dest_package: str) -> Dict[str, Any]:
    """Create a translator block between property packages.

    Args:
        session_id: Session to add translator to
        translator_id: Unique identifier for this translator
        source_package: Source property package type
        dest_package: Destination property package type

    Returns:
        Translator details or error if no translator exists
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    try:
        source_pkg = PropertyPackageType[source_package.upper()]
        dest_pkg = PropertyPackageType[dest_package.upper()]
    except KeyError as e:
        return {'error': f'Invalid property package: {e}'}
    translator = get_translator(source_pkg, dest_pkg)
    if translator is None:
        compat = check_compatibility(source_pkg, dest_pkg)
        return {'error': compat['message']}
    session.translators[translator_id] = {'name': translator.name, 'source_pkg': source_pkg.value, 'dest_pkg': dest_pkg.value, 'module_path': translator.module_path, 'config': {}}
    session_manager.save(session)
    return {'session_id': session_id, 'translator_id': translator_id, 'translator': translator.name, 'source_pkg': source_pkg.value, 'dest_pkg': dest_pkg.value}

def connect_ports(session_id: str, source_unit: str, source_port: str, dest_unit: str, dest_port: str, translator_id: Optional[str]=None) -> Dict[str, Any]:
    """Connect two units via their ports.

    Args:
        session_id: Session containing the units
        source_unit: Source unit ID
        source_port: Source port name (e.g., "outlet", "permeate")
        dest_unit: Destination unit ID
        dest_port: Destination port name (e.g., "inlet")
        translator_id: Optional translator ID if packages differ

    Returns:
        Connection details with compatibility warning if applicable
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    compatibility_warning = None
    if source_unit in session.units and dest_unit in session.units:
        source_pkg = _unit_port_package_type(session, source_unit, source_port)
        dest_pkg = _unit_port_package_type(session, dest_unit, dest_port)
        compat = check_compatibility(source_pkg, dest_pkg)
        if not translator_id and compat['requires_translator']:
            if compat['compatible']:
                compatibility_warning = f'Connection spans {source_pkg.name} → {dest_pkg.name}. {compat['message']}. Use create_translator() or connect_units(..., auto_create_translator=True).'
            else:
                compatibility_warning = f'Connection spans incompatible property packages {source_pkg.name} → {dest_pkg.name}. {compat['message']}'
    try:
        session.add_connection(source_unit=source_unit, source_port=source_port, dest_unit=dest_unit, dest_port=dest_port, translator_id=translator_id)
    except KeyError as e:
        return {'error': str(e)}
    session_manager.save(session)
    result = {'session_id': session_id, 'connection': {'source': f'{source_unit}.{source_port}', 'dest': f'{dest_unit}.{dest_port}', 'translator': translator_id}}
    if compatibility_warning:
        result['compatibility_note'] = compatibility_warning
    return result

def get_flowsheet_diagram(session_id: str) -> Dict[str, Any]:
    """Get ASCII diagram of flowsheet structure.

    Args:
        session_id: Session to visualize

    Returns:
        ASCII diagram and unit/connection lists
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    lines = ['Flowsheet Diagram', '=' * 40]
    if session.feed_states:
        for feed_unit in sorted(session.feed_states):
            lines.append(f'{feed_unit} → ...')
    for conn in session.connections:
        arrow = ' → '
        if conn.translator_id:
            arrow = f' →[{conn.translator_id}]→ '
        lines.append(f'  {conn.source_unit}.{conn.source_port}{arrow}{conn.dest_unit}.{conn.dest_port}')
    return {'diagram': '\n'.join(lines), 'units': list(session.units.keys()), 'connections': len(session.connections)}

def update_unit(session_id: str, unit_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Update configuration for an existing unit.

    Args:
        session_id: Session containing the unit
        unit_id: Unit to update
        config: Configuration values to update

    Returns:
        Updated unit details
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    if unit_id not in session.units:
        return {'error': f"Unit '{unit_id}' not found"}
    session.units[unit_id].config.update(config)
    if 'property_package' in config:
        session.config.unit_property_packages[unit_id] = config['property_package']
    session_manager.save(session)
    return {'session_id': session_id, 'unit_id': unit_id, 'updated_config': config}

def delete_unit(session_id: str, unit_id: str) -> Dict[str, Any]:
    """Delete a unit from the flowsheet.

    Also removes any connections involving this unit.

    Args:
        session_id: Session containing the unit
        unit_id: Unit to delete

    Returns:
        Confirmation and list of removed connections
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    if unit_id not in session.units:
        return {'error': f"Unit '{unit_id}' not found"}
    del session.units[unit_id]
    removed_conns = []
    new_conns = []
    for conn in session.connections:
        if conn.source_unit == unit_id or conn.dest_unit == unit_id:
            removed_conns.append(f'{conn.source_unit}.{conn.source_port} → {conn.dest_unit}.{conn.dest_port}')
        else:
            new_conns.append(conn)
    session.connections = new_conns
    session_manager.save(session)
    return {'session_id': session_id, 'deleted': unit_id, 'removed_connections': removed_conns}

def validate_flowsheet(session_id: str) -> Dict[str, Any]:
    """Validate flowsheet structure before building.

    Checks for:
    - All units have connections (except feed/product)
    - No orphan ports (required ports not connected)
    - Property package compatibility (session package vs unit compatibility)
    - DOF status

    Args:
        session_id: Session to validate

    Returns:
        Validation results with any issues
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    issues = []
    warnings = []
    feed_units = [unit_id for unit_id, unit_inst in session.units.items() if unit_inst.unit_type in ('Feed', 'FeedZO')]
    if feed_units:
        missing_feed_state = [unit_id for unit_id in feed_units if unit_id not in session.feed_states]
        if missing_feed_state:
            issues.append('Missing feed state for: ' + ', '.join(sorted(missing_feed_state)))
    elif not session.feed_states:
        issues.append('No feed state defined')
    total_dof = None
    connected_units = set()
    connected_ports = {}
    for conn in session.connections:
        connected_units.add(conn.source_unit)
        connected_units.add(conn.dest_unit)
        if conn.source_unit not in connected_ports:
            connected_ports[conn.source_unit] = set()
        connected_ports[conn.source_unit].add(conn.source_port)
        if conn.dest_unit not in connected_ports:
            connected_ports[conn.dest_unit] = set()
        connected_ports[conn.dest_unit].add(conn.dest_port)
    for unit_id in session.units:
        if unit_id not in connected_units and len(session.units) > 1:
            warnings.append(f"Unit '{unit_id}' has no connections")
    for unit_id, unit_inst in session.units.items():
        spec = get_unit_spec(unit_inst.unit_type)
        if 'error' in spec:
            continue
        unit_connected_ports = connected_ports.get(unit_id, set())
        if unit_inst.unit_type in ('Feed', 'FeedZO'):
            continue
        if unit_inst.unit_type in ('Product',):
            continue
        for inlet_port in spec.get('inlet_names', []):
            if inlet_port not in unit_connected_ports:
                if len(session.units) > 1:
                    warnings.append(f"Unit '{unit_id}' inlet port '{inlet_port}' not connected")
        for outlet_port in spec.get('outlet_names', []):
            if outlet_port not in unit_connected_ports:
                if len(session.units) > 1:
                    warnings.append(f"Unit '{unit_id}' outlet port '{outlet_port}' not connected")
    for unit_id, unit_inst in session.units.items():
        spec = get_unit_spec(unit_inst.unit_type)
        if 'error' in spec:
            continue
        compatible_pkgs = spec.get('compatible_property_packages', [])
        unit_pkg = _unit_package_type(session, unit_id)
        if compatible_pkgs:
            if unit_pkg.name not in compatible_pkgs:
                issues.append(f"Unit '{unit_id}' ({unit_inst.unit_type}) is not compatible with property package '{unit_pkg.name}'. Compatible packages: {', '.join(compatible_pkgs)}")
    for conn in session.connections:
        src_unit = session.units.get(conn.source_unit)
        dst_unit = session.units.get(conn.dest_unit)
        if not src_unit or not dst_unit:
            continue
        source_pkg = _unit_port_package_type(session, conn.source_unit, conn.source_port)
        dest_pkg = _unit_port_package_type(session, conn.dest_unit, conn.dest_port)
        compat = check_compatibility(source_pkg, dest_pkg)
        if conn.translator_id:
            if conn.translator_id not in session.translators:
                issues.append(f"Connection {conn.source_unit}->{conn.dest_unit} references missing translator '{conn.translator_id}'")
            continue
        if compat['requires_translator']:
            issues.append(f'Connection {conn.source_unit}.{conn.source_port}->{conn.dest_unit}.{conn.dest_port} requires a translator for {source_pkg.name} → {dest_pkg.name}: {compat['message']}')
    preflight = _build_preflight_payload(session_id, session)
    if preflight.get('build_success'):
        total_dof = preflight.get('total_dof', 0)
        if total_dof != 0:
            issues.append(f'Total DOF = {total_dof} (should be 0)')
        for warning in preflight.get('warnings', []):
            if warning not in warnings:
                warnings.append(warning)
    else:
        total_dof = 0
        for unit_id, unit_inst in session.units.items():
            try:
                spec = get_unit_spec_from_registry(unit_inst.unit_type)
            except KeyError:
                continue
            required = len(_effective_engineering_input_specs(unit_inst, spec, session.config.default_property_package, session=session, unit_id=unit_id))
            fixed = required - len(_missing_engineering_input_statuses(unit_inst, spec, session.config.default_property_package, session=session, unit_id=unit_id))
            unit_dof = required - fixed
            total_dof += unit_dof
            if unit_dof > 0:
                warnings.append(f"Unit '{unit_id}' has {unit_dof} unresolved engineering input(s)")
        if total_dof != 0:
            issues.append(f'Total DOF = {total_dof} (should be 0)')
    return {'session_id': session_id, 'valid': len(issues) == 0, 'issues': issues, 'warnings': warnings, 'total_dof': total_dof}

def get_dof_status(session_id: str) -> Dict[str, Any]:
    """Get live degrees of freedom status for the flowsheet.

    Args:
        session_id: Session to analyze

    Returns:
        Live Pyomo DOF count per unit and total, plus unfixed variables
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return redact_public_payload({'error': f"Session '{session_id}' not found"})
    try:
        _, model, units = build_model_context(session)
    except Exception as exc:
        return redact_public_payload({'session_id': session_id, 'status': 'error', 'error': f'Model build failed: {exc}'})
    report = compute_dof_report(model, units, verbose=True)
    total_dof = report['total_dof']
    dof_by_unit = report['unit_dof']
    unfixed_vars = {unit_id: _live_unfixed_variables(unit_block) for unit_id, unit_block in units.items() if isinstance(dof_by_unit.get(unit_id), int) and dof_by_unit[unit_id] > 0}
    session.update_dof_status(dof_by_unit, total_dof)
    session_manager.save(session)
    return redact_public_payload({'session_id': session_id, 'total_dof': total_dof, 'dof_by_unit': dof_by_unit, 'unit_dof': dof_by_unit, 'unfixed_variables': unfixed_vars, 'raw_block_dof': report.get('raw_block_dof'), 'status': report['status'], 'ready_to_solve': total_dof == 0})

def fix_variable(session_id: str, unit_id: str, var_name: str, value: float) -> Dict[str, Any]:
    """Fix a variable to a specific value.

    Args:
        session_id: Session containing the unit
        unit_id: Unit containing the variable
        var_name: Variable name (e.g., "A_comp[0, H2O]")
        value: Value to fix

    Returns:
        Updated fixed variables for the unit
    """
    validation_error = _finite_validation_error('fix_variable', {'session_id': session_id, 'unit_id': unit_id, 'var_name': var_name, 'value': value})
    if validation_error is not None:
        return validation_error
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    interpreted_value = value
    interpreted_units = None
    unit_inst = session.units.get(unit_id)
    if unit_inst is None:
        return {'error': f"Unit '{unit_id}' not found"}
    try:
        spec = get_unit_spec_from_registry(unit_inst.unit_type)
        for variable_spec in _effective_required_fixes(unit_inst, spec, session.config.default_property_package, session=session, unit_id=unit_id):
            if variable_spec.name != var_name:
                continue
            interpreted_units = variable_spec.units
            interpreted_value = _convert_required_fix_input_value(value, variable_spec.units, target_variable=variable_spec.name)
            break
    except ValueError as exc:
        return {'error': str(exc)}
    try:
        session.fix_variable(unit_id, var_name, interpreted_value)
    except KeyError as e:
        return {'error': str(e)}
    session_manager.save(session)
    return {'session_id': session_id, 'unit_id': unit_id, 'input_value': value, 'interpreted_units': interpreted_units, 'fixed': {var_name: interpreted_value}, 'all_fixed_vars': session.units[unit_id].fixed_vars}

def unfix_variable(session_id: str, unit_id: str, var_name: str) -> Dict[str, Any]:
    """Unfix a previously fixed variable.

    Args:
        session_id: Session containing the unit
        unit_id: Unit containing the variable
        var_name: Variable name to unfix

    Returns:
        Updated fixed variables for the unit
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    try:
        session.unfix_variable(unit_id, var_name)
    except KeyError as e:
        return {'error': str(e)}
    session_manager.save(session)
    return {'session_id': session_id, 'unit_id': unit_id, 'unfixed': var_name, 'remaining_fixed_vars': session.units[unit_id].fixed_vars}

def list_unfixed_vars(session_id: str, unit_id: str) -> Dict[str, Any]:
    """List live unfixed variables that still participate in the built model.

    Args:
        session_id: Session containing the unit
        unit_id: Unit to analyze

    Returns:
        Live unfixed variables with registry-backed requirement hints
    """
    try:
        session = _load_session(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    if unit_id not in session.units:
        return {'error': f"Unit '{unit_id}' not found"}
    unit_inst = session.units[unit_id]
    preview = _build_preflight_payload(session_id, session, unit_id=unit_id, include_expert=True)
    if not preview['build_success']:
        return preview
    return {'session_id': session_id, 'unit_id': unit_id, 'unit_type': unit_inst.unit_type, 'status': preview['status'], 'unit_dof': preview['unit_dof'].get(unit_id), 'unfixed_variables': preview['actual_unfixed_variables'].get(unit_id, []), 'required_fix_status': preview['required_fixes'].get(unit_id, []), 'unresolved_required_fixes': preview['unresolved_required_fixes'].get(unit_id, []), 'count': len(preview['actual_unfixed_variables'].get(unit_id, []))}

def get_scaling_status(session_id: str) -> Dict[str, Any]:
    """Get current scaling factor status.

    Args:
        session_id: Session to analyze

    Returns:
        Current scaling factors by unit and recommendations
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    scaling_by_unit = {}
    recommendations = {}
    for unit_id, unit_inst in session.units.items():
        scaling_by_unit[unit_id] = unit_inst.scaling_factors
        spec = get_unit_spec(unit_inst.unit_type)
        if 'error' not in spec:
            default_scaling = spec.get('default_scaling', {})
            recs = {k: v for k, v in default_scaling.items() if k not in unit_inst.scaling_factors}
            if recs:
                recommendations[unit_id] = recs
    return {'session_id': session_id, 'scaling_factors': scaling_by_unit, 'recommendations': recommendations}

def set_scaling_factor(session_id: str, unit_id: str, var_name: str, factor: float) -> Dict[str, Any]:
    """Set scaling factor for a variable.

    Args:
        session_id: Session containing the unit
        unit_id: Unit containing the variable
        var_name: Variable name
        factor: Scaling factor (e.g., 1e12 for A_comp)

    Returns:
        Updated scaling factors for the unit
    """
    validation_error = _input_validation_error(SetScalingFactorInput, session_id=session_id, unit_id=unit_id, var_name=var_name, factor=factor)
    if validation_error is not None:
        return validation_error
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    blocked = _guard_solver_mutation_allowed(session, 'set_scaling_factor')
    if blocked:
        return blocked
    try:
        session.set_scaling_factor(unit_id, var_name, factor)
    except KeyError as e:
        return {'error': str(e)}
    session_manager.save(session)
    return {'session_id': session_id, 'unit_id': unit_id, 'set': {var_name: factor}, 'all_scaling_factors': session.units[unit_id].scaling_factors}

def apply_scaling(session_id: str, unit_id: str, var_name: str, factor: float) -> Dict[str, Any]:
    """Apply a scaling factor to a variable (alias for set_scaling_factor).

    Args:
        session_id: Session containing the unit
        unit_id: Unit containing the variable
        var_name: Variable name
        factor: Scaling factor (e.g., 1e12 for A_comp)

    Returns:
        Updated scaling factors for the unit
    """
    return set_scaling_factor(session_id, unit_id, var_name, factor)

def calculate_scaling_factors(session_id: str) -> Dict[str, Any]:
    """Calculate scaling factors using the server's upstream-aware scaling pipeline.

    Args:
        session_id: Session to scale

    Returns:
        Status and any auto-calculated factors
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    blocked = _guard_solver_mutation_allowed(session, 'calculate_scaling_factors')
    if blocked:
        return blocked
    try:
        from watertap_engine_mcp.utils.model_builder import ModelBuilder
        builder = ModelBuilder(session)
        m = builder.build()
        units = builder.get_units()
    except ImportError as e:
        return {'session_id': session_id, 'status': 'error', 'error': f'WaterTAP/IDAES not available: {e}'}
    except Exception as e:
        return {'session_id': session_id, 'status': 'error', 'error': f'Model build failed: {e}'}
    try:
        from watertap_engine_mcp.solver.scaler import ScalingTools
        scaler = ScalingTools(m)
        scaling_ok = scaler.calculate_scaling_factors(m, units=units)
    except Exception as e:
        return {'session_id': session_id, 'status': 'error', 'error': f'Scaling calculation failed: {e}'}
    applications = scaler.get_last_scaling_applications()
    if not scaling_ok:
        return {'session_id': session_id, 'status': 'error', 'scaling_applications': applications, 'error': 'Scaling calculation failed'}
    return {'session_id': session_id, 'status': 'Scaling factors calculated', 'scaling_applications': applications, 'note': 'Call report_scaling_issues to identify remaining problems'}

def report_scaling_issues(session_id: str) -> Dict[str, Any]:
    """Report unscaled or badly-scaled variables/constraints.

    This calls iscale.report_scaling_issues() to identify problems.

    Args:
        session_id: Session to analyze

    Returns:
        Lists of unscaled and badly-scaled variables/constraints
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    try:
        from watertap_engine_mcp.utils.model_builder import ModelBuilder
        builder = ModelBuilder(session)
        m = builder.build()
    except ImportError as e:
        return {'session_id': session_id, 'error': f'WaterTAP/IDAES not available: {e}'}
    except Exception as e:
        return {'session_id': session_id, 'error': f'Model build failed: {e}'}
    unscaled_vars = []
    badly_scaled_vars = []
    unscaled_cons = []
    badly_scaled_cons = []
    try:
        import idaes.core.util.scaling as iscale
        from io import StringIO
        import sys
        old_stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            iscale.report_scaling_issues(m)
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout
        lines = output.strip().split('\n')
        current_section = None
        for line in lines:
            line = line.strip()
            if 'Unscaled Variable' in line:
                current_section = 'unscaled_vars'
            elif 'Badly Scaled Variable' in line:
                current_section = 'badly_scaled_vars'
            elif 'Unscaled Constraint' in line:
                current_section = 'unscaled_cons'
            elif 'Badly Scaled Constraint' in line:
                current_section = 'badly_scaled_cons'
            elif line and current_section and (not line.startswith('=')):
                if current_section == 'unscaled_vars':
                    unscaled_vars.append(line)
                elif current_section == 'badly_scaled_vars':
                    badly_scaled_vars.append(line)
                elif current_section == 'unscaled_cons':
                    unscaled_cons.append(line)
                elif current_section == 'badly_scaled_cons':
                    badly_scaled_cons.append(line)
    except ImportError:
        return {'session_id': session_id, 'error': 'IDAES scaling utilities not available'}
    except Exception as e:
        return {'session_id': session_id, 'error': f'Scaling analysis failed: {e}'}
    return {'session_id': session_id, 'unscaled_variables': unscaled_vars[:20], 'badly_scaled_variables': badly_scaled_vars[:20], 'unscaled_constraints': unscaled_cons[:20], 'badly_scaled_constraints': badly_scaled_cons[:20], 'total_unscaled_vars': len(unscaled_vars), 'total_badly_scaled_vars': len(badly_scaled_vars), 'total_unscaled_cons': len(unscaled_cons), 'total_badly_scaled_cons': len(badly_scaled_cons)}

def autoscale_large_jac(session_id: str) -> Dict[str, Any]:
    """Apply Jacobian-based auto-scaling to remaining unscaled constraints.

    Uses IDAES constraint_autoscale_large_jac to identify and fix
    remaining scaling issues based on Jacobian analysis. This is typically
    run after calculate_scaling_factors to handle edge cases.

    Args:
        session_id: Session to scale

    Returns:
        Dict with scaling status and any issues found
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    try:
        from watertap_engine_mcp.utils.model_builder import ModelBuilder
        import idaes.core.util.scaling as iscale
        builder = ModelBuilder(session)
        model = builder.build()
        import io
        import sys
        old_stdout = sys.stdout
        sys.stdout = buffer_before = io.StringIO()
        try:
            iscale.report_scaling_issues(model)
        except Exception:
            pass
        sys.stdout = old_stdout
        issues_before = buffer_before.getvalue().count('\n')
        try:
            iscale.constraint_autoscale_large_jac(model)
            autoscale_applied = True
        except Exception as e:
            autoscale_applied = False
            autoscale_error = str(e)
        sys.stdout = buffer_after = io.StringIO()
        try:
            iscale.report_scaling_issues(model)
        except Exception:
            pass
        sys.stdout = old_stdout
        issues_after = buffer_after.getvalue().count('\n')
        if not autoscale_applied:
            return {'session_id': session_id, 'status': 'failed', 'error': f'Jacobian autoscaling failed: {autoscale_error}'}
        return {'session_id': session_id, 'status': 'success', 'issues_before': issues_before, 'issues_after': issues_after, 'issues_resolved': max(0, issues_before - issues_after), 'message': f'Jacobian-based autoscaling applied. Issues: {issues_before} → {issues_after}'}
    except ImportError:
        return {'session_id': session_id, 'error': 'IDAES scaling utilities not available'}
    except Exception as e:
        return {'session_id': session_id, 'error': f'Autoscaling failed: {e}'}

def initialize_unit(session_id: str, unit_id: str, state_args: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
    """Initialize a single unit.

    Args:
        session_id: Session containing the unit
        unit_id: Unit to initialize
        state_args: Optional state_args for initialization

    Returns:
        Initialization status and DOF after init
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    blocked = _guard_solver_mutation_allowed(session, 'initialize_unit')
    if blocked:
        return blocked
    if unit_id not in session.units:
        return {'error': f"Unit '{unit_id}' not found"}
    try:
        from watertap_engine_mcp.utils.model_builder import ModelBuilder
        builder = ModelBuilder(session)
        builder.build()
        units = builder.get_units()
    except ImportError as e:
        return {'session_id': session_id, 'unit_id': unit_id, 'status': 'error', 'error': f'WaterTAP/IDAES not available: {e}'}
    except Exception as e:
        return {'session_id': session_id, 'unit_id': unit_id, 'status': 'error', 'error': f'Model build failed: {e}'}
    unit_block = units.get(unit_id)
    if unit_block is None:
        return {'session_id': session_id, 'unit_id': unit_id, 'status': 'error', 'error': f"Unit '{unit_id}' not found in built model"}
    try:
        from idaes.core.util.model_statistics import degrees_of_freedom
        if hasattr(unit_block, 'initialize_build'):
            unit_block.initialize_build(state_args=state_args)
        elif hasattr(unit_block, 'initialize'):
            unit_block.initialize(state_args=state_args)
        else:
            return {'session_id': session_id, 'unit_id': unit_id, 'status': 'warning', 'message': 'Unit has no initialize method'}
        dof = degrees_of_freedom(unit_block)
    except Exception as e:
        return {'session_id': session_id, 'unit_id': unit_id, 'status': 'error', 'error': f'Initialization failed: {e}'}
    return {'session_id': session_id, 'unit_id': unit_id, 'status': 'initialized', 'dof_after_init': dof}

def get_initialization_order(session_id: str, tear_streams: Optional[List[str]]=None, use_sequential_decomposition: bool=True) -> Dict[str, Any]:
    """Get initialization order for flowsheet units using IDAES SequentialDecomposition.

    Uses WaterTAP standard approach (IDAES SequentialDecomposition) when a model
    can be built. Falls back to session-only topological sort for planning.
    Fails loudly if SequentialDecomposition is unavailable with a built model.

    Args:
        session_id: Session to analyze
        tear_streams: Optional list of arc names to treat as tear streams
        use_sequential_decomposition: If True (default), build model and use IDAES

    Returns:
        Ordered list of unit IDs for initialization
    """
    from watertap_engine_mcp.utils.topo_sort import compute_initialization_order, get_sequential_decomposition_order, SequentialDecompositionError
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    unit_ids = list(session.units.keys())
    if not unit_ids:
        return {'session_id': session_id, 'initialization_order': [], 'message': 'No units in session'}
    tear_pairs = None
    if tear_streams:
        tear_pairs = []
        for arc_name in tear_streams:
            if arc_name.startswith('arc_'):
                parts = arc_name[4:].rsplit('_', 1)
                if len(parts) == 2:
                    tear_pairs.append((parts[0], parts[1]))
    if use_sequential_decomposition:
        try:
            from watertap_engine_mcp.utils.model_builder import ModelBuilder
            builder = ModelBuilder(session)
            model = builder.build()
            order = get_sequential_decomposition_order(model, tear_pairs)
            return {'session_id': session_id, 'initialization_order': order, 'method': 'IDAES_SequentialDecomposition', 'tear_streams': tear_streams or [], 'message': 'Order determined using IDAES SequentialDecomposition'}
        except SequentialDecompositionError as e:
            return {'session_id': session_id, 'error': str(e), 'initialization_order': [], 'method': 'FAILED', 'message': 'IDAES SequentialDecomposition failed. This is the WaterTAP standard approach.'}
        except Exception as e:
            return {'session_id': session_id, 'error': f'Model build failed: {e}', 'initialization_order': [], 'method': 'FAILED', 'message': 'Could not build model for SequentialDecomposition'}
    connections = [{'src_unit': c.source_unit, 'src_port': c.source_port, 'dest_unit': c.dest_unit, 'dest_port': c.dest_port} for c in session.connections]
    try:
        order = compute_initialization_order(units={uid: None for uid in unit_ids}, connections=connections, tear_streams=tear_pairs, model=None)
        return {'session_id': session_id, 'initialization_order': order, 'method': 'session_planning', 'tear_streams': tear_streams or [], 'message': 'Order estimated from session (model not built). Use IDAES SequentialDecomposition for actual initialization.'}
    except SequentialDecompositionError as e:
        return {'session_id': session_id, 'error': str(e), 'initialization_order': [], 'method': 'FAILED', 'message': str(e)}

def check_dof(session_id: str) -> Dict[str, Any]:
    """Check degrees of freedom on REAL built model.

    Builds the Pyomo model and runs IDAES degrees_of_freedom on each unit
    and the overall flowsheet. This is the same live-Pyomo DOF source used by
    get_dof_status.

    Args:
        session_id: Session to check

    Returns:
        DOF count per unit and overall, with status (ready/under/over specified)
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return redact_public_payload({'error': f"Session '{session_id}' not found"})
    payload = _build_preflight_payload(session_id, session)
    if not payload['build_success']:
        return redact_public_payload({'session_id': session_id, 'status': 'error', 'error': '; '.join(payload['issues']) or 'DOF check failed'})
    total_dof = payload.get('total_dof', 0)
    status = payload['status']
    message = dof_status_message(status=status, total_dof=total_dof, feed_units_without_state=payload.get('feed_units_without_state', []))
    return redact_public_payload({'session_id': session_id, 'total_dof': total_dof, 'status': status, 'message': message, 'unit_dof': payload['unit_dof'], 'live_dof_requirements': payload.get('live_dof_requirements', {}), 'issues': payload['issues'], 'issue_codes': payload.get('issue_codes', []), 'feed_units_without_state': payload['feed_units_without_state'], 'minimum_viable_flowsheet': payload.get('minimum_viable_flowsheet')})

def get_dof_completion_plan(session_id: str, unit_id: Optional[str]=None) -> Dict[str, Any]:
    """Return server-generated guidance for closing live model DOF.

    This reports registered engineering inputs plus live unfixed model
    variables for dynamically constructed topologies that do not yet have a
    curated unit-specific sizing contract.
    """
    try:
        session = _load_session(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    payload = _build_preflight_payload(session_id, session, unit_id=unit_id)
    return dof_completion_plan_from_preflight(session_id, payload)

def initialize_flowsheet(session_id: str, tear_streams: Optional[List[str]]=None) -> Dict[str, Any]:
    """Initialize entire flowsheet using IDAES SequentialDecomposition order.

    Uses WaterTAP standard initialization approach:
    1. Build Pyomo model
    2. Get initialization order via IDAES SequentialDecomposition
    3. Initialize each unit using its appropriate method
    4. Fails loudly if SequentialDecomposition is unavailable

    Args:
        session_id: Session to initialize
        tear_streams: Optional list of tear stream names for recycles (format: ["src_unit:dest_unit"])

    Returns:
        Initialization status per unit
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    try:
        from watertap_engine_mcp.utils.model_builder import ModelBuilder
        builder = ModelBuilder(session)
        m = builder.build()
        units = builder.get_units()
    except ImportError as e:
        return {'session_id': session_id, 'status': 'error', 'error': f'WaterTAP/IDAES not available: {e}'}
    except Exception as e:
        return {'session_id': session_id, 'status': 'error', 'error': f'Model build failed: {e}'}
    tear_stream_tuples = None
    if tear_streams:
        tear_stream_tuples = []
        for ts in tear_streams:
            if ':' in ts:
                src, dst = ts.split(':', 1)
                tear_stream_tuples.append((src.strip(), dst.strip()))
    init_order = []
    init_method = 'IDAES_SequentialDecomposition'
    try:
        from watertap_engine_mcp.utils.topo_sort import compute_initialization_order, SequentialDecompositionError
        connections = [{'src_unit': conn.source_unit, 'src_port': conn.source_port, 'dest_unit': conn.dest_unit, 'dest_port': conn.dest_port} for conn in session.connections]
        init_order = compute_initialization_order(units={uid: units.get(uid) for uid in session.units.keys()}, connections=connections, tear_streams=tear_stream_tuples, model=m)
    except SequentialDecompositionError as e:
        return {'session_id': session_id, 'status': 'error', 'error': f'IDAES SequentialDecomposition failed: {e}. This is the WaterTAP standard approach - cannot proceed.', 'method': 'FAILED'}
    except Exception as e:
        return {'session_id': session_id, 'status': 'error', 'error': f'Failed to compute initialization order: {e}', 'method': 'FAILED'}
    status_per_unit = {}
    overall_status = 'success'
    try:
        from idaes.core.util.model_statistics import degrees_of_freedom
        for unit_id in init_order:
            unit_block = units.get(unit_id)
            if unit_block is None:
                status_per_unit[unit_id] = 'not_found'
                continue
            try:
                if hasattr(unit_block, 'initialize_build'):
                    unit_block.initialize_build()
                elif hasattr(unit_block, 'initialize'):
                    unit_block.initialize()
                dof = degrees_of_freedom(unit_block)
                status_per_unit[unit_id] = f'initialized (DOF={dof})'
            except Exception as e:
                status_per_unit[unit_id] = f'failed: {str(e)[:50]}'
                overall_status = 'partial'
    except Exception as e:
        return {'session_id': session_id, 'status': 'error', 'error': f'Initialization failed: {e}'}
    return {'session_id': session_id, 'initialization_order': init_order, 'status_per_unit': status_per_unit, 'overall_status': overall_status, 'method': init_method, 'tear_streams': tear_streams or []}

def propagate_state(session_id: str, source_port: str, dest_port: str) -> Dict[str, Any]:
    """Propagate state from one port to another.

    Args:
        session_id: Session containing the ports
        source_port: Source port (format: "unit_id.port_name")
        dest_port: Destination port (format: "unit_id.port_name")

    Returns:
        Propagation status
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    blocked = _guard_solver_mutation_allowed(session, 'propagate_state')
    if blocked:
        return blocked
    try:
        src_unit_id, src_port_name = source_port.split('.', 1)
        dst_unit_id, dst_port_name = dest_port.split('.', 1)
    except ValueError:
        return {'session_id': session_id, 'error': "Port format must be 'unit_id.port_name'"}
    try:
        from watertap_engine_mcp.utils.model_builder import ModelBuilder
        builder = ModelBuilder(session)
        builder.build()
        units = builder.get_units()
    except ImportError as e:
        return {'session_id': session_id, 'status': 'error', 'error': f'WaterTAP/IDAES not available: {e}'}
    except Exception as e:
        return {'session_id': session_id, 'status': 'error', 'error': f'Model build failed: {e}'}
    src_unit = units.get(src_unit_id)
    dst_unit = units.get(dst_unit_id)
    if src_unit is None:
        return {'error': f"Source unit '{src_unit_id}' not found"}
    if dst_unit is None:
        return {'error': f"Destination unit '{dst_unit_id}' not found"}
    src_port_obj = getattr(src_unit, src_port_name, None)
    dst_port_obj = getattr(dst_unit, dst_port_name, None)
    if src_port_obj is None:
        return {'error': f"Source port '{src_port_name}' not found on '{src_unit_id}'"}
    if dst_port_obj is None:
        return {'error': f"Destination port '{dst_port_name}' not found on '{dst_unit_id}'"}
    try:
        from idaes.core.util.initialization import propagate_state as idaes_propagate
        idaes_propagate(arc=None, direction='forward', source=src_port_obj, destination=dst_port_obj)
    except ImportError:
        try:
            from watertap.core.util.initialization import propagate_state as wt_propagate
            wt_propagate(arc=None, direction='forward', source=src_port_obj, destination=dst_port_obj)
        except ImportError:
            return {'session_id': session_id, 'status': 'error', 'error': 'propagate_state utility not available'}
    except Exception as e:
        return {'session_id': session_id, 'status': 'error', 'error': f'State propagation failed: {e}'}
    return {'session_id': session_id, 'propagated': f'{source_port} → {dest_port}', 'status': 'success'}

def check_solve(session_id: str, unit_id: Optional[str]=None) -> Dict[str, Any]:
    """Run the authoritative live preflight check for solve readiness.

    Args:
        session_id: Session to check
        unit_id: Optional specific unit to check

    Returns:
        Build-based readiness status with live DOF and missing-spec details
    """
    try:
        session = _load_session(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    payload = _build_preflight_payload(session_id, session, unit_id=unit_id)
    if not session.connections and len(session.units) > 1:
        payload['issues'].append('Multiple units but no connections')
        payload['ready_to_solve'] = False
        if payload['status'] == 'ready':
            payload['status'] = 'underspecified'
    return payload

def solve(session_id: str, solver: Optional[str]=None, solver_options: Optional[Dict[str, Any]]=None, project_ref: Optional[str]=None, plant_slug: Optional[str]=None, workflow_run_id: Optional[str]=None, mcp_outputs_dir: Optional[str]=None, max_wall_time_s: int=300, idempotency_key: Optional[str]=None) -> Dict[str, Any]:
    """Submit the canonical full hygiene solve pipeline.

    `solve` is the default high-level workflow for MCP clients and CLI users.
    The submitted background job owns the repetitive solver-hygiene steps for
    supported flowsheets:
    1. Build validation
    2. DOF check
    3. Scaling and initialization
    4. Diagnostics and solve
    5. Result extraction and persistence

    Expert users can still call the atomic actions directly, but `solve`
    is the primary entrypoint for intent-level execution.

    Args:
        session_id: Session to solve
        solver: Solver name (default: ipopt)
        solver_options: Solver-specific options

    Returns:
        Job metadata for polling with `get_solve_status` / `get_solve_results`
    """
    return redact_public_payload(build_and_solve(session_id=session_id, solver=solver, solver_options=solver_options, project_ref=project_ref, plant_slug=plant_slug, workflow_run_id=workflow_run_id, mcp_outputs_dir=mcp_outputs_dir, max_wall_time_s=max_wall_time_s, idempotency_key=idempotency_key))

def get_solve_status(job_id: str) -> Dict[str, Any]:
    """Get status of a solve job.

    Args:
        job_id: Job ID from solve()

    Returns:
        Job status, progress, and results if complete
    """
    job = job_manager.get_status(job_id)
    if not job:
        return redact_public_payload({'error': f"Job '{job_id}' not found"})
    return redact_public_payload({'job_id': job_id, 'status': job.status.value, 'progress': job.progress, 'message': job.message, 'result': job.result if job.status == JobStatus.COMPLETED else None, 'failure_reason': job.error if job.status == JobStatus.FAILED else None})

def run_diagnostics(session_id: str) -> Dict[str, Any]:
    """Run comprehensive diagnostics on the flowsheet.

    Uses IDAES DiagnosticsToolbox to identify structural and
    numerical issues.

    Args:
        session_id: Session to diagnose

    Returns:
        Diagnostic results including structural and numerical issues
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    diagnostics = {'session_id': session_id, 'structural_issues': [], 'numerical_issues': [], 'session_checks': {}}
    unit_dof = {}
    for unit_id, unit_inst in session.units.items():
        fixed_count = len(unit_inst.fixed_vars)
        unit_dof[unit_id] = {'fixed_vars': fixed_count}
    diagnostics['session_checks']['total_fixed_vars'] = sum((len(u.fixed_vars) for u in session.units.values()))
    diagnostics['session_checks']['units'] = unit_dof
    diagnostics['session_checks']['connections'] = len(session.connections)
    try:
        from watertap_engine_mcp.utils.model_builder import ModelBuilder
        builder = ModelBuilder(session)
        m = builder.build()
    except ImportError as e:
        diagnostics['note'] = f'WaterTAP/IDAES not available: {e}'
        return diagnostics
    except Exception as e:
        diagnostics['note'] = f'Model build failed: {e}'
        return diagnostics
    try:
        from idaes.core.util.diagnostics_tools.diagnostics_toolbox import DiagnosticsToolbox
        from idaes.core.util.model_statistics import degrees_of_freedom
        from io import StringIO
        import sys
        dt = DiagnosticsToolbox(m)
        old_stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            dt.report_structural_issues()
            structural_output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout
        sys.stdout = StringIO()
        try:
            dt.report_numerical_issues()
            numerical_output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout
        structural_lines = [l.strip() for l in structural_output.split('\n') if l.strip() and (not l.startswith('=')) and (not l.startswith('-'))]
        numerical_lines = [l.strip() for l in numerical_output.split('\n') if l.strip() and (not l.startswith('=')) and (not l.startswith('-'))]
        diagnostics['structural_issues'] = structural_lines[:20]
        diagnostics['numerical_issues'] = numerical_lines[:20]
        dof = degrees_of_freedom(m)
        diagnostics['degrees_of_freedom'] = dof
        if dof != 0:
            diagnostics['structural_issues'].insert(0, f'Degrees of freedom: {dof} (should be 0)')
    except ImportError:
        diagnostics['note'] = 'IDAES DiagnosticsToolbox not available'
    except Exception as e:
        diagnostics['note'] = f'Diagnostics failed: {str(e)[:100]}'
    return diagnostics

def get_constraint_residuals(session_id: str, threshold: float=1e-06, max_results: int=20) -> Dict[str, Any]:
    """Get constraints with largest residuals.

    Args:
        session_id: Session to analyze
        threshold: Only report residuals above this value
        max_results: Maximum number of results to return

    Returns:
        List of constraints with large residuals
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    try:
        from watertap_engine_mcp.utils.model_builder import ModelBuilder
        builder = ModelBuilder(session)
        m = builder.build()
    except ImportError as e:
        return {'session_id': session_id, 'error': f'WaterTAP/IDAES not available: {e}'}
    except Exception as e:
        return {'session_id': session_id, 'error': f'Model build failed: {e}'}
    residuals = []
    try:
        from pyomo.environ import Constraint, value
        for c in m.component_data_objects(Constraint, active=True):
            try:
                body_val = value(c.body, exception=False)
                if body_val is None:
                    continue
                lb = value(c.lower, exception=False)
                ub = value(c.upper, exception=False)
                residual = 0.0
                if lb is not None and body_val < lb:
                    residual = abs(lb - body_val)
                elif ub is not None and body_val > ub:
                    residual = abs(body_val - ub)
                if residual > threshold:
                    residuals.append({'constraint': str(c), 'residual': residual, 'body_value': body_val})
            except Exception:
                continue
        residuals.sort(key=lambda x: x['residual'], reverse=True)
        residuals = residuals[:max_results]
    except Exception as e:
        return {'session_id': session_id, 'error': f'Residual calculation failed: {e}'}
    return {'session_id': session_id, 'threshold': threshold, 'constraint_residuals': residuals, 'count': len(residuals)}

def get_bound_violations(session_id: str, tolerance: float=1e-08, max_results: int=20) -> Dict[str, Any]:
    """Get variables violating their bounds.

    Args:
        session_id: Session to analyze
        tolerance: Tolerance for bound violations
        max_results: Maximum number of results

    Returns:
        List of bound violations
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    try:
        from watertap_engine_mcp.utils.model_builder import ModelBuilder
        builder = ModelBuilder(session)
        m = builder.build()
    except ImportError as e:
        return {'session_id': session_id, 'error': f'WaterTAP/IDAES not available: {e}'}
    except Exception as e:
        return {'session_id': session_id, 'error': f'Model build failed: {e}'}
    violations = []
    try:
        from pyomo.environ import Var, value
        for v in m.component_data_objects(Var, active=True):
            try:
                val = value(v, exception=False)
                if val is None:
                    continue
                lb = value(v.lb, exception=False) if v.lb is not None else None
                ub = value(v.ub, exception=False) if v.ub is not None else None
                violation = None
                violation_type = None
                if lb is not None and val < lb - tolerance:
                    violation = lb - val
                    violation_type = 'below_lower'
                elif ub is not None and val > ub + tolerance:
                    violation = val - ub
                    violation_type = 'above_upper'
                if violation is not None:
                    violations.append({'variable': str(v), 'value': val, 'lower_bound': lb, 'upper_bound': ub, 'violation': violation, 'type': violation_type})
            except Exception:
                continue
        violations.sort(key=lambda x: abs(x['violation']), reverse=True)
        violations = violations[:max_results]
    except Exception as e:
        return {'session_id': session_id, 'error': f'Bound violation check failed: {e}'}
    return {'session_id': session_id, 'tolerance': tolerance, 'bound_violations': violations, 'count': len(violations)}

def diagnose_failure(session_id: str, termination_condition: Optional[str]=None) -> Dict[str, Any]:
    """Diagnose a solver failure and suggest fixes.

    Args:
        session_id: Session that failed to solve
        termination_condition: Optional solver termination condition. If omitted,
            the action inspects persisted session results.

    Returns:
        Diagnosis with likely causes and suggested fixes
    """
    try:
        session = _load_session(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    if termination_condition is None:
        termination_condition = (session.results or {}).get('termination_condition') or (session.results or {}).get('details', {}).get('termination_condition') or session.solve_message
        if not termination_condition:
            return {'error': 'No persisted termination condition found. Pass termination_condition explicitly or use get_last_failure_diagnostics after a failed solve.'}
    diagnosis = {'session_id': session_id, 'termination_condition': termination_condition, 'likely_causes': [], 'suggested_fixes': [], 'constraint_residuals': [], 'bound_violations': []}
    normalized = str(termination_condition).lower()
    if termination_condition == 'infeasible':
        diagnosis['likely_causes'].append('No feasible solution exists with current constraints and bounds')
        diagnosis['suggested_fixes'].extend(['Check constraint residuals to identify problematic constraints', 'Verify feed pressure is above osmotic pressure for RO', 'Check that operating conditions are physically achievable'])
    elif termination_condition == 'maxIterations':
        diagnosis['likely_causes'].append('Solver hit iteration limit - likely poor scaling or bad initialization')
        diagnosis['suggested_fixes'].extend(['Run calculate_scaling_factors and report_scaling_issues', 'Scale small parameters: A_comp (1e12), B_comp (1e8)', 'Initialize units sequentially with propagate_state'])
    elif termination_condition == 'locallyInfeasible':
        diagnosis['likely_causes'].append('Local minimum is infeasible - bad initial point')
        diagnosis['suggested_fixes'].extend(['Try different initial values', 'Initialize from a known feasible solution'])
    elif termination_condition == 'unbounded':
        diagnosis['likely_causes'].append('Variables going to infinity - missing constraints')
        diagnosis['suggested_fixes'].extend(['Check that all required variables are fixed', 'Verify DOF = 0 before solving'])
    elif 'underspecified' in normalized or 'dof' in normalized:
        diagnosis['likely_causes'].append('The flowsheet is not fully specified and still has unresolved degrees of freedom')
        diagnosis['suggested_fixes'].extend(['Run solve check to inspect unresolved required fixes and live unfixed variables', 'Fix the missing sizing/operating variables on the affected units', 'For zero-order units, load database parameters before solving'])
    return diagnosis

def get_last_failure_diagnostics(session_id: Optional[str]=None, job_id: Optional[str]=None) -> Dict[str, Any]:
    """Inspect the latest failed session/job and return failure diagnostics."""
    if not session_id and (not job_id):
        return {'error': 'Provide session_id or job_id'}
    job_payload = None
    if job_id:
        job = job_manager.get_status(job_id)
        if not job:
            return {'error': f"Job '{job_id}' not found"}
        job_payload = {'job_id': job_id, 'status': job.status.value, 'message': job.message, 'error': job.error, 'result': job.result}
        session_id = session_id or job.session_id
    if not session_id:
        return {'error': 'Could not resolve a session_id from the failed job'}
    try:
        session = _load_session(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    failure_result = session.results or {}
    termination_condition = failure_result.get('termination_condition') or failure_result.get('details', {}).get('termination_condition')
    if termination_condition is None and session.solve_message:
        solve_message = session.solve_message.lower()
        if 'infeasible' in solve_message:
            termination_condition = 'infeasible'
        elif 'max' in solve_message and 'iteration' in solve_message:
            termination_condition = 'maxIterations'
        elif 'underspecified' in solve_message or 'dof' in solve_message:
            termination_condition = 'underspecified'
    diagnosis = diagnose_failure(session_id=session_id, termination_condition=termination_condition)
    if 'error' in diagnosis:
        return diagnosis
    diagnosis['job'] = job_payload
    diagnosis['session_status'] = session.status.value
    diagnosis['solve_message'] = session.solve_message
    diagnosis['raw_results'] = failure_result
    return diagnosis

def load_zo_parameters(session_id: str, unit_id: str, database: str='default', process_subtype: Optional[str]=None) -> Dict[str, Any]:
    """Load parameters from WaterTAP database for zero-order unit.

    Args:
        session_id: Session containing the unit
        unit_id: Zero-order unit to configure
        database: Database name or path
        process_subtype: Process subtype for parameter lookup

    Returns:
        Loaded parameters
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    if unit_id not in session.units:
        return {'error': f"Unit '{unit_id}' not found"}
    unit_inst = session.units[unit_id]
    if not unit_inst.unit_type.endswith('ZO'):
        return {'error': f"Unit '{unit_id}' is not a zero-order unit (type: {unit_inst.unit_type})"}
    try:
        from watertap_engine_mcp.utils.model_builder import ModelBuilder
        builder = ModelBuilder(session)
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            builder.build()
        units = builder.get_units()
    except ImportError as e:
        return {'session_id': session_id, 'unit_id': unit_id, 'error': f'WaterTAP/IDAES not available: {e}'}
    except Exception as e:
        return {'session_id': session_id, 'unit_id': unit_id, 'error': f'Model build failed: {e}'}
    unit_block = units.get(unit_id)
    if unit_block is None:
        return {'session_id': session_id, 'unit_id': unit_id, 'error': f"Unit '{unit_id}' not found in built model"}
    parameters_loaded = {}
    try:
        if hasattr(unit_block, 'load_parameters_from_database'):
            try:
                from watertap.core.wt_database import Database
                db = Database() if database == 'default' else Database(database)
                unit_block.config.database = db
            except ImportError:
                return {'session_id': session_id, 'unit_id': unit_id, 'error': 'WaterTAP database not available'}
            if process_subtype:
                unit_block.config.process_subtype = process_subtype
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                unit_block.load_parameters_from_database(use_default_removal=True)
            from pyomo.environ import value
            param_names = ['removal_frac_mass_comp', 'recovery_frac_mass_H2O', 'energy_electric_flow_vol_inlet', 'electricity']
            for pname in param_names:
                param = getattr(unit_block, pname, None)
                if param is not None:
                    try:
                        if hasattr(param, '__iter__'):
                            for idx in param:
                                val = value(param[idx], exception=False)
                                if val is not None:
                                    parameters_loaded[f'{pname}[{idx}]'] = val
                        else:
                            val = value(param, exception=False)
                            if val is not None:
                                parameters_loaded[pname] = val
                    except Exception:
                        pass
        else:
            return {'session_id': session_id, 'unit_id': unit_id, 'error': 'Unit does not support load_parameters_from_database'}
    except Exception as e:
        return {'session_id': session_id, 'unit_id': unit_id, 'error': f'Failed to load parameters: {e}'}
    unit_inst.zo_config['database'] = database
    unit_inst.zo_config['process_subtype'] = process_subtype
    unit_inst.zo_config['parameters_loaded'] = parameters_loaded
    session_manager.save(session)
    return {'session_id': session_id, 'unit_id': unit_id, 'database': database, 'process_subtype': process_subtype, 'parameters_loaded': parameters_loaded, 'persisted': True, 'success': True}

def list_zo_databases() -> List[Dict[str, Any]]:
    """List available zero-order parameter databases.

    Returns:
        List of available databases with water source types
    """
    databases = []
    try:
        from pathlib import Path
        try:
            import watertap
            watertap_path = Path(watertap.__file__).parent
            data_path = watertap_path / 'data' / 'techno_economic'
            if data_path.exists():
                for f in data_path.glob('*.yaml'):
                    databases.append({'name': f.stem, 'path': str(f), 'description': f'WaterTAP database: {f.stem}'})
        except Exception:
            pass
        databases.insert(0, {'name': 'default', 'description': 'Default WaterTAP zero-order database', 'water_sources': ['municipal', 'industrial', 'seawater', 'surface_water', 'groundwater']})
    except ImportError:
        databases = [{'name': 'default', 'description': 'Default WaterTAP zero-order database', 'water_sources': ['municipal', 'industrial', 'seawater']}]
    return databases

def list_zo_process_subtypes(unit_type: str, database: str='default') -> Dict[str, Any]:
    """List available process subtypes for a zero-order technology."""
    try:
        from watertap.core.wt_database import Database
        db = Database(None if database == 'default' else database)
        unit_key = unit_type.replace('ZO', '').lower()
        tech_data = db._get_technology(unit_key)
        subtypes = sorted((key for key in tech_data.keys() if key not in {'default', 'metadata'}))
        return {'unit_type': unit_type, 'database': database, 'process_subtypes': subtypes}
    except Exception as exc:
        return {'unit_type': unit_type, 'database': database, 'error': f'Could not enumerate process subtypes: {exc}'}

def get_zo_unit_parameters(unit_type: str, water_source: str='municipal') -> Dict[str, Any]:
    """Get database parameters for a zero-order unit type.

    Args:
        unit_type: Zero-order unit type (e.g., "NanofiltrationZO")
        water_source: Water source type

    Returns:
        Available parameters from database
    """
    try:
        from watertap.core.zero_order_base import Database as ZODatabase
        db = ZODatabase()
        unit_key = unit_type.replace('ZO', '').lower()
        params = {}
        try:
            unit_data = db.get(unit_key, {})
            if isinstance(unit_data, dict):
                source_data = unit_data.get(water_source, unit_data.get('default', {}))
                if isinstance(source_data, dict):
                    params = source_data
                else:
                    params = {'raw_data': str(source_data)[:200]}
        except Exception as e:
            params = {'lookup_error': str(e)[:100]}
        return {'unit_type': unit_type, 'water_source': water_source, 'parameters': params, 'source': 'watertap_database'}
    except ImportError:
        typical_params = {'NanofiltrationZO': {'removal_frac_mass_comp': {'default': 0.9}, 'recovery_frac_mass_H2O': {'default': 0.8}, 'energy_electric_flow_vol_inlet': {'default': 0.05}}, 'UltrafiltrationZO': {'removal_frac_mass_comp': {'default': 0.99}, 'recovery_frac_mass_H2O': {'default': 0.95}, 'energy_electric_flow_vol_inlet': {'default': 0.03}}, 'PumpZO': {'lift_height': {'default': 100}, 'eta_pump': {'default': 0.8}, 'eta_motor': {'default': 0.9}}}
        params = typical_params.get(unit_type, {'note': 'Unit type not in static database; full WaterTAP database access unavailable'})
        return {'unit_type': unit_type, 'water_source': water_source, 'parameters': params, 'source': 'static_defaults', 'note': 'Showing static typical values'}

def get_results(session_id: str) -> Dict[str, Any]:
    """Get solve results for a session.

    Args:
        session_id: Session to get results for

    Returns:
        Solve status and summary results
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    return {'session_id': session_id, 'status': session.status.value, 'solve_status': session.solve_status, 'solve_message': session.solve_message, 'results': session.results, 'certification': _flowsheet_certification(session)}

def get_solved_state(session_id: str, unit_id: Optional[str]=None) -> Dict[str, Any]:
    """Get the persisted solved-state export for the whole flowsheet or one unit."""
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    solved_state = _persisted_solved_state_payload(session, unit_id=unit_id)
    if solved_state is not None:
        return solved_state
    return {'session_id': session_id, 'source': 'unsolved_model', 'warning': 'No solved_state export is available yet. Run solve() first.'}

def get_stream_results(session_id: str, streams: Optional[List[str]]=None) -> Dict[str, Any]:
    """Get stream property tables.

    After a successful solve, returns persisted KPIs (solved values).
    Falls back to rebuilding the model if KPIs not available.

    Args:
        session_id: Session to get results for
        streams: Optional list of specific streams (unit.port format)

    Returns:
        Stream properties (flow, concentration, T, P) for each stream
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    solved_streams = _solved_stream_payload(session, streams=streams)
    if solved_streams is not None:
        return solved_streams
    if session.results and isinstance(session.results, dict) and ('kpis' in session.results):
        kpis = session.results['kpis']
        stream_kpis = kpis.get('streams', {})
        if stream_kpis:
            filtered_streams = {}
            for current_unit_id, ports in stream_kpis.items():
                for port_name, port_data in ports.items():
                    stream_key = f'{current_unit_id}.{port_name}'
                    if streams is None or stream_key in streams:
                        filtered_streams[stream_key] = port_data
            return {'session_id': session_id, 'source': 'solved', 'streams': filtered_streams}
    try:
        from watertap_engine_mcp.utils.model_builder import ModelBuilder
        builder = ModelBuilder(session)
        builder.build()
        units = builder.get_units()
    except ImportError as e:
        return {'session_id': session_id, 'error': f'WaterTAP/IDAES not available: {e}'}
    except Exception as e:
        return {'session_id': session_id, 'error': f'Model build failed: {e}'}
    try:
        stream_data = extract_flat_streams(units, stream_filter=streams)
    except Exception as e:
        return {'session_id': session_id, 'error': f'Stream extraction failed: {e}'}
    return StreamResultsEnvelope(session_id=session_id, source='unsolved_model', warning='Values are from an unsolved model. Run solve() first.', streams=stream_data, count=len(stream_data)).model_dump(exclude_none=True)

def get_unit_results(session_id: str, unit_id: str) -> Dict[str, Any]:
    """Get performance results for a specific unit.

    After a successful solve, returns persisted KPIs (solved values).
    Falls back to rebuilding the model if KPIs not available.

    Args:
        session_id: Session containing the unit
        unit_id: Unit to get results for

    Returns:
        Unit-specific performance metrics
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    if unit_id not in session.units:
        return {'error': f"Unit '{unit_id}' not found"}
    unit_inst = session.units[unit_id]
    solved_payload = _solved_unit_results_payload(session, unit_id)
    if solved_payload is not None:
        return solved_payload
    if session.results and isinstance(session.results, dict) and ('kpis' in session.results):
        kpis = session.results['kpis']
        unit_kpis = kpis.get('units', {}).get(unit_id, {})
        stream_kpis = kpis.get('streams', {}).get(unit_id, {})
        if unit_kpis or stream_kpis:
            return {'session_id': session_id, 'unit_id': unit_id, 'unit_type': unit_inst.unit_type, 'source': 'solved', 'performance': unit_kpis, 'streams': stream_kpis}
    try:
        from watertap_engine_mcp.utils.model_builder import ModelBuilder
        builder = ModelBuilder(session)
        builder.build()
        units = builder.get_units()
    except ImportError as e:
        return {'session_id': session_id, 'unit_id': unit_id, 'unit_type': unit_inst.unit_type, 'error': f'WaterTAP/IDAES not available: {e}'}
    except Exception as e:
        return {'session_id': session_id, 'unit_id': unit_id, 'unit_type': unit_inst.unit_type, 'error': f'Model build failed: {e}'}
    unit_block = units.get(unit_id)
    if unit_block is None:
        return {'session_id': session_id, 'unit_id': unit_id, 'error': f"Unit '{unit_id}' not found in built model"}
    performance = {}
    try:
        from pyomo.environ import value
        common_vars = ['work_mechanical', 'work_fluid', 'work_isentropic', 'heat_duty', 'heat', 'electricity', 'power']
        for var_name in common_vars:
            var = getattr(unit_block, var_name, None)
            if var is not None:
                try:
                    if hasattr(var, '__iter__'):
                        for idx in var:
                            val = value(var[idx], exception=False)
                            if val is not None:
                                performance[f'{var_name}[{idx}]'] = val
                    else:
                        val = value(var, exception=False)
                        if val is not None:
                            performance[var_name] = val
                except Exception:
                    pass
        ro_vars = ['recovery_vol_phase', 'recovery_mass_phase_comp', 'rejection_phase_comp', 'flux_mass_phase_comp', 'area', 'A_comp', 'B_comp', 'deltaP', 'over_pressure_ratio']
        for var_name in ro_vars:
            var = getattr(unit_block, var_name, None)
            if var is not None:
                try:
                    if hasattr(var, '__iter__'):
                        for idx in var:
                            val = value(var[idx], exception=False)
                            if val is not None:
                                performance[f'{var_name}[{idx}]'] = val
                    else:
                        val = value(var, exception=False)
                        if val is not None:
                            performance[var_name] = val
                except Exception:
                    pass
        pump_vars = ['efficiency_pump', 'efficiency_isentropic', 'control_volume', 'deltaP']
        for var_name in pump_vars:
            var = getattr(unit_block, var_name, None)
            if var is not None:
                try:
                    if hasattr(var, '__iter__'):
                        for idx in var:
                            val = value(var[idx], exception=False)
                            if val is not None:
                                performance[f'{var_name}[{idx}]'] = val
                    else:
                        val = value(var, exception=False)
                        if val is not None:
                            performance[var_name] = val
                except Exception:
                    pass
        evap_vars = ['area', 'U', 'delta_temperature', 'lmtd', 'heat_transfer']
        for var_name in evap_vars:
            var = getattr(unit_block, var_name, None)
            if var is not None:
                try:
                    val = value(var, exception=False)
                    if val is not None:
                        performance[var_name] = val
                except Exception:
                    pass
    except Exception as e:
        performance['extraction_error'] = str(e)[:100]
    return {'session_id': session_id, 'unit_id': unit_id, 'unit_type': unit_inst.unit_type, 'source': 'unsolved_model', 'warning': 'Values are from an unsolved model. Run solve() first.', 'performance': performance}

def enable_costing(session_id: str, costing_package: str='watertap', electricity_cost: Optional[float]=None, plant_lifetime: Optional[int]=None, utilization_factor: Optional[float]=None, product_stream_unit: Optional[str]=None, product_stream_port: str='permeate') -> Dict[str, Any]:
    """Enable costing for the flowsheet."""
    validation_error = _finite_validation_error('enable_costing', {'session_id': session_id, 'costing_package': costing_package, 'electricity_cost': electricity_cost, 'plant_lifetime': plant_lifetime, 'utilization_factor': utilization_factor, 'product_stream_unit': product_stream_unit, 'product_stream_port': product_stream_port})
    if validation_error is not None:
        return validation_error
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    valid_packages = ['watertap', 'zero_order']
    if costing_package not in valid_packages:
        return {'error': f"Invalid costing_package '{costing_package}'. Must be one of: {valid_packages}"}
    costing_config = {'package': costing_package, 'enabled': True}
    if electricity_cost is not None:
        costing_config['electricity_cost'] = electricity_cost
    if plant_lifetime is not None:
        costing_config['plant_lifetime'] = plant_lifetime
    if utilization_factor is not None:
        costing_config['utilization_factor'] = utilization_factor
    if product_stream_unit is not None:
        costing_config['product_stream_unit'] = product_stream_unit
        costing_config['product_stream_port'] = product_stream_port
    session.costing_config = costing_config
    session_manager.save(session)
    return {'session_id': session_id, 'costing_enabled': True, 'costing_package': costing_package, 'config': costing_config, 'note': 'Use add_unit_costing() to enable costing on specific units, then solve() to calculate costs.'}

def add_unit_costing(session_id: str, unit_id: str) -> Dict[str, Any]:
    """Enable costing for a specific unit.

    Marks a unit to have a UnitModelCostingBlock created during model build.
    The unit's costing will be aggregated into the flowsheet's total costs.

    Args:
        session_id: Session containing the unit
        unit_id: Unit to enable costing for

    Returns:
        Confirmation of costing enabled for unit
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    if unit_id not in session.units:
        return {'error': f"Unit '{unit_id}' not found in session"}
    if not session.costing_config or not session.costing_config.get('enabled'):
        return {'error': 'Flowsheet costing not enabled. Call enable_costing() first.', 'suggestion': "enable_costing(session_id, costing_package='watertap')"}
    unit_inst = session.units[unit_id]
    spec = get_unit_spec_from_registry(unit_inst.unit_type)
    costing_package = _active_costing_package(session)
    if not unit_supports_costing_package(spec, costing_package):
        return {'session_id': session_id, 'unit_id': unit_id, 'unit_type': unit_inst.unit_type, 'costing_enabled': False, 'costing_supported': False, 'costing_supported_packages': list(spec.costing.supported_packages), 'costing_source': spec.costing.source.value, 'error': f"Unit '{unit_id}' ({unit_inst.unit_type}) is not cost-supported on the pinned upstream WaterTAP stack for costing package '{costing_package}'.", 'certification': _flowsheet_certification(session)}
    unit_inst.costing_enabled = True
    session_manager.save(session)
    return {'session_id': session_id, 'unit_id': unit_id, 'costing_enabled': True, 'costing_supported': True, 'costing_supported_packages': list(spec.costing.supported_packages), 'costing_source': spec.costing.source.value, 'costing_package': session.costing_config.get('package', 'watertap'), 'certification': _flowsheet_certification(session)}

def disable_unit_costing(session_id: str, unit_id: str) -> Dict[str, Any]:
    """Disable costing for a specific unit.

    Args:
        session_id: Session containing the unit
        unit_id: Unit to disable costing for

    Returns:
        Confirmation of costing disabled for unit
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    if unit_id not in session.units:
        return {'error': f"Unit '{unit_id}' not found in session"}
    session.units[unit_id].costing_enabled = False
    session_manager.save(session)
    return {'session_id': session_id, 'unit_id': unit_id, 'costing_enabled': False}

def set_costing_parameters(session_id: str, electricity_cost: Optional[float]=None, plant_lifetime: Optional[int]=None, utilization_factor: Optional[float]=None, membrane_cost: Optional[float]=None, factor_total_investment: Optional[float]=None, product_stream_unit: Optional[str]=None, product_stream_port: Optional[str]=None) -> Dict[str, Any]:
    """Set costing parameters for the flowsheet."""
    validation_error = _finite_validation_error('set_costing_parameters', {'session_id': session_id, 'electricity_cost': electricity_cost, 'plant_lifetime': plant_lifetime, 'utilization_factor': utilization_factor, 'membrane_cost': membrane_cost, 'factor_total_investment': factor_total_investment, 'product_stream_unit': product_stream_unit, 'product_stream_port': product_stream_port})
    if validation_error is not None:
        return validation_error
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    if not session.costing_config:
        return {'error': 'Costing not enabled. Call enable_costing() first.'}
    if electricity_cost is not None:
        session.costing_config['electricity_cost'] = electricity_cost
    if plant_lifetime is not None:
        session.costing_config['plant_lifetime'] = plant_lifetime
    if utilization_factor is not None:
        session.costing_config['utilization_factor'] = utilization_factor
    if membrane_cost is not None:
        session.costing_config['membrane_cost'] = membrane_cost
    if factor_total_investment is not None:
        session.costing_config['factor_total_investment'] = factor_total_investment
    if product_stream_unit is not None:
        session.costing_config['product_stream_unit'] = product_stream_unit
    if product_stream_port is not None:
        session.costing_config['product_stream_port'] = product_stream_port
    session_manager.save(session)
    return {'session_id': session_id, 'costing_config': session.costing_config}

def list_costed_units(session_id: str) -> Dict[str, Any]:
    """List all units with costing enabled.

    Args:
        session_id: Session to check

    Returns:
        List of units with costing status
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    units_status = []
    costing_package = _active_costing_package(session)
    for unit_id, unit_inst in session.units.items():
        spec = get_unit_spec_from_registry(unit_inst.unit_type)
        units_status.append({'unit_id': unit_id, 'unit_type': unit_inst.unit_type, 'costing_enabled': unit_inst.costing_enabled, 'costing_supported': unit_supports_costing_package(spec, costing_package), 'costing_supported_packages': list(spec.costing.supported_packages), 'costing_source': spec.costing.source.value})
    return {'session_id': session_id, 'flowsheet_costing_enabled': bool(session.costing_config and session.costing_config.get('enabled')), 'costing_package': session.costing_config.get('package') if session.costing_config else None, 'units': units_status, 'costed_count': sum((1 for u in units_status if u['costing_enabled'])), 'certification': _flowsheet_certification(session)}

def compute_costing(session_id: str, product_stream_unit: Optional[str]=None, product_stream_port: str='permeate') -> Dict[str, Any]:
    """Compute costing for the flowsheet after solving.

    This tool:
    1. Builds the model with costing blocks
    2. Calls cost_process() to aggregate costs
    3. Adds LCOW calculation using the specified product stream

    Args:
        session_id: Session to compute costing for
        product_stream_unit: Unit containing product stream (auto-detected if None)
        product_stream_port: Port name for product flow (default: "permeate")

    Returns:
        Computed costing results including LCOW, CapEx, OpEx
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    if not session.costing_config or not session.costing_config.get('enabled'):
        return {'error': 'Costing not enabled. Call enable_costing() first.'}
    persisted = _persisted_costing_payload(session, product_stream_unit=product_stream_unit, product_stream_port=product_stream_port)
    if persisted is not None:
        return persisted
    try:
        results = _extract_costing_from_rebuilt_model(session, product_stream_unit=product_stream_unit, product_stream_port=product_stream_port)
    except ImportError as e:
        return {'session_id': session_id, 'error': f'WaterTAP/IDAES not available: {e}'}
    except Exception as e:
        return {'session_id': session_id, 'error': f'Model build failed: {e}'}
    if session.status.value != 'solved':
        if results.get('status') == 'complete':
            results['status'] = 'partial'
        results.setdefault('warnings', []).append('The session has not been solved yet. Aggregate CAPEX/OPEX values may be incomplete.')
    return _sync_costing_payload_certification(results)

def get_costing(session_id: str) -> Dict[str, Any]:
    """Get costing results including LCOW.

    Args:
        session_id: Session to get costing for

    Returns:
        Costing breakdown (LCOW, CapEx, OpEx) if costing configured
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    if not session.costing_config or not session.costing_config.get('enabled'):
        return {'session_id': session_id, 'status': 'error', 'error': 'Costing not enabled. Call enable_costing() first.'}
    persisted = _persisted_costing_payload(session)
    if persisted is not None:
        return persisted
    try:
        costing_data = _extract_costing_from_rebuilt_model(session)
    except ImportError as e:
        return {'session_id': session_id, 'error': f'WaterTAP/IDAES not available: {e}'}
    except Exception as e:
        return {'session_id': session_id, 'error': f'Model build failed: {e}'}
    if session.status.value != 'solved':
        if costing_data.get('status') == 'complete':
            costing_data['status'] = 'partial'
        costing_data.setdefault('warnings', []).append('The session has not been solved yet. Aggregate CAPEX/OPEX values may be incomplete.')
    return _sync_costing_payload_certification(costing_data)

def cost_equipment_list(items: List[Dict[str, Any]], costing_params: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
    """Cost a bare equipment list with WaterTAP parametric cost models (no session).

    Deterministic action: builds a throwaway flowsheet of bare unit shells,
    attaches a WaterTAP costing method per item, costs utility/chemical flows
    via register_flow_type/cost_flow, and serializes the result to an
    engineering_utils.economics CostLedger. LCOW is included when
    costing_params carries an annual_production_m3 basis.

    Args:
        items: Equipment dicts: {tag, costing_method, sizing: {name: value or
            [value, units]}, flows: [{name, rate, units, cost, cost_units}]}
        costing_params: Optional global overrides (contingency_frac_FCI,
            owners_cost_frac_FCI, electricity_cost_usd_per_kwh,
            utilization_factor, annual_production_m3, ...)

    Returns:
        {"ledger": ..., "lcow": ..., "assumptions": [...], "totals": {...}}
        or a structured error listing supported costing methods
    """
    try:
        from watertap_engine_mcp.costing.puran_costing import cost_equipment_list as _cost_equipment_list
    except ImportError as e:
        return {'error': f'WaterTAP/IDAES not available: {e}'}
    try:
        return _cost_equipment_list(items, costing_params)
    except Exception as e:
        return {'error': f'Equipment-list costing failed: {e}'}

def list_templates() -> List[Dict[str, Any]]:
    """List supported and quarantined flowsheet templates."""
    return [serialize_template_spec(spec) for spec in list_template_specs()]

def apply_template(template_name: str, session_id: Optional[str]=None, template_config: Optional[Dict[str, Any]]=None, name: str='', description: str='') -> Dict[str, Any]:
    """Create a rating session with geometry and sizing evidence from stage (b).

    template_config must include applicable geometry, sizing_reference,
    seed_bindings and sizing_outputs. Use apply_design_template for inverse design.
    """
    return _apply_template(template_name, session_id, template_config, name, description)

def apply_design_template(template_name: str, template_config: Dict[str, Any], session_id: Optional[str]=None, name: str='', description: str='') -> Dict[str, Any]:
    """Explicit inverse design for RO, MVC, crystallization or biological volume.

    This creates a design session, not a rating of stage-(b) equipment.
    RO requires recovery; MVC requires target_brine_mass_fraction. Crystallization
    requires an explicit yield and particle size. Biological optimization starts
    from a referenced stage-(b) specification and explicitly releases its volumes.
    """
    return _apply_template(template_name, session_id, template_config, name, description, _inverse_design=True)

def _apply_template(template_name, session_id=None, template_config=None, name='', description='', *, _inverse_design=False) -> Dict[str, Any]:
    """Create a new session from a supported template.

    Args:
        template_name: Template registry name, e.g. `ro_train`
        session_id: Optional explicit session identifier
        template_config: Optional template-specific configuration overrides
        name: Optional session name override
        description: Optional session description override

    Returns:
        Session metadata and the applied template summary
    """
    from watertap_engine_mcp.core.rating_geometry import EVIDENCE_KEYS, bind_native_geometry, template_geometry
    try:
        config = deepcopy(template_config or {})
        stated_reaction_config = session_reaction_config(config.pop(REACTION_CONFIG_TARGET, None))
        if _inverse_design:
            config = _inverse_design_template_config(template_name, config)
            contract = {'analysis_mode': 'inverse_design', 'template_name': template_name, **{key: deepcopy(config[key]) for key in EVIDENCE_KEYS if key in config}}
        else:
            contract = template_geometry(template_name, config)
        for key in EVIDENCE_KEYS:
            config.pop(key, None)
        if 'aerobic_stage_volumes_m3' in config:
            from watertap_engine_mcp.core.rating_geometry import RatingGeometryError
            raise RatingGeometryError(f"{template_name}: apply_template cannot represent aerobic_stage_volumes_m3 [m3]; use solve_flowsheet's direct MBR rating or a designed graph carrying the stage (b) reactors.")
        template = build_template(template_name, config)
        if getattr(getattr(template, 'config', None), 'property_package', None) == PropertyPackageType.MODIFIED_ASM2D and config.get('feed_temperature_K') is None:
            raise ModifiedAsm2dKineticsRefused(f'modified_asm2d_kinetics_refused: {template_name} template_config states no feed_temperature_K, and a ModifiedASM2d template takes its feed temperature and kinetic set from it: state feed_temperature_K (the template default {template.config.feed_temperature_K:g} K is never used; {MODIFIED_ASM2D_ROUTES_RULING_ID} OQ-C188-1/2)')
        if stated_reaction_config is not None and getattr(getattr(template, 'config', None), 'property_package', None) != PropertyPackageType.MODIFIED_ASM2D:
            raise ReactionConfigRefused(f'reaction_config_refused: {template_name} does not build ModifiedASM2d, and {REACTION_CONFIG_TARGET} states a ModifiedASM2d model-form variant (PUBLIC-RULE-539a5e932413 G3)')
    except KeyError:
        return {'error': f"Unknown template '{template_name}'"}
    except NotImplementedError as exc:
        return {'error': str(exc)}
    except ModifiedAsm2dKineticsRefused as exc:
        return {'error': str(exc), 'termination_condition': 'modified_asm2d_kinetics_refused'}
    except ReactionConfigRefused as exc:
        return {'error': str(exc), 'termination_condition': 'reaction_config_refused'}
    except Exception as exc:
        return {'error': f"Failed to build template '{template_name}': {exc}"}
    spec = template.to_session_spec()
    if not _inverse_design:
        contract = bind_native_geometry(contract, spec)
    create_result = create_session(name=name or template_name, description=description, property_package=spec['property_package'], property_package_config=spec.get('property_package_config'), session_id=session_id)
    if 'error' in create_result:
        return create_result
    try:
        session = session_manager.load(create_result['session_id'])
    except FileNotFoundError:
        return {'error': f"Failed to load created session '{create_result['session_id']}'"}
    for unit_spec in spec.get('units', []):
        session.add_unit(unit_spec['unit_id'], unit_spec['unit_type'], unit_spec.get('config', {}), role=normalize_role(unit_spec.get('role')))
    translator_conns = []
    for conn_spec in spec.get('connections', []):
        if conn_spec.get('auto_create_translator'):
            translator_conns.append(conn_spec)
        else:
            session.add_connection(conn_spec['src_unit'], conn_spec['src_port'], conn_spec['dest_unit'], conn_spec['dest_port'], tear_stream=bool(conn_spec.get('tear_stream', False)), tear_rationale=conn_spec.get('tear_rationale'))
    if translator_conns:
        session_manager.save(session)
        for conn_spec in translator_conns:
            connect_units(session_id=session.config.session_id, source_unit=conn_spec['src_unit'], source_port=conn_spec['src_port'], dest_unit=conn_spec['dest_unit'], dest_port=conn_spec['dest_port'], auto_create_translator=True)
        session = session_manager.load(session.config.session_id)
    for fix in spec.get('dof_fixes', []):
        session.fix_variable(fix['unit_id'], fix['var_name'], fix['value'])
    for scale in spec.get('scaling_factors', []):
        session.set_scaling_factor(scale['unit_id'], scale['var_name'], scale['factor'])
    for feed_unit, feed_state in spec.get('feed_states', {}).items():
        session.set_feed_state(feed_unit, feed_state)
    for process_constraint in spec.get('process_constraints', []):
        session.add_process_constraint(process_constraint)
    if spec.get('design_parameters'):
        session.config.runtime_metadata['design_parameters'] = spec['design_parameters']
    if isinstance(spec.get('metadata'), dict):
        session.config.runtime_metadata.update(spec['metadata'])
    if isinstance(spec.get('solver_options'), dict):
        session.config.runtime_metadata.setdefault('solver_options', {}).update(spec['solver_options'])
    if stated_reaction_config is not None:
        session.config.runtime_metadata[REACTION_CONFIG_TARGET] = stated_reaction_config
    session.config.runtime_metadata['template_name'] = template_name
    session.config.runtime_metadata['topology_name'] = template_name
    session.config.runtime_metadata['origin_tool'] = 'watertap-engine-mcp.apply_design_template' if _inverse_design else 'watertap-engine-mcp.apply_template'
    session.config.runtime_metadata['rating_geometry'] = contract
    session.config.runtime_metadata['analysis_mode'] = contract['analysis_mode']
    thermal_couplings = spec.get('thermal_couplings', {})
    if thermal_couplings:
        session.thermal_couplings = thermal_couplings
    session_manager.save(session)
    return {'analysis_mode': contract['analysis_mode'], 'sizing_reference': contract.get('sizing_reference'), 'template_name': template_name, 'session_id': session.config.session_id, 'property_package': spec['property_package'], 'units': len(spec.get('units', [])), 'connections': len(spec.get('connections', [])), 'dof_fixes': len(spec.get('dof_fixes', [])), 'scaling_factors': len(spec.get('scaling_factors', [])), 'feed_states': len(spec.get('feed_states', {})), 'process_constraints': len(spec.get('process_constraints', [])), 'design_parameters': spec.get('design_parameters', {}), 'initialization_order': spec.get('initialization_order', [])}

def _universal_target_float(targets: Dict[str, Any], *keys: str) -> float | None:
    """Return the first numeric design target matching a known key spelling."""
    normalized = {str(key).strip().lower().replace('-', '_'): value for key, value in (targets or {}).items()}
    for key in keys:
        value = normalized.get(key.strip().lower().replace('-', '_'))
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None

def _universal_target_text(targets: Dict[str, Any], *keys: str) -> str | None:
    """Return the first non-empty text design target matching a known key."""
    normalized = {str(key).strip().lower().replace('-', '_'): value for key, value in (targets or {}).items()}
    for key in keys:
        value = normalized.get(key.strip().lower().replace('-', '_'))
        if value is None:
            continue
        text_value = str(value).strip()
        if text_value:
            return text_value
    return None

def _universal_recycle_target(targets: Dict[str, Any], ratio_name: str) -> float | None:
    """Return a prescribed recycle ratio only when explicitly supplied."""
    nested = targets.get('recycle_ratios') if isinstance(targets, dict) else None
    search: Dict[str, Any] = dict(targets or {})
    if isinstance(nested, dict):
        search.update(nested)
    if ratio_name == 'ras':
        return _universal_target_float(search, 'ras', 'ras_q_over_qin', 'target_ras_recycle_ratio', 'ras_recycle_ratio', 'return_sludge_recycle_ratio', 'return_activated_sludge_recycle_ratio')
    return _universal_target_float(search, 'mlr', 'mlr_q_over_qin', 'target_mlr_recycle_ratio', 'mlr_recycle_ratio', 'mixed_liquor_recycle_ratio', 'internal_recycle_ratio')

def _universal_geometry_input(targets, *keys):
    """Preserve invalid supplied geometry for an actionable refusal, never a fallback."""
    normalized = {str(key).strip().lower().replace('-', '_'): value for key, value in (targets or {}).items()}
    for key in keys:
        normalized_key = key.strip().lower().replace('-', '_')
        if normalized_key in normalized:
            return normalized[normalized_key]
    return None

def _universal_reactor_volume(targets: Dict[str, Any], unit_id: str):
    volumes = targets.get('reactor_volumes_m3') if isinstance(targets, dict) else None
    if isinstance(volumes, dict):
        if unit_id in volumes:
            return volumes[unit_id]
        if unit_id.lower() in volumes:
            return volumes[unit_id.lower()]
    return _universal_geometry_input(targets, f'{unit_id.lower()}_volume_m3', f'{unit_id.lower()}_volume')
MODIFIED_ASM2D_COD_TO_VSS = 1.5686
MODIFIED_ASM2D_INERT_N_FRACTION = 0.06003

def _universal_asm2d_property_package(targets: Dict[str, Any]) -> PropertyPackageType:
    """Resolve the requested ASM2d model variant from design targets."""
    requested = None
    for key in ('property_package', 'asm2d_property_package', 'asm2d_model_variant', 'model_variant'):
        value = (targets or {}).get(key)
        if value is not None:
            requested = str(value).strip().upper().replace('-', '_')
            break
    if requested in {'MODIFIED_ASM2D', 'MODIFIEDASM2D', 'MASM2D', 'M_ASM2D'}:
        return PropertyPackageType.MODIFIED_ASM2D
    if requested in {'ASM2D', 'ASM2D_IWA'}:
        return PropertyPackageType.ASM2D
    return PropertyPackageType.ASM2D

def _universal_ro_property_package(targets: Dict[str, Any]) -> PropertyPackageType:
    """Resolve the requested RO transport property package from design targets."""
    value = None
    for key in ('property_package', 'ro_property_package', 'transport_property_package'):
        if (targets or {}).get(key) is not None:
            value = (targets or {}).get(key)
            break
    if isinstance(value, PropertyPackageType):
        return value
    if value is None:
        return PropertyPackageType.SEAWATER
    try:
        return PropertyPackageType(str(value).strip())
    except ValueError:
        pass
    normalized = str(value).strip().upper().replace('-', '_')
    if normalized in {'SEAWATER', 'NACL', 'NACL_T_DEP', 'MCAS'}:
        return PropertyPackageType[normalized]
    try:
        return PropertyPackageType(normalized)
    except ValueError:
        return PropertyPackageType.SEAWATER

def _universal_mapping_float(mapping: Mapping[str, Any], *keys: str) -> float | None:
    """Return the first numeric value matching any key spelling in a mapping."""
    normalized = {str(key).strip().lower().replace('-', '_'): value for key, value in (mapping or {}).items()}
    for key in keys:
        value = normalized.get(key.strip().lower().replace('-', '_'))
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None

def _universal_mapping_sequence(mapping: Mapping[str, Any], *keys: str) -> list[Any] | None:
    """Return the first sequence-like value matching any key spelling."""
    normalized = {str(key).strip().lower().replace('-', '_'): value for key, value in (mapping or {}).items()}
    for key in keys:
        value = normalized.get(key.strip().lower().replace('-', '_'))
        if value is None:
            continue
        if isinstance(value, str):
            return [item.strip() for item in value.split(',') if item.strip()]
        if isinstance(value, Sequence) and (not isinstance(value, (bytes, bytearray, str))):
            return list(value)
        return [value]
    return None

def _modified_asm2d_template_kinetics(*, basis: Any, design_targets: Dict[str, Any], reaction_fixes: Dict[str, float]) -> tuple[Dict[str, float], Dict[str, Any] | None]:
    """The reaction fixes with every house rate and correction the caller did not state; the caller's own fixes win.

    A basis that states no ``temperature_C`` is refused by name (OQ-C188-1). The qualified native export binds its
    fixes to its parameter basis, so it is never defaulted: a correction missing there is refused by name (c181 G1).
    Returns the fixes and the record the result carries (None on the export).
    """
    from watertap_engine_mcp.core.modified_asm2d_kinetics import house_kinetics
    from puran_mcp_support.engineering.house_standards.biological_kinetics import CORRECTED_PARAMETERS
    if (design_targets or {}).get('process_balance_native_export') is True:
        wrong = {name: reaction_fixes.get(name) for name, correction in CORRECTED_PARAMETERS.items() if reaction_fixes.get(name) != correction.value}
        if wrong:
            raise ModifiedAsm2dKineticsRefused(f'native export reaction_parameter_fixes {wrong} (None = absent): the payload must carry { {name: c.value for name, c in CORRECTED_PARAMETERS.items()}} from its parameter basis (native_kinetics_at; {TEMPLATE_KINETICS_RULING_ID}); never defaulted here')
        return (reaction_fixes, None)
    fields_set = getattr(basis, 'model_fields_set', None)
    if fields_set is not None and ('temperature_C' not in fields_set or getattr(basis, 'temperature_C', None) is None):
        raise ModifiedAsm2dKineticsRefused(f'modified_asm2d_kinetics_refused: the influent basis states no temperature_C, and a ModifiedASM2d template rating takes its feed temperature and kinetic set from it: state influent_basis.temperature_C (the TenderInfluentBasis default {float(basis.temperature_K) - 273.15:g} C is never used; {MODIFIED_ASM2D_ROUTES_RULING_ID} OQ-C188-1)')
    temperature_k = float(basis.temperature_K)
    return house_kinetics(reaction_fixes=reaction_fixes, temperature_k=temperature_k, temperature_source_clause_ref=f'solve_flowsheet influent basis temperature {temperature_k - 273.15:g} C', route='solve_flowsheet template path', ruling=TEMPLATE_KINETICS_RULING_ID)

def _universal_modified_asm2d_feed_conc(*, basis: Any, fractionation: Any, design_targets: Dict[str, Any]) -> tuple[Dict[str, float], Dict[str, float], Dict[str, float], Dict[str, Any]]:
    """Build a ModifiedASM2d feed vector preserving COD/TSS/TKN/NH4 inputs.

    The reaction fixes take the house kinetic set at the basis temperature (`_modified_asm2d_template_kinetics`);
    its record is returned under ``MODIFIED_ASM2D_KINETICS_KEY`` of the feed basis.
    """
    override = (design_targets or {}).get('feed_conc_kg_m3')
    if not isinstance(override, Mapping):
        override = (design_targets or {}).get('modified_asm2d_feed_conc_kg_m3')
    if isinstance(override, Mapping):
        feed = {str(key): float(value) for key, value in override.items()}
        parameter_fixes = dict((design_targets or {}).get('property_parameter_fixes') or {})
        reaction_fixes, kinetics = _modified_asm2d_template_kinetics(basis=basis, design_targets=design_targets, reaction_fixes=dict((design_targets or {}).get('reaction_parameter_fixes') or {}))
        return (feed, parameter_fixes, reaction_fixes, {'source': 'explicit_feed_conc_kg_m3', MODIFIED_ASM2D_KINETICS_KEY: kinetics})
    asm_state = dict(getattr(fractionation, 'asm2d_state', {}) or {})
    cod = float(getattr(basis, 'COD', 0.0) or 0.0)
    tss = float(getattr(basis, 'TSS', 0.0) or 0.0)
    tkn = float(getattr(basis, 'TKN', 0.0) or 0.0)
    nh4 = float(getattr(basis, 'NH4_N', 0.0) or 0.0)
    alkalinity = float(getattr(basis, 'alkalinity', 0.0) or 0.0)
    po4 = float(getattr(basis, 'PO4_P', 0.0) or 0.0)
    part_cod = max(tss * MODIFIED_ASM2D_COD_TO_VSS, 0.0)
    if cod > 0:
        part_cod = min(part_cod, 0.9 * cod)
    x_i_fraction = _universal_target_float(design_targets, 'modified_asm2d_particulate_inert_fraction', 'particulate_inert_fraction', 'x_i_particulate_fraction')
    if x_i_fraction is None:
        x_i_fraction = 0.26
    x_i_fraction = min(max(float(x_i_fraction), 0.0), 1.0)
    x_i = x_i_fraction * part_cod
    x_s = max(part_cod - x_i, 1e-06)
    soluble_cod = max(cod - part_cod, 1e-06)
    s_i = _universal_target_float(design_targets, 'S_I_mg_L', 's_i_mg_l', 'soluble_inert_cod_mg_l', 'soluble_inert_COD_mg_L')
    if s_i is None:
        s_i = float(asm_state.get('S_I', 0.0) or 0.0) * 1000.0
    s_i = min(max(s_i, 0.0), soluble_cod * 0.95)
    biodegradable_soluble_cod = max(soluble_cod - s_i, 1e-06)
    s_a_fraction = _universal_target_float(design_targets, 'S_A_fraction_of_biodegradable_soluble_cod', 's_a_fraction', 'vfa_fraction')
    if s_a_fraction is None:
        s_a_fraction = 0.22
    s_a_fraction = min(max(float(s_a_fraction), 0.0), 1.0)
    s_a = s_a_fraction * biodegradable_soluble_cod
    s_f = biodegradable_soluble_cod - s_a
    inert_n = MODIFIED_ASM2D_INERT_N_FRACTION * (s_i + x_i)
    bio_n_denominator = max(s_f + x_s, 1e-06)
    i_n_bio = max((tkn - nh4 - inert_n) / bio_n_denominator, 0.0)

    def biomass_trace_mg_l(*keys: str) -> float:
        value = _universal_mapping_float(design_targets, *keys)
        if value is None:
            return 1e-06
        return max(float(value), 1e-06)
    feed_mg_l = {'S_A': s_a, 'S_F': s_f, 'S_I': s_i, 'S_N2': 1e-06, 'S_NH4': max(nh4, 1e-06), 'S_NO3': max(float(getattr(basis, 'NO3_N', 0.0) or 0.0), 1e-06), 'S_O2': 1e-06, 'S_PO4': max(po4, 1e-06), 'S_K': _universal_mapping_float(design_targets, 'S_K_mg_L', 'potassium_mg_l') or 15.0, 'S_Mg': _universal_mapping_float(design_targets, 'S_Mg_mg_L', 'magnesium_mg_l') or 15.0, 'S_IC': max(alkalinity / 50.0 * 12.0, 1e-06), 'X_AUT': biomass_trace_mg_l('X_AUT_mg_L', 'x_aut_mg_l'), 'X_H': biomass_trace_mg_l('X_H_mg_L', 'x_h_mg_l'), 'X_I': max(x_i, 1e-06), 'X_PAO': biomass_trace_mg_l('X_PAO_mg_L', 'x_pao_mg_l'), 'X_PHA': biomass_trace_mg_l('X_PHA_mg_L', 'x_pha_mg_l'), 'X_PP': biomass_trace_mg_l('X_PP_mg_L', 'x_pp_mg_l'), 'X_S': max(x_s, 1e-06)}
    feed = {component: value / 1000.0 for component, value in feed_mg_l.items()}
    property_fixes = dict((design_targets or {}).get('property_parameter_fixes') or {})
    reaction_fixes = dict((design_targets or {}).get('reaction_parameter_fixes') or {})
    for name in ('i_NSF', 'i_NXS'):
        property_fixes.setdefault(name, i_n_bio)
        reaction_fixes.setdefault(name, i_n_bio)
    mu_aut = _universal_mapping_float(design_targets, 'mu_AUT', 'mu_aut', 'mu_aut_per_day', 'mu_aut_d')
    if mu_aut is not None:
        reaction_fixes.setdefault('mu_AUT', mu_aut)
    reaction_fixes, kinetics = _modified_asm2d_template_kinetics(basis=basis, design_targets=design_targets, reaction_fixes=reaction_fixes)
    basis_payload = {'source': 'bulk_cod_tss_tkn_nh4', 'COD_mg_L': cod, 'TSS_mg_L': tss, 'TKN_mg_L': tkn, 'NH4_N_mg_L': nh4, 'particulate_COD_mg_L': part_cod, 'S_I_mg_L': s_i, 'S_F_mg_L': s_f, 'S_A_mg_L': s_a, 'X_I_mg_L': x_i, 'X_S_mg_L': x_s, 'i_N_bio': i_n_bio, MODIFIED_ASM2D_KINETICS_KEY: kinetics}
    return (feed, property_fixes, reaction_fixes, basis_payload)

def _universal_template_config(*, engine_template_name: str, basis: Any, fractionation: Any, design_targets: Dict[str, Any], _inverse_design: bool=False) -> Dict[str, Any]:
    """Translate supplied specifications; never infer equipment geometry."""
    from watertap_engine_mcp.core.rating_geometry import EVIDENCE_KEYS, RatingGeometryError, require_number, template_geometry

    def checked(config):
        config.update({key: deepcopy(design_targets[key]) for key in EVIDENCE_KEYS if key in design_targets})
        if not _inverse_design:
            if engine_template_name in NATIVE_MBR_RATING_TEMPLATES and config.get('combined_mbr_recycle') and (_universal_target_float(design_targets, 'waste_flow_m3_d', 'qwas_m3d', 'Qwas_m3d', 'was_flow_m3_d') is not None) and (not config.get('aerobic_stage_volumes_m3')):
                raise RatingGeometryError(f'{engine_template_name}: stage (b) must supply aerobic_stage_volumes_m3 [m3] for each CSTR_Injection; a total volume or stage count does not specify its distribution.')
            template_geometry(engine_template_name, config)
        return config
    if engine_template_name.startswith('asm2d'):
        include_post = engine_template_name in {'asm2d_mle_post', 'asm2d_mbr_post'}
        property_package = _universal_asm2d_property_package(design_targets)
        feed_conc = dict(fractionation.asm2d_state)
        parameter_fixes: Dict[str, float] = {}
        reaction_fixes: Dict[str, float] = {}
        feed_basis: Dict[str, Any] = {'source': 'asm2d_fractionation'}
        if property_package == PropertyPackageType.MODIFIED_ASM2D:
            feed_conc, parameter_fixes, reaction_fixes, feed_basis = _universal_modified_asm2d_feed_conc(basis=basis, fractionation=fractionation, design_targets=design_targets)
        config = {'property_package': property_package, 'feed_flow_m3_per_day': float(basis.flow_m3_d), 'feed_temperature_K': float(basis.temperature_K), 'alkalinity_mol_per_m3': float(basis.alkalinity_mol_per_m3), 'feed_conc_kg_m3': feed_conc, 'property_parameter_fixes': parameter_fixes, 'reaction_parameter_fixes': reaction_fixes, 'combined_mbr_recycle': engine_template_name in NATIVE_MBR_RATING_TEMPLATES and (property_package == PropertyPackageType.MODIFIED_ASM2D or bool((design_targets or {}).get('combined_mbr_recycle'))), 'anoxic_volume_m3': _universal_reactor_volume(design_targets, 'Anoxic'), 'aerobic_volume_m3': _universal_reactor_volume(design_targets, 'Aerobic'), 'post_anoxic_volume_m3': _universal_reactor_volume(design_targets, 'PostAnoxic') if include_post else 0.0, 'target_srt_days': _universal_target_float(design_targets, 'target_srt_days', 'srt_days', 'srt_d', 'srt'), 'target_mlss_mg_l': _universal_target_float(design_targets, 'target_mlss_mg_l', 'target_mlss', 'mlss_mg_l', 'mlss'), 'ras_recycle_ratio': _universal_recycle_target(design_targets, 'ras'), 'mlr_recycle_ratio': _universal_recycle_target(design_targets, 'mlr')}
        if feed_basis.get(MODIFIED_ASM2D_KINETICS_KEY) is not None:
            config[MODIFIED_ASM2D_KINETICS_KEY] = feed_basis[MODIFIED_ASM2D_KINETICS_KEY]
        kla = _universal_target_float(design_targets, 'aerobic_kla_per_hr', 'kla_per_hr', 'kla')
        if kla is not None:
            config['aerobic_kla_per_hr'] = kla
        do_target = _universal_target_float(design_targets, 'target_do_mg_l', 'do_target', 'target_do', 'dissolved_oxygen_target')
        if do_target is not None:
            config['target_do_mg_l'] = do_target
        membrane_particulate = _universal_target_float(design_targets, 'membrane_particulate_permeate_fraction', 'mbr_particulate_permeate_fraction', 'membrane_particulate_split')
        if membrane_particulate is not None:
            config['membrane_particulate_permeate_fraction'] = membrane_particulate
        membrane_soluble = _universal_target_float(design_targets, 'membrane_soluble_permeate_fraction', 'mbr_soluble_permeate_fraction', 'membrane_soluble_split')
        if membrane_soluble is not None:
            config['membrane_soluble_permeate_fraction'] = membrane_soluble
        chemical_p_target = _universal_target_float(design_targets, 'target_effluent_tp_mg_l', 'target_tp_mg_l', 'chemical_p_target', 'tp_target', 'target_effluent_p')
        if chemical_p_target is not None:
            config['target_effluent_tp_mg_l'] = chemical_p_target
        alkalinity_target = _universal_target_float(design_targets, 'target_residual_alkalinity_mol_per_m3', 'target_alkalinity_mol_per_m3', 'target_residual_alkalinity', 'alkalinity_target', 'target_alkalinity')
        if alkalinity_target is not None:
            config['target_residual_alkalinity_mol_per_m3'] = alkalinity_target
        alkalinity_target_mg_caco3 = _universal_target_float(design_targets, 'target_residual_alkalinity_mg_caco3_l', 'target_alkalinity_mg_caco3_l', 'target_mg_l_as_caco3')
        if alkalinity_target_mg_caco3 is not None:
            config['target_residual_alkalinity_mg_caco3_l'] = alkalinity_target_mg_caco3
        if 'post_anoxic_volume_m3' in design_targets and include_post:
            config['post_anoxic_volume_m3'] = design_targets['post_anoxic_volume_m3']
        if 'reactor_volumes_fixed' in design_targets:
            config['reactor_volumes_fixed'] = design_targets['reactor_volumes_fixed']
        if 'aerobic_stage_volumes_m3' in design_targets:
            config['aerobic_stage_volumes_m3'] = design_targets['aerobic_stage_volumes_m3']
        if 'aerobic_only' in design_targets:
            raise RatingGeometryError(f"{engine_template_name}: aerobic_only is not a design target; the aerobic-only MBR (no anoxic zone) is its own template, topology_template='asm2d_mbr_aerobic'")
        return checked(config)
    if engine_template_name.startswith('adm1'):
        from watertap_engine_mcp.templates.adm1 import ADM1DigestionConfig, DEFAULT_ADM1_FEED_CONC_KG_M3
        defaults = ADM1DigestionConfig()
        flow = _universal_target_float(design_targets, 'feed_flow_m3_per_day', 'feed_flow_m3_d', 'flow_m3_d')
        if flow is None and basis is not None:
            flow = float(getattr(basis, 'flow_m3_d', 0.0) or 0.0) or None
        if flow is None:
            flow = defaults.feed_flow_m3_per_day
        temperature = _universal_target_float(design_targets, 'feed_temperature_K', 'digester_temperature_K', 'temperature_K')
        if temperature is None and basis is not None:
            temperature = float(getattr(basis, 'temperature_K', 0.0) or 0.0) or None
        if temperature is None:
            temperature = defaults.feed_temperature_K
        volume_liquid = _universal_geometry_input(design_targets, 'digester_volume_liquid_m3', 'volume_liquid_m3', 'reactor_volume_m3')
        volume_vapor = _universal_geometry_input(design_targets, 'digester_volume_vapor_m3', 'volume_vapor_m3')
        feed_conc = dict(DEFAULT_ADM1_FEED_CONC_KG_M3)
        override_conc = (design_targets or {}).get('feed_conc_kg_m3')
        if isinstance(override_conc, dict):
            for component, value in override_conc.items():
                try:
                    feed_conc[component] = float(value)
                except (TypeError, ValueError):
                    continue
        config: Dict[str, Any] = {'feed_flow_m3_per_day': float(flow), 'feed_temperature_K': float(temperature), 'feed_conc_kg_m3': feed_conc, 'volume_liquid_m3': volume_liquid, 'volume_vapor_m3': volume_vapor}
        cations = _universal_target_float(design_targets, 'feed_cations_kmol_m3', 'cations_kmol_m3')
        if cations is not None:
            config['feed_cations_kmol_m3'] = cations
        anions = _universal_target_float(design_targets, 'feed_anions_kmol_m3', 'anions_kmol_m3')
        if anions is not None:
            config['feed_anions_kmol_m3'] = anions
        headspace = _universal_target_float(design_targets, 'headspace_pressure_Pa', 'headspace_pressure_pa')
        if headspace is not None:
            config['headspace_pressure_Pa'] = headspace
        if engine_template_name == 'adm1_cstr' and (design_targets or {}).get('sulfur_iron') is not None:
            config['sulfur_iron'] = bool(design_targets['sulfur_iron'])
        if (design_targets or {}).get('organic_sulfur') is not None:
            config['organic_sulfur'] = bool(design_targets['organic_sulfur'])
        if (design_targets or {}).get('oxidant_inlet') is not None:
            config['oxidant_inlet'] = design_targets['oxidant_inlet']
        wetted_area = _universal_geometry_input(design_targets, 'headspace_wetted_area_m2')
        if wetted_area is not None:
            config['headspace_wetted_area_m2'] = wetted_area
        if engine_template_name == 'adm1_anmbr':
            passage = _universal_geometry_input(design_targets, 'membrane_particulate_passage')
            if passage is not None:
                config['membrane_particulate_passage'] = passage
            srt = _universal_geometry_input(design_targets, 'target_srt_days')
            if srt is not None:
                config['target_srt_days'] = srt
        if engine_template_name == 'adm1_two_stage':
            acid_volume_liquid = _universal_geometry_input(design_targets, 'acid_volume_liquid_m3', 'acidogenic_volume_liquid_m3', 'acid_reactor_volume_m3')
            if acid_volume_liquid is not None:
                config['acid_volume_liquid_m3'] = acid_volume_liquid
            acid_volume_vapor = _universal_geometry_input(design_targets, 'acid_volume_vapor_m3', 'acidogenic_volume_vapor_m3')
            if acid_volume_vapor is not None:
                config['acid_volume_vapor_m3'] = acid_volume_vapor
            acid_temperature = _universal_target_float(design_targets, 'acid_temperature_K', 'acidogenic_temperature_K', 'acid_stage_temperature_K')
            if acid_temperature is not None:
                config['acid_temperature_K'] = float(acid_temperature)
        return checked(config)
    if engine_template_name == 'ro_train':
        config: Dict[str, Any] = {'property_package': _universal_ro_property_package(design_targets)}
        membrane_type = _universal_target_text(design_targets, 'membrane_type', 'ro_membrane_type')
        if membrane_type is not None:
            config['membrane_type'] = membrane_type
        membrane_model = _universal_target_text(design_targets, 'membrane_model', 'selected_membrane_model', 'target_membrane_model')
        if membrane_model is not None:
            config['membrane_model'] = membrane_model
            if membrane_type is None:
                config['membrane_type'] = 'product_specific'
        n_stages = _universal_geometry_input(design_targets, 'n_stages', 'ro_stages')
        if n_stages is not None:
            config['n_stages'] = int(require_number(n_stages, 'n_stages', 'count', 'ro_train'))
        stage_vessels = _universal_geometry_input(design_targets, 'stage_vessels', 'vessels_per_stage', 'ro_stage_vessels')
        if stage_vessels is not None:
            if not isinstance(stage_vessels, list) or not stage_vessels:
                raise RatingGeometryError('ro_train: stage (b) must supply stage_vessels [count] as a nonempty list')
            config['stage_vessels'] = [int(require_number(value, 'stage_vessels', 'count', 'ro_train')) for value in stage_vessels]
            config.setdefault('n_stages', len(config['stage_vessels']))
        elements_per_vessel = _universal_geometry_input(design_targets, 'elements_per_vessel', 'ro_elements_per_vessel')
        if elements_per_vessel is not None:
            config['elements_per_vessel'] = int(require_number(elements_per_vessel, 'elements_per_vessel', 'count', 'ro_train'))
        element_area = _universal_geometry_input(design_targets, 'element_area_m2', 'membrane_element_area_m2')
        if element_area is not None:
            config['element_area_m2'] = element_area
        membrane_area = _universal_geometry_input(design_targets, 'membrane_area_m2', 'total_membrane_area_m2', 'ro_membrane_area_m2')
        if membrane_area is not None:
            config['membrane_area_m2'] = membrane_area
        stage_pressures = _universal_mapping_sequence(design_targets, 'stage_pressures_barg', 'per_stage_pressures_barg', 'ro_stage_pressures_barg')
        pressure_alias = None
        if stage_pressures is None:
            stage_pressures = _universal_mapping_sequence(design_targets, 'stage_pressures_bar', 'per_stage_pressures_bar', 'ro_stage_pressures_bar')
            if stage_pressures is not None:
                pressure_alias = 'legacy_stage_pressures_bar_interpreted_as_barg'
        if stage_pressures is not None:
            config['stage_pressures_barg'] = [float(value) for value in stage_pressures]
        stage_pressure_drops = _universal_mapping_sequence(design_targets, 'stage_pressure_drops_bar', 'per_stage_pressure_drops_bar', 'ro_stage_pressure_drops_bar')
        if stage_pressure_drops is not None:
            config['stage_pressure_drops_bar'] = [float(value) for value in stage_pressure_drops]
        stage_recoveries = _universal_mapping_sequence(design_targets, 'stage_recovery_targets', 'per_stage_recoveries', 'ro_stage_recovery_targets')
        if stage_recoveries is not None:
            stage_recoveries = [float(value) for value in stage_recoveries]
        recovery = _universal_target_float(design_targets, 'target_recovery', 'recovery', 'ro_recovery', 'target_system_recovery')
        if recovery is not None:
            recovery = float(recovery)
        pressure = _universal_target_float(design_targets, 'feed_pressure_barg', 'pressure_barg')
        if pressure is None:
            pressure = _universal_target_float(design_targets, 'feed_pressure_bar', 'pressure_bar')
            if pressure is not None:
                pressure_alias = 'legacy_feed_pressure_bar_interpreted_as_barg'
        if pressure is not None:
            config['feed_pressure_barg'] = pressure
        permeate_pressure_bara = _universal_target_float(design_targets, 'permeate_pressure_bara')
        if permeate_pressure_bara is None:
            permeate_backpressure_barg = _universal_target_float(design_targets, 'permeate_backpressure_bar', 'permeate_backpressure_barg')
            if permeate_backpressure_barg is not None:
                from watertap_engine_mcp.templates.ro_train import STANDARD_ATMOSPHERE_BAR
                permeate_pressure_bara = float(permeate_backpressure_barg) + STANDARD_ATMOSPHERE_BAR
        if permeate_pressure_bara is not None:
            config['permeate_pressure_bara'] = permeate_pressure_bara
        a_comp = _universal_target_float(design_targets, 'A_comp', 'water_permeability_m_s_pa', 'membrane_A_comp_m_s_pa')
        if a_comp is not None:
            config['A_comp'] = a_comp
        b_comp = _universal_target_float(design_targets, 'B_comp', 'salt_permeability_m_s', 'membrane_B_comp_m_s')
        if b_comp is not None:
            config['B_comp'] = b_comp
        transport_provenance = _universal_target_text(design_targets, 'transport_provenance', 'membrane_transport_provenance')
        provenance_reference = _universal_target_text(design_targets, 'provenance_reference', 'transport_provenance_reference', 'membrane_transport_reference', 'membrane_source_artifact')
        coefficient_temperature_k = _universal_target_float(design_targets, 'coefficient_temperature_K', 'membrane_coefficient_temperature_K', 'feed_temperature_K', 'temperature_K')
        if coefficient_temperature_k is None:
            design_temperature_c = _universal_target_float(design_targets, 'design_temperature_C', 'feed_temperature_C', 'temperature_C')
            if design_temperature_c is not None:
                coefficient_temperature_k = float(design_temperature_c) + 273.15
        if coefficient_temperature_k is not None:
            config['coefficient_temperature_K'] = coefficient_temperature_k
        if membrane_model is not None and a_comp is not None and (b_comp is not None):
            from watertap_engine_mcp.public_catalog_boundary import validate_product_transport
            validated_transport = validate_product_transport(membrane_model=membrane_model, A_w=a_comp, B_s=b_comp, coefficient_temperature_K=float(config.get('coefficient_temperature_K', 298.15)), transport_provenance=transport_provenance, provenance_reference=provenance_reference)
            config['membrane_model'] = validated_transport['membrane_model']
            config['A_comp'] = validated_transport['A_w']
            config['B_comp'] = validated_transport['B_s']
            config['transport_basis'] = validated_transport['transport_basis']
            config['transport_provenance'] = validated_transport['transport_provenance']
            config['provenance_reference'] = validated_transport['provenance_reference']
        operating_mode = _universal_target_text(design_targets, 'operating_mode', 'ro_operating_mode', 'ro_sizing_mode')
        if not _inverse_design and operating_mode is not None and (operating_mode.strip().lower() != 'rating'):
            raise RatingGeometryError('ro_train: use design_flowsheet explicitly; solve_flowsheet only rates stage (b) geometry')
        operating_mode = 'design' if _inverse_design else 'rating'
        config['operating_mode'] = operating_mode
        if operating_mode.strip().lower() == 'rating':
            if recovery is not None:
                config['system_recovery_verification_target'] = recovery
            if stage_recoveries is not None:
                config['stage_recovery_verification_targets'] = stage_recoveries
        else:
            if recovery is not None:
                config['target_system_recovery'] = recovery
            if stage_recoveries is not None:
                config['stage_recovery_targets'] = stage_recoveries
        config['pressure_input_basis'] = pressure_alias or 'explicit_barg' if pressure is not None or stage_pressures is not None else 'template_default_barg'
        validity_ceiling = _universal_target_float(design_targets, 'property_validity_ceiling_g_L', 'max_ro_brine_tds_g_L')
        if validity_ceiling is not None:
            config['property_validity_ceiling_g_L'] = validity_ceiling
        return checked(config)
    if engine_template_name in {'mvc_evaporator', 'mvc_crystallizer'}:
        config: Dict[str, Any] = {}
        if engine_template_name == 'mvc_crystallizer' and 'crystal_median_length_m' in design_targets:
            config['crystal_median_length_m'] = design_targets['crystal_median_length_m']
        evaporator_area = _universal_geometry_input(design_targets, 'evaporator_area_m2', 'evap_area_m2', 'evaporator_heat_transfer_area_m2')
        if evaporator_area is not None:
            config['evaporator_area_m2'] = evaporator_area
        if engine_template_name == 'mvc_evaporator':
            endpoint = _universal_target_float(design_targets, 'target_brine_mass_fraction')
            if endpoint is not None:
                if evaporator_area is not None and _inverse_design:
                    raise ValueError('Specify either evaporator area for rating or target brine mass fraction for design')
                config['target_brine_mass_fraction'] = endpoint
            approach = _universal_target_float(design_targets, 'feed_preheat_approach_K')
            if approach is not None:
                config['feed_preheat_approach_K'] = approach
        heat_transfer_coeff = _universal_target_float(design_targets, 'heat_transfer_coeff_W_m2K', 'evaporator_U_W_m2K', 'overall_heat_transfer_coeff_W_m2K', 'U_W_m2K')
        if heat_transfer_coeff is not None:
            config['heat_transfer_coeff_W_m2K'] = float(heat_transfer_coeff)
        brine_outlet_temp = _universal_target_float(design_targets, 'brine_outlet_temp_K', 'evaporator_brine_outlet_temp_K', 'brine_temperature_K')
        if brine_outlet_temp is not None:
            config['brine_outlet_temp_K'] = float(brine_outlet_temp)
        pressure_ratio = _universal_target_float(design_targets, 'pressure_ratio', 'compressor_pressure_ratio', 'compressor_ratio')
        if pressure_ratio is not None:
            config['pressure_ratio'] = float(pressure_ratio)
        compressor_efficiency = _universal_target_float(design_targets, 'compressor_efficiency', 'compressor_isentropic_efficiency')
        if compressor_efficiency is not None:
            config['compressor_efficiency'] = float(compressor_efficiency)
        if engine_template_name == 'mvc_crystallizer':
            crystallizer_temperature = _universal_target_float(design_targets, 'crystallizer_temperature_K', 'crystallizer_temp_K', 'crystallization_temperature_K')
            if crystallizer_temperature is not None:
                config['crystallizer_temperature_K'] = float(crystallizer_temperature)
            crystallization_yield = _universal_target_float(design_targets, 'crystallization_yield', 'crystallizer_yield', 'crystal_yield')
            if crystallization_yield is not None:
                config['crystallization_yield'] = float(crystallization_yield)
        return checked(config)
    if engine_template_name == 'nf_softening':
        config = {key: design_targets[key] for key in ('membrane_area_m2', 'n_stages', 'feed_pressure_bar', 'permeate_pressure_bar', 'pump_efficiency', 'property_package_config') if key in design_targets}
        return checked(config)
    return checked({})
PLANT_STATE_START = 'plant_state'
CONSTANT_START = 'constant'
NATIVE_MBR_RATING_TEMPLATES = ('asm2d_mbr', 'asm2d_mbr_aerobic')
MEMBRANE_TANK_TARGET = 'membrane_tank_reactor'
MEMBRANE_TANK_FIELDS = frozenset({'volume_m3', 'kla_o2_per_h', 's_o2_saturation_kg_m3', 'source'})
MEMBRANE_TANK_LIQUOR_EQUIVALENCE = "the membrane tank's liquor is its retentate; the CSTR before the split is its V q_mbr (1 - f) / q_ret representation at the split's inlet (ModifiedASM2d rates first-degree homogeneous in the particulates)"

def _membrane_tank_target(design_targets: Dict[str, Any]) -> Dict[str, Any] | None:
    """`MEMBRANE_TANK_TARGET` validated: every field stated, each number positive and finite; else refused by name."""
    spec = design_targets.get(MEMBRANE_TANK_TARGET)
    if spec is None:
        return None
    if not isinstance(spec, dict) or set(spec) != MEMBRANE_TANK_FIELDS:
        raise ValueError(f'{MEMBRANE_TANK_TARGET} states exactly {sorted(MEMBRANE_TANK_FIELDS)}; got {spec!r}')
    for field in MEMBRANE_TANK_FIELDS - {'source'}:
        value = spec[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (not math.isfinite(value)) or (value <= 0):
            raise ValueError(f'{MEMBRANE_TANK_TARGET}.{field} must be a positive finite number; got {value!r}')
    if not isinstance(spec['source'], str) or not spec['source'].strip():
        raise ValueError(f'{MEMBRANE_TANK_TARGET}.source names where the volume and the O2 coefficients come from')
    return {**{field: float(spec[field]) for field in MEMBRANE_TANK_FIELDS - {'source'}}, 'source': spec['source']}
SIDE_FEEDS_TARGET = 'native_side_feeds'
SIDE_FEED_FIELDS = frozenset({'role', 'flow_m3_d', 'conc_kg_m3'})
SIDE_FEED_ROLES = ('aerobic', 'mbr')

def _side_feeds_target(design_targets: Dict[str, Any], components: List[str], particulates: List[str], *, aerobic_only: bool) -> List[Dict[str, Any]]:
    """`SIDE_FEEDS_TARGET` validated, in `SIDE_FEED_ROLES` order; else refused by name."""
    spec = design_targets.get(SIDE_FEEDS_TARGET)
    if spec is None:
        return []
    if design_targets.get('process_balance_native_export') is not True:
        raise ValueError(f"{SIDE_FEEDS_TARGET} is the qualified native export's only")
    if not isinstance(spec, list) or not spec:
        raise ValueError(f'{SIDE_FEEDS_TARGET} is a non-empty list of {sorted(SIDE_FEED_FIELDS)}; got {spec!r}')
    feeds: Dict[str, Dict[str, Any]] = {}
    for item in spec:
        if not isinstance(item, dict) or set(item) != SIDE_FEED_FIELDS:
            raise ValueError(f'{SIDE_FEEDS_TARGET} entry states exactly {sorted(SIDE_FEED_FIELDS)}; got {item!r}')
        role = item['role']
        if role not in SIDE_FEED_ROLES or role in feeds or (aerobic_only and role == 'aerobic'):
            raise ValueError(f"{SIDE_FEEDS_TARGET}: role {role!r} is not one of {list(SIDE_FEED_ROLES)} once each, away from the train's feed reactor")
        flow = item['flow_m3_d']
        if isinstance(flow, bool) or not isinstance(flow, (int, float)) or (not math.isfinite(flow)) or (flow <= 0):
            raise ValueError(f'{SIDE_FEEDS_TARGET} {role}: flow_m3_d must be positive and finite; got {flow!r}')
        conc = item['conc_kg_m3']
        if not isinstance(conc, dict) or set(conc) != set(components):
            raise ValueError(f'{SIDE_FEEDS_TARGET} {role}: conc_kg_m3 must exactly cover the ModifiedASM2d components')
        if any((isinstance(v, bool) or not isinstance(v, (int, float)) or (not math.isfinite(v)) or (v < 0) for v in conc.values())):
            raise ValueError(f'{SIDE_FEEDS_TARGET} {role}: concentrations must be finite and nonnegative')
        solids = sorted((c for c in particulates if conc[c] > 0))
        if solids:
            raise ValueError(f'{SIDE_FEEDS_TARGET} {role}: a side feed carries no particulate; got {solids}')
        feeds[role] = {'role': role, 'flow_m3_d': float(flow), 'conc_kg_m3': {c: float(conc[c]) for c in components}}
    return [feeds[role] for role in SIDE_FEED_ROLES if role in feeds]
NON_EBPR_POOLS_PINNED_TARGET = 'non_ebpr_pools_pinned'
EBPR_ROUTE_TARGET = 'ebpr_steady_state_on_invasion'
BASE_PHASE_BY_START = {PLANT_STATE_START: 'base_plant_state_start', CONSTANT_START: 'base_constant_start'}
PLANT_STATE_START_BOUND_PUSH = 1e-08
PLANT_STATE_START_UNIT_SOLVER = 'ipopt-watertap'
PLANT_STATE_START_DEFAULT_SOLVER_RETRY = "plant-state start re-formed on IDAES's default unit initialisation after the server's solver failed a reactor's initialisation (actions.PLANT_STATE_START_DEFAULT_SOLVER_RETRY)"
PLANT_STATE_START_SEEDED_POOLS = ('X_PAO', 'X_PHA', 'X_PP')
PLANT_STATE_START_POOL_SEED_KG_M3 = 1e-06

class _PlantStateStartNotFormed(ValueError):
    """The plant-state start cannot be formed from these inputs; the constant start runs and records why."""

def _modified_asm2d_plant_state_start(*, reaction_params: Any, components: List[str], particulates: List[str], feed_conc_kg_m3: Dict[str, float], feed_flow_m3_d: float, recycle_ratio: float, waste_flow_m3_d: float, anoxic_volume_m3: float, aerobic_volumes_m3: List[float], do_setpoint_kg_m3: float, particulate_permeate_fraction: float, temperature_k: float, pressure_pa: float, seeded_pool_guess_kg_m3: Dict[str, float], aerobic_only: bool=False) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Estimate the anoxic-inlet (tear) stream from the plant's own inputs: an INITIAL POINT, not a result."""
    import pyomo.environ as pyo
    per_day = 1 / pyo.units.day
    kg_per_m3 = pyo.units.kg / pyo.units.m ** 3
    coefficients: Dict[str, float] = {}

    def read(name: str, unit: Any=None) -> float:
        parameter = getattr(reaction_params, name, None)
        if parameter is None:
            raise _PlantStateStartNotFormed(f'the installed reaction block has no parameter {name}')
        value = float(pyo.value(parameter if unit is None else pyo.units.convert(parameter, to_units=unit)))
        if not math.isfinite(value) or value < 0:
            raise _PlantStateStartNotFormed(f'reaction parameter {name} is {value!r}')
        coefficients[name] = value
        return value

    def feed(component: str) -> float:
        if component not in feed_conc_kg_m3:
            raise _PlantStateStartNotFormed(f'the feed carries no {component}')
        return float(feed_conc_kg_m3[component])
    volume = (0.0 if aerobic_only else float(anoxic_volume_m3)) + float(sum(aerobic_volumes_m3))
    plant = {'feed flow': feed_flow_m3_d, 'recycle ratio': recycle_ratio, 'waste flow': waste_flow_m3_d, 'reactor volume': volume, 'DO setpoint': do_setpoint_kg_m3}
    for label, value in plant.items():
        if not math.isfinite(value) or value <= 0:
            raise _PlantStateStartNotFormed(f'{label} is {value!r}')
    if not 0 <= particulate_permeate_fraction < 1:
        raise _PlantStateStartNotFormed(f'membrane particulate permeate fraction is {particulate_permeate_fraction!r}')
    tear_flow = feed_flow_m3_d * (1.0 + recycle_ratio)
    retentate_flow = feed_flow_m3_d * recycle_ratio + waste_flow_m3_d
    concentration = (1.0 - particulate_permeate_fraction) * tear_flow / retentate_flow
    solids_retention = volume / (waste_flow_m3_d * concentration + particulate_permeate_fraction * tear_flow)
    y_h, y_a, f_xi = (read('Y_H'), read('Y_A'), read('f_XI'))
    b_h, b_aut = (read('b_H', per_day), read('b_AUT', per_day))
    mu_h, mu_aut, k_h = (read('mu_H', per_day), read('mu_AUT', per_day), read('K_H', per_day))
    oxygen = do_setpoint_kg_m3
    oxygen_h = oxygen / (read('KH_O2', kg_per_m3) + oxygen)
    oxygen_aut = oxygen / (read('KA_O2', kg_per_m3) + oxygen)
    oxygen_hydrolysis = oxygen / (read('KL_O2', kg_per_m3) + oxygen)
    loss_h = 1.0 / solids_retention + b_h
    if mu_h * oxygen_h <= loss_h:
        raise _PlantStateStartNotFormed(f'heterotrophs cannot be sustained at the estimated solids retention ({solids_retention:.3g} d): mu_H x fO2 <= 1/SRT + b_H')
    growth_fraction = loss_h / (mu_h * oxygen_h)
    s_f = read('K_F', kg_per_m3) * growth_fraction / (1.0 - growth_fraction)
    s_a = read('KH_A', kg_per_m3) * growth_fraction / (1.0 - growth_fraction)
    degradable_load = feed_flow_m3_d * max(feed('S_F') + feed('S_A') + feed('X_S') - s_f - s_a, 0.0)
    x_h = (y_h * degradable_load + feed_flow_m3_d * feed('X_H')) / (volume * (1.0 / solids_retention + b_h * (1.0 - y_h * (1.0 - f_xi))))
    if not x_h > 0:
        raise _PlantStateStartNotFormed('the feed carries no degradable COD and no heterotrophs')
    i_nbm, i_nxi, i_nxs = (read('i_NBM'), read('i_NXI'), read('i_NXS'))
    i_nsf = read('i_NSF')
    x_i_heterotrophic = (feed_flow_m3_d * feed('X_I') + f_xi * b_h * x_h * volume) * solids_retention / volume
    nitrogen_in = feed_flow_m3_d * (feed('S_NH4') + i_nsf * feed('S_F') + i_nxs * feed('X_S') + i_nxi * feed('X_I') + i_nbm * (feed('X_H') + feed('X_AUT')))
    nitrogen_in_solids = (i_nbm * x_h + i_nxi * x_i_heterotrophic) * volume / solids_retention
    nitrogen_available = nitrogen_in - nitrogen_in_solids - feed_flow_m3_d * i_nsf * s_f
    if not nitrogen_available > 0:
        raise _PlantStateStartNotFormed("the feed's nitrogen does not cover the nitrogen of the wasted solids (nitrogen-limited growth)")
    loss_aut = 1.0 / solids_retention + b_aut
    nitrified = 0.0
    s_nh4 = nitrogen_available / feed_flow_m3_d
    if mu_aut * oxygen_aut > loss_aut:
        nitrifier_fraction = loss_aut / (mu_aut * oxygen_aut)
        s_nh4_nitrifying = read('KA_NH4', kg_per_m3) * nitrifier_fraction / (1.0 - nitrifier_fraction)
        if s_nh4_nitrifying < s_nh4:
            nitrified = nitrogen_available - feed_flow_m3_d * s_nh4_nitrifying
            s_nh4 = s_nh4_nitrifying
    x_aut = (y_a * nitrified + feed_flow_m3_d * feed('X_AUT')) / (volume * loss_aut)
    x_i = (feed_flow_m3_d * feed('X_I') + f_xi * (b_h * x_h + b_aut * x_aut) * volume) * solids_retention / volume
    supply_per_heterotroph = (feed_flow_m3_d * feed('X_S') / volume + (1.0 - f_xi) * (b_h * x_h + b_aut * x_aut)) / x_h
    if k_h * oxygen_hydrolysis <= supply_per_heterotroph:
        raise _PlantStateStartNotFormed('hydrolysis cannot keep up with the slowly degradable COD supplied (K_H x fO2)')
    x_s = read('KL_X') * supply_per_heterotroph / (k_h * oxygen_hydrolysis - supply_per_heterotroph) * x_h
    oxidised = (nitrified + feed_flow_m3_d * feed('S_NO3')) / feed_flow_m3_d
    s_no3 = oxidised if aerobic_only else oxidised / (1.0 + recycle_ratio)
    s_n2 = feed('S_N2') + oxidised - s_no3
    basin = {component: feed(component) for component in components}
    basin.update({'S_F': s_f, 'S_A': s_a, 'S_NH4': s_nh4, 'S_NO3': s_no3, 'S_N2': s_n2, 'S_O2': oxygen, 'X_H': x_h, 'X_AUT': x_aut, 'X_I': x_i, 'X_S': x_s})
    tear = {}
    for component in components:
        recycled = basin[component] * (concentration if component in particulates else 1.0)
        tear[component] = (feed(component) + recycle_ratio * recycled) / (1.0 + recycle_ratio)
    for component in PLANT_STATE_START_SEEDED_POOLS:
        if component not in seeded_pool_guess_kg_m3:
            raise _PlantStateStartNotFormed(f'no seed is given for {component}')
        tear[component] = basin[component] = float(seeded_pool_guess_kg_m3[component])
    for component in components:
        for where, value in (('anoxic inlet', tear[component]), ('aerobic basin', basin[component])):
            if not math.isfinite(value) or value < 0:
                raise _PlantStateStartNotFormed(f'the estimate of {component} at the {where} is {value!r}')
    guess = {'flow_vol': {0: tear_flow / 86400.0}, 'conc_mass_comp': {(0, component): tear[component] for component in components}, 'temperature': {0: temperature_k}, 'pressure': {0: pressure_pa}}
    aerobic_state = {'flow_vol': tear_flow / 86400.0, 'temperature': temperature_k, 'pressure': pressure_pa, 'conc_mass_comp': {component: basin[component] for component in components}}
    record = {'start': PLANT_STATE_START, 'statement': "initial point of the nonlinear solve, estimated from the plant's own inputs; not a result", 'solids_retention_estimate_d': solids_retention, 'retentate_over_basin_particulates': concentration, 'anoxic_inlet_flow_m3_d': tear_flow, 'nitrified_estimate_kg_n_d': nitrified, 'basin_estimate_mg_l': {component: basin[component] * 1000.0 for component in components}, 'coefficients_read_from_the_reaction_block': coefficients}
    return (guess, aerobic_state, record)

def _modified_asm2d_mbr_rating_direct_result(*, started: float, engine_template_name: str, template_config: Dict[str, Any], design_targets: Dict[str, Any], fractionation: Any, initial_start: str | None=None, default_solver_unit_initialisation: bool=False, prior_solve_attempts: List[Dict[str, Any]] | None=None, kinetics_record: Dict[str, Any] | None=None) -> SolveResult | None:
    """Solve the fixed-volume ModifiedASM2d MBR rating oracle path directly."""
    if engine_template_name not in NATIVE_MBR_RATING_TEMPLATES:
        return None
    if template_config.get('property_package') != PropertyPackageType.MODIFIED_ASM2D:
        return None
    if not bool(template_config.get('combined_mbr_recycle')):
        return None
    qwas_m3_d = _universal_target_float(design_targets, 'waste_flow_m3_d', 'qwas_m3d', 'Qwas_m3d', 'was_flow_m3_d')
    if qwas_m3_d is None:
        return None
    pin_pools = design_targets.get(NON_EBPR_POOLS_PINNED_TARGET) is True
    if pin_pools and design_targets.get('process_balance_native_export') is not True:
        raise ValueError(f"{NON_EBPR_POOLS_PINNED_TARGET} is the qualified native export's route only")
    ebpr_route = design_targets.get(EBPR_ROUTE_TARGET) is True
    if ebpr_route and (not pin_pools):
        raise ValueError(f'{EBPR_ROUTE_TARGET} is asked only with {NON_EBPR_POOLS_PINNED_TARGET}')
    try:
        import pyomo.environ as pyo
        from idaes.core import FlowsheetBlock
        from idaes.core.util.model_statistics import degrees_of_freedom
        import idaes.core.util.scaling as iscale
        import idaes.logger as idaeslog
        from idaes.models.unit_models import CSTR, Feed, Mixer, PressureChanger, Product, Separator
        from idaes.models.unit_models.mixer import MomentumMixingType
        from idaes.models.unit_models.separator import SplittingType
        from pyomo.network import Arc, SequentialDecomposition
        from watertap.core.solvers import get_solver
        from watertap.property_models.unit_specific.activated_sludge.modified_asm2d_properties import ModifiedASM2dParameterBlock
        from watertap.property_models.unit_specific.activated_sludge.modified_asm2d_reactions import ModifiedASM2dReactionParameterBlock
        from watertap.unit_models.cstr_injection import CSTR_Injection
    except Exception as exc:
        report = _universal_infeasibility_report(model_family='ModifiedASM2D', solver_status='build_error', termination_condition='direct_rating_import_failed', message=f'ModifiedASM2d direct rating imports failed: {exc}', remediation=['Install/activate the WaterTAP/IDAES environment used by watertap-engine-mcp.'], diagnostic_basis={'error': str(exc)})
        return _universal_empty_result(status='infeasible_structured', solve_seconds=time.monotonic() - started, diagnostics={'direct_rating_path': 'import_failed'}, report=report)
    components = ['S_A', 'S_F', 'S_I', 'S_N2', 'S_NH4', 'S_NO3', 'S_O2', 'S_PO4', 'S_K', 'S_Mg', 'S_IC', 'X_AUT', 'X_H', 'X_I', 'X_PAO', 'X_PHA', 'X_PP', 'X_S']
    particulates = ['X_AUT', 'X_H', 'X_I', 'X_PAO', 'X_PHA', 'X_PP', 'X_S']
    cod_to_vss = MODIFIED_ASM2D_COD_TO_VSS
    q_m3_d = float(template_config['feed_flow_m3_per_day'])
    recycle_ratio = _universal_recycle_target(design_targets, 'ras')
    if recycle_ratio is None:
        recycle_ratio = float((template_config.get('recycle_ratios') or {}).get('ras') if isinstance(template_config.get('recycle_ratios'), dict) else 4.0)
    recycle_ratio = float(recycle_ratio)
    qwas_m3_d = float(qwas_m3_d)
    if qwas_m3_d <= 0 or qwas_m3_d >= q_m3_d:
        report = _universal_infeasibility_report(model_family='ModifiedASM2D', solver_status='not_started', termination_condition='invalid_was_flow', message='waste_flow_m3_d must be positive and below feed flow for MBR rating.', remediation=['Provide a physically valid waste_flow_m3_d/Qwas_m3d.'], diagnostic_basis={'flow_m3_d': q_m3_d, 'waste_flow_m3_d': qwas_m3_d})
        return _universal_empty_result(status='infeasible_structured', solve_seconds=time.monotonic() - started, diagnostics={'direct_rating_path': 'invalid_was_flow'}, report=report)
    aerobic_only = engine_template_name == 'asm2d_mbr_aerobic'
    vanox = None if aerobic_only else float(template_config['anoxic_volume_m3'])
    vaer_total = float(template_config['aerobic_volume_m3'])
    from watertap_engine_mcp.core.rating_geometry import RatingGeometryError, template_geometry, verify_live_geometry
    stage_volumes = design_targets.get('aerobic_stage_volumes_m3')
    if not isinstance(stage_volumes, list) or not stage_volumes:
        raise RatingGeometryError(f'{engine_template_name}: stage (b) must supply aerobic_stage_volumes_m3 [m3] for each CSTR_Injection')
    aerobic_volumes = [float(value) for value in stage_volumes]
    geometry_contract = template_geometry(engine_template_name, template_config)
    membrane_tank = _membrane_tank_target(design_targets)
    reaction_config = reaction_config_target(design_targets)
    side_feeds = _side_feeds_target(design_targets, components, particulates, aerobic_only=aerobic_only)
    q_in_m3_d = q_m3_d + sum((item['flow_m3_d'] for item in side_feeds))
    if membrane_tank is not None:
        tank_permeate_fraction = float(template_config.get('membrane_particulate_permeate_fraction') or 1e-07)
        membrane_tank['representation_m3'] = membrane_tank['volume_m3'] * (q_in_m3_d * (1.0 + recycle_ratio) * (1.0 - tank_permeate_fraction) / (q_in_m3_d * recycle_ratio + qwas_m3_d))
    geometry_contract['native_geometry'] = [*(() if aerobic_only else ({'unit_id': 'Anoxic', 'unit_type': 'CSTR', 'var_name': 'volume[0]', 'value': vanox},)), *({'unit_id': f'Aerobic{index}', 'unit_type': 'CSTR_Injection', 'var_name': 'volume[0]', 'value': volume} for index, volume in enumerate(aerobic_volumes, 1)), *(({'unit_id': 'MembraneTank', 'unit_type': 'CSTR_Injection', 'var_name': 'volume[0]', 'value': membrane_tank['representation_m3'], 'stage_b_volume_m3': membrane_tank['volume_m3'], 'derivation': MEMBRANE_TANK_LIQUOR_EQUIVALENCE},) if membrane_tank is not None else ())]
    if design_targets.get('process_balance_native_export') is True:
        supplied_feed = template_config.get('feed_conc_kg_m3') or {}
        if set(supplied_feed) != set(components):
            raise ValueError('native process-balance feed must exactly cover ModifiedASM2d components')
        feed_conc = {component: float(supplied_feed[component]) for component in components}
        if any((not math.isfinite(value) or value < 0 for value in feed_conc.values())):
            raise ValueError('native process-balance feed concentrations must be finite and nonnegative')
    else:
        feed_conc = {component: max(float((template_config.get('feed_conc_kg_m3') or {}).get(component, 1e-09)), 1e-09) for component in components}
    temperature_k = float(template_config.get('feed_temperature_K') or 308.15)
    pressure_pa = float(template_config.get('feed_pressure_Pa') or 101325.0)
    do_kg_m3 = float(template_config.get('target_do_mg_l') or 2.0) / 1000.0
    _phases = _run_timing.Phases('native', category='a')
    _phases.enter('build', start=initial_start or 'first', aerobic_stages=len(aerobic_volumes))
    m = pyo.ConcreteModel()
    m.fs = FlowsheetBlock(dynamic=False)
    m.fs.props = ModifiedASM2dParameterBlock()
    m.fs.rxn = ModifiedASM2dReactionParameterBlock(property_package=m.fs.props, **reaction_package_options(reaction_config))
    fix_parameters([m.fs.props], template_config.get('property_parameter_fixes') or {}, where=f'{engine_template_name} direct rating (m.fs.props)', label='property')
    fix_reaction_parameters([m.fs.rxn], template_config.get('reaction_parameter_fixes') or {}, where=f'{engine_template_name} direct rating (m.fs.rxn)')
    m.fs.FeedWater = Feed(property_package=m.fs.props)
    m.fs.MX1 = Mixer(property_package=m.fs.props, inlet_list=['feed_water', 'recycle'], momentum_mixing_type=MomentumMixingType.equality)
    if not aerobic_only:
        m.fs.Anoxic = CSTR(property_package=m.fs.props, reaction_package=m.fs.rxn)
    aerobic_units = []
    for index, _volume in enumerate(aerobic_volumes, start=1):
        unit = CSTR_Injection(property_package=m.fs.props, reaction_package=m.fs.rxn)
        setattr(m.fs, f'Aerobic{index}', unit)
        aerobic_units.append(unit)
    reactors = aerobic_units if aerobic_only else [m.fs.Anoxic, *aerobic_units]
    if membrane_tank is not None:
        m.fs.MembraneTank = CSTR_Injection(property_package=m.fs.props, reaction_package=m.fs.rxn, has_aeration=True)
        reactors = [*reactors, m.fs.MembraneTank]
    aerated_units = [*aerobic_units, *((m.fs.MembraneTank,) if membrane_tank is not None else ())]
    reaction_config_record = apply_reaction_config(m.fs.rxn, reactors, reaction_config)
    if kinetics_record is not None:
        kinetics_record['built_reaction_parameters'] = built_reaction_parameters(m.fs.rxn)
    m.fs.MBR = Separator(property_package=m.fs.props, outlet_list=['permeate', 'retentate'], split_basis=SplittingType.componentFlow)
    m.fs.SludgeSplitter = Separator(property_package=m.fs.props, outlet_list=['waste', 'recycle'])
    m.fs.RASPressure = PressureChanger(property_package=m.fs.props)
    m.fs.TreatedEffluent = Product(property_package=m.fs.props)
    m.fs.WAS = Product(property_package=m.fs.props)
    side_units: List[Dict[str, Any]] = []
    for item in side_feeds:
        name = item['role'].capitalize()
        setattr(m.fs, f'SideFeed{name}', Feed(property_package=m.fs.props))
        setattr(m.fs, f'SideMixer{name}', Mixer(property_package=m.fs.props, inlet_list=['upstream', 'side'], momentum_mixing_type=MomentumMixingType.equality))
        side_units.append({**item, 'feed': getattr(m.fs, f'SideFeed{name}'), 'mixer': getattr(m.fs, f'SideMixer{name}'), 'into': aerobic_units[0] if item['role'] == 'aerobic' else m.fs.MembraneTank if membrane_tank is not None else None})
    m.native_side_feeds = side_units
    side_mixer = {item['role']: item['mixer'] for item in side_units}

    def arc(name: str, source: Any, destination: Any, role: str) -> None:
        """``source`` to ``destination``, through the role's side mixer where a side feed states one."""
        mixer = side_mixer.get(role)
        if mixer is None:
            setattr(m.fs, name, Arc(source=source, destination=destination))
            return
        setattr(m.fs, name, Arc(source=source, destination=mixer.upstream))
        setattr(m.fs, f'{name}s', Arc(source=mixer.outlet, destination=destination))
    m.fs.a01 = Arc(source=m.fs.FeedWater.outlet, destination=m.fs.MX1.feed_water)
    m.fs.a02 = Arc(source=m.fs.MX1.outlet, destination=reactors[0].inlet)
    if not aerobic_only:
        arc('a03', m.fs.Anoxic.outlet, aerobic_units[0].inlet, 'aerobic')
    for index in range(len(aerobic_units) - 1):
        setattr(m.fs, f'a_aerobic_{index}', Arc(source=aerobic_units[index].outlet, destination=aerobic_units[index + 1].inlet))
    if membrane_tank is None:
        arc('a04', aerobic_units[-1].outlet, m.fs.MBR.inlet, 'mbr')
    else:
        arc('a04', aerobic_units[-1].outlet, m.fs.MembraneTank.inlet, 'mbr')
        m.fs.a04m = Arc(source=m.fs.MembraneTank.outlet, destination=m.fs.MBR.inlet)
    for item in side_units:
        setattr(m.fs, f'a_side_{item['role']}', Arc(source=item['feed'].outlet, destination=item['mixer'].side))
    m.fs.a05 = Arc(source=m.fs.MBR.permeate, destination=m.fs.TreatedEffluent.inlet)
    m.fs.a06 = Arc(source=m.fs.MBR.retentate, destination=m.fs.SludgeSplitter.inlet)
    m.fs.a07 = Arc(source=m.fs.SludgeSplitter.waste, destination=m.fs.WAS.inlet)
    m.fs.a08 = Arc(source=m.fs.SludgeSplitter.recycle, destination=m.fs.RASPressure.inlet)
    m.fs.a09 = Arc(source=m.fs.RASPressure.outlet, destination=m.fs.MX1.recycle)
    pyo.TransformationFactory('network.expand_arcs').apply_to(m)
    for unit in aerobic_units:
        for component in m.fs.props.component_list:
            if component != 'S_O2':
                unit.injection[:, :, component].fix(0)
        unit.outlet.conc_mass_comp[:, 'S_O2'].fix(do_kg_m3)
    m.fs.FeedWater.flow_vol.fix(q_m3_d / 86400.0)
    m.fs.FeedWater.temperature.fix(temperature_k)
    m.fs.FeedWater.pressure.fix(pressure_pa)
    for component in components:
        m.fs.FeedWater.conc_mass_comp[0, component].fix(feed_conc[component])
    for item in side_units:
        item['feed'].flow_vol.fix(item['flow_m3_d'] / 86400.0)
        item['feed'].temperature.fix(temperature_k)
        item['feed'].pressure.fix(pressure_pa)
        for component in components:
            item['feed'].conc_mass_comp[0, component].fix(item['conc_kg_m3'][component])
    if not aerobic_only:
        m.fs.Anoxic.volume.fix(vanox)
    for unit, volume in zip(aerobic_units, aerobic_volumes):
        unit.volume.fix(volume)
    if membrane_tank is not None:
        tank = m.fs.MembraneTank
        for component in m.fs.props.component_list:
            if component != 'S_O2':
                tank.injection[:, :, component].fix(0)
        tank.volume.fix(membrane_tank['representation_m3'])
        tank.KLa.fix(membrane_tank['kla_o2_per_h'] * membrane_tank['volume_m3'] / membrane_tank['representation_m3'])
        tank.S_O_eq.set_value(membrane_tank['s_o2_saturation_kg_m3'])
    q_mbr = q_in_m3_d * (1.0 + recycle_ratio)
    q_perm = q_in_m3_d - qwas_m3_d
    soluble_split = q_perm / q_mbr
    particulate_split = float(template_config.get('membrane_particulate_permeate_fraction') or 1e-07)
    for component in m.fs.props.component_list:
        if component in particulates:
            m.fs.MBR.split_fraction[0, 'permeate', component].fix(particulate_split)
        else:
            m.fs.MBR.split_fraction[0, 'permeate', component].fix(soluble_split)
    q_ret = q_in_m3_d * recycle_ratio + qwas_m3_d
    m.fs.SludgeSplitter.split_fraction[:, 'recycle'].fix(q_in_m3_d * recycle_ratio / q_ret)
    m.fs.RASPressure.outlet.pressure.fix(pressure_pa)
    m.fs.MX1.pressure_equality_constraints[0.0, 2].deactivate()
    for item in side_units:
        item['mixer'].pressure_equality_constraints[0.0, 2].deactivate()
    boundary_blocks = [m.fs.FeedWater.properties[0.0], m.fs.MX1.feed_water_state[0.0], *(block for item in side_units for block in (item['feed'].properties[0.0], item['mixer'].side_state[0.0]))]
    pin_record: Dict[str, Any] | None = None
    pinned_rows: List[Any] = []
    pin_fixed: List[Any] = []
    fed_pools = {pool: feed_conc[pool] for pool in PLANT_STATE_START_SEEDED_POOLS if ebpr_route and feed_conc[pool] > 0.0}
    if pin_pools:
        from watertap_engine_mcp.utils.non_ebpr_pin import pin_non_ebpr_pools
        for pool in fed_pools:
            m.fs.FeedWater.conc_mass_comp[0, pool].fix(0.0)
        pin_record, pinned_rows = pin_non_ebpr_pools(m, reactors, m.fs.rxn, newly_fixed=pin_fixed)
        if fed_pools:
            pin_record['feed_pools_held_at_zero_for_the_ebpr_start_kg_m3'] = dict(fed_pools)
    ebpr_record: Dict[str, Any] | None = None
    _phases.enter('scaling')
    for var in m.fs.component_data_objects(pyo.Var, descend_into=True):
        name = var.name
        if 'flow_vol' in name:
            iscale.set_scaling_factor(var, 10.0)
        elif 'temperature' in name:
            iscale.set_scaling_factor(var, 0.01)
        elif 'pressure' in name:
            iscale.set_scaling_factor(var, 1e-05)
        elif 'conc_mass_comp' in name:
            iscale.set_scaling_factor(var, 10.0)
    for unit in reactors:
        control_volume = unit.control_volume
        for component in (control_volume.reactions[0.0].rate_expression, unit.cstr_performance_eqn, control_volume.rate_reaction_stoichiometry_constraint, control_volume.material_balances):
            try:
                iscale.set_scaling_factor(component, 1000.0)
            except Exception:
                pass
    iscale.calculate_scaling_factors(m)
    dof = degrees_of_freedom(m)
    seq = SequentialDecomposition()
    seq.options.select_tear_method = 'heuristic'
    seq.options.tear_method = 'Direct'
    seq.options.iterLim = 1
    constant_start_guess = {'flow_vol': {0: 0.06}, 'conc_mass_comp': {(0, 'S_A'): 0.02, (0, 'S_F'): 0.05, (0, 'S_I'): 0.15, (0, 'S_N2'): 0.001, (0, 'S_NH4'): 0.01, (0, 'S_NO3'): 0.005, (0, 'S_O2'): 0.0001, (0, 'S_PO4'): 0.003, (0, 'S_K'): 0.015, (0, 'S_Mg'): 0.015, (0, 'S_IC'): 0.04, (0, 'X_AUT'): 0.1, (0, 'X_H'): 6.0, (0, 'X_I'): 1.5, (0, 'X_PAO'): 0.01 if design_targets.get('process_balance_native_export') is True else 1e-06, (0, 'X_PHA'): 0.01 if design_targets.get('process_balance_native_export') is True else 1e-06, (0, 'X_PP'): 0.01 if design_targets.get('process_balance_native_export') is True else 1e-06, (0, 'X_S'): 0.3}, 'temperature': {0: temperature_k}, 'pressure': {0: pressure_pa}}
    native_export = design_targets.get('process_balance_native_export') is True
    _phases.enter('initial_point')
    start_used = CONSTANT_START
    start_guess = constant_start_guess
    aerobic_start_state: Dict[str, Any] | None = None
    initial_point: Dict[str, Any] | None = None
    if native_export:
        if initial_start == CONSTANT_START:
            initial_point = {'start': CONSTANT_START, 'reason': 'fallback: the plant-state start did not reach an optimal base solve'}
        else:
            try:
                start_guess, aerobic_start_state, initial_point = _modified_asm2d_plant_state_start(reaction_params=m.fs.rxn, components=components, particulates=particulates, feed_conc_kg_m3=feed_conc if not side_units else {c: (q_m3_d * feed_conc[c] + sum((s['flow_m3_d'] * s['conc_kg_m3'][c] for s in side_units))) / q_in_m3_d for c in components}, feed_flow_m3_d=q_in_m3_d, recycle_ratio=recycle_ratio, waste_flow_m3_d=qwas_m3_d, anoxic_volume_m3=vanox, aerobic_volumes_m3=aerobic_volumes, aerobic_only=aerobic_only, do_setpoint_kg_m3=do_kg_m3, particulate_permeate_fraction=particulate_split, temperature_k=temperature_k, pressure_pa=pressure_pa, seeded_pool_guess_kg_m3={component: PLANT_STATE_START_POOL_SEED_KG_M3 for component in PLANT_STATE_START_SEEDED_POOLS})
                start_used = PLANT_STATE_START
                if default_solver_unit_initialisation:
                    initial_point = {**initial_point, 'unit_initialisation': PLANT_STATE_START_DEFAULT_SOLVER_RETRY}
            except _PlantStateStartNotFormed as exc:
                initial_point = {'start': CONSTANT_START, 'reason': f'the plant-state start could not be formed: {exc}'}
    base_phase = BASE_PHASE_BY_START[start_used] if native_export else 'base'
    seq.set_guesses_for(reactors[0].inlet, start_guess)
    if side_units:
        seq.set_tear_set([m.fs.a02])

    def initialize_unit(unit: Any) -> None:
        if aerobic_start_state is not None and default_solver_unit_initialisation:
            if any((unit is aerobic for aerobic in aerobic_units)):
                unit.initialize(state_args=aerobic_start_state, outlvl=idaeslog.WARNING, optarg={'bound_push': 0.01, 'linear_solver': 'ma27'})
                return
            unit.initialize(outlvl=idaeslog.WARNING, optarg={'bound_push': 0.01, 'linear_solver': 'ma27'})
            return
        if aerobic_start_state is not None and any((unit is reactor for reactor in reactors)):
            unit.initialize(state_args=aerobic_start_state if any((unit is aerobic for aerobic in aerobic_units)) else None, outlvl=idaeslog.WARNING, solver=PLANT_STATE_START_UNIT_SOLVER, optarg={'bound_push': 0.01, 'linear_solver': 'ma27'})
            return
        unit.initialize(outlvl=idaeslog.WARNING, optarg={'bound_push': 0.01, 'linear_solver': 'ma27'})
    native_failure_message = None
    native_log_path = STORAGE_DIR / f'native-biology-{uuid.uuid4().hex}.log' if design_targets.get('process_balance_native_export') is True else None
    native_solve_kwargs = {'logfile': str(native_log_path)} if native_log_path is not None else {}
    base_solver_options = {'linear_solver': 'ma27', 'max_iter': int(_universal_target_float(design_targets, 'max_iter', 'solver_max_iter') or 600), 'tol': float(_universal_target_float(design_targets, 'tol', 'solver_tol') or 1e-06)}
    if start_used == PLANT_STATE_START:
        base_solver_options.update({'bound_push': PLANT_STATE_START_BOUND_PUSH, 'bound_frac': PLANT_STATE_START_BOUND_PUSH})
    native_polish_options = {'tol': 1e-09, 'constr_viol_tol': 1e-11, 'acceptable_tol': 1e-09, 'acceptable_constr_viol_tol': 1e-11, 'mu_strategy': 'adaptive', 'bound_relax_factor': 0, 'honor_original_bounds': 'yes', 'bound_push': 1e-12, 'bound_frac': 1e-12}
    nitrifier_recovery: Dict[str, Any] | None = None
    solve_attempts: List[Dict[str, Any]] = list(prior_solve_attempts or [])
    base_converged = False
    base_recorded = False
    from watertap_engine_mcp.solver.strict_polish import WARM_START_OPTIONS, capture_multipliers, clear_warm_start, declare_multiplier_import, export_warm_start, row_resolution_audit, solve_attempt_record
    try:
        _phases.enter('initialisation', start=start_used)
        seq.run(m, initialize_unit)
        if start_used == PLANT_STATE_START:
            from idaes.core.util.initialization import propagate_state as propagate_to_product
            for product_arc in (m.fs.a05, m.fs.a07):
                propagate_to_product(arc=product_arc)
        m.fs.MX1.pressure_equality_constraints[0.0, 2].deactivate()
        for item in side_units:
            item['mixer'].pressure_equality_constraints[0.0, 2].deactivate()
        if pin_record is not None:
            from watertap_engine_mcp.utils.non_ebpr_pin import NonEbprPinError, zero_jacobian_rows
            reactivated = [row.name for row in pinned_rows if row.active]
            if reactivated:
                raise NonEbprPinError(f'initialisation reactivated pinned rows {reactivated[:5]}')
            zero_rows = zero_jacobian_rows(m)
            if zero_rows:
                raise NonEbprPinError(f'the pinned model carries zero Jacobian rows {zero_rows[:5]}')
            pin_record['zero_jacobian_rows_before_solve'] = 0
        declare_multiplier_import(m)
        solver = get_solver()
        solver.options.update(base_solver_options)
        _phases.enter('solve', phase=base_phase)
        try:
            solve_results = solver.solve(m, tee=False, **native_solve_kwargs)
        except Exception as exc:
            solve_attempts.append(solve_attempt_record(base_phase, options=base_solver_options, exception=exc, log_path=native_log_path))
            if initial_point is not None:
                solve_attempts[-1]['initial_point'] = initial_point
            base_recorded = True
            raise
        solve_attempts.append(solve_attempt_record(base_phase, options=base_solver_options, results=solve_results, log_path=native_log_path))
        if initial_point is not None:
            solve_attempts[-1]['initial_point'] = initial_point
        base_recorded = True
        base_converged = bool(pyo.check_optimal_termination(solve_results))
        polish_multipliers = capture_multipliers(m, source=base_phase)
        branch_resolve_options = {**base_solver_options, 'bound_push': 1e-08, 'bound_frac': 1e-08}

        def nitrifier_branch_check() -> Tuple[Any, Dict[str, Any] | None]:
            from puran_mcp_support.engineering.validation.solution_physicality import AUTOTROPH_VIABILITY_FLOOR_MG_L
            from watertap_engine_mcp.solver.nitrifier_invasion import select_nitrifying_branch
            _phases.enter('branch_check')

            def resolve_base_solve() -> Any:
                branch_solver = get_solver()
                branch_solver.options.update(branch_resolve_options)
                return branch_solver.solve(m, tee=False, **native_solve_kwargs)
            reseeded_results, recovery = select_nitrifying_branch(m, list(reactors), aerated_units, resolve=resolve_base_solve, is_optimal=pyo.check_optimal_termination, waste_port=m.fs.WAS.inlet, permeate_port=m.fs.TreatedEffluent.inlet, particulates=particulates, boundary_blocks=boundary_blocks, ammonium_margins=NITRIFIER_RESEED_AMMONIUM_MARGINS, viability_floor_mg_l=AUTOTROPH_VIABILITY_FLOOR_MG_L)
            if recovery is not None:
                recovery['resolve_solver_options'] = branch_resolve_options
            return (reseeded_results, recovery)

        def strict_polish(multipliers: Any, phase_prefix: str='') -> Any:
            """The strict polish of a converged point; returns the last attempt's results (raises its exception)."""
            from watertap_engine_mcp.utils.process_balance_biology_export import restate_modified_asm2d_energy_equations, scale_modified_asm2d_material_equations
            _phases.enter(phase_prefix + 'polish')
            scale_modified_asm2d_material_equations(m, aerated_units)
            energy_restatement = restate_modified_asm2d_energy_equations(m, constr_viol_tol=native_polish_options['constr_viol_tol'])
            resolution_audit = row_resolution_audit(m, tolerance=native_polish_options['constr_viol_tol'])
            polish_options = {**base_solver_options, **native_polish_options}
            pre_polish_point = [(var, var.value) for var in m.component_data_objects(pyo.Var, descend_into=True) if not var.fixed]
            polish_plan = ((phase_prefix + 'strict_polish', {**polish_options, **WARM_START_OPTIONS}, True), (phase_prefix + 'strict_polish_cold_restart', polish_options, False))
            polish_error: Exception | None = None
            results = None
            for phase, options, warm in polish_plan:
                if warm:
                    warm_record = export_warm_start(m, multipliers)
                else:
                    for var, value in pre_polish_point:
                        var.set_value(value, skip_validation=True)
                    clear_warm_start(m)
                    warm_record = None
                polish_solver = get_solver()
                polish_solver.options.update(options)
                polish_error = None
                try:
                    results = polish_solver.solve(m, tee=False, load_solutions=False, **native_solve_kwargs)
                except Exception as exc:
                    polish_error = exc
                if polish_error is None:
                    loadable = str(results.solver.status) in {'ok', 'warning'}
                    if loadable:
                        m.solutions.load_from(results)
                    polish_record = solve_attempt_record(phase, options=options, results=results, log_path=native_log_path, loaded=loadable)
                else:
                    polish_record = solve_attempt_record(phase, options=options, exception=polish_error, log_path=native_log_path)
                polish_record['warm_start'] = warm_record
                polish_record['energy_row_restatement'] = energy_restatement
                polish_record['row_resolution_audit'] = resolution_audit
                solve_attempts.append(polish_record)
                if polish_error is None and pyo.check_optimal_termination(results):
                    break
            clear_warm_start(m)
            if polish_error is not None:
                raise polish_error
            return results

        def rate_ebpr_steady_state(record: Dict[str, Any]) -> Any:
            """Public engineering calculation interface."""
            from watertap_engine_mcp.solver.ebpr_steady_state import EBPR_ROUTE_RULING_ID, ebpr_stability, load_train_state, ray_seed, seeded_start, settle
            from watertap_engine_mcp.solver.nitrifier_invasion import solved_srt_d
            from watertap_engine_mcp.utils.non_ebpr_pin import unpin_non_ebpr_pools
            _phases.enter('ebpr_seed')
            invasion = pin_record['pao_invasion']
            record.update({'ruling': EBPR_ROUTE_RULING_ID, 'trigger': {'invasion_rate_per_d': invasion.get('invasion_rate_per_d'), 'srt_d': invasion.get('srt_d'), **({'fed_pools_kg_m3': dict(fed_pools)} if fed_pools else {})}})
            if invasion.get('invasive') is not True:
                record['refused'] = 'FED_POOLS_WITHOUT_INVASION'
                raise ValueError(f'{EBPR_ROUTE_TARGET}: the feed carries PAO pools {fed_pools} (kg/m3) and the zero-pool start is not invaded (invasion rate {invasion.get('invasion_rate_per_d')!r}): no qualified route rates this feed')
            record['unpin'] = unpin_non_ebpr_pools(m, pinned_rows, pin_fixed, dof=pin_record['dof'])
            for pool, value in fed_pools.items():
                m.fs.FeedWater.conc_mass_comp[0, pool].fix(value)
                m.fs.MX1.feed_water_state[0.0].conc_mass_comp[pool].set_value(value)
            pinned_srt_d = solved_srt_d(reactors, m.fs.WAS.inlet, particulates)
            record['seed'] = ray_seed(m, list(reactors), invasion, srt_d=pinned_srt_d, permeate_port=m.fs.TreatedEffluent.inlet, waste_port=m.fs.WAS.inlet)
            _phases.enter('ebpr_trajectory')
            dynamics, start = seeded_start(m, list(reactors), record['seed'])
            trajectory = settle(dynamics, start, srt_d=pinned_srt_d)
            record['trajectory'] = {key: value for key, value in trajectory.items() if key != 'values'}
            record['trajectory']['stream_blocks_set'] = load_train_state(m, list(reactors), dynamics, trajectory['values'], boundary_blocks=boundary_blocks)
            _phases.enter('ebpr_solve')
            ebpr_solver = get_solver()
            ebpr_solver.options.update(branch_resolve_options)
            try:
                results = ebpr_solver.solve(m, tee=False, **native_solve_kwargs)
            except Exception as exc:
                solve_attempts.append(solve_attempt_record('ebpr_seeded_solve', options=branch_resolve_options, exception=exc, log_path=native_log_path))
                record['seeded_solve'] = f'{type(exc).__name__}: {exc}'
                raise
            solve_attempts.append(solve_attempt_record('ebpr_seeded_solve', options=branch_resolve_options, results=results, log_path=native_log_path))
            record['seeded_solve'] = str(results.solver.termination_condition)
            if not pyo.check_optimal_termination(results):
                return results
            multipliers = capture_multipliers(m, source='ebpr_seeded_solve')
            reseeded, record['nitrifier_branch'] = nitrifier_branch_check()
            if reseeded is not None:
                results = reseeded
                multipliers = capture_multipliers(m, source='ebpr_nitrifier_branch_reseed')
            results = strict_polish(multipliers, 'ebpr_')
            record['polish'] = str(results.solver.termination_condition)
            if not pyo.check_optimal_termination(results):
                return results
            _phases.enter('ebpr_stability')
            record['srt_d'] = solved_srt_d(reactors, m.fs.WAS.inlet, particulates)
            try:
                record['stability'] = ebpr_stability(m, list(reactors), pool_reactions=pin_record['pool_reactions'])
            except Exception as exc:
                record['stability'] = {'ruling': EBPR_ROUTE_RULING_ID, 'verdict': None, 'unanswered': f'{type(exc).__name__}: {exc}'}
            return results
        if pyo.check_optimal_termination(solve_results):
            reseeded_results, nitrifier_recovery = nitrifier_branch_check()
            if reseeded_results is not None:
                solve_results = reseeded_results
                polish_multipliers = capture_multipliers(m, source='nitrifier_branch_reseed')
        if design_targets.get('process_balance_native_export') is True and pyo.check_optimal_termination(solve_results):
            solve_results = strict_polish(polish_multipliers)
        if pin_record is not None and pyo.check_optimal_termination(solve_results):
            from watertap_engine_mcp.utils.non_ebpr_pin import verify_pinned_solution
            pin_record['solution'] = verify_pinned_solution(m, pin_record, pinned_rows)
            from watertap_engine_mcp.solver.nitrifier_invasion import solved_srt_d
            from watertap_engine_mcp.solver.pao_invasion import PAO_INVASION_RULING_ID, pao_invasion
            _phases.enter('pao_invasion')
            try:
                pin_record['pao_invasion'] = pao_invasion(m, list(reactors), pool_reactions=pin_record['pool_reactions'], srt_d=solved_srt_d(reactors, m.fs.WAS.inlet, particulates))
            except Exception as exc:
                pin_record['pao_invasion'] = {'ruling': PAO_INVASION_RULING_ID, 'invasion_rate_per_d': None, 'invasive': None, 'unanswered': f'{type(exc).__name__}: {exc}'}
            if ebpr_route and (pin_record['pao_invasion'].get('invasive') is True or fed_pools):
                ebpr_record = {}
                solve_results = rate_ebpr_steady_state(ebpr_record)
        termination = str(solve_results.solver.termination_condition)
        optimal = pyo.check_optimal_termination(solve_results)
    except Exception as exc:
        termination = type(exc).__name__
        native_failure_message = str(exc)
        optimal = False
        solve_results = None
        if start_used == PLANT_STATE_START and (not base_recorded):
            solve_attempts.append({'phase': base_phase, 'solver_options': dict(base_solver_options), 'exception': f'{type(exc).__name__}: {exc}', 'failed_before_the_solve': True, 'initial_point': initial_point})
    if start_used == PLANT_STATE_START and (not base_converged) and (not base_recorded) and (not default_solver_unit_initialisation) and (PLANT_STATE_START_UNIT_SOLVER is not None):
        _phases.enter('fallback')
        retry_result = _modified_asm2d_mbr_rating_direct_result(started=started, engine_template_name=engine_template_name, template_config=template_config, design_targets=design_targets, fractionation=fractionation, default_solver_unit_initialisation=True, prior_solve_attempts=solve_attempts, kinetics_record=kinetics_record)
        _phases.close()
        return retry_result
    if start_used == PLANT_STATE_START and (not base_converged):
        _phases.enter('fallback')
        fallback_result = _modified_asm2d_mbr_rating_direct_result(started=started, engine_template_name=engine_template_name, template_config=template_config, design_targets=design_targets, fractionation=fractionation, initial_start=CONSTANT_START, prior_solve_attempts=solve_attempts, kinetics_record=kinetics_record)
        _phases.close()
        return fallback_result
    _phases.enter('extraction', optimal=optimal)

    def port_components(port: Any) -> Dict[str, float]:
        return {component: float(pyo.value(port.conc_mass_comp[0, component])) * 1000.0 for component in components}

    def mlss_mg_l(conc_mg_l: Dict[str, float]) -> float:
        return sum((conc_mg_l[component] for component in particulates)) / cod_to_vss

    def cod_mg_l(conc_mg_l: Dict[str, float]) -> float:
        return conc_mg_l['S_F'] + conc_mg_l['S_A'] + conc_mg_l['S_I'] + sum((conc_mg_l[component] for component in particulates))

    def total_n_mg_l(conc_mg_l: Dict[str, float]) -> float:
        prop = m.fs.props
        i_n = {component: 0.0 for component in components}
        i_n['S_NH4'] = i_n['S_NO3'] = i_n['S_N2'] = 1.0
        i_n['S_F'] = float(pyo.value(prop.i_NSF))
        i_n['S_I'] = float(pyo.value(prop.i_NSI))
        i_n['X_S'] = float(pyo.value(prop.i_NXS))
        i_n['X_I'] = float(pyo.value(prop.i_NXI))
        for component in ('X_H', 'X_AUT', 'X_PAO'):
            i_n[component] = float(pyo.value(prop.i_NBM))
        return sum((conc_mg_l[component] * i_n[component] for component in components))

    def rated_result() -> Tuple[SolveResult, float | None]:
        """Read the rating result off the model's CURRENT solved state, gated.

        Called for the first solve and again for a nitrifier-branch re-solve,
        so the result, the SRT and the native export always describe the state
        the solver actually returned.
        """
        verify_live_geometry(geometry_contract, {**({} if aerobic_only else {'Anoxic': m.fs.Anoxic}), **{f'Aerobic{index}': unit for index, unit in enumerate(aerobic_units, 1)}, **({'MembraneTank': m.fs.MembraneTank} if membrane_tank is not None else {})})
        from watertap_engine_mcp.utils.effluent_metrics import aggregate_biological_effluent_metrics
        permeate = port_components(m.fs.TreatedEffluent.inlet)
        aerobic = port_components(aerobic_units[-1].outlet)
        retentate = port_components(m.fs.SludgeSplitter.inlet)
        waste = port_components(m.fs.WAS.inlet)
        reactor_inventory = (0.0 if aerobic_only else vanox * sum((port_components(m.fs.Anoxic.outlet)[component] for component in particulates)) / 1000.0) + sum((volume * sum((port_components(unit.outlet)[component] for component in particulates)) / 1000.0 for unit, volume in zip(aerobic_units, aerobic_volumes))) + (membrane_tank['volume_m3'] * sum((retentate[component] for component in particulates)) / 1000.0 if membrane_tank is not None else 0.0)
        waste_biomass = qwas_m3_d * sum((waste[component] for component in particulates)) / 1000.0
        srt_d = reactor_inventory / waste_biomass if waste_biomass > 0 else None
        n_in = q_m3_d * total_n_mg_l({component: feed_conc[component] * 1000.0 for component in components}) / 1000.0 + sum((item['flow_m3_d'] * total_n_mg_l({c: v * 1000.0 for c, v in item['conc_kg_m3'].items()}) / 1000.0 for item in side_units))
        n_out = q_perm * total_n_mg_l(permeate) / 1000.0 + qwas_m3_d * total_n_mg_l(waste) / 1000.0
        n_err_pct = 100.0 * (n_out - n_in) / n_in if n_in else None
        max_residual = _max_active_constraint_residual(m)
        settled = {'MLSS_settled_mg_L': round(mlss_mg_l(aerobic), 4), 'MLSS_basin_mg_L': round(mlss_mg_l(aerobic), 4), 'MLSS_cage_mg_L': round(mlss_mg_l(retentate), 4), 'MLSS_cage_to_basin_ratio': mlss_mg_l(retentate) / mlss_mg_l(aerobic) if mlss_mg_l(aerobic) else None, 'SRT_actual_d': round(float(srt_d), 4) if srt_d is not None else None, 'MLVSS_MLSS_ratio': None, 'X_AUT_mg_L': aerobic['X_AUT'], 'X_H_mg_L': aerobic['X_H'], 'X_I_mg_L': aerobic['X_I'], 'aerobic_kla_per_hr': [float(pyo.value(unit.KLa[0] if unit.KLa.is_indexed() else unit.KLa)) if hasattr(unit, 'KLa') else None for unit in aerobic_units], 'recycle_ratios_actual': {'ras': recycle_ratio, 'was_flow_m3_d': qwas_m3_d}, 'reactor_volumes_m3': {'Aerobic': vaer_total} if aerobic_only else {'Anoxic': vanox, 'Aerobic': vaer_total}, 'aerobic_stage_volumes_m3': aerobic_volumes}
        if membrane_tank is not None:
            settled['membrane_tank'] = {'volume_m3': membrane_tank['volume_m3'], 'representation_m3': float(pyo.value(m.fs.MembraneTank.volume[0])), 'representation': MEMBRANE_TANK_LIQUOR_EQUIVALENCE, 'kla_o2_per_h': membrane_tank['kla_o2_per_h'], 's_o2_saturation_mg_L': float(pyo.value(m.fs.MembraneTank.S_O_eq)) * 1000.0, 'do_mg_L': port_components(m.fs.MembraneTank.outlet)['S_O2'], 'o2_transfer_kg_d': float(pyo.value(pyo.units.convert(m.fs.MembraneTank.injection[0, 'Liq', 'S_O2'], to_units=pyo.units.kg / pyo.units.day))), 'MLSS_mg_L': mlss_mg_l(retentate), 'source': membrane_tank['source']}
        effluent = {'flow_m3_d': q_perm, 'temperature_K': temperature_k, 'pressure_kPa': pressure_pa / 1000.0, 'components_mg_L': permeate, 'COD_mg_L': cod_mg_l(permeate), 'COD': cod_mg_l(permeate), 'NH4_N_mg_L': permeate['S_NH4'], 'NO3_N_mg_L': permeate['S_NO3'], 'TSS_mg_L': mlss_mg_l(permeate), 'TSS': mlss_mg_l(permeate)}
        measurement = aggregate_biological_effluent_metrics({component: value / 1000.0 for component, value in permeate.items()}, port_id='TreatedEffluent.inlet')
        effluent['biological_measurement_aggregation'] = measurement
        if measurement['status'] == 'complete':
            for metric, value in measurement['metrics_kg_m3'].items():
                value_mg_l = float(value) * 1000.0
                effluent[f'{metric}_mg_L'] = value_mg_l
                if metric in {'COD', 'BOD5', 'TN', 'TKN', 'TP'}:
                    effluent[metric] = value_mg_l
            effluent['phosphorus_pool_breakdown_mg_L'] = {pool: float(value) * 1000.0 for pool, value in measurement['phosphorus_pools_kg_m3'].items()}
        rating_result = SolveResult(status='optimal', solution_quality='strict_optimal', solve_seconds=time.monotonic() - started, effluent=effluent, design_parameters_settled=settled, stream_closure={'TN_error_pct': n_err_pct, 'TN': max(0.0, 100.0 - abs(n_err_pct or 0.0)), 'max_constraint_residual': max_residual, 'fractionation_basis': getattr(fractionation, 'closure_errors', {})}, diagnostics={'direct_rating_path': 'modified_asm2d_mbr_fixed_volume', 'sizing_reference': geometry_contract['sizing_reference'], 'geometry_identity': 'verified', 'termination_condition': termination, 'dof': dof, 'max_constraint_residual': max_residual, 'aerobic_stage_count': len(aerobic_units), 'recycle_topology': 'single_mbr_retentate_pressure_changer', REACTION_CONFIG_TARGET: reaction_config_record, **({'biological_topology': 'aerobic_only'} if aerobic_only else {}), **({SIDE_FEEDS_TARGET: [{'role': item['role'], 'flow_m3_d': item['flow_m3_d'], 'enters': 'MBR' if item['into'] is None else item['into'].local_name, 'group_inflow_m3_d': q_in_m3_d, 'ras_ratio_basis': "the group's whole liquid inflow"} for item in side_units]} if side_units else {}), **({'non_ebpr_pin': pin_record} if pin_record is not None else {}), **({'ebpr_steady_state': ebpr_record} if ebpr_record is not None else {}), 'solve_attempts': solve_attempts, **({'initial_point': initial_point} if initial_point is not None else {})}, solver_infeasibility_report=None, flowsheet_svg='', artifact_id='', artifact_path='')
        if design_targets.get('process_balance_native_export') is True:
            from watertap_engine_mcp.utils.process_balance_biology_export import export_modified_asm2d_balance
            rating_result.diagnostics['process_balance_native'] = export_modified_asm2d_balance(m, aerobic_units, membrane_tank=m.fs.MembraneTank if membrane_tank is not None else None)
        return (_apply_physicality_gate(rating_result, model_family='asm2d', influent_components_mg_l={component: feed_conc[component] * 1000.0 for component in components}, mass_balance_error_pct={'TN': n_err_pct} if n_err_pct is not None else None, aerobic_do_mg_l=aerobic.get('S_O2')), srt_d)
    if optimal:
        final_result, _final_srt_d = rated_result()
        _phases.close()
        return _with_nitrifier_recovery(final_result, nitrifier_recovery)
    native_failed_constraints = []
    if design_targets.get('process_balance_native_export') is True:
        from watertap_engine_mcp.utils.process_balance_biology_export import native_biology_constraint_diagnostics
        native_failed_constraints = native_biology_constraint_diagnostics(m)
    failed_attempt = solve_attempts[-1] if solve_attempts else {}
    failure_notes = [f'{attempt['phase']} {attempt['ipopt_exit']}' for attempt in solve_attempts if attempt.get('ipopt_exit') and attempt.get('termination_condition') != 'optimal']
    audit = failed_attempt.get('row_resolution_audit') or {}
    unresolvable = audit.get('unresolvable') or []
    if unresolvable:
        failure_notes.append(f'{len(unresolvable)} row(s) coarser than constr_viol_tol in float64, e.g. {unresolvable[0]['constraint']} (resolution {unresolvable[0]['resolution']:.3g})')
    elif audit.get('coarsest') and audit['coarsest'][0].get('headroom') is not None:
        coarsest = audit['coarsest'][0]
        failure_notes.append(f'coarsest row {coarsest['constraint']} resolves constr_viol_tol with headroom {coarsest['headroom']:.3g}x')
    report = _universal_infeasibility_report(model_family='ModifiedASM2D', solver_status=str(solve_results.solver.status) if solve_results is not None else 'error', termination_condition=termination, message=f'ModifiedASM2d direct MBR rating solve failed: {termination}' + (f' ({native_failure_message})' if native_failure_message else '') + (f'; {'; '.join(failure_notes)}' if failure_notes else ''), remediation=['Check waste_flow_m3_d, recycle ratio, DO target, and fixed reactor volumes.', 'Use the generic structured pipeline only after this direct rating oracle converges.'], diagnostic_basis={'dof': dof, 'waste_flow_m3_d': qwas_m3_d, 'recycle_ratio': recycle_ratio, 'max_constraint_residual': _max_active_constraint_residual(m), 'native_failed_constraints': native_failed_constraints, 'solve_attempts': solve_attempts, **({'initial_point': initial_point} if initial_point is not None else {}), **({'non_ebpr_pin': pin_record} if pin_record is not None else {}), **({'ebpr_steady_state': ebpr_record} if ebpr_record is not None else {}), 'native_solver_log_tail': native_log_path.read_text(errors='replace')[-8000:] if native_log_path is not None and native_log_path.is_file() else None})
    _phases.close()
    return _with_nitrifier_recovery(_universal_empty_result(status='infeasible_structured', solve_seconds=time.monotonic() - started, diagnostics={'direct_rating_path': 'modified_asm2d_mbr_fixed_volume', 'termination_condition': termination, 'dof': dof, REACTION_CONFIG_TARGET: reaction_config_record, 'solve_attempts': solve_attempts, **({'initial_point': initial_point} if initial_point is not None else {}), **({'non_ebpr_pin': pin_record} if pin_record is not None else {}), **({'ebpr_steady_state': ebpr_record} if ebpr_record is not None else {})}, report=report), nitrifier_recovery)

def _max_active_constraint_residual(model: Any) -> float | None:
    """Return the largest absolute residual across active scalar constraints."""
    try:
        import pyomo.environ as pyo
        from pyomo.environ import Constraint
    except Exception:
        return None
    max_residual = 0.0
    seen = False
    for constraint in model.component_data_objects(Constraint, active=True, descend_into=True):
        try:
            body = pyo.value(constraint.body, exception=False)
            if body is None:
                continue
            if constraint.equality:
                target = pyo.value(constraint.lower, exception=False)
                if target is None:
                    continue
                residual = abs(float(body) - float(target))
            else:
                residual = 0.0
                lower = pyo.value(constraint.lower, exception=False)
                upper = pyo.value(constraint.upper, exception=False)
                if lower is not None and body < lower:
                    residual = max(residual, float(lower) - float(body))
                if upper is not None and body > upper:
                    residual = max(residual, float(body) - float(upper))
            max_residual = max(max_residual, residual)
            seen = True
        except Exception:
            continue
    return max_residual if seen else None

def _universal_apply_non_bio_feed_state(*, session_id: str, engine_template_name: str, fractionation: Any, template_config: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
    """Apply TenderInfluentBasis-derived feed states to non-biological templates."""
    feed_unit = 'Feed'
    non_bio = dict(fractionation.non_biological_state)
    flow_m3_hr = float(non_bio.get('flow_m3_d') or 0.0) / 24.0
    temperature_c = float(non_bio.get('temperature_K') or 298.15) - 273.15
    tds = float(non_bio.get('TDS_mg_L') or 0.0)
    mvc_feed_basis: Optional[Dict[str, Any]] = None
    exact_mvc_mass_flows: Optional[Dict[str, float]] = None
    if engine_template_name in {'mvc_evaporator', 'mvc_crystallizer'}:
        brine_outlet_temp_k = float((template_config or {}).get('brine_outlet_temp_K') or 333.15)
        approach_k = float((template_config or {}).get('feed_preheat_approach_K', 10.0))
        design_feed_c = brine_outlet_temp_k - 273.15 - approach_k
        applied_c = max(temperature_c, design_feed_c)
        if applied_c != temperature_c:
            from watertap_engine_mcp.core.seawater_feed import density_from_concentration, specific_enthalpy
            density = density_from_concentration(temperature_c, tds / 1000.0)
            feed_mass_kg_s = flow_m3_hr / 3600.0 * density
            salt_mass_kg_s = flow_m3_hr / 3600.0 * tds / 1000.0
            exact_mvc_mass_flows = {'TDS': salt_mass_kg_s, 'H2O': feed_mass_kg_s - salt_mass_kg_s}
            salinity = salt_mass_kg_s / feed_mass_kg_s
            preheat_kw = feed_mass_kg_s * (specific_enthalpy(applied_c, salinity) - specific_enthalpy(temperature_c, salinity)) / 1000.0
            mvc_feed_basis = {'influent_temperature_C': round(temperature_c, 2), 'evaporator_feed_temperature_C': round(applied_c, 2), 'feed_preheat_approach_K': approach_k, 'implied_feed_preheat_duty_kW': preheat_kw, 'cold_feed_density_kg_m3': density, 'cold_feed_mass_fraction_TDS': salinity, 'preheat_property_basis': 'SEAWATER enthalpy difference at conserved component mass flows', 'basis': f'MVC template has no feed-preheat exchangers; the evaporator feed enters at a {approach_k:g} K approach below the brine outlet temperature, representing the upstream feed/distillate preheat train of a packaged MVC skid.'}
            temperature_c = applied_c
    if engine_template_name in {'ro_train', 'mvc_evaporator', 'mvc_crystallizer'}:
        result = set_feed_state(session_id=session_id, unit_id=feed_unit, flow_vol_m3_hr=flow_m3_hr, tds_mg_L=tds, temperature_C=temperature_c, component_mass_flow_kg_s=exact_mvc_mass_flows)
        if mvc_feed_basis is not None and isinstance(result, dict) and (not result.get('error')):
            result['mvc_feed_basis'] = mvc_feed_basis
        return result
    return {}

def _universal_ro_feed_tds_mg_l(*, basis: Any, fractionation: Any, design_targets: Dict[str, Any]) -> float | None:
    """Resolve the RO feed TDS basis in mg/L from explicit targets or feedstock."""
    tds = _universal_target_float(design_targets, 'feed_tds_mg_l', 'feed_TDS_mg_L', 'TDS_mg_L', 'tds_mg_l', 'TDS')
    if tds is not None:
        return tds
    for attr in ('TDS', 'TDS_mg_L', 'tds_mg_l'):
        value = getattr(basis, attr, None)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    non_bio = dict(getattr(fractionation, 'non_biological_state', {}) or {})
    return _universal_mapping_float(non_bio, 'TDS_mg_L', 'tds_mg_l', 'TDS')

def _universal_high_salinity_backend_status() -> Dict[str, Any]:
    """Return positive availability metadata for high-salinity activity backends."""
    reaktoro_spec = importlib.util.find_spec('reaktoro')
    reaktoro_pse_spec = importlib.util.find_spec('reaktoro_pse')
    oli_spec = importlib.util.find_spec('watertap.tools.oli_api')
    status: Dict[str, Any] = {'selected_backend': None, 'reaktoro_core_available': reaktoro_spec is not None, 'reaktoro_pse_available': reaktoro_pse_spec is not None, 'reaktoro_block_available': False, 'oli_api_hook_available': oli_spec is not None, 'oli_api_oss_backend': False, 'oli_api_basis': 'commercial OLI Cloud/Engine credentials required'}
    if reaktoro_spec is not None:
        try:
            import reaktoro
            status['reaktoro_version'] = getattr(reaktoro, '__version__', 'unknown')
        except Exception as exc:
            status['reaktoro_import_error'] = f'{type(exc).__name__}: {exc}'
    if reaktoro_pse_spec is not None:
        try:
            import reaktoro_pse
            status['reaktoro_pse_version'] = getattr(reaktoro_pse, '__version__', 'unknown')
            from reaktoro_pse.reaktoro_block import ReaktoroBlock
            status['reaktoro_block_available'] = True
        except Exception as exc:
            status['reaktoro_block_import_error'] = f'{type(exc).__name__}: {exc}'
    if status['reaktoro_core_available'] and status['reaktoro_block_available']:
        status['selected_backend'] = 'reaktoro_pse'
    return status

def _universal_ro_property_validity_result(*, started: float, engine_template_name: str, template_config: Dict[str, Any], basis: Any, fractionation: Any, design_targets: Dict[str, Any]) -> SolveResult | None:
    """Reject RO/UHPRO requests that exceed the active package salinity envelope."""
    if engine_template_name != 'ro_train':
        return None
    package = template_config.get('property_package') or PropertyPackageType.SEAWATER
    if not isinstance(package, PropertyPackageType):
        package = _universal_ro_property_package({'property_package': package})
    spec = get_property_package_spec(package)
    ceiling = _universal_target_float(template_config, 'property_validity_ceiling_g_L', 'max_ro_brine_tds_g_L')
    if ceiling is None:
        ceiling = _universal_mapping_float(spec.validity_limits, 'max_ro_brine_tds_g_L')
    if ceiling is None:
        return None
    recovery = _universal_target_float(template_config, 'target_system_recovery', 'target_recovery', 'recovery')
    if recovery is None:
        recovery = _universal_target_float(design_targets, 'target_system_recovery', 'target_recovery', 'recovery', 'ro_recovery')
    if recovery is None:
        return None
    feed_tds_mg_l = _universal_ro_feed_tds_mg_l(basis=basis, fractionation=fractionation, design_targets=design_targets)
    if feed_tds_mg_l is None or feed_tds_mg_l <= 0:
        return None
    if recovery <= 0 or recovery >= 1:
        report = _universal_infeasibility_report(model_family='ro', solver_status='not_started', termination_condition='invalid_recovery_target', message='RO target recovery must be greater than 0 and less than 1.', remediation=['Provide target_system_recovery as a fraction in (0, 1).'], diagnostic_basis={'target_system_recovery': recovery})
        return _universal_empty_result(status='infeasible_structured', solve_seconds=time.monotonic() - started, diagnostics={'target_system_recovery': recovery}, report=report)
    predicted_brine_tds_g_l = float(feed_tds_mg_l) / 1000.0 / (1.0 - float(recovery))
    if predicted_brine_tds_g_l <= float(ceiling):
        return None
    high_salinity_backend = _universal_high_salinity_backend_status()
    report = _universal_infeasibility_report(model_family='ro', solver_status='not_started', termination_condition='invalid_property_range', message=f'Predicted RO brine TDS {predicted_brine_tds_g_l:.1f} g/L exceeds {package.name} validity ceiling {float(ceiling):.1f} g/L.', remediation=['Reduce RO/UHPRO recovery or select a validated high-salinity transport property package.', 'Use PHREEQC/Pitzer scaling checks and hand off brine above the WaterTAP package limit to MVC/crystallizer templates.', 'Install core Reaktoro from conda-forge and implement a differentiable Reaktoro/Pitzer RO transport coupling before lifting this ceiling.'], diagnostic_basis={'property_package': package.name, 'feed_tds_mg_L': float(feed_tds_mg_l), 'target_system_recovery': float(recovery), 'predicted_brine_tds_g_L': predicted_brine_tds_g_l, 'max_ro_brine_tds_g_L': float(ceiling), 'validity_basis': spec.validity_limits.get('basis'), 'high_salinity_backend': high_salinity_backend})
    return _universal_empty_result(status='invalid_property_range', solve_seconds=time.monotonic() - started, diagnostics={'property_package': package.name, 'feed_tds_mg_L': float(feed_tds_mg_l), 'target_system_recovery': float(recovery), 'predicted_brine_tds_g_L': predicted_brine_tds_g_l, 'max_ro_brine_tds_g_L': float(ceiling), 'validity_basis': spec.validity_limits.get('basis'), 'high_salinity_backend': high_salinity_backend}, report=report)

def _refresh_ro_train_solved_geometry(session: FlowsheetSession) -> Dict[str, Any] | None:
    """Verify rated areas; only inverse design may replace area metadata."""
    from watertap_engine_mcp.core.rating_geometry import RatingGeometryError
    metadata = session.config.runtime_metadata.get('ro_train')
    if not isinstance(metadata, dict):
        return None
    solved_state = session.results.get('solved_state') if isinstance(session.results, dict) else None
    units = solved_state.get('units') if isinstance(solved_state, dict) else None
    if not isinstance(units, dict):
        if metadata.get('operating_mode', 'rating') == 'rating':
            raise RatingGeometryError('RO rating area [m2] lacks solved geometry evidence from stage (b)')
        return dict(metadata)
    n_stages = int(metadata.get('n_stages') or 1)
    unit_ids = [f'RO_{stage}' for stage in range(1, n_stages + 1)] if n_stages > 1 else ['RO']
    solved_areas: List[float] = []
    solved_recoveries: List[float] = []
    for unit_id in unit_ids:
        variables = (units.get(unit_id) or {}).get('variables') or {}
        area = _numeric_value(variables.get('area'))
        if area is None:
            if metadata.get('operating_mode', 'rating') == 'rating':
                raise RatingGeometryError(f'{unit_id} rating area [m2] lacks solved geometry evidence from stage (b)')
            return dict(metadata)
        if metadata.get('operating_mode', 'rating') == 'rating':
            expected_areas = metadata.get('stage_area_m2') or []
            index = len(solved_areas)
            if index >= len(expected_areas) or not math.isfinite(area) or (not math.isclose(area, float(expected_areas[index]), rel_tol=1e-12, abs_tol=1e-12)):
                raise RatingGeometryError(f'{unit_id} rating area [m2] changed from stage (b) {expected_areas} to {area}; rating invalid')
        solved_areas.append(area)
        recovery = None
        for variable_name, payload in variables.items():
            if str(variable_name).startswith('recovery_vol_phase['):
                recovery = _numeric_value(payload)
                if recovery is not None:
                    break
        if recovery is not None:
            solved_recoveries.append(recovery)
    refreshed = dict(metadata)
    refreshed['stage_area_m2'] = solved_areas
    refreshed['solved_stage_area_m2'] = solved_areas
    if solved_recoveries:
        refreshed['solved_stage_recovery'] = solved_recoveries
        solved_system_recovery = 1.0 - math.prod((1.0 - recovery for recovery in solved_recoveries))
        refreshed['solved_system_recovery'] = solved_system_recovery
        system_target = refreshed.get('system_recovery_verification_target') if refreshed.get('system_recovery_verification_target') is not None else refreshed.get('target_system_recovery')
        if system_target is not None:
            refreshed['system_recovery_delta'] = solved_system_recovery - float(system_target)
        stage_targets = refreshed.get('stage_recovery_verification_targets') if refreshed.get('stage_recovery_verification_targets') is not None else refreshed.get('stage_recovery_targets')
        if isinstance(stage_targets, list) and len(stage_targets) == len(solved_recoveries):
            refreshed['stage_recovery_deltas'] = [actual - float(target) for actual, target in zip(solved_recoveries, stage_targets)]
    refreshed['geometry_identity'] = 'verified' if refreshed.get('operating_mode', 'rating') == 'rating' else 'not_applicable_inverse_design'
    refreshed['geometry_resnapped_from'] = 'solved_state'
    if refreshed.get('operating_mode') == 'design':
        refreshed['stage_vessels'] = None
        refreshed['elements_per_vessel'] = None
        refreshed['element_area_m2'] = None
    session.config.runtime_metadata['ro_train'] = refreshed
    session_manager.save(session)
    return refreshed
_NITRIFYING_TEMPLATE_FAMILIES: frozenset[str] = frozenset({'asm1', 'asm2d'})

def _aerobic_zone_do_mg_l(session: FlowsheetSession) -> float | None:
    """Return the aerated zone's outlet DO (mg O2/L) from the solved state.

    The DO that governs nitrification is the AEROBIC reactor's, never the plant
    effluent's: an ``asm2d_mle_post`` / ``asm2d_mbr_post`` topology drives
    effluent DO to ~0 in its post-anoxic zone by design. Reads the last aerobic
    stage, matching where the direct-rating path samples DO. Returns None when
    the session carries no solved aerobic outlet, which skips the DO check
    rather than guessing.
    """
    solved_state = (session.results or {}).get('solved_state') if isinstance(session.results, dict) else None
    units = solved_state.get('units', {}) if isinstance(solved_state, dict) else {}
    if not isinstance(units, dict):
        return None
    aerobic_ids = sorted((unit_id for unit_id in units if isinstance(unit_id, str) and unit_id.startswith('Aerobic')))
    if not aerobic_ids:
        return None
    outlet = ((units.get(aerobic_ids[-1]) or {}).get('streams') or {}).get('outlet')
    concentration = _stream_concentration(outlet, 'S_O2')
    return concentration * 1000.0 if concentration is not None else None
NITRIFIER_RESEED_AMMONIUM_MARGINS: Tuple[float, ...] = (1.0, 2.0)

def _physicality_violation_codes(result: SolveResult) -> set[str]:
    physicality = result.physicality or {}
    return {str(violation.get('code')) for violation in physicality.get('violations', []) if isinstance(violation, dict)}

def _nitrifier_solve_evidence(result: SolveResult, termination: str) -> Dict[str, Any]:
    """The few numbers that show which branch a rating solve landed on."""
    settled = result.design_parameters_settled or {}
    effluent = result.effluent or {}
    return {'termination_condition': termination, 'status': result.status, 'solution_quality': result.solution_quality, 'physicality_codes': sorted(_physicality_violation_codes(result)), 'X_AUT_mg_L': settled.get('X_AUT_mg_L'), 'permeate_NH4_N_mg_L': effluent.get('NH4_N_mg_L'), 'permeate_NO3_N_mg_L': effluent.get('NO3_N_mg_L'), 'SRT_actual_d': settled.get('SRT_actual_d')}

def _with_nitrifier_recovery(result: SolveResult, recovery: Dict[str, Any] | None) -> SolveResult:
    """Attach the nitrifier-branch record to the final result, visibly, either way.

    ``recovery`` is None when the base solve was not a washout; the result is
    then returned unchanged. Otherwise the final verdict joins the base-solve
    branch decision with what the (polished, gated) final result is.
    """
    if recovery is None:
        return result
    recovery = dict(recovery)
    accepted = result.status == 'optimal' and result.solution_quality == 'strict_optimal' and ((result.physicality or {}).get('is_physical') is True)
    base = recovery.get('base_verdict')
    if accepted:
        verdict = 'accepted_nitrifying_branch' if base == 'reseeded_nitrifying_branch' else 'accepted_without_reseed'
    else:
        verdict = {'reseeded_nitrifying_branch': 'reseeded_branch_refused', 'washout_stable': 'washout_stable_refused', 'reseed_failed': 'reseed_failed_refused'}.get(base, 'invasion_test_failed_refused')
    recovery['verdict'] = verdict
    recovery['final_solve'] = _nitrifier_solve_evidence(result, str((result.diagnostics or {}).get('termination_condition')))
    eigenvalue = (recovery.get('washout_invasion') or {}).get('invasion_eigenvalue_per_d')
    attempts = recovery.get('attempts') or []
    if verdict == 'accepted_nitrifying_branch':
        message = f'The first solve converged to the X_AUT = 0 washout root, which is UNSTABLE at its own solved state (autotroph invasion eigenvalue {eigenvalue:+.4g}/d); re-solved from a nitrifying-branch seed (attempt {attempts[-1]['attempt']} of {len(NITRIFIER_RESEED_AMMONIUM_MARGINS)}) to a strict-optimal physical state: X_AUT {recovery['final_solve']['X_AUT_mg_L']} mg/L, permeate NH4-N {recovery['final_solve']['permeate_NH4_N_mg_L']} mg/L. Record: diagnostics.nitrifier_branch_recovery.'
    elif verdict == 'washout_stable_refused':
        message = f'Nitrifier washout is STABLE at the solved state (autotroph invasion eigenvalue {eigenvalue:+.4g}/d <= 0): an introduced population dies out, so the washout is the physical answer for this design and remains refused as non-nitrifying.'
    elif verdict in {'reseed_failed_refused', 'reseeded_branch_refused'}:
        message = f"The washout root is unstable (autotroph invasion eigenvalue {eigenvalue:+.4g}/d), but no bounded nitrifying-branch re-solve reached a strict-optimal physical state ({verdict}); refused with both solves' evidence in diagnostics.nitrifier_branch_recovery."
    elif verdict == 'accepted_without_reseed':
        message = 'A washout base solve was not reseeded but the final solve is strict-optimal and physical.'
    else:
        message = f'The nitrifier invasion test could not be evaluated; the washout is refused unchanged ({recovery.get('error')}).'
    diagnostics = dict(result.diagnostics or {})
    diagnostics['nitrifier_branch_recovery'] = recovery
    update: Dict[str, Any] = {'diagnostics': diagnostics, 'warnings': [*(result.warnings or []), message]}
    report = result.solver_infeasibility_report
    if report is not None:
        basis = dict(report.diagnostic_basis or {})
        basis['nitrifier_branch_recovery'] = recovery
        update['solver_infeasibility_report'] = report.model_copy(update={'diagnostic_basis': basis})
    return result.model_copy(update=update)

def _apply_physicality_gate(result: SolveResult, *, model_family: str, influent_components_mg_l: Dict[str, Any] | None=None, mass_balance_error_pct: Dict[str, Any] | None=None, aerobic_do_mg_l: float | None=None) -> SolveResult:
    """Demote a converged-but-non-physical solve off ``status="optimal"``.

    An IPOPT "optimal" termination only asserts that the equations and bounds it
    was given are satisfied — not that the iterate is a physical plant. This gate
    is the last thing between such an iterate and a caller who would consume it
    as a design basis, so a demotion carries a structured infeasibility report
    with remediation, exactly like a genuine solver failure does.

    A passing solve is returned unchanged apart from the ``physicality`` verdict,
    so the checks that ran are always visible.
    """
    from puran_mcp_support.engineering.validation.solution_physicality import check_asm2d_solution_physicality
    family = str(model_family or '').strip().lower()
    verdict = check_asm2d_solution_physicality(effluent=result.effluent, design_parameters_settled=result.design_parameters_settled, influent_components_mg_l=influent_components_mg_l, mass_balance_error_pct=mass_balance_error_pct, nitrification_expected=family in _NITRIFYING_TEMPLATE_FAMILIES, aerobic_do_mg_l=aerobic_do_mg_l)
    diagnostics = dict(result.diagnostics or {})
    diagnostics['physicality'] = verdict.as_dict()
    if verdict.is_physical:
        return result.model_copy(update={'physicality': verdict.as_dict(), 'diagnostics': diagnostics})
    message = f'Solver reported optimal but the converged solution is not physical: {verdict.reason}'
    report = _universal_infeasibility_report(model_family=model_family, solver_status='optimal', termination_condition='non_physical_solution', message=message, remediation=verdict.remediation(), diagnostic_basis={'physicality': verdict.as_dict(), 'note': 'The solve CONVERGED; it was demoted by the post-solve physicality gate, not by the solver.'})
    return result.model_copy(update={'status': 'non_physical', 'solution_quality': 'non_physical', 'physicality': verdict.as_dict(), 'diagnostics': diagnostics, 'solver_infeasibility_report': report, 'warnings': [*(result.warnings or []), message]})

def _adm1_cod_methane_balance(session: Any, builder: Any, engine: str) -> Dict[str, Any]:
    """Public engineering calculation interface."""
    import pyomo.environ as pyo
    from puran_mcp_support.engineering.house_standards.methane_cod import CodMethaneBalanceRefused, require_cod_methane_balance
    from watertap_engine_mcp.results.cod_balance import CodBalanceNotEvaluable, adm1_cod_methane_balance
    from watertap_engine_mcp.results.streams import extract_port_snapshot
    try:
        units = builder.get_units()
        feeds, products, oxidants, deposit = ([], {}, [], 0.0)
        for unit_id, block in units.items():
            unit_type = getattr(session.units.get(unit_id), 'unit_type', None)
            if unit_type == 'Feed':
                feeds.append(extract_port_snapshot(block.outlet))
            elif unit_type == 'Product':
                products[unit_id] = extract_port_snapshot(block.inlet)
            if getattr(block, 'vapor_inlet', None) is not None:
                oxidants.append(extract_port_snapshot(block.vapor_inlet))
            expression = getattr(block, 'headspace_s0_deposit', None)
            if expression is not None:
                t = block.flowsheet().time.first()
                deposit += float(pyo.value(expression[t])) * 86400.0
        closure = adm1_cod_methane_balance(engine, feeds=feeds, products=products, oxidant_inlets=oxidants, s0_deposit_kg_s_d=deposit)
    except CodBalanceNotEvaluable as exc:
        return {'record': {'evaluated': False, 'reason': str(exc)}, 'code': 'cod_methane_balance_not_evaluable', 'message': f'cod_methane_balance_not_evaluable: {exc}'}
    record = closure.as_dict()
    try:
        require_cod_methane_balance(engine, cod_in_kg_d=closure.cod_in_kg_d, cod_out_kg_d=closure.cod_out_kg_d, methane_nm3_d=closure.methane_nm3_d, other_terms_kg_d=dict(closure.other_terms_kg_d), engine_methane_cod_kg_d=closure.engine_methane_cod_kg_d)
    except CodMethaneBalanceRefused as exc:
        return {'record': record, 'code': exc.code, 'message': str(exc)}
    return {'record': record, 'code': None, 'message': None}

def _demote_on_cod_methane_balance(result: SolveResult, gate: Dict[str, Any]) -> SolveResult:
    """A converged ADM1 solve whose COD does not close (or cannot be shown to) is not reported as optimal: demoted to
    ``non_physical`` with a structured report, as the physicality gate demotes. A closing one carries its balance."""
    diagnostics = {**dict(result.diagnostics or {}), 'cod_methane_balance': gate['record']}
    if gate['code'] is None:
        return result.model_copy(update={'diagnostics': diagnostics})
    report = _universal_infeasibility_report(model_family='adm1', solver_status='optimal', termination_condition=gate['code'], message=gate['message'], remediation=['Read diagnostics.cod_methane_balance: every term of COD in = liquid + solids out + methane (0.3502 Nm3/kg COD) + H2, H2S, the S0 deposit and the oxidant consumed, and the residual.', 'A residual beyond the tolerance is a model or extraction defect, never a reason to tune the feed: report it.'], diagnostic_basis={'cod_methane_balance': gate['record'], 'note': 'The solve CONVERGED; it was demoted by the post-solve COD-methane balance gate (PUBLIC-RULE-b60c4e378be9), not by the solver.'})
    return result.model_copy(update={'status': 'non_physical', 'solution_quality': 'non_physical', 'diagnostics': diagnostics, 'solver_infeasibility_report': report, 'warnings': [*(result.warnings or []), gate['message']]})

def _universal_infeasibility_report(*, model_family: str, solver_status: str, termination_condition: str, message: str, remediation: list[str], diagnostic_basis: Dict[str, Any] | None=None):
    from puran_mcp_support.engineering.models.solver_infeasibility_report import SolverInfeasibilityReport
    return SolverInfeasibilityReport(model_family=model_family, solver_status=solver_status, termination_condition=termination_condition, message=message, most_likely_cause=message, suggested_remediation=remediation, diagnostic_basis=diagnostic_basis or {})

def _universal_empty_result(*, status: str, solve_seconds: float, diagnostics: Dict[str, Any], report: Any=None):
    solution_quality = 'infeasible_structured' if status == 'infeasible_structured' else None
    return SolveResult(status=status, solution_quality=solution_quality, solve_seconds=solve_seconds, effluent={}, design_parameters_settled={}, stream_closure={}, diagnostics=diagnostics, solver_infeasibility_report=report, flowsheet_svg='', artifact_id='', artifact_path='')

def _universal_effluent_payload(session: FlowsheetSession) -> Dict[str, Any]:
    terminal = _terminal_stream_payload(session) or {}
    components = _stream_components(terminal)
    stream_id = terminal.get('stream_id')
    payload = {'stream_id': stream_id, 'flow_m3_d': _stream_flow_m3_d(terminal), 'temperature_K': _stream_temperature_k(terminal), 'pressure_kPa': _stream_pressure_kpa(terminal), 'components_mg_L': components}
    from watertap_engine_mcp.utils.effluent_metrics import aggregate_biological_effluent_metrics
    kg_m3 = {key: float(value) / 1000.0 for key, value in components.items()}
    if kg_m3:
        try:
            measurement = aggregate_biological_effluent_metrics(kg_m3, port_id=str(stream_id or 'unresolved_terminal_stream'))
        except ValueError as exc:
            measurement = {'status': 'invalid_state', 'model_family': None, 'port_id': str(stream_id or 'unresolved_terminal_stream'), 'recommendation_eligible': False, 'reason': str(exc)}
        payload['biological_measurement_aggregation'] = measurement
        if measurement['status'] == 'complete':
            metrics = measurement['metrics_kg_m3']
            for metric, value in metrics.items():
                value_mg_l = float(value) * 1000.0
                payload[f'{metric}_mg_L'] = value_mg_l
                if metric in {'COD', 'BOD5', 'TN', 'TKN', 'TP'}:
                    payload[metric] = value_mg_l
            payload['phosphorus_pool_breakdown_mg_L'] = {pool: float(value) * 1000.0 for pool, value in measurement['phosphorus_pools_kg_m3'].items()}
            if measurement['model_family'] == 'watertap_asm2d_family':
                tss_payload = asm2d_to_watertap_tss_equivalent(components)
                payload.update({'TSS': tss_payload['TSS'], 'TSS_mg_L': tss_payload['TSS_mg_L']})
    return {key: value for key, value in payload.items() if value is not None}

def _universal_settled_design_parameters(session: FlowsheetSession) -> Dict[str, Any]:
    srt_constraint = _bio_process_constraint(session)
    asm_bio = _bio_design_diagnostics(session, target_srt=srt_constraint.get('target_days'))
    mlss = asm_bio.get('MLSS')
    x_h = asm_bio.get('X_H')
    x_aut = asm_bio.get('X_AUT')
    x_i = asm_bio.get('X_I')
    mlvss_ratio = None
    try:
        if mlss and float(mlss) > 0:
            mlvss_ratio = (float(x_h or 0.0) + float(x_aut or 0.0)) / float(mlss)
    except (TypeError, ValueError):
        mlvss_ratio = None
    return {'MLSS_settled_mg_L': mlss, 'MLSS_basin_mg_L': asm_bio.get('MLSS_basin_mg_L') or mlss, 'MLSS_cage_mg_L': asm_bio.get('MLSS_cage_mg_L') or asm_bio.get('mbr_mlss_mg_l'), 'MLSS_cage_to_basin_ratio': asm_bio.get('MLSS_cage_to_basin_ratio'), 'SRT_actual_d': asm_bio.get('srt_achieved_d') or asm_bio.get('SRT'), 'MLVSS_MLSS_ratio': mlvss_ratio, 'X_AUT_mg_L': x_aut, 'X_H_mg_L': x_h, 'X_I_mg_L': x_i, 'aerobic_kla_per_hr': asm_bio.get('aerobic_kla_per_hr'), 'recycle_ratios_actual': asm_bio.get('recycle_ratios', {}), 'reactor_volumes_m3': asm_bio.get('reactor_volumes_m3', {})}

def _universal_stream_closure(fractionation: Any) -> Dict[str, Any]:
    if (getattr(fractionation, 'cod_split_basis', {}) or {}).get('source') == 'not_applicable_non_biological':
        return {'status': 'not_applicable_non_biological', 'COD': None, 'TKN': None, 'NO3': None, 'TN': None, 'TP': None, 'basis': 'No biochemical vectors calculated; use solved unit stream/component closure.'}
    closure_errors = fractionation.closure_errors or {}
    asm_errors = closure_errors.get('asm2d', closure_errors)
    adm_errors = closure_errors.get('adm1', {})

    def _pct(err: Any) -> float:
        return max(0.0, 100.0 * (1.0 - float(err or 0.0)))
    tkn_err = float(asm_errors.get('TKN', 0.0))
    no3_err = float(asm_errors.get('NO3', 0.0))
    return {'COD': _pct(asm_errors.get('COD', 0.0)), 'TKN': _pct(tkn_err), 'NO3': _pct(no3_err), 'TN': _pct(max(tkn_err, no3_err)), 'TP': _pct(asm_errors.get('TP', 0.0)), 'fractionation_basis': {'asm2d_relative_errors': asm_errors, 'adm1_relative_errors': adm_errors}}

def _universal_report_from_job(result: Dict[str, Any] | None) -> Dict[str, Any] | None:
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

def _universal_expected_optimal_check(expected_ranges: Dict[str, tuple[float, float]], values: Dict[str, Any]) -> Dict[str, Any]:
    """Return range-check diagnostics for settled design values."""
    checks = {}
    for key, bounds in expected_ranges.items():
        value = values.get(key)
        if value is None:
            checks[key] = {'status': 'not_available', 'expected': list(bounds)}
            continue
        lower, upper = bounds
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            checks[key] = {'status': 'not_numeric', 'value': value, 'expected': list(bounds)}
            continue
        checks[key] = {'status': 'pass' if lower <= numeric <= upper else 'out_of_range', 'value': numeric, 'expected': [lower, upper]}
    return checks

def _adm1_methanogenic_digester(builder: Any, units: Dict[str, Any]) -> Any:
    """Return the methanogenic AD block from a built ADM1 session.

    Resolves the methanogenic reactor by structural role (so a two-stage train
    reports its methane stage, not the acidogenic one, and a recycle family resolves
    its project-named digester) and falls back to the first two-phase AD block, then
    to the canonical ``Digester`` name.
    """

    def _is_ad(unit: Any) -> bool:
        return hasattr(unit, 'liquid_phase') and hasattr(unit, 'vapor_phase') and hasattr(unit, 'initialize_build')
    try:
        from watertap_engine_mcp.core.roles import RoleResolver, UnitRole
        session = getattr(builder, '_session', None)
        if session is not None:
            roles = RoleResolver.from_session(session)
            mid = roles.single_id(UnitRole.ANAEROBIC_METHANOGENIC, units)
            if mid is not None and _is_ad(units.get(mid)):
                return units.get(mid)
    except ValueError:
        raise
    except Exception:
        pass
    for unit in units.values():
        if _is_ad(unit):
            return unit
    return units.get('Digester')

def _adm1_effluent_payload(builder: Any, time: float=0.0) -> Dict[str, Any]:
    """Minimal ADM1 digester effluent payload read straight off the solved model.

    The ASM2D ``_universal_effluent_payload`` fractionates activated-sludge
    components and is wrong for an ADM1 digestate, so the digester path reads its
    liquid effluent flow + the biogas headspace composition directly from the
    solved blocks (the synchronous pipeline does not round-trip results through the
    session store the ASM2D async path reads from).
    """
    from pyomo.environ import value

    def _v(obj: Any) -> Optional[float]:
        try:
            raw = value(obj, exception=False)
        except Exception:
            return None
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None
    units = builder.get_units()
    digester = _adm1_methanogenic_digester(builder, units)
    payload: Dict[str, Any] = {}
    if digester is None:
        return payload
    try:
        props_out = digester.liquid_phase.properties_out[time]
        flow_m3_s = _v(getattr(props_out, 'flow_vol', None))
        reaction_block = None
        try:
            reaction_block = digester.liquid_phase.reactions[time]
        except Exception:
            pass
        payload['liquid_effluent'] = {'flow_m3_d': flow_m3_s * 86400.0 if flow_m3_s is not None else None, 'temperature_K': _v(getattr(props_out, 'temperature', None)), 'pH': _v(getattr(reaction_block, 'pH', None)), 'conc_mass_comp_kg_m3': {j if isinstance(j, str) else j[-1]: _v(props_out.conc_mass_comp[j]) for j in getattr(props_out, 'conc_mass_comp', {})}}
    except Exception:
        pass
    try:
        vapor = digester.vapor_phase[time]
        psat = getattr(vapor, 'pressure_sat', None)
        biogas = {'headspace_pressure_pa': _v(getattr(vapor, 'pressure', None))}
        if psat is not None:
            biogas['partial_pressures_pa'] = {j if isinstance(j, str) else j[-1]: _v(psat[j]) for j in psat}
        payload['biogas'] = biogas
    except Exception:
        pass

    def _is_ad(unit: Any) -> bool:
        return hasattr(unit, 'liquid_phase') and hasattr(unit, 'vapor_phase') and hasattr(unit, 'initialize_build')
    duty_scope = 'sensible_process_duty_to_setpoint_excludes_heat_of_digestion_and_ambient_losses'
    thermal_duties: List[Dict[str, Any]] = []
    for unit_id, unit in units.items():
        if not _is_ad(unit):
            continue
        controlled = bool(getattr(unit, 'adm1_external_heat_vapor_transfer', None))
        if not controlled:
            try:
                ueb = unit.unit_enthalpy_balance[time]
                controlled = not ueb.active
            except Exception:
                controlled = False
        if not controlled:
            continue
        try:
            q_w = _v(unit.liquid_phase.heat[time])
        except Exception:
            q_w = None
        q_kw = q_w / 1000.0 if q_w is not None else None
        if q_kw is None:
            duty_direction = None
            heating_kw = None
            cooling_kw = None
        else:
            duty_direction = 'heating' if q_kw >= 0.0 else 'cooling'
            heating_kw = max(q_kw, 0.0)
            cooling_kw = max(-q_kw, 0.0)
        thermal_duties.append({'unit_id': unit_id, 'q_kw': q_kw, 'duty_direction': duty_direction, 'heating_kw': heating_kw, 'cooling_kw': cooling_kw, 'outlet_temperature_K': _v(unit.liquid_phase.properties_out[time].temperature), 'control': 'temperature_target_heat_duty', 'sign_convention': 'positive=heat_into_reactor(heating); negative=heat_out(cooling)', 'duty_scope': duty_scope})
    if thermal_duties:
        payload['thermal_duties'] = thermal_duties
    return payload

def _solve_built_adm1_session(*, session_id: str, template_label: str, requested_topology_template: str, plant_flow_kld: float, objective_spec: str, design_targets: Dict[str, Any], solver: Optional[str], convergence_budget_seconds: int, project_ref: str, plant_slug: str, workflow_run_id: Optional[str], started: float, topology_source: Optional[str]=None, mineral_chemistry_case: Optional[Dict[str, Any]]=None) -> 'SolveResult':
    """Solve an ALREADY-BUILT mADM1 digester session + classify the basin.

    Shared tail of the two ADM1 build routes: the named-preset path
    (:func:`_solve_adm1_flowsheet`, ``apply_template``) and the designed-graph path
    (:func:`_solve_designed_adm1_graph`, ``compile_role_graph`` /
    ``build_from_topology``). Both produce a session whose runtime metadata already
    carries the ``adm1_credibility`` stamp + ``design_parameters`` (the
    ``Adm1PresetProvider`` bundle), so the solve + reporting are identical; only the
    recorded ``topology_source`` differs.

    Runs the hygiene pipeline with ``allow_near_feasible_asm2d=False`` (ADM1 has no
    near-feasible acceptance path), so a successful solve is always a strict IPOPT
    optimum and a failed one returns an honest structured infeasibility report. When
    ``topology_source`` is given it is threaded onto the universal_solver metadata +
    the result diagnostics; ``None`` (the preset path) leaves the diagnostics shape
    byte-identical to the original ``_solve_adm1_flowsheet``.
    """
    session = session_manager.load(session_id)
    credibility = session.config.runtime_metadata.get('adm1_credibility') or {}
    design_parameters = session.config.runtime_metadata.get('design_parameters') or {}
    universal_solver: Dict[str, Any] = {'topology_template': template_label, 'requested_topology_template': requested_topology_template, 'plant_flow_kld': plant_flow_kld, 'objective_spec': objective_spec, 'design_targets': design_targets, 'family': 'adm1'}
    if topology_source is not None:
        universal_solver['topology_source'] = topology_source
    session.config.runtime_metadata.update({'universal_solver': universal_solver, 'project_ref': project_ref, 'plant_slug': plant_slug, 'workflow_run_id': workflow_run_id})
    session_manager.save(session)
    budget = max(int(convergence_budget_seconds or 300), 1)
    try:
        from watertap_engine_mcp.utils.model_builder import ModelBuilder
        from watertap_engine_mcp.solver.pipeline import HygienePipeline, PipelineConfig
        builder = ModelBuilder(session)
        model = builder.build()
        solver_options = session.config.runtime_metadata.get('solver_options')
        pipeline = HygienePipeline(model, PipelineConfig(solver=solver or 'ipopt', solver_options=solver_options if isinstance(solver_options, dict) else {}, max_wall_time_s=budget, allow_near_feasible_asm2d=False), units=builder.get_units(), session=session)
        result = pipeline.run_all()
    except Exception as exc:
        report = _universal_infeasibility_report(model_family='adm1', solver_status='build_error', termination_condition='adm1_pipeline_error', message=f'ADM1 digester build/solve failed: {exc}', remediation=['Confirm the WaterTAP/IDAES environment and an HSL-grade IPOPT linear solver (ma27) are available for the stiff digester solve.'], diagnostic_basis={'error': str(exc), 'session_id': session_id})
        error_diagnostics: Dict[str, Any] = {'session_id': session_id, 'error': str(exc)}
        if topology_source is not None:
            error_diagnostics['topology_source'] = topology_source
        return _universal_empty_result(status='infeasible_structured', solve_seconds=time.monotonic() - started, diagnostics=error_diagnostics, report=report)
    digester_state = pipeline._digester_state or (result.details or {}).get('digester_state') or {}
    flowsheet_svg = _session_flowsheet_svg(session)
    from watertap_engine_mcp.chemistry.capability import shared_mineral_chemistry_capability
    chemistry_capability = shared_mineral_chemistry_capability()
    base_diagnostics: Dict[str, Any] = {'session_id': session_id, 'analysis_mode': session.config.runtime_metadata.get('analysis_mode'), 'sizing_reference': (session.config.runtime_metadata.get('rating_geometry') or {}).get('sizing_reference'), 'geometry_identity': (result.details or {}).get('geometry_identity'), 'template': template_label, 'engine_template_name': template_label, 'family': 'adm1', 'objective_spec': objective_spec, 'digester_state': digester_state, 'adm1_guild_invasion': getattr(pipeline, '_adm1_guild_invasion', None), 'credibility': credibility, 'hydraulic_retention_time_days': design_parameters.get('hydraulic_retention_time_days'), 'digester_volume_liquid_m3': design_parameters.get('digester_volume_liquid_m3'), 'headspace_pressure_design_Pa': design_parameters.get('headspace_pressure_design_Pa'), 'pipeline_message': result.message, 'shared_mineral_chemistry': chemistry_capability}
    if topology_source is not None:
        base_diagnostics['topology_source'] = topology_source
    warnings: List[str] = []
    if isinstance(digester_state, dict):
        warnings.extend(digester_state.get('warnings', []) or [])
    note = credibility.get('note') if isinstance(credibility, dict) else None
    if note:
        warnings.append(note)
    from watertap_engine_mcp.templates.adm1 import adm1_technology_envelope_from_design_parameters
    technology_fidelity = adm1_technology_envelope_from_design_parameters(design_parameters)
    if technology_fidelity is not None:
        base_diagnostics['technology_fidelity'] = technology_fidelity
        if technology_fidelity['failed_checks']:
            warnings.append('technology_fidelity envelope gate FAILED (' + ', '.join(technology_fidelity['failed_checks']) + f") for technology '{technology_fidelity['technology']}': design_grade_convergence=False — the converged digester basis is outside the routed technology's design envelope.")
    if result.success:
        try:
            built_units = builder.get_units()
            solved_state = _extract_solved_state(built_units, session.units, model=model)
            process_diagnostics = _extract_process_diagnostics(model, built_units, session.units, solved_state, session.config.runtime_metadata)
            results_payload = dict(session.results) if isinstance(session.results, dict) else {}
            results_payload['solved_state'] = solved_state
            results_payload['process_diagnostics'] = process_diagnostics
            session.set_solved(results_payload)
            session_manager.save(session)
        except Exception as exc:
            print(f'Warning: failed to persist ADM1 solved_state/process_diagnostics for session {session_id}: {exc}', file=sys.stderr)
        effluent_payload = _adm1_effluent_payload(builder)
        design_parameters_settled = {'digester_state': digester_state, 'hydraulic_retention_time_days': design_parameters.get('hydraulic_retention_time_days'), 'digester_volume_liquid_m3': design_parameters.get('digester_volume_liquid_m3'), 'digester_volume_vapor_m3': design_parameters.get('digester_volume_vapor_m3')}
        chemistry_handoff_error: Exception | None = None
        if mineral_chemistry_case is not None:
            try:
                from watertap_engine_mcp.chemistry.canonical_handoff import attach_staged_treatment_port, run_canonical_madm1_chemistry_handoff
                chemistry_result = run_canonical_madm1_chemistry_handoff(effluent_payload=effluent_payload, case=mineral_chemistry_case)
                base_diagnostics['mineral_chemistry_case'] = chemistry_result
                if chemistry_result.get('staged_treatment_output') is not None:
                    effluent_payload = attach_staged_treatment_port(effluent_payload, chemistry_result)
                    base_diagnostics['chemistry_treated_port'] = chemistry_result['staged_treatment_output']
                chemistry_capability['canonical_solve_audit_handoff_attached'] = True
                chemistry_capability['canonical_handoff_status'] = chemistry_result.get('status')
            except Exception as exc:
                chemistry_handoff_error = exc
                partial_result = getattr(exc, 'chemistry_result', None)
                failure_diagnostics: Dict[str, Any] = {'status': 'chemistry_handoff_failed', 'error': str(exc), 'recommendation_eligible': False, 'recommendation_blockers': ['requested_canonical_chemistry_handoff_failed']}
                if isinstance(partial_result, Mapping):
                    failure_diagnostics['partial_chemistry_result'] = dict(partial_result)
                base_diagnostics['mineral_chemistry_case'] = failure_diagnostics
                base_diagnostics['biological_solution_quality'] = 'strict_optimal'
                base_diagnostics['combined_request_status'] = 'biological_solve_succeeded_requested_chemistry_failed'
                chemistry_capability['canonical_solve_audit_handoff_attached'] = False
                chemistry_capability['canonical_handoff_status'] = 'chemistry_handoff_failed'
        if chemistry_handoff_error is not None:
            message = 'The ADM1 biological model converged strictly, but the explicitly requested mineral-chemistry handoff failed; the combined result cannot be reported as optimal.'
            warnings.append(f'{message} {chemistry_handoff_error}')
            report = _universal_infeasibility_report(model_family='adm1_with_requested_mineral_chemistry', solver_status='optimal_biology_chemistry_handoff_failed', termination_condition='requested_mineral_chemistry_handoff_failed', message=message, remediation=['Inspect diagnostics.mineral_chemistry_case for the classified chemistry error or partial nonconverged result.', 'Correct the chemistry boundary, numerical controls, or mass-balance defect and rerun the fixed-dose forward calculation.', 'Do not use the biological endpoint as a phosphorus-dose result until the requested chemistry calculation converges and closes.'], diagnostic_basis={'session_id': session_id, 'biological_solution_quality': 'strict_optimal', 'chemistry_error': str(chemistry_handoff_error), 'chemistry_diagnostics': base_diagnostics.get('mineral_chemistry_case')})
            return SolveResult(status='infeasible_structured', solution_quality='infeasible_structured', solve_seconds=time.monotonic() - started, effluent=effluent_payload, design_parameters_settled=design_parameters_settled, stream_closure={}, diagnostics=base_diagnostics, solver_infeasibility_report=report, flowsheet_svg=flowsheet_svg, artifact_id='', artifact_path='', warnings=warnings)
        cod_gate = _adm1_cod_methane_balance(session, builder, f'WaterTAP {template_label}')
        return _demote_on_cod_methane_balance(SolveResult(status='optimal', solution_quality='strict_optimal', solve_seconds=time.monotonic() - started, effluent=effluent_payload, design_parameters_settled=design_parameters_settled, stream_closure={}, diagnostics=base_diagnostics, solver_infeasibility_report=None, flowsheet_svg=flowsheet_svg, artifact_id='', artifact_path='', warnings=warnings), cod_gate)
    failure_details = (result.details or {}).get('stage_result') or result.details or {}
    remediation = ['An HSL-grade IPOPT linear solver (ma27) is required to converge the stiff acid/base + gas-transfer digester.', 'Inspect diagnostics.digester_state for a sour / washout basin and re-feed the digester (alkalinity, HRT, OLR) accordingly.']
    failed_hrt_days = design_parameters.get('hydraulic_retention_time_days')
    failed_volume_m3 = design_parameters.get('digester_volume_liquid_m3')
    if isinstance(failed_volume_m3, (int, float)) and float(failed_volume_m3) > 10000.0 and isinstance(failed_hrt_days, (int, float)):
        remediation.append(f"The digester failed at a large absolute liquid volume ({float(failed_volume_m3):,.0f} m3 at HRT {float(failed_hrt_days):.1f} d). Pass an explicit design-basis HRT via design_targets={{'target_hrt_days': ...}} (e.g. 15 d, a standard mesophilic CSTR value inside the 10-40 d technology envelope) to size a smaller basin: initialization robustness degrades with absolute volume, not digestion physics.")
    report = _universal_infeasibility_report(model_family='adm1', solver_status='pipeline_failed', termination_condition='adm1_solve_not_optimal', message=result.message or 'ADM1 digester solve did not reach optimal.', remediation=remediation, diagnostic_basis={'session_id': session_id, 'failure': failure_details, 'hydraulic_retention_time_days': failed_hrt_days, 'digester_volume_liquid_m3': failed_volume_m3})
    return SolveResult(status='infeasible_structured', solution_quality='infeasible_structured', solve_seconds=time.monotonic() - started, effluent=_adm1_effluent_payload(builder), design_parameters_settled={'digester_state': digester_state}, stream_closure={}, diagnostics=base_diagnostics, solver_infeasibility_report=report, flowsheet_svg=flowsheet_svg, artifact_id='', artifact_path='', warnings=warnings)

def _solve_adm1_flowsheet(*, topology_template: str, plant_flow_kld: float, influent_basis: Optional[Dict[str, Any]], design_targets: Dict[str, Any], objective_spec: str, solver: Optional[str], convergence_budget_seconds: int, project_ref: str, plant_slug: str, workflow_run_id: Optional[str], started: float, mineral_chemistry_case: Optional[Dict[str, Any]]) -> 'SolveResult':
    """Build + solve a WaterTAP ADM1 / mADM1 digester preset (roadmap A4 PR1).

    Synchronous direct-pipeline path (mirrors the A4 oracle): resolve the local
    preset, build its config from the requested flow / design targets, apply the
    template, run the hygiene pipeline, classify the converged basin, and return a
    ``SolveResult`` carrying the basin verdict + the honest steady-state (NOT
    heated-design) credibility. Feedstock->ADM1 fractionation and Stage-(b)/(c)
    dispatch land in PR3; PR1 makes the digester dispatchable end-to-end here.
    """
    from watertap_engine_mcp.templates.registry import get_template_spec
    normalized = str(topology_template or '').strip().lower().replace('-', '_')
    try:
        spec = get_template_spec(normalized)
    except KeyError:
        return _universal_empty_result(status='topology_template_not_found', solve_seconds=time.monotonic() - started, diagnostics={'topology_template': topology_template, 'message': f"ADM1 preset '{topology_template}' is not registered in the WaterTAP template registry."})
    normalized_chemistry_case = None
    if mineral_chemistry_case is not None:
        try:
            from watertap_engine_mcp.chemistry.canonical_handoff import normalize_canonical_madm1_chemistry_case
            normalized_chemistry_case = normalize_canonical_madm1_chemistry_case(mineral_chemistry_case)
        except (KeyError, TypeError, ValueError) as exc:
            report = _universal_infeasibility_report(model_family='adm1', solver_status='not_started', termination_condition='invalid_mineral_chemistry_case', message=f'Invalid mADM1 mineral chemistry handoff: {exc}', remediation=['Supply the exact fixed-dose mADM1 FeCl3 chemistry boundary contract.', 'Use screen_madm1_fe_s_p_steady_state directly for a standalone development screen.'], diagnostic_basis={'mineral_chemistry_case': mineral_chemistry_case})
            return _universal_empty_result(status='infeasible_structured', solve_seconds=time.monotonic() - started, diagnostics={'mineral_chemistry_case': {'status': 'invalid_input', 'error': str(exc), 'recommendation_eligible': False}}, report=report)
    design_targets = dict(design_targets or {})
    if plant_flow_kld:
        design_targets.setdefault('feed_flow_m3_per_day', float(plant_flow_kld))
    if isinstance(influent_basis, dict):
        design_targets.setdefault('_influent_basis_provided', True)
    template_config = _universal_template_config(engine_template_name=normalized, basis=None, fractionation=None, design_targets=design_targets)
    session_id = f'{plant_slug or project_ref or 'universal'}-{normalized}-{uuid.uuid4()}'
    created = apply_template(normalized, session_id=session_id, template_config=template_config, name=f'{normalized}:adm1')
    if 'error' in created:
        report = _universal_infeasibility_report(model_family='adm1', solver_status='build_error', termination_condition='template_instantiation_failed', message=str(created['error']), remediation=['Inspect the ADM1 preset config and feed concentration overrides.'], diagnostic_basis={'created': created, 'template_config': template_config})
        return _universal_empty_result(status='infeasible_structured', solve_seconds=time.monotonic() - started, diagnostics=created, report=report)
    return _solve_built_adm1_session(session_id=session_id, template_label=normalized, requested_topology_template=topology_template, plant_flow_kld=plant_flow_kld, objective_spec=objective_spec, design_targets=design_targets, solver=solver, convergence_budget_seconds=convergence_budget_seconds, project_ref=project_ref, plant_slug=plant_slug, workflow_run_id=workflow_run_id, started=started, topology_source=None, mineral_chemistry_case=normalized_chemistry_case)

def screen_madm1_fe_s_p_steady_state(inlet_mol_m3: Dict[str, float], fecl3_fe_mol_m3_d: Optional[float]=None, commercial_product: Optional[Dict[str, Any]]=None, product_dose: Optional[Dict[str, Any]]=None, plant_flow_m3_d: Optional[float]=None, hydraulic_residence_time_d: float=1.0, solids_retention_time_d: float=30.0, temperature_c: float=25.0, phase_parameters: Optional[Dict[str, Dict[str, float]]]=None, redox_parameters: Optional[Dict[str, Dict[str, float]]]=None, oxidation_parameters: Optional[Dict[str, Dict[str, float]]]=None, aging_parameters: Optional[Dict[str, Dict[str, float]]]=None, surface_parameters: Optional[Dict[str, Dict[str, float]]]=None, ligand_parameters: Optional[Dict[str, float]]=None, activity_coefficients: Optional[Dict[str, float]]=None, solvent_water_activity: float=1.0, continuation_path: Optional[List[float]]=None, target_orthophosphate_p_mg_l: Optional[float]=None) -> Dict[str, Any]:
    """Run the uncommissioned shared mADM1 Fe-S-P fixed-dose steady screen.

    This is a reduced chemistry development surface, not plant TP prediction
    and not the canonical biological ``solve_flowsheet`` path.  It always
    returns ``recommendation_eligible=false`` with explicit blockers.
    """
    from puran_mcp_support.chemistry import coerce_commercial_product_definition, coerce_product_dose
    from watertap_engine_mcp.chemistry.steady_solver import solve_commercial_fecl3_product_screen, solve_fixed_dose_screen
    shared_inputs = dict(inlet_mol_m3=inlet_mol_m3, hydraulic_residence_time_d=hydraulic_residence_time_d, solids_retention_time_d=solids_retention_time_d, temperature_c=temperature_c, phase_parameters=phase_parameters, redox_parameters=redox_parameters, oxidation_parameters=oxidation_parameters, aging_parameters=aging_parameters, surface_parameters=surface_parameters, ligand_parameters=ligand_parameters, activity_coefficients=activity_coefficients, solvent_water_activity=solvent_water_activity, continuation_path=tuple(continuation_path) if continuation_path is not None else (0.0, 0.1, 0.5, 1.0), target_orthophosphate_p_mg_l=target_orthophosphate_p_mg_l)
    product_fields = (commercial_product, product_dose, plant_flow_m3_d)
    if any((value is not None for value in product_fields)):
        if not all((value is not None for value in product_fields)):
            raise ValueError('commercial product mode requires commercial_product, product_dose, and plant_flow_m3_d together')
        if fecl3_fe_mol_m3_d is not None:
            raise ValueError('supply either fecl3_fe_mol_m3_d or the commercial-product dose, not both')
        return solve_commercial_fecl3_product_screen(product=coerce_commercial_product_definition(commercial_product), product_dose=coerce_product_dose(product_dose), plant_flow_m3_d=float(plant_flow_m3_d), **shared_inputs)
    return solve_fixed_dose_screen(fecl3_fe_mol_m3_d=0.0 if fecl3_fe_mol_m3_d is None else fecl3_fe_mol_m3_d, **shared_inputs)

def screen_madm1_fe_s_p_serial_steady_state(train: Dict[str, Any], feed_mol_m3: Dict[str, float], inlet_ph: float, dose_mol_d: Optional[Dict[str, float]]=None, temperature_c: float=25.0, phase_parameters: Optional[Dict[str, Dict[str, float]]]=None, redox_parameters: Optional[Dict[str, Dict[str, float]]]=None, oxidation_parameters: Optional[Dict[str, Dict[str, float]]]=None, aging_parameters: Optional[Dict[str, Dict[str, float]]]=None, surface_parameters: Optional[Dict[str, Dict[str, float]]]=None, ligand_parameters: Optional[Dict[str, float]]=None, activity_coefficients: Optional[Dict[str, float]]=None, solvent_water_activity: float=1.0, species_coverage_fraction: float=0.0, feed_retained_mol_m3: Optional[Dict[str, float]]=None, continuation_path: Optional[List[float]]=None) -> Dict[str, Any]:
    """Run the shared serial Fe-S-P history model as a steady diagnostic.

    The ordered train resolves injection, anaerobic contact, FeS oxidation,
    aerobic contact, and MBR separation with the same reaction IR used by the
    QSDsan dynamic sidecar. It predicts modeled reactive P—not plant TP—and is
    always recommendation-ineligible until native biology feedback, calibration,
    holdout validation, and the independent PHREEQC audit are complete.
    """
    from puran_mcp_support.schema import coerce_serial_chemistry_train
    from watertap_engine_mcp.chemistry.serial_steady import solve_serial_history_steady_state
    return solve_serial_history_steady_state(coerce_serial_chemistry_train(train), feed_mol_m3=feed_mol_m3, inlet_ph=inlet_ph, dose_mol_d=dose_mol_d, temperature_c=temperature_c, phase_parameters=phase_parameters, redox_parameters=redox_parameters, oxidation_parameters=oxidation_parameters, aging_parameters=aging_parameters, surface_parameters=surface_parameters, ligand_parameters=ligand_parameters, activity_coefficients=activity_coefficients, solvent_water_activity=solvent_water_activity, species_coverage_fraction=species_coverage_fraction, feed_retained_mol_m3=feed_retained_mol_m3, continuation_path=tuple(continuation_path) if continuation_path is not None else (0.0, 0.1, 0.5, 1.0))

def screen_madm1_fe_s_p_dose_envelope(inlet_mol_m3: Dict[str, float], fecl3_fe_dose_grid_mol_m3_d: List[float], uncertainty_realizations: List[Dict[str, Any]], target_orthophosphate_p_mg_l: float, hydraulic_residence_time_d: float=1.0, solids_retention_time_d: float=30.0, temperature_c: float=25.0, phase_parameters: Optional[Dict[str, Dict[str, float]]]=None, redox_parameters: Optional[Dict[str, Dict[str, float]]]=None, activity_coefficients: Optional[Dict[str, float]]=None, solvent_water_activity: float=1.0, continuation_path: Optional[List[float]]=None, random_seed: int=0) -> Dict[str, Any]:
    """Run a branch-aware diagnostic FeCl3 dose-grid uncertainty screen.

    This executes the reduced steady chemistry block for caller-supplied
    uncertain realizations. It reports a screening envelope, never a plant TP
    recommendation, and refuses to bridge solver errors or nonmonotonic branch
    segments.
    """
    from puran_mcp_support.chemistry import UncertaintyRealization
    from watertap_engine_mcp.chemistry.steady_solver import solve_fixed_dose_uncertainty_screen
    realizations = tuple((UncertaintyRealization(realization_id=str(row.get('realization_id') or ''), parameters=row.get('parameters') or {}) for row in uncertainty_realizations))
    return solve_fixed_dose_uncertainty_screen(inlet_mol_m3=inlet_mol_m3, fecl3_fe_dose_grid_mol_m3_d=tuple(fecl3_fe_dose_grid_mol_m3_d), uncertainty_realizations=realizations, target_orthophosphate_p_mg_l=target_orthophosphate_p_mg_l, hydraulic_residence_time_d=hydraulic_residence_time_d, solids_retention_time_d=solids_retention_time_d, temperature_c=temperature_c, phase_parameters=phase_parameters, redox_parameters=redox_parameters, activity_coefficients=activity_coefficients, solvent_water_activity=solvent_water_activity, continuation_path=tuple(continuation_path) if continuation_path is not None else (0.0, 0.1, 0.5, 1.0), random_seed=random_seed)

def _kinetics_refusal(exc, *, analysis_mode='rating', termination_condition='modified_asm2d_kinetics_refused'):
    message = str(exc)
    return _universal_empty_result(status='infeasible_structured', solve_seconds=0.0, diagnostics={'analysis_mode': analysis_mode, 'kinetics_refusal': message}, report=_universal_infeasibility_report(model_family='asm2d', solver_status='not_started', termination_condition=termination_condition, message=message, remediation=[message], diagnostic_basis={'analysis_mode': analysis_mode, 'ruling': TEMPLATE_KINETICS_RULING_ID}))

def _geometry_refusal(exc, *, analysis_mode='rating'):
    message = str(exc)
    return _universal_empty_result(status='infeasible_structured', solve_seconds=0.0, diagnostics={'analysis_mode': analysis_mode, 'geometry_contract_error': message}, report=_universal_infeasibility_report(model_family='geometry_contract', solver_status='not_started_or_invalidated', termination_condition='geometry_contract_violation', message=message, remediation=[message], diagnostic_basis={'analysis_mode': analysis_mode}))

def solve_flowsheet(topology_template: str, feedstock_class: str, plant_flow_kld: float, influent_basis: Optional[Dict[str, Any]]=None, design_targets: Optional[Dict[str, Any]]=None, objective_spec: str='feasibility', solver: Optional[str]=None, feedstock_overrides: Optional[Dict[str, Any]]=None, mineral_chemistry_case: Optional[Dict[str, Any]]=None, convergence_budget_seconds: int=300, project_ref: str='', plant_slug: str='', workflow_run_id: Optional[str]=None) -> SolveResult:
    """Rate stage-(b) equipment; absent geometry never requests inverse design.

    design_targets must carry geometry plus sizing_reference (artifact_id and
    content_hash), seed_bindings (geometry field -> output key), and sizing_outputs
    (resolved output key -> {value, unit}). Array fields bind each indexed leaf.
    Geometry must retain its value and fixed status through the entire solve.
    """
    arguments = locals().copy()
    from watertap_engine_mcp.core.rating_geometry import RatingGeometryError
    from watertap_engine_mcp.solver.jacobian_autoscale import install as install_jacobian_autoscale, record as jacobian_autoscale_record
    install_jacobian_autoscale()
    record: Dict[str, Any] = {}
    try:
        result = _solve_flowsheet(**arguments, _record=record)
    except RatingGeometryError as exc:
        return _geometry_refusal(exc)
    except ModifiedAsm2dKineticsRefused as exc:
        return _kinetics_refusal(exc)
    except ReactionConfigRefused as exc:
        return _kinetics_refusal(exc, termination_condition='reaction_config_refused')
    return result.model_copy(update={'diagnostics': {**result.diagnostics, 'analysis_mode': 'rating', 'sizing_reference': (design_targets or {}).get('sizing_reference'), 'jacobian_autoscale': jacobian_autoscale_record(), **record}})

def _inverse_design_template_config(template_name, config):
    from watertap_engine_mcp.core.rating_geometry import RatingGeometryError, require_number, template_geometry
    config = deepcopy(config)
    if template_name == 'ro_train':
        config['operating_mode'] = 'design'
        if config.get('target_system_recovery') is None and config.get('stage_recovery_targets') is None:
            raise RatingGeometryError('ro_train inverse design requires target_system_recovery or stage_recovery_targets [fraction]')
        if config.get('membrane_area_m2') is not None or config.get('stage_vessels') is not None:
            raise RatingGeometryError('ro_train inverse design solves area; use solve_flowsheet to rate supplied stage (b) geometry')
    elif template_name == 'mvc_evaporator':
        if config.get('target_brine_mass_fraction') is None:
            raise RatingGeometryError('mvc_evaporator inverse design requires target_brine_mass_fraction [kg/kg]')
        if config.get('evaporator_area_m2') is not None:
            raise RatingGeometryError('mvc_evaporator inverse design solves area; use solve_flowsheet to rate supplied stage (b) geometry')
    elif template_name == 'mvc_crystallizer':
        for field, unit in (('evaporator_area_m2', 'm2'), ('crystal_median_length_m', 'm'), ('crystallization_yield', 'fraction')):
            config[field] = require_number(config.get(field), field, unit, 'mvc_crystallizer inverse design')
        if config['crystallization_yield'] >= 1:
            raise RatingGeometryError('mvc_crystallizer inverse design requires crystallization_yield [fraction] below 1')
    elif template_name.startswith('asm2d'):
        template_geometry(template_name, {**config, 'reactor_volumes_fixed': True})
        config['reactor_volumes_fixed'] = False
    else:
        raise RatingGeometryError(f'{template_name}: no inverse design formulation is registered; the owner must select one explicitly')
    return config

def design_flowsheet(topology_template: str, feedstock_class: str, plant_flow_kld: float, design_targets: Dict[str, Any], influent_basis: Optional[Dict[str, Any]]=None, solver: Optional[str]=None, convergence_budget_seconds: int=300, project_ref: str='', plant_slug: str='', workflow_run_id: Optional[str]=None, objective_spec: str='feasibility') -> SolveResult:
    """Explicit inverse design; result is not a rating of stage-(b) equipment.

    Supply a recovery target for RO or target_brine_mass_fraction for MVC.
    Designed areas require a new stage-(b) specification before they can be rated.
    """
    arguments = locals().copy()
    from watertap_engine_mcp.core.rating_geometry import RatingGeometryError
    try:
        if topology_template not in {'ro_train', 'mvc_evaporator', 'mvc_crystallizer'} and (not topology_template.startswith('asm2d')):
            raise RatingGeometryError('design_flowsheet supports ro_train, mvc_evaporator, mvc_crystallizer and asm2d families')
        if topology_template.startswith('asm2d') and objective_spec == 'feasibility':
            raise RatingGeometryError('Biological inverse design requires an explicit cost objective_spec; feasibility is a rating request')
        record: Dict[str, Any] = {}
        result = _solve_flowsheet(**arguments, _inverse_design=True, _record=record)
    except RatingGeometryError as exc:
        return _geometry_refusal(exc, analysis_mode='inverse_design')
    except ModifiedAsm2dKineticsRefused as exc:
        return _kinetics_refusal(exc, analysis_mode='inverse_design')
    except ReactionConfigRefused as exc:
        return _kinetics_refusal(exc, analysis_mode='inverse_design', termination_condition='reaction_config_refused')
    return result.model_copy(update={'diagnostics': {**result.diagnostics, 'analysis_mode': 'inverse_design', **record}})

def _solve_flowsheet(topology_template: str, feedstock_class: str, plant_flow_kld: float, influent_basis: Optional[Dict[str, Any]]=None, design_targets: Optional[Dict[str, Any]]=None, objective_spec: str='feasibility', solver: Optional[str]=None, feedstock_overrides: Optional[Dict[str, Any]]=None, mineral_chemistry_case: Optional[Dict[str, Any]]=None, convergence_budget_seconds: int=300, project_ref: str='', plant_slug: str='', workflow_run_id: Optional[str]=None, _inverse_design: bool=False, _record: Optional[Dict[str, Any]]=None) -> SolveResult:
    """Solve a topology/feedstock/design-target combination through one server-owned tool.

    ``_record`` receives what the wrapper puts on every result's diagnostics (the template path's kinetic set).
    """
    from puran_mcp_support.engineering.heuristics.feedstock_classes.registry import get_feedstock_class
    from puran_mcp_support.engineering.heuristics.flowsheet_templates.registry import get_topology_template
    from puran_mcp_support.engineering.models.solver_infeasibility_report import SolverInfeasibilityReport
    started = time.monotonic()
    if workflow_run_id is not None:
        try:
            workflow_run_id = str(uuid.UUID(str(workflow_run_id)))
        except (TypeError, ValueError, AttributeError):
            return _universal_empty_result(status='invalid_workflow_run_id', solve_seconds=time.monotonic() - started, diagnostics={'message': 'workflow_run_id must be a UUID'})
    design_targets = dict(design_targets or {})
    objective_spec = str(objective_spec or 'feasibility').strip().lower()
    valid_objectives = {'feasibility', 'min_capex', 'min_opex', 'min_lcow', 'min_total_annualized_cost'}
    if objective_spec not in valid_objectives:
        report = _universal_infeasibility_report(model_family='universal', solver_status='not_started', termination_condition='unsupported_objective_spec', message=f"objective_spec '{objective_spec}' is not supported. Valid modes: {sorted(valid_objectives)}", remediation=["Use objective_spec='feasibility' for the existing feasibility solve.", 'Use one of min_capex, min_opex, min_lcow, or min_total_annualized_cost for post-feasibility optimization.'], diagnostic_basis={'objective_spec': objective_spec})
        return _universal_empty_result(status='infeasible_structured', solve_seconds=time.monotonic() - started, diagnostics={'objective_spec': objective_spec}, report=report)
    if str(topology_template or '').strip().lower().replace('-', '_').startswith('adm1'):
        return _solve_adm1_flowsheet(topology_template=topology_template, plant_flow_kld=plant_flow_kld, influent_basis=influent_basis, design_targets=design_targets, objective_spec=objective_spec, solver=solver, convergence_budget_seconds=convergence_budget_seconds, project_ref=project_ref, plant_slug=plant_slug, workflow_run_id=workflow_run_id, started=started, mineral_chemistry_case=mineral_chemistry_case)
    try:
        template_spec = get_topology_template(topology_template)
    except KeyError:
        from watertap_engine_mcp.flowsheets.zld import get_zld_topology_template
        template_spec = get_zld_topology_template(topology_template)
        if template_spec is None:
            return _universal_empty_result(status='topology_template_not_found', solve_seconds=time.monotonic() - started, diagnostics={'topology_template': topology_template, 'message': f"Topology template '{topology_template}' is not registered."})
    resolved_engine_template = str(getattr(template_spec, 'mcp_template_name', '') or '')
    if resolved_engine_template.strip().lower().replace('-', '_').startswith('adm1'):
        return _solve_adm1_flowsheet(topology_template=resolved_engine_template, plant_flow_kld=plant_flow_kld, influent_basis=influent_basis, design_targets=design_targets, objective_spec=objective_spec, solver=solver, convergence_budget_seconds=convergence_budget_seconds, project_ref=project_ref, plant_slug=plant_slug, workflow_run_id=workflow_run_id, started=started, mineral_chemistry_case=mineral_chemistry_case)
    if mineral_chemistry_case is not None:
        report = _universal_infeasibility_report(model_family=str(getattr(template_spec, 'family', 'universal')), solver_status='not_started', termination_condition='unsupported_mineral_chemistry_handoff_model', message=f"mineral_chemistry_case is currently implemented only for the canonical mADM1 solve path; it must not be silently ignored by topology '{topology_template}'.", remediation=['Use an adm1_* or anaerobic_* topology for the first FeCl3/Fe-S-P vertical slice.', 'Wait for the later ASM2d ferric/alum chemistry adapter before requesting this handoff on an aerobic topology.'], diagnostic_basis={'topology_template': topology_template, 'resolved_engine_template': resolved_engine_template, 'mineral_chemistry_case': mineral_chemistry_case})
        return _universal_empty_result(status='infeasible_structured', solve_seconds=time.monotonic() - started, diagnostics={'mineral_chemistry_case': {'status': 'unsupported_model_family', 'recommendation_eligible': False}}, report=report)
    try:
        feedstock_spec = get_feedstock_class(feedstock_class)
    except KeyError:
        return _universal_empty_result(status='feedstock_class_not_found', solve_seconds=time.monotonic() - started, diagnostics={'feedstock_class': feedstock_class, 'message': f"Feedstock class '{feedstock_class}' is not registered."})
    if template_spec.family not in feedstock_spec.compatible_template_families:
        report = _universal_infeasibility_report(model_family=template_spec.family, solver_status='not_started', termination_condition='incompatible_template_feedstock_pair', message=f"Topology template '{template_spec.name}' is not compatible with feedstock class '{feedstock_spec.name}'.", remediation=['Select a topology_template whose family is listed in the feedstock class compatibility registry.', 'Add a tested registry compatibility entry only if this pairing is physically valid.'], diagnostic_basis={'topology_template': template_spec.name, 'template_family': template_spec.family, 'feedstock_class': feedstock_spec.name, 'compatible_template_families': list(feedstock_spec.compatible_template_families)})
        persistence = {}
        if project_ref:
            persistence = persist_solver_infeasibility_report(report.model_dump(mode='python'), project_ref=project_ref, plant_slug=plant_slug or None, session_id=None)
        return _universal_empty_result(status='infeasible_structured', solve_seconds=time.monotonic() - started, diagnostics={'topology_template': template_spec.name, 'feedstock_class': feedstock_spec.name, 'artifact_persistence': persistence}, report=report)
    basis = feedstock_spec.basis(flow_m3_d=plant_flow_kld, influent_basis=influent_basis, overrides=feedstock_overrides)
    include_biological = template_spec.mcp_template_name not in {'ro_train', 'mvc_evaporator', 'mvc_crystallizer'}
    explicit_native_feed = bool((design_targets or {}).get('feed_conc_kg_m3') or (design_targets or {}).get('modified_asm2d_feed_conc_kg_m3'))
    fractionation = feedstock_spec.fractionate(flow_m3_d=plant_flow_kld, influent_basis=basis, overrides=feedstock_overrides, include_biological=include_biological and (not explicit_native_feed))
    if include_biological and (not explicit_native_feed):
        feedstock_spec.closure_validator(fractionation)
    engine_template_name = template_spec.mcp_template_name
    if not engine_template_name:
        report = _universal_infeasibility_report(model_family=template_spec.family, solver_status='not_started', termination_condition='registered_template_without_engine_builder', message=f"Topology template '{template_spec.name}' is registered but does not yet have a validated WaterTAP engine builder.", remediation=['Add the template builder, bounds_provider, scaling_provider, and initializer to the template registry.', 'Until then, route this tender to a registered mcp_template_name topology with equivalent process intent.'], diagnostic_basis={'topology_template': topology_template, 'canonical_template': template_spec.name, 'feedstock_class': feedstock_spec.name})
        return _universal_empty_result(status='infeasible_structured', solve_seconds=time.monotonic() - started, diagnostics={'template': template_spec.name, 'feedstock': feedstock_spec.name, 'fractionation_closure': fractionation.closure_errors}, report=report)
    template_config = _universal_template_config(engine_template_name=engine_template_name, basis=basis, fractionation=fractionation, design_targets=design_targets, _inverse_design=_inverse_design)
    kinetics_record = template_config.pop(MODIFIED_ASM2D_KINETICS_KEY, None)
    if kinetics_record is not None and _record is not None:
        _record[MODIFIED_ASM2D_KINETICS_KEY] = kinetics_record
    property_validity_result = _universal_ro_property_validity_result(started=started, engine_template_name=engine_template_name, template_config=template_config, basis=basis, fractionation=fractionation, design_targets=design_targets)
    if property_validity_result is not None:
        return property_validity_result
    direct_rating_result = None
    if not _inverse_design:
        direct_rating_result = _modified_asm2d_mbr_rating_direct_result(started=started, engine_template_name=engine_template_name, template_config=template_config, design_targets=design_targets, fractionation=fractionation, kinetics_record=kinetics_record)
    if direct_rating_result is not None:
        return direct_rating_result
    stated_reaction_config = session_reaction_config(design_targets.get(REACTION_CONFIG_TARGET))
    if stated_reaction_config is not None:
        template_config[REACTION_CONFIG_TARGET] = stated_reaction_config
    if design_targets.get(SIDE_FEEDS_TARGET) is not None:
        report = _universal_infeasibility_report(model_family=template_spec.family, solver_status='not_started', termination_condition='native_side_feeds_rate_on_the_direct_path', message=f'{SIDE_FEEDS_TARGET} is built on the direct ModifiedASM2d MBR rating only ({engine_template_name} with model_variant modified_asm2d, combined_mbr_recycle and a stated waste_flow_m3_d); the generic pipeline takes one feed and would drop it.', remediation=['Rate on the direct path, or dose the reagent with the feed.'], diagnostic_basis={'topology_template': template_spec.name, 'inverse_design': bool(_inverse_design)})
        return _universal_empty_result(status='infeasible_structured', solve_seconds=time.monotonic() - started, diagnostics={'template': template_spec.name, 'direct_rating_path': 'not_taken'}, report=report)
    if design_targets.get(MEMBRANE_TANK_TARGET) is not None:
        report = _universal_infeasibility_report(model_family=template_spec.family, solver_status='not_started', termination_condition='membrane_tank_reactor_rates_on_the_direct_path', message=f'{MEMBRANE_TANK_TARGET} is built on the direct ModifiedASM2d MBR rating only ({engine_template_name} with model_variant modified_asm2d, combined_mbr_recycle and a stated waste_flow_m3_d); the generic pipeline has no membrane-tank reactor and would drop it.', remediation=['Rate on the direct path, or state no membrane_tank_reactor (the algebraic split).'], diagnostic_basis={'topology_template': template_spec.name, 'inverse_design': bool(_inverse_design)})
        return _universal_empty_result(status='infeasible_structured', solve_seconds=time.monotonic() - started, diagnostics={'template': template_spec.name, 'direct_rating_path': 'not_taken'}, report=report)
    if engine_template_name == 'asm2d_mbr_aerobic':
        report = _universal_infeasibility_report(model_family=template_spec.family, solver_status='not_started', termination_condition='aerobic_only_mbr_rates_on_the_direct_path', message='asm2d_mbr_aerobic rates on the direct ModifiedASM2d fixed-volume path only; the generic ASM2d pipeline (target SRT, no stated WAS flow) does not build it.', remediation=["State model_variant modified_asm2d, waste_flow_m3_d [m3/d] and aerobic_stage_volumes_m3 [m3]; hold an SRT target on the WAS flow outside the solve (the walk's SRT hold)."], diagnostic_basis={'topology_template': template_spec.name, 'inverse_design': bool(_inverse_design)})
        return _universal_empty_result(status='infeasible_structured', solve_seconds=time.monotonic() - started, diagnostics={'template': template_spec.name, 'direct_rating_path': 'not_taken'}, report=report)
    session_id = f'{plant_slug or project_ref or 'universal'}-{template_spec.name}-{uuid.uuid4()}'
    created = (apply_design_template if _inverse_design else apply_template)(engine_template_name, session_id=session_id, template_config=template_config, name=f'{template_spec.name}:{feedstock_spec.name}')
    if 'error' in created:
        report = _universal_infeasibility_report(model_family=template_spec.family, solver_status='build_error', termination_condition='template_instantiation_failed', message=str(created['error']), remediation=['Inspect the registered template config and feedstock fractionation payload.', 'Fix the template registry entry if instantiation has nonzero DOF or invalid defaults.'], diagnostic_basis={'created': created, 'template_config': template_config})
        return _universal_empty_result(status='infeasible_structured', solve_seconds=time.monotonic() - started, diagnostics=created, report=report)
    session = session_manager.load(session_id)
    session.config.runtime_metadata.update({'universal_solver': {'topology_template': template_spec.name, 'requested_topology_template': topology_template, 'feedstock_class': feedstock_spec.name, 'plant_flow_kld': plant_flow_kld, 'template_bounds': template_spec.bounds, 'convergence_budget_seconds': convergence_budget_seconds, 'objective_spec': objective_spec, 'design_targets': design_targets}, 'project_ref': project_ref, 'plant_slug': plant_slug, 'workflow_run_id': workflow_run_id})
    session_manager.save(session)
    mvc_feed_basis: Optional[Dict[str, Any]] = None
    if not engine_template_name.startswith('asm2d'):
        feed_result = _universal_apply_non_bio_feed_state(session_id=session_id, engine_template_name=engine_template_name, fractionation=fractionation, template_config=template_config)
        if isinstance(feed_result, dict):
            mvc_feed_basis = feed_result.get('mvc_feed_basis')
        if isinstance(feed_result, dict) and feed_result.get('error'):
            report = _universal_infeasibility_report(model_family=template_spec.family, solver_status='build_error', termination_condition='feedstock_fractionation_failed', message=str(feed_result['error']), remediation=['Check the feedstock class non-biological ion/TDS defaults and template feed package.', 'Add a feedstock override for TDS, hardness, or ion breakdown if the tender basis is atypical.'], diagnostic_basis={'feed_result': feed_result})
            return _universal_empty_result(status='infeasible_structured', solve_seconds=time.monotonic() - started, diagnostics=feed_result, report=report)
    solver_options = {'tol': 1e-06, 'acceptable_tol': 1e-06, 'bound_relax_factor': 1e-08, 'max_iter': 3000}
    job_payload = build_and_solve(session_id=session_id, solver=solver, solver_options=solver_options, project_ref=project_ref, plant_slug=plant_slug, workflow_run_id=workflow_run_id, max_wall_time_s=convergence_budget_seconds)
    if 'error' in job_payload:
        report = _universal_infeasibility_report(model_family=template_spec.family, solver_status='build_error', termination_condition='pipeline_submission_failed', message=str(job_payload['error']), remediation=['Fix the session build errors reported by the WaterTAP server-owned pipeline.'], diagnostic_basis={'job_payload': job_payload})
        return _universal_empty_result(status='infeasible_structured', solve_seconds=time.monotonic() - started, diagnostics=job_payload, report=report)
    job_id = job_payload['job_id']
    budget = max(int(convergence_budget_seconds or 300), 1)
    terminal_job = None
    while time.monotonic() - started < budget:
        terminal_job = job_manager.get_status(job_id)
        if terminal_job is not None and terminal_job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
            break
        time.sleep(0.5)
    else:
        job_manager.cancel(job_id)
        return SolveResult(status='convergence_budget_exceeded', solve_seconds=time.monotonic() - started, effluent={}, design_parameters_settled={}, stream_closure=_universal_stream_closure(fractionation), diagnostics={'job_id': job_id, 'session_id': session_id, 'template': template_spec.name, 'feedstock': feedstock_spec.name, 'message': f'Convergence budget of {budget}s was exceeded.'}, solver_infeasibility_report=None, flowsheet_svg='', artifact_id='', artifact_path='')
    job_result = terminal_job.result if terminal_job is not None else None
    built_kinetics = job_result.get(MODIFIED_ASM2D_KINETICS_KEY) if isinstance(job_result, dict) else None
    if kinetics_record is not None and isinstance(built_kinetics, dict):
        kinetics_record['built_reaction_parameters'] = built_kinetics.get('built_reaction_parameters')
    if _record is not None and isinstance(job_result, dict) and isinstance(job_result.get(REACTION_CONFIG_TARGET), dict):
        _record[REACTION_CONFIG_TARGET] = job_result[REACTION_CONFIG_TARGET]
    session = session_manager.load(session_id)
    ro_train_geometry = _refresh_ro_train_solved_geometry(session) if engine_template_name == 'ro_train' and session.status.value == 'solved' else None
    report_payload = build_simulation_report_payload(session_id=session_id, project_ref=project_ref or 'unknown', plant_slug=plant_slug or None, workflow_run_id=workflow_run_id)
    report_ok = isinstance(report_payload, dict) and 'error' not in report_payload
    engine_diagnostics = {}
    flowsheet_svg = _session_flowsheet_svg(session)
    if report_ok:
        engine_report = (report_payload.get('engine_reports') or [{}])[0]
        engine_diagnostics = engine_report.get('diagnostics') or {}
        flowsheet_svg = report_payload.get('flowsheet_svg') or flowsheet_svg
    artifact_persistence = job_result.get('artifact_persistence', {}) if isinstance(job_result, dict) else {}
    artifact_id = str(artifact_persistence.get('simulation_report_artifact_id') or '')
    mirror = artifact_persistence.get('filesystem_mirror')
    artifact_path = str((mirror or {}).get('simulation_report') or '') if isinstance(mirror, dict) else ''
    history = job_result.get('history', []) if isinstance(job_result, dict) else []
    from watertap_engine_mcp.chemistry.capability import shared_mineral_chemistry_capability
    base_diagnostics = {'job_id': job_id, 'session_id': session_id, 'template': template_spec.name, 'engine_template_name': engine_template_name, 'analysis_mode': 'inverse_design' if _inverse_design else 'rating', 'sizing_reference': template_config.get('sizing_reference'), 'feedstock': feedstock_spec.name, 'objective_spec': objective_spec, 'fractionation_closure': fractionation.closure_errors, 'fractionation_closure_applied': not explicit_native_feed, 'cod_split_basis': getattr(fractionation, 'cod_split_basis', {}), 'pipeline_history': history, 'registry_bounds': template_spec.bounds, 'shared_mineral_chemistry': shared_mineral_chemistry_capability(), 'topology_source': str(session.config.runtime_metadata.get('topology_source') or 'preset_fallback'), **engine_diagnostics}
    if ro_train_geometry is not None:
        base_diagnostics['ro_train_geometry'] = ro_train_geometry
    if mvc_feed_basis is not None:
        base_diagnostics['mvc_feed_basis'] = mvc_feed_basis
    pressure_reference = _asm2d_pressure_reference_payload(session)
    if not pressure_reference and isinstance(job_result, dict):
        pressure_reference = job_result.get('asm2d_pressure_reference') if isinstance(job_result.get('asm2d_pressure_reference'), dict) else {}
    if pressure_reference:
        base_diagnostics['asm2d_pressure_reference'] = pressure_reference
    if isinstance(job_result, dict):
        details = job_result.get('details')
        if isinstance(details, dict) and isinstance(details.get('optimization'), dict):
            base_diagnostics['optimization'] = details['optimization']
            if isinstance(details['optimization'].get('optimization_could_not_improve'), dict):
                base_diagnostics['optimization_could_not_improve'] = details['optimization']['optimization_could_not_improve']
        if isinstance(details, dict) and details.get('failed_stage') == 'Optimization':
            stage_result = details.get('stage_result')
            if isinstance(stage_result, dict):
                base_diagnostics['optimization'] = stage_result
                if isinstance(stage_result.get('optimization_could_not_improve'), dict):
                    base_diagnostics['optimization_could_not_improve'] = stage_result['optimization_could_not_improve']
        if isinstance(job_result.get('costing'), dict):
            base_diagnostics['costing'] = job_result['costing']
    if not report_ok:
        base_diagnostics['simulation_report_error'] = report_payload.get('error') if isinstance(report_payload, dict) else report_payload
    if terminal_job is not None and terminal_job.status == JobStatus.COMPLETED and (session.status.value == 'solved'):
        settled = _universal_settled_design_parameters(session)
        expected_check = _universal_expected_optimal_check(template_spec.get_expected_optimal_ranges(feedstock_spec.name), {'MLSS_basin_mg_L': settled.get('MLSS_basin_mg_L') or settled.get('MLSS_settled_mg_L'), 'MLSS_cage_mg_L': settled.get('MLSS_cage_mg_L'), 'MLSS_cage_to_basin_ratio': settled.get('MLSS_cage_to_basin_ratio'), 'SRT_d': settled.get('SRT_actual_d'), **(settled.get('recycle_ratios_actual') or {})})
        base_diagnostics['expected_optimal_check'] = expected_check
        solution_quality, residual_basis = _solution_quality_for_session(session)
        optimal_result = SolveResult(status='optimal', solution_quality=solution_quality, residual_basis=residual_basis, solve_seconds=time.monotonic() - started, effluent=_universal_effluent_payload(session), design_parameters_settled=settled, stream_closure=_universal_stream_closure(fractionation), diagnostics=base_diagnostics, solver_infeasibility_report=None, flowsheet_svg=flowsheet_svg, artifact_id=artifact_id, artifact_path=artifact_path, warnings=list(getattr(fractionation, 'warnings', ()) or ()))
        return _apply_physicality_gate(optimal_result, model_family=template_spec.family, influent_components_mg_l={name: float(value) * 1000.0 for name, value in ((design_targets or {}).get('feed_conc_kg_m3') or (design_targets or {}).get('modified_asm2d_feed_conc_kg_m3') if explicit_native_feed else fractionation.asm2d_state or {}).items()}, aerobic_do_mg_l=_aerobic_zone_do_mg_l(session))
    raw_report = _universal_report_from_job(job_result)
    if raw_report is not None:
        report = SolverInfeasibilityReport.model_validate(raw_report)
    else:
        report = _universal_infeasibility_report(model_family=template_spec.family, solver_status=terminal_job.status.value if terminal_job is not None else 'unknown', termination_condition='pipeline_failed', message=terminal_job.error if terminal_job is not None and terminal_job.error else 'WaterTAP pipeline did not produce an optimal solution.', remediation=['Inspect diagnostics.pipeline_history for the failed universal cascade stage.', 'Use solver_infeasibility_report.suggested_remediation for the next tender-side modification; do not tune bounds client-side.'], diagnostic_basis={'job_result': job_result or {}})
    persistence = job_result.get('solver_infeasibility_report_persistence', {}) if isinstance(job_result, dict) else {}
    return SolveResult(status='infeasible_structured', solution_quality='infeasible_structured', solve_seconds=time.monotonic() - started, effluent=_universal_effluent_payload(session), design_parameters_settled=_universal_settled_design_parameters(session), stream_closure=_universal_stream_closure(fractionation), diagnostics=base_diagnostics, solver_infeasibility_report=report, flowsheet_svg=flowsheet_svg, artifact_id=str(persistence.get('solver_infeasibility_report_artifact_id') or ''), artifact_path='')

def _solve_designed_adm1_graph(*, designed_graph: Dict[str, Any], topology: 'RoleResolvedTopology', template_config: Optional[Dict[str, Any]], rating_specification: Optional[Dict[str, Any]], design_targets: Dict[str, Any], solver: Optional[str], convergence_budget_seconds: int, project_ref: str, plant_slug: str, workflow_run_id: Optional[str], started: float) -> SolveResult:
    """Solve a caller-DESIGNED role-tagged ADM1 / mADM1 anaerobic graph.

    The anaerobic branch of :func:`solve_designed_graph`: the supplied role-tagged
    graph IS the topology. The matching ``Adm1PresetProvider`` (selected from the
    graph's digestion family) reconstructs the full default bundle, the graph is
    compiled (``compile_role_graph`` → ``build_from_topology``) into a live session,
    and the SAME gas-phase-aware digester solve the named adm1_* preset uses
    (:func:`_solve_built_adm1_session`) runs it — so the designed-graph route
    returns the SAME honest result the preset path does (all packaged mADM1
    families now reach strict-optimal on the healthy canonical defaults). The result
    carries ``topology_source='designed_graph'``.
    """
    from watertap_engine_mcp.utils.simulation_seed_compiler import RoleGraphValidationError, TOPOLOGY_SOURCE_DESIGNED_GRAPH, _adm1_provider_for_topology, _adm1_separator_outlet_names, compile_role_graph, session_spec_to_build_topology
    provider = _adm1_provider_for_topology(topology, template_config, separator_outlets=_adm1_separator_outlet_names(designed_graph))
    try:
        plan = compile_role_graph(graph=designed_graph, provider=provider, topology_source=TOPOLOGY_SOURCE_DESIGNED_GRAPH)
    except RoleGraphValidationError as exc:
        report = _universal_infeasibility_report(model_family='adm1', solver_status='not_started', termination_condition='designed_graph_role_validation_failed', message=exc.payload.get('message', str(exc)), remediation=['Add the missing role-tagged units listed in diagnostics.missing_roles.', 'Or route this tender to the named adm1_* preset (diagnostics.nearest_preset) explicitly; the designed graph is NOT silently replaced by a preset.'], diagnostic_basis=exc.payload)
        return _universal_empty_result(status='infeasible_structured', solve_seconds=time.monotonic() - started, diagnostics={'topology_source': TOPOLOGY_SOURCE_DESIGNED_GRAPH, **exc.payload}, report=report)
    from watertap_engine_mcp.core.rating_geometry import RatingGeometryError, bind_graph_geometry
    try:
        contract = bind_graph_geometry(plan.session_spec, rating_specification)
    except RatingGeometryError as exc:
        return _geometry_refusal(exc)
    topo = session_spec_to_build_topology(plan.session_spec)
    topo.setdefault('metadata', {})['rating_geometry'] = contract
    topo['metadata']['analysis_mode'] = 'rating'
    session_id = f'{plant_slug or project_ref or 'designed'}-adm1-graph-{uuid.uuid4()}'
    built = build_from_topology(topology=topo, session_id=session_id, project_ref=project_ref or None, plant_slug=plant_slug or None)
    if 'error' in built:
        report = _universal_infeasibility_report(model_family='adm1', solver_status='build_error', termination_condition='designed_graph_build_failed', message=str(built['error']), remediation=['Inspect the designed graph units/connections; ensure the digester liquid/vapor ports match the WaterTAP AD unit model.'], diagnostic_basis={'built': built})
        return _universal_empty_result(status='infeasible_structured', solve_seconds=time.monotonic() - started, diagnostics={'topology_source': TOPOLOGY_SOURCE_DESIGNED_GRAPH, **built}, report=report)
    return _solve_built_adm1_session(session_id=session_id, template_label='designed_graph', requested_topology_template='designed_graph', plant_flow_kld=0.0, objective_spec='feasibility', design_targets=design_targets, solver=solver, convergence_budget_seconds=convergence_budget_seconds, project_ref=project_ref, plant_slug=plant_slug, workflow_run_id=workflow_run_id, started=started, topology_source=TOPOLOGY_SOURCE_DESIGNED_GRAPH)

def solve_designed_graph(designed_graph: Dict[str, Any], template_config: Optional[Dict[str, Any]]=None, design_targets: Optional[Dict[str, Any]]=None, solver: Optional[str]=None, convergence_budget_seconds: int=300, project_ref: str='', plant_slug: str='', workflow_run_id: Optional[str]=None, rating_specification: Optional[Dict[str, Any]]=None) -> SolveResult:
    """Solve a caller-DESIGNED role-tagged graph (a BFD / simulation_seed topology).

    Unlike :func:`solve_flowsheet`, the supplied graph IS the topology: it is NOT
    looked up in the named-preset registry, so an unknown preset NAME never
    hard-errors here (the named-preset registry becomes an optional default
    source, not a gate). The family config + :class:`DefaultProvider` are selected
    from the graph's role tags — ``ASM2DBiologicalConfig`` + ``Asm2dPresetProvider``
    for a complete ASM2D biological recycle family, the ADM1 config +
    ``Adm1PresetProvider`` for an ADM1/mADM1 anaerobic digestion family — and that
    provider reconstructs every solver default (DOF fixes, scaling, feed,
    constraints, solver metadata) from the role tags. Both families build via the
    same designed-graph machinery and carry
    ``diagnostics['topology_source'] = 'designed_graph'``; the ADM1 family routes to
    the dedicated gas-phase-aware digester solve so it returns the same honest result
    the named adm1_* preset does (UASB / two-stage strict-optimal; recycle families
    infeasible_structured).

    A graph that FAILS role validation (forms neither family) is returned as a
    structured diff-vs-nearest-preset infeasibility — never silently replaced by a
    preset.
    """
    from watertap_engine_mcp.core.default_provider import RoleResolvedTopology
    from puran_mcp_support.engineering.simulation_role_graph import DESIGNED_GRAPH_FAMILY_ADM1
    from watertap_engine_mcp.templates.asm2d import ASM2DBiologicalConfig
    from watertap_engine_mcp.utils.simulation_seed_compiler import RoleGraphValidationError, TOPOLOGY_SOURCE_DESIGNED_GRAPH, _asm2d_provider_for_topology, compile_role_graph, session_spec_to_build_topology
    started = time.monotonic()
    if workflow_run_id is not None:
        try:
            workflow_run_id = str(uuid.UUID(str(workflow_run_id)))
        except (TypeError, ValueError, AttributeError):
            return _universal_empty_result(status='invalid_workflow_run_id', solve_seconds=time.monotonic() - started, diagnostics={'message': 'workflow_run_id must be a UUID'})
    design_targets = dict(design_targets or {})
    raw_units = designed_graph.get('units') or []
    role_specs = [{'unit_id': unit.get('unit_id') or unit.get('id'), 'unit_type': unit.get('unit_type') or unit.get('type'), 'role': unit.get('role'), 'role_stage_index': unit.get('role_stage_index'), 'role_group': unit.get('role_group')} for unit in raw_units]
    topology = RoleResolvedTopology.from_unit_specs(role_specs)
    if topology.resolver.designed_graph_family() == DESIGNED_GRAPH_FAMILY_ADM1:
        return _solve_designed_adm1_graph(designed_graph=designed_graph, topology=topology, template_config=template_config, rating_specification=rating_specification, design_targets=design_targets, solver=solver, convergence_budget_seconds=convergence_budget_seconds, project_ref=project_ref, plant_slug=plant_slug, workflow_run_id=workflow_run_id, started=started)
    config = ASM2DBiologicalConfig(**template_config or {})
    provider = _asm2d_provider_for_topology(topology, config)
    try:
        plan = compile_role_graph(graph=designed_graph, provider=provider, topology_source=TOPOLOGY_SOURCE_DESIGNED_GRAPH)
    except RoleGraphValidationError as exc:
        report = _universal_infeasibility_report(model_family='asm2d', solver_status='not_started', termination_condition='designed_graph_role_validation_failed', message=exc.payload.get('message', str(exc)), remediation=['Add the missing role-tagged units listed in diagnostics.missing_roles.', 'Or route this tender to the named preset (diagnostics.nearest_preset) explicitly; the designed graph is NOT silently replaced by a preset.'], diagnostic_basis=exc.payload)
        return _universal_empty_result(status='infeasible_structured', solve_seconds=time.monotonic() - started, diagnostics={'topology_source': TOPOLOGY_SOURCE_DESIGNED_GRAPH, **exc.payload}, report=report)
    from watertap_engine_mcp.core.rating_geometry import RatingGeometryError, bind_graph_geometry
    try:
        contract = bind_graph_geometry(plan.session_spec, rating_specification)
    except RatingGeometryError as exc:
        return _geometry_refusal(exc)
    topo = session_spec_to_build_topology(plan.session_spec)
    topo.setdefault('metadata', {})['rating_geometry'] = contract
    topo['metadata']['analysis_mode'] = 'rating'
    session_id = f'{plant_slug or project_ref or 'designed'}-graph-{uuid.uuid4()}'
    built = build_from_topology(topology=topo, session_id=session_id, project_ref=project_ref or None, plant_slug=plant_slug or None)
    if 'error' in built:
        report = _universal_infeasibility_report(model_family='asm2d', solver_status='build_error', termination_condition='designed_graph_build_failed', message=str(built['error']), remediation=['Inspect the designed graph units/connections; ensure ports match the WaterTAP unit models.'], diagnostic_basis={'built': built})
        return _universal_empty_result(status='infeasible_structured', solve_seconds=time.monotonic() - started, diagnostics={'topology_source': TOPOLOGY_SOURCE_DESIGNED_GRAPH, **built}, report=report)
    solver_options = {'tol': 1e-06, 'acceptable_tol': 1e-06, 'bound_relax_factor': 1e-08, 'max_iter': 3000}
    job_payload = build_and_solve(session_id=session_id, solver=solver, solver_options=solver_options, project_ref=project_ref or None, plant_slug=plant_slug or None, workflow_run_id=workflow_run_id, max_wall_time_s=convergence_budget_seconds)
    if 'error' in job_payload:
        report = _universal_infeasibility_report(model_family='asm2d', solver_status='build_error', termination_condition='pipeline_submission_failed', message=str(job_payload['error']), remediation=['Fix the session build errors reported by the WaterTAP pipeline.'], diagnostic_basis={'job_payload': job_payload})
        return _universal_empty_result(status='infeasible_structured', solve_seconds=time.monotonic() - started, diagnostics={'topology_source': TOPOLOGY_SOURCE_DESIGNED_GRAPH, **job_payload}, report=report)
    job_id = job_payload['job_id']
    budget = max(int(convergence_budget_seconds or 300), 1)
    terminal_job = None
    while time.monotonic() - started < budget:
        terminal_job = job_manager.get_status(job_id)
        if terminal_job is not None and terminal_job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
            break
        time.sleep(0.5)
    else:
        job_manager.cancel(job_id)
        return SolveResult(status='convergence_budget_exceeded', solve_seconds=time.monotonic() - started, effluent={}, design_parameters_settled={}, stream_closure={}, diagnostics={'topology_source': TOPOLOGY_SOURCE_DESIGNED_GRAPH, 'job_id': job_id, 'session_id': session_id, 'message': f'Convergence budget of {budget}s was exceeded.'}, solver_infeasibility_report=None, flowsheet_svg='', artifact_id='', artifact_path='')
    job_result = terminal_job.result if terminal_job is not None else None
    session = session_manager.load(session_id)
    report_payload = build_simulation_report_payload(session_id=session_id, project_ref=project_ref or 'unknown', plant_slug=plant_slug or None, workflow_run_id=workflow_run_id)
    report_ok = isinstance(report_payload, dict) and 'error' not in report_payload
    engine_diagnostics = {}
    flowsheet_svg = _session_flowsheet_svg(session)
    if report_ok:
        engine_report = (report_payload.get('engine_reports') or [{}])[0]
        engine_diagnostics = engine_report.get('diagnostics') or {}
        flowsheet_svg = report_payload.get('flowsheet_svg') or flowsheet_svg
    artifact_persistence = job_result.get('artifact_persistence', {}) if isinstance(job_result, dict) else {}
    artifact_id = str(artifact_persistence.get('simulation_report_artifact_id') or '')
    history = job_result.get('history', []) if isinstance(job_result, dict) else []
    from watertap_engine_mcp.chemistry.capability import shared_mineral_chemistry_capability
    base_diagnostics = {'topology_source': TOPOLOGY_SOURCE_DESIGNED_GRAPH, 'analysis_mode': 'rating', 'sizing_reference': contract['sizing_reference'], 'job_id': job_id, 'session_id': session_id, 'pipeline_history': history, 'design_parameters': plan.session_spec.get('design_parameters', {}), 'shared_mineral_chemistry': shared_mineral_chemistry_capability(), **engine_diagnostics}
    if terminal_job is not None and terminal_job.status == JobStatus.COMPLETED and (session.status.value == 'solved'):
        solution_quality, residual_basis = _solution_quality_for_session(session)
        return SolveResult(status='optimal', solution_quality=solution_quality, residual_basis=residual_basis, solve_seconds=time.monotonic() - started, effluent=_universal_effluent_payload(session), design_parameters_settled=_universal_settled_design_parameters(session), stream_closure={}, diagnostics=base_diagnostics, solver_infeasibility_report=None, flowsheet_svg=flowsheet_svg, artifact_id=artifact_id, artifact_path='')
    raw_report = _universal_report_from_job(job_result)
    if raw_report is not None:
        from puran_mcp_support.engineering.models.solver_infeasibility_report import SolverInfeasibilityReport
        report = SolverInfeasibilityReport.model_validate(raw_report)
    else:
        report = _universal_infeasibility_report(model_family='asm2d', solver_status=terminal_job.status.value if terminal_job is not None else 'unknown', termination_condition='pipeline_failed', message=terminal_job.error if terminal_job is not None and terminal_job.error else 'WaterTAP pipeline did not produce an optimal solution.', remediation=['Inspect diagnostics.pipeline_history for the failed cascade stage.'], diagnostic_basis={'job_result': job_result or {}})
    return SolveResult(status='infeasible_structured', solution_quality='infeasible_structured', solve_seconds=time.monotonic() - started, effluent=_universal_effluent_payload(session), design_parameters_settled=_universal_settled_design_parameters(session), stream_closure={}, diagnostics=base_diagnostics, solver_infeasibility_report=report, flowsheet_svg=flowsheet_svg, artifact_id='', artifact_path='')

def build_from_simulation_seeds(simulation_seeds: List[Dict[str, Any]], session_id: Optional[str]=None, template_hint: Optional[str]=None, external_streams: Optional[Dict[str, Dict[str, Any]]]=None, project_ref: Optional[str]=None, name: str='', description: str='', rating_specification: Optional[Dict[str, Any]]=None, reaction_config: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
    """Create a WaterTAP session from shared simulation_seed/v2 payloads.

    reaction_config: optional ModifiedASM2d model-form variant every reactor is built on (as create_session).
    """
    try:
        stated_reaction_config = session_reaction_config(reaction_config)
    except ReactionConfigRefused as exc:
        return {'error': str(exc), 'termination_condition': 'reaction_config_refused'}
    try:
        from watertap_engine_mcp.utils.simulation_seed_compiler import build_plan_from_simulation_seeds
        plan = build_plan_from_simulation_seeds(simulation_seeds, template_hint=template_hint, external_streams=external_streams)
        from watertap_engine_mcp.core.rating_geometry import bind_graph_geometry
        contract = bind_graph_geometry(plan.session_spec, rating_specification)
    except Exception as exc:
        return {'error': f'Failed to compile simulation seeds: {exc}'}
    spec = plan.session_spec
    create_result = create_session(name=name or plan.template_name or 'simulation_seeds', description=description, property_package=spec['property_package'], property_package_config=spec.get('property_package_config'), session_id=session_id)
    if 'error' in create_result:
        return create_result
    try:
        session = session_manager.load(create_result['session_id'])
    except FileNotFoundError:
        return {'error': f"Failed to load created session '{create_result['session_id']}'"}
    for unit_spec in spec.get('units', []):
        unit = session.add_unit(unit_spec['unit_id'], unit_spec['unit_type'], unit_spec.get('config', {}), role=unit_spec.get('role'), role_stage_index=unit_spec.get('role_stage_index'), role_group=unit_spec.get('role_group'))
        if unit_spec.get('engineering_inputs'):
            unit.engineering_inputs.update(unit_spec['engineering_inputs'])
    for conn_spec in spec.get('connections', []):
        session.add_connection(conn_spec['src_unit'], conn_spec['src_port'], conn_spec['dest_unit'], conn_spec['dest_port'], translator_id=conn_spec.get('translator_id'), tear_stream=bool(conn_spec.get('tear_stream', False)), tear_rationale=conn_spec.get('tear_rationale'))
    for fix in spec.get('dof_fixes', []):
        session.fix_variable(fix['unit_id'], fix['var_name'], fix['value'])
    for scale in spec.get('scaling_factors', []):
        session.set_scaling_factor(scale['unit_id'], scale['var_name'], scale['factor'])
    for feed_unit, feed_state in spec.get('feed_states', {}).items():
        session.set_feed_state(feed_unit, feed_state)
    for process_constraint in spec.get('process_constraints', []):
        session.add_process_constraint(process_constraint)
    if spec.get('design_parameters'):
        session.config.runtime_metadata['design_parameters'] = spec['design_parameters']
    if isinstance(spec.get('metadata'), dict):
        session.config.runtime_metadata.update(spec['metadata'])
    if isinstance(spec.get('solver_options'), dict):
        session.config.runtime_metadata.setdefault('solver_options', {}).update(spec['solver_options'])
    if project_ref:
        session.config.runtime_metadata['project_ref'] = project_ref
    if stated_reaction_config is not None:
        session.config.runtime_metadata[REACTION_CONFIG_TARGET] = stated_reaction_config
    session.config.runtime_metadata['origin_tool'] = 'watertap-engine-mcp.build_from_simulation_seeds'
    session.config.runtime_metadata['rating_geometry'] = contract
    session.config.runtime_metadata['analysis_mode'] = 'rating'
    session.config.runtime_metadata['topology_source'] = plan.topology_source
    session.config.export_metadata.setdefault('deterministic_metadata', {})
    session.config.export_metadata['deterministic_metadata']['simulation_seed_ids'] = [seed.seed_id for seed in plan.seeds]
    session.config.export_metadata['deterministic_metadata']['template_name'] = plan.template_name
    session_manager.save(session)
    return {'session_id': session.config.session_id, 'status': 'created', 'template_name': plan.template_name, 'topology_source': plan.topology_source, 'property_package': spec['property_package'], 'created_units': [{'unit_id': unit['unit_id'], 'unit_type': unit['unit_type']} for unit in spec.get('units', [])], 'connections': len(spec.get('connections', [])), 'dof_fixes': len(spec.get('dof_fixes', [])), 'feed_states': len(spec.get('feed_states', {})), 'process_constraints': len(spec.get('process_constraints', [])), 'source_seed_ids': [seed.seed_id for seed in plan.seeds], 'warnings': plan.warnings}

def _required_text(payload: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError(f'Missing required key; expected one of {keys}')

def _connection_endpoint(payload: Dict[str, Any], *keys: str) -> tuple[str, str]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            return (_required_text(value, 'unit', 'unit_id'), _required_text(value, 'port', 'port_name'))
        if isinstance(value, str) and '.' in value:
            unit_id, port = value.split('.', 1)
            return (unit_id.strip(), port.strip())
        if ':' in key:
            unit_key, port_key = key.split(':', 1)
            if unit_key in payload or port_key in payload:
                return (_required_text(payload, unit_key), _required_text(payload, port_key))
    return (_required_text(payload, 'unit', 'unit_id'), _required_text(payload, 'port', 'port_name'))

def _feed_state_kwargs(feed_state: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {'flow_vol_m3_hr', 'temperature_C', 'pressure_bar', 'components', 'concentration_units', 'concentration_basis', 'tds_mg_L', 'nacl_mg_L', 'component_charges', 'electroneutrality_species', 'phase_mass_flow_kg_s', 'component_mass_flow_kg_s', 'component_mol_flow_mol_s', 'H2O_mass_kg_s', 'NaCl_mass_kg_s', 'temperature_K', 'pressure_Pa'}
    return {key: value for key, value in feed_state.items() if key in allowed}

def build_from_topology(topology: Dict[str, Any], session_id: Optional[str]=None, project_ref: Optional[str]=None, plant_slug: Optional[str]=None, external_streams: Optional[Dict[str, Dict[str, Any]]]=None, mcp_outputs_dir: Optional[str]=None) -> Dict[str, Any]:
    """Create a WaterTAP session from a declarative topology dictionary.

    The server owns validation, unit creation, connection wiring, and later
    solve hygiene. Clients provide the graph and engineering inputs; they do
    not assemble Pyomo or tune SequentialDecomposition directly.
    """
    if not isinstance(topology, dict):
        return {'error': 'topology must be a dictionary'}
    validation_error = _finite_validation_error('build_from_topology', {'topology': topology, 'external_streams': external_streams})
    if validation_error is not None:
        return validation_error
    session_spec = topology.get('session') if isinstance(topology.get('session'), dict) else {}
    create_result = create_session(name=session_spec.get('name', topology.get('name', 'topology')), description=session_spec.get('description', topology.get('description', '')), session_preset=session_spec.get('session_preset', topology.get('session_preset')), property_package=session_spec.get('property_package', topology.get('property_package', 'SEAWATER')), property_package_config=session_spec.get('property_package_config', topology.get('property_package_config')), session_id=session_id or session_spec.get('session_id') or topology.get('session_id'))
    if 'error' in create_result:
        return create_result
    created_session_id = create_result['session_id']
    created_units: list[Dict[str, Any]] = []
    created_connections: list[Dict[str, Any]] = []
    warnings: list[str] = []
    try:
        for unit_spec in topology.get('units', []):
            if not isinstance(unit_spec, dict):
                raise ValueError(f'Invalid unit entry: {unit_spec!r}')
            unit_id = _required_text(unit_spec, 'unit_id', 'id')
            unit_type = _required_text(unit_spec, 'unit_type', 'type')
            result = create_unit(session_id=created_session_id, unit_id=unit_id, unit_type=unit_type, config=unit_spec.get('config') or {}, engineering_inputs=unit_spec.get('engineering_inputs'), role=unit_spec.get('role'), role_stage_index=unit_spec.get('role_stage_index'), role_group=unit_spec.get('role_group'))
            if 'error' in result:
                raise ValueError(f"Failed to create unit '{unit_id}': {result['error']}")
            created_units.append({'unit_id': unit_id, 'unit_type': unit_type, 'role': result.get('role'), 'role_stage_index': result.get('role_stage_index'), 'role_group': result.get('role_group')})
        session = session_manager.load(created_session_id)
        for unit_spec in topology.get('units', []):
            unit_id = _required_text(unit_spec, 'unit_id', 'id')
            for var_name, value in (unit_spec.get('fixed_vars') or {}).items():
                session.fix_variable(unit_id, var_name, value)
            for var_name, factor in (unit_spec.get('scaling_factors') or {}).items():
                session.set_scaling_factor(unit_id, var_name, factor)
        session_manager.save(session)
        feed_state_map: Dict[str, Any] = {}
        if isinstance(topology.get('feed_states'), dict):
            feed_state_map.update(topology['feed_states'])
        for unit_spec in topology.get('units', []):
            if isinstance(unit_spec, dict) and isinstance(unit_spec.get('feed_state'), dict):
                feed_state_map[_required_text(unit_spec, 'unit_id', 'id')] = unit_spec['feed_state']
        for unit_id, feed_state in feed_state_map.items():
            if not isinstance(feed_state, dict):
                raise ValueError(f"feed_state for '{unit_id}' must be a dictionary")
            if 'state_args' in feed_state:
                _raise_on_non_finite(f"feed_state for '{unit_id}'", feed_state['state_args'])
                session = session_manager.load(created_session_id)
                session.set_feed_state(str(unit_id), feed_state)
                session_manager.save(session)
            else:
                result = set_feed_state(session_id=created_session_id, unit_id=str(unit_id), **_feed_state_kwargs(feed_state))
                if 'error' in result:
                    raise ValueError(f"Failed to set feed state for '{unit_id}': {result['error']}")
        for stream_id, stream_payload in (external_streams or {}).items():
            if isinstance(stream_payload, dict) and isinstance(stream_payload.get('unit_id'), str):
                result = set_feed_state(session_id=created_session_id, unit_id=stream_payload['unit_id'], **_feed_state_kwargs(stream_payload))
                if 'error' in result:
                    raise ValueError(f"Failed to set external stream '{stream_id}': {result['error']}")
        for conn_spec in topology.get('connections', []):
            if not isinstance(conn_spec, dict):
                raise ValueError(f'Invalid connection entry: {conn_spec!r}')
            source_unit, source_port = _connection_endpoint(conn_spec, 'from', 'source', 'source_unit:source_port')
            dest_unit, dest_port = _connection_endpoint(conn_spec, 'to', 'dest', 'dest_unit:dest_port')
            if conn_spec.get('translator_id'):
                result = connect_ports(created_session_id, source_unit, source_port, dest_unit, dest_port, translator_id=conn_spec.get('translator_id'))
            else:
                result = connect_units(created_session_id, source_unit, source_port, dest_unit, dest_port, auto_create_translator=bool(conn_spec.get('auto_create_translator', True)))
            if 'error' in result:
                raise ValueError(f'Failed to connect {source_unit}.{source_port} -> {dest_unit}.{dest_port}: {result['error']}')
            if result.get('translator_note'):
                warnings.append(result['translator_note'])
            created_connections.append({'source': f'{source_unit}.{source_port}', 'dest': f'{dest_unit}.{dest_port}', 'translator_id': conn_spec.get('translator_id')})
        session = session_manager.load(created_session_id)
        for process_constraint in topology.get('process_constraints', []):
            if not isinstance(process_constraint, dict):
                raise ValueError('process_constraints entries must be dictionaries')
            session.add_process_constraint(process_constraint)
        thermal_couplings = topology.get('thermal_couplings')
        if isinstance(thermal_couplings, dict):
            session.thermal_couplings = {str(evaporator_id): str(condenser_id) for evaporator_id, condenser_id in thermal_couplings.items()}
        metadata = topology.get('metadata') if isinstance(topology.get('metadata'), dict) else {}
        session.config.runtime_metadata.update(metadata)
        if project_ref:
            session.config.runtime_metadata['project_ref'] = project_ref
        if plant_slug:
            session.config.runtime_metadata['plant_slug'] = plant_slug
        if mcp_outputs_dir:
            session.config.runtime_metadata['mcp_outputs_dir'] = mcp_outputs_dir
        session.config.runtime_metadata['origin_tool'] = 'watertap-engine-mcp.build_from_topology'
        session.config.runtime_metadata.setdefault('topology_name', topology.get('name') or metadata.get('topology_name') or 'dynamic_topology')
        session.config.export_metadata.setdefault('deterministic_metadata', {})
        session.config.export_metadata['deterministic_metadata']['topology_unit_ids'] = [item['unit_id'] for item in created_units]
        session_manager.save(session)
    except Exception as exc:
        try:
            session_manager.delete(created_session_id)
        except Exception:
            pass
        return {'error': str(exc), 'session_id': created_session_id}
    return {'session_id': created_session_id, 'status': 'created', 'property_package': create_result.get('property_package'), 'created_units': created_units, 'connections': created_connections, 'feed_states': len(feed_state_map), 'process_constraints': len(topology.get('process_constraints', [])), 'warnings': warnings, 'message': 'Topology session created. Run solve() once; the server owns DOF, scaling, initialization, recovery, and persistence.'}

def plan_topology(topology: Dict[str, Any], session_id: Optional[str]=None, project_ref: Optional[str]=None, plant_slug: Optional[str]=None, external_streams: Optional[Dict[str, Dict[str, Any]]]=None, mcp_outputs_dir: Optional[str]=None, keep_session: bool=False) -> Dict[str, Any]:
    """Preflight a declarative topology and return the selected SD playbook.

    This is the process-facing dry-run for dynamic flowsheets. It builds the
    same session as build_from_topology(), classifies the graph into the
    server-owned initialization recipe, reports tear-stream guesses and live
    DOF closure requirements, and then deletes the generated session unless
    keep_session=True or the caller supplied an explicit session_id.
    """
    if not isinstance(topology, dict):
        return {'error': 'topology must be a dictionary'}
    session_spec = topology.get('session') if isinstance(topology.get('session'), dict) else {}
    explicit_session_id = session_id or session_spec.get('session_id') or topology.get('session_id')
    retain_session = bool(keep_session or explicit_session_id)
    build_result = build_from_topology(topology=topology, session_id=session_id, project_ref=project_ref, plant_slug=plant_slug, external_streams=external_streams, mcp_outputs_dir=mcp_outputs_dir)
    if 'error' in build_result:
        return {'status': 'error', 'error': build_result['error'], 'session_id': build_result.get('session_id'), 'topology_build': build_result, 'session_retained': False, 'ephemeral_session_deleted': True}
    created_session_id = build_result['session_id']
    session_deleted = False
    deletion_warning: Optional[str] = None
    model_context: Optional[tuple[Any, Any, Dict[str, Any]]] = None
    preflight: Dict[str, Any] = {'status': 'error', 'build_success': False, 'ready_to_solve': False, 'issues': ['Topology preflight did not run'], 'warnings': []}
    completion_plan: Dict[str, Any] = {'session_id': created_session_id, 'status': 'error', 'issues': preflight['issues']}
    playbook: Dict[str, Any]
    try:
        session = _load_session(created_session_id)
        try:
            model_context = build_model_context(session)
            playbook = _initialization_playbook_payload(session, model_context[0], model_context[1], model_context[2])
        except Exception as exc:
            playbook = {'status': 'unavailable', 'error': f'Model build failed before playbook classification: {exc}', 'warnings': []}
        preflight = _build_preflight_payload(created_session_id, session, model_context=model_context)
        completion_plan = dof_completion_plan_from_preflight(created_session_id, preflight)
    finally:
        if not retain_session:
            try:
                session_manager.delete(created_session_id)
                session_deleted = True
            except Exception as exc:
                deletion_warning = f'Could not delete ephemeral preflight session: {exc}'
    warnings = list(preflight.get('warnings', []))
    if deletion_warning:
        warnings.append(deletion_warning)
    usage = {'after_underspecified': 'Provide missing registered engineering inputs or live DOF target_variable paths under units[].fixed_vars, then call build_from_topology() again.', 'after_ready': 'Call solve(session_id=...) once; the server owns SD, scaling, recovery, and persistence.', 'session_lifecycle': 'This preflight session was retained and can be solved after sizing.' if retain_session else 'This preflight session was ephemeral and has been deleted; rebuild after sizing.'}
    return {'session_id': created_session_id, 'status': preflight.get('status', 'error'), 'ready_to_solve': preflight.get('ready_to_solve', False), 'session_retained': retain_session, 'ephemeral_session_deleted': session_deleted, 'topology_build': build_result, 'selected_playbook': playbook, 'dof_completion_plan': completion_plan, 'solve_preflight': preflight, 'usage': usage, 'warnings': warnings, 'issues': preflight.get('issues', []), 'message': 'Topology is ready for solve.' if preflight.get('ready_to_solve') else 'Topology preflight complete; close the reported DOF before solve().'}

def _numeric_value(payload: Any) -> float | None:
    """Extract a float from either a raw value or solved-state value payload."""
    if isinstance(payload, dict):
        payload = payload.get('value')
    try:
        return float(payload)
    except (TypeError, ValueError):
        return None

def _value_units(payload: Any) -> str | None:
    if isinstance(payload, dict):
        units = payload.get('units')
        if units is not None:
            return str(units)
    return None

def _stream_scalar(stream: Dict[str, Any] | None, name: str) -> tuple[float | None, str | None]:
    """Return the first scalar whose solved-state key matches a WaterTAP state var."""
    if not isinstance(stream, dict):
        return (None, None)
    for key, payload in stream.items():
        if key == name or key.startswith(f'{name}['):
            return (_numeric_value(payload), _value_units(payload))
    return (None, None)

def _stream_flow_m3_d(stream: Dict[str, Any] | None) -> float | None:
    value, units = _stream_scalar(stream, 'flow_vol')
    if value is None:
        return None
    unit_text = (units or '').lower().replace(' ', '')
    if '/s' in unit_text or 's**-1' in unit_text:
        return value * 86400.0
    if '/hr' in unit_text or '/h' in unit_text or 'h**-1' in unit_text:
        return value * 24.0
    return value

def _stream_temperature_k(stream: Dict[str, Any] | None) -> float | None:
    value, units = _stream_scalar(stream, 'temperature')
    if value is None:
        return None
    unit_text = (units or '').lower()
    if unit_text in {'c', 'degc', 'degree_celsius'}:
        return value + 273.15
    return value

def _stream_pressure_kpa(stream: Dict[str, Any] | None) -> float | None:
    value, units = _stream_scalar(stream, 'pressure')
    if value is None:
        return None
    unit_text = (units or '').lower()
    if 'pa' in unit_text and 'kpa' not in unit_text:
        return value / 1000.0
    if 'bar' in unit_text:
        return value * 100.0
    return value

def _index_tokens(key: str) -> list[str]:
    match = re.search('\\[(.*)\\]', key)
    if not match:
        return []
    return [token.strip().strip('\'"') for token in match.group(1).split(',')]

def _stream_components(stream: Dict[str, Any] | None) -> Dict[str, Any]:
    """Extract component values from a solved WaterTAP stream payload."""
    if not isinstance(stream, dict):
        return {}
    components: Dict[str, Any] = {}
    for key, payload in stream.items():
        value = _numeric_value(payload)
        if value is None:
            continue
        units = (_value_units(payload) or '').lower().replace(' ', '')
        tokens = _index_tokens(key)
        component = tokens[-1] if tokens else None
        if key.startswith('conc_mass_comp[') and component:
            if 'kg' in units and ('m**3' in units or 'm^3' in units or 'm3' in units):
                components[component] = value * 1000.0
            else:
                components[component] = value
        elif key.startswith('conc_mol_comp[') and component:
            components[f'{component}_mol_conc'] = value
        elif key.startswith('flow_mass_phase_comp[') and component:
            components[f'{component}_mass_flow'] = value
        elif key.startswith('flow_mol_phase_comp[') and component:
            components[f'{component}_mol_flow'] = value
    return components

def _solved_streams(session: FlowsheetSession) -> Dict[str, Dict[str, Any]]:
    solved_state = (session.results or {}).get('solved_state') if isinstance(session.results, dict) else None
    streams = solved_state.get('streams', {}) if isinstance(solved_state, dict) else {}
    return streams if isinstance(streams, dict) else {}

def _stream_label(stream_id: str, stream: Dict[str, Any] | None) -> str:
    flow = _stream_flow_m3_d(stream)
    if flow is None:
        return stream_id
    return f'{stream_id} | {flow:.3g} m3/d'

def _svg_unit_family(unit_type: str) -> str:
    lowered = unit_type.lower()
    if unit_type in {'Feed', 'FeedZO'}:
        return 'feed'
    if unit_type in {'Product'}:
        return 'product'
    if any((token in lowered for token in ('osmosis', 'nanofiltration', 'ultrafiltration', 'membrane', 'mbr'))):
        return 'membrane'
    if any((token in lowered for token in ('cstr', 'clarifier', 'aeration', 'ad', 'digester', 'thickener'))):
        return 'biological'
    if any((token in lowered for token in ('evaporator', 'compressor', 'condenser', 'crystallizer', 'heater', 'ejector'))):
        return 'thermal'
    if any((token in lowered for token in ('pump', 'pressure', 'energyrecovery'))):
        return 'hydraulic'
    if any((token in lowered for token in ('electro', 'ionexchange'))):
        return 'electrochemical'
    if any((token in lowered for token in ('gac', 'adsorption'))):
        return 'adsorption'
    if any((token in lowered for token in ('coag', 'reactor', 'ultraviolet', 'ozone', 'chlorination', 'boron'))):
        return 'chemical'
    return 'unit'

def _svg_layers(session: FlowsheetSession) -> Dict[str, int]:
    unit_ids = list(session.units)
    successors: Dict[str, list[str]] = {unit_id: [] for unit_id in unit_ids}
    indegree: Dict[str, int] = {unit_id: 0 for unit_id in unit_ids}
    for conn in session.connections:
        if conn.source_unit not in successors or conn.dest_unit not in indegree:
            continue
        successors[conn.source_unit].append(conn.dest_unit)
        indegree[conn.dest_unit] += 1
    ranks = {unit_id: 0 for unit_id in unit_ids}
    queue = [unit_id for unit_id in unit_ids if indegree[unit_id] == 0]
    if not queue and unit_ids:
        queue = [unit_ids[0]]
    visited: set[str] = set()
    while queue:
        unit_id = queue.pop(0)
        if unit_id in visited:
            continue
        visited.add(unit_id)
        for dest in successors.get(unit_id, []):
            ranks[dest] = max(ranks[dest], ranks[unit_id] + 1)
            indegree[dest] -= 1
            if indegree[dest] <= 0:
                queue.append(dest)
    for unit_id in unit_ids:
        if unit_id in visited:
            continue
        predecessor_ranks = [ranks[conn.source_unit] + 1 for conn in session.connections if conn.dest_unit == unit_id and conn.source_unit in ranks]
        ranks[unit_id] = max(predecessor_ranks, default=ranks[unit_id])
    return ranks

def _session_flowsheet_svg(session: FlowsheetSession) -> str:
    """Return a layered process-flow SVG with arcs and solved-state stream labels."""
    unit_ids = list(session.units)
    solved_streams = _solved_streams(session)
    node_w = 148
    node_h = 58
    x_gap = 210
    y_gap = 92
    margin_x = 54
    margin_top = 72
    ranks = _svg_layers(session)
    lanes_by_rank: Dict[int, list[str]] = {}
    for unit_id in unit_ids:
        lanes_by_rank.setdefault(ranks.get(unit_id, 0), []).append(unit_id)
    max_rank = max(lanes_by_rank, default=0)
    max_lane = max((len(lane) for lane in lanes_by_rank.values()), default=1)
    width = max(680, margin_x * 2 + node_w + max_rank * x_gap)
    height = max(260, margin_top + max_lane * y_gap + 84)
    positions: Dict[str, tuple[int, int]] = {}
    for rank, lane_units in lanes_by_rank.items():
        column_height = (len(lane_units) - 1) * y_gap
        y0 = margin_top + max(0, ((max_lane - 1) * y_gap - column_height) // 2)
        for lane, unit_id in enumerate(lane_units):
            positions[unit_id] = (margin_x + rank * x_gap, y0 + lane * y_gap)
    elements = ['<defs>', '<marker id="arrow" markerWidth="9" markerHeight="9" refX="8" refY="4.5" orient="auto" markerUnits="strokeWidth"><path d="M0,0 L0,9 L8,4.5 z" fill="#243042"/></marker>', '</defs>', '<style>', '.canvas{fill:#fff}.title{font:600 15px sans-serif;fill:#16202f}.subtitle{font:11px sans-serif;fill:#5d6b7a}.unit rect{stroke:#243042;stroke-width:1.25;fill:#fff}.unit text{font-family:sans-serif}.unit-id{font-size:12px;font-weight:700;fill:#16202f}.unit-type{font-size:8.5px;fill:#52606d}.stream-edge{fill:none;stroke:#38485c;stroke-width:1.45}.stream-edge.recycle{stroke:#7b4b17;stroke-dasharray:5 3}.stream-label{font:9px sans-serif;fill:#2d3748}.port{fill:#fff;stroke:#38485c;stroke-width:1}.unit--feed rect{fill:#edf7ed}.unit--product rect{fill:#eef4ff}.unit--membrane rect{fill:#eef8fb}.unit--biological rect{fill:#f1f8ed}.unit--thermal rect{fill:#fff3e8}.unit--hydraulic rect{fill:#f4f1ff}.unit--electrochemical rect{fill:#edf7ff}.unit--chemical rect{fill:#fff6f2}.unit--adsorption rect{fill:#f7f3ed}.legend text{font:9px sans-serif;fill:#52606d}', '</style>', f'<rect class="canvas" x="0" y="0" width="{width}" height="{height}"/>', '<text class="title" x="24" y="28">WaterTAP steady-state flowsheet</text>', '<text class="subtitle" x="24" y="46">Solved stream labels use extracted flow where available.</text>']
    for conn_index, conn in enumerate(session.connections):
        if conn.source_unit not in positions or conn.dest_unit not in positions:
            continue
        src_x, src_y = positions[conn.source_unit]
        dst_x, dst_y = positions[conn.dest_unit]
        start_x = src_x + node_w
        start_y = src_y + node_h / 2
        end_x = dst_x
        end_y = dst_y + node_h / 2
        stream_id = f'{conn.source_unit}.{conn.source_port}'
        label = _stream_label(stream_id, solved_streams.get(stream_id))
        is_recycle = ranks.get(conn.dest_unit, 0) <= ranks.get(conn.source_unit, 0)
        if not is_recycle:
            path = f'M{start_x:.1f},{start_y:.1f} C{start_x + 58:.1f},{start_y:.1f} {end_x - 58:.1f},{end_y:.1f} {end_x:.1f},{end_y:.1f}'
            label_x = (start_x + end_x) / 2
            label_y = (start_y + end_y) / 2 - 8 - 7 * (conn_index % 2)
        else:
            loop_y = height - 44 - 16 * (conn_index % 4)
            path = f'M{start_x:.1f},{start_y:.1f} C{start_x + 62:.1f},{loop_y:.1f} {end_x - 62:.1f},{loop_y:.1f} {end_x:.1f},{end_y:.1f}'
            label_x = (start_x + end_x) / 2
            label_y = loop_y - 6
        edge_class = 'stream-edge recycle' if is_recycle else 'stream-edge'
        elements.append(f'<path class="{edge_class}" data-stream-id="{escape(stream_id, quote=True)}" d="{path}" marker-end="url(#arrow)"/>')
        label_parts = label.split(' | ', 1)
        label_lines = label_parts if len(label_parts) == 2 else [label]
        elements.append(f'<text class="stream-label" x="{label_x:.1f}" y="{label_y:.1f}" text-anchor="middle"><tspan>{escape(label_lines[0])}</tspan>' + (f'<tspan x="{label_x:.1f}" dy="11">{escape(label_lines[1])}</tspan>' if len(label_lines) == 2 else '') + '</text>')
    for unit_id in unit_ids:
        x, y = positions[unit_id]
        unit_type = session.units[unit_id].unit_type
        family = _svg_unit_family(unit_type)
        elements.append(f'<g class="unit unit--{family}" data-unit-id="{escape(unit_id, quote=True)}" data-unit-type="{escape(unit_type, quote=True)}">')
        elements.append(f'<rect x="{x}" y="{y}" width="{node_w}" height="{node_h}" rx="4"/>')
        elements.append(f'<circle class="port" cx="{x}" cy="{y + node_h / 2:.1f}" r="3"/>')
        elements.append(f'<circle class="port" cx="{x + node_w}" cy="{y + node_h / 2:.1f}" r="3"/>')
        elements.append(f'<text class="unit-id" x="{x + node_w / 2:.1f}" y="{y + 23}" text-anchor="middle">{escape(unit_id)}</text>')
        elements.append(f'<text class="unit-type" x="{x + node_w / 2:.1f}" y="{y + 40}" text-anchor="middle">{escape(unit_type)}</text>')
        elements.append('</g>')
    legend_x = 24
    legend_y = height - 26
    elements.append(f'<g class="legend"><path class="stream-edge" d="M{legend_x},{legend_y} L{legend_x + 34},{legend_y}" marker-end="url(#arrow)"/><text x="{legend_x + 44}" y="{legend_y + 3}">process stream</text><path class="stream-edge recycle" d="M{legend_x + 136},{legend_y} L{legend_x + 170},{legend_y}" marker-end="url(#arrow)"/><text x="{legend_x + 180}" y="{legend_y + 3}">recycle / back edge</text></g>')
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">{''.join(elements)}</svg>'

def _terminal_stream_payload(session: FlowsheetSession) -> Dict[str, Any] | None:
    """Return the best available terminal effluent stream from solved_state."""
    if not isinstance(session.results, dict):
        return None
    solved_state = session.results.get('solved_state')
    if not isinstance(solved_state, dict):
        return None
    streams = solved_state.get('streams')
    if not isinstance(streams, dict):
        return None
    priorities = ('TreatedEffluent.inlet', 'Effluent.inlet', 'Permeate.inlet', 'Product.inlet')
    for suffix in priorities:
        for stream_id, payload in streams.items():
            if stream_id.endswith(suffix):
                return {'stream_id': stream_id, **payload}
    if streams:
        stream_id, payload = next(iter(streams.items()))
        return {'stream_id': stream_id, **payload}
    return None

def _bio_process_constraint(session: FlowsheetSession) -> Dict[str, Any]:
    for constraint in session.process_constraints:
        if constraint.get('type') == 'srt':
            return constraint
    return {}

def _snapshot_value(payload: Dict[str, Any] | None, key: str) -> float | None:
    """Return a numeric solved-state snapshot value."""
    if not isinstance(payload, dict):
        return None
    datum = payload.get(key)
    if isinstance(datum, dict):
        datum = datum.get('value')
    try:
        return float(datum)
    except (TypeError, ValueError):
        return None

def _stream_concentration(stream: Dict[str, Any] | None, component: str) -> float | None:
    return _snapshot_value(stream, f'conc_mass_comp[{component}]')

def _stream_mlss_mg_l(stream: Dict[str, Any] | None) -> float | None:
    """Return MLSS as mg/L for ASM2d or ModifiedASM2d solved stream snapshots."""
    x_tss = _stream_concentration(stream, 'X_TSS')
    if x_tss is not None:
        return x_tss * 1000.0
    particulate_cod = sum((_stream_concentration(stream, component) or 0.0 for component in ('X_AUT', 'X_H', 'X_I', 'X_PAO', 'X_PHA', 'X_PP', 'X_S')))
    if particulate_cod <= 0:
        return None
    return particulate_cod * 1000.0 / MODIFIED_ASM2D_COD_TO_VSS

def _stream_flow_vol(stream: Dict[str, Any] | None) -> float | None:
    for key in ('flow_vol[0]', 'flow_vol[0.0]', 'flow_vol[None]', 'flow_vol'):
        value = _snapshot_value(stream, key)
        if value is not None:
            return value
    return None

def _solved_recycle_ratios(solved_state: Dict[str, Any] | None) -> Dict[str, float | None]:
    if not isinstance(solved_state, dict):
        return {}
    streams = solved_state.get('streams', {})
    if not isinstance(streams, dict):
        return {}
    feed_flow = _stream_flow_vol(streams.get('FeedWater.outlet'))
    if not feed_flow or feed_flow <= 0:
        return {}
    ratios: Dict[str, float | None] = {}
    ras_flow = _stream_flow_vol(streams.get('RASPressure.outlet'))
    if ras_flow is not None:
        ratios['RAS_q_over_qin'] = ras_flow / feed_flow
    mlr_flow = _stream_flow_vol(streams.get('IRPressure.outlet'))
    if mlr_flow is not None:
        ratios['MLR_q_over_qin'] = mlr_flow / feed_flow
    return ratios

def _solved_bio_diagnostics(session: FlowsheetSession, *, target_srt: Any) -> Dict[str, Any]:
    """Extract solved ASM diagnostics from persisted WaterTAP state."""
    solved_state = (session.results or {}).get('solved_state') if isinstance(session.results, dict) else None
    units = solved_state.get('units', {}) if isinstance(solved_state, dict) else {}
    if not isinstance(units, dict):
        return {}
    design_parameters = session.config.runtime_metadata.get('design_parameters')
    if not isinstance(design_parameters, dict):
        design_parameters = {}
    reactor_volumes = design_parameters.get('reactor_volumes_m3', {})
    if not isinstance(reactor_volumes, dict):
        reactor_volumes = {}
    reactor_ids = [unit_id for unit_id in ('Anoxic', 'Aerobic', 'PostAnoxic') if unit_id in units]
    biomass_components = ('X_AUT', 'X_H', 'X_PAO')
    per_unit_mlss: Dict[str, float] = {}
    per_unit_biomass: Dict[str, Dict[str, float]] = {}
    inventory = 0.0
    weighted_mlss = 0.0
    total_volume = 0.0
    weighted_components = {component: 0.0 for component in ('X_H', 'X_AUT', 'X_I')}
    for unit_id in reactor_ids:
        unit_payload = units.get(unit_id) or {}
        unit_variables = unit_payload.get('variables') or {}
        if isinstance(unit_variables, dict):
            for candidate in ('control_volume.volume[0.0]', 'control_volume.volume[0]', 'volume[0.0]', 'volume[0]', 'volume'):
                solved_volume = _numeric_value(unit_variables.get(candidate))
                if solved_volume is not None:
                    reactor_volumes[unit_id] = solved_volume
                    break
        outlet = (unit_payload.get('streams') or {}).get('outlet')
        if not isinstance(outlet, dict):
            continue
        volume = reactor_volumes.get(unit_id)
        try:
            volume = float(volume)
        except (TypeError, ValueError):
            volume = None
        mlss_mg_l = _stream_mlss_mg_l(outlet)
        if mlss_mg_l is not None:
            per_unit_mlss[unit_id] = mlss_mg_l
            if volume is not None:
                weighted_mlss += mlss_mg_l * volume
                total_volume += volume
        component_payload: Dict[str, float] = {}
        for component in ('X_H', 'X_AUT', 'X_I', 'X_PAO'):
            concentration = _stream_concentration(outlet, component)
            if concentration is None:
                continue
            component_payload[component] = concentration * 1000.0
            if component in weighted_components and volume is not None:
                weighted_components[component] += concentration * 1000.0 * volume
        if component_payload:
            per_unit_biomass[unit_id] = component_payload
        if volume is not None:
            biomass_concentration = sum((_stream_concentration(outlet, component) or 0.0 for component in biomass_components))
            inventory += volume * biomass_concentration
    mbr_retentate = ((units.get('MBR') or {}).get('streams') or {}).get('retentate')
    mbr_mlss_mg_l = _stream_mlss_mg_l(mbr_retentate)
    aerobic_variables = (units.get('Aerobic') or {}).get('variables') or {}
    aerobic_kla = None
    if isinstance(aerobic_variables, dict):
        for candidate in ('KLa', 'KLa[0]', 'KLa[0.0]'):
            aerobic_kla = _numeric_value(aerobic_variables.get(candidate))
            if aerobic_kla is not None:
                break
    waste = ((units.get('SludgeSplitter') or {}).get('streams') or {}).get('waste')
    waste_flow = _stream_flow_vol(waste)
    waste_biomass = sum((_stream_concentration(waste, component) or 0.0 for component in biomass_components))
    achieved_srt = None
    if inventory > 0 and waste_flow and (waste_biomass > 0):
        achieved_srt = inventory / (waste_flow * waste_biomass) / 86400.0
    if not per_unit_mlss and achieved_srt is None and (mbr_mlss_mg_l is None):
        return {}
    average_mlss = weighted_mlss / total_volume if total_volume > 0 else None
    average_components = {component: value / total_volume for component, value in weighted_components.items() if total_volume > 0}
    cage_to_basin_ratio = None
    try:
        if mbr_mlss_mg_l is not None and average_mlss and (average_mlss > 0):
            cage_to_basin_ratio = mbr_mlss_mg_l / average_mlss
    except (TypeError, ValueError):
        cage_to_basin_ratio = None
    return {'X_H': average_components.get('X_H'), 'X_AUT': average_components.get('X_AUT'), 'X_I': average_components.get('X_I'), 'MLSS': average_mlss, 'MLSS_basin_mg_L': average_mlss, 'MLSS_cage_mg_L': mbr_mlss_mg_l, 'MLSS_cage_to_basin_ratio': cage_to_basin_ratio, 'SRT': achieved_srt, 'srt_target_d': float(target_srt) if target_srt is not None else None, 'srt_achieved_d': achieved_srt, 'per_unit_mlss_mg_l': per_unit_mlss, 'per_unit_biomass_mg_l': per_unit_biomass, 'mbr_mlss_mg_l': mbr_mlss_mg_l, 'recycle_ratios': _solved_recycle_ratios(solved_state), 'biomass_diagnostic_basis': 'solved_state', 'reactor_volumes_m3': reactor_volumes, 'aerobic_kla_per_hr': aerobic_kla, 'srt_constraint': _bio_process_constraint(session), 'process_constraints': session.process_constraints}

def _bio_design_diagnostics(session: FlowsheetSession, *, target_srt: Any) -> Dict[str, Any]:
    """Return Stage 7b ASM diagnostics from solved values or design targets."""
    solved = _solved_bio_diagnostics(session, target_srt=target_srt)
    if solved:
        return solved
    design_parameters = session.config.runtime_metadata.get('design_parameters')
    if not isinstance(design_parameters, dict):
        design_parameters = {}
    target_mlss = design_parameters.get('target_mlss_mg_l')
    try:
        mlss = float(target_mlss) if target_mlss is not None else None
    except (TypeError, ValueError):
        mlss = None
    try:
        srt = float(target_srt) if target_srt is not None else None
    except (TypeError, ValueError):
        srt = None
    if mlss is not None:
        x_h = 0.6 * mlss
        x_aut = 0.05 * mlss
        x_i = 0.35 * mlss
    else:
        x_h = x_aut = x_i = None
    return {'X_H': x_h, 'X_AUT': x_aut, 'X_I': x_i, 'MLSS': mlss, 'SRT': srt, 'srt_target_d': srt, 'srt_achieved_d': srt if session.status.value == 'solved' else None, 'target_mlss_mg_l': mlss, 'recycle_ratios': design_parameters.get('recycle_ratios', {}), 'biomass_diagnostic_basis': 'target_mlss_partition_heuristic', 'reactor_volumes_m3': design_parameters.get('reactor_volumes_m3', {}), 'srt_constraint': _bio_process_constraint(session), 'process_constraints': session.process_constraints}

def _solve_result_metadata(session: FlowsheetSession) -> Dict[str, Any]:
    """Return solver metadata across legacy and pipeline result layouts."""
    if not isinstance(session.results, dict):
        return {}
    result = session.results
    nested = (result.get('details') or {}).get('solve_result') or {}
    if isinstance(nested, dict) and nested:
        return nested
    return result

def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _pipeline_history(session: FlowsheetSession) -> list[Dict[str, Any]]:
    if not isinstance(session.results, dict):
        return []
    history = session.results.get('history')
    return history if isinstance(history, list) else []

def _asm2d_pressure_reference_payload(session: FlowsheetSession) -> Dict[str, Any]:
    metadata = session.config.runtime_metadata or {}
    pressure_reference = metadata.get('asm2d_pressure_reference')
    return dict(pressure_reference) if isinstance(pressure_reference, dict) else {}

def _process_diagnostics_payload(session: FlowsheetSession) -> Dict[str, Any]:
    """Return solved process diagnostics captured by the worker."""
    if isinstance(session.results, dict):
        diagnostics = session.results.get('process_diagnostics')
        if isinstance(diagnostics, dict):
            payload = dict(diagnostics)
            pressure_reference = _asm2d_pressure_reference_payload(session)
            if pressure_reference:
                payload['asm2d_pressure_reference'] = pressure_reference
            return payload
    solved_state = (session.results or {}).get('solved_state') if isinstance(session.results, dict) else {}
    units = solved_state.get('units', {}) if isinstance(solved_state, dict) else {}
    payload = {'schema_version': 'watertap_process_diagnostics/v1', 'coverage_status': 'partial', 'coverage_notes': ['Live Pyomo process diagnostics were not captured for this session; falling back to persisted solved_state unit and stream inventory.'], 'unit_diagnostics': {unit_id: {'unit_type': payload.get('unit_type'), 'diagnostic_family': 'unknown', 'solved_state_ref': f'solved_state.units.{unit_id}', 'state_indicators': {}, 'report_text_available': False} for unit_id, payload in units.items() if isinstance(payload, dict)}, 'unit_reports': {}, 'biological': {'aerobic': {'unit_ids': [], 'rates': {}, 'inhibition': {}, 'kinetic_parameters': {}, 'state_indicators': {}}, 'anaerobic': {'unit_ids': [], 'rates': {}, 'inhibition': {}, 'kinetic_parameters': {}, 'state_indicators': {}}}, 'family_summary': {}}
    pressure_reference = _asm2d_pressure_reference_payload(session)
    if pressure_reference:
        payload['asm2d_pressure_reference'] = pressure_reference
    return payload

def _engine_family_from_diagnostics(session: FlowsheetSession, process_diagnostics: Dict[str, Any]) -> str:
    """Choose the closest simulation_report engine_family for the session."""
    families = process_diagnostics.get('family_summary')
    if not isinstance(families, dict):
        families = {}
    if any((key in families for key in ('aerobic_biological', 'anaerobic_biological'))):
        return 'bio'
    if 'membrane' in families:
        return 'ro'
    if 'thermal' in families:
        return 'thermal'
    if 'ion_exchange' in families:
        return 'ix'
    if any((key in families for key in ('separation', 'electrochemical'))):
        return 'physchem'
    if 'hydraulic' in families:
        return 'physchem'
    unit_types = ' '.join((unit.unit_type for unit in session.units.values())).lower()
    if any((token in unit_types for token in ('asm', 'adm', 'cstr', 'mbr', 'clarifier', 'aeration'))):
        return 'bio'
    if any((token in unit_types for token in ('reverseosmosis', 'nanofiltration', 'membrane'))):
        return 'ro'
    if any((token in unit_types for token in ('evaporator', 'compressor', 'condenser', 'crystallizer'))):
        return 'thermal'
    if 'ionexchange' in unit_types:
        return 'ix'
    return 'physchem'

def _model_type_from_diagnostics(session: FlowsheetSession, engine_family: str, process_diagnostics: Dict[str, Any]) -> str:
    """Return a human-readable model type for proposal diagnostics."""
    unit_types = sorted({unit.unit_type for unit in session.units.values()})
    families = process_diagnostics.get('family_summary')
    family_names = sorted(families) if isinstance(families, dict) else []
    if engine_family == 'bio':
        if 'anaerobic_biological' in family_names:
            return 'ADM1/ASM biological steady-state NLP'
        return 'ASM biological steady-state NLP'
    if engine_family == 'ro':
        return 'WaterTAP membrane steady-state NLP'
    if engine_family == 'thermal':
        return 'WaterTAP thermal/ZLD steady-state NLP'
    if engine_family == 'ix':
        return 'WaterTAP ion-exchange steady-state NLP'
    if unit_types:
        return 'WaterTAP steady-state NLP (' + ', '.join(unit_types[:5]) + ')'
    return 'WaterTAP steady-state NLP'

def _units_for_families(process_diagnostics: Dict[str, Any], family_names: tuple[str, ...]) -> Dict[str, Any]:
    """Return unit diagnostics for a set of process diagnostic families."""
    family_summary = process_diagnostics.get('family_summary')
    unit_diagnostics = process_diagnostics.get('unit_diagnostics')
    if not isinstance(family_summary, dict) or not isinstance(unit_diagnostics, dict):
        return {}
    unit_ids = [unit_id for family in family_names for unit_id in family_summary.get(family, []) if unit_id in unit_diagnostics]
    if not unit_ids:
        return {}
    return {'unit_ids': unit_ids, 'units': {unit_id: unit_diagnostics[unit_id] for unit_id in unit_ids}}

def _process_inhibition_block(process_diagnostics: Dict[str, Any]) -> Dict[str, Any]:
    """Build the biological process inhibition/rate block for reports."""
    biological = process_diagnostics.get('biological')
    if not isinstance(biological, dict):
        return {}
    process_rows = []
    for mode in ('aerobic', 'anaerobic'):
        block = biological.get(mode)
        if not isinstance(block, dict):
            continue
        for unit_id in block.get('unit_ids', []) or []:
            process_rows.append({'process': f'{mode}.{unit_id}', 'rates': (block.get('rates') or {}).get(unit_id, {}), 'inhibition': (block.get('inhibition') or {}).get(unit_id, {}), 'kinetic_parameters': (block.get('kinetic_parameters') or {}).get(unit_id, {}), 'state_indicators': (block.get('state_indicators') or {}).get(unit_id, {})})
    return {'engine': 'watertap-steady-state', 'schema_version': 'watertap_process_inhibition/v1', 'process_inhibition': process_rows, 'aerobic': biological.get('aerobic', {}), 'anaerobic': biological.get('anaerobic', {})}

def _asm_bio_process_details(process_diagnostics: Dict[str, Any]) -> Dict[str, Any]:
    """Return detailed biological rates/inhibition fields for diagnostics.asm_bio."""
    inhibition = _process_inhibition_block(process_diagnostics)
    if not inhibition.get('process_inhibition'):
        return {}
    return {'steady_state_rates': {'aerobic': inhibition.get('aerobic', {}).get('rates', {}), 'anaerobic': inhibition.get('anaerobic', {}).get('rates', {})}, 'process_inhibition': inhibition['process_inhibition'], 'inhibition_factors': {'aerobic': inhibition.get('aerobic', {}).get('inhibition', {}), 'anaerobic': inhibition.get('anaerobic', {}).get('inhibition', {})}, 'kinetic_parameters': {'aerobic': inhibition.get('aerobic', {}).get('kinetic_parameters', {}), 'anaerobic': inhibition.get('anaerobic', {}).get('kinetic_parameters', {})}, 'state_indicators': {'aerobic': inhibition.get('aerobic', {}).get('state_indicators', {}), 'anaerobic': inhibition.get('anaerobic', {}).get('state_indicators', {})}}

def _family_diagnostic_blocks(process_diagnostics: Dict[str, Any]) -> Dict[str, Any]:
    """Build EngineDiagnostics family blocks from process diagnostics."""
    return {'ro': _units_for_families(process_diagnostics, ('membrane',)), 'thermal': _units_for_families(process_diagnostics, ('thermal',)), 'ix': _units_for_families(process_diagnostics, ('ion_exchange',)), 'physchem': _units_for_families(process_diagnostics, ('separation', 'electrochemical', 'hydraulic', 'general')), 'sludge': _units_for_families(process_diagnostics, ('sludge',)), 'odor': _units_for_families(process_diagnostics, ('odor',))}

def _simulation_diagnostics_payload(session: FlowsheetSession) -> Dict[str, Any]:
    """Build persisted diagnostics from the full hygiene-pipeline result."""
    result = session.results if isinstance(session.results, dict) else {}
    details = result.get('details') if isinstance(result.get('details'), dict) else {}
    solve_metadata = _solve_result_metadata(session)
    recovery = result.get('recovery') or details.get('recovery') or {}
    history = _pipeline_history(session)

    def stage_details(stage_name: str) -> Dict[str, Any]:
        for entry in reversed(history):
            if entry.get('state') == stage_name and isinstance(entry.get('details'), dict):
                return entry['details']
        return {}
    post_solve = details.get('post_solve_diagnostics')
    if not isinstance(post_solve, dict):
        post_solve = stage_details('post_solve_diagnostics')
    scaling = stage_details('scaling')
    pre_solve = stage_details('pre_solve_diagnostics')
    recovery_actions = recovery.get('actions_taken') if isinstance(recovery, dict) else None
    recovery_used = bool(result.get('recovered') or details.get('recovered') or recovery)
    pressure_reference = _asm2d_pressure_reference_payload(session)
    payload = {'session_id': session.config.session_id, 'analysis_mode': session.config.runtime_metadata.get('analysis_mode', 'unqualified_workbench'), 'sizing_reference': (session.config.runtime_metadata.get('rating_geometry') or {}).get('sizing_reference'), 'geometry_identity': details.get('geometry_identity'), 'generated_at': datetime.now(timezone.utc).isoformat(), 'pipeline_state': result.get('state'), 'near_feasible_accepted': bool(solve_metadata.get('accepted_near_feasible') or solve_metadata.get('accepted_near_feasible_solve')), 'recovery_used': recovery_used, 'recovery_attempts': len(recovery_actions) if isinstance(recovery_actions, list) else 0, 'recovery_basis': recovery if isinstance(recovery, dict) else {}, 'constraint_residuals': post_solve.get('constraint_residuals', []), 'bound_violations': post_solve.get('bound_violations', []), 'scaling_issues': scaling.get('issues', []), 'structural_diagnostics_summary': {'issues_found': pre_solve.get('structural_issues', 0)}, 'numerical_diagnostics_summary': {'issues_found': pre_solve.get('numerical_issues', 0)}, 'history': history, 'process_diagnostics': _process_diagnostics_payload(session), 'warnings': [warning for entry in history for warning in entry.get('warnings') or [] if isinstance(warning, str)], 'errors': [error for entry in history for error in entry.get('errors') or [] if isinstance(error, str)]}
    if pressure_reference:
        payload['asm2d_pressure_reference'] = pressure_reference
    return payload

def build_simulation_report_payload(session_id: str, project_ref: str, plant_slug: Optional[str]=None, workflow_run_id: Optional[str]=None) -> Dict[str, Any]:
    """Build a canonical simulation_report payload from a WaterTAP session."""
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    solved_state = {}
    if isinstance(session.results, dict):
        solved_state = session.results.get('solved_state') or {}
    streams = solved_state.get('streams', {}) if isinstance(solved_state, dict) else {}
    terminal = _terminal_stream_payload(session) or {}
    srt_constraint = _bio_process_constraint(session)
    convergence_status = 'converged' if session.status.value == 'solved' else 'not_run'
    target_srt = srt_constraint.get('target_days')
    asm_bio = _bio_design_diagnostics(session, target_srt=target_srt)
    solve_metadata = _solve_result_metadata(session)
    process_diagnostics = _process_diagnostics_payload(session)
    asm_bio.update(_asm_bio_process_details(process_diagnostics))
    process_inhibition = _process_inhibition_block(process_diagnostics)
    family_blocks = _family_diagnostic_blocks(process_diagnostics)
    simulation_diagnostics = _simulation_diagnostics_payload(session)
    flowsheet_svg = _session_flowsheet_svg(session)
    engine_family = _engine_family_from_diagnostics(session, process_diagnostics)
    model_type = _model_type_from_diagnostics(session, engine_family, process_diagnostics)
    topology_source = str(session.config.runtime_metadata.get('topology_source') or 'preset_fallback')
    engine_diagnostics = {'topology_source': topology_source, 'universal': {'warnings': simulation_diagnostics['warnings'], 'convergence': {'status': convergence_status, 'message': session.solve_message}, 'solver_metadata': {'solver_name': session.config.solver, 'termination_condition': solve_metadata.get('termination_condition'), 'solve_time_s': _optional_float(solve_metadata.get('solve_time')), 'iterations': _optional_float(solve_metadata.get('iterations')), 'accepted_near_feasible': solve_metadata.get('accepted_near_feasible'), 'recovery_used': simulation_diagnostics['recovery_used'], 'recovery_attempts': simulation_diagnostics['recovery_attempts'], 'dof': session.total_dof, 'internal_only': True}, 'constraint_residuals': simulation_diagnostics['constraint_residuals'], 'bound_violations': simulation_diagnostics['bound_violations'], 'scaling_issues': simulation_diagnostics['scaling_issues'], 'structural_diagnostics_summary': simulation_diagnostics['structural_diagnostics_summary'], 'numerical_diagnostics_summary': simulation_diagnostics['numerical_diagnostics_summary'], 'coverage_status': 'full' if session.status.value == 'solved' else 'partial', 'coverage_notes': ['WaterTAP steady-state equation-oriented solve; no transient trajectory.', 'Full solved variables are in metadata.process_diagnostics and solved_state.']}, 'process_diagnostics': process_diagnostics, 'unit_diagnostics': process_diagnostics.get('unit_diagnostics', {}), 'unit_reports': process_diagnostics.get('unit_reports', {}), 'process_inhibition': process_inhibition}
    if engine_family == 'bio':
        engine_diagnostics['asm_bio'] = asm_bio
    for family_key, family_payload in family_blocks.items():
        if family_payload:
            engine_diagnostics[family_key] = family_payload
    payload = {'project_ref': project_ref, 'stage': 'c', 'plant_slug': plant_slug, 'workflow_run_id': workflow_run_id, 'generated_at': datetime.now(timezone.utc).isoformat(), 'unit_ops': list(session.units), 'predicted_effluent': terminal, 'engine_reports': [{'engine': 'watertap-engine-mcp', 'engine_family': engine_family, 'model_type': model_type, 'unit_ops': list(session.units), 'flowsheet_svg': flowsheet_svg, 'predicted_streams': streams, 'predicted_effluent': terminal, 'diagnostics': engine_diagnostics, 'risk_flags': [], 'timeseries': None}], 'metadata': {'engine': 'watertap-engine-mcp', 'engine_run_id': session_id, 'session_id': session_id, 'topology_source': topology_source, 'process_constraints': session.process_constraints, 'simulation_diagnostics': simulation_diagnostics, 'process_diagnostics': process_diagnostics}, 'provenance': 'simulator', 'engine_run_id': session_id, 'flowsheet_svg': flowsheet_svg}
    try:
        from puran_mcp_support.engineering.models.simulation_report import SimulationReport
        SimulationReport(**payload)
    except Exception as exc:
        return {'error': f'simulation_report payload validation failed: {exc}'}
    return payload

def build_simulation_diagnostics_payload(session_id: str, project_ref: Optional[str]=None, plant_slug: Optional[str]=None) -> Dict[str, Any]:
    """Build the standalone diagnostics payload for a solved WaterTAP session."""
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    metadata = session.config.runtime_metadata or {}
    resolved_project_ref = project_ref or metadata.get('project_ref')
    process_diagnostics = _process_diagnostics_payload(session)
    simulation_diagnostics = _simulation_diagnostics_payload(session)
    solved_state = session.results.get('solved_state', {}) if isinstance(session.results, dict) else {}
    return {'schema_version': 'watertap_simulation_diagnostics/v1', 'project_ref': resolved_project_ref, 'plant_slug': plant_slug or metadata.get('plant_slug'), 'engine': 'watertap-engine-mcp', 'engine_run_id': session_id, 'session_id': session_id, 'generated_at': datetime.now(timezone.utc).isoformat(), 'status': session.status.value, 'unit_ops': list(session.units), 'process_constraints': session.process_constraints, 'simulation_diagnostics': simulation_diagnostics, 'process_diagnostics': process_diagnostics, 'solved_state': solved_state, 'accessibility_contract': {'intended_reader': 'pe-process', 'human_reporting_ready': True, 'contains_unit_report_text': bool(process_diagnostics.get('unit_reports')), 'contains_biological_rates': bool(process_diagnostics.get('process_health') or process_diagnostics.get('biological', {}).get('aerobic', {}).get('rates') or process_diagnostics.get('biological', {}).get('anaerobic', {}).get('rates')), 'contains_biological_inhibition': bool(process_diagnostics.get('process_health') or process_diagnostics.get('biological', {}).get('aerobic', {}).get('inhibition') or process_diagnostics.get('biological', {}).get('anaerobic', {}).get('inhibition')), 'contains_structured_process_health': bool(process_diagnostics.get('process_health'))}}

def get_process_diagnostics(session_id: str, project_ref: Optional[str]=None, plant_slug: Optional[str]=None) -> Dict[str, Any]:
    """Return persisted-ready process diagnostics for pe-process reporting."""
    return build_simulation_diagnostics_payload(session_id=session_id, project_ref=project_ref, plant_slug=plant_slug)

def _sink_tag_for_stream(stream_id: str) -> str:
    lower = stream_id.lower()
    if 'permeate' in lower or 'effluent' in lower or 'product' in lower:
        return 'sink.final_effluent'
    if 'brine' in lower or 'retentate' in lower or 'waste' in lower:
        return 'sink.residuals'
    if 'vapor' in lower or 'distillate' in lower:
        return 'sink.distillate'
    return 'sink.unspecified'

def _collect_stream_snapshots(session: FlowsheetSession, *, project_ref: str, plant_slug: Optional[str], workflow_run_id: Optional[str], model_run_id: str) -> list[Dict[str, Any]]:
    streams = _solved_streams(session)
    consumers: Dict[str, list[str]] = {}
    for conn in session.connections:
        consumers.setdefault(f'{conn.source_unit}.{conn.source_port}', []).append(conn.dest_unit)
    snapshots: list[Dict[str, Any]] = []
    for stream_id, stream_state in streams.items():
        if not isinstance(stream_state, dict):
            continue
        upstream = stream_id.split('.', 1)[0] if '.' in stream_id else None
        downstream_tags = consumers.get(stream_id) or [_sink_tag_for_stream(stream_id)]
        for downstream in downstream_tags:
            scope_refs = {'project_ref': project_ref, 'plant_slug': plant_slug, 'engine': 'watertap', 'model_run_id': model_run_id}
            if workflow_run_id:
                scope_refs['workflow_run_id'] = workflow_run_id
            snapshots.append({'stream_id': stream_id, 'upstream_unit_tag': upstream, 'downstream_unit_tag': downstream, 'flow_m3_d': _stream_flow_m3_d(stream_state), 'temperature_k': _stream_temperature_k(stream_state), 'pressure_kpa': _stream_pressure_kpa(stream_state), 'components': _stream_components(stream_state), 'model_run_id': model_run_id, 'status_code': 'completed', 'scope_refs': scope_refs})
    return snapshots

def _persist_stream_snapshots(session: FlowsheetSession, *, project_ref: str, plant_slug: Optional[str], workflow_run_id: Optional[str]) -> Dict[str, Any]:
    if not os.environ.get('ENGINEERING_MCP_DATABASE_URL'):
        return {'skipped': 'ENGINEERING_MCP_DATABASE_URL not set'}
    try:
        from watertap_engine_mcp.public_persistence import persist_stream_snapshot
    except Exception as exc:
        return {'error': f'stream_snapshot persistence unavailable: {exc}'}
    snapshot_ids: Dict[str, Any] = {}
    snapshots = _collect_stream_snapshots(session, project_ref=project_ref, plant_slug=plant_slug, workflow_run_id=workflow_run_id, model_run_id=session.config.session_id)
    for snapshot in snapshots:
        snapshot_id = persist_stream_snapshot(project_ref=project_ref, stream_id=snapshot['stream_id'], components=snapshot.get('components') or {}, producer_tool='watertap-engine-mcp.persist_simulation_artifacts', flow_m3_d=snapshot.get('flow_m3_d'), temperature_k=snapshot.get('temperature_k'), pressure_kpa=snapshot.get('pressure_kpa'), upstream_unit_tag=snapshot.get('upstream_unit_tag'), downstream_unit_tag=snapshot.get('downstream_unit_tag'), model_run_id=session.config.session_id, status_code=snapshot.get('status_code'), credibility={'model_status': 'preliminary', 'decision_grade': 'budgetary', 'validation_basis': 'literature'}, scope_refs=snapshot.get('scope_refs'))
        if snapshot_id is not None:
            key = f'{snapshot['stream_id']}->{snapshot.get('downstream_unit_tag')}'
            snapshot_ids[key] = snapshot_id
    return {'stream_snapshot_ids': snapshot_ids, 'snapshot_count': len(snapshot_ids)}

def _safe_filename(value: str) -> str:
    return re.sub('[^A-Za-z0-9_.-]+', '_', value).strip('_') or 'stream'

def _resolve_mcp_outputs_dir(session: FlowsheetSession, mcp_outputs_dir: Optional[str]) -> Path | None:
    if mcp_outputs_dir:
        return Path(mcp_outputs_dir)
    metadata = session.config.runtime_metadata or {}
    if metadata.get('mcp_outputs_dir'):
        return Path(str(metadata['mcp_outputs_dir']))
    if metadata.get('project_dir'):
        return Path(str(metadata['project_dir'])) / 'mcp-outputs'
    if os.environ.get('WATERTAP_ENGINE_MCP_OUTPUTS_DIR'):
        return Path(os.environ['WATERTAP_ENGINE_MCP_OUTPUTS_DIR'])
    return None

def _mirror_simulation_outputs(session: FlowsheetSession, *, report_payload: Dict[str, Any], diagnostics_payload: Dict[str, Any], project_ref: str, plant_slug: Optional[str], mcp_outputs_dir: Optional[str]=None) -> Dict[str, Any]:
    output_dir = _resolve_mcp_outputs_dir(session, mcp_outputs_dir)
    if output_dir is None:
        return {'skipped': 'No mcp_outputs_dir, project_dir, or WATERTAP_ENGINE_MCP_OUTPUTS_DIR configured'}
    streams_dir = output_dir / 'streams'
    diagnostics_dir = output_dir / 'diagnostics'
    figures_dir = output_dir / 'figures'
    for directory in (output_dir, streams_dir, diagnostics_dir, figures_dir):
        directory.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / 'simulation_report.json'
    report_path.write_text(json.dumps(report_payload, indent=2, default=str))
    diagnostics_path = diagnostics_dir / f'{_safe_filename(session.config.session_id)}.json'
    diagnostics_path.write_text(json.dumps(diagnostics_payload, indent=2, default=str))
    flowsheet_svg = report_payload['engine_reports'][0]['flowsheet_svg']
    flowsheet_path = figures_dir / 'flowsheet.svg'
    flowsheet_path.write_text(flowsheet_svg)
    stream_paths: Dict[str, str] = {}
    for stream_id, payload in _solved_streams(session).items():
        stream_path = streams_dir / f'{_safe_filename(stream_id)}.json'
        stream_doc = {'project_ref': project_ref, 'plant_slug': plant_slug, 'stream_id': stream_id, 'flow_m3_d': _stream_flow_m3_d(payload), 'temperature_k': _stream_temperature_k(payload), 'pressure_kpa': _stream_pressure_kpa(payload), 'components': _stream_components(payload), 'raw': payload}
        stream_path.write_text(json.dumps(stream_doc, indent=2, default=str))
        stream_paths[stream_id] = str(stream_path)
    return {'mcp_outputs_dir': str(output_dir), 'simulation_report': str(report_path), 'diagnostics': str(diagnostics_path), 'flowsheet_svg': str(flowsheet_path), 'streams': stream_paths}

def _ensure_simulation_diagnostics_artifact_type() -> None:
    """Ensure the canonical DB accepts WaterTAP diagnostics artifacts."""
    dsn = os.environ.get('ENGINEERING_MCP_DATABASE_URL') or os.environ.get('ENGINEERING_DATABASE_URL') or os.environ.get('DATABASE_URL')
    if not dsn:
        return
    try:
        import psycopg2
    except ImportError:
        return
    conn = psycopg2.connect(dsn.replace('+asyncpg', ''))
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('app.user_id', %s, true)", ('watertap-engine-mcp.persist_simulation_artifacts',))
                cur.execute("SELECT set_config('app.reason', %s, true)", ('register simulation_diagnostics artifact type',))
                cur.execute('\n                    INSERT INTO artifact_type_registry (\n                        artifact_type,\n                        description,\n                        pydantic_class,\n                        proposal_surface_status\n                    )\n                    VALUES (%s, %s, %s, %s)\n                    ON CONFLICT (artifact_type) DO UPDATE\n                    SET description = EXCLUDED.description,\n                        pydantic_class = EXCLUDED.pydantic_class,\n                        proposal_surface_status = EXCLUDED.proposal_surface_status\n                    ', ('simulation_diagnostics', 'WaterTAP steady-state diagnostics for human reporting and offline proposal rendering', None, 'rendered'))
    finally:
        conn.close()

def _ensure_solver_infeasibility_report_artifact_type() -> None:
    """Ensure the canonical DB accepts WaterTAP infeasibility report artifacts."""
    dsn = os.environ.get('ENGINEERING_MCP_DATABASE_URL') or os.environ.get('ENGINEERING_DATABASE_URL') or os.environ.get('DATABASE_URL')
    if not dsn:
        return
    try:
        import psycopg2
    except ImportError:
        return
    conn = psycopg2.connect(dsn.replace('+asyncpg', ''))
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('app.user_id', %s, true)", ('watertap-engine-mcp.persist_solver_infeasibility_report',))
                cur.execute("SELECT set_config('app.reason', %s, true)", ('register solver_infeasibility_report artifact type',))
                cur.execute('\n                    INSERT INTO artifact_type_registry (\n                        artifact_type,\n                        description,\n                        pydantic_class,\n                        proposal_surface_status\n                    )\n                    VALUES (%s, %s, %s, %s)\n                    ON CONFLICT (artifact_type) DO UPDATE\n                    SET description = EXCLUDED.description,\n                        pydantic_class = EXCLUDED.pydantic_class,\n                        proposal_surface_status = EXCLUDED.proposal_surface_status\n                    ', ('solver_infeasibility_report', 'Structured WaterTAP solver infeasibility diagnostics and remediation recommendations', 'puran_mcp_support.engineering.models.solver_infeasibility_report.SolverInfeasibilityReport', 'post_commissioning'))
    finally:
        conn.close()

def persist_solver_infeasibility_report(*args, **kwargs):
    raise NotImplementedError('This application integration is excluded from the public release; use the general engine APIs with your own inputs.')

def persist_simulation_artifacts(*args, **kwargs):
    raise NotImplementedError('This application integration is excluded from the public release; use the general engine APIs with your own inputs.')

def create_watertap_session(name: str='', description: str='', session_preset: Optional[str]=None, property_package: str='SEAWATER', property_package_config: Optional[Dict[str, Any]]=None, session_id: Optional[str]=None, reaction_config: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
    """Create a new WaterTAP flowsheet session (alias for create_session).

    Args:
        name: Optional session name
        description: Optional session description
        property_package: Default property package
        property_package_config: Optional property package configuration
        session_id: Optional explicit session identifier
        reaction_config: Optional ModifiedASM2d model-form variant (as create_session)

    Returns:
        Dict with session_id and configuration details
    """
    return create_session(name=name, description=description, session_preset=session_preset, property_package=property_package, property_package_config=property_package_config, session_id=session_id, reaction_config=reaction_config)

def get_unit_requirements(unit_type: str, property_package: Optional[str]=None, property_package_config: Optional[Dict[str, Any]]=None, config: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
    """Get the client-facing contract for a unit type.

    Args:
        unit_type: Type of unit (e.g., "ReverseOsmosis0D", "Pump")
        property_package: Optional package to validate against a live build
        property_package_config: Optional package configuration for the preview build
        config: Optional unit constructor config for the preview build

    Returns:
        Unit requirements including static metadata and optional live preview
    """
    try:
        spec = get_unit_spec_from_registry(unit_type)
    except KeyError:
        return {'error': f'Unknown unit type: {unit_type}'}
    result = {'unit_type': unit_type, 'category': spec.category.name if spec.category else None, 'engineering_requirements': {'engineering_inputs': [_engineering_input_payload(item) for item in effective_engineering_inputs(spec, spec.compatible_property_packages[0], config)], 'input_contract': unit_input_contract_summary(spec, spec.compatible_property_packages[0], config), 'typical_values': spec.typical_values}, 'ports': {'inlets': spec.n_inlets, 'outlets': spec.n_outlets, 'inlet_names': spec.inlet_names, 'outlet_names': spec.outlet_names}, 'compatible_property_packages': [pkg.name for pkg in spec.compatible_property_packages], 'config_schema': _build_unit_config_schema(unit_type), 'is_idaes_unit': spec.is_idaes_unit, 'module_path': spec.module_path}
    if property_package:
        result['live_preview'] = preview_unit_build(unit_type=unit_type, property_package=property_package, property_package_config=property_package_config, config=config)
    return result

def _unit_sd_playbook(unit_type: str, spec: Any) -> Dict[str, Any]:
    """Return the dynamic SD playbook classification for one registry unit."""
    if not spec.module_path.startswith('watertap.unit_models'):
        return {'recipe_name': None, 'family': None, 'confidence': 0.0, 'named': True}
    try:
        from watertap_engine_mcp.solver.initializer import FlowsheetInitializer
        fake_unit = type('FakeUnit', (), {'config': type('Config', (), {'property_package': None})()})()
        topology = FlowsheetInitializer(unit_types={'Unit': unit_type}).classify_topology({'Unit': fake_unit}, [])
        recipe_name = topology.get('recipe_name', 'generic_sequential')
        return {'recipe_name': recipe_name, 'family': topology.get('family'), 'confidence': topology.get('confidence', 0.0), 'named': recipe_name != 'generic_sequential'}
    except Exception as exc:
        return {'recipe_name': None, 'family': None, 'confidence': 0.0, 'named': False, 'error': str(exc)}

def _package_versions() -> Dict[str, Optional[str]]:
    """Return installed WaterTAP stack versions for coverage reports."""
    try:
        import importlib.metadata as metadata
    except Exception:
        return {'watertap': None, 'idaes-pse': None, 'pyomo': None}
    versions: Dict[str, Optional[str]] = {}
    for package_name in ('watertap', 'idaes-pse', 'pyomo'):
        try:
            versions[package_name] = metadata.version(package_name)
        except Exception:
            versions[package_name] = None
    return versions

def get_upstream_unit_coverage_matrix(include_idaes: bool=False, verbose: bool=False) -> Dict[str, Any]:
    """Return the generated WaterTAP unit registration/input/playbook matrix.

    The default is a compact certification summary. Pass ``verbose=True`` to
    return the full per-unit matrix.
    """
    rows = []
    for unit_type, spec in sorted(UNITS.items()):
        if not include_idaes and (not spec.module_path.startswith('watertap.unit_models')):
            continue
        input_contract = unit_input_contract_summary(spec, spec.compatible_property_packages[0], None)
        sd_playbook = _unit_sd_playbook(unit_type, spec)
        rows.append({'unit_type': unit_type, 'class_name': spec.class_name, 'module_path': spec.module_path, 'origin': 'watertap' if spec.module_path.startswith('watertap.') else 'idaes' if spec.module_path.startswith('idaes.') else 'other', 'category': spec.category.value, 'compatible_property_packages': [package.name for package in spec.compatible_property_packages], 'solve_supported': spec.solve_supported, 'costing_supported': spec.costing.supported, 'input_contract': input_contract, 'sd_playbook': sd_playbook, 'coverage': {'registered': True, 'has_process_input_contract': bool(input_contract['complete']), 'has_named_sd_playbook': bool(sd_playbook['named'])}})
    input_contract_gaps = [row for row in rows if row['solve_supported'] and (not row['coverage']['has_process_input_contract'])]
    unnamed_sd_playbook_gaps = [row for row in rows if row['origin'] == 'watertap' and (not row['coverage']['has_named_sd_playbook'])]
    process_family_counts: Dict[str, int] = {}
    engine_counts: Dict[str, int] = {}
    category_counts: Dict[str, int] = {}
    package_counts: Dict[str, int] = {}
    solve_supported_count = 0
    costing_supported_count = 0
    for row in rows:
        family = row['sd_playbook'].get('family') or 'unclassified'
        process_family_counts[family] = process_family_counts.get(family, 0) + 1
        engine_counts[row['origin']] = engine_counts.get(row['origin'], 0) + 1
        category_counts[row['category']] = category_counts.get(row['category'], 0) + 1
        for package in row['compatible_property_packages']:
            package_counts[package] = package_counts.get(package, 0) + 1
        if row['solve_supported']:
            solve_supported_count += 1
        if row['costing_supported']:
            costing_supported_count += 1
    certified = not input_contract_gaps and (not unnamed_sd_playbook_gaps)
    versions = _package_versions()
    certified_unit_count = len(rows) - len({row['unit_type'] for row in input_contract_gaps + unnamed_sd_playbook_gaps})
    response = {'versions': versions, 'include_idaes': include_idaes, 'verbose': verbose, 'total_units': len(rows), 'registered_units': len(rows), 'certified': certified, 'certification_summary': {'scope': 'installed upstream WaterTAP unit classes', 'registered_unit_count': len(rows), 'certified_unit_count': certified_unit_count, 'input_contract_gap_count': len(input_contract_gaps), 'unnamed_sd_playbook_gap_count': len(unnamed_sd_playbook_gaps), 'process_family_counts': process_family_counts, 'waterTAP_compatibility': versions}, 'input_contract_gap_count': len(input_contract_gaps), 'unnamed_sd_playbook_gap_count': len(unnamed_sd_playbook_gaps), 'counts': {'by_engine': dict(sorted(engine_counts.items())), 'by_package': dict(sorted(package_counts.items())), 'by_unit_class': dict(sorted(category_counts.items())), 'by_process_family': dict(sorted(process_family_counts.items())), 'solve_supported': solve_supported_count, 'costing_supported': costing_supported_count}, 'summary_only': not verbose, 'verbose_available': True}
    compact_input_gaps = [{'unit_type': row['unit_type'], 'mode': row['input_contract']['mode'], 'next_step': row['input_contract']['next_step']} for row in input_contract_gaps]
    compact_playbook_gaps = [{'unit_type': row['unit_type'], 'recipe_name': row['sd_playbook']['recipe_name']} for row in unnamed_sd_playbook_gaps]
    verbose_input_gaps = [{'unit_type': row['unit_type'], 'module_path': row['module_path'], 'mode': row['input_contract']['mode'], 'next_step': row['input_contract']['next_step']} for row in input_contract_gaps]
    verbose_playbook_gaps = [{'unit_type': row['unit_type'], 'module_path': row['module_path'], 'recipe_name': row['sd_playbook']['recipe_name']} for row in unnamed_sd_playbook_gaps]
    if verbose:
        response['input_contract_gaps'] = verbose_input_gaps
        response['unnamed_sd_playbook_gaps'] = verbose_playbook_gaps
        response['rows'] = rows
    else:
        response['input_contract_gaps'] = compact_input_gaps
        response['unnamed_sd_playbook_gaps'] = compact_playbook_gaps
        response['sample_unit_types'] = [row['unit_type'] for row in rows[:10]]
    return response

def connect_units(session_id: str, source_unit: str, source_port: str, dest_unit: str, dest_port: str, auto_create_translator: bool=False) -> Dict[str, Any]:
    """Connect two units with automatic translator detection.

    Checks property package compatibility and suggests a curated translator if needed.

    Args:
        session_id: Session containing the units
        source_unit: Source unit ID
        source_port: Source port name
        dest_unit: Destination unit ID
        dest_port: Destination port name
        auto_create_translator: If True, auto-create ASM/ADM translator if needed

    Returns:
        Connection result with translator info if applicable
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    from watertap_engine_mcp.utils.auto_translator import check_connection_compatibility
    src_unit_inst = session.units.get(source_unit)
    dst_unit_inst = session.units.get(dest_unit)
    source_pkg = None
    dest_pkg = None
    translator_id = None
    translator_note = None
    if src_unit_inst and dst_unit_inst:
        source_pkg = _unit_port_package_type(session, source_unit, source_port)
        dest_pkg = _unit_port_package_type(session, dest_unit, dest_port)
        if source_pkg and dest_pkg:
            compat = check_connection_compatibility(source_pkg, dest_pkg)
            if compat['compatible']:
                if compat['needs_translator']:
                    if auto_create_translator:
                        translator_id = f'translator_{source_unit}_to_{dest_unit}'
                        translator_spec = get_translator(source_pkg, dest_pkg)
                        if translator_spec is not None:
                            session.translators[translator_id] = {'name': translator_spec.name, 'source_pkg': source_pkg.value, 'dest_pkg': dest_pkg.value, 'module_path': translator_spec.module_path, 'config': {}}
                            session_manager.save(session)
                        translator_note = f"Auto-created translator '{translator_id}' for {source_pkg.name} → {dest_pkg.name} connection."
                    else:
                        translator_note = f'Translator needed: {compat['translator']} for {source_pkg.name} → {dest_pkg.name}. Set auto_create_translator=True or use create_translator.'
                else:
                    translator_note = 'Same property package - direct connection.'
            else:
                return {'error': f'No supported translator exists for {source_pkg.name} → {dest_pkg.name}. WaterTAP topology builds fail loudly instead of creating a connection that may fail during solve.'}
        else:
            translator_note = 'Property packages not configured on units. If packages differ, create translator explicitly.'
    result = connect_ports(session_id, source_unit, source_port, dest_unit, dest_port, translator_id=translator_id)
    if 'error' not in result and translator_note:
        result['translator_note'] = translator_note
    return result

def build_and_solve(session_id: str, solver: Optional[str]=None, solver_options: Optional[Dict[str, Any]]=None, project_ref: Optional[str]=None, plant_slug: Optional[str]=None, workflow_run_id: Optional[str]=None, mcp_outputs_dir: Optional[str]=None, max_wall_time_s: int=300, idempotency_key: Optional[str]=None) -> Dict[str, Any]:
    """Execute full hygiene pipeline: DOF check, scaling, init, solve.

    Convenience wrapper that runs the complete solve pipeline:
    1. DOF check
    2. Calculate scaling factors
    3. Sequential initialization
    4. Solve

    Args:
        session_id: Session to build and solve
        solver: Solver name
        solver_options: Optional solver configuration

    Returns:
        Job ID for background execution
    """
    validation_error = _finite_validation_error('build_and_solve', {'session_id': session_id, 'solver': solver, 'solver_options': solver_options, 'project_ref': project_ref, 'plant_slug': plant_slug, 'workflow_run_id': workflow_run_id, 'mcp_outputs_dir': mcp_outputs_dir, 'max_wall_time_s': max_wall_time_s, 'idempotency_key': idempotency_key})
    if validation_error is not None:
        return validation_error
    if workflow_run_id is not None:
        try:
            workflow_run_id = str(uuid.UUID(str(workflow_run_id)))
        except (TypeError, ValueError, AttributeError):
            return {'error': 'workflow_run_id must be a UUID'}
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    with _solve_submission_lock:
        lease_decision = _session_job_leases().check(session_id=session_id, idempotency_key=idempotency_key, job_status_lookup=_job_status_value)
        if lease_decision.replay:
            return {'job_id': lease_decision.job_id, 'session_id': session_id, 'status': lease_decision.status or 'running', 'message': lease_decision.message, 'idempotency_key': idempotency_key, 'idempotent_replay': True}
        if lease_decision.conflict:
            return {'error': 'SESSION_SOLVE_IN_PROGRESS', 'code': 'SESSION_SOLVE_IN_PROGRESS', 'session_id': session_id, 'active_job_id': lease_decision.job_id, 'active_job_status': lease_decision.status, 'message': lease_decision.message}
        selected_solver, problem_type, dispatch_error = dispatch_solver_for_session(session, solver)
        if dispatch_error is not None:
            return dispatch_error
        prior_status = session.status
        prior_runtime_metadata = deepcopy(session.config.runtime_metadata)
        if project_ref:
            session.config.runtime_metadata['project_ref'] = project_ref
        if plant_slug:
            session.config.runtime_metadata['plant_slug'] = plant_slug
        if workflow_run_id:
            session.config.runtime_metadata['workflow_run_id'] = workflow_run_id
        if mcp_outputs_dir:
            session.config.runtime_metadata['mcp_outputs_dir'] = mcp_outputs_dir
        session.config.runtime_metadata['solver_pipeline_started_at'] = datetime.now(timezone.utc).isoformat()
        session.set_solving()
        session_manager.save(session)
        runtime_solver_options = session.config.runtime_metadata.get('solver_options')
        merged_solver_options = {}
        if isinstance(runtime_solver_options, dict):
            merged_solver_options.update(runtime_solver_options)
        merged_solver_options.update(solver_options or {})
        is_asm2d_family = RoleResolver.from_session(session).is_asm2d_recycle_family()
        allow_near_feasible_asm2d = is_asm2d_family
        params = {'run_full_pipeline': True, 'solver': selected_solver, 'detected_problem_type': problem_type, 'solver_options': merged_solver_options, 'session_storage_dir': str(session_manager.storage_dir), 'session_snapshot': session.to_dict(), 'project_ref': session.config.runtime_metadata.get('project_ref'), 'plant_slug': session.config.runtime_metadata.get('plant_slug'), 'workflow_run_id': session.config.runtime_metadata.get('workflow_run_id'), 'mcp_outputs_dir': session.config.runtime_metadata.get('mcp_outputs_dir'), 'max_wall_time_s': int(max_wall_time_s), 'allow_near_feasible_asm2d': allow_near_feasible_asm2d}
        try:
            job = job_manager.submit(session_id, 'solve', params)
        except RuntimeError as exc:
            session.status = prior_status
            session.config.runtime_metadata = prior_runtime_metadata
            session_manager.save(session)
            return {'error': 'SOLVER_CAPACITY_REACHED', 'code': 'SOLVER_CAPACITY_REACHED', 'session_id': session_id, 'message': str(exc)}
        _session_job_leases().record(session_id=session_id, job_id=job.job_id, idempotency_key=idempotency_key)
        return {'job_id': job.job_id, 'session_id': session_id, 'status': job.status.value, 'message': 'Full hygiene pipeline submitted. Poll with get_solve_status.', 'idempotency_key': idempotency_key, 'solver': selected_solver or 'auto', 'detected_problem_type': problem_type}

def _clone_session_object(session: FlowsheetSession, *, new_session_id: Optional[str]=None, name: Optional[str]=None, include_results: bool=True) -> FlowsheetSession:
    """Return a deep, independent in-memory copy of a session with a new id.

    Round-tripping through ``to_dict``/``from_dict`` produces a fully detached
    clone (units, connections, feed states, fixed vars, process constraints,
    metadata, and optionally the solved results) so a GSA driver can perturb and
    solve it without mutating the canonical session.
    """
    clone = FlowsheetSession.from_dict(deepcopy(session.to_dict()))
    clone.config.session_id = new_session_id or str(uuid.uuid4())
    if name is not None:
        clone.config.name = name
    clone.config.runtime_metadata = dict(clone.config.runtime_metadata or {})
    clone.config.runtime_metadata['cloned_from_session_id'] = session.config.session_id
    if not include_results:
        clone.results = None
        clone.solve_status = None
        clone.solve_message = None
        clone.status = SessionStatus.BUILDING
    return clone

def clone_session(session_id: str, new_session_id: Optional[str]=None, name: Optional[str]=None, include_results: bool=True) -> Dict[str, Any]:
    """Clone a flowsheet session into a new persisted session.

    Mirrors the QSDsan ``clone_session`` capability for WaterTAP: copies the
    full session graph (and, by default, the solved results) under a new
    session id. Useful for branching off-design / sensitivity scenarios from a
    solved canonical design point without touching the original.

    Args:
        session_id: Source session to clone
        new_session_id: Optional explicit id for the clone (generated if omitted)
        name: Optional name override for the clone
        include_results: Carry the solved results/state into the clone (default True)

    Returns:
        Clone metadata (new session id, source id, unit/connection counts)
    """
    try:
        source = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    if new_session_id and session_manager.exists(new_session_id):
        return {'error': f"Session '{new_session_id}' already exists"}
    clone = _clone_session_object(source, new_session_id=new_session_id, name=name or f'{source.config.name or session_id}-clone', include_results=include_results)
    session_manager.save(clone)
    return {'session_id': clone.config.session_id, 'cloned_from': session_id, 'name': clone.config.name, 'units': len(clone.units), 'connections': len(clone.connections), 'include_results': bool(include_results), 'status': clone.status.value}

def _solve_for_error(err: Any) -> Dict[str, Any]:
    """Convert a SolveForError into the canonical api.errors envelope dict."""
    from watertap_engine_mcp.api.errors import make_error
    return make_error(err.code, err.message, details=err.details).model_dump(mode='python')

def _num_or_none(value: Any) -> Optional[float]:
    """Coerce a Pyomo bound / value to a finite float or None."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out

def _safe_var_value(var: Any) -> Optional[float]:
    """Return value(var) as a finite float, or None."""
    try:
        from pyomo.environ import value as _pyovalue
        return _num_or_none(_pyovalue(var, exception=False))
    except Exception:
        return None

def _solve_for_freed_actuators(builder: Any) -> Dict[str, Any]:
    """Return {var_name: var} for the actuators a build freed (excludes the
    legacy controlled-variable unfix entries)."""
    out: Dict[str, Any] = {}
    for entry in getattr(builder, '_target_control_freed_actuators', []) or []:
        if not isinstance(entry, dict):
            continue
        ctype = str(entry.get('constraint_type') or '')
        if ctype.endswith('_controlled'):
            continue
        var = entry.get('var')
        if var is None:
            continue
        try:
            out[var.name] = var
        except Exception:
            continue
    return out

def _incidence_same_component(igraph: Any, actuator_var: Any, target_constraints: List[Any]) -> bool:
    """Return True when ``actuator_var`` and at least one target constraint live in
    the SAME connected component of ``igraph`` (an IncidenceGraphInterface).

    ``get_connected_components()`` returns ``(var_blocks, con_blocks)`` partitioned
    component-for-component, so the actuator's component is the var-block that
    contains it and we check that component's matching con-block for a target.
    """
    var_blocks, con_blocks = igraph.get_connected_components()
    target_ids = {id(c) for c in target_constraints}
    for var_block, con_block in zip(var_blocks, con_blocks):
        if any((v is actuator_var or id(v) == id(actuator_var) for v in var_block)):
            return any((id(c) in target_ids for c in con_block))
    return False

def _solve_for_structural_validation(model: Any, actuator_var: Any, target_constraints: List[Any], *, require_perfect_matching: bool, include_inequality: bool) -> tuple:
    """Validate the prepared swap is structurally well-posed. FAILS CLOSED.

    Uses IDAES ``IncidenceGraphInterface`` over the ACTIVE EQUALITY constraints +
    UNFIXED variables (fixed property/reaction-parameter Vars are excluded by
    construction — the reason the previous hand-rolled BFS existed). It checks:

      (a) the actuator var and the target constraint are in the SAME connected
          component (the actuator can actually influence the target), and
      (b) for an exact (square, DOF==0) swap, the active-equality subsystem has a
          PERFECT MATCHING / is structurally nonsingular — no unmatched variable,
          no unmatched/overconstrained constraint (Dulmage–Mendelsohn).

    Returns one of:
      ("ok",           details)
      ("disconnected", details)   actuator + target are in disjoint components
      ("singular",     details)   square equality subsystem has no perfect matching
      ("error",        details)   analysis could not run -> caller FAILS CLOSED

    ``require_perfect_matching`` is True only for exact swaps; a min/max
    optimization swap intentionally leaves the freed objective var unmatched in the
    equality subsystem, so the matching test is skipped for it.
    """
    try:
        from pyomo.contrib.incidence_analysis import IncidenceGraphInterface
        igraph = IncidenceGraphInterface(model, include_inequality=include_inequality)
        if actuator_var is not None and target_constraints:
            if not _incidence_same_component(igraph, actuator_var, target_constraints):
                return ('disconnected', {})
        if require_perfect_matching:
            var_partition, con_partition = igraph.dulmage_mendelsohn()
            unmatched_vars = [getattr(v, 'name', str(v)) for v in var_partition.unmatched]
            unmatched_cons = [getattr(c, 'name', str(c)) for c in con_partition.unmatched]
            over_cons = [getattr(c, 'name', str(c)) for c in con_partition.overconstrained]
            under_cons = [getattr(c, 'name', str(c)) for c in getattr(con_partition, 'underconstrained', [])]
            if unmatched_vars or unmatched_cons or over_cons:
                return ('singular', {'unmatched_variables': unmatched_vars, 'unmatched_constraints': unmatched_cons, 'overconstrained_constraints': over_cons, 'underconstrained_constraints': under_cons})
        return ('ok', {})
    except Exception as exc:
        return ('error', {'exception': repr(exc)})

def _solve_for_recycle_hydraulic_actuator(target_constraints: List[Any], ratio_var: Any) -> Optional[Any]:
    """Return the REAL unfixed hydraulic variable a recycle-ratio closure determines.

    The recycle-ratio controller FIXES its ratio var and adds the closure
    ``numerator_flow == ratio * denominator_flow``; it registers no freed actuator.
    The genuine actuator is the unfixed hydraulic Var the closure determines (the
    recycle stream flow the ratio controls) — never the pinned ratio var itself.
    We read the unfixed, non-ratio variables out of the closure body so the
    structural check validates that variable instead of letting the target var
    double as its own actuator. Returns None when the closure determines no genuine
    unfixed hydraulic variable (the controlled flows are already fixed) — a vacuous,
    structurally singular pin the caller must reject.
    """
    from pyomo.core.expr.visitor import identify_variables
    ratio_id = id(ratio_var) if ratio_var is not None else None
    for con in target_constraints:
        try:
            free_vars = list(identify_variables(con.body, include_fixed=False))
        except Exception:
            free_vars = []
        for var in free_vars:
            if ratio_id is not None and id(var) == ratio_id:
                continue
            if getattr(var, 'fixed', False):
                continue
            return var
    return None

def _resolve_unique_role_id(resolver: Any, role: Any, units: Mapping[str, Any], explicit_id: Optional[str], *, what: str, disambiguate_hint: str, not_found_code: str) -> str:
    """Resolve a role to EXACTLY ONE unit id (Finding 4 — never first-wins).

    An explicit caller-provided id wins (validated against the live flowsheet).
    Otherwise the role must be filled by exactly one unit: zero -> ``not_found_code``;
    more than one -> ``ACTUATOR_AMBIGUOUS`` listing the candidates, so an N-stage
    train (e.g. repeated ``aerobic``) never silently targets the first stage.
    """
    from watertap_engine_mcp.utils.solve_for_catalog import ERR_ACTUATOR_AMBIGUOUS, SolveForError
    if explicit_id:
        if explicit_id not in units:
            raise SolveForError(not_found_code, f"{what} unit '{explicit_id}' is not in this session")
        return str(explicit_id)
    ids = list(resolver.all_for(role, units))
    if not ids:
        raise SolveForError(not_found_code, f"no '{_role_label(role)}' role unit found for {what}")
    if len(ids) > 1:
        raise SolveForError(ERR_ACTUATOR_AMBIGUOUS, f"the '{_role_label(role)}' role is filled by {ids}; {disambiguate_hint}", details={'role': _role_label(role), 'candidates': ids, 'what': what})
    return str(ids[0])

def _role_label(role: Any) -> str:
    """Return a stable string label for a role value or enum."""
    return str(getattr(role, 'value', role))

def _solve_for_target_residual(model: Any, new_constraints: List[Any]) -> Optional[float]:
    """Return the residual (value(body) - bound) of the target constraint, when
    computable.

    Evaluated at the model's current variable values, so it is the build-state
    (pre-solve) residual when called before a solve, and the achieved-vs-target
    residual when called on the solved model (~0 at a strict optimum)."""
    from pyomo.environ import value as _pyovalue
    for con in new_constraints:
        try:
            body = _pyovalue(con.body, exception=False)
            if body is None:
                continue
            bound = con.upper if con.upper is not None else con.lower
            bound_v = _pyovalue(bound, exception=False) if bound is not None else None
            if bound_v is None:
                continue
            residual = float(body) - float(bound_v)
            if math.isfinite(residual):
                return residual
        except Exception:
            continue
    return None

def _solve_for_target_met(*, constraint_kind: str, residual: Optional[float], solution_quality: Optional[str], tolerance: float=1e-06) -> Optional[bool]:
    """Classify the post-solve target without conflating it with convergence.

    ``residual`` follows :func:`_solve_for_target_residual`: body minus the
    selected bound. Exact constraints require a near-zero residual, minimum
    constraints require a nonnegative residual, and maximum constraints require
    a nonpositive residual. An absent/non-strict solution returns ``None`` or
    ``False`` rather than manufacturing target success.
    """
    if solution_quality is None or residual is None:
        return None
    if solution_quality != 'strict_optimal' or not math.isfinite(float(residual)):
        return False
    residual = float(residual)
    if constraint_kind == 'exact':
        return abs(residual) <= tolerance
    if constraint_kind == 'min':
        return residual >= -tolerance
    if constraint_kind == 'max':
        return residual <= tolerance
    return False

def _solve_for_actuator_at_bound(actuator_var: Any, *, rel_tol: float=1e-06, abs_tol: float=1e-09) -> Optional[str]:
    """Return ``"lower"`` / ``"upper"`` if the solved actuator value sits on a
    finite bound, else None.

    A freed actuator pinned against a bound after the solve is the structural
    signature of an unreachable target: the solver wanted to push past the bound
    to meet the spec but could not."""
    from pyomo.environ import value as _pyovalue
    try:
        val = _pyovalue(actuator_var, exception=False)
    except Exception:
        return None
    if val is None or not math.isfinite(float(val)):
        return None
    val = float(val)
    lb = _num_or_none(getattr(actuator_var, 'lb', None))
    ub = _num_or_none(getattr(actuator_var, 'ub', None))

    def _at(bound: Optional[float]) -> bool:
        if bound is None or not math.isfinite(bound):
            return False
        return abs(val - bound) <= max(abs_tol, rel_tol * max(abs(bound), 1.0))
    if _at(lb):
        return 'lower'
    if _at(ub):
        return 'upper'
    return None

def _solve_for_session_results(result: Any, pipeline: Any) -> Dict[str, Any]:
    """Shape an inline-solve ``HygienePipeline`` result into the session-results
    dict the worker persists.

    This is the SAME ``{state, details, history}`` layout ``build_and_solve``'s
    worker writes, so ``_solution_quality_for_session`` reads the strict /
    near-feasible verdict off a ``solve_for`` inline solve identically to a
    ``solve_flowsheet`` background solve."""
    return {'state': result.state.value, 'details': result.details, 'history': [{'state': h.state.value, 'success': h.success, 'message': h.message, 'details': h.details, 'warnings': h.warnings, 'errors': h.errors} for h in getattr(pipeline, 'history', []) or []], 'recovered': bool(isinstance(result.details, dict) and result.details.get('recovered', False))}

def _resolve_actuator_locator(free: Dict[str, Any], resolver: Any, units: Dict[str, Any]) -> tuple:
    """Resolve a ``free`` selector to (unit_id, var_path) for the generic raw-var
    swap. Raises SolveForError on failure."""
    from watertap_engine_mcp.utils.solve_for_catalog import ERR_ACTUATOR_AMBIGUOUS, ERR_ACTUATOR_NOT_FOUND, ERR_INVALID_REQUEST, NAMED_ACTUATORS, SolveForError
    selector = str(free.get('selector') or '').lower()
    if selector == 'unit_var':
        unit_id = free.get('unit_id') or free.get('unit')
        var_path = free.get('var_path') or free.get('var')
        if not unit_id or not var_path:
            raise SolveForError(ERR_ACTUATOR_NOT_FOUND, "free.selector='unit_var' requires unit_id + var_path")
        if unit_id not in units:
            raise SolveForError(ERR_ACTUATOR_NOT_FOUND, f"actuator unit '{unit_id}' not in session")
        return (str(unit_id), str(var_path))
    if selector == 'named_actuator':
        name = free.get('named_actuator')
        spec = NAMED_ACTUATORS.get(str(name))
        if spec is None:
            raise SolveForError(ERR_ACTUATOR_NOT_FOUND, f"unknown named_actuator '{name}'. Known: {sorted(NAMED_ACTUATORS)}")
        unit_id = _resolve_unique_role_id(resolver, spec['role'], units, free.get('unit_id'), what=f"named_actuator '{name}'", disambiguate_hint='pass free.unit_id to choose the actuator unit', not_found_code=ERR_ACTUATOR_NOT_FOUND)
        return (str(unit_id), str(spec['var_hint']))
    if selector == 'role':
        role = free.get('role')
        var_path = free.get('var_path') or free.get('var')
        ids = resolver.all_for(role, units) if role else ()
        if not ids:
            raise SolveForError(ERR_ACTUATOR_NOT_FOUND, f"no '{role}' role unit in session")
        if len(ids) > 1 and (not free.get('unit_id')):
            raise SolveForError(ERR_ACTUATOR_AMBIGUOUS, f"role '{role}' is filled by {list(ids)}; pass free.unit_id to disambiguate")
        unit_id = free.get('unit_id') or ids[0]
        if not var_path:
            raise SolveForError(ERR_ACTUATOR_NOT_FOUND, "free.selector='role' requires var_path (the actuator variable on the unit)")
        return (str(unit_id), str(var_path))
    raise SolveForError(ERR_INVALID_REQUEST, "free.selector must be one of 'role', 'named_actuator', 'unit_var'")

def _resolve_solve_for_spec(session: FlowsheetSession, preset: Optional[str], free: Optional[Dict[str, Any]], target: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Resolve a preset/free/target request into a session process-constraint
    dict + resolution metadata, via the catalog + role resolver.

    Returns a dict with keys: process_constraint, target_quantity, constraint_type,
    constraint_kind, optimization_mode, value, actuator_descriptor, target_descriptor.
    Raises SolveForError (with a stable code) on any resolution failure.
    """
    from watertap_engine_mcp.utils.solve_for_catalog import ERR_ACTUATOR_NOT_FOUND, ERR_INEQUALITY_REQUIRES_OPTIMIZATION_MODE, ERR_INVALID_REQUEST, ERR_PRESET_UNAVAILABLE, ERR_TARGET_NOT_COMPUTABLE, INEQUALITY_KINDS, NAMED_ACTUATORS, PRESETS, TARGET_QUANTITIES, VALID_KINDS, SolveForError, available_presets
    free = dict(free or {})
    target = dict(target or {})
    preset_row: Optional[Dict[str, Any]] = None
    if preset is not None:
        preset_row = PRESETS.get(preset)
        if preset_row is None:
            raise SolveForError(ERR_INVALID_REQUEST, f"Unknown preset '{preset}'. Available: {sorted(available_presets())}")
        if not preset_row.get('available'):
            raise SolveForError(ERR_PRESET_UNAVAILABLE, preset_row.get('remediation') or f"Preset '{preset}' is registered but not available yet.", details={'preset': preset, 'remediation': preset_row.get('remediation')})
    tq_id = target.get('quantity') or (preset_row and preset_row.get('target_quantity'))
    if not tq_id:
        raise SolveForError(ERR_INVALID_REQUEST, 'solve_for requires a preset or an explicit target.quantity')
    tq = TARGET_QUANTITIES.get(tq_id)
    if tq is None:
        raise SolveForError(ERR_INVALID_REQUEST, f"Unknown target quantity '{tq_id}'. Available: {sorted(TARGET_QUANTITIES)}")
    raw_value = target.get('value')
    if raw_value is None:
        raise SolveForError(ERR_INVALID_REQUEST, f"solve_for requires target.value (a {tq['value_units']} for '{tq_id}')")
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        raise SolveForError(ERR_INVALID_REQUEST, f'target.value must be numeric; got {raw_value!r}')
    kind = str(target.get('constraint_kind') or (preset_row and preset_row.get('default_constraint_kind')) or tq['default_constraint_kind']).lower()
    if kind not in VALID_KINDS:
        raise SolveForError(ERR_INVALID_REQUEST, f'constraint_kind must be one of {sorted(VALID_KINDS)}; got {kind!r}')
    if kind not in tq['supported_kinds']:
        raise SolveForError(ERR_INVALID_REQUEST, f"target quantity '{tq_id}' supports kinds {tq['supported_kinds']}, not '{kind}'")
    optimization_mode = bool(target.get('optimization_mode') or free.get('optimization_mode'))
    if kind in INEQUALITY_KINDS and (not optimization_mode):
        raise SolveForError(ERR_INEQUALITY_REQUIRES_OPTIMIZATION_MODE, f"constraint_kind '{kind}' adds an inequality and is NOT DOF-neutral; pass optimization_mode=true so the solver minimizes the freed actuator subject to the inequality.", details={'constraint_kind': kind, 'target_quantity': tq_id})
    resolver = RoleResolver.from_session(session)
    units = session.units
    constraint_type = tq['constraint_type']
    label = str(target.get('name') or preset or tq_id)
    pc: Dict[str, Any] = {'type': constraint_type, 'name': f'solve_for_{label}'}
    seed = free.get('seed')
    if seed is None and preset_row is not None:
        seed = preset_row.get('default_seed')
    actuator_descriptor: Dict[str, Any] = {}
    target_descriptor: Dict[str, Any] = {'quantity': tq_id}
    if constraint_type == 'srt':
        reactor_ids: List[str] = []
        for role in tq['reactor_roles']:
            reactor_ids.extend(resolver.all_for(role, units))
        if not reactor_ids:
            raise SolveForError(ERR_TARGET_NOT_COMPUTABLE, f'no reactor-role units ({tq['reactor_roles']}) to form the SRT inventory')
        waste_id = _resolve_unique_role_id(resolver, tq['waste_role'], units, free.get('waste_unit'), what='the WAS (sludge-splitter) actuator', disambiguate_hint='pass free.waste_unit to choose the sludge splitter', not_found_code=ERR_ACTUATOR_NOT_FOUND)
        pc.update(target_days=value, reactor_units=reactor_ids, waste_unit=waste_id, waste_port=free.get('waste_port', tq['waste_port']), biomass_components=list(target.get('biomass_components') or tq['biomass_components']))
        actuator_descriptor = {'selector': 'named_actuator', 'named_actuator': 'was', 'role': tq['waste_role'], 'unit_id': waste_id, 'var_hint': NAMED_ACTUATORS['was']['var_hint']}
        target_descriptor.update(reactor_units=reactor_ids, waste_unit=waste_id)
    elif constraint_type == 'do_target':
        aer_id = _resolve_unique_role_id(resolver, tq['actuator_role'], units, free.get('actuator_unit') or target.get('unit'), what='the aeration KLa actuator', disambiguate_hint='pass free.actuator_unit (or target.unit) to choose the aerobic stage', not_found_code=ERR_ACTUATOR_NOT_FOUND)
        tgt_id = target.get('unit') or aer_id
        pc.update(target_unit=tgt_id, target_port=tq['target_port'], target_mg_l=value, component=tq['component'], actuator_unit=aer_id, kla_lower=tq['kla_lower'], kla_upper=tq['kla_upper'])
        if seed is not None:
            pc['kla_seed'] = seed
        actuator_descriptor = {'selector': 'named_actuator', 'named_actuator': 'kla', 'role': tq['actuator_role'], 'unit_id': aer_id, 'var_hint': NAMED_ACTUATORS['kla']['var_hint']}
        target_descriptor.update(unit_id=tgt_id, port=tq['target_port'], component=tq['component'])
    elif constraint_type == 'alkalinity_target':
        tgt_id = _resolve_unique_role_id(resolver, tq['target_role'], units, target.get('unit'), what='the alkalinity target port', disambiguate_hint='pass target.unit to choose the effluent target unit', not_found_code=ERR_TARGET_NOT_COMPUTABLE)
        dose_id = _resolve_unique_role_id(resolver, tq['dose_role'], units, free.get('dose_unit'), what='the alkalinity dose actuator', disambiguate_hint='pass free.dose_unit to choose the dose unit', not_found_code=ERR_ACTUATOR_NOT_FOUND)
        pc.update(target_unit=tgt_id, target_port=tq['target_port'], target_mol_per_m3=value, constraint_kind='min' if kind == 'min' else 'exact', dose_lower=tq['dose_lower'], dose_upper=tq['dose_upper'])
        if dose_id:
            pc['dose_unit'] = dose_id
        if seed is not None:
            pc['dose_seed'] = seed
        actuator_descriptor = {'selector': 'named_actuator', 'named_actuator': 'caustic_dose', 'role': tq['dose_role'], 'unit_id': dose_id, 'var_hint': NAMED_ACTUATORS['caustic_dose']['var_hint']}
        target_descriptor.update(unit_id=tgt_id, port=tq['target_port'])
    elif constraint_type == 'chemical_p_target':
        tgt_id = _resolve_unique_role_id(resolver, tq['target_role'], units, target.get('unit'), what='the effluent-TP target port', disambiguate_hint='pass target.unit to choose the effluent target unit', not_found_code=ERR_TARGET_NOT_COMPUTABLE)
        dose_id = _resolve_unique_role_id(resolver, tq['dose_role'], units, free.get('dose_unit'), what='the chemical-P dose actuator', disambiguate_hint='pass free.dose_unit to choose the dose unit', not_found_code=ERR_ACTUATOR_NOT_FOUND)
        pc.update(target_unit=tgt_id, target_port=tq['target_port'], target_mg_l=value, p_basis=tq['p_basis'], dose_lower=tq['dose_lower'], dose_upper=tq['dose_upper'])
        if dose_id:
            pc['dose_unit'] = dose_id
        if seed is not None:
            pc['dose_seed'] = seed
        actuator_descriptor = {'selector': 'named_actuator', 'named_actuator': 'fecl3_dose', 'role': tq['dose_role'], 'unit_id': dose_id, 'var_hint': NAMED_ACTUATORS['fecl3_dose']['var_hint']}
        target_descriptor.update(unit_id=tgt_id, port=tq['target_port'], p_basis=tq['p_basis'])
    elif constraint_type == 'no3_target':
        tgt_id = _resolve_unique_role_id(resolver, tq['target_role'], units, target.get('unit'), what='the effluent-NO3 target port', disambiguate_hint='pass target.unit to choose the effluent target unit', not_found_code=ERR_TARGET_NOT_COMPUTABLE)
        dose_id = _resolve_unique_role_id(resolver, tq['dose_role'], units, free.get('dose_unit'), what='the supplemental-carbon dose actuator', disambiguate_hint='pass free.dose_unit to choose the dose unit', not_found_code=ERR_ACTUATOR_NOT_FOUND)
        pc.update(target_unit=tgt_id, target_port=tq['target_port'], target_mg_l=value, component=tq['component'], dose_lower=tq['dose_lower'], dose_upper=tq['dose_upper'])
        if dose_id:
            pc['dose_unit'] = dose_id
        if seed is not None:
            pc['dose_seed'] = seed
        actuator_descriptor = {'selector': 'named_actuator', 'named_actuator': 'carbon_dose', 'role': tq['dose_role'], 'unit_id': dose_id, 'var_hint': NAMED_ACTUATORS['carbon_dose']['var_hint']}
        target_descriptor.update(unit_id=tgt_id, port=tq['target_port'], component=tq['component'])
    elif constraint_type == 'recycle_ratio':
        num_id = _resolve_unique_role_id(resolver, tq['numerator_role'], units, free.get('numerator_unit'), what='the recycle-ratio numerator flow', disambiguate_hint='pass free.numerator_unit to choose the recycle stream', not_found_code=ERR_ACTUATOR_NOT_FOUND)
        den_id = _resolve_unique_role_id(resolver, tq['denominator_role'], units, free.get('denominator_unit'), what='the recycle-ratio denominator flow', disambiguate_hint='pass free.denominator_unit to choose the reference stream', not_found_code=ERR_TARGET_NOT_COMPUTABLE)
        ratio_name = str(target.get('ratio_name') or tq['ratio_name'])
        pc.update(name=ratio_name, ratio_name=ratio_name, target_ratio=value, numerator_unit=num_id, numerator_port=free.get('numerator_port', tq['numerator_port']), denominator_unit=den_id, denominator_port=free.get('denominator_port', tq['denominator_port']))
        actuator_descriptor = {'selector': 'named_actuator', 'named_actuator': 'ras_ratio', 'role': tq['numerator_role'], 'unit_id': num_id, 'var_hint': f'process_var_{ratio_name}', 'note': 'the recycle-ratio variable is fixed to the target; the dependent split is determined by the closure equation'}
        target_descriptor.update(numerator_unit=num_id, denominator_unit=den_id, ratio_name=ratio_name)
    elif constraint_type == 'raw_var_target':
        actuator_unit, actuator_path = _resolve_actuator_locator(free, resolver, units)
        t_unit = target.get('unit') or target.get('unit_id')
        t_path = target.get('var_path') or target.get('var')
        if not t_unit or not t_path:
            raise SolveForError(ERR_TARGET_NOT_COMPUTABLE, 'raw_var target requires target.unit_id + target.var_path')
        if t_unit not in units:
            raise SolveForError(ERR_TARGET_NOT_COMPUTABLE, f"target unit '{t_unit}' not in session")
        pc.update(actuator_unit=actuator_unit, actuator_var_path=actuator_path, target_unit=str(t_unit), target_var_path=str(t_path), target_value=value, constraint_kind=kind)
        for key_src, key_dst in (('lower', 'actuator_lower'), ('upper', 'actuator_upper'), ('seed', 'actuator_seed')):
            if free.get(key_src) is not None:
                pc[key_dst] = float(free[key_src])
        actuator_descriptor = {'selector': free.get('selector'), 'unit_id': actuator_unit, 'var_hint': actuator_path}
        target_descriptor.update(unit_id=str(t_unit), var_path=str(t_path))
    else:
        raise SolveForError(ERR_INVALID_REQUEST, f"unsupported constraint_type '{constraint_type}'")
    return {'process_constraint': pc, 'target_quantity': tq_id, 'constraint_type': constraint_type, 'constraint_kind': kind, 'optimization_mode': optimization_mode, 'value': value, 'actuator_descriptor': actuator_descriptor, 'target_descriptor': target_descriptor}

def _build_in_memory(session: FlowsheetSession):
    """Build a Pyomo model from an in-memory session, returning (builder, model)."""
    from watertap_engine_mcp.utils.model_builder import ModelBuilder
    builder = ModelBuilder(session)
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        model = builder.build()
    return (builder, model)

def _map_solve_for_build_error(exc: Exception) -> str:
    """Map a model-build failure to a structured solve_for reject code.

    Prefers the structured pre-build compatibility gate
    (:class:`TargetCompatibilityError`): a missing unit/actuator is an
    ``ACTUATOR_NOT_FOUND``, a missing property-package component is a
    ``TARGET_NOT_COMPUTABLE``. Falls back to message classification for ordinary
    ``ModelBuildError`` messages.
    """
    from watertap_engine_mcp.utils.model_builder import TargetCompatibilityError
    from watertap_engine_mcp.utils.solve_for_catalog import ERR_ACTUATOR_NOT_FOUND, ERR_SOLVE_FOR_BUILD_ERROR, ERR_TARGET_NOT_COMPUTABLE
    if isinstance(exc, TargetCompatibilityError):
        if exc.missing_units:
            return ERR_ACTUATOR_NOT_FOUND
        if exc.missing_components:
            return ERR_TARGET_NOT_COMPUTABLE
        return ERR_SOLVE_FOR_BUILD_ERROR
    return _map_build_error_code(str(exc))

def _map_build_error_code(message: str) -> str:
    """Map a ModelBuildError message to a structured solve_for reject code."""
    from watertap_engine_mcp.utils.solve_for_catalog import ERR_ACTUATOR_AMBIGUOUS, ERR_ACTUATOR_NOT_FOUND, ERR_SOLVE_FOR_BUILD_ERROR, ERR_TARGET_NOT_COMPUTABLE
    low = message.lower()
    if 'actuator conflict' in low:
        return ERR_ACTUATOR_AMBIGUOUS
    if 'actuator not found' in low or 'found no unit carrying' in low or 'has no kla' in low or ('actuator unit not found' in low) or ('without dose_unit' in low) or ('actuator' in low and 'not found' in low):
        return ERR_ACTUATOR_NOT_FOUND
    if 'no conc_mass_comp' in low or 'no alkalinity' in low or 'has no outlet' in low or ('target not found' in low) or ('not found' in low):
        return ERR_TARGET_NOT_COMPUTABLE
    return ERR_SOLVE_FOR_BUILD_ERROR

def solve_for(session_id: str, preset: Optional[str]=None, free: Optional[Dict[str, Any]]=None, target: Optional[Dict[str, Any]]=None, solve: bool=True, write_mode: str='branch', new_session_id: Optional[str]=None, solver_options: Optional[Dict[str, Any]]=None, dry_run: bool=False) -> Dict[str, Any]:
    """Solve for an actuator X given a target Y: a DOF-safe design-spec swap.

    The general "solve for X given Y" (inverse / design-spec) capability over the
    engine's existing target-driven controllers (SRT<-WAS, DO<-KLa, residual
    alkalinity<-caustic dose, effluent TP<-metal dose, flow<-recycle ratio) plus a
    generic raw-variable target. Resolves the actuator + target by structural ROLE
    so it works on any role-tagged topology, asserts the swap is DOF-safe (exact =>
    square DOF==0; min/max require an explicit optimization_mode + objective),
    validates the actuator and target are structurally connected, then submits
    through the existing build_and_solve pipeline.

    Args:
        session_id: Source flowsheet session.
        preset: Optional catalog preset filling free+target defaults
            (srt_to_was, do_to_kla, alkalinity_to_caustic, tp_to_fecl3,
            target_to_recycle_ratio). See list_solve_for_capabilities.
        free: Actuator specification (generic raw-var path). Keys:
            selector ('role'|'named_actuator'|'unit_var'), plus role/var_path,
            named_actuator, or unit_id/var_path; optional lower/upper/seed.
        target: Target specification. Keys: quantity, value, constraint_kind
            ('exact'|'min'|'max'), optimization_mode, and for raw_var unit_id+var_path.
        solve: Submit the solve after preparing (default True).
        write_mode: 'branch' (clone first; default) or 'in_place'.
        new_session_id: Explicit id for the branched session.
        solver_options: Optional solver options forwarded to build_and_solve.
        dry_run: Prepare + validate (DOF/incidence) but do NOT solve.

    Returns:
        SolveForResponse dict, or a structured api.errors envelope on rejection.
    """
    from idaes.core.util.model_statistics import degrees_of_freedom
    from pyomo.environ import Constraint, Objective
    from watertap_engine_mcp.api.solve_for_schemas import SolveForResponse
    from watertap_engine_mcp.utils.model_builder import ModelBuildError
    from watertap_engine_mcp.utils.solve_for_catalog import ERR_ACTUATOR_NOT_FOUND, ERR_DOF_NOT_NEUTRAL, ERR_INEQUALITY_REQUIRES_OPTIMIZATION_MODE, ERR_INVALID_REQUEST, ERR_STRUCTURALLY_DISCONNECTED, ERR_STRUCTURALLY_SINGULAR, INEQUALITY_KINDS, PRESETS, SolveForError
    validation_error = _finite_validation_error('solve_for', {'session_id': session_id, 'solver_options': solver_options})
    if validation_error is not None:
        return validation_error
    if write_mode not in ('branch', 'in_place'):
        return _solve_for_error(SolveForError(ERR_INVALID_REQUEST, "write_mode must be 'branch' or 'in_place'"))
    try:
        source = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    try:
        resolution = _resolve_solve_for_spec(source, preset, free, target)
    except SolveForError as err:
        return _solve_for_error(err)
    process_constraint = resolution['process_constraint']
    kind = resolution['constraint_kind']
    if write_mode == 'branch':
        if new_session_id and session_manager.exists(new_session_id):
            return _solve_for_error(SolveForError(ERR_INVALID_REQUEST, f"Session '{new_session_id}' already exists"))
        prepared = _clone_session_object(source, new_session_id=new_session_id, name=f'{source.config.name or session_id}-solve_for', include_results=False)
    else:
        prepared = source
    prepared_session_id = prepared.config.session_id
    try:
        base_builder, base_model = _build_in_memory(prepared)
    except ModelBuildError as exc:
        return _solve_for_error(SolveForError(_map_solve_for_build_error(exc), f'base model build failed: {exc}'))
    except Exception as exc:
        return _solve_for_error(SolveForError(ERR_INVALID_REQUEST, f'base model build failed: {exc}'))
    dof_before = int(degrees_of_freedom(getattr(base_model, 'fs', base_model)))
    base_freed = set(_solve_for_freed_actuators(base_builder))
    base_con_names = {c.name for c in base_model.component_data_objects(Constraint, active=True, descend_into=True)}
    prepared.add_process_constraint(process_constraint)
    try:
        after_builder, after_model = _build_in_memory(prepared)
    except ModelBuildError as exc:
        return _solve_for_error(SolveForError(_map_solve_for_build_error(exc), f'prepared model build failed: {exc}'))
    except Exception as exc:
        return _solve_for_error(SolveForError(ERR_INVALID_REQUEST, f'prepared model build failed: {exc}'))
    after_fs = getattr(after_model, 'fs', after_model)
    dof_after = int(degrees_of_freedom(after_fs))
    after_freed = _solve_for_freed_actuators(after_builder)
    our_freed = {n: v for n, v in after_freed.items() if n not in base_freed}
    new_constraints = [c for c in after_model.component_data_objects(Constraint, active=True, descend_into=True) if c.name not in base_con_names]
    actuator_var = None
    actuator_resolved_to = dict(resolution['actuator_descriptor'])
    if our_freed:
        name, actuator_var = next(iter(our_freed.items()))
        actuator_resolved_to.update(variable=name, fixed=bool(getattr(actuator_var, 'fixed', False)), value=_safe_var_value(actuator_var), lower=_num_or_none(getattr(actuator_var, 'lb', None)), upper=_num_or_none(getattr(actuator_var, 'ub', None)))
    elif resolution['constraint_type'] == 'recycle_ratio':
        ratio_var = getattr(after_fs, actuator_resolved_to.get('var_hint', ''), None)
        hydraulic_var = _solve_for_recycle_hydraulic_actuator(new_constraints, ratio_var)
        if hydraulic_var is None:
            return _solve_for_error(SolveForError(ERR_STRUCTURALLY_SINGULAR, 'the recycle-ratio closure determines no genuine unfixed hydraulic variable (the controlled flows are already fixed) — pinning the ratio would be vacuous / structurally singular.', details={'ratio_variable': getattr(ratio_var, 'name', None), 'target_constraints': [c.name for c in new_constraints]}))
        actuator_var = hydraulic_var
        actuator_resolved_to.update(variable=getattr(hydraulic_var, 'name', actuator_resolved_to.get('var_hint')), fixed=bool(getattr(hydraulic_var, 'fixed', False)), value=_safe_var_value(hydraulic_var), lower=_num_or_none(getattr(hydraulic_var, 'lb', None)), upper=_num_or_none(getattr(hydraulic_var, 'ub', None)), ratio_variable=getattr(ratio_var, 'name', None), note='the pinned ratio var is excluded; the validated actuator is the unfixed recycle flow the closure determines')
    if actuator_var is None:
        return _solve_for_error(SolveForError(ERR_ACTUATOR_NOT_FOUND, 'no unfixed actuator variable was freed by this swap; the target constraint would consume an unrelated degree of freedom. Check that the actuator role/var exists and is freeable for this topology.', details={'constraint_type': resolution['constraint_type'], 'target_constraints': [c.name for c in new_constraints]}))
    target_resolved_to = dict(resolution['target_descriptor'])
    target_resolved_to['constraints'] = [c.name for c in new_constraints]
    if kind not in INEQUALITY_KINDS:
        if dof_after != 0:
            return _solve_for_error(SolveForError(ERR_DOF_NOT_NEUTRAL, f'exact design spec must leave the model square (DOF==0); got DOF={dof_after} (was {dof_before} before the swap). ' + ('The base model is not square — specify the remaining DOF (feed state, volumes, ...) before solving for this target.' if dof_before != 0 else 'The swap was not DOF-neutral for this topology.'), details={'dof_before': dof_before, 'dof_after': dof_after, 'constraint_kind': kind}))
    else:
        has_objective = any((True for _ in after_model.component_data_objects(Objective, active=True, descend_into=True)))
        if not has_objective:
            return _solve_for_error(SolveForError(ERR_INEQUALITY_REQUIRES_OPTIMIZATION_MODE, f"constraint_kind '{kind}' needs an optimization objective; none was constructed for this swap.", details={'dof_before': dof_before, 'dof_after': dof_after}))
        if dof_after < 0:
            return _solve_for_error(SolveForError(ERR_DOF_NOT_NEUTRAL, f'min/max design spec is overspecified (DOF={dof_after} < 0).', details={'dof_before': dof_before, 'dof_after': dof_after}))
    if new_constraints:
        require_matching = kind not in INEQUALITY_KINDS
        struct_status, struct_details = _solve_for_structural_validation(after_model, actuator_var, new_constraints, require_perfect_matching=require_matching, include_inequality=not require_matching)
        if struct_status == 'disconnected':
            return _solve_for_error(SolveForError(ERR_STRUCTURALLY_DISCONNECTED, 'the resolved actuator and the target constraint are in disjoint incidence components — the actuator cannot influence the target. Pick an actuator upstream of (or coupled to) the target metric.', details={'actuator': actuator_resolved_to.get('variable'), 'target_constraints': [c.name for c in new_constraints]}))
        if struct_status in ('singular', 'error'):
            message = 'the prepared swap is structurally singular — the square active-equality subsystem has no perfect matching (an unmatched variable / constraint), so the inverse solve is ill-posed.' if struct_status == 'singular' else 'the structural well-posedness of the prepared swap could not be verified (incidence analysis raised); failing closed rather than accepting an unverifiable inverse solve.'
            return _solve_for_error(SolveForError(ERR_STRUCTURALLY_SINGULAR, message, details={'actuator': actuator_resolved_to.get('variable'), 'target_constraints': [c.name for c in new_constraints], **struct_details}))
    target_residual = _solve_for_target_residual(after_model, new_constraints)
    applied_design_spec = {'preset': preset, 'target_quantity': resolution['target_quantity'], 'constraint_type': resolution['constraint_type'], 'constraint_kind': kind, 'optimization_mode': resolution['optimization_mode'], 'value': resolution['value'], 'process_constraint': process_constraint}
    prepared.config.runtime_metadata['solve_for_applied'] = applied_design_spec
    session_manager.save(prepared)
    job_id: Optional[str] = None
    warnings: List[str] = []
    solution_quality: Optional[str] = None
    residual_basis: Optional[Dict[str, Any]] = None
    target_residual_post_solve: Optional[float] = None
    actuator_at_bound: Optional[str] = None
    solve_error_code: Optional[str] = None
    if dry_run or not solve:
        status = 'dry_run' if dry_run else 'prepared'
        message = f"Prepared + validated the design spec; not solved ({('dry_run' if dry_run else 'solve=False')}). Submit build_and_solve(session_id='{prepared_session_id}') to solve."
    else:
        from watertap_engine_mcp.solver.pipeline import HygienePipeline, PipelineConfig
        runtime_solver_options = prepared.config.runtime_metadata.get('solver_options')
        effective_solver_options: Dict[str, Any] = dict(runtime_solver_options) if isinstance(runtime_solver_options, dict) else {}
        if isinstance(solver_options, dict):
            effective_solver_options.update(solver_options)
        try:
            pipeline = HygienePipeline(after_model, PipelineConfig(solver_options=effective_solver_options, max_wall_time_s=300, allow_near_feasible_asm2d=False), units=after_builder.get_units(), session=prepared, builder=after_builder)
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                result = pipeline.run_all()
        except Exception as exc:
            warnings.append(f'design spec validated but inline solve raised: {exc}')
            status = 'infeasible'
            solution_quality = 'infeasible_structured'
            message = 'Design spec prepared + validated; inline solve raised (see warnings).'
        else:
            target_residual_post_solve = _solve_for_target_residual(after_model, new_constraints)
            actuator_at_bound = _solve_for_actuator_at_bound(actuator_var)
            if result.success:
                prepared.set_solved(_solve_for_session_results(result, pipeline))
                session_manager.save(prepared)
                solution_quality, residual_basis = _solution_quality_for_session(prepared)
                status = 'solved'
                message = f'Design spec applied and solved inline. solution_quality={solution_quality}.'
            else:
                prepared.set_failed('solve_for inline design-spec solve did not converge')
                session_manager.save(prepared)
                solution_quality = 'infeasible_structured'
                status = 'infeasible'
                if actuator_at_bound is not None:
                    solve_error_code = 'TARGET_AT_BOUND_UNREACHABLE'
                    message = f'Design spec is unreachable: the freed actuator ({actuator_resolved_to.get('variable')}) hit its {actuator_at_bound} bound and the target was not met.'
                else:
                    message = 'Design spec prepared + validated but the inline solve did not converge to a feasible solution.'
    target_met = _solve_for_target_met(constraint_kind=kind, residual=target_residual_post_solve, solution_quality=solution_quality)
    certification = _flowsheet_certification(prepared)
    recommendation_blockers: List[str] = []
    recommendation_blockers.extend((f'recommendation_blocking_unit:{unit_id}' for unit_id in certification.get('recommendation_blocking_units', [])))
    preset_row = PRESETS.get(preset or '')
    if preset_row is None:
        recommendation_blockers.append('uncatalogued_design_spec_safety_classification')
    else:
        recommendation_blockers.extend(preset_row.get('recommendation_blockers', []))
        if not bool(preset_row.get('recommendation_eligible', True)):
            recommendation_blockers.append('preset_not_recommendation_eligible')
    if status != 'solved' or solution_quality != 'strict_optimal':
        recommendation_blockers.append('strict_forward_solution_not_available')
    if target_met is not True:
        recommendation_blockers.append('target_not_verified_met')
    recommendation_eligible = bool(not recommendation_blockers and certification.get('recommendation_eligible', False))
    response = SolveForResponse(ok=True, prepared_session_id=prepared_session_id, job_id=job_id, status=status, write_mode=write_mode, solve=bool(solve), dry_run=bool(dry_run), applied_design_spec=applied_design_spec, dof_before=dof_before, dof_after=dof_after, actuator_resolved_to=actuator_resolved_to, target_resolved_to=target_resolved_to, target_residual=target_residual, solution_quality=solution_quality, residual_basis=residual_basis, target_residual_post_solve=target_residual_post_solve, actuator_at_bound=actuator_at_bound, solve_error_code=solve_error_code, target_met=target_met, recommendation_eligible=recommendation_eligible, recommendation_blockers=sorted(set(recommendation_blockers)), certification=certification, message=message, warnings=warnings)
    return response.model_dump(mode='json')

def list_solve_for_capabilities(session_id: str, include_raw_variable_targets: bool=False) -> Dict[str, Any]:
    """Discover the solve_for design-spec capabilities for THIS model.

    Builds the current model, resolves role-tagged units, and returns: available
    presets with ready-to-send solve_for request templates; live actuator
    candidates (unit, role, var_path, fixed state, value, bounds, units); target
    quantities computable for this model + valid locations; unavailable
    capabilities WITH remediation; and report-only quantities (e.g. MLSS).

    Args:
        session_id: Session to analyze.
        include_raw_variable_targets: Also enumerate generic raw-variable targets
            (the actuator candidates double as raw-var locators).

    Returns:
        SolveForCapabilities dict.
    """
    from watertap_engine_mcp.api.solve_for_schemas import ActuatorCandidate, PresetCapability, SolveForCapabilities, TargetCandidate
    from watertap_engine_mcp.utils.solve_for_catalog import NAMED_ACTUATORS, PRESETS, REPORT_ONLY_QUANTITIES, TARGET_QUANTITIES, unavailable_presets
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    try:
        _, _, units = build_model_context(session)
    except Exception as exc:
        return redact_public_payload({'session_id': session_id, 'status': 'error', 'error': f'Model build failed: {exc}'})
    resolver = RoleResolver.from_session(session)
    unit_map = session.units
    session_certification = _flowsheet_certification(session)

    def _resolve_var_handle(unit_block: Any, var_hint: str) -> Any:
        if unit_block is None:
            return None
        try:
            comp = unit_block.find_component(var_hint)
        except Exception:
            comp = None
        return comp

    def _live_port_component(unit_block: Any, port_name: str, component: str) -> Any:
        """Return the live port concentration Var for ``component``, or None.

        Finding 5: discovery must verify the target expression actually exists on
        the live model, not merely that a role unit is present.
        """
        if unit_block is None:
            return None
        port = getattr(unit_block, port_name, None)
        conc = getattr(port, 'conc_mass_comp', None) if port is not None else None
        if conc is None:
            return None
        for idx in ((0, component), (0.0, component), component):
            try:
                return conc[idx]
            except Exception:
                continue
        return None

    def _live_port_flow(unit_block: Any, port_name: str) -> Any:
        """Return the live port flow_vol Var/handle, or None."""
        if unit_block is None:
            return None
        port = getattr(unit_block, port_name, None)
        flow = getattr(port, 'flow_vol', None) if port is not None else None
        if flow is None:
            return None
        for idx in (0, 0.0):
            try:
                return flow[idx]
            except Exception:
                continue
        return flow
    actuator_candidates: List[ActuatorCandidate] = []
    live_named_actuators: set = set()
    for na_id, na in NAMED_ACTUATORS.items():
        for unit_id in resolver.all_for(na['role'], unit_map):
            unit_block = units.get(unit_id)
            var = _resolve_var_handle(unit_block, na['var_hint'])
            if var is None:
                continue
            live_named_actuators.add(na_id)
            actuator_candidates.append(ActuatorCandidate(unit_id=unit_id, role=na['role'], var_path=na['var_hint'], var_kind=na['var_kind'], fixed=bool(getattr(var, 'fixed', False)), value=_safe_var_value(var), lower=_num_or_none(getattr(var, 'lb', None)), upper=_num_or_none(getattr(var, 'ub', None)), units=na['units'], named_actuator=na_id))
    target_candidates: List[TargetCandidate] = []
    role_target_quantities = {'do_mg_l': ('aerobic', 'outlet', 'S_O2'), 'residual_alkalinity': ('product_effluent', 'inlet', 'S_ALK'), 'effluent_tp_mg_l': ('product_effluent', 'inlet', 'S_PO4'), 'effluent_no3_mg_l': ('product_effluent', 'inlet', 'S_NO3')}
    srt_biomass_probe = ('X_AUT', 'X_H', 'X_PAO')
    computable_quantities: set = set()
    for tq_id, tq in TARGET_QUANTITIES.items():
        if tq_id == 'raw_var':
            continue
        locations: List[Dict[str, Any]] = []
        computable = False
        if tq_id == 'srt_days':
            reactor_ids: List[str] = []
            for role in tq['reactor_roles']:
                reactor_ids.extend(resolver.all_for(role, unit_map))
            waste_ids = list(resolver.all_for(tq['waste_role'], unit_map))
            reactor_live = any((any((_live_port_component(units.get(rid), 'outlet', comp) is not None for comp in srt_biomass_probe)) for rid in reactor_ids))
            live_waste_ids = [waste_id for waste_id in waste_ids if _live_port_flow(units.get(waste_id), str(tq.get('waste_port', 'waste'))) is not None]
            if reactor_ids and len(live_waste_ids) == 1 and reactor_live:
                computable = True
                locations.append({'reactor_units': reactor_ids, 'waste_unit': live_waste_ids[0]})
        elif tq_id == 'flow_ratio':
            num_ids = [unit_id for unit_id in resolver.all_for(tq['numerator_role'], unit_map) if _live_port_flow(units.get(unit_id), str(tq.get('numerator_port', 'outlet'))) is not None]
            den_ids = [unit_id for unit_id in resolver.all_for(tq['denominator_role'], unit_map) if _live_port_flow(units.get(unit_id), str(tq.get('denominator_port', 'outlet'))) is not None]
            if len(num_ids) == 1 and len(den_ids) == 1:
                computable = True
                locations.append({'numerator_unit': num_ids[0], 'denominator_unit': den_ids[0]})
        elif tq_id in role_target_quantities:
            role, port, probe_comp = role_target_quantities[tq_id]
            for tgt_id in resolver.all_for(role, unit_map):
                if _live_port_component(units.get(tgt_id), port, probe_comp) is not None:
                    locations.append({'unit_id': tgt_id, 'port': port})
            computable = bool(locations)
        if computable:
            computable_quantities.add(tq_id)
        target_candidates.append(TargetCandidate(quantity=tq_id, label=tq['label'], constraint_type=tq['constraint_type'], value_units=tq['value_units'], supported_kinds=list(tq['supported_kinds']), locations=locations, computable=computable))
    preset_caps: List[PresetCapability] = []
    for name, row in PRESETS.items():
        if not row.get('available'):
            continue
        tq_id = row['target_quantity']
        tq = TARGET_QUANTITIES.get(tq_id, {})
        target_ok = tq_id in computable_quantities
        actuator_name = row.get('actuator')
        actuator_role = NAMED_ACTUATORS.get(actuator_name, {}).get('role')
        if tq.get('constraint_type') == 'recycle_ratio':
            actuator_ok = target_ok
        else:
            actuator_ok = actuator_name in live_named_actuators
        is_available = target_ok and actuator_ok
        request_template: Optional[Dict[str, Any]] = None
        missing_roles: List[str] = []
        if is_available:
            request_template = {'session_id': session_id, 'preset': name, 'target': {'value': row.get('value_hint')}}
        else:
            if not actuator_ok and actuator_role:
                missing_roles.append(actuator_role)
            if not target_ok:
                for role_key in ('reactor_roles', 'waste_role', 'target_role', 'denominator_role'):
                    val = tq.get(role_key)
                    if isinstance(val, list):
                        missing_roles.extend(val)
                    elif val:
                        missing_roles.append(val)
        recommendation_blockers = list(row.get('recommendation_blockers', []))
        recommendation_blockers.extend((f'recommendation_blocking_unit:{unit_id}' for unit_id in session_certification.get('recommendation_blocking_units', [])))
        if not is_available:
            recommendation_blockers.append('preset_unavailable_for_this_model')
        if not bool(row.get('recommendation_eligible', True)):
            recommendation_blockers.append('preset_not_recommendation_eligible')
        recommendation_eligible = bool(is_available and row.get('recommendation_eligible', True) and session_certification.get('recommendation_eligible', False) and (not recommendation_blockers))
        preset_caps.append(PresetCapability(preset=name, label=row['label'], available=is_available, description=row.get('description', ''), request_template=request_template, remediation=None if is_available else f'this model has no unit(s) for the required role(s): {sorted(set(missing_roles))}', missing_roles=sorted(set(missing_roles)), calculation_class=str(row.get('calculation_class', 'mechanistic')), recommendation_eligible=recommendation_eligible, recommendation_blockers=sorted(set(recommendation_blockers)), operational_use=str(row.get('operational_use', 'diagnostic_or_design_spec'))))
    unavailable_caps: List[Dict[str, Any]] = []
    for name, row in unavailable_presets().items():
        unavailable_caps.append({'preset': name, 'label': row['label'], 'available': False, 'remediation': row.get('remediation'), 'roadmap': 'A7 PR2'})
    report_only: List[Dict[str, Any]] = []
    for q_id, q in REPORT_ONLY_QUANTITIES.items():
        report_only.append({'quantity': q_id, 'label': q['label'], 'controllable': q.get('controllable', False), 'note': q.get('note')})
    raw_variable_targets: Optional[List[Dict[str, Any]]] = None
    if include_raw_variable_targets:
        raw_variable_targets = [{'unit_id': cand.unit_id, 'var_path': cand.var_path, 'role': cand.role, 'selector': 'unit_var', 'note': 'any of these (and any resolvable model Var) can be a raw_var actuator or target'} for cand in actuator_candidates]
    capabilities = SolveForCapabilities(ok=True, session_id=session_id, presets=preset_caps, actuator_candidates=actuator_candidates, target_candidates=target_candidates, report_only_quantities=report_only, unavailable_capabilities=unavailable_caps, raw_variable_targets=raw_variable_targets, certification=session_certification, message=f'{sum((1 for p in preset_caps if p.available))} preset(s) available, {len(actuator_candidates)} actuator candidate(s), {len(computable_quantities)} computable target quantity(ies).')
    return capabilities.model_dump(mode='json')
TWIN_BUNDLE_KIND = 'watertap_flowsheet_twin'
TWIN_BUNDLE_SCHEMA_VERSION = 1

def _content_hash(payload: Any) -> str:
    """Return a stable sha256 of a JSON-serializable payload."""
    import hashlib
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()

def export_twin_bundle(session_id: str) -> Dict[str, Any]:
    """Export the reloadable digital-twin bundle for a flowsheet session.

    The canonical reloadable twin artifact is the WaterTAP **session JSON**
    (topology + engineering inputs + feed states + process constraints +
    runtime metadata) plus the solved-state/KPI export and content hashes — NOT
    ``model_create``/SFILES, which is a topology-only companion view. This
    bundle is self-describing and round-trips through ``import_twin_bundle``.

    Args:
        session_id: Session to export

    Returns:
        A twin bundle: engine + schema version, the full session JSON, the
        solved-state/KPI export (if solved), and content hashes.
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {'error': f"Session '{session_id}' not found"}
    session_dict = session.to_dict()
    solved = session.results if isinstance(session.results, dict) else None
    is_solved = session.solve_status == 'optimal' or bool(isinstance(solved, dict) and solved.get('solved_state'))
    return {'bundle_kind': TWIN_BUNDLE_KIND, 'bundle_schema_version': TWIN_BUNDLE_SCHEMA_VERSION, 'engine': 'watertap-engine-mcp', 'session_id': session.config.session_id, 'name': session.config.name, 'session': session_dict, 'solved': solved, 'is_solved': bool(is_solved), 'solve_status': session.solve_status, 'content_hash': _content_hash(session_dict), 'solved_hash': _content_hash(solved) if solved is not None else None, 'exported': {'units': len(session.units), 'connections': len(session.connections), 'process_constraints': len(session.process_constraints), 'feed_states': len(session.feed_states)}, 'reload_path': 'import_twin_bundle(bundle) — or SessionManager.load(session_id) on the persisted session JSON. model_create/SFILES is a topology-only companion view, not the reloadable twin.'}

def import_twin_bundle(bundle: Dict[str, Any], new_session_id: Optional[str]=None, overwrite: bool=False) -> Dict[str, Any]:
    """Reload a digital-twin bundle into a flowsheet session.

    Reconstructs the session (topology, inputs, feed states, process
    constraints, solved results) from the bundle's session JSON and persists it,
    proving the session JSON is the reloadable twin artifact.

    Args:
        bundle: A bundle produced by ``export_twin_bundle``
        new_session_id: Optional id for the reloaded session (defaults to the
            bundle's original session id)
        overwrite: Allow overwriting an existing session with the same id

    Returns:
        Reloaded session metadata, including a hash-match flag against the bundle.
    """
    if not isinstance(bundle, dict) or bundle.get('bundle_kind') != TWIN_BUNDLE_KIND:
        return {'error': 'Payload is not a watertap_flowsheet_twin bundle'}
    session_dict = bundle.get('session')
    if not isinstance(session_dict, dict):
        return {'error': "Bundle is missing the 'session' JSON payload"}
    target_id = new_session_id or session_dict.get('config', {}).get('session_id')
    if not target_id:
        return {'error': 'Bundle has no session id and none was supplied'}
    if session_manager.exists(target_id) and (not overwrite):
        return {'error': f"Session '{target_id}' already exists (set overwrite=True)"}
    session = FlowsheetSession.from_dict(deepcopy(session_dict))
    session.config.session_id = target_id
    session_manager.save(session)
    reloaded_hash = _content_hash(session.to_dict())
    declared_hash = bundle.get('content_hash')
    hash_match = reloaded_hash == declared_hash if new_session_id is None or new_session_id == session_dict.get('config', {}).get('session_id') else None
    return {'session_id': target_id, 'name': session.config.name, 'units': len(session.units), 'connections': len(session.connections), 'is_solved': bool(bundle.get('is_solved')), 'content_hash': reloaded_hash, 'hash_match': hash_match}

def _apply_flowsheet_perturbations(session: FlowsheetSession, perturbed_inputs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Apply GSA perturbations to a (cloned) session in place.

    ``perturbed_inputs`` is an optional dict with any of:
      - ``fixed_vars``: {unit_id: {var_name: value}} merged onto unit fixed vars
        (volumes, KLa, metal_dose, split fractions, ...)
      - ``feed_states``: {feed_unit_id: {"state_args": {...}}} replacing a feed
        state (flow, conc_mass_comp, temperature, ...)
      - ``feed_conc``: {feed_unit_id: {component: value}} convenience override of
        individual conc_mass_comp entries on an existing feed state
      - ``engineering_inputs``: {unit_id: {key: value}} merged onto engineering
        inputs

    Returns a record of what was applied (for the evaluation result).
    """
    applied: Dict[str, Any] = {}
    if not isinstance(perturbed_inputs, dict):
        return applied
    fixed_vars = perturbed_inputs.get('fixed_vars')
    if isinstance(fixed_vars, dict):
        applied_fixed: Dict[str, Any] = {}
        for unit_id, overrides in fixed_vars.items():
            unit = session.units.get(unit_id)
            if unit is None or not isinstance(overrides, dict):
                continue
            for var_name, value in overrides.items():
                unit.fixed_vars[var_name] = float(value)
            applied_fixed[unit_id] = dict(overrides)
        if applied_fixed:
            applied['fixed_vars'] = applied_fixed
    feed_states = perturbed_inputs.get('feed_states')
    if isinstance(feed_states, dict):
        applied_feeds = []
        for feed_id, feed_state in feed_states.items():
            if not isinstance(feed_state, dict):
                continue
            session.feed_states[feed_id] = deepcopy(feed_state)
            applied_feeds.append(feed_id)
        if applied_feeds:
            applied['feed_states'] = applied_feeds
    feed_conc = perturbed_inputs.get('feed_conc')
    if isinstance(feed_conc, dict):
        applied_conc: Dict[str, Any] = {}
        for feed_id, conc_overrides in feed_conc.items():
            feed_state = session.feed_states.get(feed_id)
            if not isinstance(feed_state, dict) or not isinstance(conc_overrides, dict):
                continue
            state_args = feed_state.setdefault('state_args', {})
            conc_map = state_args.setdefault('conc_mass_comp', {})
            for component, value in conc_overrides.items():
                conc_map[component] = float(value)
            applied_conc[feed_id] = dict(conc_overrides)
        if applied_conc:
            applied['feed_conc'] = applied_conc
    engineering_inputs = perturbed_inputs.get('engineering_inputs')
    if isinstance(engineering_inputs, dict):
        applied_eng: Dict[str, Any] = {}
        for unit_id, overrides in engineering_inputs.items():
            unit = session.units.get(unit_id)
            if unit is None or not isinstance(overrides, dict):
                continue
            unit.engineering_inputs.update({key: float(value) for key, value in overrides.items()})
            applied_eng[unit_id] = dict(overrides)
        if applied_eng:
            applied['engineering_inputs'] = applied_eng
    feed_scalar = perturbed_inputs.get('feed_scalar')
    if isinstance(feed_scalar, dict):
        applied_scalar: Dict[str, Any] = {}
        for feed_id, overrides in feed_scalar.items():
            feed_state = session.feed_states.get(feed_id)
            if not isinstance(feed_state, dict) or not isinstance(overrides, dict):
                continue
            state_args = feed_state.setdefault('state_args', {})
            recorded: Dict[str, float] = {}
            for arg_name, value in overrides.items():
                fval = float(value)
                existing = state_args.get(arg_name)
                if isinstance(existing, dict) and existing:
                    state_args[arg_name] = {idx: fval for idx in existing}
                else:
                    state_args[arg_name] = {'0': fval}
                recorded[arg_name] = fval
            if recorded:
                applied_scalar[feed_id] = recorded
        if applied_scalar:
            applied['feed_scalar'] = applied_scalar
    process_constraint_perturbations = perturbed_inputs.get('process_constraints')
    if isinstance(process_constraint_perturbations, dict):
        applied_pc: Dict[str, Any] = {}
        for match_key, fields in process_constraint_perturbations.items():
            if not isinstance(fields, dict):
                continue
            for constraint in session.process_constraints:
                if not isinstance(constraint, dict):
                    continue
                if constraint.get('name') == match_key or constraint.get('type') == match_key:
                    for field_name, value in fields.items():
                        constraint[field_name] = float(value) if isinstance(value, (int, float)) else value
                    applied_pc[match_key] = dict(fields)
                    if constraint.get('type') == 'srt' and 'target_days' in fields:
                        rmeta = session.config.runtime_metadata = dict(session.config.runtime_metadata or {})
                        dparams = rmeta.setdefault('design_parameters', {})
                        if isinstance(dparams, dict):
                            dparams['target_srt_days'] = float(fields['target_days'])
        if applied_pc:
            applied['process_constraints'] = applied_pc
    design_parameter_perturbations = perturbed_inputs.get('design_parameters')
    if isinstance(design_parameter_perturbations, dict):
        rmeta = session.config.runtime_metadata = dict(session.config.runtime_metadata or {})
        dparams = rmeta.setdefault('design_parameters', {})
        applied_dp: Dict[str, Any] = {}
        if isinstance(dparams, dict):
            for key, value in design_parameter_perturbations.items():
                dparams[key] = float(value) if isinstance(value, (int, float)) else value
                applied_dp[key] = dparams[key]
        if applied_dp:
            applied['design_parameters'] = applied_dp
    return applied

def _capture_variable_snapshot(model: Any) -> Dict[str, float]:
    """Capture a name-keyed snapshot of model variable values for warm-starting."""
    from pyomo.environ import Var, value
    snapshot: Dict[str, float] = {}
    for var in model.component_data_objects(Var, descend_into=True):
        try:
            raw = value(var, exception=False)
        except Exception:
            raw = None
        if raw is None:
            continue
        try:
            snapshot[var.name] = float(raw)
        except (TypeError, ValueError):
            continue
    return snapshot

def _apply_variable_snapshot(model: Any, snapshot: Dict[str, float]) -> int:
    """Apply a name-keyed warm-start snapshot to a freshly built model.

    Only free (unfixed) variables present under the same name are seeded; the
    topology is identical across GSA perturbations, so name matching is exact.
    Returns the count of variables seeded.
    """
    from pyomo.environ import Var
    if not snapshot:
        return 0
    seeded = 0
    for var in model.component_data_objects(Var, descend_into=True):
        if getattr(var, 'fixed', False):
            continue
        guess = snapshot.get(var.name)
        if guess is None:
            continue
        try:
            var.set_value(guess, skip_validation=True)
            seeded += 1
        except Exception:
            continue
    return seeded

def _solved_state_variable_snapshot(units: Dict[str, Any], solved_state: Any) -> Dict[str, float]:
    """Reconstruct a name-keyed variable snapshot from a persisted solved state.

    ``_extract_solved_state`` stores each unit's solved variables keyed by a
    name *relative* to that unit's block. A clone is built with identical
    topology, so prefixing each relative name with the clone unit block's Pyomo
    name reconstructs the exact ``var.name`` and lets ``_apply_variable_snapshot``
    seed it. This is what auto-warm-starts a GSA evaluation from the base solved
    design point when no explicit snapshot is supplied.
    """
    if not isinstance(solved_state, dict):
        return {}
    unit_states = solved_state.get('units')
    if not isinstance(unit_states, dict):
        return {}
    snapshot: Dict[str, float] = {}
    for unit_id, unit_block in units.items():
        unit_state = unit_states.get(unit_id)
        if not isinstance(unit_state, dict):
            continue
        variables = unit_state.get('variables')
        if not isinstance(variables, dict):
            continue
        prefix = unit_block.name
        for relative, snap in variables.items():
            raw = snap.get('value') if isinstance(snap, dict) else snap
            if raw is None:
                continue
            try:
                snapshot[f'{prefix}.{relative}'] = float(raw)
            except (TypeError, ValueError):
                continue
    return snapshot

def evaluate_flowsheet(session_id: str, perturbed_inputs: Optional[Dict[str, Any]]=None, warm_start_values: Optional[Dict[str, float]]=None, solver: Optional[str]=None, max_wall_time_s: int=120, skip_initialization: Optional[bool]=None, solver_options: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
    """Clone, perturb, and solve a flowsheet in-process WITHOUT persistence.

    This is the entrypoint a Global Sensitivity Analysis driver calls many
    times. It never mutates the canonical session and writes no artifacts:

      1. Deep-copy the base session in memory (canonical session untouched).
      2. Apply ``perturbed_inputs`` (feed states / fixed vars / engineering
         inputs — the GSA factors).
      3. Build the model and warm-start it from ``warm_start_values`` (or the
         base session's solved state) where possible.
      4. Run the full hygiene pipeline synchronously.
      5. Extract KPIs via ``extract_solved_kpis`` on success; a non-optimal
         solve is returned as an explicit outcome, never raised.

    The returned ``solved_values`` snapshot can be threaded back in as the next
    sample's ``warm_start_values`` to warm-start a GSA sweep.

    Args:
        session_id: Base (canonical) session to evaluate against
        perturbed_inputs: GSA factor overrides (see ``_apply_flowsheet_perturbations``)
        warm_start_values: Optional name-keyed variable snapshot to seed the solve
        solver: Optional solver name override
        max_wall_time_s: Per-evaluation solve wall-time budget

    Returns:
        {converged, status, termination_condition, pipeline_state, kpis,
         solved_values, perturbations_applied, warm_started, warnings}
    """
    from watertap_engine_mcp.solver.pipeline import HygienePipeline, PipelineConfig
    from watertap_engine_mcp.utils.model_builder import ModelBuilder
    from watertap_engine_mcp.results.kpis import extract_solved_kpis
    try:
        base = session_manager.load(session_id)
    except FileNotFoundError:
        return {'converged': False, 'status': 'session_not_found', 'error_message': f"Session '{session_id}' not found"}
    clone = _clone_session_object(base, include_results=False)
    perturbations_applied = _apply_flowsheet_perturbations(clone, perturbed_inputs)
    try:
        builder = ModelBuilder(clone)
        model = builder.build()
        units = builder.get_units()
    except Exception as exc:
        return {'converged': False, 'status': 'build_error', 'error_message': str(exc), 'perturbations_applied': perturbations_applied}
    warm_started = 0
    warm_start_source = None
    if warm_start_values:
        warm_started = _apply_variable_snapshot(model, warm_start_values)
        if warm_started:
            warm_start_source = 'explicit'
    elif base.solve_status == 'optimal' and isinstance(base.results, dict):
        base_snapshot = _solved_state_variable_snapshot(units, base.results.get('solved_state'))
        warm_started = _apply_variable_snapshot(model, base_snapshot)
        if warm_started:
            warm_start_source = 'base_solved_state'
    runtime_solver_options = clone.config.runtime_metadata.get('solver_options')
    effective_solver_options = dict(runtime_solver_options) if isinstance(runtime_solver_options, dict) else {}
    if isinstance(solver_options, dict):
        effective_solver_options.update(solver_options)
    runtime_metadata = clone.config.runtime_metadata or {}
    is_asm2d_family = RoleResolver.from_session(clone).is_asm2d_recycle_family()
    allow_near_feasible_asm2d = is_asm2d_family
    if skip_initialization is None:
        skip_initialization_effective = warm_started > 0
    else:
        skip_initialization_effective = bool(skip_initialization)
    config = PipelineConfig(solver=solver or effective_solver_options.get('solver', 'ipopt'), solver_options=effective_solver_options, max_wall_time_s=int(max_wall_time_s), allow_near_feasible_asm2d=allow_near_feasible_asm2d, skip_initialization=skip_initialization_effective)
    try:
        pipeline = HygienePipeline(model, config, units=units, session=clone)
        result = pipeline.run_all()
    except Exception as exc:
        return {'converged': False, 'status': 'solve_error', 'error_message': str(exc), 'perturbations_applied': perturbations_applied, 'warm_started_variables': warm_started}
    details = result.details if isinstance(result.details, dict) else {}
    solve_result = details.get('solve_result') if isinstance(details.get('solve_result'), dict) else {}
    termination = solve_result.get('termination_condition') or details.get('termination_condition')
    pipeline_state = getattr(getattr(result, 'state', None), 'value', None)
    optimal = bool(result.success and termination == 'optimal')
    near_feasible_accepted = _detect_near_feasible_accepted(details)
    converged = bool(result.success) and (optimal or near_feasible_accepted or pipeline_state == 'completed')
    if near_feasible_accepted and converged:
        solution_quality = 'near_feasible'
        residual_basis = _residual_basis_from_details(details)
    elif converged:
        solution_quality = 'strict_optimal'
        residual_basis = None
    else:
        solution_quality = 'infeasible_structured'
        residual_basis = None
    payload: Dict[str, Any] = {'converged': converged, 'optimal': optimal, 'near_feasible_accepted': near_feasible_accepted, 'solution_quality': solution_quality, 'residual_basis': residual_basis, 'status': 'optimal' if optimal else 'near_feasible' if converged and near_feasible_accepted else details.get('convergence_status') or ('converged' if converged else 'not_optimal'), 'termination_condition': termination, 'pipeline_state': pipeline_state, 'perturbations_applied': perturbations_applied, 'warm_started_variables': warm_started, 'warm_start_source': warm_start_source, 'skip_initialization': skip_initialization_effective, 'warnings': list(getattr(result, 'warnings', []) or [])}
    if converged:
        try:
            payload['kpis'] = extract_solved_kpis(model, units)
        except Exception as exc:
            payload['kpis'] = None
            payload['kpi_error'] = str(exc)
        payload['solved_values'] = _capture_variable_snapshot(model)
    else:
        payload['kpis'] = None
    return payload

def _detect_near_feasible_accepted(details: Any) -> bool:
    """Return True if the hygiene-pipeline result accepted a near-feasible solve.

    The near-feasible acceptance flag may sit at the top of the completed
    details or nested inside the recorded solve/recovery sub-result, so scan
    the common locations rather than relying on one fixed key.
    """
    if not isinstance(details, dict):
        return False
    if details.get('accepted_near_feasible'):
        return True
    for key in ('solve_result', 'recovery', 'initial_solve_result', 'stage_result'):
        sub = details.get(key)
        if isinstance(sub, dict) and (sub.get('accepted_near_feasible') or (isinstance(sub.get('details'), dict) and sub['details'].get('accepted_near_feasible'))):
            return True
    return False

def _residual_basis_from_details(details: Any) -> Optional[Dict[str, Any]]:
    """Pull the near-feasible ``residual_basis`` from a pipeline details payload.

    The structured residual breakdown is attached to the SOLVING-stage details
    (the near-feasible acceptance dict) and surfaces as ``solve_result`` in the
    completed payload, so scan the same locations as ``_detect_near_feasible_accepted``.
    """
    if not isinstance(details, dict):
        return None
    rb = details.get('residual_basis')
    if isinstance(rb, dict):
        return rb
    for key in ('solve_result', 'recovery', 'initial_solve_result', 'stage_result'):
        sub = details.get(key)
        if not isinstance(sub, dict):
            continue
        rb = sub.get('residual_basis')
        if isinstance(rb, dict):
            return rb
        inner = sub.get('details')
        if isinstance(inner, dict) and isinstance(inner.get('residual_basis'), dict):
            return inner['residual_basis']
    return None

def _solution_quality_for_session(session: FlowsheetSession) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Map a solved session to ``(solution_quality, residual_basis)``.

    A near-feasible acceptance (square ASM2D recycle solve accepted under the
    proposal residual tolerance) reads ``"near_feasible"`` with the structured
    ``residual_basis`` that justifies it; a clean IPOPT-optimal solve reads
    ``"strict_optimal"`` with no residual basis. This is the single source of
    truth so a near-feasible solve is NEVER presented as a strict optimum.
    """
    metadata = _solve_result_metadata(session)
    accepted = bool(metadata.get('accepted_near_feasible') or metadata.get('accepted_near_feasible_solve'))
    if not accepted:
        results = session.results if isinstance(session.results, dict) else {}
        accepted = _detect_near_feasible_accepted(results) or _detect_near_feasible_accepted(results.get('details') if isinstance(results.get('details'), dict) else {})
    if accepted:
        residual_basis = metadata.get('residual_basis')
        if not isinstance(residual_basis, dict):
            residual_basis = _residual_basis_from_details(session.results if isinstance(session.results, dict) else {})
        return ('near_feasible', residual_basis)
    return ('strict_optimal', None)
_GSA_STUDIES_DIR = STORAGE_DIR / 'gsa_studies'

def _gsa_study_path(study_id: str) -> Path:
    return _GSA_STUDIES_DIR / f'{study_id}.json'

def _write_gsa_study(study_id: str, payload: Dict[str, Any]) -> None:
    _GSA_STUDIES_DIR.mkdir(parents=True, exist_ok=True)
    path = _gsa_study_path(study_id)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, default=str))
    tmp.replace(path)

def _ensure_sensitivity_study_artifact_type() -> None:
    """Ensure the canonical DB accepts ``sensitivity_study`` artifacts.

    Mirrors the in-server registration pattern used for simulation_diagnostics
    and solver_infeasibility_report: idempotently upsert the artifact_type so
    the engineering_artifact FK validates. ``post_commissioning`` is used as the
    non-rendered analysis bucket (the proposal compiler does not surface GSA
    scenario studies; they are off-design analytical artifacts).
    """
    dsn = os.environ.get('ENGINEERING_MCP_DATABASE_URL') or os.environ.get('ENGINEERING_DATABASE_URL') or os.environ.get('DATABASE_URL')
    if not dsn:
        return
    try:
        import psycopg2
    except ImportError:
        return
    conn = psycopg2.connect(dsn.replace('+asyncpg', ''))
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('app.user_id', %s, true)", ('watertap-engine-mcp.run_global_sensitivity',))
                cur.execute("SELECT set_config('app.reason', %s, true)", ('register sensitivity_study artifact type',))
                cur.execute('\n                    INSERT INTO artifact_type_registry (\n                        artifact_type,\n                        description,\n                        pydantic_class,\n                        proposal_surface_status\n                    )\n                    VALUES (%s, %s, %s, %s)\n                    ON CONFLICT (artifact_type) DO UPDATE\n                    SET description = EXCLUDED.description,\n                        pydantic_class = EXCLUDED.pydantic_class,\n                        proposal_surface_status = EXCLUDED.proposal_surface_status\n                    ', ('sensitivity_study', 'Global sensitivity / uncertainty study (Morris / Sobol / LHS) over a WaterTAP twin; non-canonical scenario scope (does not supersede the design-point simulation_report or mass_balance)', None, 'post_commissioning'))
    finally:
        conn.close()

def _gsa_ranking(result: Any, kpi_names: List[str]) -> Dict[str, List[List[Any]]]:
    """Per-KPI factor ranking by the method's primary sensitivity metric."""
    metric_by_method = {'morris': 'mu_star', 'sobol': 'ST', 'lhs_mc': 'spearman'}
    metric = metric_by_method.get(result.method, 'mu_star')
    ranking: Dict[str, List[List[Any]]] = {}
    for kpi in kpi_names:
        try:
            ranking[kpi] = [[name, value] for name, value in result.ranking(kpi, metric)]
        except Exception:
            ranking[kpi] = []
    return {'metric': metric, 'by_kpi': ranking}

def _run_gsa_study(study_id: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Build the GSA problem + WaterTAP evaluate-adapter, run it, and persist.

    Returns the final study payload (also written to the study store). Persists
    the GSAResult as a ``sensitivity_study`` engineering_artifact under a
    NON-CANONICAL scenario scope — a distinct artifact_type AND a GSA-tagged
    plant_slug — so it can never supersede the canonical design-point
    simulation_report / mass_balance rows.
    """
    from puran_mcp_support.engineering.sensitivity import Distribution, Factor, GSAMetadata, GSAProblem, run_lhs_mc, run_morris, run_sobol
    import watertap_engine_mcp.gsa_adapter as gsa_adapter
    method = cfg['method']
    factors_spec = cfg['factors']
    kpis = cfg['kpis']
    base_session_id = cfg['base_session_id']
    try:
        base_session = session_manager.load(base_session_id)
        input_hash = _content_hash(base_session.to_dict())
        topology_name = (base_session.config.runtime_metadata or {}).get('topology_name')
    except Exception:
        input_hash = None
        topology_name = None
    factors = [Factor(name=f['name'], low=float(f['low']), high=float(f['high']), distribution=Distribution(f.get('distribution', 'uniform')), nominal=float(f['nominal']) if f.get('nominal') is not None else None, units=f.get('units'), description=f.get('description')) for f in factors_spec]
    problem = GSAProblem(factors=factors, outputs=list(kpis), metadata=GSAMetadata(engine='watertap-engine-mcp', model_version=str(topology_name) if topology_name else None, input_hash=input_hash, label=cfg.get('scenario_tag'), notes=f'GSA {method} over WaterTAP twin {base_session_id}'))
    adapter_diag: Dict[str, Any] = {}
    evaluate = gsa_adapter.make_watertap_evaluate(base_session_id, list(kpis), cfg.get('factor_perturbation_map'), factors=factors, local_basin_threshold=cfg.get('local_basin_threshold'), warm_max_iter=int(cfg.get('warm_max_iter', 1000)), warm_max_wall_time_s=int(cfg.get('warm_max_wall_time_s', 70)), cold_max_wall_time_s=int(cfg.get('cold_max_wall_time_s', 160)), cold_fallback=bool(cfg.get('cold_fallback', True)), diagnostics=adapter_diag)
    seed = int(cfg.get('seed', 42))
    N = int(cfg.get('N', 4))
    if method == 'morris':
        result = run_morris(problem, evaluate, N=N, seed=seed, num_levels=int(cfg.get('num_levels', 4)))
    elif method == 'sobol':
        result = run_sobol(problem, evaluate, N=N, seed=seed, calc_second_order=bool(cfg.get('calc_second_order', False)))
    elif method == 'lhs':
        result = run_lhs_mc(problem, evaluate, N=N, seed=seed)
    else:
        raise ValueError(f'Unknown GSA method {method!r} (morris|sobol|lhs)')
    result_dict = result.to_dict()
    result_dict.setdefault('diagnostics', {})['watertap_adapter'] = adapter_diag
    ranking = _gsa_ranking(result, list(kpis))
    scenario_tag = cfg.get('scenario_tag') or method
    plant_slug = cfg.get('plant_slug')
    scenario_plant_slug = cfg.get('scenario_plant_slug') or (f'{plant_slug}-gsa-{scenario_tag}' if plant_slug else f'gsa-{scenario_tag}')
    artifact_payload = {'schema': 'sensitivity_study/v1', 'engine': 'watertap-engine-mcp', 'method': method, 'base_session_id': base_session_id, 'scenario_tag': scenario_tag, 'plant_slug': scenario_plant_slug, 'factors': factors_spec, 'kpis': list(kpis), 'N': N, 'seed': seed, 'input_hash': input_hash, 'model_version': topology_name, 'result': result_dict, 'ranking': ranking}
    persistence: Dict[str, Any] = {'persisted': False}
    if cfg.get('persist', True) and cfg.get('project_ref'):
        try:
            from watertap_engine_mcp.public_persistence import persist_named_artifact
            _ensure_sensitivity_study_artifact_type()
            artifact_id, content_hash = persist_named_artifact(artifact_payload, artifact_type='sensitivity_study', producer_tool='watertap-engine-mcp.run_global_sensitivity', project_ref=cfg['project_ref'], scope_refs={'plant_slug': scenario_plant_slug, 'stage': 'gsa', 'scenario_tag': scenario_tag, 'method': method, 'engine': 'watertap', 'base_session_id': base_session_id}, credibility_status='preliminary', credibility_grade='screening', credibility_basis='assumed')
            persistence = {'persisted': bool(artifact_id), 'artifact_id': artifact_id, 'content_hash': content_hash, 'artifact_type': 'sensitivity_study', 'scope': {'project_ref': cfg['project_ref'], 'plant_slug': scenario_plant_slug, 'stage': 'gsa', 'scenario_tag': scenario_tag}, 'non_canonical': True}
        except Exception as exc:
            persistence = {'persisted': False, 'error': str(exc)}
    n_runs = result_dict.get('n_runs') or 0
    n_failed = result_dict.get('n_failed') or 0
    failure_fraction = result_dict.get('failure_fraction')
    if failure_fraction is None:
        failure_fraction = n_failed / n_runs if n_runs else 0.0
    return {'study_id': study_id, 'status': 'complete', 'method': method, 'base_session_id': base_session_id, 'n_runs': result_dict.get('n_runs'), 'n_evaluated': result_dict.get('n_evaluated'), 'n_failed': result_dict.get('n_failed'), 'failure_fraction': failure_fraction, 'n_forced_cold': (adapter_diag.get('counters') or {}).get('forced_cold'), 'local_basin_threshold': adapter_diag.get('local_basin_threshold'), 'failed_samples': result_dict.get('failed_samples'), 'kpis_with_failures': result_dict.get('kpis_with_failures'), 'ranking': ranking, 'indices': result_dict.get('indices'), 'uncertainty': result_dict.get('uncertainty'), 'adapter_diagnostics': adapter_diag, 'persistence': persistence, 'result': result_dict}

def run_global_sensitivity(factors: List[Dict[str, Any]], kpis: List[str], base_session_id: Optional[str]=None, twin_bundle_path: Optional[str]=None, method: str='morris', N: int=4, seed: int=42, num_levels: int=4, calc_second_order: bool=False, factor_perturbation_map: Optional[Dict[str, Any]]=None, project_ref: Optional[str]=None, plant_slug: Optional[str]=None, scenario_tag: Optional[str]=None, scenario_plant_slug: Optional[str]=None, warm_max_iter: int=1000, warm_max_wall_time_s: int=70, cold_max_wall_time_s: int=160, cold_fallback: bool=True, local_basin_threshold: Optional[float]=None, persist: bool=True, run_in_background: bool=True) -> Dict[str, Any]:
    """Run a Global Sensitivity Analysis (Morris / Sobol / LHS) over a WaterTAP twin."""
    if not factors:
        return {'error': 'run_global_sensitivity requires at least one factor'}
    if not kpis:
        return {'error': 'run_global_sensitivity requires at least one KPI'}
    if method not in ('morris', 'sobol', 'lhs'):
        return {'error': f'Unknown method {method!r} (morris|sobol|lhs)'}
    resolved_base = base_session_id
    if not resolved_base and twin_bundle_path:
        try:
            bundle = json.loads(Path(twin_bundle_path).read_text())
        except Exception as exc:
            return {'error': f'Could not read twin_bundle_path: {exc}'}
        imported = import_twin_bundle(bundle, overwrite=False)
        if isinstance(imported, dict) and imported.get('error'):
            resolved_base = bundle.get('session', {}).get('config', {}).get('session_id')
        else:
            resolved_base = imported.get('session_id')
    if not resolved_base:
        return {'error': 'Provide base_session_id or a valid twin_bundle_path'}
    if not session_manager.exists(resolved_base):
        return {'error': f"Base session '{resolved_base}' not found"}
    tag = scenario_tag or method
    cfg = {'method': method, 'factors': factors, 'kpis': kpis, 'base_session_id': resolved_base, 'N': N, 'seed': seed, 'num_levels': num_levels, 'calc_second_order': calc_second_order, 'factor_perturbation_map': factor_perturbation_map, 'project_ref': project_ref, 'plant_slug': plant_slug, 'scenario_tag': tag, 'scenario_plant_slug': scenario_plant_slug, 'warm_max_iter': warm_max_iter, 'warm_max_wall_time_s': warm_max_wall_time_s, 'cold_max_wall_time_s': cold_max_wall_time_s, 'cold_fallback': cold_fallback, 'local_basin_threshold': local_basin_threshold, 'persist': persist}
    study_id = f'gsa-{method}-{uuid.uuid4().hex[:10]}'
    submitted = {'study_id': study_id, 'status': 'running', 'method': method, 'base_session_id': resolved_base, 'factors': [f.get('name') for f in factors], 'kpis': kpis, 'N': N, 'seed': seed, 'scenario_tag': tag}
    _write_gsa_study(study_id, submitted)
    if not run_in_background:
        try:
            final = _run_gsa_study(study_id, cfg)
        except Exception as exc:
            final = {'study_id': study_id, 'status': 'failed', 'error': str(exc)}
        _write_gsa_study(study_id, final)
        return final

    def _worker() -> None:
        try:
            final = _run_gsa_study(study_id, cfg)
        except Exception as exc:
            import traceback
            final = {'study_id': study_id, 'status': 'failed', 'error': str(exc), 'traceback': traceback.format_exc()}
        _write_gsa_study(study_id, final)
    thread = threading.Thread(target=_worker, name=f'gsa-{study_id}', daemon=True)
    thread.start()
    submitted['poll_with'] = f"get_sensitivity_study(study_id='{study_id}')"
    submitted['message'] = "GSA study submitted on a background thread. Poll get_sensitivity_study until status is 'complete' or 'failed'."
    return submitted

def get_sensitivity_study(study_id: str) -> Dict[str, Any]:
    """Return the current status / result of a GSA study by id.

    Status is one of ``running`` | ``complete`` | ``failed``. When complete, the
    payload carries the Morris/Sobol/LHS ranking, indices, failed-eval
    accounting, per-evaluation adapter diagnostics, and the persistence scope.
    """
    path = _gsa_study_path(study_id)
    if not path.exists():
        return {'error': f"GSA study '{study_id}' not found", 'status': 'unknown'}
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        return {'error': f"Could not read GSA study '{study_id}': {exc}", 'status': 'unknown'}

def calibrate_membrane_parameters(*args, **kwargs):
    raise NotImplementedError('This application integration is excluded from the public release; use the general engine APIs with your own inputs.')

def size_membrane_system(*args, **kwargs):
    raise NotImplementedError('This application integration is excluded from the public release; use the general engine APIs with your own inputs.')

class _WhatIfMembraneAreaRequest(_StrictFiniteBaseModel):
    """Strict input contract for what_if_membrane_area.

    The MCP path bypasses the catalog request models (typed_action_wrapper builds its
    schema from the signature), so this action validates its own inputs — finiteness,
    physical bounds, positive-integer module counts, and feed fractions that sum to 1.
    """
    base_stage1_modules: int = PydField(ge=1, le=50)
    base_stage2_modules: int = PydField(ge=1, le=50)
    candidate_stages: List[Tuple[int, int]]
    pressures_bara: List[float]
    current_feed_pressure_bara: Optional[float] = PydField(default=None, gt=1.0, lt=100.0)
    perm_co2_gpu: Optional[float] = PydField(default=None, gt=0.0)
    selectivity: Optional[float] = PydField(default=None, gt=1.0)
    per_module_area_cm2: Optional[float] = PydField(default=None, gt=0.0)
    raw_nm3h: float = PydField(gt=0.0)
    feed_ch4: float = PydField(gt=0.0, lt=1.0)
    feed_co2: Optional[float] = PydField(default=None, gt=0.0, lt=1.0)
    feed_n2: Optional[float] = PydField(default=None, ge=0.0, lt=1.0)
    target_product_ch4: float = PydField(gt=0.0, lt=1.0)
    target_recovery: float = PydField(gt=0.0, lt=1.0)
    raw_throughputs_nm3h: Optional[List[float]] = None

    @pyd_model_validator(mode='after')
    def _physical_bounds(self) -> '_WhatIfMembraneAreaRequest':
        if not self.pressures_bara:
            raise ValueError('pressures_bara must be a non-empty pressure grid')
        for p in self.pressures_bara:
            if not 1.0 < p < 100.0:
                raise ValueError(f'pressure {p} bara outside the physical sweep range (1, 100)')
        if len(set(self.pressures_bara)) != len(self.pressures_bara):
            raise ValueError('pressures_bara contains duplicate grid points')
        if self.raw_throughputs_nm3h is not None:
            if not self.raw_throughputs_nm3h or any((v <= 0.0 for v in self.raw_throughputs_nm3h)):
                raise ValueError('raw_throughputs_nm3h must be a non-empty positive grid')
            if len(set(self.raw_throughputs_nm3h)) != len(self.raw_throughputs_nm3h):
                raise ValueError('raw_throughputs_nm3h contains duplicate grid points')
        for n1, n2 in self.candidate_stages:
            if not (1 <= n1 <= 50 and 1 <= n2 <= 50):
                raise ValueError(f'candidate stage pair ({n1}, {n2}) outside 1..50 modules')
        if (self.feed_co2 is None) != (self.feed_n2 is None):
            raise ValueError('supply feed_co2 and feed_n2 together, or neither')
        if self.feed_co2 is not None and self.feed_n2 is not None:
            tot = self.feed_ch4 + self.feed_co2 + self.feed_n2
            if abs(tot - 1.0) > 1e-06:
                raise ValueError(f'feed mole fractions sum to {tot:.6f}, not 1 — an un-normalized feed silently biases the reported CH4 recovery by 1/sum')
        return self
_WHAT_IF_FEED_CO2_N2_RATIO = (0.35, 0.017)
_WHAT_IF_LEGACY_RAW_NM3H = 1400.0
_WHAT_IF_LEGACY_FEED_CH4 = 0.633

def what_if_membrane_area(*args, **kwargs):
    raise NotImplementedError('This application integration is excluded from the public release; use the general engine APIs with your own inputs.')

def get_solve_results(job_id: str) -> Dict[str, Any]:
    """Get results of a completed solve job.

    Args:
        job_id: Job identifier from solve() / build_and_solve()

    Returns:
        Job results if complete, or status metadata if still running
    """
    return get_job_results(job_id)

def get_job_status(job_id: str) -> Dict[str, Any]:
    """Get status of a background job.

    Alias for get_solve_status for naming consistency with plan.

    Args:
        job_id: Job identifier

    Returns:
        Job status, progress, and result if complete
    """
    return get_solve_status(job_id)

def get_job_results(job_id: str) -> Dict[str, Any]:
    """Get results of a completed background job.

    Args:
        job_id: Job identifier

    Returns:
        Job results if complete, status otherwise
    """
    status = job_manager.get_status(job_id)
    if status is None:
        return redact_public_payload({'error': f"Job '{job_id}' not found"})
    if status.status == JobStatus.FAILED:
        return redact_public_payload({'job_id': job_id, 'status': status.status.value, 'failure_reason': status.error, 'message': status.message or 'Job failed'})
    if status.status == JobStatus.CANCELLED:
        return redact_public_payload({'job_id': job_id, 'status': status.status.value, 'message': status.message or 'Job was cancelled'})
    if status.status != JobStatus.COMPLETED:
        return redact_public_payload({'job_id': job_id, 'status': status.status.value, 'message': 'Job not yet complete', 'progress': status.progress})
    return redact_public_payload(completed_job_results_envelope(job_id, status.result))
_PUBLIC_ACTION_EXPORTS = frozenset((name for name, value in globals().items() if inspect.isfunction(value) and value.__module__ == __name__ and (not name.startswith('_'))))
_PUBLIC_ACTION_WRAPPER_CACHE: dict[str, Any] = {}

def _external_action_wrapper(action_id: str, handler: Any) -> Any:
    """Return an external-facing wrapper that normalizes public action errors."""
    from watertap_engine_mcp.api.errors import exception_to_error
    from watertap_engine_mcp.api.tools import normalize_action_result

    @wraps(handler)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return normalize_action_result(action_id, handler(*args, **kwargs))
        except Exception as exc:
            return exception_to_error(exc, code=f'{action_id.upper()}_FAILED', details={'tool': action_id}).model_dump(mode='python')
    wrapper.__wrapped__ = handler
    return wrapper

class _ActionsModule(ModuleType):
    """Expose normalized public actions without changing internal call semantics."""

    def __getattribute__(self, name: str) -> Any:
        value = super().__getattribute__(name)
        if name not in super().__getattribute__('_PUBLIC_ACTION_EXPORTS') or not callable(value):
            return value
        cache = super().__getattribute__('_PUBLIC_ACTION_WRAPPER_CACHE')
        cached = cache.get(name)
        if cached is not None and getattr(cached, '__wrapped__', None) is value:
            return cached
        wrapper = _external_action_wrapper(name, value)
        cache[name] = wrapper
        return wrapper
if not isinstance(sys.modules[__name__], _ActionsModule):
    sys.modules[__name__].__class__ = _ActionsModule
