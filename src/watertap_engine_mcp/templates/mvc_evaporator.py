"""MVC Evaporator Flowsheet Template.

Provides a pre-configured mechanical vapor compression (MVC) evaporator with:
- Feed unit (SEAWATER)
- Evaporator (SEAWATER feed/brine + WATER vapor, auto-split by engine)
- Compressor (WATER package)
- Condenser (WATER package)
- Distillate product (WATER -> SEAWATER via auto-translator)
- Brine product (SEAWATER)

Thermal coupling: Evaporator <-> Condenser via connect_to_condenser().
The template sets thermal_couplings so the engine uses explicit coupling
instead of the MVC heuristic fallback.

Validity: SEAWATER vapor-pressure correlations valid to 160 g/kg,
BPE to 120 g/kg.  Sufficient for MVC concentration (typical 70-120 g/kg)
but NOT for ZLD (>200 g/kg).  ZLD requires NACL_T_DEP or crystallizer.

Costing: Evaporator + Compressor are costable.  Condenser is non-costable --
compute_costing() returns partial results with condenser listed as
blocker/skipped.

Proven topology: test_public_mvc_translator_chain_solves (test_public_workflows.py:2047).
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from watertap_engine_mcp.core.property_registry import PropertyPackageType

@dataclass
class MVCEvaporatorConfig:
    """Configuration for MVC evaporator template."""
    property_package: PropertyPackageType = PropertyPackageType.SEAWATER
    brine_outlet_temp_K: float = 333.15
    heat_transfer_coeff_W_m2K: float = 1000.0
    evaporator_area_m2: float = 400.0
    target_brine_mass_fraction: Optional[float] = None
    feed_preheat_approach_K: float = 10.0
    pressure_ratio: float = 2.0
    compressor_efficiency: float = 0.8

    def __post_init__(self):
        if self.target_brine_mass_fraction is not None and (not 0 < self.target_brine_mass_fraction <= 0.12):
            raise ValueError('SEAWATER BPE design endpoint must be in (0, 0.12] kg TDS/kg solution')
        if not 0 < self.feed_preheat_approach_K <= 10:
            raise ValueError('feed_preheat_approach_K must be in (0, 10] K')

class MVCEvaporatorTemplate:
    """Template for MVC evaporator flowsheet."""
    UNIT_IDS = {'feed': 'Feed', 'evaporator': 'Evaporator', 'compressor': 'Compressor', 'condenser': 'Condenser', 'distillate': 'Distillate', 'brine': 'Brine'}

    def __init__(self, config: Optional[MVCEvaporatorConfig]=None):
        self.config = config or MVCEvaporatorConfig()

    def get_units(self) -> List[Dict[str, Any]]:
        ids = self.UNIT_IDS
        return [{'unit_id': ids['feed'], 'unit_type': 'Feed', 'config': {}}, {'unit_id': ids['evaporator'], 'unit_type': 'Evaporator', 'config': {}}, {'unit_id': ids['compressor'], 'unit_type': 'Compressor', 'config': {'property_package': 'WATER'}}, {'unit_id': ids['condenser'], 'unit_type': 'Condenser', 'config': {'property_package': 'WATER'}}, {'unit_id': ids['distillate'], 'unit_type': 'Product', 'config': {}}, {'unit_id': ids['brine'], 'unit_type': 'Product', 'config': {}}]

    def get_connections(self) -> List[Dict[str, str]]:
        ids = self.UNIT_IDS
        return [{'src_unit': ids['feed'], 'src_port': 'outlet', 'dest_unit': ids['evaporator'], 'dest_port': 'inlet_feed'}, {'src_unit': ids['evaporator'], 'src_port': 'outlet_vapor', 'dest_unit': ids['compressor'], 'dest_port': 'inlet'}, {'src_unit': ids['compressor'], 'src_port': 'outlet', 'dest_unit': ids['condenser'], 'dest_port': 'inlet'}, {'src_unit': ids['condenser'], 'src_port': 'outlet', 'dest_unit': ids['distillate'], 'dest_port': 'inlet', 'auto_create_translator': True}, {'src_unit': ids['evaporator'], 'src_port': 'outlet_brine', 'dest_unit': ids['brine'], 'dest_port': 'inlet'}]

    def get_dof_fixes(self) -> List[Dict[str, Any]]:
        ids = self.UNIT_IDS
        c = self.config
        return [{'unit_id': ids['evaporator'], 'var_name': 'outlet_brine.temperature[0]', 'value': c.brine_outlet_temp_K}, {'unit_id': ids['evaporator'], 'var_name': 'U', 'value': c.heat_transfer_coeff_W_m2K}, {'unit_id': ids['evaporator'], 'var_name': 'area', 'value': c.evaporator_area_m2} if c.target_brine_mass_fraction is None else {'unit_id': ids['evaporator'], 'var_name': 'properties_brine[0].mass_frac_phase_comp[Liq,TDS]', 'value': c.target_brine_mass_fraction}, {'unit_id': ids['compressor'], 'var_name': 'pressure_ratio', 'value': c.pressure_ratio}, {'unit_id': ids['compressor'], 'var_name': 'efficiency', 'value': c.compressor_efficiency}]

    def get_scaling_factors(self) -> List[Dict[str, Any]]:
        ids = self.UNIT_IDS
        return [{'unit_id': ids['evaporator'], 'var_name': 'area', 'factor': 0.01}, {'unit_id': ids['evaporator'], 'var_name': 'U', 'factor': 0.001}]

    def get_thermal_couplings(self) -> Dict[str, str]:
        """Explicit thermal coupling: Evaporator <-> Condenser."""
        ids = self.UNIT_IDS
        return {ids['evaporator']: ids['condenser']}

    def get_initialization_order(self) -> List[str]:
        ids = self.UNIT_IDS
        return [ids['feed'], ids['evaporator'], ids['compressor'], ids['condenser'], ids['distillate'], ids['brine']]

    def to_session_spec(self) -> Dict[str, Any]:
        return {'property_package': self.config.property_package.name, 'units': self.get_units(), 'connections': self.get_connections(), 'dof_fixes': self.get_dof_fixes(), 'scaling_factors': self.get_scaling_factors(), 'thermal_couplings': self.get_thermal_couplings(), 'initialization_order': self.get_initialization_order()}
