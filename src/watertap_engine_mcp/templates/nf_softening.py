"""NF Softening Flowsheet Template.

Provides a pre-configured nanofiltration softening system for:
- Hardness removal (Ca, Mg)
- Partial desalination
- Low-pressure operation
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from watertap_engine_mcp.core.property_registry import PropertyPackageType
DEFAULT_NF_MCAS_CONFIG = {'solute_list': ['Na_+', 'Ca_2+', 'Mg_2+', 'Cl_-', 'SO4_2-'], 'charge': {'Na_+': 1, 'Ca_2+': 2, 'Mg_2+': 2, 'Cl_-': -1, 'SO4_2-': -2}, 'mw_data': {'Na_+': 0.02299, 'Ca_2+': 0.040078, 'Mg_2+': 0.024305, 'Cl_-': 0.03545, 'SO4_2-': 0.09606}}

@dataclass
class NFSofteningConfig:
    """Configuration for NF softening template."""
    property_package: PropertyPackageType = PropertyPackageType.MCAS
    membrane_area_m2: float = 500.0
    n_stages: int = 1
    feed_pressure_bar: float = 4.0
    permeate_pressure_bar: float = 1.01325
    pump_efficiency: float = 0.8
    target_hardness_removal: float = 0.9
    property_package_config: Dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_NF_MCAS_CONFIG))

@dataclass
class NFSofteningUnits:
    """Units in the NF softening system."""
    feed: str = 'Feed'
    pump: str = 'NFPump'
    nf: str = 'NF'
    permeate: str = 'SoftenedWater'
    concentrate: str = 'Concentrate'

class NFSofteningTemplate:
    """Template for NF softening flowsheet."""

    def __init__(self, config: Optional[NFSofteningConfig]=None):
        """Initialize NF softening template.

        Args:
            config: Configuration options
        """
        self.config = config or NFSofteningConfig()
        self.units = NFSofteningUnits()

    def get_units(self) -> List[Dict[str, Any]]:
        """Get list of units to create.

        Returns:
            List of unit specifications
        """
        return [{'unit_id': self.units.feed, 'unit_type': 'Feed', 'config': {}}, {'unit_id': self.units.pump, 'unit_type': 'Pump', 'config': {}}, {'unit_id': self.units.nf, 'unit_type': 'Nanofiltration0D', 'config': {'has_pressure_change': True}}, {'unit_id': self.units.permeate, 'unit_type': 'Product', 'config': {}}, {'unit_id': self.units.concentrate, 'unit_type': 'Product', 'config': {}}]

    def get_connections(self) -> List[Dict[str, str]]:
        """Get list of connections.

        Returns:
            List of connection specifications
        """
        return [{'src_unit': self.units.feed, 'src_port': 'outlet', 'dest_unit': self.units.pump, 'dest_port': 'inlet'}, {'src_unit': self.units.pump, 'src_port': 'outlet', 'dest_unit': self.units.nf, 'dest_port': 'inlet'}, {'src_unit': self.units.nf, 'src_port': 'permeate', 'dest_unit': self.units.permeate, 'dest_port': 'inlet'}, {'src_unit': self.units.nf, 'src_port': 'retentate', 'dest_unit': self.units.concentrate, 'dest_port': 'inlet'}]

    def get_dof_fixes(self) -> List[Dict[str, Any]]:
        """Get DOF fix specifications.

        Returns:
            List of variable fixes
        """
        return [{'unit_id': self.units.pump, 'var_name': 'efficiency_pump[0]', 'value': self.config.pump_efficiency}, {'unit_id': self.units.pump, 'var_name': 'control_volume.properties_out[0].pressure', 'value': self.config.feed_pressure_bar * 100000.0}, {'unit_id': self.units.nf, 'var_name': 'recovery_solvent[0]', 'value': 0.8}, {'unit_id': self.units.nf, 'var_name': 'rejection_comp[0,Na_+]', 'value': 0.9}, {'unit_id': self.units.nf, 'var_name': 'rejection_comp[0,Ca_2+]', 'value': 0.9}, {'unit_id': self.units.nf, 'var_name': 'rejection_comp[0,Mg_2+]', 'value': 0.9}, {'unit_id': self.units.nf, 'var_name': 'rejection_comp[0,SO4_2-]', 'value': 0.9}, {'unit_id': self.units.nf, 'var_name': 'area', 'value': self.config.membrane_area_m2}, {'unit_id': self.units.nf, 'var_name': 'permeate.pressure[0]', 'value': self.config.permeate_pressure_bar * 100000.0}, {'unit_id': self.units.nf, 'var_name': 'deltaP[0]', 'value': -10000.0}]

    def get_scaling_factors(self) -> List[Dict[str, Any]]:
        """Get recommended scaling factors.

        Returns:
            List of scaling factor specifications
        """
        return [{'unit_id': self.units.pump, 'var_name': 'control_volume.properties_out[0].pressure', 'factor': 1e-05}, {'unit_id': self.units.nf, 'var_name': 'area', 'factor': 0.01}]

    def get_initialization_order(self) -> List[str]:
        """Get unit initialization order.

        Returns:
            List of unit IDs in init order
        """
        return [self.units.feed, self.units.pump, self.units.nf, self.units.permeate, self.units.concentrate]

    def to_session_spec(self) -> Dict[str, Any]:
        """Convert template to session specification.

        Returns:
            Dict with complete session spec
        """
        return {'property_package': self.config.property_package.name, 'property_package_config': self.config.property_package_config, 'units': self.get_units(), 'connections': self.get_connections(), 'dof_fixes': self.get_dof_fixes(), 'scaling_factors': self.get_scaling_factors(), 'initialization_order': self.get_initialization_order()}
