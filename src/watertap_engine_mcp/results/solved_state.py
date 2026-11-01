"""Solved-state and process-diagnostics extraction for WaterTAP flowsheets.

Canonical extractors for the digital-twin round-trip. ``_extract_solved_state``
exports the solved variable/stream/state-block snapshot for every built unit, and
``_extract_process_diagnostics`` exports the kinetic/inhibition/numerical
diagnostics layered on top of it. Both the async worker path (``worker.py``) and
the synchronous ``solve_flowsheet`` path (``actions.py``) import these so the
solved state + diagnostics persist into ``session.results`` identically.

Pyomo / WaterTAP / ``core.unit_registry`` imports are kept function-local (as they
were in ``worker.py``) so this module is import-cheap and free of solver-side
import cycles.
"""
from __future__ import annotations

def _format_index(index) -> str:
    """Serialize an indexed component key without Python tuple syntax noise."""
    from watertap_engine_mcp.results.streams import format_index
    return format_index(index)

def _safe_float(value_obj) -> float | None:
    """Convert Pyomo values and bounds to JSON-safe floats."""
    from watertap_engine_mcp.results.streams import safe_float
    return safe_float(value_obj)

def _safe_units(component) -> str | None:
    """Serialize Pyomo units when available."""
    from watertap_engine_mcp.results.streams import safe_units
    return safe_units(component)

def _relative_name(full_name: str, prefix: str) -> str:
    """Strip the parent component prefix from a Pyomo component name."""
    needle = f'{prefix}.'
    if full_name.startswith(needle):
        return full_name[len(needle):]
    return full_name

def _var_snapshot(var_data) -> dict:
    """Return a JSON-native snapshot for one solved variable."""
    return {'value': _safe_float(var_data), 'fixed': bool(getattr(var_data, 'fixed', False)), 'lb': _safe_float(getattr(var_data, 'lb', None)), 'ub': _safe_float(getattr(var_data, 'ub', None)), 'units': _safe_units(var_data), 'stale': bool(getattr(var_data, 'stale', False))}

def _iter_var_data(component):
    """Yield scalar VarData entries from a Var-like component."""
    from watertap_engine_mcp.results.streams import iter_component_data
    yield from iter_component_data(component)

def _extract_port_snapshot(port) -> dict:
    """Export the solved variables visible on a Port."""
    from watertap_engine_mcp.results.streams import extract_port_snapshot
    return extract_port_snapshot(port)

def _extract_state_block_snapshot(unit_block) -> dict:
    """Export state-block variables grouped by state block path."""
    from pyomo.core.base.block import Block
    from pyomo.core.base.var import Var
    snapshots = {}
    for block in unit_block.component_objects(Block, descend_into=True):
        block_name = getattr(block, 'local_name', '')
        if not any((token in block_name for token in ('properties', 'state'))):
            continue
        if hasattr(block, 'is_indexed') and block.is_indexed():
            block_items = ((f'{block_name}[{_format_index(index)}]', block[index]) for index in block)
        else:
            block_items = ((block_name, block),)
        for export_name, block_data in block_items:
            vars_payload = {}
            for component in block_data.component_objects(Var, descend_into=False):
                for var_data in _iter_var_data(component):
                    key = _relative_name(var_data.name, block_data.name)
                    vars_payload[key] = _var_snapshot(var_data)
            if vars_payload:
                snapshots[export_name] = vars_payload
    return snapshots

def _extract_solved_state(units: dict, unit_instances: dict, *, model=None) -> dict:
    """Export the solved variable state for all built units."""
    from pyomo.core.base.var import Var
    from pyomo.network import Port
    from watertap_engine_mcp.core.unit_registry import get_unit_spec
    solved_state = {'units': {}, 'streams': {}, 'stream_roles': {}}
    for unit_id, unit_block in units.items():
        unit_inst = unit_instances.get(unit_id)
        if unit_inst is not None:
            unit_type = unit_inst.unit_type
            spec = get_unit_spec(unit_type)
            port_roles = dict(spec.port_roles)
        else:
            unit_type = 'Translator'
            spec = None
            port_roles = {}
        unit_payload = {'unit_type': unit_type, 'variables': {}, 'streams': {}, 'state_blocks': {}, 'port_roles': port_roles}
        for var_data in unit_block.component_data_objects(Var, descend_into=True):
            relative = _relative_name(var_data.name, unit_block.name)
            unit_payload['variables'][relative] = _var_snapshot(var_data)
        for port_component in unit_block.component_objects(Port, descend_into=False):
            if hasattr(port_component, 'is_indexed') and port_component.is_indexed():
                port_items = ((f'{port_component.local_name}[{_format_index(index)}]', port_component[index]) for index in port_component)
            else:
                port_items = ((port_component.local_name, port_component),)
            for port_name, port in port_items:
                port_payload = _extract_port_snapshot(port)
                if port_payload:
                    unit_payload['streams'][port_name] = port_payload
                    solved_state['streams'][f'{unit_id}.{port_name}'] = port_payload
                    solved_state['stream_roles'][f'{unit_id}.{port_name}'] = port_roles.get(port_name)
        unit_payload['state_blocks'] = _extract_state_block_snapshot(unit_block)
        solved_state['units'][unit_id] = unit_payload
    if model is not None:
        from watertap_engine_mcp.results.model_state import capture_model_state
        try:
            solved_state['full_model_state'] = capture_model_state(model)
        except Exception as exc:
            solved_state['full_model_state'] = {'schema_version': 'watertap_full_model_state_error/v1', 'error': str(exc), 'governing_eligible': False}
        from watertap_engine_mcp.results.equation_workbook import retain_requested_model
        retain_requested_model(model, state=solved_state['full_model_state'])
    return solved_state

def _bound_activity(var_data, *, tolerance: float=1e-07) -> dict:
    value = _safe_float(var_data)
    lb = _safe_float(getattr(var_data, 'lb', None))
    ub = _safe_float(getattr(var_data, 'ub', None))

    def active(bound):
        if value is None or bound is None:
            return False
        tol = max(tolerance, tolerance * max(abs(bound), 1.0))
        return abs(value - bound) <= tol
    return {'value': value, 'lb': lb, 'ub': ub, 'lower_active': active(lb), 'upper_active': active(ub), 'fixed': bool(getattr(var_data, 'fixed', False)), 'units': _safe_units(var_data)}

def _extract_actuator_bound_activity(units: dict) -> dict:
    """Report bound activity for target-driven manipulated variables."""
    actuators = {}

    def add(unit_id: str, actuator: str, var_data) -> None:
        if var_data is None:
            return
        payload = _bound_activity(var_data)
        payload['unit_id'] = unit_id
        payload['actuator'] = actuator
        payload['variable'] = getattr(var_data, 'name', f'{unit_id}.{actuator}')
        actuators[f'{unit_id}.{actuator}'] = payload
    for unit_id, unit_block in units.items():
        kla = getattr(unit_block, 'KLa', None)
        if kla is not None:
            add(unit_id, 'KLa', kla)
        for attr_name, actuator_name in (('metal_dose', 'metal_dose'), ('caustic_dose', 'caustic_dose')):
            component = getattr(unit_block, attr_name, None)
            if component is None:
                continue
            try:
                var_data = component[0]
            except Exception:
                try:
                    var_data = component[0.0]
                except Exception:
                    var_data = component if not getattr(component, 'is_indexed', lambda: True)() else None
            add(unit_id, actuator_name, var_data)
        split_fraction = getattr(unit_block, 'split_fraction', None)
        if split_fraction is None:
            continue
        try:
            indexes = list(split_fraction)
        except Exception:
            indexes = []
        for index in indexes:
            if isinstance(index, tuple):
                label = str(index[-1])
            else:
                label = str(index)
            if 'waste' not in label.lower() and 'was' not in label.lower():
                continue
            try:
                add(unit_id, f'split_fraction[{_format_index(index)}]', split_fraction[index])
            except Exception:
                continue
    return actuators

def _component_snapshot(component_data) -> dict:
    """Return a JSON-native snapshot for a solved Pyomo Var/Expression/Param."""
    payload = {'value': _safe_float(component_data), 'units': _safe_units(component_data)}
    if hasattr(component_data, 'fixed'):
        payload['fixed'] = bool(getattr(component_data, 'fixed', False))
    if hasattr(component_data, 'lb'):
        payload['lb'] = _safe_float(getattr(component_data, 'lb', None))
    if hasattr(component_data, 'ub'):
        payload['ub'] = _safe_float(getattr(component_data, 'ub', None))
    if hasattr(component_data, 'mutable'):
        payload['mutable'] = bool(getattr(component_data, 'mutable', False))
    return payload

