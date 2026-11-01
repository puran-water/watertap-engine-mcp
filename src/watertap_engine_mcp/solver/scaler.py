"""Scaling Tools for WaterTAP Flowsheets.

Provides tools for managing variable and constraint scaling in WaterTAP models.
Wraps IDAES scaling utilities and provides unit-specific defaults.
"""

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence


DEFAULT_ASM2D_SCALING_FACTORS: Dict[str, float] = {
    "S_O2": 1e3,
    "S_F": 1e3,
    "S_A": 1e3,
    "S_NH4": 1e2,
    "S_PO4": 1e3,
    "S_ALK": 1e3,
    "S_IC": 1e3,
    "S_K": 1e2,
    "S_Mg": 1e2,
    "S_I": 1e2,
    "S_N2": 1e2,
    "X_I": 1e1,
    "X_S": 1e2,
    "X_H": 1e1,
    "X_AUT": 1e1,
    "X_PAO": 1e1,
    "X_PP": 1e2,
    "X_PHA": 1e2,
    "X_TSS": 1e0,
    # X_MeOH/X_MeP are bulk products of the chemical-P (metal-salt precipitation)
    # unit at ~0.01-0.05 kg/m3, not trace metals. A 1e7 factor (sized for ~1e-9
    # background) makes their Jacobian entries ~1e5 in every state block and
    # ill-conditions the recycle loop. 1e2 matches the analogous X_S particulate
    # and is safe in the no-dose regime (1e-9 * 1e2 = 1e-7, near-zero, harmless).
    "X_MeOH": 1e2,
    "X_MeP": 1e2,
    "S_NO3": 1e2,
}

# Anaerobic-digestion (ADM1 / mADM1) liquid-species concentration scaling. The
# ADM1 state spans low-concentration dissolved gases (S_h2 ~1e-5..1e-7 kg/m3),
# moderate VFAs / solubles (~1e-3..0.2), inorganic carbon/nitrogen/phosphorus
# (~0.1..1), strong ions (kmol/m3, ~0.02..0.4), and high particulate substrate /
# biomass (~0.1..25). The WaterTAP modified-ADM1 reference flowsheet conditions
# the whole digester with a flat conc_mass_comp factor of 1e1 alongside
# flow_vol=1e0, temperature=1e-2, pressure=1e-5 (see ``DEFAULT_ADM1_STATE_SCALING``);
# that flat scaling is the proven-convergent baseline. These per-species factors
# stay on that baseline except for dissolved S_h2, which is several orders of
# magnitude smaller than every other species and needs a large factor so its
# Jacobian row is not numerically invisible. Applied AFTER the recursive IDAES
# pass (mirroring the reference ``scale_variables`` override) so the unit/property
# block constraint transforms keep their own conditioning while the state-var
# scaling is corrected to the digester-appropriate magnitudes.
DEFAULT_ADM1_SCALING_FACTORS: Dict[str, float] = {
    # Dissolved hydrogen: ~1e-5..1e-7 kg COD/m3 -> large factor (very high).
    "S_h2": 1e3,
    # Soluble substrates / VFAs / dissolved methane (kg COD/m3).
    "S_su": 1e1, "S_aa": 1e1, "S_fa": 1e1, "S_va": 1e1, "S_bu": 1e1,
    "S_pro": 1e1, "S_ac": 1e1, "S_ch4": 1e1,
    # Inorganic carbon / nitrogen / phosphorus + inert solubles (~0.05..1).
    "S_IC": 1e1, "S_IN": 1e1, "S_IP": 1e1, "S_I": 1e1,
    # Strong ions carried in conc_mass_comp on modified ADM1 (kmol/m3 scale).
    "S_K": 1e2, "S_Mg": 1e2,
    # Particulate substrate + biomass (kg COD/m3, ~0.1..25).
    "X_c": 1e1, "X_ch": 1e1, "X_pr": 1e1, "X_li": 1e1, "X_su": 1e1, "X_aa": 1e1,
    "X_fa": 1e1, "X_c4": 1e1, "X_pro": 1e1, "X_ac": 1e1, "X_h2": 1e1, "X_I": 1e1,
    "X_PHA": 1e1, "X_PP": 1e1, "X_PAO": 1e1,
    # The S/Fe digester (chemistry.modified_adm1_sfe): dissolved sulfide and the headspace H2S (~0.005..0.05 kg S/m3)
    # and the SRB guilds (~1e-4..1e-2 kg COD/m3) sit an order below the core; the rest keep the 1e1 fallback.
    "S_IS": 1e2, "S_h2s": 1e2,
    "X_hSRB": 1e2, "X_aSRB": 1e2, "X_pSRB": 1e2, "X_c4SRB": 1e2,
    # The S/Fe digester's iron minerals (process_model_schema.madm1_iron_minerals): kg of the mineral/m3, from the
    # nucleation seed (1e-6) to the dosed digester's ~0.5.
    "X_Fe3PO42": 1e2, "X_Siderite": 1e2, "X_Mackinawite": 1e2,
    # Its struvite (kg MgNH4PO4/m3): from the seed to a layer-manure digester's ~4 (its Mg), on S_IN's scale.
    "X_struv": 1e1,
    # The opt-in organic-sulfur variant: particulate organic S (kg S/m3), on the dissolved sulfide's scale.
    "X_Sorg": 1e2,
    # The opt-in micro-aeration variant: dissolved O2 (kg O2/m3; nM-to-uM in a digester liquor).
    "S_O2": 1e4,
}

# Structural (non-concentration) ADM1 state-variable scaling, applied to both the
# liquid and vapor digester state blocks plus the cations/anions strong-ion state
# vars and the reaction-block H+ reference. Mirrors the reference flowsheet's
# flow_vol=1e0 / temperature=1e-2 / pressure=1e-5 override.
DEFAULT_ADM1_STATE_SCALING: Dict[str, float] = {
    "flow_vol": 1e0,
    "temperature": 1e-2,
    "pressure": 1e-5,
    "anions": 1e2,
    "cations": 1e2,
    "S_H": 1e7,
}

# The feed-flow basis (m3/s) at which the WaterTAP AD's hard-coded extensive-rate
# scaling (rate_reaction_extent/generation = 1e4, mass_transfer_term = 1e2 in
# ``AD.calculate_scaling_factors``) is proven convergent — the canonical 259.2
# m3/d mADM1 UASB preset. Those factors are ABSOLUTE-magnitude constants while
# the underlying variables are extensive (extent ~ Q * delta-conc), so at a
# larger plant flow the scaled Jacobian drifts off the proven conditioning and
# the digester's own initialize_build degrades (reproduced at 2000 m3/d /
# default 20.06 d HRT: init terminal check near-feasible, flowsheet solve
# maxIterations). ``apply_adm1_species_scaling`` normalizes the extensive-rate
# factors by (reference flow / actual feed flow) so the SCALED magnitudes stay
# at the proven canonical values at any absolute plant scale; at the canonical
# flow the ratio is exactly 1.0 and behavior is unchanged.
ADM1_REFERENCE_FEED_FLOW_M3_S = 259.2 / 86400.0
ADM1_EXTENSIVE_RATE_BASE_SCALING: Dict[str, float] = {
    "rate_reaction_extent": 1e4,
    "rate_reaction_generation": 1e4,
    "mass_transfer_term": 1e2,
}

