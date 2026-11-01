"""Flow-based KPI extraction built on the unified stream extractor."""
from __future__ import annotations
import ast
import sys
from typing import Any, Mapping
from pyomo.environ import value
from .streams import extract_flowsheet_streams
UNIT_KPI_NAMES: tuple[str, ...] = ('recovery_frac_mass_H2O', 'recovery_vol_phase', 'specific_energy_consumption', 'area', 'flux_mass_io_phase_comp', 'dens_mass_phase', 'work_mechanical', 'deltaP', 'efficiency_pump', 'recovery_frac', 'heat_duty')
PRODUCT_PORTS = {'outlet', 'treated', 'product', 'permeate', 'product_outlet'}
WASTE_PORTS = {'retentate', 'brine', 'reject', 'waste', 'byproduct', 'waste_outlet'}
INLET_PORTS = {'inlet', 'feed', 'feed_inlet', 'inlet_feed'}

def _index_parts(index_key: Any) -> tuple[str, ...]:
    if isinstance(index_key, tuple):
        return tuple((str(item) for item in index_key))
    text = str(index_key)
    if text.startswith('('):
        try:
            parsed = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            parsed = None
        if isinstance(parsed, tuple):
            return tuple((str(item) for item in parsed))
    return tuple((part.strip() for part in text.split(',') if part.strip()))

def component_mass_flows(stream_data: Mapping[str, Any]) -> dict[str, float]:
    """Return component mass flows from phase or no-phase stream data."""
    flows: dict[str, float] = {}
    raw = stream_data.get('flow_mass_comp')
    if isinstance(raw, Mapping):
        for key, value_obj in raw.items():
            parts = _index_parts(key)
            if not parts:
                continue
            comp = parts[-1]
            value_float = _float_or_none(value_obj)
            if value_float is not None:
                flows[comp] = flows.get(comp, 0.0) + value_float
    raw = stream_data.get('flow_mass_phase_comp')
    if isinstance(raw, Mapping):
        for key, value_obj in raw.items():
            parts = _index_parts(key)
            if len(parts) < 2:
                continue
            comp = parts[-1]
            value_float = _float_or_none(value_obj)
            if value_float is not None:
                flows[comp] = flows.get(comp, 0.0) + value_float
    return flows

def component_molar_flows(stream_data: Mapping[str, Any]) -> dict[str, float]:
    """Return component molar flows [mol basis] from phase, no-phase, or FTPx data.

    Handles flow_mol_phase_comp, flow_mol_comp, and the FTPx case
    (total ``flow_mol`` x ``mole_frac_comp``) used by natural_gas_PR / gas membranes.
    """
    flows: dict[str, float] = {}
    for var_name, min_parts in (('flow_mol_comp', 1), ('flow_mol_phase_comp', 2)):
        raw = stream_data.get(var_name)
        if isinstance(raw, Mapping):
            for key, value_obj in raw.items():
                parts = _index_parts(key)
                if len(parts) < min_parts:
                    continue
                comp = parts[-1]
                value_float = _float_or_none(value_obj)
                if value_float is not None:
                    flows[comp] = flows.get(comp, 0.0) + value_float
    if flows:
        return flows
    total = _float_or_none(stream_data.get('flow_mol'))
    fracs = stream_data.get('mole_frac_comp')
    if total is not None and isinstance(fracs, Mapping):
        for key, value_obj in fracs.items():
            comp = _index_parts(key)[-1]
            frac = _float_or_none(value_obj)
            if frac is not None:
                flows[comp] = flows.get(comp, 0.0) + total * frac
    return flows

def gas_mole_fraction(stream_data: Mapping[str, Any], component: str) -> float | None:
    """Mole fraction of a component in a gas stream (CH4 purity, CO2 fraction, ...)."""
    flows = component_molar_flows(stream_data)
    total = sum(flows.values())
    if total <= 0:
        return None
    return flows.get(component, 0.0) / total

def ch4_recovery(product_data: Mapping[str, Any], feed_data: Mapping[str, Any]) -> float | None:
    """CH4 recovery = CH4 molar flow in product / CH4 molar flow in feed."""
    feed_ch4 = component_molar_flows(feed_data).get('CH4', 0.0)
    if feed_ch4 <= 0:
        return None
    return component_molar_flows(product_data).get('CH4', 0.0) / feed_ch4

def co2_ch4_selectivity(permeate_data: Mapping[str, Any], feed_data: Mapping[str, Any]) -> float | None:
    """Apparent CO2/CH4 selectivity = (CO2/CH4)_permeate / (CO2/CH4)_feed."""
    perm = component_molar_flows(permeate_data)
    feed = component_molar_flows(feed_data)
    if perm.get('CH4', 0.0) <= 0 or feed.get('CO2', 0.0) <= 0 or feed.get('CH4', 0.0) <= 0:
        return None
    perm_ratio = perm.get('CO2', 0.0) / perm['CH4']
    feed_ratio = feed['CO2'] / feed['CH4']
    if feed_ratio <= 0:
        return None
    return perm_ratio / feed_ratio

def recycle_fraction(recycle_data: Mapping[str, Any], feed_data: Mapping[str, Any]) -> float | None:
    """Recycle ratio = recycle molar flow / fresh-feed molar flow."""
    feed_total = sum(component_molar_flows(feed_data).values())
    if feed_total <= 0:
        return None
    return sum(component_molar_flows(recycle_data).values()) / feed_total

def _float_or_none(value_obj: Any) -> float | None:
    if isinstance(value_obj, Mapping) and 'value' in value_obj:
        value_obj = value_obj['value']
    try:
        resolved = value(value_obj, exception=False)
    except Exception:
        resolved = value_obj
    if resolved is None:
        return None
    try:
        return float(resolved)
    except (TypeError, ValueError):
        return None