def _iter_component_data(component):
    """Yield scalar component data from Pyomo Var/Expression/Param components."""
    if hasattr(component, 'is_indexed') and component.is_indexed():
        for index in component:
            yield component[index]
    else:
        yield component
_DIAGNOSTIC_TOPIC_TOKENS = {'biological_rates': ('rate', 'reaction', 'reaction_rate', 'rxn', 'rho', 'generation', 'decay', 'growth', 'hydrolysis', 'uptake', 'methan', 'acidogen', 'acetogen', 'nitrif', 'denitrif', 'ferment'), 'biological_inhibition': ('inhib', 'inhibition', 'i_', 's_h2', 's_nh3', 's_nh4', 's_o2', 's_no2', 's_no3', 'vfa', 'alk'), 'kinetic_parameters': ('mu_', 'k_', 'kmax', 'ks_', 'ki_', 'b_', 'y_', 'yield', 'stoich', 'half', 'saturation'), 'membrane': ('recovery', 'rejection', 'flux', 'permeability', 'a_comp', 'b_comp', 'cp_modulus', 'area', 'membrane', 'pressure', 'delta'), 'thermal': ('heat', 'steam', 'temperature', 'evapor', 'crystal', 'work', 'compress', 'enthalpy', 'vapor', 'condens', 'supersat'), 'ion_exchange': ('breakthrough', 'regeneration', 'resin', 'loading', 'leakage', 'bed', 'capacity', 'ix'), 'electrochemical': ('current', 'voltage', 'electron', 'electro', 'power', 'efficiency', 'overpotential', 'transport_number'), 'hydraulic': ('flow', 'pressure', 'head', 'deltaP', 'pump', 'efficiency', 'velocity'), 'separation': ('split', 'removal', 'separation', 'treated', 'byproduct', 'product', 'brine', 'solids')}

def _diagnostic_topics(name: str, unit_type: str) -> list[str]:
    """Classify a model component into human-reporting diagnostic topics."""
    haystack = f'{unit_type}.{name}'.lower()
    topics = [topic for topic, tokens in _DIAGNOSTIC_TOPIC_TOKENS.items() if any((token.lower() in haystack for token in tokens))]
    return topics or ['general']

def _looks_biological(unit_type: str, package_name: str | None=None) -> bool:
    lower = f'{unit_type} {package_name or ''}'.lower()
    return any((token in lower for token in ('asm', 'adm', 'cstr', 'aeration', 'clarifier', 'mbr', 'anaerobic', 'digester', 'activated')))

def _bio_mode(unit_type: str, package_name: str | None=None) -> str | None:
    lower = f'{unit_type} {package_name or ''}'.lower()
    if any((token in lower for token in ('adm', 'anaerobic', 'digester'))):
        return 'anaerobic'
    if _looks_biological(unit_type, package_name):
        return 'aerobic'
    return None

def _unit_package_name(unit_block) -> str | None:
    try:
        package = getattr(getattr(unit_block, 'config', None), 'property_package', None)
        return type(package).__name__ if package is not None else None
    except Exception:
        return None

def _diagnostic_family(unit_type: str, package_name: str | None=None) -> str:
    lower = f'{unit_type} {package_name or ''}'.lower()
    bio = _bio_mode(unit_type, package_name)
    if bio is not None:
        return f'{bio}_biological'
    if any((token in lower for token in ('reverseosmosis', 'nanofiltration', 'membrane', 'oaro'))):
        return 'membrane'
    if any((token in lower for token in ('evaporator', 'compressor', 'condenser', 'crystallizer', 'heater', 'ejector'))):
        return 'thermal'
    if 'ionexchange' in lower or lower.startswith('ix'):
        return 'ion_exchange'
    if any((token in lower for token in ('electro', 'ozone', 'uv'))):
        return 'electrochemical'
    if any((token in lower for token in ('pump', 'pressure', 'energyrecovery'))):
        return 'hydraulic'
    if any((token in lower for token in ('separation', 'desalter', 'separator', 'clarifier', 'dewatering', 'thickener'))):
        return 'separation'
    return 'general'

def _extract_performance_contents(unit_block) -> dict:
    """Capture a unit's native WaterTAP/IDAES performance contents."""
    if not hasattr(unit_block, '_get_performance_contents'):
        return {}
    try:
        contents = unit_block._get_performance_contents(time_point=0)
    except TypeError:
        try:
            contents = unit_block._get_performance_contents()
        except Exception:
            return {}
    except Exception:
        return {}
    performance = {'vars': {}, 'exprs': {}}
    for section in ('vars', 'exprs'):
        raw = contents.get(section, {}) if isinstance(contents, dict) else {}
        if not isinstance(raw, dict):
            continue
        for label, component in raw.items():
            try:
                performance[section][str(label)] = _component_snapshot(component)
            except Exception:
                continue
    return {key: value for key, value in performance.items() if value}

def _capture_unit_report(unit_block) -> str | None:
    """Capture the native unit report text for offline human review."""
    if not hasattr(unit_block, 'report'):
        return None
    try:
        from contextlib import redirect_stdout
        from io import StringIO
        buffer = StringIO()
        with redirect_stdout(buffer):
            unit_block.report()
        text = buffer.getvalue().strip()
        return text or None
    except Exception as exc:
        return f'unit report unavailable: {type(exc).__name__}: {exc}'

def _extract_component_topic_snapshots(unit_block, unit_type: str) -> dict:
    """Extract diagnostic variables, expressions, and params grouped by topic."""
    from pyomo.core.base.expression import Expression
    from pyomo.core.base.param import Param
    from pyomo.core.base.var import Var
    grouped: dict[str, dict[str, dict]] = {'variables': {}, 'expressions': {}, 'parameters': {}}
    component_classes = (('variables', Var), ('expressions', Expression), ('parameters', Param))
    for section, component_class in component_classes:
        for component in unit_block.component_objects(component_class, descend_into=True):
            for component_data in _iter_component_data(component):
                try:
                    relative = _relative_name(component_data.name, unit_block.name)
                except Exception:
                    relative = str(getattr(component_data, 'name', 'unknown'))
                topics = _diagnostic_topics(relative, unit_type)
                snapshot = _component_snapshot(component_data)
                for topic in topics:
                    grouped[section].setdefault(topic, {})[relative] = snapshot
    return grouped

def _extract_state_indicators(unit_payload: dict, mode: str | None) -> dict:
    """Extract biological state indicators from stream/state snapshots."""
    if mode is None:
        return {}
    if mode == 'anaerobic':
        markers = ('S_su', 'S_aa', 'S_fa', 'S_va', 'S_bu', 'S_pro', 'S_ac', 'S_h2', 'S_ch4', 'S_IC', 'S_IN', 'S_nh3', 'S_NH4', 'pH')
    else:
        markers = ('S_O2', 'S_NH4', 'S_NO2', 'S_NO3', 'S_PO4', 'S_ALK', 'X_H', 'X_AUT', 'X_PAO', 'X_TSS', 'pH')
    indicators: dict[str, dict] = {}
    for section_name in ('streams', 'state_blocks'):
        section = unit_payload.get(section_name)
        if not isinstance(section, dict):
            continue
        for block_name, values in section.items():
            if not isinstance(values, dict):
                continue
            selected = {key: value for key, value in values.items() if any((marker.lower() in key.lower() for marker in markers))}
            if selected:
                indicators[f'{section_name}.{block_name}'] = selected
    return indicators