# ADM1 material-balance row scaling, applied after the recursive IDAES pass.
# These factors are inverse expected mass-flow magnitudes, not concentration-state
# factors. High-recycle EGSB basins can carry S_IP / X_I at tens of kg/m3, so the
# material-balance rows need much smaller factors than the flat 1e1 state scaling.
ADM1_MATERIAL_BALANCE_SCALING_FACTORS: Dict[str, float] = {
    "H2O": 1e-2,
    "S_h2": 1e3,
    "S_su": 1e0,
    "S_aa": 1e0,
    "S_fa": 1e0,
    "S_va": 1e0,
    "S_bu": 1e0,
    "S_pro": 1e0,
    "S_ac": 1e0,
    "S_ch4": 1e0,
    "S_IC": 1e-1,
    "S_IN": 1e-1,
    "S_IP": 1e-2,
    "S_I": 1e-1,
    "S_K": 1e-1,
    "S_Mg": 1e-1,
    "X_c": 1e-2,
    "X_ch": 1e-2,
    "X_pr": 1e-2,
    "X_li": 1e-2,
    "X_su": 1e-2,
    "X_aa": 1e-2,
    "X_fa": 1e-2,
    "X_c4": 1e-2,
    "X_pro": 1e-2,
    "X_ac": 1e-2,
    "X_h2": 1e-2,
    "X_I": 1e-2,
    "X_PHA": 1e-2,
    "X_PP": 1e-2,
    "X_PAO": 1e-2,
}


DEFAULT_TOPOLOGY_SCALING_CLASSES: tuple[str, ...] = (
    "mass_balance",
    "reaction_rates",
    "hydraulic",
    "retention",
    "recycle_ratios",
)

ASM2D_ARC_CONCENTRATION_SCALING_CAP = 1e4

# Gas-phase (biogas upgrading) scaling for the natural_gas_PR / GasMembraneStage1D
# path. The FTPx state spans high pressure (~1e6 Pa), moderate molar flow (~1-100
# mol/s), and dimensionless permeance/mole-fractions. The cascade converges with
# IDAES default scaling today; these factors are available to harden conditioning if
# larger trains or tighter recycles need it (apply via set_scaling_factor by var name).
GAS_MEMBRANE_SCALING_FACTORS: Dict[str, float] = {
    "flow_mol": 1e-1,
    "pressure": 1e-6,
    "temperature": 1e-2,
    "mole_frac_comp": 1e0,
    "permeance": 1e0,
    "area": 1e-4,
    "cell_area": 1e-3,
}


def _is_natural_gas_pr_property_package(package: Any) -> bool:
    """True for the IDAES modular natural_gas_PR (GenericParameterBlock) gas package."""
    if package is None:
        return False
    return "GenericParameterBlock" in type(package).__name__ and hasattr(package, "CH4")


# MVC / ZLD thermal-train scaling (WaterTAP mvc unit family: Evaporator /
# Compressor / Condenser, plus the crystallizer's NaCl feed chain). Mirrors the
# scaling the packaged WaterTAP MVC reference flowsheet
# (watertap/flowsheets/mvc/mvc_single_stage.py) applies by hand:
#   * per-package flow_mass_phase_comp default scaling (Liq/H2O ~1 kg/s basis,
#     the TDS/NaCl minority components two orders finer),
#   * the evaporator temperature-difference / LMTD variables at the ~10 K scale,
#   * the compressor work and condenser heat at the ~1e5-1e6 W scale.
# Without these the recursive IDAES pass CRASHES on the MVC compressor
# (``Compressor.calculate_scaling_factors`` divides by the — absent — scaling
# factor of ``control_volume.work``), which historically left the whole ZLD
# model unscaled and made the Condenser initialization converge to a locally
# infeasible point at small plant flows.
MVC_FLOW_MASS_DEFAULT_SCALING: tuple = (
    (("Liq", "H2O"), 1e0),
    (("Liq", "TDS"), 1e2),
    (("Vap", "H2O"), 1e0),
    (("Liq", "NaCl"), 1e2),
    (("Sol", "NaCl"), 1e2),
)


def _is_mvc_thermal_unit(unit: Any) -> bool:
    """True for the WaterTAP MVC unit family (Evaporator/Compressor/Condenser).

    Scalar Pyomo process blocks are dynamically generated (``_ScalarCondenser``
    in ``idaes.core.base.process_block``), so the MVC family membership is read
    off the MRO — the ``...Data`` base class carries the defining module.
    """

    try:
        mro = type(unit).__mro__
    except Exception:
        return False
    return any(
        "watertap.unit_models.mvc" in (getattr(klass, "__module__", "") or "")
        for klass in mro
    )


def _model_uses_mvc_thermal(model: Any, units: Optional[Dict[str, Any]] = None) -> bool:
    """True when the flowsheet carries WaterTAP MVC thermal units."""

    if units and any(_is_mvc_thermal_unit(unit) for unit in units.values()):
        return True
    try:
        from pyomo.environ import Block
    except ImportError:
        return False
    if not hasattr(model, "component_objects"):
        return False
    try:
        for blk in model.component_objects(Block, descend_into=True):
            if _is_mvc_thermal_unit(blk):
                return True
    except Exception:
        return False
    return False


def apply_mvc_thermal_scaling(model: Any, units: Optional[Dict[str, Any]] = None) -> int:
    """Apply the MVC/ZLD thermal-train scaling catalog.

    Fills the per-package ``flow_mass_phase_comp`` default scaling (only where a
    default is absent, so caller/package-provided factors win) and sets the unit
    variable factors the WaterTAP MVC family's own ``calculate_scaling_factors``
    assumes to exist (compressor work, condenser heat, evaporator dT/LMTD).
    Must run BEFORE the recursive IDAES pass: the MVC compressor's scaling
    routine raises a ``TypeError`` when ``control_volume.work`` carries no
    factor, aborting the whole model's recursive scaling.
    """

    try:
        import idaes.core.util.scaling as iscale
        from idaes.core import PhysicalParameterBlock
    except ImportError:
        return 0

    if not hasattr(model, "component_objects"):
        return 0

    applied = 0

    fs = getattr(model, "fs", None)
    seen_params: set = set()
    for blk in (model, fs):
        if blk is None or not hasattr(blk, "component_objects"):
            continue
        try:
            components = list(blk.component_objects(descend_into=False))
        except Exception:
            continue
        for param in components:
            if not isinstance(param, PhysicalParameterBlock):
                continue
            if id(param) in seen_params:
                continue
            seen_params.add(id(param))
            defaults = getattr(param, "default_scaling_factor", None)
            if defaults is None:
                continue
            for index, factor in MVC_FLOW_MASS_DEFAULT_SCALING:
                if ("flow_mass_phase_comp", index) in defaults:
                    continue
                try:
                    param.set_default_scaling(
                        "flow_mass_phase_comp", factor, index=index
                    )
                    applied += 1
                except Exception:
                    continue

    unit_blocks = list((units or {}).values())
    if not unit_blocks and fs is not None:
        try:
            from pyomo.environ import Block

            unit_blocks = list(fs.component_objects(Block, descend_into=False))
        except Exception:
            unit_blocks = []
    for unit in unit_blocks:
        if not _is_mvc_thermal_unit(unit):
            continue
        class_name = type(unit).__name__.lower()
        targets = []
        if "compressor" in class_name:
            targets.append((getattr(getattr(unit, "control_volume", None), "work", None), 1e-5))
        elif "condenser" in class_name:
            targets.append((getattr(getattr(unit, "control_volume", None), "heat", None), 1e-5))
        elif "evaporator" in class_name:
            targets.extend(
                (getattr(unit, name, None), 1e-1)
                for name in ("delta_temperature_in", "delta_temperature_out", "lmtd")
            )
            targets.append((getattr(unit, "area", None), 1e-2))
            targets.append((getattr(unit, "U", None), 1e-3))
        for component, factor in targets:
            if component is None:
                continue
            # The MVC unit scaling routines look the factor up on the PARENT
            # component (get_scaling_factor(self.control_volume.work)), so the
            # existence probe must be parent-level too: a data-level entry left
            # by the control-volume defaults does NOT satisfy the parent lookup
            # and still crashes the recursive pass.
            try:
                existing = iscale.get_scaling_factor(component)
            except Exception:
                existing = None
            if existing is not None:
                continue
            if _set_scaling_factor(iscale, component, factor):
                applied += 1
    return applied


def _component_from_index(index: Any) -> Optional[str]:
    """Return a component label from a Pyomo concentration index."""

    if isinstance(index, tuple):
        for item in reversed(index):
            if isinstance(item, str):
                return item
        return None
    if isinstance(index, str):
        return index
    return None


