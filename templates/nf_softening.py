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

from core.property_registry import PropertyPackageType


@dataclass
class NFSofteningConfig:
    """Configuration for NF softening template."""

    # Property package - MCAS recommended for ion-specific rejection
    property_package: PropertyPackageType = PropertyPackageType.MCAS

    # Membrane parameters
    membrane_area_m2: float = 100.0
    n_stages: int = 1

    # Operating conditions
    feed_pressure_bar: float = 10.0  # NF operates at lower pressure than RO
    permeate_pressure_bar: float = 1.01325

    # Pump efficiency
    pump_efficiency: float = 0.80

    # Target hardness removal (for guidance)
    target_hardness_removal: float = 0.90


@dataclass
class NFSofteningUnits:
    """Units in the NF softening system."""
    feed: str = "Feed"
    pump: str = "NFPump"
    nf: str = "NF"
    permeate: str = "SoftenedWater"
    concentrate: str = "Concentrate"


class NFSofteningTemplate:
    """Template for NF softening flowsheet."""

    def __init__(self, config: Optional[NFSofteningConfig] = None):
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
        return [
            {
                "unit_id": self.units.feed,
                "unit_type": "Feed",
                "config": {},
            },
            {
                "unit_id": self.units.pump,
                "unit_type": "Pump",
                "config": {},
            },
            {
                "unit_id": self.units.nf,
                "unit_type": "Nanofiltration0D",
                "config": {
                    "has_pressure_change": True,
                },
            },
            {
                "unit_id": self.units.permeate,
                "unit_type": "Product",
                "config": {},
            },
            {
                "unit_id": self.units.concentrate,
                "unit_type": "Product",
                "config": {},
            },
        ]

    def get_connections(self) -> List[Dict[str, str]]:
        """Get list of connections.

        Returns:
            List of connection specifications
        """
        return [
            {
                "src_unit": self.units.feed,
                "src_port": "outlet",
                "dest_unit": self.units.pump,
                "dest_port": "inlet",
            },
            {
                "src_unit": self.units.pump,
                "src_port": "outlet",
                "dest_unit": self.units.nf,
                "dest_port": "inlet",
            },
            {
                "src_unit": self.units.nf,
                "src_port": "permeate",
                "dest_unit": self.units.permeate,
                "dest_port": "inlet",
            },
            {
                "src_unit": self.units.nf,
                "src_port": "retentate",
                "dest_unit": self.units.concentrate,
                "dest_port": "inlet",
            },
        ]

    def get_dof_fixes(self) -> List[Dict[str, Any]]:
        """Get DOF fix specifications.

        Returns:
            List of variable fixes
        """
        return [
            # Pump
            {
                "unit_id": self.units.pump,
                "var_name": "efficiency_pump",
                "value": self.config.pump_efficiency,
            },
            {
                "unit_id": self.units.pump,
                "var_name": "outlet.pressure[0]",
                "value": self.config.feed_pressure_bar * 1e5,
            },
            # NF membrane - typical values for softening
            {
                "unit_id": self.units.nf,
                "var_name": "area",
                "value": self.config.membrane_area_m2,
            },
            {
                "unit_id": self.units.nf,
                "var_name": "permeate.pressure[0]",
                "value": self.config.permeate_pressure_bar * 1e5,
            },
        ]

    def get_scaling_factors(self) -> List[Dict[str, Any]]:
        """Get recommended scaling factors.

        Returns:
            List of scaling factor specifications
        """
        return [
            {
                "unit_id": self.units.pump,
                "var_name": "outlet.pressure",
                "factor": 1e-5,
            },
            {
                "unit_id": self.units.nf,
                "var_name": "area",
                "factor": 1e-2,
            },
        ]

    def get_initialization_order(self) -> List[str]:
        """Get unit initialization order.

        Returns:
            List of unit IDs in init order
        """
        return [
            self.units.feed,
            self.units.pump,
            self.units.nf,
            self.units.permeate,
            self.units.concentrate,
        ]

    def to_session_spec(self) -> Dict[str, Any]:
        """Convert template to session specification.

        Returns:
            Dict with complete session spec
        """
        return {
            "property_package": self.config.property_package.name,
            "units": self.get_units(),
            "connections": self.get_connections(),
            "dof_fixes": self.get_dof_fixes(),
            "scaling_factors": self.get_scaling_factors(),
            "initialization_order": self.get_initialization_order(),
        }