_MADM1_PROCESS_MAP = [('R1', 'Carbohydrate Hydrolysis', 'hydrolysis', None, None, None, {}), ('R2', 'Protein Hydrolysis', 'hydrolysis', None, None, None, {}), ('R3', 'Lipid Hydrolysis', 'hydrolysis', None, None, None, {}), ('R4', 'Sugar Uptake', 'uptake', 'S_su', 'K_S_su', None, {'pH': 'I_pH_aa', 'n_limitation': 'I_IN_lim', 'p_limitation': 'I_IP_lim'}), ('R5', 'Amino Acid Uptake', 'uptake', 'S_aa', 'K_S_aa', None, {'pH': 'I_pH_aa', 'n_limitation': 'I_IN_lim', 'p_limitation': 'I_IP_lim'}), ('R6', 'LCFA Uptake', 'uptake', 'S_fa', 'K_S_fa', None, {'pH': 'I_pH_aa', 'n_limitation': 'I_IN_lim', 'p_limitation': 'I_IP_lim', 'h2': 'I_h2_fa'}), ('R7', 'Valerate Uptake', 'uptake', 'S_va', 'K_S_c4', ('S_va', 'S_bu'), {'pH': 'I_pH_aa', 'n_limitation': 'I_IN_lim', 'p_limitation': 'I_IP_lim', 'h2': 'I_h2_c4', 'h2s': 'I_h2s_c4'}), ('R8', 'Butyrate Uptake', 'uptake', 'S_bu', 'K_S_c4', ('S_bu', 'S_va'), {'pH': 'I_pH_aa', 'n_limitation': 'I_IN_lim', 'p_limitation': 'I_IP_lim', 'h2': 'I_h2_c4', 'h2s': 'I_h2s_c4'}), ('R9', 'Propionate Uptake', 'uptake', 'S_pro', 'K_S_pro', None, {'pH': 'I_pH_aa', 'n_limitation': 'I_IN_lim', 'p_limitation': 'I_IP_lim', 'h2': 'I_h2_pro', 'h2s': 'I_h2s_pro'}), ('R10', 'Acetate Uptake', 'uptake', 'S_ac', 'K_S_ac', None, {'pH': 'I_pH_ac', 'n_limitation': 'I_IN_lim', 'p_limitation': 'I_IP_lim', 'nh3': 'I_nh3', 'h2s': 'I_h2s_ac'}), ('R11', 'Hydrogen Uptake', 'uptake', 'S_h2', 'K_S_h2', None, {'pH': 'I_pH_h2', 'n_limitation': 'I_IN_lim', 'p_limitation': 'I_IP_lim', 'h2s': 'I_h2s_h2'}), ('R12', 'Sugar Degrader Decay', 'decay', None, None, None, {}), ('R13', 'Amino Acid Degrader Decay', 'decay', None, None, None, {}), ('R14', 'LCFA Degrader Decay', 'decay', None, None, None, {}), ('R15', 'Valerate/Butyrate Degrader Decay', 'decay', None, None, None, {}), ('R16', 'Propionate Degrader Decay', 'decay', None, None, None, {}), ('R17', 'Acetate Degrader Decay', 'decay', None, None, None, {}), ('R18', 'Hydrogen Degrader Decay', 'decay', None, None, None, {}), ('R19', 'PAO Valerate Storage', 'storage', 'S_va', 'K_A', ('S_va', 'S_bu', 'S_pro', 'S_ac'), {}), ('R20', 'PAO Butyrate Storage', 'storage', 'S_bu', 'K_A', ('S_bu', 'S_va', 'S_pro', 'S_ac'), {}), ('R21', 'PAO Propionate Storage', 'storage', 'S_pro', 'K_A', ('S_pro', 'S_va', 'S_bu', 'S_ac'), {}), ('R22', 'PAO Acetate Storage', 'storage', 'S_ac', 'K_A', ('S_ac', 'S_va', 'S_bu', 'S_pro'), {}), ('R23', 'PAO Lysis', 'lysis', None, None, None, {}), ('R24', 'PP Lysis', 'lysis', None, None, None, {}), ('R25', 'PHA Lysis', 'lysis', None, None, None, {})]
_ADM1_PROCESS_MAP = [('R1', 'Disintegration', 'disintegration', None, None, None, {}), ('R2', 'Carbohydrate Hydrolysis', 'hydrolysis', None, None, None, {}), ('R3', 'Protein Hydrolysis', 'hydrolysis', None, None, None, {}), ('R4', 'Lipid Hydrolysis', 'hydrolysis', None, None, None, {}), ('R5', 'Sugar Uptake', 'uptake', 'S_su', 'K_S_su', None, {'pH': 'I_pH_aa', 'n_limitation': 'I_IN_lim'}), ('R6', 'Amino Acid Uptake', 'uptake', 'S_aa', 'K_S_aa', None, {'pH': 'I_pH_aa', 'n_limitation': 'I_IN_lim'}), ('R7', 'LCFA Uptake', 'uptake', 'S_fa', 'K_S_fa', None, {'pH': 'I_pH_aa', 'n_limitation': 'I_IN_lim', 'h2': 'I_h2_fa'}), ('R8', 'Valerate Uptake', 'uptake', 'S_va', 'K_S_c4', ('S_va', 'S_bu'), {'pH': 'I_pH_aa', 'n_limitation': 'I_IN_lim', 'h2': 'I_h2_c4'}), ('R9', 'Butyrate Uptake', 'uptake', 'S_bu', 'K_S_c4', ('S_bu', 'S_va'), {'pH': 'I_pH_aa', 'n_limitation': 'I_IN_lim', 'h2': 'I_h2_c4'}), ('R10', 'Propionate Uptake', 'uptake', 'S_pro', 'K_S_pro', None, {'pH': 'I_pH_aa', 'n_limitation': 'I_IN_lim', 'h2': 'I_h2_pro'}), ('R11', 'Acetate Uptake', 'uptake', 'S_ac', 'K_S_ac', None, {'pH': 'I_pH_ac', 'n_limitation': 'I_IN_lim', 'nh3': 'I_nh3'}), ('R12', 'Hydrogen Uptake', 'uptake', 'S_h2', 'K_S_h2', None, {'pH': 'I_pH_h2', 'n_limitation': 'I_IN_lim'}), ('R13', 'Sugar Degrader Decay', 'decay', None, None, None, {}), ('R14', 'Amino Acid Degrader Decay', 'decay', None, None, None, {}), ('R15', 'LCFA Degrader Decay', 'decay', None, None, None, {}), ('R16', 'Valerate/Butyrate Degrader Decay', 'decay', None, None, None, {}), ('R17', 'Propionate Degrader Decay', 'decay', None, None, None, {}), ('R18', 'Acetate Degrader Decay', 'decay', None, None, None, {}), ('R19', 'Hydrogen Degrader Decay', 'decay', None, None, None, {})]
_ADM_ACETATE_RXN = {'madm1': 'R10', 'adm1': 'R11'}

def _reaction_block_classes():
    """Return ``[(family, ReactionBlockDataClass), ...]`` for type-based detection.

    Detection keys off ``isinstance`` against the watertap reaction block-data
    classes (a robust type check, NOT a name heuristic). Modified families are
    listed first defensively; the watertap classes are in fact siblings (none
    subclasses another), so ordering is not load-bearing.
    """
    try:
        from watertap.property_models.unit_specific.anaerobic_digestion.modified_adm1_reactions import ModifiedADM1ReactionBlockData
        from watertap.property_models.unit_specific.anaerobic_digestion.adm1_reactions import ADM1ReactionBlockData
        from watertap.property_models.unit_specific.activated_sludge.modified_asm2d_reactions import ModifiedASM2dReactionBlockData
        from watertap.property_models.unit_specific.activated_sludge.asm2d_reactions import ASM2dReactionBlockData
    except Exception:
        return []
    return [('madm1', ModifiedADM1ReactionBlockData), ('adm1', ADM1ReactionBlockData), ('masm2d', ModifiedASM2dReactionBlockData), ('asm2d', ASM2dReactionBlockData)]

def _detect_reaction_block(unit_block):
    """Return ``(family, reaction_block_data)`` for a biological unit, else ``(None, None)``.

    Fast path probes the known control-volume locations (AD digesters carry
    ``liquid_phase.reactions``; CSTR/AerationTank carry ``control_volume.reactions``);
    the general scan is a type-based fallback over every sub-block. The family
    decision is always ``isinstance`` on the reaction block class.
    """
    classes = _reaction_block_classes()
    if not classes:
        return (None, None)
    for cv_name in ('liquid_phase', 'control_volume'):
        cv = getattr(unit_block, cv_name, None)
        reactions = getattr(cv, 'reactions', None) if cv is not None else None
        if reactions is None:
            continue
        try:
            for t in reactions:
                blk = reactions[t]
                for family, cls in classes:
                    if isinstance(blk, cls):
                        return (family, blk)
                break
        except Exception:
            continue
    try:
        from pyomo.core.base.block import Block
        for blk in unit_block.component_data_objects(Block, descend_into=True):
            for family, cls in classes:
                if isinstance(blk, cls):
                    return (family, blk)
    except Exception:
        return (None, None)
    return (None, None)

def _pyo_value(component) -> float | None:
    """Evaluate a solved Pyomo component to a JSON-safe float (None on failure)."""
    try:
        from pyomo.environ import value as pyo_value
        return float(pyo_value(component))
    except Exception:
        return None

