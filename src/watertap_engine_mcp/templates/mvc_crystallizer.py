"""MVC crystallizer/ZLD flowsheet template."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from watertap_engine_mcp.core.property_registry import PropertyPackageType
from watertap_engine_mcp.flowsheets.zld import EvaporatorCrystallizerZLDTemplate, ZLDConfig


@dataclass
class MVCCrystallizerConfig:
    """Configuration for the public MVC evaporator plus crystallizer template."""

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


class MVCCrystallizerTemplate:
    """Public ZLD template with an MVC evaporator feeding a NaCl crystallizer."""

    def __init__(self, config: Optional[MVCCrystallizerConfig] = None):
        self.config = config or MVCCrystallizerConfig()
        self._template = EvaporatorCrystallizerZLDTemplate(
            ZLDConfig(
                property_package=self.config.property_package,
                evaporator_property_package=self.config.evaporator_property_package,
                evaporator_area_m2=self.config.evaporator_area_m2,
                heat_transfer_coeff_W_m2K=self.config.heat_transfer_coeff_W_m2K,
                brine_outlet_temp_K=self.config.brine_outlet_temp_K,
                compressor_efficiency=self.config.compressor_efficiency,
                pressure_ratio=self.config.pressure_ratio,
                crystallizer_temperature_K=self.config.crystallizer_temperature_K,
                crystallization_yield=self.config.crystallization_yield,
                crystal_median_length_m=self.config.crystal_median_length_m,
            )
        )

    def get_units(self) -> List[Dict[str, Any]]:
        return self._template.get_units()

    def get_connections(self) -> List[Dict[str, Any]]:
        return self._template.get_connections()

    def get_dof_fixes(self) -> List[Dict[str, Any]]:
        return self._template.get_dof_fixes()

    def get_scaling_factors(self) -> List[Dict[str, Any]]:
        return self._template.get_scaling_factors()

    def get_thermal_couplings(self) -> Dict[str, str]:
        return self._template.get_thermal_couplings()

    def get_initialization_order(self) -> List[str]:
        return self._template.get_initialization_order()

    def to_session_spec(self) -> Dict[str, Any]:
        return self._template.to_session_spec()
