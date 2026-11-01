"""Unified stream extraction for phase and no-phase property packages."""
from __future__ import annotations
from copy import deepcopy
from typing import Any, Iterable, Mapping
COMMON_PORT_NAMES: tuple[str, ...] = ('inlet', 'outlet', 'permeate', 'retentate', 'feed', 'product', 'brine', 'treated', 'byproduct', 'reject', 'vapor', 'liquid', 'waste', 'recycle', 'feed_inlet', 'feed_outlet', 'product_outlet', 'waste_outlet', 'inlet_feed', 'outlet_brine', 'outlet_vapor', 'feed_side_inlet', 'feed_side_outlet', 'sweep_side_outlet')
VALUE_STATE_VARS: tuple[str, ...] = ('flow_mol_phase_comp', 'flow_mol_comp', 'flow_mol', 'flow_vol', 'temperature', 'pressure', 'conc_mass_phase_comp', 'conc_mass_comp', 'conc_mol_phase_comp', 'conc_mol_comp', 'mass_frac_phase_comp', 'mass_frac_comp', 'mole_frac_phase_comp', 'mole_frac_comp')

def format_index(index: Any) -> str:
    """Serialize an indexed component key without Python tuple syntax noise."""
    if isinstance(index, tuple):
        return ','.join((str(item) for item in index))
    return str(index)

def safe_float(value_obj: Any) -> float | None:
    """Convert Pyomo values and bounds to JSON-safe floats."""
    try:
        from pyomo.environ import value
        resolved = value(value_obj, exception=False)
    except Exception:
        resolved = None
    if resolved is None:
        return None
    try:
        return float(resolved)
    except (TypeError, ValueError):
        return None

def safe_units(component: Any) -> str | None:
    """Serialize Pyomo units when available."""
    try:
        from pyomo.environ import units as pyunits
        units = pyunits.get_units(component)
    except Exception:
        units = None
    return str(units) if units is not None else None

def iter_component_data(component: Any) -> Iterable[Any]:
    """Yield scalar data entries from a Var-like component."""
    if hasattr(component, 'is_indexed') and component.is_indexed():
        for index in component:
            yield component[index]
    else:
        yield component

def port_vars(port: Any) -> Mapping[str, Any]:
    """Return the Port member map if available."""
    vars_map = getattr(port, 'vars', None)
    if isinstance(vars_map, Mapping):
        return vars_map
    return {}

def port_member(port: Any, name: str) -> Any | None:
    """Return a named Port member from ``vars`` or attribute access."""
    vars_map = port_vars(port)
    if name in vars_map:
        return vars_map[name]
    return getattr(port, name, None)

def _phase_list_from_object(obj: Any) -> tuple[str, ...] | None:
    phase_list = getattr(obj, 'phase_list', None)
    if phase_list is None:
        return None
    try:
        return tuple((str(item) for item in phase_list))
    except TypeError:
        return None

def detect_phase_list(port: Any) -> tuple[str, ...]:
    """Best-effort discovery of the connected property package phase list."""
    candidates: list[Any] = [port]
    for component in port_vars(port).values():
        candidates.append(component)
        parent = getattr(component, 'parent_block', lambda: None)()
        if parent is not None:
            candidates.append(parent)
        for data in iter_component_data(component):
            data_parent = getattr(data, 'parent_block', lambda: None)()
            if data_parent is not None:
                candidates.append(data_parent)
    for candidate in candidates:
        for obj in _candidate_property_objects(candidate):
            phases = _phase_list_from_object(obj)
            if phases:
                return phases
    return ()

def _candidate_property_objects(obj: Any) -> Iterable[Any]:
    yield obj
    params = getattr(obj, 'params', None)
    if params is not None:
        yield params
    config = getattr(obj, 'config', None)
    if config is not None:
        yield config
        parameters = getattr(config, 'parameters', None)
        if parameters is not None:
            yield parameters

def detect_flow_state_var(port: Any) -> str | None:
    """Choose the correct mass-flow state variable for one stream port.

    Phase-indexed packages expose ``flow_mass_phase_comp``. Zero-order packages
    expose no-phase ``flow_mass_comp``. The variable presence is authoritative;
    ``phase_list`` is used to disambiguate rare ports exposing both.
    """
    phase_flow = port_member(port, 'flow_mass_phase_comp')
    no_phase_flow = port_member(port, 'flow_mass_comp')
    if phase_flow is not None and no_phase_flow is None:
        return 'flow_mass_phase_comp'
    if no_phase_flow is not None and phase_flow is None:
        return 'flow_mass_comp'
    if phase_flow is not None and no_phase_flow is not None:
        phases = set(detect_phase_list(port))
        if phases and phases != {'Liq'}:
            return 'flow_mass_phase_comp'
        return 'flow_mass_comp'
    return None

def _component_values(component: Any) -> Any:
    if hasattr(component, 'is_indexed') and component.is_indexed():
        values: dict[str, float | None] = {}
        for index in component:
            values[format_index(index)] = safe_float(component[index])
        return values
    return safe_float(component)