def _factor_to_pct(factor) -> float | None:
    """Convert a [0, 1] activity factor to a [0, 100]% inhibition degree."""
    if factor is None:
        return None
    f = float(factor)
    if f > 1.0:
        f = 1.0
    if f < 0.0:
        f = 0.0
    return round((1.0 - f) * 100.0, 3)

def _pct0(factor) -> float:
    """``_factor_to_pct`` with a None -> 0.0 floor (a missing factor = no inhibition)."""
    pct = _factor_to_pct(factor)
    return pct if pct is not None else 0.0

def _monod(S, K):
    """Saturation factor S/(K+S); None if inputs missing."""
    if S is None or K is None:
        return None
    denom = K + S
    if denom <= 0:
        return 0.0
    return S / denom

def _switch_off(K, S):
    """Inhibitory switch K/(K+S) (off-when-present); None if inputs missing."""
    if S is None or K is None:
        return None
    denom = K + S
    if denom <= 0:
        return 0.0
    return K / denom

def _health_status(max_inhibition_pct: float) -> str:
    """Tier the worst-case inhibition % into a 5-level health label (adm1-mcp tiers)."""
    if max_inhibition_pct < 10:
        return 'Excellent'
    if max_inhibition_pct < 20:
        return 'Good'
    if max_inhibition_pct < 40:
        return 'Fair'
    if max_inhibition_pct < 60:
        return 'Poor'
    return 'Critical'

def _safety_status(min_safety_margin_pct: float) -> str:
    """Tier the minimum substrate-limitation % into a safety label."""
    if min_safety_margin_pct > 60:
        return 'High'
    if min_safety_margin_pct > 30:
        return 'Good'
    if min_safety_margin_pct > 10:
        return 'Moderate'
    return 'Low'

def _substrate_monod(conc, param, sub_key, ks_key, competition):
    """Reconstruct an ADM1 primary-substrate Monod factor (+ va/bu competition).

    Not a named component on the reaction block -- it is inlined in the rate
    expression -- so it is reconstructed from the solved substrate concentration
    and the named ``K_S_*`` saturation coefficient (Strategy-B style, applied to
    the ADM families purely for the QSDsan-parity ``substrate_limitation`` column).
    """
    if sub_key is None or ks_key is None:
        return None
    S = conc(sub_key)
    K = param(ks_key)
    m = _monod(S, K)
    if m is None:
        return None
    if competition:
        total = 0.0
        for key in competition:
            c = conc(key)
            total += c if c is not None else 0.0
        total += 1e-10
        num = conc(competition[0])
        num = num if num is not None else 0.0
        m *= num / total
    return m

def _adm_process_health(rblock, family: str, unit_type: str) -> dict:
    """Strategy A: harvest the named ADM1/mADM1 inhibition + rate components.

    Reads (by name, at the converged optimum) ``reaction_rate[r]``, the aggregate
    ``I[r]`` inhibition Var, and every per-factor Expression (``I_pH_*``,
    ``I_IN_lim``, ``I_IP_lim`` (mADM1), ``I_h2_*``, ``I_h2s_*`` (mADM1),
    ``I_nh3``), then reconstructs the per-reaction substrate Monod for the
    ``substrate_limitation`` column.
    """
    from math import exp
    process_map = _MADM1_PROCESS_MAP if family == 'madm1' else _ADM1_PROCESS_MAP

    def conc(key):
        try:
            return _pyo_value(rblock.conc_mass_comp_ref[key])
        except Exception:
            return None

    def param(key):
        comp = getattr(getattr(rblock, 'params', None), key, None)
        return _pyo_value(comp) if comp is not None else None

    def expr(name):
        comp = getattr(rblock, name, None)
        return _pyo_value(comp) if comp is not None else None

    def rate(rxn):
        try:
            return _pyo_value(rblock.reaction_rate[rxn])
        except Exception:
            return None

    def agg_I(rxn):
        try:
            return _pyo_value(rblock.I[rxn])
        except Exception:
            return None
    pH_value = expr('pH')
    rows: list[dict] = []
    consistency = None
    acetate_rxn = _ADM_ACETATE_RXN[family]
    worst: dict[str, float] = {}

    def _bump(label: str, pct: float | None) -> None:
        if pct is None:
            return
        worst[label] = max(worst.get(label, 0.0), pct)

    def pp_storage_factor():
        """X_PP/(K_PP*X_PAO + X_PP) -- the mADM1 PAO polyphosphate-storage Monod
        applied DIRECTLY in the R19-R22 rate expressions (not via I[r])."""
        X_PP, X_PAO, K_PP = (conc('X_PP'), conc('X_PAO'), param('K_PP'))
        if X_PP is None or X_PAO is None or K_PP is None:
            return None
        return _monod(X_PP, K_PP * X_PAO)
    for rxn, name, category, sub_key, ks_key, competition, fmap in process_map:
        process_rate = rate(rxn)
        if not fmap and sub_key is None:
            rows.append({'process': name, 'reaction_index': rxn, 'category': category, 'process_rate': process_rate, 'pH_inhibition': 0.0, 'h2_inhibition': 0.0, 'h2s_inhibition': 0.0, 'n_limitation': 0.0, 'p_limitation': 0.0, 'nh3_inhibition': 0.0, 'substrate_limitation': 0.0, 'without_substrate': 0.0, 'overall_inhibition': 0.0, 'factors': {}})
            continue
        raw: dict[str, float] = {}
        for ftype, exprname in fmap.items():
            value = expr(exprname)
            if value is None:
                continue
            if ftype == 'pH':
                value = exp(value)
            raw[ftype] = value
        substrate = _substrate_monod(conc, param, sub_key, ks_key, competition)
        pp_storage = pp_storage_factor() if category == 'storage' else None
        I_aggr = agg_I(rxn)
        h2s = raw.get('h2s')
        without_sub = (I_aggr if I_aggr is not None else 1.0) * (h2s if h2s is not None else 1.0)
        overall = without_sub * (substrate if substrate is not None else 1.0) * (pp_storage if pp_storage is not None else 1.0)
        row = {'process': name, 'reaction_index': rxn, 'category': category, 'process_rate': process_rate, 'pH_inhibition': _pct0(raw.get('pH')), 'h2_inhibition': _pct0(raw.get('h2')), 'h2s_inhibition': _pct0(raw.get('h2s')), 'n_limitation': _pct0(raw.get('n_limitation')), 'p_limitation': _pct0(raw.get('p_limitation')), 'nh3_inhibition': _pct0(raw.get('nh3')), 'substrate_limitation': _pct0(substrate), 'without_substrate': _pct0(without_sub), 'overall_inhibition': _pct0(overall), 'factors': {**raw, 'substrate': substrate, **({'pp_storage': pp_storage} if pp_storage is not None else {}), 'aggregate_I': I_aggr, 'overall': overall}}
        rows.append(row)
        _bump('pH Inhibition', row['pH_inhibition'])
        _bump('H2 Inhibition', row['h2_inhibition'])
        _bump('N Limitation', row['n_limitation'])
        if family == 'madm1':
            _bump('H2S Inhibition', row['h2s_inhibition'])
            _bump('P Limitation', row['p_limitation'])
        if 'nh3' in raw:
            _bump('NH3 Inhibition', row['nh3_inhibition'])
        if rxn == acetate_rxn:
            product = 1.0
            for ftype, exprname in fmap.items():
                if ftype == 'h2s':
                    continue
                value = expr(exprname)
                if value is None:
                    continue
                if ftype == 'pH':
                    value = exp(value)
                product *= value
            if I_aggr is not None:
                rel_error = abs(I_aggr - product) / max(abs(I_aggr), 1e-12)
                consistency = {'reaction': rxn, 'process': name, 'aggregate_I': I_aggr, 'reconstructed_product': product, 'rel_error': rel_error, 'consistent': rel_error < 0.0001}
    uptake_rows = [r for r in rows if r['category'] == 'uptake']
    without_sub_pcts = [r['without_substrate'] for r in uptake_rows]
    max_inhibition = max(without_sub_pcts) if without_sub_pcts else 0.0
    limiting_process = ''
    if uptake_rows:
        limiting_process = max(uptake_rows, key=lambda r: r['without_substrate'])['process']
    margins = [r['substrate_limitation'] for r in uptake_rows]
    min_margin = min(margins) if margins else 0.0
    factors = [{'type': label, 'value': pct} for label, pct in worst.items()]
    factors.sort(key=lambda x: x['value'], reverse=True)
    return {'success': True, 'engine': family, 'unit_type': unit_type, 'strategy': 'harvest_named_components', 'convention': 'factor in [0,1] (1=no inhibition); *_inhibition columns = 100*(1-factor)', 'current_pH': round(pH_value, 3) if pH_value is not None else None, 'process_inhibition': rows, 'inhibition_factors': factors, 'health_assessment': {'status': _health_status(max_inhibition), 'max_inhibition': round(max_inhibition, 2), 'limiting_process': limiting_process}, 'safety_assessment': {'status': _safety_status(min_margin), 'min_safety_margin': round(min_margin, 2), 'limiting_process': 'substrate_Monod'}, 'i_consistency_check': consistency}
