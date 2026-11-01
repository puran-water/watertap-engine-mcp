"""Complete, non-executable inventory of the model that the producer solved.

Unit snapshots intentionally serve stream/twin consumers. They cannot establish
which flowsheet controls, objectives or active equations produced those streams.
This companion records the actual final model, including inactive components;
it never rebuilds equations from answers or certifies a treatment workbook.
"""
from __future__ import annotations
import hashlib
import importlib.metadata
import json
import math

def _number(expression):
    from pyomo.environ import value
    result = value(expression, exception=False)
    if result is None:
        return None
    if isinstance(result, (int, float)) and math.isfinite(result):
        return float(result)
    raise ValueError('model state requires finite numeric component values')

def _expression(expression):
    if expression is None:
        return None
    from pyomo.core.expr.visitor import expression_to_string
    return expression_to_string(expression, verbose=True, compute_values=False)

def _effective_active(component):
    if not component.active:
        return False
    parent = component.parent_block()
    while parent is not None:
        if not parent.active:
            return False
        parent = parent.parent_block()
    return True

def capture_model_state(model):
    """Capture every final variable/parameter/equation and its activation state.

    Values are allowed to be null for unused, uninitialized components. The
    readiness inventory explicitly reports missing active decision values.
    Optimization, inequality and non-square counts are retained, not removed
    to satisfy the equality workbook compiler. The content hash binds this
    evidence record; it is not a protected engineering result or receipt.
    """
    from pyomo.core.base.component_namer import index_repr
    from pyomo.core.expr.visitor import identify_variables
    from pyomo.environ import Block, Constraint, Expression, Objective, Param, Suffix, Var
    from watertap_engine_mcp.results.solved_state import _safe_units, _var_snapshot
    variables = {v.name: _var_snapshot(v) | {'domain': str(v.domain), 'lower_expression': _expression(v.lower), 'upper_expression': _expression(v.upper)} for v in model.component_data_objects(Var, active=None, descend_into=True)}
    parameters = {}
    for component in model.component_objects(Param, active=None, descend_into=True):
        for index, number in component.items():
            name = component.name + (index_repr(index) if component.is_indexed() else '')
            parameters[name] = {'value': _number(number), 'mutable': bool(component.mutable), 'units': _safe_units(component), 'domain': str(component.domain)}
    blocks = {b.name: bool(b.active) for b in model.component_data_objects(Block, active=None, descend_into=True)}
    expressions = {e.name: _expression(e.expr) for e in model.component_data_objects(Expression, active=None, descend_into=True)}
    constraints, objectives, incident = ({}, {}, set())
    active_equalities, active_inequalities = ([], [])
    for c in model.component_data_objects(Constraint, active=None, descend_into=True):
        active = _effective_active(c)
        constraints[c.name] = {'active': bool(c.active), 'effective_active': active, 'equality': bool(c.equality), 'body': _expression(c.body), 'lower': _expression(c.lower), 'upper': _expression(c.upper), 'body_value': _number(c.body), 'lower_value': _number(c.lower), 'upper_value': _number(c.upper)}
        if active:
            (active_equalities if c.equality else active_inequalities).append(c.name)
            for expression in (c.body, c.lower, c.upper):
                if expression is not None:
                    incident.update((v.name for v in identify_variables(expression, include_fixed=False)))
    objective_variables = set()
    for o in model.component_data_objects(Objective, active=None, descend_into=True):
        active = _effective_active(o)
        objectives[o.name] = {'active': bool(o.active), 'effective_active': active, 'sense': str(o.sense), 'expression': _expression(o.expr), 'value': _number(o.expr)}
        if active:
            objective_variables.update((v.name for v in identify_variables(o.expr, include_fixed=False)))
    suffixes = {}
    for suffix in model.component_objects(Suffix, active=None, descend_into=True):
        suffixes[suffix.name] = {str(getattr(component, 'name', component)): _number(number) for component, number in suffix.items()}
    active_objectives = [name for name, item in objectives.items() if item['effective_active']]
    missing = sorted((name for name in incident | objective_variables if variables[name]['value'] is None))
    reasons = []
    if active_objectives:
        reasons.append('active optimization objectives require their complete optimization representation')
    if active_inequalities:
        reasons.append('active inequalities cannot be discarded by an equality workbook')
    if len(incident) != len(active_equalities):
        reasons.append('active equation incidence is not square')
    if missing:
        reasons.append('active decision variables have no numeric solution')
    payload = {'schema_version': 'watertap_full_model_state/v1', 'model_name': model.name, 'model_active': bool(model.active), 'blocks': blocks, 'variables': variables, 'parameters': parameters, 'expressions': expressions, 'constraints': constraints, 'objectives': objectives, 'suffixes': suffixes, 'runtime_versions': {name: importlib.metadata.version(name) for name in ('pyomo', 'idaes-pse', 'watertap')}, 'equation_inventory': {'active_equalities': active_equalities, 'active_inequalities': active_inequalities, 'active_objectives': active_objectives, 'incident_decision_variables': sorted(incident), 'objective_decision_variables': sorted(objective_variables), 'missing_decision_values': missing, 'equality_export_obstructions': reasons}, 'governing_eligible': False, 'qualification': 'complete model-state inventory only; no independent workbook or treatment receipt'}
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return payload | {'content_sha256': hashlib.sha256(encoded).hexdigest()}

def compare_model_structure(model, snapshot):
    """Report reconstruction differences before anyone uses a rebuilt model.

    This read-only comparison never transfers solved outputs into fixed inputs.
    Values/staleness are excluded; fixedness, bounds, domains, expressions,
    activation, objective sense and parameter values are compared explicitly.
    """
    expected = dict(snapshot)
    digest = expected.pop('content_sha256')
    encoded = json.dumps(expected, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    if hashlib.sha256(encoded).hexdigest() != digest:
        raise ValueError('full model-state content hash mismatch')
    actual = capture_model_state(model)
    differences = []
    for section in ('blocks', 'variables', 'parameters', 'expressions', 'constraints', 'objectives', 'suffixes'):
        before, after = (expected[section], actual[section])
        for name in sorted(set(before) | set(after)):
            if name not in before or name not in after:
                differences.append({'section': section, 'component': name, 'difference': 'component inventory changed'})
                continue
            old, new = (before[name], after[name])
            if section == 'variables':
                old = {k: v for k, v in old.items() if k not in {'value', 'stale'}}
                new = {k: v for k, v in new.items() if k not in {'value', 'stale'}}
            elif section in {'constraints', 'objectives'}:
                old = {k: v for k, v in old.items() if k not in {'value', 'body_value', 'lower_value', 'upper_value'}}
                new = {k: v for k, v in new.items() if k not in {'value', 'body_value', 'lower_value', 'upper_value'}}
            if old != new:
                differences.append({'section': section, 'component': name, 'difference': 'definition or governing state changed'})
    if expected['model_active'] != actual['model_active']:
        differences.append({'section': 'model', 'component': model.name, 'difference': 'model activation changed'})
    return {'matches': not differences, 'differences': differences}
