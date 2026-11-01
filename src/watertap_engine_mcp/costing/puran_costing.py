"""Flowsheet-native costing adapter: Puran detailed costing + equipment-list costing.

Two pieces live here:

1. ``PuranCostingDetailed`` — a thin subclass of WaterTAP's
   ``WaterTAPCostingDetailed`` that adds explicit ``contingency_frac_FCI`` and
   ``owners_cost_frac_FCI`` parameters (both default 0, explicit opt-in) and
   folds them into ``total_investment_factor`` the same way the parent folds
   land and working-capital %FCI fractions. No new cost correlations.

2. ``cost_equipment_list`` — costs a plain equipment list (no process model)
   by building a throwaway ``ConcreteModel`` with bare ``UnitModelBlock``
   shells carrying only the sizing variables each WaterTAP ``cost_*`` callable
   reads (the pattern used by WaterTAP's own costing tests,
   ``watertap/costing/tests/test_util.py``), attaching
   ``UnitModelCostingBlock`` per item, costing utility/chemical flows via
   ``register_flow_type`` / ``cost_flow``, and evaluating everything
   deterministically with ``calculate_variable_from_constraint`` (DOF is zero
   by construction — no solver is invoked). Results serialize to the
   ``engineering_utils.economics`` ``CostLedger``.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
import pyomo.environ as pyo
from puran_mcp_support.engineering.economics import CapexLine, CostLedger, OpexCategory, OpexLine
from idaes.core import FlowsheetBlock, UnitModelBlock, UnitModelCostingBlock, declare_process_block_class
from idaes.core.util.model_statistics import degrees_of_freedom
from pyomo.environ import units as pyunits
from pyomo.util.calc_var_value import calculate_variable_from_constraint
from watertap.costing.unit_models.cstr import cost_cstr
from watertap.costing.unit_models.heat_exchanger import cost_heat_exchanger
from watertap.costing.unit_models.mixer import cost_mixer
from watertap.costing.unit_models.nanofiltration import cost_nanofiltration
from watertap.costing.unit_models.pump import PumpType, cost_pump
from watertap.costing.unit_models.reverse_osmosis import ROType, cost_reverse_osmosis
from watertap.costing.watertap_costing_package import WaterTAPCostingDetailedData

@declare_process_block_class('PuranCostingDetailed')
class PuranCostingDetailedData(WaterTAPCostingDetailedData):
    """WaterTAPCostingDetailed plus explicit contingency and owner's-cost %FCI.

    ``WaterTAPCostingDetailed`` decomposes its investment factor as
    ``1 + working_capital_percent_FCI + land_cost_percent_FCI``. WaterTAP3's
    financials carry an explicit contingency %FCI line the same way (pattern:
    github.com/NatLabRockies/WaterTAP3, watertap3/utils/financials.py ~L218).
    This subclass adds two more %FCI fractions — contingency and owner's cost
    — both defaulting to 0 so they are explicit opt-ins, wired into the same
    ``total_investment_factor`` expression. No new unit-cost correlations.
    """

    def build_global_params(self):
        self.contingency_frac_FCI = pyo.Param(initialize=0.0, mutable=True, units=pyo.units.dimensionless, doc='Contingency as % FCI (explicit opt-in, default 0)')
        self.owners_cost_frac_FCI = pyo.Param(initialize=0.0, mutable=True, units=pyo.units.dimensionless, doc="Owner's cost as % FCI (explicit opt-in, default 0)")
        super().build_global_params()
        self.del_component(self.total_investment_factor)
        self.total_investment_factor = pyo.Expression(expr=1.0 + self.working_capital_percent_FCI + self.land_cost_percent_FCI + self.contingency_frac_FCI + self.owners_cost_frac_FCI, doc='Total investment factor [investment cost/equipment cost]')

    def build_process_costs(self):
        super().build_process_costs()
        self.contingency_cost = pyo.Expression(expr=self.contingency_frac_FCI * self.aggregate_capital_cost, doc='Contingency - based on aggregate capital costs')
        self.owners_cost = pyo.Expression(expr=self.owners_cost_frac_FCI * self.aggregate_capital_cost, doc="Owner's costs - based on aggregate capital costs")

def _attach_work_mechanical(unit: Any, value: float) -> None:
    fs = unit.flowsheet()
    unit.work_mechanical = pyo.Var(fs.time, initialize=value, units=pyo.units.W)
    unit.work_mechanical.fix(value)

def _attach_area(unit: Any, value: float) -> None:
    unit.area = pyo.Var(initialize=value, units=pyo.units.m ** 2)
    unit.area.fix(value)

def _attach_mixed_state_flow_vol(unit: Any, value: float) -> None:
    fs = unit.flowsheet()
    unit.mixed_state = pyo.Block(fs.time)
    for t in fs.time:
        unit.mixed_state[t].flow_vol = pyo.Var(initialize=value, units=pyo.units.m ** 3 / pyo.units.s)
        unit.mixed_state[t].flow_vol.fix(value)

def _attach_control_volume_volume(unit: Any, value: float) -> None:
    fs = unit.flowsheet()
    unit.control_volume = pyo.Block()
    unit.control_volume.volume = pyo.Var(fs.time, initialize=value, units=pyo.units.m ** 3)
    unit.control_volume.volume.fix(value)

@dataclass(frozen=True)
class SizingVarSpec:
    """One sizing input a costing method reads off the bare unit shell."""
    name: str
    canonical_units: str
    canonical_pyomo_units: Any
    attach: Callable[[Any, float], None]

@dataclass(frozen=True)
class MethodSpec:
    """A supported WaterTAP costing callable and the shell it needs."""
    costing_method: Callable
    sizing_vars: Tuple[SizingVarSpec, ...]
    method_arguments: Dict[str, Any] = field(default_factory=dict)
    doc: str = ''
_WORK_MECHANICAL = SizingVarSpec(name='work_mechanical', canonical_units='W', canonical_pyomo_units=pyunits.W, attach=_attach_work_mechanical)
_AREA = SizingVarSpec(name='area', canonical_units='m**2', canonical_pyomo_units=pyunits.m ** 2, attach=_attach_area)
_MIXED_FLOW_VOL = SizingVarSpec(name='flow_vol', canonical_units='m**3/s', canonical_pyomo_units=pyunits.m ** 3 / pyunits.s, attach=_attach_mixed_state_flow_vol)
_TANK_VOLUME = SizingVarSpec(name='volume', canonical_units='m**3', canonical_pyomo_units=pyunits.m ** 3, attach=_attach_control_volume_volume)
SUPPORTED_METHODS: Dict[str, MethodSpec] = {'pump_high_pressure': MethodSpec(costing_method=cost_pump, method_arguments={'pump_type': PumpType.high_pressure}, sizing_vars=(_WORK_MECHANICAL,), doc='watertap.costing.unit_models.pump.cost_pump (high_pressure); capital from mechanical work, electricity flow auto-costed'), 'reverse_osmosis': MethodSpec(costing_method=cost_reverse_osmosis, method_arguments={'ro_type': ROType.standard}, sizing_vars=(_AREA,), doc='watertap.costing.unit_models.reverse_osmosis.cost_reverse_osmosis (standard, via cost_membrane); capital + replacement from membrane area'), 'nanofiltration': MethodSpec(costing_method=cost_nanofiltration, sizing_vars=(_AREA,), doc='watertap.costing.unit_models.nanofiltration.cost_nanofiltration (via cost_membrane); capital + replacement from membrane area'), 'heat_exchanger': MethodSpec(costing_method=cost_heat_exchanger, sizing_vars=(_AREA,), doc='watertap.costing.unit_models.heat_exchanger.cost_heat_exchanger; capital from exchange area'), 'mixer': MethodSpec(costing_method=cost_mixer, sizing_vars=(_MIXED_FLOW_VOL,), doc='watertap.costing.unit_models.mixer.cost_mixer (default); capital from mixed volumetric flow'), 'cstr': MethodSpec(costing_method=cost_cstr, sizing_vars=(_TANK_VOLUME,), doc='watertap.costing.unit_models.cstr.cost_cstr; tank-style capital from vessel volume (power-law)')}
_UNIT_VOCAB: Dict[str, Any] = {'W': pyunits.W, 'kW': pyunits.kW, 'MW': pyunits.MW, 'hp': pyunits.hp, 'm**2': pyunits.m ** 2, 'm2': pyunits.m ** 2, 'ft**2': pyunits.ft ** 2, 'm**3': pyunits.m ** 3, 'm3': pyunits.m ** 3, 'L': pyunits.L, 'm**3/s': pyunits.m ** 3 / pyunits.s, 'm3/s': pyunits.m ** 3 / pyunits.s, 'm**3/hr': pyunits.m ** 3 / pyunits.hr, 'm3/hr': pyunits.m ** 3 / pyunits.hr, 'm**3/day': pyunits.m ** 3 / pyunits.day, 'm3/day': pyunits.m ** 3 / pyunits.day, 'L/s': pyunits.L / pyunits.s, 'kg/s': pyunits.kg / pyunits.s, 'kg/hr': pyunits.kg / pyunits.hr, 'kg/day': pyunits.kg / pyunits.day}
_PRICE_DENOMINATORS: Dict[str, Any] = {'kg': pyunits.kg, 'kWh': pyunits.kWh, 'm**3': pyunits.m ** 3, 'm3': pyunits.m ** 3, 'L': pyunits.L}
_SUPPORTED_COSTING_PARAMS = ('electricity_cost_usd_per_kwh', 'utilization_factor', 'wacc', 'plant_lifetime_years', 'contingency_frac_FCI', 'owners_cost_frac_FCI', 'land_cost_percent_FCI', 'working_capital_percent_FCI', 'TIC', 'TPEC', 'annual_production_m3')

class EquipmentListCostingError(ValueError):
    """Structured validation error for cost_equipment_list inputs."""

    def __init__(self, message: str, **details: Any):
        super().__init__(message)
        self.details = details

def _quantity(raw: Any, what: str) -> Tuple[float, Optional[str]]:
    """Parse a (value, units) input: number, [value, units], or {value, units}."""
    if isinstance(raw, (int, float)) and (not isinstance(raw, bool)):
        return (float(raw), None)
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return (float(raw[0]), str(raw[1]))
    if isinstance(raw, dict) and 'value' in raw:
        units = raw.get('units')
        return (float(raw['value']), None if units is None else str(units))
    raise EquipmentListCostingError(f"{what} must be a number, [value, units], or {{'value': ..., 'units': ...}}", received=raw)

def _convert_to_canonical(value: float, units: Optional[str], spec: SizingVarSpec) -> float:
    if units is None or units == spec.canonical_units:
        return value
    from_units = _UNIT_VOCAB.get(units)
    if from_units is None:
        raise EquipmentListCostingError(f"Unknown units '{units}' for sizing variable '{spec.name}'", supported_units=sorted(_UNIT_VOCAB))
    return pyunits.convert_value(value, from_units=from_units, to_units=spec.canonical_pyomo_units)

def _sanitize_component_name(tag: str, index: int) -> str:
    name = re.sub('\\W', '_', tag) or f'unit_{index}'
    if name[0].isdigit():
        name = f'u_{name}'
    return f'{name}_{index}'

def _initialize_block_from_constraints(block: Any) -> None:
    """Value same-named vars from `<name>_constraint` constraints (no solver)."""
    for _ in range(3):
        for constraint in block.component_objects(pyo.Constraint, descend_into=False):
            name = constraint.local_name
            if not name.endswith('_constraint'):
                continue
            target = getattr(block, name[:-len('_constraint')], None)
            if target is None:
                continue
            if constraint.is_indexed():
                if not hasattr(target, 'is_indexed') or not target.is_indexed():
                    continue
                for index in constraint:
                    calculate_variable_from_constraint(target[index], constraint[index])
            else:
                calculate_variable_from_constraint(target, constraint)

def evaluate_costing_block(costing: Any) -> None:
    """Deterministically value a costed flowsheet (DOF=0, no solver).

    Every unfixed costing variable has a same-named defining constraint, so
    repeated ``calculate_variable_from_constraint`` passes fully value the
    model: unit blocks first, then the flowsheet costing block (aggregates,
    totals, capital recovery factor).
    """
    for unit_costing in costing._registered_unit_costing:
        _initialize_block_from_constraints(unit_costing)
    _initialize_block_from_constraints(costing)
    costing.initialize()

def _base_year(costing: Any) -> Optional[int]:
    """Extract the base currency year from the costing block (e.g. USD_2018)."""
    match = re.search('(\\d{4})', str(costing.base_currency))
    return int(match.group(1)) if match else None

def _apply_costing_params(costing: Any, params: Dict[str, Any]) -> List[str]:
    unknown = [key for key in params if key not in _SUPPORTED_COSTING_PARAMS]
    if unknown:
        raise EquipmentListCostingError(f'Unknown costing_params key(s): {', '.join(sorted(unknown))}', supported_costing_params=list(_SUPPORTED_COSTING_PARAMS))
    assumptions: List[str] = []
    if 'electricity_cost_usd_per_kwh' in params:
        costing.electricity_cost.fix(float(params['electricity_cost_usd_per_kwh']))
    if 'utilization_factor' in params:
        costing.utilization_factor.fix(float(params['utilization_factor']))
    if 'wacc' in params:
        costing.wacc.fix(float(params['wacc']))
    if 'plant_lifetime_years' in params:
        costing.plant_lifetime.fix(float(params['plant_lifetime_years']))
    if 'contingency_frac_FCI' in params:
        costing.contingency_frac_FCI.set_value(float(params['contingency_frac_FCI']))
    if 'owners_cost_frac_FCI' in params:
        costing.owners_cost_frac_FCI.set_value(float(params['owners_cost_frac_FCI']))
    if 'land_cost_percent_FCI' in params:
        costing.land_cost_percent_FCI.fix(float(params['land_cost_percent_FCI']))
    if 'working_capital_percent_FCI' in params:
        costing.working_capital_percent_FCI.fix(float(params['working_capital_percent_FCI']))
    if 'TIC' in params:
        costing.TIC.fix(float(params['TIC']))
    if 'TPEC' in params:
        costing.TPEC.fix(float(params['TPEC']))
    for key in _SUPPORTED_COSTING_PARAMS:
        if key in params and key != 'annual_production_m3':
            assumptions.append(f'costing_params override: {key}={params[key]}')
    return assumptions

def _register_item_flows(costing: Any, tag: str, flows: List[Dict[str, Any]]) -> List[str]:
    assumptions: List[str] = []
    for flow in flows:
        if not isinstance(flow, dict) or 'name' not in flow or 'rate' not in flow:
            raise EquipmentListCostingError(f"Each flow on item '{tag}' needs at least 'name' and 'rate'", received=flow)
        name = str(flow['name'])
        rate_value, rate_units = _quantity(flow['rate'], f"flow '{name}' rate on '{tag}'")
        if rate_units is None:
            rate_units = flow.get('units')
        if name == 'electricity':
            rate_units = rate_units or 'kW'
        if rate_units is None:
            raise EquipmentListCostingError(f"Flow '{name}' on item '{tag}' needs rate units", supported_units=sorted(_UNIT_VOCAB) + ['kW'])
        pyomo_rate_units = _UNIT_VOCAB.get(str(rate_units))
        if pyomo_rate_units is None:
            raise EquipmentListCostingError(f"Unknown rate units '{rate_units}' for flow '{name}' on item '{tag}'", supported_units=sorted(_UNIT_VOCAB))
        already_priced = name == 'electricity' or costing.component(f'{name}_cost') is not None
        if not already_priced:
            if 'cost' not in flow or 'cost_units' not in flow:
                raise EquipmentListCostingError(f"Flow '{name}' on item '{tag}' needs 'cost' and 'cost_units' (e.g. cost_units='USD/kg') the first time it appears", received=flow)
            cost_units = str(flow['cost_units'])
            parts = cost_units.split('/', 1)
            if len(parts) != 2 or parts[0] != 'USD' or parts[1] not in _PRICE_DENOMINATORS:
                raise EquipmentListCostingError(f"Unknown cost_units '{cost_units}' for flow '{name}' on item '{tag}'", supported_cost_units=[f'USD/{d}' for d in sorted(_PRICE_DENOMINATORS)])
            price_units = costing.base_currency / _PRICE_DENOMINATORS[parts[1]]
            costing.register_flow_type(name, float(flow['cost']) * price_units)
            assumptions.append(f"flow '{name}' priced at {flow['cost']} {cost_units} (interpreted in {costing.base_currency})")
        costing.cost_flow(rate_value * pyomo_rate_units, name)
    return assumptions

def _error(message: str, **details: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {'error': message}
    payload.update(details)
    return payload

def cost_equipment_list(items: List[Dict[str, Any]], costing_params: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
    """Cost a bare equipment list with WaterTAP parametric cost models.

    Args:
        items: One dict per equipment item:
            ``{"tag": str, "costing_method": <SUPPORTED_METHODS key>,
            "sizing": {name: value | [value, units] | {"value","units"}},
            "flows": [{"name", "rate", "units", "cost", "cost_units"}, ...]}``
        costing_params: Optional global overrides (see error payload for the
            supported keys); ``annual_production_m3`` triggers LCOW via the
            costing block's own ``add_LCOW`` machinery.

    Returns:
        ``{"ledger": CostLedger dump, "lcow": float | None, "assumptions": [...],
        "totals": {...}, "evaluation": {...}}`` or a structured ``{"error": ...}``.
    """
    params = dict(costing_params or {})
    if not isinstance(items, list) or not items:
        return _error('items must be a non-empty list of equipment dicts', supported_methods=sorted(SUPPORTED_METHODS))
    try:
        m = pyo.ConcreteModel()
        m.fs = FlowsheetBlock(dynamic=False)
        m.fs.costing = PuranCostingDetailed()
        assumptions = _apply_costing_params(m.fs.costing, params)
        built: List[Dict[str, Any]] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                return _error(f'items[{index}] must be a dict', supported_methods=sorted(SUPPORTED_METHODS))
            tag = str(item.get('tag') or f'unit_{index}')
            method_key = item.get('costing_method')
            spec = SUPPORTED_METHODS.get(method_key)
            if spec is None:
                return _error(f"Unsupported costing_method '{method_key}' on item '{tag}'", supported_methods=sorted(SUPPORTED_METHODS), method_docs={key: ms.doc for key, ms in SUPPORTED_METHODS.items()})
            block_name = _sanitize_component_name(tag, index)
            setattr(m.fs, block_name, UnitModelBlock())
            unit = getattr(m.fs, block_name)
            sizing = item.get('sizing') or {}
            sizing_record: Dict[str, Any] = {}
            for sizing_var in spec.sizing_vars:
                if sizing_var.name not in sizing:
                    return _error(f"Item '{tag}' ({method_key}) is missing sizing variable '{sizing_var.name}'", required_sizing={sv.name: sv.canonical_units for sv in spec.sizing_vars})
                raw_value, raw_units = _quantity(sizing[sizing_var.name], f"sizing '{sizing_var.name}' on '{tag}'")
                canonical = _convert_to_canonical(raw_value, raw_units, sizing_var)
                sizing_var.attach(unit, canonical)
                sizing_record[sizing_var.name] = {'value': canonical, 'units': sizing_var.canonical_units}
            unit.costing = UnitModelCostingBlock(flowsheet_costing_block=m.fs.costing, costing_method=spec.costing_method, costing_method_arguments=spec.method_arguments)
            assumptions.extend(_register_item_flows(m.fs.costing, tag, item.get('flows') or []))
            built.append({'tag': tag, 'method': method_key, 'unit': unit, 'sizing': sizing_record})
        m.fs.costing.cost_process()
        dof = degrees_of_freedom(m)
        evaluate_costing_block(m.fs.costing)
        lcow = None
        annual_production = params.get('annual_production_m3')
        if annual_production:
            utilization = pyo.value(m.fs.costing.utilization_factor)
            design_flow = float(annual_production) / utilization
            m.fs.annual_water_production = pyo.Var(initialize=design_flow, units=pyo.units.m ** 3 / pyo.units.year)
            m.fs.annual_water_production.fix(design_flow)
            m.fs.costing.add_LCOW(m.fs.annual_water_production)
            lcow = pyo.value(m.fs.costing.LCOW)
            assumptions.append(f'LCOW basis: annual production {annual_production} m**3/yr (design flow backed out of utilization factor {utilization})')
    except EquipmentListCostingError as exc:
        return _error(str(exc), **exc.details)
    costing = m.fs.costing
    base_year = _base_year(costing)
    currency = str(costing.base_currency)
    capex_lines: List[CapexLine] = []
    for record in built:
        unit_costing = record['unit'].costing
        includes: Dict[str, Any] = {'direct_capital_cost_usd': pyo.value(unit_costing.direct_capital_cost), 'indirect_cost_factor': pyo.value(unit_costing.cost_factor)}
        if hasattr(unit_costing, 'fixed_operating_cost'):
            includes['fixed_operating_cost_usd_per_year'] = pyo.value(unit_costing.fixed_operating_cost)
        capex_lines.append(CapexLine(equipment_tag=record['tag'], description=f'WaterTAP parametric cost: {record['method']}', unit_cost_usd=pyo.value(unit_costing.capital_cost), cost_basis='installed', cost_source='parametric_model', base_year=base_year, includes=includes, sizing_params=record['sizing']))
    utilization = pyo.value(costing.utilization_factor)
    opex_lines: List[OpexLine] = []
    for flow_type in sorted(costing.used_flows):
        annual_at_full_use = pyo.value(pyo.units.convert(costing.aggregate_flow_costs[flow_type], to_units=costing.base_currency / pyo.units.year))
        category = OpexCategory.ENERGY if flow_type == 'electricity' else OpexCategory.CHEMICAL
        opex_lines.append(OpexLine(category=category, annual_usd=annual_at_full_use * utilization, basis={'flow_type': flow_type, 'aggregate_flow': pyo.value(getattr(costing, f'aggregate_flow_{flow_type}')), 'unit_price': pyo.value(getattr(costing, f'{flow_type}_cost')), 'utilization_factor': utilization, 'base_year': base_year}))
    ledger = CostLedger(capex_lines=capex_lines, opex_lines=opex_lines)
    totals = {'currency': currency, 'base_year': base_year, 'aggregate_capital_cost_usd': pyo.value(costing.aggregate_capital_cost), 'total_capital_cost_usd': pyo.value(costing.total_capital_cost), 'total_investment_factor': pyo.value(costing.total_investment_factor), 'contingency_cost_usd': pyo.value(costing.contingency_cost), 'owners_cost_usd': pyo.value(costing.owners_cost), 'land_cost_usd': pyo.value(costing.land_cost), 'working_capital_usd': pyo.value(costing.working_capital), 'total_operating_cost_usd_per_year': pyo.value(costing.total_operating_cost), 'total_fixed_operating_cost_usd_per_year': pyo.value(costing.total_fixed_operating_cost), 'total_variable_operating_cost_usd_per_year': pyo.value(costing.total_variable_operating_cost), 'maintenance_labor_chemical_operating_cost_usd_per_year': pyo.value(costing.maintenance_labor_chemical_operating_cost), 'capital_recovery_factor_per_year': pyo.value(costing.capital_recovery_factor), 'utilization_factor': utilization}
    assumptions.extend(["WaterTAP capital_cost is installed-basis (direct cost x TIC/TPEC indirect factor); CapexLine.cost_basis='installed'", "Ledger opex lines carry aggregate flow costs only (energy/chemical); fixed operating costs (membrane replacement, maintenance-labor-chemical %FCI) are reported under 'totals'", f'contingency_frac_FCI={pyo.value(costing.contingency_frac_FCI)}, owners_cost_frac_FCI={pyo.value(costing.owners_cost_frac_FCI)} folded into total_investment_factor (WaterTAP3-style %FCI lines)', 'Evaluated deterministically via calculate_variable_from_constraint (degrees of freedom = 0); no solver invoked'])
    return {'ledger': ledger.model_dump(mode='json'), 'lcow': lcow, 'assumptions': assumptions, 'totals': totals, 'evaluation': {'approach': 'calculate_variable_from_constraint', 'degrees_of_freedom': dof, 'solver_invoked': False}, 'units_costed': [{'tag': record['tag'], 'costing_method': record['method']} for record in built]}
