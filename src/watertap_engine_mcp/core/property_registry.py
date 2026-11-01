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
from typing import Any, Dict, List, Optional, Set


class PropertyPackageType(Enum):
    """Enumeration of all supported WaterTAP property packages."""

    # Desalination packages (watertap.property_models)
    SEAWATER = "SeawaterParameterBlock"
    NACL = "NaClParameterBlock"
    NACL_T_DEP = "NaClParameterBlock_T_dep"  # Temperature-dependent
    WATER = "WaterParameterBlock"
    MCAS = "MCASParameterBlock"
    NDMA = "NDMAParameterBlock"
    CRYST_NACL = "NaClParameterBlock_Crystallizer"

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
    # The house S/Fe mADM1 (phase 3a, owner ruling 2026-10-07 "After SRB in WaterTAP"): ModifiedADM1 + the 17
    # sulfur/iron components of the shared S/Fe kernel, and the headspace with H2S (chemistry.modified_adm1_sfe).
    MODIFIED_ADM1_SFE = "SFEModifiedADM1ParameterBlock"
    ADM1_SFE_VAPOR = "SFEADM1VaporParameterBlock"
    
    # package plus X_Sorg (particulate organic S, kg S), built only when a digester asks for organic sulfur.
    MODIFIED_ADM1_SFE_ORGANIC_S = "SFEOrganicSModifiedADM1ParameterBlock"
    # The organic-S S/Fe mADM1 with the OPT-IN micro-aeration extension (PUBLIC-RULE-6dce80fc160d):
    # plus dissolved O2 (S_O2), and the headspace plus O2 and N2 (Ar lumped); built only for an oxidant-dosed digester.
    MODIFIED_ADM1_SFE_MICROAERATION = "MicroaerationModifiedADM1ParameterBlock"
    ADM1_SFE_MICROAERATION_VAPOR = "MicroaerationADM1VaporParameterBlock"

    # Gas-phase separation (biogas upgrading) - IDAES modular Peng-Robinson
    NATURAL_GAS_PR = "NaturalGasPR"


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
    reaction_module_path: Optional[str] = None
    reaction_class_name: Optional[str] = None

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

    # Published operating envelope metadata. Callers must check the relevant
    # positive limit before accepting high-salinity RO/UHPRO predictions.
    validity_limits: Dict[str, Any] = field(default_factory=dict)


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
        validity_limits={
            "max_ro_brine_tds_g_L": 120.0,
            "basis": "proposal RO certification envelope for seawater_property RO/UHPRO use",
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
        validity_limits={
            "max_ro_brine_tds_g_L": 120.0,
            "basis": "proposal RO certification envelope for NaCl surrogate RO/UHPRO use",
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
        validity_limits={
            "max_ro_brine_tds_g_L": 120.0,
            "basis": "proposal RO certification envelope for temperature-dependent NaCl surrogate RO/UHPRO use",
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
            "stokes_radius_data": "Optional advanced: dict mapping solute → Stokes radius",
            "diffusivity_data": "Optional advanced: dict mapping solute or ('Liq', solute) → diffusivity",
            "elec_mobility_data": "Optional advanced: dict mapping solute or ('Liq', solute) → electrical mobility",
            "molar_volume_data": "Optional advanced: dict mapping solute → molar volume",
            "activity_coefficient_model": "Optional advanced: activity coefficient model name",
            "density_calculation": "Optional advanced: density calculation mode",
            "material_flow_basis": "Optional advanced: material flow basis enum/name",
            "ignore_neutral_charge": "Optional advanced: allow neutral solutes without charge data",
        },
        requires_config=True,
        config_fields=["solute_list", "charge", "mw_data"],
        validity_limits={
            "max_ro_brine_tds_g_L": 70.0,
            "max_ionic_strength_mol_kg": 1.0,
            "basis": "Debye-Huckel-style MCAS proposal envelope; use PHREEQC/Pitzer above this range",
        },
    ),

    PropertyPackageType.NDMA: PropertyPackageSpec(
        pkg_type=PropertyPackageType.NDMA,
        class_name="NDMAParameterBlock",
        module_path="watertap.property_models.unit_specific.NDMA_prop_pack",
        phases={"Liq"},
        required_components=["H2O", "NDMA"],
        state_vars=["flow_mass_phase_comp", "temperature", "pressure"],
        flow_basis="mass",
        default_scaling={
            "flow_mass_phase_comp": 1e-3,
            "temperature": 1e-2,
            "pressure": 1e-5,
        },
    ),

    PropertyPackageType.CRYST_NACL: PropertyPackageSpec(
        pkg_type=PropertyPackageType.CRYST_NACL,
        class_name="NaClParameterBlock",
        module_path="watertap.property_models.unit_specific.cryst_prop_pack",
        phases={"Liq", "Sol", "Vap"},
        required_components=["H2O", "NaCl"],
        state_vars=["flow_mass_phase_comp", "temperature", "pressure"],
        flow_basis="mass",
        default_scaling={
            "flow_mass_phase_comp": 1e-1,
            "temperature": 1e-2,
            "pressure": 1e-5,
        },
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
        reaction_module_path="watertap.property_models.unit_specific.activated_sludge.asm1_reactions",
        reaction_class_name="ASM1ReactionParameterBlock",
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
            "S_I", "S_N2", "S_ALK", "X_I", "X_S", "X_H", "X_PAO", "X_PP",
            "X_PHA", "X_AUT", "X_MeOH", "X_MeP", "X_TSS"
        ],
        state_vars=["flow_vol", "conc_mass_comp", "temperature", "pressure"],
        flow_basis="volumetric",
        requires_reaction_package=True,
        compatible_reaction_packages=[
            "watertap.property_models.unit_specific.activated_sludge.asm2d_reactions"
        ],
        reaction_module_path="watertap.property_models.unit_specific.activated_sludge.asm2d_reactions",
        reaction_class_name="ASM2dReactionParameterBlock",
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
        reaction_module_path="watertap.property_models.unit_specific.activated_sludge.asm3_reactions",
        reaction_class_name="ASM3ReactionParameterBlock",
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
            "S_I", "S_N2", "S_K", "S_Mg", "S_IC", "X_I", "X_S",
            "X_H", "X_PAO", "X_PP", "X_PHA", "X_AUT"
        ],
        state_vars=["flow_vol", "conc_mass_comp", "temperature", "pressure"],
        flow_basis="volumetric",
        requires_reaction_package=True,
        compatible_reaction_packages=[
            "watertap.property_models.unit_specific.activated_sludge.modified_asm2d_reactions"
        ],
        reaction_module_path="watertap.property_models.unit_specific.activated_sludge.modified_asm2d_reactions",
        reaction_class_name="ModifiedASM2dReactionParameterBlock",
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
        reaction_module_path="watertap.property_models.unit_specific.anaerobic_digestion.adm1_reactions",
        reaction_class_name="ADM1ReactionParameterBlock",
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
        # House Davies subclass retains the modified-ADM1 state/stoichiometry.
        reaction_module_path='watertap_engine_mcp.chemistry.modified_adm1_davies',
        reaction_class_name="DaviesADM1ReactionParameterBlock",
        default_scaling={
            "flow_vol": 1e5,
            "conc_mass_comp": 1e2,
        },
    ),

    PropertyPackageType.MODIFIED_ADM1_SFE: PropertyPackageSpec(
        pkg_type=PropertyPackageType.MODIFIED_ADM1_SFE,
        class_name="SFEModifiedADM1ParameterBlock",
        module_path='watertap_engine_mcp.chemistry.modified_adm1_sfe',
        phases={"Liq"},
        required_components=[
            "H2O", "S_su", "S_aa", "S_fa", "S_va", "S_bu", "S_pro",
            "S_ac", "S_h2", "S_ch4", "S_IC", "S_IN", "S_IP", "S_I",
            "X_ch", "X_pr", "X_li", "X_su", "X_aa", "X_fa",
            "X_c4", "X_pro", "X_ac", "X_h2", "X_I", "X_PHA", "X_PP", "X_PAO",
            "S_K", "S_Mg",
            "S_SO4", "S_IS", "X_hSRB", "X_aSRB", "X_pSRB", "X_c4SRB", "S_S0", "S_Fe3", "S_Fe2",
            "X_HFO_H", "X_HFO_L", "X_HFO_old", "X_HFO_HP", "X_HFO_LP", "X_HFO_HP_old", "X_HFO_LP_old", "X_FeS",
            "X_Fe3PO42", "X_Siderite", "X_Mackinawite", "X_struv",
        ],
        state_vars=["flow_vol", "conc_mass_comp", "temperature", "pressure"],
        flow_basis="volumetric",
        requires_reaction_package=True,
        compatible_reaction_packages=['watertap_engine_mcp.chemistry.modified_adm1_sfe'],
        # the S/Fe kernel block is the only reaction model that closes this state (core + S/Fe stoichiometry)
        reaction_module_path='watertap_engine_mcp.chemistry.modified_adm1_sfe',
        reaction_class_name="SFEKernelADM1ReactionParameterBlock",
        default_scaling={
            "flow_vol": 1e5,
            "conc_mass_comp": 1e2,
        },
    ),

    PropertyPackageType.MODIFIED_ADM1_SFE_ORGANIC_S: PropertyPackageSpec(
        pkg_type=PropertyPackageType.MODIFIED_ADM1_SFE_ORGANIC_S,
        class_name="SFEOrganicSModifiedADM1ParameterBlock",
        module_path='watertap_engine_mcp.chemistry.modified_adm1_sfe',
        phases={"Liq"},
        required_components=[
            "H2O", "S_su", "S_aa", "S_fa", "S_va", "S_bu", "S_pro",
            "S_ac", "S_h2", "S_ch4", "S_IC", "S_IN", "S_IP", "S_I",
            "X_ch", "X_pr", "X_li", "X_su", "X_aa", "X_fa",
            "X_c4", "X_pro", "X_ac", "X_h2", "X_I", "X_PHA", "X_PP", "X_PAO",
            "S_K", "S_Mg",
            "S_SO4", "S_IS", "X_hSRB", "X_aSRB", "X_pSRB", "X_c4SRB", "S_S0", "S_Fe3", "S_Fe2",
            "X_HFO_H", "X_HFO_L", "X_HFO_old", "X_HFO_HP", "X_HFO_LP", "X_HFO_HP_old", "X_HFO_LP_old", "X_FeS",
            "X_Fe3PO42", "X_Siderite", "X_Mackinawite", "X_struv",
            "X_Sorg",
        ],
        state_vars=["flow_vol", "conc_mass_comp", "temperature", "pressure"],
        flow_basis="volumetric",
        requires_reaction_package=True,
        compatible_reaction_packages=['watertap_engine_mcp.chemistry.modified_adm1_sfe'],
        # the organic-sulfur S/Fe kernel block is the only reaction model that closes this state (R1-R68 + R69)
        reaction_module_path='watertap_engine_mcp.chemistry.modified_adm1_sfe',
        reaction_class_name="SFEOrganicSKernelADM1ReactionParameterBlock",
        default_scaling={
            "flow_vol": 1e5,
            "conc_mass_comp": 1e2,
        },
    ),

    PropertyPackageType.MODIFIED_ADM1_SFE_MICROAERATION: PropertyPackageSpec(
        pkg_type=PropertyPackageType.MODIFIED_ADM1_SFE_MICROAERATION,
        class_name="MicroaerationModifiedADM1ParameterBlock",
        module_path='watertap_engine_mcp.chemistry.modified_adm1_microaeration',
        phases={"Liq"},
        required_components=[
            "H2O", "S_su", "S_aa", "S_fa", "S_va", "S_bu", "S_pro",
            "S_ac", "S_h2", "S_ch4", "S_IC", "S_IN", "S_IP", "S_I",
            "X_ch", "X_pr", "X_li", "X_su", "X_aa", "X_fa",
            "X_c4", "X_pro", "X_ac", "X_h2", "X_I", "X_PHA", "X_PP", "X_PAO",
            "S_K", "S_Mg",
            "S_SO4", "S_IS", "X_hSRB", "X_aSRB", "X_pSRB", "X_c4SRB", "S_S0", "S_Fe3", "S_Fe2",
            "X_HFO_H", "X_HFO_L", "X_HFO_old", "X_HFO_HP", "X_HFO_LP", "X_HFO_HP_old", "X_HFO_LP_old", "X_FeS",
            "X_Fe3PO42", "X_Siderite", "X_Mackinawite", "X_struv",
            "X_Sorg", "S_O2",
        ],
        state_vars=["flow_vol", "conc_mass_comp", "temperature", "pressure"],
        flow_basis="volumetric",
        requires_reaction_package=True,
        compatible_reaction_packages=['watertap_engine_mcp.chemistry.modified_adm1_microaeration'],
        # the micro-aeration kernel block is the only reaction model that closes this state (R1-R62 + R63-R70)
        reaction_module_path='watertap_engine_mcp.chemistry.modified_adm1_microaeration',
        reaction_class_name="MicroaerationKernelADM1ReactionParameterBlock",
        default_scaling={
            "flow_vol": 1e5,
            "conc_mass_comp": 1e2,
        },
    ),

    PropertyPackageType.ADM1_SFE_VAPOR: PropertyPackageSpec(
        pkg_type=PropertyPackageType.ADM1_SFE_VAPOR,
        class_name="SFEADM1VaporParameterBlock",
        module_path='watertap_engine_mcp.chemistry.modified_adm1_sfe',
        phases={"Vap"},
        required_components=["H2O", "S_h2", "S_ch4", "S_co2", "S_h2s"],
        state_vars=["flow_vol", "conc_mass_comp", "temperature", "pressure"],
        flow_basis="volumetric",
        default_scaling={
            "flow_vol": 1e5,
        },
    ),

    PropertyPackageType.ADM1_SFE_MICROAERATION_VAPOR: PropertyPackageSpec(
        pkg_type=PropertyPackageType.ADM1_SFE_MICROAERATION_VAPOR,
        class_name="MicroaerationADM1VaporParameterBlock",
        module_path='watertap_engine_mcp.chemistry.modified_adm1_microaeration',
        phases={"Vap"},
        required_components=["H2O", "S_h2", "S_ch4", "S_co2", "S_h2s", "S_o2", "S_n2"],
        state_vars=["flow_vol", "conc_mass_comp", "temperature", "pressure"],
        flow_basis="volumetric",
        default_scaling={
            "flow_vol": 1e5,
        },
    ),

    PropertyPackageType.ADM1_VAPOR: PropertyPackageSpec(
        pkg_type=PropertyPackageType.ADM1_VAPOR,
        class_name="ADM1_vaporParameterBlock",
        module_path="watertap.property_models.unit_specific.anaerobic_digestion.adm1_properties_vapor",
        phases={"Vap"},
        required_components=["H2O", "S_h2", "S_ch4", "S_co2"],
        state_vars=["flow_vol", "conc_mass_comp", "temperature", "pressure"],
        flow_basis="volumetric",
        default_scaling={
            "flow_vol": 1e5,
        },
    ),

    # ==================== GAS-PHASE SEPARATION (BIOGAS UPGRADING) ====================

    PropertyPackageType.NATURAL_GAS_PR: PropertyPackageSpec(
        pkg_type=PropertyPackageType.NATURAL_GAS_PR,
        # GenericParameterBlock(**get_prop(components, ["Vap"], eos=EosType.PR)) —
        # config is auto-built in ModelBuilder._build_package_config, NOT user-required.
        class_name="GenericParameterBlock",
        module_path="idaes.models.properties.modular_properties.base.generic_property",
        phases={"Vap"},
        required_components=["CH4", "CO2"],
        optional_components=["N2", "H2O", "H2S", "O2"],
        # FTPx state definition (total flow_mol + mole_frac_comp + T + P)
        state_vars=["flow_mol", "mole_frac_comp", "temperature", "pressure"],
        flow_basis="molar",
        config_kwargs={"components": "CH4,CO2,N2", "eos": "PR"},
        config_fields=["components", "eos"],
        default_scaling={
            "flow_mol": 1e-1,
            "temperature": 1e-2,
            "pressure": 1e-6,
        },
    ),
}