def stream_flow_kpis(stream_data: Mapping[str, Any]) -> dict[str, Any]:
    """Compute mass-flow KPIs for one stream."""
    component_flows = component_mass_flows(stream_data)
    total = sum(component_flows.values()) if component_flows else None
    return {'component_mass_flow_kg_s': component_flows, 'total_mass_flow_kg_s': total}

def enrich_streams_with_flow_kpis(unit_streams: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Add per-stream mass-flow KPI fields."""
    enriched: dict[str, dict[str, Any]] = {}
    inlet_total = _first_total(unit_streams, INLET_PORTS)
    for port_name, stream_data in unit_streams.items():
        payload = dict(stream_data)
        payload.update(stream_flow_kpis(payload))
        total = payload.get('total_mass_flow_kg_s')
        if inlet_total and total is not None:
            payload['fraction_of_inlet_mass'] = total / inlet_total
        enriched[port_name] = payload
    return enriched

def unit_flow_kpis(unit_streams: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Compute unit-level recovery and mass-balance KPIs from stream flows."""
    totals = {port_name: stream_flow_kpis(stream_data).get('total_mass_flow_kg_s') for port_name, stream_data in unit_streams.items()}
    inlet_total = _first_total(unit_streams, INLET_PORTS)
    product_total = sum((total or 0.0 for port_name, total in totals.items() if port_name in PRODUCT_PORTS))
    waste_total = sum((total or 0.0 for port_name, total in totals.items() if port_name in WASTE_PORTS))
    outlet_total = product_total + waste_total
    payload: dict[str, Any] = {'inlet_mass_flow_kg_s': inlet_total, 'product_mass_flow_kg_s': product_total if product_total else None, 'waste_mass_flow_kg_s': waste_total if waste_total else None}
    if inlet_total is not None:
        payload['outlet_mass_flow_kg_s'] = outlet_total
        payload['mass_balance_residual_kg_s'] = inlet_total - outlet_total
        payload['mass_recovery_frac'] = product_total / inlet_total if inlet_total else None
    rejection = _component_rejection(unit_streams)
    if rejection:
        payload['component_rejection_frac'] = rejection
    return {key: value for key, value in payload.items() if value is not None}

def _first_total(unit_streams: Mapping[str, Mapping[str, Any]], port_names: set[str]) -> float | None:
    for port_name, stream_data in unit_streams.items():
        if port_name not in port_names:
            continue
        total = stream_flow_kpis(stream_data).get('total_mass_flow_kg_s')
        if total is not None:
            return total
    return None

def _component_rejection(unit_streams: Mapping[str, Mapping[str, Any]]) -> dict[str, float]:
    inlet = _first_component_flows(unit_streams, INLET_PORTS)
    product = _combined_component_flows(unit_streams, PRODUCT_PORTS)
    rejection: dict[str, float] = {}
    for comp, inlet_flow in inlet.items():
        if comp in {'H2O', 'water'} or not inlet_flow:
            continue
        rejection[comp] = 1.0 - product.get(comp, 0.0) / inlet_flow
    return rejection

def _first_component_flows(unit_streams: Mapping[str, Mapping[str, Any]], port_names: set[str]) -> dict[str, float]:
    for port_name, stream_data in unit_streams.items():
        if port_name in port_names:
            return component_mass_flows(stream_data)
    return {}

def _combined_component_flows(unit_streams: Mapping[str, Mapping[str, Any]], port_names: set[str]) -> dict[str, float]:
    combined: dict[str, float] = {}
    for port_name, stream_data in unit_streams.items():
        if port_name not in port_names:
            continue
        for comp, flow in component_mass_flows(stream_data).items():
            combined[comp] = combined.get(comp, 0.0) + flow
    return combined

def extract_unit_metric_kpis(unit_block: Any) -> dict[str, Any]:
    """Extract common unit-level KPI variables from one unit block."""
    unit_kpis: dict[str, Any] = {}
    for kpi_name in UNIT_KPI_NAMES:
        kpi_var = getattr(unit_block, kpi_name, None)
        if kpi_var is None:
            continue
        try:
            if hasattr(kpi_var, '__iter__') and (not isinstance(kpi_var, str)):
                unit_kpis[kpi_name] = {str(idx): _float_or_none(kpi_var[idx]) for idx in kpi_var}
            else:
                unit_kpis[kpi_name] = _float_or_none(kpi_var)
        except Exception as exc:
            print(f'Warning: Failed to extract KPI {kpi_name}: {exc}', file=sys.stderr)
    return unit_kpis

def extract_solved_kpis(model: Any, units: Mapping[str, Any]) -> dict[str, Any]:
    """Extract JSON-serializable stream and unit KPIs from a solved model."""
    del model
    stream_values = extract_flowsheet_streams(units)
    kpis = {'streams': {}, 'units': {}}
    for unit_id, unit_block in units.items():
        unit_streams = stream_values.get(unit_id, {})
        if unit_streams:
            kpis['streams'][unit_id] = enrich_streams_with_flow_kpis(unit_streams)
        unit_kpis = {}
        if unit_streams:
            unit_kpis.update(unit_flow_kpis(unit_streams))
        unit_kpis.update(extract_unit_metric_kpis(unit_block))
        if unit_kpis:
            kpis['units'][unit_id] = unit_kpis
    return kpis
__all__ = ['UNIT_KPI_NAMES', 'component_mass_flows', 'enrich_streams_with_flow_kpis', 'extract_solved_kpis', 'extract_unit_metric_kpis', 'stream_flow_kpis', 'unit_flow_kpis']