_MASM2D_PROCESS_META = {'R1': ('Aerobic Hydrolysis', 'hydrolysis'), 'R2': ('Anoxic Hydrolysis', 'hydrolysis'), 'R3': ('Anaerobic Hydrolysis', 'hydrolysis'), 'R4': ('Aerobic Heterotroph Growth on S_F', 'growth'), 'R5': ('Aerobic Heterotroph Growth on S_A', 'growth'), 'R6': ('Anoxic Heterotroph Growth on S_F (denit)', 'growth'), 'R7': ('Anoxic Heterotroph Growth on S_A (denit)', 'growth'), 'R8': ('Fermentation', 'growth'), 'R9': ('Heterotroph Lysis', 'lysis'), 'R10': ('PAO PHA Storage', 'storage'), 'R11': ('Aerobic PP Storage', 'storage'), 'R12': ('Anoxic PP Storage', 'storage'), 'R13': ('Aerobic PAO Growth', 'growth'), 'R14': ('Anoxic PAO Growth', 'growth'), 'R15': ('PAO Lysis', 'lysis'), 'R16': ('PP Lysis', 'lysis'), 'R17': ('PHA Lysis', 'lysis'), 'R18': ('Autotroph Aerobic Growth (Nitrification)', 'growth'), 'R19': ('Autotroph Lysis', 'lysis')}

