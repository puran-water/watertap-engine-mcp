"""Stage-(b) geometry custody for stage-(c) rating. No sizing correlations.

The caller resolves the sizing artifact; this boundary checks its reference and
the bound output values/units without doing database I/O. Whole-artifact hash
verification remains the responsibility of the process-balance evidence resolver.
"""
from __future__ import annotations
import math
import re
from copy import deepcopy
EVIDENCE_KEYS = ('sizing_reference', 'seed_bindings', 'sizing_outputs')

class RatingGeometryError(ValueError):
    """A missing, unbound, changed, or unsupported equipment specification."""

def require_number(value, field, unit, unit_type):
    if isinstance(value, bool):
        value = None
    try:
        number = float(value)
    except (ValueError, TypeError):
        number = float('nan')
    if not math.isfinite(number) or number <= 0 or (unit == 'count' and (not number.is_integer())) or (unit == 'fraction' and number > 1):
        raise RatingGeometryError(f'{unit_type}: missing or invalid {field} [{unit}]; stage (b) heuristic sizing must supply a finite positive specification and its sizing reference.')
    return number

def validate_bindings(values, evidence, unit_type):
    """Match every applicable geometry leaf to a resolved, unit-bearing output."""
    reference = evidence.get('sizing_reference')
    if not isinstance(reference, dict) or reference.get('artifact_type') not in {'heuristic_design_snapshot', 'heuristic_sizing', 'sizing_output'} or (not str(reference.get('artifact_id') or '').strip()) or (not re.fullmatch('[0-9a-fA-F]{64}', str(reference.get('content_hash') or ''))):
        fields = ', '.join((f'{key} [{unit}]' for key, (_, unit) in values.items()))
        raise RatingGeometryError(f'{unit_type}: {fields} require sizing_reference.artifact_id and sizing_reference.content_hash (SHA256), with a heuristic sizing artifact_type, from stage (b) heuristic sizing.')
    bindings = evidence.get('seed_bindings')
    outputs = evidence.get('sizing_outputs')
    if not isinstance(bindings, dict) or not isinstance(outputs, dict):
        raise RatingGeometryError(f'{unit_type}: stage (b) must supply seed_bindings and sizing_outputs for ' + ', '.join((f'{key} [{unit}]' for key, (_, unit) in values.items())))
    for field, (value, unit) in values.items():
        output_key = bindings.get(field)
        output = outputs.get(output_key) if isinstance(output_key, str) else None
        if not isinstance(output, dict) or output.get('unit') != unit or output.get('value') != value:
            raise RatingGeometryError(f'{unit_type}: {field} [{unit}] differs from or lacks its bound stage (b) heuristic sizing output; supply seed_bindings and matching sizing_outputs.')
    return {key: deepcopy(evidence[key]) for key in EVIDENCE_KEYS}

