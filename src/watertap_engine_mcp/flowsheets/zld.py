"""Evaporator/crystallizer zero-liquid-discharge flowsheet template."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from watertap_engine_mcp.core.property_registry import PropertyPackageType
from watertap_engine_mcp.flowsheets.connections import connection

@dataclass
class ZLDConfig:
    """Configuration for an MVC evaporator plus NaCl crystallizer ZLD train."""
    property_package: PropertyPackageType = PropertyPackageType.CRYST_NACL
    evaporator_property_package: PropertyPackageType = PropertyPackageType.NACL_T_DEP
    evaporator_area_m2: float = 400.0
    heat_transfer_coeff_W_m2K: float = 1000.0
    brine_outlet_temp_K: float = 333.15
    compressor_efficiency: float = 0.8
    pressure_ratio: float = 2.0
    crystallizer_temperature_K: float = 328.15
    crystallization_yield: float = 0.4
    crystal_median_length_m: Optional[float] = None

@dataclass(frozen=True)
class ZLDTopologyTemplate:
    """Local universal-solver topology record for ZLD."""
    name: str = 'zld_evaporator_crystallizer'
    family: str = 'zld'
    implemented: bool = True
    mcp_template_name: str = 'mvc_crystallizer'
    recycle_variables: tuple = ()
    bounds: dict = None
    scaling_classes: tuple[str, ...] = ('mass_balance', 'thermal', 'crystallization')
    initializer: str = 'zld_mvc_crystallizer'

    def __post_init__(self) -> None:
        if self.bounds is None:
            object.__setattr__(self, 'bounds', {})

    def recycle_keys(self) -> tuple[str, ...]:
        return ()

    def get_expected_optimal_ranges(self, feedstock_class: Optional[str]=None) -> dict[str, tuple[float, float]]:
        return {}
ZLD_TOPOLOGY_ALIASES = {'zld_train', 'zld_evaporator_crystallizer', 'evaporator_crystallizer_zld', 'mvc_crystallizer'}

def get_zld_topology_template(name: str) -> Optional[ZLDTopologyTemplate]:
    """Return the local ZLD topology template for supported aliases."""
    normalized = name.strip().lower().replace('-', '_').replace(' ', '_')
    if normalized in ZLD_TOPOLOGY_ALIASES:
        return ZLDTopologyTemplate()
    return None

class EvaporatorCrystallizerZLDTemplate:
    """Session template for an MVC evaporator feeding a NaCl crystallizer."""
    UNIT_IDS = {'feed': 'Feed', 'evaporator': 'Evaporator', 'compressor': 'Compressor', 'condenser': 'Condenser', 'distillate': 'Distillate', 'crystallizer': 'Crystallizer', 'liquid': 'ResidualBrine', 'crystals': 'Crystals', 'vapor': 'CrystallizerVapor'}

    def __init__(self, config: Optional[ZLDConfig]=None):
        self.config = config or ZLDConfig()

    def get_units(self) -> List[Dict[str, Any]]:
        ids = self.UNIT_IDS
        evap_pkg = self.config.evaporator_property_package.name
        return [{'unit_id': ids['feed'], 'unit_type': 'Feed', 'config': {'property_package': evap_pkg}}, {'unit_id': ids['evaporator'], 'unit_type': 'Evaporator', 'config': {'property_package': evap_pkg}}, {'unit_id': ids['compressor'], 'unit_type': 'Compressor', 'config': {'property_package': 'WATER'}}, {'unit_id': ids['condenser'], 'unit_type': 'Condenser', 'config': {'property_package': 'WATER'}}, {'unit_id': ids['distillate'], 'unit_type': 'Product', 'config': {'property_package': 'WATER'}}, {'unit_id': ids['crystallizer'], 'unit_type': 'Crystallizer', 'config': {}}, {'unit_id': ids['liquid'], 'unit_type': 'Product', 'config': {}}, {'unit_id': ids['crystals'], 'unit_type': 'Product', 'config': {}}, {'unit_id': ids['vapor'], 'unit_type': 'Product', 'config': {}}]

    def get_connections(self) -> List[Dict[str, Any]]:
        ids = self.UNIT_IDS
        return [connection(ids['feed'], 'outlet', ids['evaporator'], 'inlet_feed'), connection(ids['evaporator'], 'outlet_vapor', ids['compressor'], 'inlet'), connection(ids['compressor'], 'outlet', ids['condenser'], 'inlet'), connection(ids['condenser'], 'outlet', ids['distillate'], 'inlet'), connection(ids['evaporator'], 'outlet_brine', ids['crystallizer'], 'inlet', auto_create_translator=True), connection(ids['crystallizer'], 'outlet', ids['liquid'], 'inlet'), connection(ids['crystallizer'], 'solids', ids['crystals'], 'inlet'), connection(ids['crystallizer'], 'vapor', ids['vapor'], 'inlet')]

    def get_dof_fixes(self) -> List[Dict[str, Any]]:
        ids = self.UNIT_IDS
        c = self.config
        fixes = [{'unit_id': ids['evaporator'], 'var_name': 'outlet_brine.temperature[0]', 'value': c.brine_outlet_temp_K}, {'unit_id': ids['evaporator'], 'var_name': 'U', 'value': c.heat_transfer_coeff_W_m2K}, {'unit_id': ids['evaporator'], 'var_name': 'area', 'value': c.evaporator_area_m2}, {'unit_id': ids['compressor'], 'var_name': 'pressure_ratio', 'value': c.pressure_ratio}, {'unit_id': ids['compressor'], 'var_name': 'efficiency', 'value': c.compressor_efficiency}, {'unit_id': ids['crystallizer'], 'var_name': 'temperature_operating', 'value': c.crystallizer_temperature_K}, {'unit_id': ids['crystallizer'], 'var_name': 'crystallization_yield[NaCl]', 'value': c.crystallization_yield}]
        if c.crystal_median_length_m is not None:
            fixes.append({'unit_id': ids['crystallizer'], 'var_name': 'crystal_median_length', 'value': c.crystal_median_length_m})
        return fixes

    def get_scaling_factors(self) -> List[Dict[str, Any]]:
        ids = self.UNIT_IDS
        return [{'unit_id': ids['evaporator'], 'var_name': 'area', 'factor': 0.01}, {'unit_id': ids['evaporator'], 'var_name': 'U', 'factor': 0.001}]

    def get_thermal_couplings(self) -> Dict[str, str]:
        ids = self.UNIT_IDS
        return {ids['evaporator']: ids['condenser']}

    def get_initialization_order(self) -> List[str]:
        ids = self.UNIT_IDS
        return [ids['feed'], ids['evaporator'], ids['compressor'], ids['condenser'], ids['distillate'], ids['crystallizer'], ids['liquid'], ids['crystals'], ids['vapor']]

    def to_session_spec(self) -> Dict[str, Any]:
        return {'property_package': self.config.property_package.name, 'units': self.get_units(), 'connections': self.get_connections(), 'dof_fixes': self.get_dof_fixes(), 'scaling_factors': self.get_scaling_factors(), 'thermal_couplings': self.get_thermal_couplings(), 'initialization_order': self.get_initialization_order()}