ASM2D_PROPERTY_PACKAGE_TYPES = frozenset(
    {PropertyPackageType.ASM2D, PropertyPackageType.MODIFIED_ASM2D}
)

ASM2D_PROPERTY_BLOCK_CLASS_NAMES = frozenset(
    {
        "ASM2dParameterBlock",
        "_ScalarASM2dParameterBlock",
        "ModifiedASM2dParameterBlock",
        "_ScalarModifiedASM2dParameterBlock",
    }
)


@dataclass(frozen=True)
class ReactionPackageVariant:
    """A named alternative reaction package for a property package (the property package is unchanged).

    The ADM1 templates name one by its key in the AD unit spec's ``reaction_package`` config; ``ModelBuilder``
    builds it paired with ``pairs_with``'s live property block, so the unit's state and its reactions share one
    property package.
    """

    name: str
    pairs_with: PropertyPackageType
    module_path: str
    class_name: str
    note: str


#: Named reaction-package variants. ``MADM1_KERNEL`` is the shared mADM1 biokinetic kernel compiled into WaterTAP

REACTION_PACKAGE_VARIANTS: Dict[str, ReactionPackageVariant] = {
    "MADM1_KERNEL": ReactionPackageVariant(
        name="MADM1_KERNEL",
        pairs_with=PropertyPackageType.MODIFIED_ADM1,
        module_path='watertap_engine_mcp.chemistry.modified_adm1_kernel',
        class_name="KernelADM1ReactionParameterBlock",
        note=("process_model_schema.madm1_biokinetics (the twin's mADM1 core) over the house Davies acid-base; "
              "parameters from the digester design set read through custody"),
    ),
    "MADM1_SFE_KERNEL": ReactionPackageVariant(
        name="MADM1_SFE_KERNEL",
        pairs_with=PropertyPackageType.MODIFIED_ADM1_SFE,
        module_path='watertap_engine_mcp.chemistry.modified_adm1_sfe',
        class_name="SFEKernelADM1ReactionParameterBlock",
        note=("process_model_schema.madm1_sfe_kinetics (the twin's mADM1 core + its sulfur/iron processes 25-55, FeS) "
              "over the house Davies acid-base with sulfide, sulfate and iron explicit; core and S/Fe parameters "
              "from the digester design sets read through custody"),
    ),
    "MADM1_SFE_ORGANIC_S_KERNEL": ReactionPackageVariant(
        name="MADM1_SFE_ORGANIC_S_KERNEL",
        pairs_with=PropertyPackageType.MODIFIED_ADM1_SFE_ORGANIC_S,
        module_path='watertap_engine_mcp.chemistry.modified_adm1_sfe',
        class_name="SFEOrganicSKernelADM1ReactionParameterBlock",
        note=("the MADM1_SFE_KERNEL block plus the opt-in process_model_schema.madm1_organic_sulfur extension "
              "(X_Sorg -> S_IS at q_pr_hyd x X_Sorg, the twin's process 75, as R62); compiled only when a digester "
              "asks for organic sulfur"),
    ),
    "MADM1_SFE_MICROAERATION_KERNEL": ReactionPackageVariant(
        name="MADM1_SFE_MICROAERATION_KERNEL",
        pairs_with=PropertyPackageType.MODIFIED_ADM1_SFE_MICROAERATION,
        module_path='watertap_engine_mcp.chemistry.modified_adm1_microaeration',
        class_name="MicroaerationKernelADM1ReactionParameterBlock",
        note=("the MADM1_SFE_ORGANIC_S_KERNEL block plus the opt-in process_model_schema.madm1_microaeration "
              "extension: Botheju's aerobic uptakes and the S0 re-reduction bracket (twin 78-85, as R63-R70) and the O2 "
              "inhibition of the anaerobic and SRB uptakes; the headspace oxidation and O2 transfer (twin 76-77) are "
              "the SFEAD(oxidant_inlet=True) unit's; compiled only for an oxidant-dosed digester"),
    ),
}