def _masm2d_process_health(rblock, unit_type: str) -> dict:
    """Strategy B: reconstruct the inlined mASM2d Monod/switching factors.

    ``reaction_rate[r]`` IS a named Var (harvested directly), but mASM2d has ZERO
    named inhibition Expressions -- every Monod / off-switch / nutrient term is
    inlined inside the single ``rate_expression`` constraint. We replay each
    factor from the solved component concentrations (``conc_mass_comp_ref``) and
    the named saturation-coefficient params, bit-for-bit with ``_asm2d.py``.
    """

    def conc(key):
        try:
            v = _pyo_value(rblock.conc_mass_comp_ref[key])
            return v if v is not None else 0.0
        except Exception:
            return 0.0

    def param(key):
        comp = getattr(getattr(rblock, 'params', None), key, None)
        return _pyo_value(comp) if comp is not None else None

    def rate(rxn):
        try:
            return _pyo_value(rblock.reaction_rate[rxn])
        except Exception:
            return None
    decay_switch = bool(getattr(getattr(getattr(rblock, 'params', None), 'config', None), 'decay_switch', True))
    S_O2, S_F, S_A = (conc('S_O2'), conc('S_F'), conc('S_A'))
    S_NH4, S_NO3, S_PO4 = (conc('S_NH4'), conc('S_NO3'), conc('S_PO4'))
    X_S, X_H, X_PAO = (conc('X_S'), conc('X_H'), conc('X_PAO'))
    X_PP, X_PHA, X_AUT = (conc('X_PP'), conc('X_PHA'), conc('X_AUT'))
    KL_O2, KL_NO3, KL_X = (param('KL_O2'), param('KL_NO3'), param('KL_X'))
    KH_O2, KH_NO3, KH_NH4, KH_PO4 = (param('KH_O2'), param('KH_NO3'), param('KH_NH4'), param('KH_PO4'))
    K_F, KH_A, K_fe = (param('K_F'), param('KH_A'), param('K_fe'))
    KP_O2, KP_NO3, KP_NH4, KP_PO4 = (param('KP_O2'), param('KP_NO3'), param('KP_NH4'), param('KP_PO4'))
    KP_A, KP_P, KP_PP, KP_PHA = (param('KP_A'), param('KP_P'), param('KP_PP'), param('KP_PHA'))
    K_MAX, KI_PP = (param('K_MAX'), param('KI_PP'))
    KA_O2, KA_NO3, KA_NH4, KA_PO4 = (param('KA_O2'), param('KA_NO3'), param('KA_NH4'), param('KA_PO4'))
    hL_NO3, hL_fe, hH_NO3 = (param('hL_NO3'), param('hL_fe'), param('hH_NO3'))
    hH_NO3_end, hP_NO3 = (param('hH_NO3_end'), param('hP_NO3'))
    hP_NO3_end, hPP_NO3_end = (param('hP_NO3_end'), param('hPP_NO3_end'))
    hPHA_NO3_end, hAUT_NO3_end = (param('hPHA_NO3_end'), param('hAUT_NO3_end'))

    def m(S, K):
        v = _monod(S, K)
        return v if v is not None else 1.0

    def off(K, S):
        v = _switch_off(K, S)
        return v if v is not None else 1.0
    Xratio = m(X_S, KL_X * X_H if KL_X is not None else None)
    sf_sa = S_F + S_A + 1e-10
    frac_F = S_F / sf_sa
    frac_A = S_A / sf_sa
    X_PP_frac = m(X_PP, KP_PP * X_PAO if KP_PP is not None else None)
    X_PHA_frac = m(X_PHA, KP_PHA * X_PAO if KP_PHA is not None else None)
    if K_MAX is not None and KI_PP is not None:
        kmax_x = K_MAX * X_PAO
        denom = KI_PP * X_PAO + kmax_x - X_PP
        M_PPmax_raw = (kmax_x - X_PP) / denom if denom != 0 else 0.0
    else:
        M_PPmax_raw = 1.0
    M_PPmax_disp = min(max(M_PPmax_raw, 0.0), 1.0)

    def decay_env(KO2, KNO3, h_no3):
        if not decay_switch:
            return 1.0
        o2 = m(S_O2, KO2)
        if h_no3 is None:
            return o2
        return o2 + h_no3 * off(KO2, S_O2) * m(S_NO3, KNO3)
    rows: list[dict] = []

    def add(rxn: str, activity: float, attributes: dict[str, float]) -> None:
        name, category = _MASM2D_PROCESS_META[rxn]
        activity_clamped = min(max(activity, 0.0), 1.0)
        rows.append({'process': name, 'reaction_index': rxn, 'category': category, 'activity': activity_clamped, 'activity_raw': activity, 'inhibition_pct': _pct0(activity_clamped), 'process_rate': rate(rxn), 'factors': dict(attributes), 'attributes_pct': {k: _pct0(v) for k, v in attributes.items()}})
    add('R1', m(S_O2, KL_O2) * Xratio, {'O2': m(S_O2, KL_O2), 'X_ratio': Xratio})
    add('R2', (hL_NO3 if hL_NO3 is not None else 1.0) * off(KL_O2, S_O2) * m(S_NO3, KL_NO3) * Xratio, {'anoxic_env': off(KL_O2, S_O2) * m(S_NO3, KL_NO3), 'X_ratio': Xratio})
    add('R3', (hL_fe if hL_fe is not None else 1.0) * off(KL_O2, S_O2) * off(KL_NO3, S_NO3) * Xratio, {'anaerobic_env': off(KL_O2, S_O2) * off(KL_NO3, S_NO3), 'X_ratio': Xratio})
    nutH = lambda: m(S_NH4, KH_NH4) * m(S_PO4, KH_PO4)
    add('R4', m(S_O2, KH_O2) * m(S_F, K_F) * frac_F * nutH(), {'O2': m(S_O2, KH_O2), 'S_F': m(S_F, K_F), 'frac_F': frac_F, 'NH4': m(S_NH4, KH_NH4), 'PO4': m(S_PO4, KH_PO4)})
    add('R5', m(S_O2, KH_O2) * m(S_A, KH_A) * frac_A * nutH(), {'O2': m(S_O2, KH_O2), 'S_A': m(S_A, KH_A), 'frac_A': frac_A, 'NH4': m(S_NH4, KH_NH4), 'PO4': m(S_PO4, KH_PO4)})
    add('R6', (hH_NO3 if hH_NO3 is not None else 1.0) * off(KH_O2, S_O2) * m(S_F, K_F) * frac_F * m(S_NO3, KH_NO3) * nutH(), {'O2_off': off(KH_O2, S_O2), 'S_F': m(S_F, K_F), 'frac_F': frac_F, 'NO3': m(S_NO3, KH_NO3), 'NH4': m(S_NH4, KH_NH4), 'PO4': m(S_PO4, KH_PO4)})
    add('R7', (hH_NO3 if hH_NO3 is not None else 1.0) * off(KH_O2, S_O2) * m(S_A, KH_A) * frac_A * m(S_NO3, KH_NO3) * nutH(), {'O2_off': off(KH_O2, S_O2), 'S_A': m(S_A, KH_A), 'frac_A': frac_A, 'NO3': m(S_NO3, KH_NO3), 'NH4': m(S_NH4, KH_NH4), 'PO4': m(S_PO4, KH_PO4)})
    add('R8', off(KH_O2, S_O2) * off(KH_NO3, S_NO3) * m(S_F, K_fe), {'O2_off': off(KH_O2, S_O2), 'NO3_off': off(KH_NO3, S_NO3), 'S_F': m(S_F, K_fe)})
    add('R9', decay_env(KH_O2, KH_NO3, hH_NO3_end), {'decay_env': decay_env(KH_O2, KH_NO3, hH_NO3_end)})
    add('R10', m(S_A, KP_A) * X_PP_frac, {'S_A': m(S_A, KP_A), 'X_PP_frac': X_PP_frac})
    add('R11', m(S_O2, KP_O2) * m(S_PO4, KP_P) * X_PHA_frac * M_PPmax_raw, {'O2': m(S_O2, KP_O2), 'PO4': m(S_PO4, KP_P), 'X_PHA_frac': X_PHA_frac, 'PP_headroom': M_PPmax_disp})
    add('R12', (hP_NO3 if hP_NO3 is not None else 1.0) * off(KP_O2, S_O2) * m(S_NO3, KP_NO3) * m(S_PO4, KP_P) * X_PHA_frac * M_PPmax_raw, {'O2_off': off(KP_O2, S_O2), 'NO3': m(S_NO3, KP_NO3), 'PO4': m(S_PO4, KP_P), 'X_PHA_frac': X_PHA_frac, 'PP_headroom': M_PPmax_disp})
    add('R13', m(S_O2, KP_O2) * m(S_NH4, KP_NH4) * m(S_PO4, KP_PO4) * X_PHA_frac, {'O2': m(S_O2, KP_O2), 'NH4': m(S_NH4, KP_NH4), 'PO4': m(S_PO4, KP_PO4), 'X_PHA_frac': X_PHA_frac})
    add('R14', (hP_NO3 if hP_NO3 is not None else 1.0) * off(KP_O2, S_O2) * m(S_NO3, KP_NO3) * m(S_NH4, KP_NH4) * m(S_PO4, KP_PO4) * X_PHA_frac, {'O2_off': off(KP_O2, S_O2), 'NO3': m(S_NO3, KP_NO3), 'NH4': m(S_NH4, KP_NH4), 'PO4': m(S_PO4, KP_PO4), 'X_PHA_frac': X_PHA_frac})
    add('R15', decay_env(KP_O2, KP_NO3, hP_NO3_end), {'decay_env': decay_env(KP_O2, KP_NO3, hP_NO3_end)})
    add('R16', decay_env(KP_O2, KP_NO3, hPP_NO3_end), {'decay_env': decay_env(KP_O2, KP_NO3, hPP_NO3_end)})
    add('R17', decay_env(KP_O2, KP_NO3, hPHA_NO3_end), {'decay_env': decay_env(KP_O2, KP_NO3, hPHA_NO3_end)})
    add('R18', m(S_O2, KA_O2) * m(S_NH4, KA_NH4) * m(S_PO4, KA_PO4), {'O2': m(S_O2, KA_O2), 'NH4': m(S_NH4, KA_NH4), 'PO4': m(S_PO4, KA_PO4)})
    add('R19', decay_env(KA_O2, KA_NO3, hAUT_NO3_end), {'decay_env': decay_env(KA_O2, KA_NO3, hAUT_NO3_end)})

    def _rate_check(rxn, mu_key, X_val):
        mu = param(mu_key)
        observed = rate(rxn)
        activity = next((r['activity_raw'] for r in rows if r['reaction_index'] == rxn), None)
        if mu is None or observed is None or activity is None:
            return None
        predicted = mu * activity * X_val / 86400.0
        rel = abs(predicted - observed) / max(abs(observed), 1e-30)
        return {'reaction': rxn, 'predicted_kg_m3_s': predicted, 'observed_kg_m3_s': observed, 'rel_error': rel, 'consistent': rel < 0.01}
    rate_checks = [chk for chk in (_rate_check('R4', 'mu_H', X_H), _rate_check('R13', 'mu_PAO', X_PAO), _rate_check('R18', 'mu_AUT', X_AUT)) if chk is not None]
    active = [r for r in rows if r['category'] != 'lysis']
    inhibition_pcts = [r['inhibition_pct'] for r in active]
    max_inhibition = max(inhibition_pcts) if inhibition_pcts else 0.0
    limiting_process = max(active, key=lambda r: r['inhibition_pct'])['process'] if active else ''
    substrate_monods = [m(S_F, K_F), m(S_A, KH_A), m(S_A, KP_A), m(S_NH4, KA_NH4), m(S_PO4, KA_PO4)]
    positive = [x for x in substrate_monods if x and x > 0]
    min_monod = min(positive) if positive else 1.0
    min_margin = _pct0(min_monod)
    factors = [{'type': 'O2 limitation (heterotroph)', 'value': _pct0(m(S_O2, KH_O2))}, {'type': 'NO3 limitation (anoxic heterotroph)', 'value': _pct0(m(S_NO3, KH_NO3))}, {'type': 'SF substrate limitation', 'value': _pct0(m(S_F, K_F))}, {'type': 'SA substrate limitation', 'value': _pct0(m(S_A, KH_A))}, {'type': 'NH4 limitation (autotroph)', 'value': _pct0(m(S_NH4, KA_NH4))}, {'type': 'NH4 limitation (heterotroph)', 'value': _pct0(m(S_NH4, KH_NH4))}, {'type': 'PO4 limitation (autotroph)', 'value': _pct0(m(S_PO4, KA_PO4))}, {'type': 'O2 limitation (autotroph nitrifier)', 'value': _pct0(m(S_O2, KA_O2))}]
    factors.sort(key=lambda x: x['value'], reverse=True)
    return {'success': True, 'engine': 'masm2d', 'unit_type': unit_type, 'strategy': 'reconstruct_inlined_factors', 'convention': 'factor in [0,1] (1=no limitation); inhibition_pct = 100*(1-factor)', 'decay_switch': decay_switch, 'dissolved_oxygen_mg_l': S_O2 * 1000.0, 'nh4_mg_l_n': S_NH4 * 1000.0, 'no3_mg_l_n': S_NO3 * 1000.0, 'po4_mg_l_p': S_PO4 * 1000.0, 'process_inhibition': rows, 'inhibition_factors': factors, 'health_assessment': {'status': _health_status(max_inhibition), 'max_inhibition': round(max_inhibition, 2), 'limiting_process': limiting_process}, 'safety_assessment': {'status': _safety_status(min_margin), 'min_safety_margin': round(min_margin, 2), 'limiting_process': 'substrate_Monod'}, 'rate_reconstruction_check': rate_checks}
_ASM2D_STOCK_PROCESS_META = {'R1': ('Aerobic Hydrolysis', 'hydrolysis'), 'R2': ('Anoxic Hydrolysis', 'hydrolysis'), 'R3': ('Anaerobic Hydrolysis', 'hydrolysis'), 'R4': ('Aerobic Heterotroph Growth on S_F', 'growth'), 'R5': ('Aerobic Heterotroph Growth on S_A', 'growth'), 'R6': ('Anoxic Heterotroph Growth on S_F (denit)', 'growth'), 'R7': ('Anoxic Heterotroph Growth on S_A (denit)', 'growth'), 'R8': ('Fermentation', 'growth'), 'R9': ('Heterotroph Lysis', 'lysis'), 'R10': ('PAO PHA Storage', 'storage'), 'R11': ('Aerobic PP Storage', 'storage'), 'R12': ('Anoxic PP Storage', 'storage'), 'R13': ('Aerobic PAO Growth', 'growth'), 'R14': ('Anoxic PAO Growth', 'growth'), 'R15': ('PAO Lysis', 'lysis'), 'R16': ('PP Lysis', 'lysis'), 'R17': ('PHA Lysis', 'lysis'), 'R18': ('Autotroph Aerobic Growth (Nitrification)', 'growth'), 'R19': ('Autotroph Lysis', 'lysis'), 'R20': ('Chemical P Precipitation', 'precipitation'), 'R21': ('Chemical P Re-dissolution', 'redissolution')}

