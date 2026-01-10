"""MVC Crystallizer Flowsheet Template.

Provides a pre-configured mechanical vapor compression (MVC)
crystallization system for:
- Zero liquid discharge (ZLD)
- Salt recovery
- Brine concentration
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.property_registry import PropertyPackageType


@dataclass
class MVCCrystallizerConfig:
    """Configuration for MVC crystallizer template."""

    # Property package - NACL recommended for crystallization
    property_package: PropertyPackageType = PropertyPackageType.NACL

    # Evaporator parameters
    evaporator_area_m2: float = 100.0
    evaporator_U: float = 1000.0  # W/m2/K
    delta_T_K: float = 5.0  # Temperature difference

    # Compressor parameters
    compressor_efficiency: float = 0.70
    pressure_ratio: float = 1.5

    # Crystallizer parameters
    crystallizer_volume_m3: float = 10.0
    crystal_growth_rate: float = 5e-9  # m/s

    # Operating temperature
    operating_temp_C: float = 70.0


@dataclass
class MVCCrystallizerUnits:
    """Units in the MVC crystallizer system."""
    feed: str = "Feed"
    feed_heater: str = "FeedHeater"
    evaporator: str = "Evaporator"
    compressor: str = "Compressor"
    condenser: str = "Condenser"
    crystallizer: str = "Crystallizer"
    distillate: str = "Distillate"
    crystals: str = "Crystals"


class MVCCrystallizerTemplate:
    """Template for MVC crystallizer flowsheet."""

    def __init__(self, config: Optional[MVCCrystallizerConfig] = None):
        """Initialize MVC crystallizer template.

        Args:
            config: Configuration options
        """
        self.config = config or MVCCrystallizerConfig()
        self.units = MVCCrystallizerUnits()

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
                "unit_id": self.units.feed_heater,
                "unit_type": "Heater",
                "config": {},
            },
            {
                "unit_id": self.units.evaporator,
                "unit_type": "Evaporator",
                "config": {},
            },
            {
                "unit_id": self.units.compressor,
                "unit_type": "Compressor",
                "config": {},
            },
            {
                "unit_id": self.units.condenser,
                "unit_type": "Condenser",
                "config": {},
            },
            {
                "unit_id": self.units.crystallizer,
                "unit_type": "Crystallization",
                "config": {},
            },
            {
                "unit_id": self.units.distillate,
                "unit_type": "Product",
                "config": {},
            },
            {
                "unit_id": self.units.crystals,
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
            # Feed path
            {
                "src_unit": self.units.feed,
                "src_port": "outlet",
                "dest_unit": self.units.feed_heater,
                "dest_port": "inlet",
            },
            {
                "src_unit": self.units.feed_heater,
                "src_port": "outlet",
                "dest_unit": self.units.evaporator,
                "dest_port": "feed",
            },
            # Vapor path
            {
                "src_unit": self.units.evaporator,
                "src_port": "vapor",
                "dest_unit": self.units.compressor,
                "dest_port": "inlet",
            },
            {
                "src_unit": self.units.compressor,
                "src_port": "outlet",
                "dest_unit": self.units.condenser,
                "dest_port": "inlet",
            },
            {
                "src_unit": self.units.condenser,
                "src_port": "outlet",
                "dest_unit": self.units.distillate,
                "dest_port": "inlet",
            },
            # Brine path
            {
                "src_unit": self.units.evaporator,
                "src_port": "brine",
                "dest_unit": self.units.crystallizer,
                "dest_port": "inlet",
            },
            {
                "src_unit": self.units.crystallizer,
                "src_port": "solid",
                "dest_unit": self.units.crystals,
                "dest_port": "inlet",
            },
        ]

    def get_dof_fixes(self) -> List[Dict[str, Any]]:
        """Get DOF fix specifications.

        Returns:
            List of variable fixes
        """
        operating_temp_K = self.config.operating_temp_C + 273.15

        return [
            # Feed heater
            {
                "unit_id": self.units.feed_heater,
                "var_name": "outlet.temperature[0]",
                "value": operating_temp_K - 10,  # Pre-heat to near operating
            },
            # Evaporator
            {
                "unit_id": self.units.evaporator,
                "var_name": "area",
                "value": self.config.evaporator_area_m2,
            },
            {
                "unit_id": self.units.evaporator,
                "var_name": "U",
                "value": self.config.evaporator_U,
            },
            {
                "unit_id": self.units.evaporator,
                "var_name": "outlet_brine.temperature[0]",
                "value": operating_temp_K,
            },
            # Compressor
            {
                "unit_id": self.units.compressor,
                "var_name": "efficiency",
                "value": self.config.compressor_efficiency,
            },
            {
                "unit_id": self.units.compressor,
                "var_name": "pressure_ratio",
                "value": self.config.pressure_ratio,
            },
            # Crystallizer
            {
                "unit_id": self.units.crystallizer,
                "var_name": "volume",
                "value": self.config.crystallizer_volume_m3,
            },
            {
                "unit_id": self.units.crystallizer,
                "var_name": "crystal_growth_rate",
                "value": self.config.crystal_growth_rate,
            },
        ]

    def get_scaling_factors(self) -> List[Dict[str, Any]]:
        """Get recommended scaling factors.

        Returns:
            List of scaling factor specifications
        """
        return [
            {
                "unit_id": self.units.evaporator,
                "var_name": "area",
                "factor": 1e-2,
            },
            {
                "unit_id": self.units.evaporator,
                "var_name": "U",
                "factor": 1e-3,
            },
            {
                "unit_id": self.units.crystallizer,
                "var_name": "volume",
                "factor": 0.1,
            },
            {
                "unit_id": self.units.crystallizer,
                "var_name": "crystal_growth_rate",
                "factor": 1e9,
            },
        ]

    def get_initialization_order(self) -> List[str]:
        """Get unit initialization order.

        Returns:
            List of unit IDs in init order
        """
        return [
            self.units.feed,
            self.units.feed_heater,
            self.units.evaporator,
            self.units.compressor,
            self.units.condenser,
            self.units.distillate,
            self.units.crystallizer,
            self.units.crystals,
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