def is_asm2d_property_package_type(pkg_type: Any) -> bool:
    """Return True only for the supported ASM2d-family package enum values."""

    if isinstance(pkg_type, PropertyPackageType):
        return pkg_type in ASM2D_PROPERTY_PACKAGE_TYPES
    if isinstance(pkg_type, str):
        normalized = pkg_type.strip().upper()
        return normalized in {pkg.name for pkg in ASM2D_PROPERTY_PACKAGE_TYPES}
    return False


def is_asm2d_property_block(package_block: Any) -> bool:
    """Return True for concrete WaterTAP ASM2d-family property blocks."""

    return type(package_block).__name__ in ASM2D_PROPERTY_BLOCK_CLASS_NAMES


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


def build_property_package(
    parent: Any,
    name: str,
    pkg_type: PropertyPackageType,
    config_kwargs: Optional[Dict[str, Any]] = None,
) -> Any:
    'Construct a registered property package on ``parent`` and correct it.'
    import importlib

    from .watertap_corrections import apply_property_corrections

    spec = get_property_package_spec(pkg_type)
    module = importlib.import_module(spec.module_path)
    package_class = getattr(module, spec.class_name)
    block = package_class(**config_kwargs) if config_kwargs else package_class()
    parent.add_component(name, block)
    apply_property_corrections(block)
    return block


def get_import_statement(pkg_type: PropertyPackageType) -> str:
    """Generate Python import statement for a property package.

    Args:
        pkg_type: The property package type

    Returns:
        Python import statement string
    """
    spec = get_property_package_spec(pkg_type)
    return f"from {spec.module_path} import {spec.class_name}"