def _asm2d_stock_process_health(rblock, unit_type: str) -> dict:
    """Strategy B for stock IWA ASM2d: reconstruct the inlined Monod factors.

    Same approach as :func:`_masm2d_process_health` but for the stock ASM2d
    reaction package: single shared saturation coefficients, an alkalinity Monod
    term, and the precipitation/re-dissolution pair. ``reaction_rate[r]`` is
    harvested directly; every switching/Monod factor is replayed from the solved
    state + named params, matching ``asm2d_reactions.py`` bit-for-bit.
    """

    def conc(key):
        try:
            v = _pyo_value(rblock.conc_mass_comp_ref[key])
            return v if v is not None else 0.0
        except Exception:
            return 0.0

    def param(key):
        comp = getattr(getattr(rblock, 'params', None), key, None)
        return _pyo_value(comp) if comp is not None else None

    def rate(rxn):
        try:
            return _pyo_value(rblock.reaction_rate[rxn])
        except Exception:
            return None

    def m(S, K):
        v = _monod(S, K)
        return v if v is not None else 1.0

    def off(K, S):
        v = _switch_off(K, S)
        return v if v is not None else 1.0
    S_O2, S_F, S_A = (conc('S_O2'), conc('S_F'), conc('S_A'))
    S_NH4, S_NO3, S_PO4 = (conc('S_NH4'), conc('S_NO3'), conc('S_PO4'))
    X_S, X_H, X_PAO = (conc('X_S'), conc('X_H'), conc('X_PAO'))
    X_PP, X_PHA, X_AUT = (conc('X_PP'), conc('X_PHA'), conc('X_AUT'))
    alk = _pyo_value(getattr(rblock.state_ref, 'alkalinity', None)) if hasattr(rblock, 'state_ref') else None
    K_O2, K_NO3, K_X = (param('K_O2'), param('K_NO3'), param('K_X'))
    K_F, K_A, K_fe, K_NH4, K_P = (param('K_F'), param('K_A'), param('K_fe'), param('K_NH4'), param('K_P'))
    K_ALK = param('K_ALK')
    K_PS, K_PP, K_PHA, K_MAX, K_IPP = (param('K_PS'), param('K_PP'), param('K_PHA'), param('K_MAX'), param('K_IPP'))
    eta_NO3, eta_fe = (param('eta_NO3'), param('eta_fe'))
    alk_m = m(alk, K_ALK)
    Xratio = m(X_S, K_X * X_H if K_X is not None else None)
    sf_sa = S_F + S_A + 1e-10
    frac_F, frac_A = (S_F / sf_sa, S_A / sf_sa)
    X_PP_frac = m(X_PP, K_PP * X_PAO if K_PP is not None else None)
    X_PHA_frac = m(X_PHA, K_PHA * X_PAO if K_PHA is not None else None)
    if K_MAX is not None and K_IPP is not None:
        kmax_x = K_MAX * X_PAO
        denom = K_IPP * X_PAO + kmax_x - X_PP
        M_PPmax_raw = (kmax_x - X_PP) / denom if denom != 0 else 0.0
    else:
        M_PPmax_raw = 1.0
    M_PPmax_disp = min(max(M_PPmax_raw, 0.0), 1.0)
    nutH = m(S_NH4, K_NH4) * m(S_PO4, K_P)
    anoxic_pp_mult = (eta_NO3 if eta_NO3 is not None else 1.0) * (K_O2 / S_O2 if K_O2 is not None and S_O2 > 0 else 0.0) * m(S_NO3, K_NO3)
    rows: list[dict] = []

    def add(rxn: str, activity: float, attributes: dict[str, float], extra: dict | None=None) -> None:
        name, category = _ASM2D_STOCK_PROCESS_META[rxn]
        activity_clamped = min(max(activity, 0.0), 1.0)
        row = {'process': name, 'reaction_index': rxn, 'category': category, 'activity': activity_clamped, 'activity_raw': activity, 'inhibition_pct': _pct0(activity_clamped), 'process_rate': rate(rxn), 'factors': dict(attributes), 'attributes_pct': {k: _pct0(v) for k, v in attributes.items()}}
        if extra:
            row.update(extra)
        rows.append(row)
    add('R1', m(S_O2, K_O2) * Xratio, {'O2': m(S_O2, K_O2), 'X_ratio': Xratio})
    add('R2', (eta_NO3 if eta_NO3 is not None else 1.0) * off(K_O2, S_O2) * m(S_NO3, K_NO3) * Xratio, {'anoxic_env': off(K_O2, S_O2) * m(S_NO3, K_NO3), 'X_ratio': Xratio})
    add('R3', (eta_fe if eta_fe is not None else 1.0) * off(K_O2, S_O2) * off(K_NO3, S_NO3) * Xratio, {'anaerobic_env': off(K_O2, S_O2) * off(K_NO3, S_NO3), 'X_ratio': Xratio})
    add('R4', m(S_O2, K_O2) * m(S_F, K_F) * frac_F * nutH * alk_m, {'O2': m(S_O2, K_O2), 'S_F': m(S_F, K_F), 'frac_F': frac_F, 'NH4': m(S_NH4, K_NH4), 'PO4': m(S_PO4, K_P), 'ALK': alk_m})
    add('R5', m(S_O2, K_O2) * m(S_A, K_A) * frac_A * nutH * alk_m, {'O2': m(S_O2, K_O2), 'S_A': m(S_A, K_A), 'frac_A': frac_A, 'NH4': m(S_NH4, K_NH4), 'PO4': m(S_PO4, K_P), 'ALK': alk_m})
    add('R6', (eta_NO3 if eta_NO3 is not None else 1.0) * off(K_O2, S_O2) * m(S_NO3, K_NO3) * m(S_F, K_F) * frac_F * nutH * alk_m, {'O2_off': off(K_O2, S_O2), 'NO3': m(S_NO3, K_NO3), 'S_F': m(S_F, K_F), 'frac_F': frac_F, 'NH4': m(S_NH4, K_NH4), 'PO4': m(S_PO4, K_P), 'ALK': alk_m})
    add('R7', (eta_NO3 if eta_NO3 is not None else 1.0) * off(K_O2, S_O2) * m(S_NO3, K_NO3) * m(S_A, K_A) * frac_A * nutH * alk_m, {'O2_off': off(K_O2, S_O2), 'NO3': m(S_NO3, K_NO3), 'S_A': m(S_A, K_A), 'frac_A': frac_A, 'NH4': m(S_NH4, K_NH4), 'PO4': m(S_PO4, K_P), 'ALK': alk_m})
    add('R8', off(K_O2, S_O2) * off(K_NO3, S_NO3) * m(S_F, K_F) * alk_m, {'O2_off': off(K_O2, S_O2), 'NO3_off': off(K_NO3, S_NO3), 'S_F': m(S_F, K_F), 'ALK': alk_m})
    add('R9', 1.0, {})
    add('R10', m(S_A, K_A) * alk_m * X_PP_frac, {'S_A': m(S_A, K_A), 'ALK': alk_m, 'X_PP_frac': X_PP_frac})
    add('R11', m(S_O2, K_O2) * m(S_PO4, K_PS) * alk_m * X_PHA_frac * M_PPmax_raw, {'O2': m(S_O2, K_O2), 'PO4': m(S_PO4, K_PS), 'ALK': alk_m, 'X_PHA_frac': X_PHA_frac, 'PP_headroom': M_PPmax_disp})
    add('R12', anoxic_pp_mult, {'NO3': m(S_NO3, K_NO3)}, extra={'relative_rate_multiplier': anoxic_pp_mult, 'relative_to': 'R11'})
    add('R13', m(S_O2, K_O2) * m(S_NH4, K_NH4) * m(S_PO4, K_P) * alk_m * X_PHA_frac, {'O2': m(S_O2, K_O2), 'NH4': m(S_NH4, K_NH4), 'PO4': m(S_PO4, K_P), 'ALK': alk_m, 'X_PHA_frac': X_PHA_frac})
    add('R14', anoxic_pp_mult, {'NO3': m(S_NO3, K_NO3)}, extra={'relative_rate_multiplier': anoxic_pp_mult, 'relative_to': 'R13'})
    add('R15', alk_m, {'ALK': alk_m})
    add('R16', alk_m, {'ALK': alk_m})
    add('R17', alk_m, {'ALK': alk_m})
    add('R18', m(S_O2, K_O2) * m(S_NH4, K_NH4) * m(S_PO4, K_P) * alk_m, {'O2': m(S_O2, K_O2), 'NH4': m(S_NH4, K_NH4), 'PO4': m(S_PO4, K_P), 'ALK': alk_m})
    add('R19', 1.0, {})
    add('R20', 1.0, {})
    add('R21', alk_m, {'ALK': alk_m})

    def _rate_check(rxn, mu_key, X_val):
        mu = param(mu_key)
        observed = rate(rxn)
        activity = next((r['activity_raw'] for r in rows if r['reaction_index'] == rxn), None)
        if mu is None or observed is None or activity is None:
            return None
        predicted = mu * activity * X_val / 86400.0
        rel = abs(predicted - observed) / max(abs(observed), 1e-30)
        return {'reaction': rxn, 'predicted_kg_m3_s': predicted, 'observed_kg_m3_s': observed, 'rel_error': rel, 'consistent': rel < 0.01}
    rate_checks = [chk for chk in (_rate_check('R4', 'mu_H', X_H), _rate_check('R13', 'mu_PAO', X_PAO), _rate_check('R18', 'mu_AUT', X_AUT)) if chk is not None]
    active = [r for r in rows if r['category'] not in ('lysis', 'precipitation', 'redissolution')]
    inhibition_pcts = [r['inhibition_pct'] for r in active]
    max_inhibition = max(inhibition_pcts) if inhibition_pcts else 0.0
    limiting_process = max(active, key=lambda r: r['inhibition_pct'])['process'] if active else ''
    substrate_monods = [m(S_F, K_F), m(S_A, K_A), m(S_NH4, K_NH4), m(S_PO4, K_P)]
    positive = [x for x in substrate_monods if x and x > 0]
    min_margin = _pct0(min(positive)) if positive else 0.0
    factors = [{'type': 'O2 limitation', 'value': _pct0(m(S_O2, K_O2))}, {'type': 'NO3 limitation (anoxic)', 'value': _pct0(m(S_NO3, K_NO3))}, {'type': 'SF substrate limitation', 'value': _pct0(m(S_F, K_F))}, {'type': 'SA substrate limitation', 'value': _pct0(m(S_A, K_A))}, {'type': 'NH4 limitation', 'value': _pct0(m(S_NH4, K_NH4))}, {'type': 'PO4 limitation', 'value': _pct0(m(S_PO4, K_P))}, {'type': 'Alkalinity limitation', 'value': _pct0(alk_m)}]
    factors.sort(key=lambda x: x['value'], reverse=True)
    return {'success': True, 'engine': 'asm2d', 'unit_type': unit_type, 'strategy': 'reconstruct_inlined_factors', 'convention': 'factor in [0,1] (1=no limitation); inhibition_pct = 100*(1-factor)', 'dissolved_oxygen_mg_l': S_O2 * 1000.0, 'nh4_mg_l_n': S_NH4 * 1000.0, 'no3_mg_l_n': S_NO3 * 1000.0, 'po4_mg_l_p': S_PO4 * 1000.0, 'alkalinity_mol_m3': alk, 'process_inhibition': rows, 'inhibition_factors': factors, 'health_assessment': {'status': _health_status(max_inhibition), 'max_inhibition': round(max_inhibition, 2), 'limiting_process': limiting_process}, 'safety_assessment': {'status': _safety_status(min_margin), 'min_safety_margin': round(min_margin, 2), 'limiting_process': 'substrate_Monod'}, 'rate_reconstruction_check': rate_checks}