def template_geometry(template_name, config):
    """Validate supplied config BEFORE any template defaults are instantiated."""
    name = str(template_name).strip().lower().replace('-', '_')
    if str(config.get('operating_mode', 'rating')).strip().lower() != 'rating' or config.get('reactor_volumes_fixed') is False:
        raise RatingGeometryError(f'{name}: rating cannot release geometry; use design_flowsheet or apply_design_template to explicitly request supported inverse design.')
    values = {}

    def need(field, unit):
        value = config.get(field)
        kind = 'CSTR_Injection' if field.startswith('aerobic') else 'CSTR' if name.startswith('asm2d') else 'Separator: AnMBR membrane' if field == 'membrane_particulate_passage' else 'AnMBR retained-solids SRT' if field == 'target_srt_days' else 'AD' if name.startswith('adm1') else 'ReverseOsmosis0D' if name == 'ro_train' else 'Nanofiltration0D' if name == 'nf_softening' else 'Electrodialysis1D' if name.startswith('ed_') else 'Evaporator'
        values[field] = (require_number(value, field, unit, f'{name} ({kind})'), unit)
    if name.startswith('asm2d'):
        if name == 'asm2d_mbr_aerobic':
            if config.get('anoxic_volume_m3') is not None:
                raise RatingGeometryError(f'{name}: the aerobic-only MBR has no anoxic zone; stage (b) supplies anoxic_volume_m3 {config.get('anoxic_volume_m3')!r} [m3] (an MLE block rates on asm2d_mbr)')
        else:
            need('anoxic_volume_m3', 'm3')
        need('aerobic_volume_m3', 'm3')
        if name.endswith('_post'):
            need('post_anoxic_volume_m3', 'm3')
        stages = config.get('aerobic_stage_volumes_m3')
        if stages is not None:
            if not isinstance(stages, list) or not stages:
                raise RatingGeometryError(f'{name}: stage (b) must supply aerobic_stage_volumes_m3 [m3] per reactor')
            for index, value in enumerate(stages):
                field = f'aerobic_stage_volumes_m3[{index}]'
                values[field] = (require_number(value, field, 'm3', name), 'm3')
            if not math.isclose(sum(stages), float(config['aerobic_volume_m3']), rel_tol=1e-12):
                raise RatingGeometryError(f'{name}: aerobic_stage_volumes_m3 [m3] must sum to stage (b) aerobic_volume_m3')
    elif name.startswith('adm1'):
        need('volume_liquid_m3', 'm3')
        need('volume_vapor_m3', 'm3')
        if name == 'adm1_two_stage':
            need('acid_volume_liquid_m3', 'm3')
            need('acid_volume_vapor_m3', 'm3')
        if config.get('oxidant_inlet') is not None:
            need('headspace_wetted_area_m2', 'm2')
        elif config.get('headspace_wetted_area_m2') is not None or 'headspace_wetted_area_m2' in (config.get('seed_bindings') or {}):
            raise RatingGeometryError(f"{name}: stage (b) supplies headspace_wetted_area_m2 [m2] but no oxidant_inlet is stated; the wetted area is the micro-aeration variant's geometry and would be dropped.")
        membrane_fields = ('membrane_particulate_passage',)
        if name == 'adm1_anmbr':
            need('membrane_particulate_passage', 'fraction')
            need('target_srt_days', 'd')
        else:
            carried = sorted((field for field in membrane_fields if field in (config.get('seed_bindings') or {}) or config.get(field) is not None))
            if carried:
                raise RatingGeometryError(f'{name}: stage (b) supplies an AnMBR membrane ({', '.join(carried)}) that {name} cannot represent; rate adm1_anmbr, never a digester that silently drops the membrane.')
    elif name == 'ro_train':
        vessels = config.get('stage_vessels')
        if vessels is not None:
            if not isinstance(vessels, list) or not vessels:
                raise RatingGeometryError(f'{name}: stage (b) must supply nonempty stage_vessels [count]')
            for index, value in enumerate(vessels):
                field = f'stage_vessels[{index}]'
                values[field] = (require_number(value, field, 'count', name), 'count')
            need('elements_per_vessel', 'count')
            need('element_area_m2', 'm2')
            if config.get('n_stages', len(vessels)) != len(vessels):
                raise RatingGeometryError(f'{name}: n_stages [count] must match stage (b) stage_vessels')
            if config.get('membrane_area_m2') is not None:
                need('membrane_area_m2', 'm2')
                total = sum(vessels) * config['elements_per_vessel'] * config['element_area_m2']
                if not math.isclose(total, config['membrane_area_m2'], rel_tol=1e-12):
                    raise RatingGeometryError(f'{name}: membrane_area_m2 [m2] conflicts with the stage (b) array')
        else:
            need('membrane_area_m2', 'm2')
            if config.get('n_stages', 1) != 1:
                raise RatingGeometryError(f'{name}: stage (b) must supply stage_vessels [count], elements_per_vessel [count], and element_area_m2 [m2] for a multistage rating; total area cannot define its allocation.')
    elif name == 'nf_softening':
        need('membrane_area_m2', 'm2')
        if config.get('n_stages', 1) != 1:
            raise RatingGeometryError(f'{name}: multistage geometry rating is not implemented; stage (b) must name each NF unit')
    elif name in {'mvc_evaporator', 'mvc_crystallizer'}:
        need('evaporator_area_m2', 'm2')
        if config.get('target_brine_mass_fraction') is not None:
            raise RatingGeometryError(f'{name}: target_brine_mass_fraction releases evaporator_area_m2 [m2]; use design_flowsheet explicitly. Stage (b) supplies geometry for rating.')
        if name == 'mvc_crystallizer':
            raise RatingGeometryError('mvc_crystallizer: fixed crystallizer volume_suspension [m3] rating is not qualified; stage (b) must supply crystallizer geometry and the owner must select its rating formulation. The residence-time/yield model designs geometry and cannot be reported as rating.')
    elif name in {'ed_k_stage_series', 'ed_stack_conc_recirc'}:
        if name == 'ed_k_stage_series':
            pairs = config.get('stage_cell_pairs')
            if not isinstance(pairs, list) or not pairs:
                raise RatingGeometryError(f'{name}: stage (b) must supply stage_cell_pairs [count] per stack')
            for index, value in enumerate(pairs):
                field = f'stage_cell_pairs[{index}]'
                values[field] = (require_number(value, field, 'count', f'{name} (Electrodialysis1D)'), 'count')
        else:
            need('cell_pairs', 'count')
        for field, unit in (('cell_width_m', 'm'), ('cell_length_m', 'm'), ('channel_height_m', 'm'), ('spacer_porosity', 'fraction'), ('membrane_thickness_m', 'm')):
            need(field, unit)
    else:
        raise RatingGeometryError(f'{name}: no stage (b) geometry rating contract is registered')
    evidence = validate_bindings(values, config, name)
    return {'analysis_mode': 'rating', 'template_name': name, 'geometry_inputs': values, **evidence}