def _asm2d_constraint_component_factor(component: Optional[str]) -> float:
    if component == "H2O":
        return 1e-2
    if component == "S_ALK":
        return 1.0
    return min(
        DEFAULT_ASM2D_SCALING_FACTORS.get(component or "", 1e3),
        1e3,
    )


def _constraint_in_adm1_block(constraint: Any) -> bool:
    """True when a constraint belongs to an ADM1-backed block."""

    try:
        block = constraint.parent_block()
    except Exception:
        return False
    while block is not None:
        if _is_adm1_property_package(_state_block_property_package(block)):
            return True
        config = getattr(block, "config", None)
        if config is not None:
            for attr in (
                "property_package",
                "liquid_property_package",
                "vapor_property_package",
                "reaction_package",
            ):
                if _is_adm1_property_package(getattr(config, attr, None)):
                    return True
        try:
            block = block.parent_block()
        except Exception:
            return False
    return False


def _adm1_material_balance_constraint_factor(constraint: Any) -> Optional[float]:
    name = str(getattr(constraint, "name", ""))
    if "material_balances" not in name and "material_mixing_equations" not in name:
        return None
    if not _constraint_in_adm1_block(constraint):
        return None
    component = _component_from_index(constraint.index())
    return ADM1_MATERIAL_BALANCE_SCALING_FACTORS.get(component or "", 1e0)


def _adm1_recycle_constraint_factor(
    constraint: Any,
    *,
    include_gas_transfer_scaling: bool = False,
) -> Optional[float]:
    """ADM1 recycle-family row scaling applied after recursive IDAES scaling."""

    material_factor = _adm1_material_balance_constraint_factor(constraint)
    if material_factor is not None:
        return material_factor
    name = str(getattr(constraint, "name", ""))
    if "AD_retention_time" in name:
        if not _constraint_in_adm1_block(constraint):
            return None
        return 1e-5
    if include_gas_transfer_scaling and "Sh2_conc" in name:
        if not _constraint_in_adm1_block(constraint):
            return None
        return 1e2
    if include_gas_transfer_scaling and "outlet_P" in name:
        if not _constraint_in_adm1_block(constraint):
            return None
        return 1e-5
    if "unit_enthalpy_balance" not in name:
        return None
    if not _constraint_in_adm1_block(constraint):
        return None
    return 1e-10


def _asm2d_pressure_changer_h2o_constraint_factor(constraint: Any) -> Optional[float]:
    """Post-recursive scaling for recycle-pump H2O material-balance rows."""

    name = str(getattr(constraint, "name", ""))
    lower = name.lower()
    if "material_balances" not in lower:
        return None
    if "pump" not in lower and "pressure" not in lower:
        return None
    if _component_from_index(constraint.index()) != "H2O":
        return None
    return 5e-2


def _set_scaling_factor(iscale: Any, component: Any, factor: float) -> bool:
    """Set a scaling factor across IDAES versions."""

    try:
        iscale.set_scaling_factor(component, factor, overwrite=True)
        return True
    except TypeError:
        try:
            iscale.set_scaling_factor(component, factor)
            return True
        except Exception:
            return False
    except Exception:
        return False


def _scaling_classes(
    scaling_classes: Optional[Sequence[str]],
) -> set[str]:
    if scaling_classes is None:
        return set(DEFAULT_TOPOLOGY_SCALING_CLASSES)
    if not scaling_classes:
        return set(DEFAULT_TOPOLOGY_SCALING_CLASSES)
    return set(scaling_classes)


def _enabled(classes: set[str], *names: str) -> bool:
    return any(name in classes for name in names)


def _scaled_component_count(component: Any, iscale: Any) -> int:
    try:
        if iscale.get_scaling_factor(component) is not None:
            return 1
    except Exception:
        pass
    return 0


def _is_asm2d_property_package(package: Any) -> bool:
    """Return True when a unit property package is ASM2D-backed."""

    if package is None:
        return False
    package_type = type(package).__name__
    package_text = f"{package_type} {package}"
    return "ASM2d" in package_text or "ASM2D" in package_text


def _unit_uses_asm2d(unit: Any) -> bool:
    config = getattr(unit, "config", None)
    if _is_asm2d_property_package(getattr(config, "property_package", None)):
        return True
    return False


def _model_uses_asm2d(model: Any, units: Optional[Dict[str, Any]] = None) -> bool:
    if units and any(_unit_uses_asm2d(unit) for unit in units.values()):
        return True
    config = getattr(model, "config", None)
    return _is_asm2d_property_package(getattr(config, "property_package", None))


def _is_adm1_property_package(package: Any) -> bool:
    """Return True for an ADM1 / modified-ADM1 / ADM1-vapor property package."""

    if package is None:
        return False
    package_text = f"{type(package).__name__} {package}"
    return "ADM1" in package_text or "adm1" in package_text


def _state_block_property_package(block: Any) -> Any:
    """Return the property/reaction parameter block a state or reaction block binds to.

    IDAES state blocks expose their parameter block as ``.params`` (reaction
    blocks likewise). Resolving the parameter block lets the species scalers
    scope themselves to the package that *owns* a variable's block, so a mixed
    ASM2D+ADM1 flowsheet conditions each family by its own factors instead of
    keying purely on the variable name (which collides on shared species such as
    ``S_I`` / ``X_PAO`` and on the structural ``flow_vol`` / ``temperature`` /
    ``pressure`` vars present on every state block).
    """

    for attr in ("params", "_params"):
        params = getattr(block, attr, None)
        if params is not None:
            return params
    return None


def _var_in_adm1_block(var: Any) -> bool:
    """True when a variable's parent block is owned by an ADM1-family package."""

    try:
        block = var.parent_block()
    except Exception:
        return False
    return _is_adm1_property_package(_state_block_property_package(block))


def _unit_uses_adm1(unit: Any) -> bool:
    """Return True when a unit is backed by an ADM1-family property package.

    Covers the anaerobic digester (AD) which carries both a liquid ADM1 package
    and a vapor ADM1 package, and any unit whose default/liquid property package
    is ADM1-backed. Keyed on the property package, not the unit name.
    """

    config = getattr(unit, "config", None)
    if config is None:
        return False
    for attr in (
        "property_package",
        "liquid_property_package",
        "vapor_property_package",
        "reaction_package",
    ):
        if _is_adm1_property_package(getattr(config, attr, None)):
            return True
    return False


def _model_uses_adm1(model: Any, units: Optional[Dict[str, Any]] = None) -> bool:
    """Return True when the flowsheet carries an ADM1-family digester."""

    if units and any(_unit_uses_adm1(unit) for unit in units.values()):
        return True
    config = getattr(model, "config", None)
    if _is_adm1_property_package(getattr(config, "property_package", None)):
        return True
    try:
        from pyomo.environ import Block
    except ImportError:
        return False
    if not hasattr(model, "component_objects"):
        return False
    try:
        for blk in model.component_objects(Block, descend_into=True):
            if _is_adm1_property_package(blk):
                return True
    except Exception:
        return False
    return False


def _model_uses_natural_gas_pr(model: Any, units: Optional[Dict[str, Any]] = None) -> bool:
    """True when the flowsheet carries a natural_gas_PR (gas membrane) property package."""
    if units:
        for unit in units.values():
            cfg = getattr(unit, "config", None)
            if _is_natural_gas_pr_property_package(getattr(cfg, "property_package", None)):
                return True
            for side in ("feed_side", "sweep_side"):
                sidecfg = getattr(cfg, side, None)
                pkg = sidecfg.get("property_package") if isinstance(sidecfg, dict) \
                    else getattr(sidecfg, "property_package", None)
                if _is_natural_gas_pr_property_package(pkg):
                    return True
    try:
        from pyomo.environ import Block
    except ImportError:
        return False
    if not hasattr(model, "component_objects"):
        return False
    try:
        for blk in model.component_objects(Block, descend_into=True):
            if _is_natural_gas_pr_property_package(blk):
                return True
    except Exception:
        return False
    return False


