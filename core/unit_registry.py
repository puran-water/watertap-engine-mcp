"""Unit Registry for WaterTAP.

Provides comprehensive metadata for all WaterTAP unit models including:
- Import paths (WaterTAP vs IDAES units)
- DOF requirements and typical values
- Initialization methods and hints
- Scaling defaults
- Port configurations

IMPORTANT: Some units are from IDAES, not WaterTAP:
- Feed, Product, Mixer, Separator → idaes.models.unit_models
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set

from .property_registry import PropertyPackageType


class UnitCategory(Enum):
    """Categories of unit operations."""
    MEMBRANE = "membrane"
    MEMBRANE_ZO = "membrane_zero_order"
    THERMAL = "thermal"
    PUMP = "pump"
    PUMP_ZO = "pump_zero_order"
    ERD = "energy_recovery"
    MIXER = "mixer"
    SPLITTER = "splitter"
    FEED = "feed"
    FEED_ZO = "feed_zero_order"
    PRODUCT = "product"
    CRYSTALLIZER = "crystallizer"
    BIOLOGICAL = "biological"
    OTHER = "other"


class InitMethod(Enum):
    """Initialization method types."""
    INITIALIZE = "initialize"
    INITIALIZE_BUILD = "initialize_build"
    INITIALIZER_CLASS = "initializer_class"


@dataclass
class VariableSpec:
    """Specification for a variable that needs to be fixed."""
    name: str
    description: str
    units: str
    typical_min: Optional[float] = None
    typical_max: Optional[float] = None
    typical_default: Optional[float] = None
    indexed: bool = False
    index_set: Optional[str] = None  # e.g., "phase_comp" for flow_mass_phase_comp


@dataclass
class InitHints:
    """Hints for unit initialization."""
    method: InitMethod
    initializer_class: Optional[str] = None
    requires_state_args: bool = False
    state_args_format: Optional[str] = None  # Description of state_args structure
    special_requirements: List[str] = field(default_factory=list)


@dataclass
class UnitSpec:
    """Specification for a WaterTAP/IDAES unit model."""

    unit_type: str  # e.g., "ReverseOsmosis0D"
    class_name: str
    module_path: str
    category: UnitCategory

    # Property package compatibility
    compatible_property_packages: List[PropertyPackageType]

    # DOF Management
    required_fixes: List[VariableSpec]
    typical_values: Dict[str, float] = field(default_factory=dict)

    # Scaling defaults
    default_scaling: Dict[str, float] = field(default_factory=dict)

    # Initialization
    init_hints: Optional[InitHints] = None

    # Port configuration
    n_inlets: int = 1
    n_outlets: int = 1
    inlet_names: List[str] = field(default_factory=lambda: ["inlet"])
    outlet_names: List[str] = field(default_factory=lambda: ["outlet"])

    # Additional metadata
    description: str = ""
    is_idaes_unit: bool = False  # True for units from idaes.models.unit_models


# Unit Registry
UNITS: Dict[str, UnitSpec] = {

    # ==================== MEMBRANE UNITS ====================

    "ReverseOsmosis0D": UnitSpec(
        unit_type="ReverseOsmosis0D",
        class_name="ReverseOsmosis0D",
        module_path="watertap.unit_models.reverse_osmosis_0D",
        category=UnitCategory.MEMBRANE,
        compatible_property_packages=[
            PropertyPackageType.SEAWATER,
            PropertyPackageType.NACL,
            PropertyPackageType.NACL_T_DEP,
        ],
        required_fixes=[
            VariableSpec("A_comp", "Water permeability coefficient", "m/s/Pa",
                        typical_min=1e-12, typical_max=1e-11, typical_default=4.2e-12,
                        indexed=True, index_set="solvent_set"),
            VariableSpec("B_comp", "Salt permeability coefficient", "m/s",
                        typical_min=1e-8, typical_max=1e-7, typical_default=3.5e-8,
                        indexed=True, index_set="solute_set"),
            VariableSpec("area", "Membrane area", "m^2",
                        typical_min=10, typical_max=1000, typical_default=50),
            VariableSpec("permeate.pressure[0]", "Permeate pressure", "Pa",
                        typical_default=101325),
            VariableSpec("feed_side.cp_modulus[0,*,*]", "Concentration polarization modulus", "-",
                        typical_default=1.0, indexed=True),
        ],
        typical_values={
            "A_comp[0, H2O]": 4.2e-12,
            "B_comp[0, NaCl]": 3.5e-8,
            "B_comp[0, TDS]": 3.5e-8,
            "area": 50,
            "permeate.pressure[0]": 101325,
        },
        default_scaling={
            "A_comp": 1e12,
            "B_comp": 1e8,
            "area": 1e-2,
            "flux_mass_phase_comp": 1e3,
        },
        init_hints=InitHints(
            method=InitMethod.INITIALIZE_BUILD,
            requires_state_args=True,
            state_args_format="dict with flow_mass_phase_comp, pressure, temperature",
        ),
        n_outlets=2,
        outlet_names=["permeate", "retentate"],
        description="0D reverse osmosis membrane model",
    ),

    "ReverseOsmosis1D": UnitSpec(
        unit_type="ReverseOsmosis1D",
        class_name="ReverseOsmosis1D",
        module_path="watertap.unit_models.reverse_osmosis_1D",
        category=UnitCategory.MEMBRANE,
        compatible_property_packages=[
            PropertyPackageType.SEAWATER,
            PropertyPackageType.NACL,
            PropertyPackageType.NACL_T_DEP,
        ],
        required_fixes=[
            VariableSpec("A_comp", "Water permeability coefficient", "m/s/Pa",
                        typical_default=4.2e-12, indexed=True),
            VariableSpec("B_comp", "Salt permeability coefficient", "m/s",
                        typical_default=3.5e-8, indexed=True),
            VariableSpec("area", "Membrane area", "m^2", typical_default=50),
            VariableSpec("permeate.pressure[0,*]", "Permeate pressure", "Pa",
                        typical_default=101325, indexed=True),
            VariableSpec("length", "Channel length", "m", typical_default=1.0),
        ],
        typical_values={
            "A_comp[0, H2O]": 4.2e-12,
            "B_comp[0, NaCl]": 3.5e-8,
            "area": 50,
            "length": 1.0,
        },
        default_scaling={
            "A_comp": 1e12,
            "B_comp": 1e8,
            "area": 1e-2,
        },
        init_hints=InitHints(
            method=InitMethod.INITIALIZE_BUILD,
            requires_state_args=True,
            special_requirements=["Multi-CV unit - needs discretization config"],
        ),
        n_outlets=2,
        outlet_names=["permeate", "retentate"],
        description="1D reverse osmosis membrane model with spatial discretization",
    ),

    "Nanofiltration0D": UnitSpec(
        unit_type="Nanofiltration0D",
        class_name="Nanofiltration0D",
        module_path="watertap.unit_models.nanofiltration_0D",
        category=UnitCategory.MEMBRANE,
        compatible_property_packages=[
            PropertyPackageType.SEAWATER,
            PropertyPackageType.NACL,
            PropertyPackageType.MCAS,
        ],
        required_fixes=[
            VariableSpec("recovery_solvent", "Solvent recovery fraction", "-",
                        typical_min=0.3, typical_max=0.9, typical_default=0.7),
            VariableSpec("rejection_comp", "Solute rejection", "-",
                        typical_min=0.5, typical_max=0.99, typical_default=0.9,
                        indexed=True),
            VariableSpec("permeate.pressure[0]", "Permeate pressure", "Pa",
                        typical_default=101325),
            VariableSpec("deltaP", "Pressure drop", "Pa", typical_default=-1e4),
            VariableSpec("area", "Membrane area", "m^2", typical_default=500),
        ],
        typical_values={
            "recovery_solvent": 0.7,
            "area": 500,
            "permeate.pressure[0]": 101325,
            "deltaP": -1e4,
        },
        default_scaling={
            "area": 1e-2,
            "flux_vol_solvent": 1e5,
        },
        init_hints=InitHints(
            method=InitMethod.INITIALIZER_CLASS,
            initializer_class="Nanofiltration0DInitializer",
        ),
        n_outlets=2,
        outlet_names=["permeate", "retentate"],
        description="0D nanofiltration membrane model",
    ),

    "NanofiltrationDSPMDE0D": UnitSpec(
        unit_type="NanofiltrationDSPMDE0D",
        class_name="NanofiltrationDSPMDE0D",
        module_path="watertap.unit_models.nanofiltration_DSPMDE_0D",
        category=UnitCategory.MEMBRANE,
        compatible_property_packages=[PropertyPackageType.MCAS],
        required_fixes=[
            VariableSpec("recovery_vol_phase", "Volume recovery", "-",
                        typical_default=0.5, indexed=True),
            VariableSpec("radius_pore", "Membrane pore radius", "m",
                        typical_default=0.5e-9),
            VariableSpec("membrane_thickness_effective", "Membrane thickness", "m",
                        typical_default=1.33e-6),
            VariableSpec("membrane_charge_density", "Membrane charge", "mol/m^3",
                        typical_default=-27),
        ],
        typical_values={
            "recovery_vol_phase[Liq]": 0.5,
            "radius_pore": 0.5e-9,
            "membrane_thickness_effective": 1.33e-6,
        },
        default_scaling={
            "radius_pore": 1e9,
            "membrane_thickness_effective": 1e6,
        },
        init_hints=InitHints(
            method=InitMethod.INITIALIZE_BUILD,
            requires_state_args=True,
            special_requirements=["Multi-CV unit", "MCAS property package required"],
        ),
        n_outlets=2,
        outlet_names=["permeate", "retentate"],
        description="0D nanofiltration with Donnan-Steric Pore Model",
    ),

    # ==================== ZERO-ORDER MEMBRANE UNITS ====================

    "NanofiltrationZO": UnitSpec(
        unit_type="NanofiltrationZO",
        class_name="NanofiltrationZO",
        module_path="watertap.unit_models.zero_order.nanofiltration_zo",
        category=UnitCategory.MEMBRANE_ZO,
        compatible_property_packages=[PropertyPackageType.ZERO_ORDER],
        required_fixes=[],  # Parameters loaded from database
        init_hints=InitHints(
            method=InitMethod.INITIALIZE,
            special_requirements=["Load parameters from database first"],
        ),
        n_outlets=2,
        outlet_names=["treated", "byproduct"],
        description="Zero-order nanofiltration model (database-driven)",
    ),

    "UltraFiltrationZO": UnitSpec(
        unit_type="UltraFiltrationZO",
        class_name="UltraFiltrationZO",
        module_path="watertap.unit_models.zero_order.ultra_filtration_zo",
        category=UnitCategory.MEMBRANE_ZO,
        compatible_property_packages=[PropertyPackageType.ZERO_ORDER],
        required_fixes=[],
        init_hints=InitHints(
            method=InitMethod.INITIALIZE,
            special_requirements=["Load parameters from database first"],
        ),
        n_outlets=2,
        outlet_names=["treated", "byproduct"],
        description="Zero-order ultrafiltration model (database-driven)",
    ),

    # ==================== THERMAL UNITS ====================

    "Evaporator": UnitSpec(
        unit_type="Evaporator",
        class_name="Evaporator",
        module_path="watertap.unit_models.mvc.components.evaporator",
        category=UnitCategory.THERMAL,
        compatible_property_packages=[
            PropertyPackageType.NACL,
            PropertyPackageType.NACL_T_DEP,
            PropertyPackageType.WATER,
        ],
        required_fixes=[
            VariableSpec("outlet_brine.temperature[0]", "Brine outlet temperature", "K",
                        typical_min=323, typical_max=373, typical_default=348),
            VariableSpec("U", "Overall heat transfer coefficient", "W/m^2/K",
                        typical_min=500, typical_max=3000, typical_default=1000),
            VariableSpec("area", "Heat exchanger area", "m^2",
                        typical_min=10, typical_max=500, typical_default=100),
            VariableSpec("delta_temperature_in", "Inlet temp difference", "K",
                        typical_default=10),
            VariableSpec("delta_temperature_out", "Outlet temp difference", "K",
                        typical_default=5),
        ],
        typical_values={
            "U": 1000,
            "area": 100,
            "delta_temperature_in": 10,
            "delta_temperature_out": 5,
        },
        default_scaling={
            "heat_transfer": 1e-6,
            "area": 1e-2,
        },
        init_hints=InitHints(
            method=InitMethod.INITIALIZE,
            requires_state_args=True,
            special_requirements=[
                "Separate feed/vapor property packages",
                "Multi-phase handling required",
            ],
        ),
        n_inlets=2,
        inlet_names=["inlet_feed", "inlet_vapor"],
        n_outlets=2,
        outlet_names=["outlet_brine", "outlet_vapor"],
        description="MVC evaporator unit",
    ),

    "Condenser": UnitSpec(
        unit_type="Condenser",
        class_name="Condenser",
        module_path="watertap.unit_models.mvc.components.complete_condenser",
        category=UnitCategory.THERMAL,
        compatible_property_packages=[PropertyPackageType.WATER],
        required_fixes=[
            VariableSpec("control_volume.heat[0]", "Heat duty", "W"),
        ],
        init_hints=InitHints(method=InitMethod.INITIALIZE_BUILD),
        description="Complete condenser for MVC systems",
    ),

    "Compressor": UnitSpec(
        unit_type="Compressor",
        class_name="Compressor",
        module_path="watertap.unit_models.mvc.components.compressor",
        category=UnitCategory.THERMAL,
        compatible_property_packages=[PropertyPackageType.WATER],
        required_fixes=[
            VariableSpec("pressure_ratio", "Compression ratio", "-",
                        typical_min=1.1, typical_max=3.0, typical_default=1.5),
            VariableSpec("efficiency", "Isentropic efficiency", "-",
                        typical_min=0.6, typical_max=0.9, typical_default=0.8),
        ],
        typical_values={
            "pressure_ratio": 1.5,
            "efficiency": 0.8,
        },
        init_hints=InitHints(method=InitMethod.INITIALIZE_BUILD),
        description="Vapor compressor for MVC systems",
    ),

    # ==================== CRYSTALLIZER ====================

    "Crystallization": UnitSpec(
        unit_type="Crystallization",
        class_name="Crystallization",
        module_path="watertap.unit_models.crystallizer",
        category=UnitCategory.CRYSTALLIZER,
        compatible_property_packages=[
            PropertyPackageType.NACL,
            PropertyPackageType.NACL_T_DEP,
        ],
        required_fixes=[
            VariableSpec("temperature_operating", "Operating temperature", "K",
                        typical_min=323, typical_max=373),
            VariableSpec("crystal_growth_rate", "Crystal growth rate", "m/s",
                        typical_default=5e-9),
            VariableSpec("crystal_median_length", "Median crystal size", "m",
                        typical_default=0.5e-3),
        ],
        typical_values={
            "crystal_growth_rate": 5e-9,
            "crystal_median_length": 0.5e-3,
        },
        default_scaling={
            "crystal_growth_rate": 1e9,
        },
        init_hints=InitHints(
            method=InitMethod.INITIALIZE_BUILD,
            requires_state_args=True,
            special_requirements=[
                "Interval arithmetic pre-solve",
                "Multi-phase: liquid + solid + vapor",
            ],
        ),
        n_outlets=3,
        outlet_names=["outlet", "solids", "vapor"],
        description="Crystallization unit for salt recovery",
    ),

    # ==================== PUMP UNITS ====================

    "Pump": UnitSpec(
        unit_type="Pump",
        class_name="Pump",
        module_path="watertap.unit_models.pressure_changer",
        category=UnitCategory.PUMP,
        compatible_property_packages=[
            PropertyPackageType.SEAWATER,
            PropertyPackageType.NACL,
            PropertyPackageType.NACL_T_DEP,
            PropertyPackageType.WATER,
            PropertyPackageType.MCAS,
        ],
        required_fixes=[
            VariableSpec("efficiency_pump[0]", "Pump efficiency", "-",
                        typical_min=0.6, typical_max=0.9, typical_default=0.8),
            VariableSpec("control_volume.properties_out[0].pressure",
                        "Outlet pressure", "Pa",
                        typical_min=1e5, typical_max=1e7),
        ],
        typical_values={
            "efficiency_pump[0]": 0.8,
        },
        default_scaling={
            "work_mechanical": 1e-5,
        },
        init_hints=InitHints(method=InitMethod.INITIALIZE),
        description="Pressure changer (pump mode)",
    ),

    "EnergyRecoveryDevice": UnitSpec(
        unit_type="EnergyRecoveryDevice",
        class_name="EnergyRecoveryDevice",
        module_path="watertap.unit_models.pressure_changer",
        category=UnitCategory.ERD,
        compatible_property_packages=[
            PropertyPackageType.SEAWATER,
            PropertyPackageType.NACL,
        ],
        required_fixes=[
            VariableSpec("efficiency_pump[0]", "Turbine efficiency", "-",
                        typical_default=0.9),
            VariableSpec("control_volume.properties_out[0].pressure",
                        "Outlet pressure", "Pa"),
        ],
        typical_values={
            "efficiency_pump[0]": 0.9,
        },
        init_hints=InitHints(method=InitMethod.INITIALIZE),
        description="Pressure changer (turbine mode) for energy recovery",
    ),

    "PressureExchanger": UnitSpec(
        unit_type="PressureExchanger",
        class_name="PressureExchanger",
        module_path="watertap.unit_models.pressure_exchanger",
        category=UnitCategory.ERD,
        compatible_property_packages=[
            PropertyPackageType.SEAWATER,
            PropertyPackageType.NACL,
        ],
        required_fixes=[
            VariableSpec("efficiency_pressure_exchanger", "PX efficiency", "-",
                        typical_default=0.95),
        ],
        typical_values={
            "efficiency_pressure_exchanger": 0.95,
        },
        init_hints=InitHints(method=InitMethod.INITIALIZE),
        n_inlets=2,
        inlet_names=["high_pressure_inlet", "low_pressure_inlet"],
        n_outlets=2,
        outlet_names=["high_pressure_outlet", "low_pressure_outlet"],
        description="Isobaric pressure exchanger for RO energy recovery",
    ),

    "PumpZO": UnitSpec(
        unit_type="PumpZO",
        class_name="PumpZO",
        module_path="watertap.unit_models.zero_order.pump_zo",
        category=UnitCategory.PUMP_ZO,
        compatible_property_packages=[PropertyPackageType.ZERO_ORDER],
        required_fixes=[],
        init_hints=InitHints(
            method=InitMethod.INITIALIZE,
            special_requirements=["Load parameters from database first"],
        ),
        description="Zero-order pump model (database-driven)",
    ),

    # ==================== IDAES UNITS (not from WaterTAP) ====================

    "Feed": UnitSpec(
        unit_type="Feed",
        class_name="Feed",
        module_path="idaes.models.unit_models.feed",
        category=UnitCategory.FEED,
        compatible_property_packages=[
            PropertyPackageType.SEAWATER,
            PropertyPackageType.NACL,
            PropertyPackageType.NACL_T_DEP,
            PropertyPackageType.WATER,
            PropertyPackageType.MCAS,
        ],
        required_fixes=[],  # Feed conditions set via state variables
        init_hints=InitHints(method=InitMethod.INITIALIZE),
        n_inlets=0,
        is_idaes_unit=True,
        description="Feed block (IDAES unit)",
    ),

    "Product": UnitSpec(
        unit_type="Product",
        class_name="Product",
        module_path="idaes.models.unit_models.product",
        category=UnitCategory.PRODUCT,
        compatible_property_packages=[
            PropertyPackageType.SEAWATER,
            PropertyPackageType.NACL,
            PropertyPackageType.NACL_T_DEP,
            PropertyPackageType.WATER,
            PropertyPackageType.MCAS,
        ],
        required_fixes=[],
        init_hints=InitHints(method=InitMethod.INITIALIZE),
        n_outlets=0,
        is_idaes_unit=True,
        description="Product block (IDAES unit)",
    ),

    "Mixer": UnitSpec(
        unit_type="Mixer",
        class_name="Mixer",
        module_path="idaes.models.unit_models.mixer",
        category=UnitCategory.MIXER,
        compatible_property_packages=[
            PropertyPackageType.SEAWATER,
            PropertyPackageType.NACL,
            PropertyPackageType.WATER,
            PropertyPackageType.MCAS,
            PropertyPackageType.ASM1,
            PropertyPackageType.ASM2D,
            PropertyPackageType.ADM1,
        ],
        required_fixes=[],
        init_hints=InitHints(
            method=InitMethod.INITIALIZE,
            special_requirements=["Must init AFTER all inlets are connected"],
        ),
        n_inlets=2,  # Configurable
        inlet_names=["inlet_1", "inlet_2"],
        is_idaes_unit=True,
        description="Stream mixer (IDAES unit)",
    ),

    "Separator": UnitSpec(
        unit_type="Separator",
        class_name="Separator",
        module_path="idaes.models.unit_models.separator",
        category=UnitCategory.SPLITTER,
        compatible_property_packages=[
            PropertyPackageType.SEAWATER,
            PropertyPackageType.NACL,
            PropertyPackageType.WATER,
            PropertyPackageType.MCAS,
        ],
        required_fixes=[
            VariableSpec("split_fraction[0,*,*]", "Split fractions", "-",
                        indexed=True),
        ],
        init_hints=InitHints(method=InitMethod.INITIALIZE),
        n_outlets=2,  # Configurable
        outlet_names=["outlet_1", "outlet_2"],
        is_idaes_unit=True,
        description="Stream separator/splitter (IDAES unit)",
    ),

    "FeedZO": UnitSpec(
        unit_type="FeedZO",
        class_name="FeedZO",
        module_path="watertap.unit_models.zero_order.feed_zo",
        category=UnitCategory.FEED_ZO,
        compatible_property_packages=[PropertyPackageType.ZERO_ORDER],
        required_fixes=[],
        init_hints=InitHints(
            method=InitMethod.INITIALIZE,
            special_requirements=["Configure with database and water source"],
        ),
        n_inlets=0,
        description="Zero-order feed block (database-driven)",
    ),
}


def get_unit_spec(unit_type: str) -> UnitSpec:
    """Get specification for a unit type.

    Args:
        unit_type: The unit type name (e.g., "ReverseOsmosis0D")

    Returns:
        UnitSpec with full metadata

    Raises:
        KeyError: If unit type not found
    """
    if unit_type not in UNITS:
        raise KeyError(f"Unknown unit type: {unit_type}")
    return UNITS[unit_type]


def list_units(
    category: Optional[UnitCategory] = None,
    property_package: Optional[PropertyPackageType] = None,
    is_idaes: Optional[bool] = None,
) -> List[UnitSpec]:
    """List units with optional filtering.

    Args:
        category: Filter by unit category
        property_package: Filter by compatible property package
        is_idaes: Filter by whether unit is from IDAES

    Returns:
        List of matching UnitSpec objects
    """
    results = list(UNITS.values())

    if category is not None:
        results = [u for u in results if u.category == category]

    if property_package is not None:
        results = [u for u in results if property_package in u.compatible_property_packages]

    if is_idaes is not None:
        results = [u for u in results if u.is_idaes_unit == is_idaes]

    return results


def get_import_statement(unit_type: str) -> str:
    """Generate Python import statement for a unit.

    Args:
        unit_type: The unit type name

    Returns:
        Python import statement string
    """
    spec = get_unit_spec(unit_type)
    return f"from {spec.module_path} import {spec.class_name}"