GEOMETRY_VARIABLES = frozenset({'area', 'volume', 'volume_liquid', 'volume_vapor', 'headspace_wetted_area', 'volume_suspension', 'bed_depth', 'bed_length', 'reactor_volume', 'length', 'channel_height', 'cell_width', 'cell_length', 'crystal_median_length', 'spacer_porosity', 'membrane_thickness', 'spacer_mixing_length', 'resin_diam', 'bed_porosity', 'cell_triplet_num', 'cell_pair_num'})
GEOMETRY_UNITS = {name: 'm' for name in GEOMETRY_VARIABLES}
GEOMETRY_UNITS.update({'area': 'm2', 'volume': 'm3', 'volume_liquid': 'm3', 'volume_vapor': 'm3', 'headspace_wetted_area': 'm2', 'volume_suspension': 'm3', 'reactor_volume': 'm3', 'spacer_porosity': 'dimensionless', 'bed_porosity': 'dimensionless', 'cell_triplet_num': 'count', 'cell_pair_num': 'count'})

def _split_fraction_index(var_name):
    """``split_fraction[0,permeate,X_su]`` -> ("permeate", "X_su"), else None."""
    if not var_name.startswith('split_fraction[') or not var_name.endswith(']'):
        return None
    parts = [part.strip() for part in var_name[len('split_fraction['):-1].split(',')]
    return (parts[1], parts[2]) if len(parts) == 3 else None

def _bind_membrane_passage(passage, session_spec):
    """The AnMBR membrane's compiled particulate splits must carry the stage-(b) passage.

    Each particulate component's permeate split must equal the solvent's permeate
    split x the passage (permeate particulate concentration = passage x the
    digester's), and stays a fixed native quantity checked again after the solve.
    """
    separators = [unit for unit in session_spec['units'] if getattr(unit.get('role'), 'value', unit.get('role', '')) == 'anaerobic_thickener']
    if len(separators) != 1:
        raise RatingGeometryError(f'membrane_particulate_passage [fraction] needs exactly one AnMBR membrane separator; the compiled template has {len(separators)}')
    separator = separators[0]
    splits = {}
    for fix in session_spec.get('dof_fixes', []):
        index = _split_fraction_index(fix['var_name']) if fix['unit_id'] == separator['unit_id'] else None
        if index is not None:
            splits[index[1]] = (index[0], fix)
    if 'H2O' not in splits:
        raise RatingGeometryError(f'{separator['unit_id']} (Separator): the compiled membrane fixes no solvent permeate split; the stage (b) membrane_particulate_passage [fraction] cannot be carried')
    solvent = float(splits['H2O'][1]['value'])
    rows = []
    for component, (outlet, fix) in sorted(splits.items()):
        if not component.startswith('X_'):
            continue
        expected = solvent * float(passage)
        if not math.isclose(float(fix['value']), expected, rel_tol=1e-12, abs_tol=1e-15):
            raise RatingGeometryError(f'{separator['unit_id']} (Separator).{fix['var_name']}: compiled particulate split {fix['value']} differs from stage (b) membrane_particulate_passage x solvent split = {expected}')
        rows.append({**fix, 'unit_type': separator['unit_type'], 'source_field': 'membrane_particulate_passage', 'unit': 'fraction'})
    if not rows:
        raise RatingGeometryError(f'{separator['unit_id']} (Separator): the compiled membrane fixes no particulate split; the stage (b) membrane_particulate_passage [fraction] was dropped')
    return rows

def _bind_srt_target(target_srt_days, session_spec):
    """The compiled retained-solids SRT closure must target the stage-(b) SRT exactly."""
    constraints = [c for c in session_spec.get('process_constraints', []) if c.get('type') == 'srt']
    if len(constraints) != 1:
        raise RatingGeometryError(f'target_srt_days [d] needs exactly one retained-solids SRT closure; the compiled template has {len(constraints)}')
    constraint = constraints[0]
    compiled = float(constraint.get('target_days'))
    if not math.isclose(compiled, float(target_srt_days), rel_tol=1e-12, abs_tol=1e-12):
        raise RatingGeometryError(f'{constraint.get('name')}: compiled SRT target {compiled} d differs from stage (b) target_srt_days={target_srt_days}')
    return {'unit_id': constraint.get('waste_unit'), 'var_name': None, 'kind': 'process_constraint_target', 'constraint': constraint.get('name'), 'value': compiled, 'unit_type': 'retained-solids SRT closure', 'source_field': 'target_srt_days', 'unit': 'd'}