def _transform_constraint(iscale: Any, constraint: Any, factor: float) -> bool:
    try:
        iscale.constraint_scaling_transform(constraint, factor, overwrite=True)
        if _scaled_component_count(constraint, iscale):
            return True
        return _set_scaling_factor(iscale, constraint, factor)
    except TypeError:
        try:
            iscale.constraint_scaling_transform(constraint, factor)
            if _scaled_component_count(constraint, iscale):
                return True
            return _set_scaling_factor(iscale, constraint, factor)
        except Exception:
            return _set_scaling_factor(iscale, constraint, factor)
    except Exception:
        return _set_scaling_factor(iscale, constraint, factor)


def _var_factor(var: Any, classes: set[str]) -> Optional[float]:
    parent = var.parent_component()
    parent_name = getattr(parent, "local_name", getattr(parent, "name", ""))
    var_name = str(getattr(var, "name", ""))

    if _enabled(classes, "hydraulic"):
        if parent_name == "flow_vol" or "flow_vol" in var_name:
            return 1e3
        if "pressure" in parent_name or "pressure" in var_name:
            return 1e-5
        if parent_name == "temperature" or "temperature" in var_name:
            return 1e-2
        if parent_name == "alkalinity" or "alkalinity" in var_name:
            return 1e3
        if parent_name == "split_fraction":
            return 1.0
        if parent_name == "deltaP":
            return 1e-5

    if _enabled(classes, "retention"):
        if parent_name == "volume" or var_name.endswith(".volume[0.0]"):
            return 1e-3
        if parent_name == "hydraulic_retention_time":
            return 1e-3

    if _enabled(classes, "mass_balance") and parent_name == "conc_mass_comp":
        component = _component_from_index(var.index())
        return DEFAULT_ASM2D_SCALING_FACTORS.get(component or "", 1e3)

    if _enabled(classes, "reaction_rates"):
        if parent_name in {"KLa", "injection"}:
            return 1e-1 if parent_name == "KLa" else 1e3
        if (
            "rate_reaction_extent" in var_name
            or "reaction_rate" in var_name
            or "mass_transfer_term" in var_name
        ):
            return 1e3

    if _enabled(classes, "recycle_ratios") and "recycle_ratio" in var_name:
        return 1.0

    # Freed chemical-P metal dose (kg/m^3, O(1e-2)) when effluent-TP target
    # control is active; scale it to order 1 like the other manipulated vars.
    if parent_name == "metal_dose" or "metal_dose" in var_name:
        return 1e2
    if parent_name == "caustic_dose" or "caustic_dose" in var_name:
        return 1.0
    if parent_name == "carbon_dose" or "carbon_dose" in var_name:
        return 1e2

    return None


def _constraint_factor(constraint: Any, classes: set[str]) -> Optional[float]:
    name = str(getattr(constraint, "name", ""))

    if _enabled(classes, "retention") and "retention_time" in name:
        return 1e-3

    if _enabled(classes, "hydraulic"):
        if "sum_split_frac" in name:
            return 1.0
        if "flow_vol" in name or "flow_balance" in name:
            return 1e3
        if "pressure" in name:
            return 1e-4
        if "temperature" in name:
            return 1e-2
        if "alkalinity" in name:
            return 1e3

    if _enabled(classes, "mass_balance") and "conc_mass_comp_equality" in name:
        component = _component_from_index(constraint.index())
        return min(
            DEFAULT_ASM2D_SCALING_FACTORS.get(component or "", 1e3),
            ASM2D_ARC_CONCENTRATION_SCALING_CAP,
        )

    if _enabled(classes, "mass_balance") and "carbon_balance" in name:
        return DEFAULT_ASM2D_SCALING_FACTORS.get("S_A", 1e3)

    if _enabled(classes, "mass_balance") and "component_passthrough" in name:
        component = _component_from_index(constraint.index())
        return _asm2d_constraint_component_factor(component)

    if _enabled(classes, "mass_balance") and "material_balances" in name:
        component = _component_from_index(constraint.index())
        return _asm2d_constraint_component_factor(component)

    if _enabled(classes, "mass_balance") and (
        "enthalpy_balances" in name or "enthalpy_mixing_equations" in name
    ):
        return 1e-10

    if _enabled(classes, "reaction_rates") and "mass_transfer" in name:
        return 1e3

    if _enabled(classes, "reaction_rates") and "rate_expression" in name:
        return 1e3

    if _enabled(classes, "reaction_rates") and any(
        token in name
        for token in (
            "rate_reaction",
            "reaction_rate",
            "cstr_performance",
        )
    ):
        return 1e3

    if _enabled(classes, "reaction_rates") and "electricity_consumption" in name:
        return 1e-3

    if _enabled(classes, "recycle_ratios") and "recycle_ratio" in name:
        return 1e2

    if "process_constraint_" in name and "srt" in name:
        return 1.0

    # Effluent total-P target (kg P/m^3, O(1e-3)); scale to order 1.
    if "process_constraint_" in name and "chemical_p_target" in name:
        return 1e3

    if "process_constraint_" in name and "do_target" in name:
        return 1e3

    if "process_constraint_" in name and "no3_target" in name:
        return DEFAULT_ASM2D_SCALING_FACTORS.get("S_NO3", 1e2)

    if "process_constraint_" in name and "alkalinity_target" in name:
        return 1.0

    return None


def _scale_unit_by_classes(
    unit: Any,
    iscale: Any,
    scaling_classes: Optional[Sequence[str]] = None,
) -> int:
    try:
        from pyomo.environ import Constraint, Var
    except ImportError:
        return 0

    if not hasattr(unit, "component_data_objects"):
        return 0

    classes = _scaling_classes(scaling_classes)
    applied = 0

    for var in unit.component_data_objects(Var, descend_into=True):
        factor = _var_factor(var, classes)
        if factor is not None and _set_scaling_factor(iscale, var, factor):
            applied += 1

    for constraint in unit.component_data_objects(
        Constraint,
        active=True,
        descend_into=True,
    ):
        factor = _constraint_factor(constraint, classes)
        if factor is not None and _transform_constraint(iscale, constraint, factor):
            applied += 1

    return applied


def _scale_model_by_classes(
    model: Any,
    iscale: Any,
    scaling_classes: Optional[Sequence[str]] = None,
) -> int:
    try:
        from pyomo.environ import Constraint, Var
        from pyomo.environ import value
    except ImportError:
        return 0

    if not hasattr(model, "component_data_objects"):
        return 0

    classes = _scaling_classes(scaling_classes)
    applied = 0

    for var in model.component_data_objects(Var, descend_into=True):
        var_name = str(getattr(var, "name", ""))
        factor = None
        if _enabled(classes, "recycle_ratios") and "recycle_ratio" in var_name:
            factor = 1.0
        elif "rxn_params" in var_name:
            try:
                magnitude = abs(value(var, exception=False) or 0.0)
            except Exception:
                magnitude = 0.0
            factor = 1.0 / magnitude if magnitude > 0 else 1.0

        if factor is not None and _set_scaling_factor(iscale, var, factor):
            applied += 1

    for constraint in model.component_data_objects(
        Constraint,
        active=True,
        descend_into=True,
    ):
        factor = _constraint_factor(constraint, classes)
        if factor is not None and _transform_constraint(iscale, constraint, factor):
            applied += 1

    return applied


def apply_asm2d_species_scaling(model: Any) -> int:
    """Apply industrial-strength ASM2D species concentration scaling.

    The factors target 1-100 g/m3 soluble/particulate concentrations and are
    intentionally more aggressive for trace oxygen, nitrogen gas, nitrate, and
    storage/polyphosphate species than WaterTAP's municipal example defaults.
    The function is safe to call on non-ASM2D models; it only touches variables
    whose parent component is named ``conc_mass_comp`` and whose species exists
    in ``DEFAULT_ASM2D_SCALING_FACTORS``.
    """

    try:
        import idaes.core.util.scaling as iscale
        from pyomo.environ import Var
    except ImportError:
        return 0

    if not hasattr(model, "component_data_objects"):
        return 0

    applied = 0
    for var in model.component_data_objects(Var, descend_into=True):
        parent = var.parent_component()
        parent_name = getattr(parent, "local_name", getattr(parent, "name", ""))
        if parent_name != "conc_mass_comp":
            continue
        component = _component_from_index(var.index())
        factor = DEFAULT_ASM2D_SCALING_FACTORS.get(component or "")
        if factor is None:
            continue
        if _set_scaling_factor(iscale, var, factor):
            applied += 1
    return applied


