"""Property Package Registry for WaterTAP.

Provides comprehensive metadata for all WaterTAP property packages including:
- Module paths (critical for avoiding class-name collisions)
- Required/optional components
- Flow basis and state variables
- Scaling defaults
- Reaction package requirements

WARNING: Some class names are shared across modules:
- NaClParameterBlock: NaCl_prop_pack.py AND NaCl_T_dep_prop_pack.py
- WaterParameterBlock: water_prop_pack.py AND zero_order_properties.py
Always select by FULL MODULE PATH, not class name alone!
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set


class PropertyPackageType(Enum):
    """Enumeration of all supported WaterTAP property packages."""

    # Desalination packages (watertap.property_models)
    SEAWATER = "SeawaterParameterBlock"
    NACL = "NaClParameterBlock"
    NACL_T_DEP = "NaClParameterBlock_T_dep"  # Temperature-dependent
    WATER = "WaterParameterBlock"
    MCAS = "MCASParameterBlock"

    # Zero-order (watertap.core - different location!)
    ZERO_ORDER = "WaterParameterBlock_ZO"

    # Biological treatment - Activated Sludge Models
    ASM1 = "ASM1ParameterBlock"
    ASM2D = "ASM2dParameterBlock"
    ASM3 = "ASM3ParameterBlock"
    MODIFIED_ASM2D = "ModifiedASM2dParameterBlock"

    # Biological treatment - Anaerobic Digestion Models
    ADM1 = "ADM1ParameterBlock"
    MODIFIED_ADM1 = "ModifiedADM1ParameterBlock"
    ADM1_VAPOR = "ADM1PropertiesVapor"


@dataclass
class PropertyPackageSpec:
    """Specification for a WaterTAP property package."""

    pkg_type: PropertyPackageType
    class_name: str
    module_path: str  # CRITICAL: Full import path to avoid collisions

    # Phase and component info
    phases: Set[str]
    required_components: List[str]
    optional_components: List[str] = field(default_factory=list)

    # State variable info
    state_vars: List[str] = field(default_factory=list)
    flow_basis: str = "mass"  # "mass", "molar", "volumetric"

    # Reaction package requirements
    requires_reaction_package: bool = False
    compatible_reaction_packages: List[str] = field(default_factory=list)

    # Special configuration
    charge_balance_required: bool = False
    database_required: bool = False  # For zero-order

    # Default scaling factors
    default_scaling: Dict[str, float] = field(default_factory=dict)

    # Configuration kwargs needed for instantiation
    config_kwargs: Dict[str, str] = field(default_factory=dict)

    # Configuration requirements
    requires_config: bool = False
    config_fields: List[str] = field(default_factory=list)


# Property Package Registry
# Maps PropertyPackageType to full specifications
PROPERTY_PACKAGES: Dict[PropertyPackageType, PropertyPackageSpec] = {

    # ==================== DESALINATION PACKAGES ====================

    PropertyPackageType.SEAWATER: PropertyPackageSpec(
        pkg_type=PropertyPackageType.SEAWATER,
        class_name="SeawaterParameterBlock",
        module_path="watertap.property_models.seawater_prop_pack",
        phases={"Liq"},
        required_components=["H2O", "TDS"],
        state_vars=["flow_mass_phase_comp", "temperature", "pressure"],
        flow_basis="mass",
        default_scaling={
            "flow_mass_phase_comp": 1e-1,
            "temperature": 1e-2,
            "pressure": 1e-5,
        },
    ),

    PropertyPackageType.NACL: PropertyPackageSpec(
        pkg_type=PropertyPackageType.NACL,
        class_name="NaClParameterBlock",
        module_path="watertap.property_models.NaCl_prop_pack",
        phases={"Liq"},
        required_components=["H2O", "NaCl"],
        state_vars=["flow_mass_phase_comp", "temperature", "pressure"],
        flow_basis="mass",
        default_scaling={
            "flow_mass_phase_comp": 1e-1,
            "temperature": 1e-2,
            "pressure": 1e-5,
        },
    ),

    PropertyPackageType.NACL_T_DEP: PropertyPackageSpec(
        pkg_type=PropertyPackageType.NACL_T_DEP,
        class_name="NaClParameterBlock",
        module_path="watertap.property_models.NaCl_T_dep_prop_pack",  # Different module!
        phases={"Liq"},
        required_components=["H2O", "NaCl"],
        state_vars=["flow_mass_phase_comp", "temperature", "pressure"],
        flow_basis="mass",
        default_scaling={
            "flow_mass_phase_comp": 1e-1,
            "temperature": 1e-2,
            "pressure": 1e-5,
        },
    ),

    PropertyPackageType.WATER: PropertyPackageSpec(
        pkg_type=PropertyPackageType.WATER,
        class_name="WaterParameterBlock",
        module_path="watertap.property_models.water_prop_pack",
        phases={"Liq", "Vap"},
        required_components=["H2O"],
        state_vars=["flow_mass_phase_comp", "temperature", "pressure"],
        flow_basis="mass",
        default_scaling={
            "flow_mass_phase_comp": 1e-1,
            "temperature": 1e-2,
            "pressure": 1e-5,
        },
    ),

    PropertyPackageType.MCAS: PropertyPackageSpec(
        pkg_type=PropertyPackageType.MCAS,
        class_name="MCASParameterBlock",
        module_path="watertap.property_models.multicomp_aq_sol_prop_pack",
        phases={"Liq"},
        required_components=["H2O"],  # Solutes configured via solute_list
        optional_components=[
            "Na_+", "Cl_-", "Ca_2+", "Mg_2+", "SO4_2-", "HCO3_-", "K_+",
            "NO3_-", "SiO2", "Fe_2+", "Fe_3+", "Mn_2+", "Ba_2+", "Sr_2+"
        ],
        state_vars=["flow_mol_phase_comp", "temperature", "pressure"],
        flow_basis="molar",
        charge_balance_required=True,
        default_scaling={
            "flow_mol_phase_comp": 1e-1,
            "temperature": 1e-2,
            "pressure": 1e-5,
        },
        config_kwargs={
            "solute_list": "Required: list of solute names",
            "charge": "Required: dict mapping solute → charge",
            "mw_data": "Required: dict mapping solute → molecular weight",
        },
        requires_config=True,
        config_fields=["solute_list", "charge", "mw_data"],
    ),

    # ==================== ZERO-ORDER PACKAGE ====================

    PropertyPackageType.ZERO_ORDER: PropertyPackageSpec(
        pkg_type=PropertyPackageType.ZERO_ORDER,
        class_name="WaterParameterBlock",
        module_path="watertap.core.zero_order_properties",  # Different from water_prop_pack!
        phases={"Liq"},
        required_components=["H2O"],  # Solutes from database
        state_vars=["flow_vol", "conc_mass_comp", "temperature", "pressure"],
        flow_basis="volumetric",
        database_required=True,
        default_scaling={
            "flow_vol": 1e3,
            "conc_mass_comp": 1e2,
            "temperature": 1e-2,
            "pressure": 1e-5,
        },
        config_kwargs={
            "database": "Required: WaterTAPDatabase instance",
            "water_source": "Optional: water source type string",
            "solute_list": "Optional: explicit solute list",
        },
        requires_config=True,
        config_fields=["database"],
    ),

    # ==================== BIOLOGICAL - ASM PACKAGES ====================

    PropertyPackageType.ASM1: PropertyPackageSpec(
        pkg_type=PropertyPackageType.ASM1,
        class_name="ASM1ParameterBlock",
        module_path="watertap.property_models.unit_specific.activated_sludge.asm1_properties",
        phases={"Liq"},
        required_components=[
            "H2O", "S_I", "S_S", "X_I", "X_S", "X_BH", "X_BA", "X_P",
            "S_O", "S_NO", "S_NH", "S_ND", "X_ND", "S_ALK"
        ],
        state_vars=["flow_vol", "conc_mass_comp", "temperature", "pressure"],
        flow_basis="volumetric",
        requires_reaction_package=True,
        compatible_reaction_packages=[
            "watertap.property_models.unit_specific.activated_sludge.asm1_reactions"
        ],
        default_scaling={
            "flow_vol": 1e5,
            "conc_mass_comp": 1e2,
        },
    ),

    PropertyPackageType.ASM2D: PropertyPackageSpec(
        pkg_type=PropertyPackageType.ASM2D,
        class_name="ASM2dParameterBlock",
        module_path="watertap.property_models.unit_specific.activated_sludge.asm2d_properties",
        phases={"Liq"},
        required_components=[
            "H2O", "S_O2", "S_F", "S_A", "S_NH4", "S_NO3", "S_PO4",
            "S_I", "S_ALK", "X_I", "X_S", "X_H", "X_PAO", "X_PP",
            "X_PHA", "X_AUT", "X_MeOH", "X_MeP"
        ],
        state_vars=["flow_vol", "conc_mass_comp", "temperature", "pressure"],
        flow_basis="volumetric",
        requires_reaction_package=True,
        compatible_reaction_packages=[
            "watertap.property_models.unit_specific.activated_sludge.asm2d_reactions"
        ],
        default_scaling={
            "flow_vol": 1e5,
            "conc_mass_comp": 1e2,
        },
    ),

    PropertyPackageType.ASM3: PropertyPackageSpec(
        pkg_type=PropertyPackageType.ASM3,
        class_name="ASM3ParameterBlock",
        module_path="watertap.property_models.unit_specific.activated_sludge.asm3_properties",
        phases={"Liq"},
        required_components=[
            "H2O", "S_O2", "S_I", "S_S", "S_NH4", "S_N2", "S_NO3",
            "S_ALK", "X_I", "X_S", "X_H", "X_STO", "X_A"
        ],
        state_vars=["flow_vol", "conc_mass_comp", "temperature", "pressure"],
        flow_basis="volumetric",
        requires_reaction_package=True,
        compatible_reaction_packages=[
            "watertap.property_models.unit_specific.activated_sludge.asm3_reactions"
        ],
        default_scaling={
            "flow_vol": 1e5,
            "conc_mass_comp": 1e2,
        },
    ),

    PropertyPackageType.MODIFIED_ASM2D: PropertyPackageSpec(
        pkg_type=PropertyPackageType.MODIFIED_ASM2D,
        class_name="ModifiedASM2dParameterBlock",
        module_path="watertap.property_models.unit_specific.activated_sludge.modified_asm2d_properties",
        phases={"Liq"},
        required_components=[
            "H2O", "S_O2", "S_F", "S_A", "S_NH4", "S_NO3", "S_PO4",
            "S_I", "S_N2", "S_ALK", "X_I", "X_S", "X_H", "X_PAO",
            "X_PP", "X_PHA", "X_AUT"
        ],
        state_vars=["flow_vol", "conc_mass_comp", "temperature", "pressure"],
        flow_basis="volumetric",
        requires_reaction_package=True,
        compatible_reaction_packages=[
            "watertap.property_models.unit_specific.activated_sludge.modified_asm2d_reactions"
        ],
        default_scaling={
            "flow_vol": 1e5,
            "conc_mass_comp": 1e2,
        },
    ),

    # ==================== BIOLOGICAL - ADM PACKAGES ====================

    PropertyPackageType.ADM1: PropertyPackageSpec(
        pkg_type=PropertyPackageType.ADM1,
        class_name="ADM1ParameterBlock",
        module_path="watertap.property_models.unit_specific.anaerobic_digestion.adm1_properties",
        phases={"Liq"},
        required_components=[
            "H2O", "S_su", "S_aa", "S_fa", "S_va", "S_bu", "S_pro",
            "S_ac", "S_h2", "S_ch4", "S_IC", "S_IN", "S_I",
            "X_c", "X_ch", "X_pr", "X_li", "X_su", "X_aa", "X_fa",
            "X_c4", "X_pro", "X_ac", "X_h2", "X_I"
        ],
        state_vars=["flow_vol", "conc_mass_comp", "temperature", "pressure"],
        flow_basis="volumetric",
        requires_reaction_package=True,
        compatible_reaction_packages=[
            "watertap.property_models.unit_specific.anaerobic_digestion.adm1_reactions"
        ],
        default_scaling={
            "flow_vol": 1e5,
            "conc_mass_comp": 1e2,
        },
    ),

    PropertyPackageType.MODIFIED_ADM1: PropertyPackageSpec(
        pkg_type=PropertyPackageType.MODIFIED_ADM1,
        class_name="ModifiedADM1ParameterBlock",
        module_path="watertap.property_models.unit_specific.anaerobic_digestion.modified_adm1_properties",
        phases={"Liq"},
        required_components=[
            "H2O", "S_su", "S_aa", "S_fa", "S_va", "S_bu", "S_pro",
            "S_ac", "S_h2", "S_ch4", "S_IC", "S_IN", "S_IP", "S_I",
            "X_c", "X_ch", "X_pr", "X_li", "X_su", "X_aa", "X_fa",
            "X_c4", "X_pro", "X_ac", "X_h2", "X_I", "X_PHA", "X_PP", "X_PAO"
        ],
        state_vars=["flow_vol", "conc_mass_comp", "temperature", "pressure"],
        flow_basis="volumetric",
        requires_reaction_package=True,
        compatible_reaction_packages=[
            "watertap.property_models.unit_specific.anaerobic_digestion.modified_adm1_reactions"
        ],
        default_scaling={
            "flow_vol": 1e5,
            "conc_mass_comp": 1e2,
        },
    ),

    PropertyPackageType.ADM1_VAPOR: PropertyPackageSpec(
        pkg_type=PropertyPackageType.ADM1_VAPOR,
        class_name="ADM1PropertiesVapor",
        module_path="watertap.property_models.unit_specific.anaerobic_digestion.adm1_properties_vapor",
        phases={"Vap"},
        required_components=["H2O", "S_h2", "S_ch4", "S_co2"],
        state_vars=["flow_vol", "conc_mass_comp", "temperature", "pressure"],
        flow_basis="volumetric",
        default_scaling={
            "flow_vol": 1e5,
        },
    ),
}


def get_property_package_spec(pkg_type: PropertyPackageType) -> PropertyPackageSpec:
    """Get specification for a property package type.

    Args:
        pkg_type: The property package type enum value

    Returns:
        PropertyPackageSpec with full metadata

    Raises:
        KeyError: If property package type not found
    """
    if pkg_type not in PROPERTY_PACKAGES:
        raise KeyError(f"Unknown property package type: {pkg_type}")
    return PROPERTY_PACKAGES[pkg_type]


def list_property_packages(
    flow_basis: Optional[str] = None,
    requires_reaction: Optional[bool] = None,
) -> List[PropertyPackageSpec]:
    """List property packages with optional filtering.

    Args:
        flow_basis: Filter by flow basis ("mass", "molar", "volumetric")
        requires_reaction: Filter by reaction package requirement

    Returns:
        List of matching PropertyPackageSpec objects
    """
    results = list(PROPERTY_PACKAGES.values())

    if flow_basis is not None:
        results = [p for p in results if p.flow_basis == flow_basis]

    if requires_reaction is not None:
        results = [p for p in results if p.requires_reaction_package == requires_reaction]

    return results


def get_import_statement(pkg_type: PropertyPackageType) -> str:
    """Generate Python import statement for a property package.

    Args:
        pkg_type: The property package type

    Returns:
        Python import statement string
    """
    spec = get_property_package_spec(pkg_type)
    return f"from {spec.module_path} import {spec.class_name}"
