"""RO Train Flowsheet Template.

Provides a pre-configured RO desalination train with:
- Feed pump
- High-pressure pump
- RO membrane
- Energy recovery device (optional)
- Permeate and brine outlets
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.property_registry import PropertyPackageType


@dataclass
class ROTrainConfig:
    """Configuration for RO train template."""

    # Property package (SEAWATER or NACL recommended)
    property_package: PropertyPackageType = PropertyPackageType.SEAWATER

    # Membrane parameters
    membrane_type: str = "SWRO"  # SWRO, BWRO, NF
    membrane_area_m2: float = 50.0
    n_stages: int = 1

    # Operating conditions
    feed_pressure_bar: float = 60.0
    permeate_pressure_bar: float = 1.01325

    # Typical membrane parameters for SWRO
    A_comp: float = 4.2e-12  # m/s/Pa
    B_comp: float = 3.5e-8   # m/s

    # Include ERD
    include_erd: bool = True
    erd_efficiency: float = 0.95

    # Pump efficiencies
    hp_pump_efficiency: float = 0.80
    booster_pump_efficiency: float = 0.80


@dataclass
class ROTrainUnits:
    """Units in the RO train."""
    feed: str = "Feed"
    hp_pump: str = "HPPump"
    ro: str = "RO"
    permeate: str = "Permeate"
    brine: str = "Brine"
    erd: Optional[str] = "ERD"
    booster: Optional[str] = "Booster"


class ROTrainTemplate:
    """Template for RO desalination train flowsheet."""

    def __init__(self, config: Optional[ROTrainConfig] = None):
        """Initialize RO train template.

        Args:
            config: Configuration options
        """
        self.config = config or ROTrainConfig()
        self.units = ROTrainUnits()

    def get_units(self) -> List[Dict[str, Any]]:
        """Get list of units to create.

        Returns:
            List of unit specifications
        """
        units = [
            {
                "unit_id": self.units.feed,
                "unit_type": "Feed",
                "config": {},
            },
            {
                "unit_id": self.units.hp_pump,
                "unit_type": "Pump",
                "config": {},
            },
            {
                "unit_id": self.units.ro,
                "unit_type": "ReverseOsmosis0D",
                "config": {
                    "has_pressure_change": True,
                    "concentration_polarization_type": "calculated",
                    "mass_transfer_coefficient": "calculated",
                },
            },
            {
                "unit_id": self.units.permeate,
                "unit_type": "Product",
                "config": {},
            },
            {
                "unit_id": self.units.brine,
                "unit_type": "Product",
                "config": {},
            },
        ]

        if self.config.include_erd:
            units.extend([
                {
                    "unit_id": self.units.erd,
                    "unit_type": "PressureExchanger",
                    "config": {},
                },
                {
                    "unit_id": self.units.booster,
                    "unit_type": "Pump",
                    "config": {},
                },
            ])

        return units

    def get_connections(self) -> List[Dict[str, str]]:
        """Get list of connections.

        Returns:
            List of connection specifications
        """
        connections = [
            {
                "src_unit": self.units.feed,
                "src_port": "outlet",
                "dest_unit": self.units.hp_pump,
                "dest_port": "inlet",
            },
            {
                "src_unit": self.units.hp_pump,
                "src_port": "outlet",
                "dest_unit": self.units.ro,
                "dest_port": "inlet",
            },
            {
                "src_unit": self.units.ro,
                "src_port": "permeate",
                "dest_unit": self.units.permeate,
                "dest_port": "inlet",
            },
        ]

        if self.config.include_erd:
            # With ERD: brine -> ERD -> booster -> back to system
            connections.extend([
                {
                    "src_unit": self.units.ro,
                    "src_port": "retentate",
                    "dest_unit": self.units.erd,
                    "dest_port": "high_pressure_inlet",
                },
                {
                    "src_unit": self.units.erd,
                    "src_port": "high_pressure_outlet",
                    "dest_unit": self.units.brine,
                    "dest_port": "inlet",
                },
            ])
        else:
            # Without ERD: brine goes directly to product
            connections.append({
                "src_unit": self.units.ro,
                "src_port": "retentate",
                "dest_unit": self.units.brine,
                "dest_port": "inlet",
            })

        return connections

    def get_dof_fixes(self) -> List[Dict[str, Any]]:
        """Get DOF fix specifications.

        Returns:
            List of variable fixes
        """
        fixes = [
            # HP Pump
            {
                "unit_id": self.units.hp_pump,
                "var_name": "efficiency_pump",
                "value": self.config.hp_pump_efficiency,
            },
            {
                "unit_id": self.units.hp_pump,
                "var_name": "outlet.pressure[0]",
                "value": self.config.feed_pressure_bar * 1e5,  # bar -> Pa
            },
            # RO membrane
            {
                "unit_id": self.units.ro,
                "var_name": "A_comp[0,H2O]",
                "value": self.config.A_comp,
            },
            {
                "unit_id": self.units.ro,
                "var_name": "B_comp[0,TDS]",
                "value": self.config.B_comp,
            },
            {
                "unit_id": self.units.ro,
                "var_name": "area",
                "value": self.config.membrane_area_m2,
            },
            {
                "unit_id": self.units.ro,
                "var_name": "permeate.pressure[0]",
                "value": self.config.permeate_pressure_bar * 1e5,
            },
        ]

        if self.config.include_erd:
            fixes.extend([
                {
                    "unit_id": self.units.erd,
                    "var_name": "efficiency_pressure_exchanger",
                    "value": self.config.erd_efficiency,
                },
                {
                    "unit_id": self.units.booster,
                    "var_name": "efficiency_pump",
                    "value": self.config.booster_pump_efficiency,
                },
            ])

        return fixes

    def get_scaling_factors(self) -> List[Dict[str, Any]]:
        """Get recommended scaling factors.

        Returns:
            List of scaling factor specifications
        """
        return [
            # Membrane parameters need aggressive scaling
            {
                "unit_id": self.units.ro,
                "var_name": "A_comp",
                "factor": 1e12,
            },
            {
                "unit_id": self.units.ro,
                "var_name": "B_comp",
                "factor": 1e8,
            },
            # Pressure scaling
            {
                "unit_id": self.units.hp_pump,
                "var_name": "outlet.pressure",
                "factor": 1e-5,
            },
            {
                "unit_id": self.units.ro,
                "var_name": "feed_side.pressure",
                "factor": 1e-5,
            },
            # Area
            {
                "unit_id": self.units.ro,
                "var_name": "area",
                "factor": 1e-2,
            },
        ]

    def get_initialization_order(self) -> List[str]:
        """Get unit initialization order.

        Returns:
            List of unit IDs in init order
        """
        order = [
            self.units.feed,
            self.units.hp_pump,
            self.units.ro,
            self.units.permeate,
        ]

        if self.config.include_erd:
            order.extend([self.units.erd, self.units.booster])

        order.append(self.units.brine)

        return order

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