_ED_GEOMETRY_FIELDS = {'cell_pair_num': None, 'cell_width': 'cell_width_m', 'cell_length': 'cell_length_m', 'channel_height': 'channel_height_m', 'spacer_porosity': 'spacer_porosity', 'membrane_thickness': 'membrane_thickness_m'}

def bind_native_geometry(contract, session_spec):
    """Capture the exact fixed native geometry after template compilation."""
    unit_specs = {u['unit_id']: u for u in session_spec['units']}
    inputs = {key: quantity[0] for key, quantity in contract['geometry_inputs'].items()}
    rows = []
    consumed = set()
    for fix in session_spec.get('dof_fixes', []):
        variable = fix['var_name'].split('[')[0]
        if variable not in GEOMETRY_VARIABLES:
            continue
        unit = unit_specs[fix['unit_id']]
        role = getattr(unit.get('role'), 'value', unit.get('role', ''))
        if variable == 'volume':
            field = {'pre_anoxic': 'anoxic_volume_m3', 'aerobic': 'aerobic_volume_m3', 'post_anoxic': 'post_anoxic_volume_m3'}.get(role)
        elif variable in {'volume_liquid', 'volume_vapor'}:
            field = ('acid_' if role == 'anaerobic_acidogenic' else '') + variable + '_m3'
        elif variable == 'headspace_wetted_area':
            field = 'headspace_wetted_area_m2'
        elif variable == 'area':
            field = 'evaporator_area_m2' if unit['unit_type'] == 'Evaporator' else 'membrane_area_m2'
        elif unit['unit_type'] == 'Electrodialysis1D' and variable in _ED_GEOMETRY_FIELDS:
            if variable == 'cell_pair_num':
                field = f'stage_cell_pairs[{int(fix['unit_id'].split('_')[-1]) - 1}]' if fix['unit_id'].startswith('ED_') else 'cell_pairs'
            else:
                field = _ED_GEOMETRY_FIELDS[variable]
        else:
            field = None
        expected = inputs.get(field)
        if variable == 'area' and 'stage_vessels[0]' in inputs:
            stage = int(fix['unit_id'].split('_')[-1]) - 1 if fix['unit_id'].startswith('RO_') else 0
            expected = inputs[f'stage_vessels[{stage}]'] * inputs['elements_per_vessel'] * inputs['element_area_m2']
            field = f'stage_vessels[{stage}] * elements_per_vessel * element_area_m2'
            consumed.update({f'stage_vessels[{stage}]', 'elements_per_vessel', 'element_area_m2'})
            if 'membrane_area_m2' in inputs:
                consumed.add('membrane_area_m2')
        elif field is not None:
            consumed.add(field)
        if expected is None or not math.isclose(float(fix['value']), expected, rel_tol=1e-12, abs_tol=1e-12):
            raise RatingGeometryError(f'{fix['unit_id']} ({unit['unit_type']}).{fix['var_name']}: compiled geometry {fix['value']} differs from stage (b) {field}={expected}')
        rows.append({**fix, 'unit_type': unit['unit_type'], 'source_field': field, 'unit': GEOMETRY_UNITS[variable]})
    if 'membrane_particulate_passage' in inputs:
        rows.extend(_bind_membrane_passage(inputs['membrane_particulate_passage'], session_spec))
        consumed.add('membrane_particulate_passage')
    if 'target_srt_days' in inputs:
        rows.append(_bind_srt_target(inputs['target_srt_days'], session_spec))
        consumed.add('target_srt_days')
    if not rows:
        raise RatingGeometryError('Rating compiled no fixed geometry from stage (b)')
    missing = set(inputs) - consumed
    if missing:
        quantities = ', '.join((f'{field} [{contract['geometry_inputs'][field][1]}]' for field in sorted(missing)))
        raise RatingGeometryError(f'{contract['template_name']}: compiled model dropped stage (b) geometry {quantities}; each supplied specification must remain a fixed native rating quantity.')
    return {**deepcopy(contract), 'native_geometry': rows}