def extract_stream_state(port: Any) -> dict[str, Any]:
    """Extract JSON-native stream values from one Port."""
    stream_data: dict[str, Any] = {}
    flow_name = detect_flow_state_var(port)
    state_vars = [flow_name] if flow_name else []
    state_vars.extend((name for name in VALUE_STATE_VARS if name != flow_name))
    seen: set[str] = set()
    for var_name in state_vars:
        if not var_name or var_name in seen:
            continue
        seen.add(var_name)
        component = port_member(port, var_name)
        if component is None:
            continue
        try:
            values = _component_values(component)
        except Exception:
            continue
        if values not in ({}, None):
            stream_data[var_name] = values
    return stream_data

def extract_unit_streams(unit_block: Any, *, port_names: Iterable[str]=COMMON_PORT_NAMES) -> dict[str, dict[str, Any]]:
    """Extract stream states for all known ports on one unit block."""
    unit_streams: dict[str, dict[str, Any]] = {}
    for port_name in port_names:
        port = getattr(unit_block, port_name, None)
        if port is None:
            continue
        stream_data = extract_stream_state(port)
        if stream_data:
            unit_streams[port_name] = stream_data
    return unit_streams

def extract_flowsheet_streams(units: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract nested stream states as ``unit_id -> port_name -> state``."""
    streams: dict[str, dict[str, Any]] = {}
    for unit_id, unit_block in units.items():
        unit_streams = extract_unit_streams(unit_block)
        if unit_streams:
            streams[unit_id] = unit_streams
    return streams

def extract_flat_streams(units: Mapping[str, Any], *, stream_filter: Iterable[str] | None=None) -> dict[str, dict[str, Any]]:
    """Extract stream states keyed as ``unit_id.port_name``."""
    requested = set(stream_filter or [])
    flat: dict[str, dict[str, Any]] = {}
    for unit_id, unit_streams in extract_flowsheet_streams(units).items():
        for port_name, stream_data in unit_streams.items():
            stream_key = f'{unit_id}.{port_name}'
            if requested and stream_key not in requested:
                continue
            flat[stream_key] = stream_data
    return flat

def component_snapshot(component_data: Any) -> dict[str, Any]:
    """Return a JSON-native snapshot for a solved Pyomo component."""
    payload = {'value': safe_float(component_data), 'units': safe_units(component_data)}
    if hasattr(component_data, 'fixed'):
        payload['fixed'] = bool(getattr(component_data, 'fixed', False))
    if hasattr(component_data, 'lb'):
        payload['lb'] = safe_float(getattr(component_data, 'lb', None))
    if hasattr(component_data, 'ub'):
        payload['ub'] = safe_float(getattr(component_data, 'ub', None))
    if hasattr(component_data, 'stale'):
        payload['stale'] = bool(getattr(component_data, 'stale', False))
    return payload

def extract_port_snapshot(port: Any) -> dict[str, Any]:
    """Export all solved variables visible on a Port."""
    port_data: dict[str, Any] = {}
    for member_name, component in port_vars(port).items():
        if hasattr(component, 'is_indexed') and component.is_indexed():
            for component_data in iter_component_data(component):
                key = f'{member_name}[{format_index(component_data.index())}]'
                port_data[key] = component_snapshot(component_data)
        else:
            port_data[member_name] = component_snapshot(component)
    return port_data

def filter_solved_state_streams(solved_state: Mapping[str, Any], stream_filter: Iterable[str] | None=None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Filter persisted solved-state streams and matching stream roles."""
    requested = set(stream_filter or [])
    streams = solved_state.get('streams', {})
    roles = solved_state.get('stream_roles', {})
    if not isinstance(streams, Mapping):
        streams = {}
    if not isinstance(roles, Mapping):
        roles = {}
    filtered_streams = {stream_key: data for stream_key, data in streams.items() if not requested or stream_key in requested}
    filtered_roles = {stream_key: role for stream_key, role in roles.items() if not requested or stream_key in requested}
    return (filtered_streams, filtered_roles)

def normalize_job_result_streams(result: Any) -> Any:
    """Normalize stream maps inside a job result without changing its shape."""
    if not isinstance(result, Mapping):
        return result
    payload = deepcopy(dict(result))
    solved_state = payload.get('solved_state')
    if isinstance(solved_state, Mapping):
        solved_payload = dict(solved_state)
        streams, roles = filter_solved_state_streams(solved_payload)
        solved_payload['streams'] = streams
        solved_payload['stream_roles'] = roles
        payload['solved_state'] = solved_payload
    return payload
__all__ = ['COMMON_PORT_NAMES', 'VALUE_STATE_VARS', 'component_snapshot', 'detect_flow_state_var', 'detect_phase_list', 'extract_flat_streams', 'extract_flowsheet_streams', 'extract_port_snapshot', 'extract_stream_state', 'extract_unit_streams', 'filter_solved_state_streams', 'format_index', 'iter_component_data', 'normalize_job_result_streams', 'port_member', 'safe_float', 'safe_units']