#: Components every digester row of which is scaled as their own liquid material-balance row: the control volume's
#: generation (``rate_reaction_stoichiometry_constraint``) and the unit's liquid-gas transfer (``unit_material_balance``,
#: zero for a solid). The S/Fe digester's minerals (process_model_schema.madm1_iron_minerals) grow on a 1e-6 kg/m3
#: nucleation seed, ~1e-9 kg/s at a digester's flow until they establish; with those rows at the recursive pass's 1,
#: IPOPT closed the balance on a generation 1e-9 kg/s from the law's and a nonzero transfer (ZLR ad-1, lane c77).
ADM1_ROWS_AS_BALANCE_COMPONENTS: tuple[str, ...] = ("X_Fe3PO42", "X_Siderite", "X_Mackinawite", "X_struv")
ADM1_ROWS_AS_BALANCE: tuple[str, ...] = ("rate_reaction_stoichiometry_constraint", "unit_material_balance")


def _apply_adm1_rows_as_balance_scaling(model: Any, iscale: Any) -> int:
    """`ADM1_ROWS_AS_BALANCE_COMPONENTS`: each such component's `ADM1_ROWS_AS_BALANCE` rows take the factor of its liquid
    material-balance row (``liquid_phase.material_balances[t, "Liq", j]``)."""
    from pyomo.environ import Constraint

    applied = 0
    for con in model.component_data_objects(Constraint, active=True, descend_into=True):
        if con.parent_component().local_name not in ADM1_ROWS_AS_BALANCE:
            continue
        component = _component_from_index(con.index())
        if component not in ADM1_ROWS_AS_BALANCE_COMPONENTS or not _constraint_in_adm1_block(con):
            continue
        block = con.parent_block()
        balances = getattr(getattr(block, "liquid_phase", block), "material_balances", None)
        key = (con.index()[0], "Liq", component)
        if balances is None or key not in balances:
            continue
        factor = iscale.get_constraint_transform_applied_scaling_factor(balances[key])
        if factor is None:
            continue
        iscale.constraint_scaling_transform(con, factor, overwrite=True)
        applied += 1
    return applied


def apply_adm1_species_scaling(model: Any) -> int:
    """Apply ADM1 / modified-ADM1 digester state and row scaling.

    Sets digester-appropriate magnitudes on every ADM1 state block in the model:
    per-species ``conc_mass_comp`` from ``DEFAULT_ADM1_SCALING_FACTORS`` (with a
    1e1 fallback matching the WaterTAP reference flat scaling) plus the structural
    ``flow_vol`` / ``temperature`` / ``pressure`` / strong-ion / H+ factors from
    ``DEFAULT_ADM1_STATE_SCALING``. Must be called AFTER the recursive IDAES
    scaling pass so it overrides the property/unit block defaults (e.g. the vapor
    block's ``flow_vol=1e5``) the same way the reference flowsheet's
    ``scale_variables`` override does — this is the conditioning that lets the
    stiff acid/base + gas-transfer digester solve. Safe no-op on non-ADM1 models;
    only touches parent components named ``conc_mass_comp`` or matching the
    structural-scaling keys.

    Scoped to ADM1 / MODIFIED_ADM1 state + reaction blocks by parent-block
    package ancestry: in a mixed ASM2D+ADM1 flowsheet the ADM1 factors (and the
    flat 1e1 conc fallback) must NOT land on the ASM2D state blocks, whose
    shared-name species (``S_I`` / ``X_PAO`` / ...) and structural
    ``flow_vol`` / ``temperature`` / ``pressure`` vars are conditioned by
    ``apply_asm2d_species_scaling`` / the topology scaler instead.
    """

    try:
        import idaes.core.util.scaling as iscale
        from pyomo.environ import Constraint, Var
    except ImportError:
        return 0

    if not hasattr(model, "component_data_objects"):
        return 0

    applied = 0
    for var in model.component_data_objects(Var, descend_into=True):
        if not _var_in_adm1_block(var):
            continue
        parent = var.parent_component()
        parent_name = getattr(parent, "local_name", getattr(parent, "name", ""))
        if parent_name == "conc_mass_comp":
            component = _component_from_index(var.index())
            factor = DEFAULT_ADM1_SCALING_FACTORS.get(component or "", 1e1)
        else:
            factor = DEFAULT_ADM1_STATE_SCALING.get(parent_name)
        if factor is None:
            continue
        if _set_scaling_factor(iscale, var, factor):
            applied += 1

    applied += _apply_adm1_extensive_rate_scaling(model, iscale)
    applied += _apply_adm1_rows_as_balance_scaling(model, iscale)

    # A5-PR1 energy swap: a heat-duty temperature target replaces the AD's native
    # adiabatic ``unit_enthalpy_balance`` with ``adm1_external_heat_vapor_transfer``
    # (``enthalpy_transfer + H_vap == 0``). The recursive IDAES pass only scales the
    # native (now-deactivated) balance, leaving the replacement row unscaled, so its
    # enthalpy-flow terms (~1e10 J/s) would wreck the Jacobian conditioning. Transform
    # it by the same enthalpy-magnitude factor the topology scaler uses for the IDAES
    # liquid ``enthalpy_balances`` (1e-10). Applied here -- the LAST scaling stage for
    # ADM1 models, after the recursive pass -- so it is never clobbered, and it covers
    # both pure-ADM1 and mixed ASM2D+ADM1 flowsheets (anything with ADM1 blocks).
    for con in model.component_data_objects(Constraint, active=True, descend_into=True):
        if "adm1_external_heat_vapor_transfer" not in str(getattr(con, "name", "")):
            continue
        try:
            iscale.constraint_scaling_transform(con, 1e-10, overwrite=True)
            applied += 1
        except Exception:
            continue
    return applied


def _adm1_reference_flow_ratio(model: Any) -> Optional[float]:
    """Return (canonical reference flow / actual ADM1 feed flow), clamped.

    The actual feed flow is read from the largest FIXED ``flow_vol`` among the
    ADM1 state blocks — the feed boundary specification, fixed by the builder
    before any solve, so it is reliable at scaling time (pre-initialization).
    ``None`` when no fixed ADM1 flow boundary exists (nothing to normalize by).
    """

    try:
        from pyomo.environ import Var, value
    except ImportError:
        return None
    if not hasattr(model, "component_data_objects"):
        return None

    feed_flow = None
    for var in model.component_data_objects(Var, descend_into=True):
        parent = var.parent_component()
        if getattr(parent, "local_name", "") != "flow_vol":
            continue
        if not getattr(var, "fixed", False):
            continue
        if not _var_in_adm1_block(var):
            continue
        try:
            raw = value(var, exception=False)
        except Exception:
            continue
        if raw is None or raw <= 0.0:
            continue
        if feed_flow is None or float(raw) > feed_flow:
            feed_flow = float(raw)
    if feed_flow is None:
        return None
    ratio = ADM1_REFERENCE_FEED_FLOW_M3_S / feed_flow
    return min(max(ratio, 1e-3), 1e3)