def bind_graph_geometry(session_spec, specification):
    """Guard designed-graph/seed compilation against provider geometry defaults.

    Native geometry uses keys such as ``Anoxic.volume[0]`` with {value, unit},
    and the same sizing_reference/seed_bindings/sizing_outputs contract.
    """
    specification = specification or {}
    supplied = specification.get('geometry') or {}
    expected_units = GEOMETRY_UNITS
    values, rows = ({}, [])
    unit_types = {u['unit_id']: u['unit_type'] for u in session_spec['units']}
    fixes = list(session_spec.get('dof_fixes', []))
    for unit_spec in session_spec['units']:
        if unit_spec['unit_type'] == 'GasMembraneStage1D':
            config = unit_spec.get('config', {})
            for variable, field in (('area', 'area_cm2'), ('length', 'length_cm')):
                fixes.append({'unit_id': unit_spec['unit_id'], 'var_name': variable, 'value': config.get(field)})
    for fix in fixes:
        variable = fix['var_name'].split('[')[0]
        if variable not in GEOMETRY_VARIABLES:
            continue
        key = f'{fix['unit_id']}.{fix['var_name']}'
        unit = expected_units.get(variable, 'm')
        if unit_types[fix['unit_id']] == 'GasMembraneStage1D':
            unit = 'cm2' if variable == 'area' else 'cm'
        provided = supplied.get(key) or {}
        number = require_number(provided.get('value'), key, unit, unit_types[fix['unit_id']])
        compiled = require_number(fix['value'], key, unit, unit_types[fix['unit_id']])
        if provided.get('unit') != unit or not math.isclose(number, compiled, rel_tol=1e-12):
            raise RatingGeometryError(f'{key} [{unit}] differs from its stage (b) geometry; update the graph config from that sizing output')
        values[key] = (number, unit)
        rows.append({**fix, 'unit_type': unit_types[fix['unit_id']], 'unit': unit})
    if not rows:
        raise RatingGeometryError('Designed graph has no qualified fixed geometry rating contract; stage (b) must supply native geometry [m3/m2/m]')
    native_requirements = {'CSTR': ('volume',), 'CSTR_Injection': ('volume',), 'AD': ('volume_liquid', 'volume_vapor'), 'ReverseOsmosis0D': ('area',), 'ReverseOsmosis1D': ('area', 'length'), 'Evaporator': ('area',), 'Crystallizer': ('volume_suspension',), 'Nanofiltration0D': ('area',), 'NanofiltrationDSPMDE0D': ('area',), 'GasMembraneStage1D': ('area', 'length')}
    fixed_keys = {(row['unit_id'], row['var_name'].split('[')[0]) for row in rows}
    for unit_id, unit_type in unit_types.items():
        for variable in native_requirements.get(unit_type, ()):
            if (unit_id, variable) not in fixed_keys:
                raise RatingGeometryError(f'{unit_id} ({unit_type}): stage (b) must supply fixed {variable} [{expected_units.get(variable, 'm3/m')}] for rating')
    if set(supplied) != set(values):
        raise RatingGeometryError(f'Stage (b) graph geometry has unconsumed fields: {sorted(set(supplied) - set(values))}')
    evidence = validate_bindings(values, specification, 'designed_graph')
    return {'analysis_mode': 'rating', 'native_geometry': rows, 'geometry_inputs': values, **evidence}

def verify_live_geometry(contract, units):
    """Fail closed on changed, missing or unfixed native geometry, before issue."""
    if not contract or contract.get('analysis_mode') != 'rating':
        return
    from pyomo.core.base.componentuid import ComponentUID
    from pyomo.environ import value
    rows = contract.get('native_geometry')
    if not rows:
        raise RatingGeometryError('Rating lacks stage (b) native geometry identity evidence')
    for row in rows:
        if row.get('kind') == 'process_constraint_target':
            continue
        unit = units.get(row['unit_id'])
        var = ComponentUID(row['var_name']).find_component_on(unit) if unit is not None else None
        if var is not None and getattr(var, 'is_indexed', lambda: False)():
            candidates = list(var.values())
            var = candidates[0] if len(candidates) == 1 else None
        actual = value(var, exception=False) if var is not None else None
        expected = float(row['value'])
        if actual is None or not math.isfinite(float(actual)) or (not math.isclose(float(actual), expected, rel_tol=1e-12, abs_tol=1e-12)) or (not getattr(var, 'fixed', False)):
            raise RatingGeometryError(f'Rating geometry changed: {row['unit_id']} ({row['unit_type']}).{row['var_name']} [{row.get('unit', 'native model units')}] stage (b) value={expected}, solved={actual}, fixed={getattr(var, 'fixed', False)}. Return geometry changes to stage (b) sizing; this rating is invalid.')