def extract_biological_process_health(model, units: dict, unit_instances: dict, solved_state: dict) -> dict:
    """Build the structured per-unit ``process_health`` payload for a solved flowsheet.

    For every biological unit whose reaction package is ADM1 / mADM1 / mASM2d
    (type-detected via :func:`_detect_reaction_block`), produce the QSDsan-shaped
    health report (per-process inhibition rows + ranked ``inhibition_factors`` +
    ``health_assessment``). READ-ONLY on the solved model: every term is evaluated
    with ``pyo.value`` at the converged optimum -- it never perturbs the solve.
    """
    reports: dict[str, dict] = {}
    if not isinstance(units, dict):
        return reports
    for unit_id, unit_block in units.items():
        if unit_block is None:
            continue
        family, rblock = _detect_reaction_block(unit_block)
        if family is None or rblock is None:
            continue
        unit_inst = unit_instances.get(unit_id) if isinstance(unit_instances, dict) else None
        unit_type = getattr(unit_inst, 'unit_type', None) or 'unknown'
        try:
            if family in ('madm1', 'adm1'):
                report = _adm_process_health(rblock, family, unit_type)
            elif family == 'masm2d':
                report = _masm2d_process_health(rblock, unit_type)
            elif family == 'asm2d':
                report = _asm2d_stock_process_health(rblock, unit_type)
            else:
                continue
        except Exception as exc:
            report = {'success': False, 'engine': family, 'unit_type': unit_type, 'reason': f'{type(exc).__name__}: {exc}'}
        if report:
            reports[unit_id] = report
    return reports

def _extract_process_diagnostics(model, units: dict, unit_instances: dict, solved_state: dict, runtime_metadata: dict | None=None) -> dict:
    """Export comprehensive steady-state diagnostics for reporting and persistence."""
    diagnostics = {'schema_version': 'watertap_process_diagnostics/v1', 'unit_diagnostics': {}, 'unit_reports': {}, 'biological': {'aerobic': {'unit_ids': [], 'rates': {}, 'inhibition': {}, 'kinetic_parameters': {}, 'state_indicators': {}}, 'anaerobic': {'unit_ids': [], 'rates': {}, 'inhibition': {}, 'kinetic_parameters': {}, 'state_indicators': {}}}, 'family_summary': {}, 'actuator_bound_activity': _extract_actuator_bound_activity(units)}
    pressure_reference = runtime_metadata.get('asm2d_pressure_reference') if isinstance(runtime_metadata, dict) else None
    if isinstance(pressure_reference, dict):
        diagnostics['asm2d_pressure_reference'] = pressure_reference
    solved_units = solved_state.get('units', {}) if isinstance(solved_state, dict) else {}
    for unit_id, unit_block in units.items():
        unit_inst = unit_instances.get(unit_id)
        unit_type = getattr(unit_inst, 'unit_type', 'Translator')
        package_name = _unit_package_name(unit_block)
        family = _diagnostic_family(unit_type, package_name)
        bio_mode = _bio_mode(unit_type, package_name)
        component_topics = _extract_component_topic_snapshots(unit_block, unit_type)
        performance = _extract_performance_contents(unit_block)
        report_text = _capture_unit_report(unit_block)
        solved_unit_payload = solved_units.get(unit_id, {}) if isinstance(solved_units, dict) else {}
        state_indicators = _extract_state_indicators(solved_unit_payload, bio_mode)
        unit_payload = {'unit_type': unit_type, 'property_package': package_name, 'diagnostic_family': family, 'performance_contents': performance, 'diagnostic_variables': component_topics['variables'], 'diagnostic_expressions': component_topics['expressions'], 'diagnostic_parameters': component_topics['parameters'], 'state_indicators': state_indicators, 'solved_state_ref': f'solved_state.units.{unit_id}', 'report_text_available': bool(report_text)}
        diagnostics['unit_diagnostics'][unit_id] = unit_payload
        diagnostics['family_summary'].setdefault(family, []).append(unit_id)
        if report_text:
            diagnostics['unit_reports'][unit_id] = report_text
        if bio_mode in ('aerobic', 'anaerobic'):
            bio = diagnostics['biological'][bio_mode]
            bio['unit_ids'].append(unit_id)
            rates = {**component_topics['variables'].get('biological_rates', {}), **component_topics['expressions'].get('biological_rates', {})}
            inhibition = {**component_topics['variables'].get('biological_inhibition', {}), **component_topics['expressions'].get('biological_inhibition', {})}
            kinetics = {**component_topics['parameters'].get('kinetic_parameters', {}), **component_topics['variables'].get('kinetic_parameters', {}), **component_topics['expressions'].get('kinetic_parameters', {})}
            if rates:
                bio['rates'][unit_id] = rates
            if inhibition:
                bio['inhibition'][unit_id] = inhibition
            if kinetics:
                bio['kinetic_parameters'][unit_id] = kinetics
            if state_indicators:
                bio['state_indicators'][unit_id] = state_indicators
    process_health = extract_biological_process_health(model, units, unit_instances, solved_state)
    if process_health:
        diagnostics['process_health'] = process_health
    return diagnostics