def _apply_adm1_extensive_rate_scaling(model: Any, iscale: Any) -> int:
    'Normalize the AD extensive-rate scaling to the proven canonical basis.'

    ratio = _adm1_reference_flow_ratio(model)
    if ratio is None or abs(ratio - 1.0) < 1e-9:
        return 0

    applied = 0
    try:
        from pyomo.environ import Var
    except ImportError:
        return 0
    seen_parents: set = set()
    for var in model.component_data_objects(Var, descend_into=True):
        parent = var.parent_component()
        if id(parent) in seen_parents:
            continue
        local_name = getattr(parent, "local_name", "")
        base = ADM1_EXTENSIVE_RATE_BASE_SCALING.get(local_name)
        if base is None:
            continue
        # These vars live on the digester's liquid-phase CONTROL VOLUME (not a
        # state block), so membership is the control volume's own configured
        # property package rather than a state block's ``params``.
        control_volume = var.parent_block()
        cv_config = getattr(control_volume, "config", None)
        if not _is_adm1_property_package(
            getattr(cv_config, "property_package", None)
        ):
            continue
        seen_parents.add(id(parent))
        if _set_scaling_factor(iscale, parent, base * ratio):
            applied += 1
    return applied


def apply_adm1_material_balance_constraint_scaling(
    model: Any,
    *,
    include_gas_transfer_scaling: bool = False,
) -> int:
    """Apply ADM1 recycle continuation row scaling."""

    try:
        import idaes.core.util.scaling as iscale
        from pyomo.environ import Constraint
    except ImportError:
        return 0

    if not hasattr(model, "component_data_objects"):
        return 0

    applied = 0
    for con in model.component_data_objects(Constraint, active=True, descend_into=True):
        factor = _adm1_recycle_constraint_factor(
            con,
            include_gas_transfer_scaling=include_gas_transfer_scaling,
        )
        if factor is None:
            continue
        try:
            iscale.constraint_scaling_transform(con, factor, overwrite=True)
            applied += 1
        except Exception:
            continue
    return applied


def apply_asm2d_recycle_pressure_changer_scaling(model: Any) -> int:
    """Apply post-recursive H2O row scaling to ASM2D recycle pressure changers."""

    try:
        import idaes.core.util.scaling as iscale
        from pyomo.environ import Constraint
    except ImportError:
        return 0

    if not hasattr(model, "component_data_objects"):
        return 0

    applied = 0
    for con in model.component_data_objects(Constraint, active=True, descend_into=True):
        factor = _asm2d_pressure_changer_h2o_constraint_factor(con)
        if factor is None:
            continue
        if _transform_constraint(iscale, con, factor):
            applied += 1
    return applied


def apply_gas_membrane_scaling(model: Any) -> int:
    """Apply the gas-membrane default scaling catalog (``GAS_MEMBRANE_SCALING_FACTORS``).

    Sets scaling factors on the gas state + membrane variables (flow_mol, pressure,
    temperature, mole_frac_comp, permeance, area, cell_area) keyed by the variable's
    parent component name, so the universal solver pipeline conditions a NATURAL_GAS_PR
    flowsheet correctly. Only called when the model uses a gas package; safe no-op
    otherwise (no matching parent names).
    """

    try:
        import idaes.core.util.scaling as iscale
        from pyomo.environ import Var
    except ImportError:
        return 0

    if not hasattr(model, "component_data_objects"):
        return 0

    applied = 0
    for var in model.component_data_objects(Var, descend_into=True):
        parent = var.parent_component()
        parent_name = getattr(parent, "local_name", getattr(parent, "name", ""))
        factor = GAS_MEMBRANE_SCALING_FACTORS.get(parent_name)
        if factor is None:
            continue
        if _set_scaling_factor(iscale, var, factor):
            applied += 1
    return applied


class ScalingIssueType(Enum):
    """Type of scaling issue."""
    UNSCALED_VAR = "unscaled_variable"
    UNSCALED_CONSTRAINT = "unscaled_constraint"
    BADLY_SCALED_VAR = "badly_scaled_variable"
    BADLY_SCALED_CONSTRAINT = "badly_scaled_constraint"


@dataclass
class ScalingIssue:
    """A scaling issue identified in the model."""
    issue_type: ScalingIssueType
    component_name: str
    current_magnitude: Optional[float] = None
    suggested_factor: Optional[float] = None
    message: str = ""


@dataclass
class ScalingReport:
    """Report of scaling analysis."""
    total_issues: int
    issues: List[ScalingIssue] = field(default_factory=list)
    unscaled_vars: int = 0
    unscaled_constraints: int = 0
    badly_scaled_vars: int = 0
    badly_scaled_constraints: int = 0
    message: str = ""


@dataclass
class ScalingApplication:
    """Details from one automatic scaling stage."""

    stage: str
    success: bool
    applied: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    message: str = ""


class ScalingTools:
    """Tools for managing model scaling.

    Wraps IDAES scaling utilities and provides WaterTAP-specific defaults.
    """

    def __init__(self, model: Any = None):
        """Initialize scaling tools.

        Args:
            model: Optional Pyomo model reference
        """
        self._model = model
        self._last_applications: List[ScalingApplication] = []

    def set_scaling_factor(
        self,
        var: Any,
        factor: float,
    ) -> bool:
        """Set scaling factor for a variable.

        Args:
            var: Pyomo variable or expression
            factor: Scaling factor (multiply var by this to get O(1))

        Returns:
            True if successful
        """
        try:
            import idaes.core.util.scaling as iscale
            iscale.set_scaling_factor(var, factor)
            return True
        except ImportError:
            return self._manual_set_scaling(var, factor)
        except Exception:
            return False

    def _manual_set_scaling(self, var: Any, factor: float) -> bool:
        """Set scaling using Pyomo suffix directly."""
        try:
            # Find or create scaling suffix
            model = var.model()
            if not hasattr(model, 'scaling_factor'):
                from pyomo.environ import Suffix
                model.scaling_factor = Suffix(direction=Suffix.EXPORT)

            model.scaling_factor[var] = factor
            return True
        except Exception:
            return False

    def get_scaling_factor(self, var: Any) -> Optional[float]:
        """Get current scaling factor for a variable.

        Args:
            var: Pyomo variable

        Returns:
            Current scaling factor or None if unscaled
        """
        try:
            import idaes.core.util.scaling as iscale
            return iscale.get_scaling_factor(var)
        except Exception:
            return None

    def _default_scaler_application(
        self,
        unit_id: str,
        unit: Any,
        scaling_classes: Optional[Sequence[str]] = None,
    ) -> ScalingApplication:
        """Apply one unit's upstream and MCP topology-aware default scaling."""

        try:
            import idaes.core.util.scaling as iscale
        except ImportError:
            iscale = None

        scaler_cls = getattr(unit, "default_scaler", None)
        upstream_error = None
        use_mcp_topology_fallback = _unit_uses_asm2d(unit)
        if scaler_cls is None:
            local_applied = (
                _scale_unit_by_classes(unit, iscale, scaling_classes)
                if iscale is not None and use_mcp_topology_fallback
                else 0
            )
            return ScalingApplication(
                stage="default_scaler",
                success=True,
                applied=[unit_id] if local_applied else [],
                message=f"{unit_id} has no default_scaler",
            )

        try:
            scaler = scaler_cls() if callable(scaler_cls) else scaler_cls
            scaler.scale_model(unit)
        except Exception as exc:
            upstream_error = f"{unit_id}: {type(exc).__name__}: {exc}"

        local_applied = (
            _scale_unit_by_classes(unit, iscale, scaling_classes)
            if iscale is not None and use_mcp_topology_fallback
            else 0
        )
        if upstream_error and not local_applied:
            return ScalingApplication(
                stage="default_scaler",
                success=False,
                errors=[upstream_error],
                message=f"Default scaler failed for {unit_id}",
            )

        return ScalingApplication(
            stage="default_scaler",
            success=True,
            applied=[unit_id],
            message=(
                f"Applied MCP fallback scaling for {unit_id} after upstream failure: "
                f"{upstream_error}"
                if upstream_error
                else f"Applied default scaler for {unit_id}"
            ),
        )

    def apply_default_scalers(
        self,
        units: Dict[str, Any],
        scaling_classes: Optional[Sequence[str]] = None,
    ) -> ScalingApplication:
        """Apply upstream default scaler classes for built unit blocks."""

        applied: List[str] = []
        errors: List[str] = []
        for unit_id, unit in units.items():
            result = self._default_scaler_application(
                unit_id,
                unit,
                scaling_classes=scaling_classes,
            )
            applied.extend(result.applied)
            errors.extend(result.errors)

        return ScalingApplication(
            stage="default_scalers",
            success=not errors,
            applied=applied,
            errors=errors,
            message=(
                f"Applied {len(applied)} upstream default scaler(s)"
                if not errors
                else f"Applied {len(applied)} upstream default scaler(s); {len(errors)} failed"
            ),
        )

    def apply_model_default_scaling(
        self,
        model: Any,
        scaling_classes: Optional[Sequence[str]] = None,
        *,
        asm2d_enabled: bool = True,
    ) -> ScalingApplication:
        """Apply topology-level scaling not owned by a unit block."""

        if not asm2d_enabled:
            return ScalingApplication(
                stage="model_default_scaling",
                success=True,
                message="Skipped ASM2D model-level default scaling for non-ASM2D model",
            )

        try:
            import idaes.core.util.scaling as iscale
        except ImportError as exc:
            return ScalingApplication(
                stage="model_default_scaling",
                success=False,
                errors=[f"IDAES scaling utilities unavailable: {exc}"],
                message="Model-level default scaling unavailable",
            )

        try:
            applied_count = _scale_model_by_classes(
                model,
                iscale,
                scaling_classes=scaling_classes,
            )
        except Exception as exc:
            return ScalingApplication(
                stage="model_default_scaling",
                success=False,
                errors=[f"{type(exc).__name__}: {exc}"],
                message="Model-level default scaling failed",
            )

        return ScalingApplication(
            stage="model_default_scaling",
            success=True,
            applied=[f"{applied_count} model component(s)"],
            message=f"Applied model-level default scaling to {applied_count} component(s)",
        )

    def apply_asm2d_species_scaling(self, model: Any) -> ScalingApplication:
        """Apply the shared industrial ASM2D species scaling catalog."""

        try:
            applied_count = apply_asm2d_species_scaling(model)
        except Exception as exc:
            return ScalingApplication(
                stage="asm2d_species_scaling",
                success=False,
                errors=[f"{type(exc).__name__}: {exc}"],
                message="ASM2D species scaling failed",
            )

        return ScalingApplication(
            stage="asm2d_species_scaling",
            success=True,
            applied=[f"{applied_count} concentration variable(s)"],
            message=(
                f"Applied ASM2D species scaling to {applied_count} concentration variable(s)"
            ),
        )

    def apply_mvc_thermal_scaling(
        self,
        model: Any,
        units: Optional[Dict[str, Any]] = None,
    ) -> ScalingApplication:
        """Apply the MVC/ZLD thermal-train scaling catalog."""

        try:
            applied_count = apply_mvc_thermal_scaling(model, units)
        except Exception as exc:
            return ScalingApplication(
                stage="mvc_thermal_scaling",
                success=False,
                errors=[f"{type(exc).__name__}: {exc}"],
                message="MVC thermal scaling failed",
            )

        return ScalingApplication(
            stage="mvc_thermal_scaling",
            success=True,
            applied=[f"{applied_count} MVC thermal component(s)"],
            message=(
                f"Applied MVC thermal scaling to {applied_count} component(s)"
            ),
        )

    def apply_adm1_species_scaling(self, model: Any) -> ScalingApplication:
        """Apply the ADM1 / modified-ADM1 digester state scaling catalog."""

        try:
            applied_count = apply_adm1_species_scaling(model)
        except Exception as exc:
            return ScalingApplication(
                stage="adm1_species_scaling",
                success=False,
                errors=[f"{type(exc).__name__}: {exc}"],
                message="ADM1 species scaling failed",
            )

        return ScalingApplication(
            stage="adm1_species_scaling",
            success=True,
            applied=[f"{applied_count} digester state variable(s)"],
            message=(
                f"Applied ADM1 digester scaling to {applied_count} state variable(s)"
            ),
        )

    def apply_adm1_material_balance_constraint_scaling(
        self,
        model: Any,
        *,
        include_gas_transfer_scaling: bool = False,
    ) -> ScalingApplication:
        """Apply ADM1 recycle row scaling for recycle continuation."""

        try:
            applied_count = apply_adm1_material_balance_constraint_scaling(
                model,
                include_gas_transfer_scaling=include_gas_transfer_scaling,
            )
        except Exception as exc:
            return ScalingApplication(
                stage="adm1_material_balance_constraint_scaling",
                success=False,
                errors=[f"{type(exc).__name__}: {exc}"],
                message="ADM1 recycle constraint scaling failed",
            )

        return ScalingApplication(
            stage="adm1_material_balance_constraint_scaling",
            success=True,
            applied=[f"{applied_count} recycle row(s)"],
            message=(
                "Applied ADM1 recycle constraint scaling to "
                f"{applied_count} row(s)"
                + (
                    " including retained-solids gas-transfer rows"
                    if include_gas_transfer_scaling
                    else ""
                )
            ),
        )

    def apply_asm2d_recycle_pressure_changer_scaling(
        self,
        model: Any,
    ) -> ScalingApplication:
        """Apply ASM2D recycle pressure-changer material-balance row scaling."""

        try:
            applied_count = apply_asm2d_recycle_pressure_changer_scaling(model)
        except Exception as exc:
            return ScalingApplication(
                stage="asm2d_recycle_pressure_changer_scaling",
                success=False,
                errors=[f"{type(exc).__name__}: {exc}"],
                message="ASM2D recycle pressure-changer scaling failed",
            )

        return ScalingApplication(
            stage="asm2d_recycle_pressure_changer_scaling",
            success=True,
            applied=[f"{applied_count} pressure-changer row(s)"],
            message=(
                "Applied ASM2D pressure-changer scaling to "
                f"{applied_count} row(s)"
            ),
        )

    def apply_gas_membrane_scaling(self, model: Any) -> ScalingApplication:
        """Apply the gas-membrane default scaling catalog."""

        try:
            applied_count = apply_gas_membrane_scaling(model)
        except Exception as exc:
            return ScalingApplication(
                stage="gas_membrane_scaling",
                success=False,
                errors=[f"{type(exc).__name__}: {exc}"],
                message="Gas membrane scaling failed",
            )

        return ScalingApplication(
            stage="gas_membrane_scaling",
            success=True,
            applied=[f"{applied_count} gas variable(s)"],
            message=f"Applied gas membrane scaling to {applied_count} variable(s)",
        )

    def run_interval_initializer(self, model: Any) -> ScalingApplication:
        """Run WaterTAP's FBBT-backed interval initializer as a pre-init conditioner."""

        try:
            from watertap.core.util.initialization import interval_initializer
        except ImportError as exc:
            return ScalingApplication(
                stage="interval_initializer",
                success=False,
                errors=[f"WaterTAP interval_initializer unavailable: {exc}"],
                message="WaterTAP interval initializer unavailable",
            )

        try:
            interval_initializer(model, fail_flag=True)
        except Exception as exc:
            return ScalingApplication(
                stage="interval_initializer",
                success=False,
                errors=[f"{type(exc).__name__}: {exc}"],
                message="WaterTAP interval initializer failed",
            )

        return ScalingApplication(
            stage="interval_initializer",
            success=True,
            applied=[str(getattr(model, "name", "model"))],
            message="WaterTAP interval initializer completed",
        )

    def calculate_scaling_factors(
        self,
        model: Any,
        units: Optional[Dict[str, Any]] = None,
        scaling_classes: Optional[Sequence[str]] = None,
    ) -> bool:
        """Calculate scaling factors for model.

        Applies upstream default scaler classes for unit blocks first, then
        runs the legacy IDAES recursive scaling pass as a broad final fill-in.

        Args:
            model: Pyomo model (typically m or m.fs)
            units: Optional unit_id -> unit_block mapping for explicit scaler dispatch

        Returns:
            True if successful
        """
        self._last_applications = []

        asm2d_enabled = _model_uses_asm2d(model, units)
        if asm2d_enabled:
            asm2d_scaling_result = self.apply_asm2d_species_scaling(model)
            self._last_applications.append(asm2d_scaling_result)

        adm1_enabled = _model_uses_adm1(model, units)

        if _model_uses_natural_gas_pr(model, units):
            self._last_applications.append(self.apply_gas_membrane_scaling(model))

        if _model_uses_mvc_thermal(model, units):
            # Must precede the recursive IDAES pass: the MVC compressor's own
            # calculate_scaling_factors raises on a missing work factor, which
            # aborts recursive scaling for the whole ZLD model. Re-verified
            # still required on watertap 1.7 (Wave 5c deletion trial,
            # 2026-08-25): without this stage the recursive pass raises
            # "TypeError: NoneType / int" on the compressor and the scaling
            # stage fails loud — upstream #1767 did not absorb it.
            self._last_applications.append(self.apply_mvc_thermal_scaling(model, units))

        if units:
            default_scaler_result = self.apply_default_scalers(
                units,
                scaling_classes=scaling_classes,
            )
            self._last_applications.append(default_scaler_result)

        model_default_result = self.apply_model_default_scaling(
            model,
            scaling_classes=scaling_classes,
            asm2d_enabled=asm2d_enabled,
        )
        self._last_applications.append(model_default_result)

        try:
            import idaes.core.util.scaling as iscale
            iscale.calculate_scaling_factors(model)
            self._last_applications.append(
                ScalingApplication(
                    stage="idaes_recursive_scaling",
                    success=True,
                    applied=[str(getattr(model, "name", "model"))],
                    message="IDAES recursive scaling completed",
                )
            )
            # ADM1 digester state scaling is applied LAST so it overrides the
            # property/unit block defaults baked by the recursive pass (e.g. the
            # vapor block's flow_vol=1e5), exactly as the WaterTAP reference
            # modified-ADM1 flowsheet's ``scale_variables`` override does. This is
            # the conditioning that lets the stiff acid/base + gas-transfer
            # digester solve to optimal.
            if adm1_enabled:
                self._last_applications.append(self.apply_adm1_species_scaling(model))
            return True
        except ImportError as exc:
            self._last_applications.append(
                ScalingApplication(
                    stage="idaes_recursive_scaling",
                    success=False,
                    errors=[f"IDAES scaling utilities unavailable: {exc}"],
                    message="IDAES recursive scaling unavailable",
                )
            )
            return False
        except Exception as exc:
            # A raise here means the recursive pass did NOT complete: part of
            # the model is left unscaled. Failing the SCALING stage loudly is
            # the contract — the old ``return True`` swallow let a ZLD model
            # with 476 unscaled variables proceed to solve for months.
            self._last_applications.append(
                ScalingApplication(
                    stage="idaes_recursive_scaling",
                    success=False,
                    errors=[f"{type(exc).__name__}: {exc}"],
                    message=(
                        "IDAES recursive scaling raised; model is partially "
                        "unscaled — failing the scaling stage"
                    ),
                )
            )
            return False

    def get_last_scaling_applications(self) -> List[Dict[str, Any]]:
        """Return structured details from the last automatic scaling run."""

        return [asdict(item) for item in self._last_applications]

    def report_scaling_issues(
        self,
        model: Any,
        threshold: float = 1e-4,
    ) -> ScalingReport:
        """Report scaling issues in the model.

        Args:
            model: Pyomo model
            threshold: Threshold for badly scaled (< threshold or > 1/threshold)

        Returns:
            ScalingReport with identified issues
        """
        try:
            import idaes.core.util.scaling as iscale

            issues = []
            unscaled_vars = 0
            unscaled_cons = 0
            badly_scaled_vars = 0
            badly_scaled_cons = 0

            # Check variables
            from pyomo.environ import Var
            for v in model.component_data_objects(Var, active=True, descend_into=True):
                sf = iscale.get_scaling_factor(v)
                if sf is None:
                    unscaled_vars += 1
                    if v.value is not None and abs(v.value) > 0:
                        suggested = 1.0 / abs(v.value)
                        issues.append(ScalingIssue(
                            issue_type=ScalingIssueType.UNSCALED_VAR,
                            component_name=str(v),
                            current_magnitude=v.value,
                            suggested_factor=suggested,
                            message=f"Unscaled variable with value {v.value:.2e}",
                        ))
                else:
                    # Check if badly scaled
                    if v.value is not None:
                        scaled_value = abs(v.value * sf)
                        if scaled_value < threshold or scaled_value > 1/threshold:
                            badly_scaled_vars += 1
                            issues.append(ScalingIssue(
                                issue_type=ScalingIssueType.BADLY_SCALED_VAR,
                                component_name=str(v),
                                current_magnitude=v.value,
                                suggested_factor=1.0/abs(v.value) if v.value != 0 else 1.0,
                                message=f"Scaled value {scaled_value:.2e} outside [{threshold:.0e}, {1/threshold:.0e}]",
                            ))

            # Check constraints (simplified - full check requires Jacobian)
            from pyomo.environ import Constraint
            for c in model.component_data_objects(Constraint, active=True, descend_into=True):
                sf = iscale.get_scaling_factor(c)
                if sf is None:
                    unscaled_cons += 1

            total = len(issues)

            return ScalingReport(
                total_issues=total,
                issues=issues[:50],  # Limit to first 50
                unscaled_vars=unscaled_vars,
                unscaled_constraints=unscaled_cons,
                badly_scaled_vars=badly_scaled_vars,
                badly_scaled_constraints=badly_scaled_cons,
                message=f"Found {unscaled_vars} unscaled vars, {unscaled_cons} unscaled constraints",
            )

        except ImportError:
            return ScalingReport(
                total_issues=-1,
                message="IDAES scaling utilities not available",
            )
        except Exception as e:
            return ScalingReport(
                total_issues=-1,
                message=f"Error analyzing scaling: {e}",
            )

    def get_scaling_report(self, model: Any) -> ScalingReport:
        """Alias for report_scaling_issues for pipeline compatibility.

        Args:
            model: Pyomo model

        Returns:
            ScalingReport
        """
        return self.report_scaling_issues(model)

    def autoscale_large_jac(self, model: Any) -> bool:
        """Auto-scale based on Jacobian analysis.

        Uses IDAES constraint_autoscale_large_jac to identify
        and fix scaling issues based on Jacobian structure.

        Args:
            model: Pyomo model

        Returns:
            True if successful
        """
        try:
            import idaes.core.util.scaling as iscale
            iscale.constraint_autoscale_large_jac(model)
            return True
        except Exception:
            return False

    def apply_default_scaling(
        self,
        model: Any,
        scaling_dict: Dict[str, float],
    ) -> int:
        """Apply default scaling factors from a dictionary.

        Args:
            model: Pyomo model
            scaling_dict: Dict mapping variable patterns to scaling factors

        Returns:
            Number of variables scaled
        """
        count = 0
        try:
            import idaes.core.util.scaling as iscale
            from pyomo.environ import Var

            for v in model.component_data_objects(Var, active=True, descend_into=True):
                v_name = str(v)
                for pattern, factor in scaling_dict.items():
                    if pattern in v_name:
                        iscale.set_scaling_factor(v, factor)
                        count += 1
                        break
        except Exception:
            pass

        return count


# Common scaling factors for WaterTAP variables
DEFAULT_SCALING_FACTORS = {
    # Membrane parameters
    "A_comp": 1e12,
    "B_comp": 1e8,

    # Pressure
    "pressure": 1e-5,

    # Temperature
    "temperature": 1e-2,

    # Membrane area
    "area": 1e-2,

    # Flux
    "flux_mass": 1e3,

    # Energy
    "work_mechanical": 1e-5,
    "heat_transfer": 1e-6,

    # Crystallizer
    "crystal_growth_rate": 1e9,
    "crystal_median_length": 1e3,

    # NF DSPMDE
    "radius_pore": 1e9,
    "membrane_thickness": 1e6,
}
