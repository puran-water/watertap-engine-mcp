"""ASM2d biological steady-state proposal templates.

A registered ASM2D "template" is a **named preset**: a binding of the ASM2D
biological *family provider* (:class:`Asm2dPresetProvider`) to a specific config
(volumes, recycle targets, SRT) and a structural *family profile*
(:class:`Asm2dFamilyProfile` — design bounds + which structural roles the
topology carries). The provider, not the template, is the authority for the
solver defaults: DOF fixes, scaling, feed states, process constraints,
recycle-ratio declarations, initialization order, solver options, near-feasible
policy, and expected ranges/metadata.

The provider resolves every default against the graph's **roles** (via the A1
:class:`core.roles.RoleResolver` multiplicity API) rather than hardcoded unit
names, so the SAME provider defaults a template-built topology and a from-graph
(role-tagged BFD / ``simulation_seed``) build identically. ``to_session_spec()``
delegates the default bundle to the provider and composes it with the template's
topology (units + connections); the from-graph path runs the same provider over
a :class:`core.default_provider.RoleResolvedTopology` built from a live session.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from watertap_engine_mcp.core.default_provider import DefaultBundle, DefaultProvider, RoleResolvedTopology
from watertap_engine_mcp.core.property_registry import PropertyPackageType, is_asm2d_property_package_type
from watertap_engine_mcp.core.roles import UnitRole
from puran_mcp_support.engineering.heuristics.flowsheet_templates.registry import CONVENTIONAL_AEROBIC_BOUNDS, IFAS_AEROBIC_BOUNDS, MBR_AEROBIC_BOUNDS
from watertap_engine_mcp.ontology.asm2d import WATERTAP_ASM2D_COMPONENTS, WATERTAP_ASM2D_PARTICULATE_COMPONENTS
ASM2D_COMPONENTS = list(WATERTAP_ASM2D_COMPONENTS)
ASM2D_PARTICULATE_COMPONENTS = list(WATERTAP_ASM2D_PARTICULATE_COMPONENTS)
MODIFIED_ASM2D_COMPONENTS = ('S_A', 'S_F', 'S_I', 'S_N2', 'S_NH4', 'S_NO3', 'S_O2', 'S_PO4', 'S_K', 'S_Mg', 'S_IC', 'X_AUT', 'X_H', 'X_I', 'X_PAO', 'X_PHA', 'X_PP', 'X_S')
MODIFIED_ASM2D_PARTICULATE_COMPONENTS = ('X_AUT', 'X_H', 'X_I', 'X_PAO', 'X_PHA', 'X_PP', 'X_S')
ASM2D_DEFAULT_SRT_COMPONENTS = ['X_AUT', 'X_H', 'X_PAO']
ASM2D_COD_COMPONENTS = ('S_F', 'S_A', 'S_I', 'X_S', 'X_I', 'X_H')
ASM2D_F_CV_AUT = 1.42
ASM2D_UNIVERSAL_RECYCLE_RATIO_BOUNDS = (0.0, 10.0)
ASM2D_CONVENTIONAL_RAS_RECYCLE_RATIO_BOUNDS = CONVENTIONAL_AEROBIC_BOUNDS.ras_recycle_ratio
ASM2D_IFAS_RAS_RECYCLE_RATIO_BOUNDS = IFAS_AEROBIC_BOUNDS.ras_recycle_ratio
ASM2D_MBR_RAS_RECYCLE_RATIO_BOUNDS = MBR_AEROBIC_BOUNDS.ras_recycle_ratio
ASM2D_AEROBIC_MLR_RECYCLE_RATIO_BOUNDS = MBR_AEROBIC_BOUNDS.mlr_recycle_ratio
RATING_MODE_MLSS_FLOOR_MG_L = 500.0
RATING_MODE_MLSS_CEILING_MG_L = 25000.0
DESIGN_MODE_MBR_MLSS_BAND_MG_L = (8000.0, 12000.0)
DESIGN_MODE_MUNICIPAL_MBR_MLSS_BAND_MG_L = (2500.0, 25000.0)
DEFAULT_ASM2D_FEED_CONC_KG_M3 = {'S_O2': 1e-09, 'S_F': 0.03, 'S_A': 0.02, 'S_NH4': 0.016, 'S_NO3': 1e-09, 'S_PO4': 0.0036, 'S_I': 0.03, 'S_N2': 1e-09, 'X_I': 0.025, 'X_S': 0.125, 'X_H': 0.03, 'X_PAO': 1e-09, 'X_PP': 1e-09, 'X_PHA': 1e-09, 'X_AUT': 1e-09, 'X_MeOH': 1e-09, 'X_MeP': 1e-09, 'X_TSS': 0.18}

@dataclass
class ASM2DBiologicalConfig:
    """Common configuration for ASM2d MLE biological templates."""
    property_package: PropertyPackageType = PropertyPackageType.ASM2D
    feed_flow_m3_per_day: float = 350.0
    feed_temperature_K: float = 298.15
    feed_pressure_Pa: float = 101325.0
    alkalinity_mol_per_m3: float = 7.0
    feed_strength_class: Optional[str] = None
    feed_conc_kg_m3: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_ASM2D_FEED_CONC_KG_M3))
    anoxic_volume_m3: float = 300.0
    aerobic_volume_m3: float = 900.0
    post_anoxic_volume_m3: float = 150.0
    target_mlss_mg_l: Optional[float] = None
    reactor_volumes_fixed: bool = True
    aerobic_kla_per_hr: float = 10.0
    target_do_mg_l: Optional[float] = None
    internal_recycle_fraction: Optional[float] = None
    ras_recycle_ratio: Optional[float] = None
    mlr_recycle_ratio: Optional[float] = None
    ras_recycle_ratio_bounds: Tuple[float, float] = ASM2D_UNIVERSAL_RECYCLE_RATIO_BOUNDS
    mlr_recycle_ratio_bounds: Tuple[float, float] = ASM2D_UNIVERSAL_RECYCLE_RATIO_BOUNDS
    default_waste_fraction: float = 0.02
    target_srt_days: Optional[float] = 20.0
    srt_components: List[str] = field(default_factory=lambda: list(ASM2D_DEFAULT_SRT_COMPONENTS))
    target_effluent_tp_mg_l: Optional[float] = None
    target_residual_alkalinity_mol_per_m3: Optional[float] = None
    target_residual_alkalinity_mg_caco3_l: Optional[float] = None
    property_parameter_fixes: Dict[str, float] = field(default_factory=dict)
    reaction_parameter_fixes: Dict[str, float] = field(default_factory=dict)
    clarifier_soluble_effluent_fraction: float = 0.49986
    clarifier_particulate_effluent_fraction: float = 0.022
    membrane_soluble_permeate_fraction: float = 0.99
    combined_mbr_recycle: bool = False
    membrane_particulate_permeate_fraction: float = 0.001
    membrane_default_crossflow_ratio: float = 5.0

    def feed_cod_kg_m3(self) -> float:
        """Return the feed COD represented by ASM2D COD species."""
        total = sum((float(self.feed_conc_kg_m3.get(component, 0.0)) for component in ASM2D_COD_COMPONENTS))
        total += ASM2D_F_CV_AUT * float(self.feed_conc_kg_m3.get('X_AUT', 0.0))
        return total

    def detected_feed_strength_class(self) -> str:
        """Classify feed strength from COD concentration.

        The ASM2D recycle initializer is sensitive to the first nonlinear
        barrier path because the tear stream mixes feed, RAS, and internal
        recycle before SRT closure is restored. Municipal feeds below about
        0.8 kg COD/m3 use tight IPOPT defaults. Mid-strength feeds loosen only
        the initialization residual target. Industrial feeds above about
        1.5 kg COD/m3 use a smaller barrier ``mu_init`` and a modestly looser
        proposal residual tolerance so the SD tear state can enter the final
        square solve without falsely reporting infeasibility from scaled
        industrial COD terms.
        """
        if self.feed_strength_class:
            return self.feed_strength_class
        cod = self.feed_cod_kg_m3()
        if cod >= 1.5:
            return 'high_strength_industrial'
        if cod >= 0.8:
            return 'mid_strength'
        return 'municipal'

    def strength_solver_options(self) -> Dict[str, Any]:
        """Return IPOPT options selected for the detected feed-strength class."""
        strength = self.detected_feed_strength_class()
        if strength == 'high_strength_industrial':
            return {'linear_solver': 'ma27', 'mu_init': 1e-05, 'tol': 1e-07, 'acceptable_tol': 1e-05}
        if strength == 'mid_strength':
            return {'mu_init': 3e-05, 'tol': 1e-08, 'acceptable_tol': 3e-06}
        return {'mu_init': 0.0001, 'tol': 1e-08, 'acceptable_tol': 1e-06}

    def initialization_solver_options(self) -> Dict[str, Any]:
        """Return options passed to unit initializers during SD startup."""
        strength = self.detected_feed_strength_class()
        if strength == 'high_strength_industrial':
            return {'bound_push': 1e-08, 'mu_init': 1e-05}
        if strength == 'mid_strength':
            return {'bound_push': 1e-08, 'mu_init': 3e-05}
        return {'bound_push': 1e-08, 'mu_init': 0.0001}

    def residual_tolerance(self) -> float:
        """Return near-feasible residual tolerance for the final proposal solve.

        Values reflect defensible steady-state convergence: a 0.1-0.3% scaled
        constraint residual is the engineering acceptance point. Looser
        tolerances (>1%) accept solver iterates that did not converge and
        produce non-physical settled values; tighter tolerances (<1e-4) chase
        roundoff at no engineering benefit.
        """
        strength = self.detected_feed_strength_class()
        if strength == 'high_strength_industrial':
            return 0.003
        if strength == 'mid_strength':
            return 0.0015
        return 0.001

    def sd_almost_equal_tolerance(self) -> float:
        """Return fixed-state propagation tolerance for ASM2D SD startup."""
        strength = self.detected_feed_strength_class()
        if strength == 'high_strength_industrial':
            return 0.005
        if strength == 'mid_strength':
            return 0.001
        return 0.0001

    def effective_membrane_soluble_permeate_fraction(self) -> float:
        """Return the MBR hydraulic split used by the algebraic separator.

        The steady-state ASM2D template represents the MBR as an algebraic
        separator between permeate and the solids return stream. For proposal
        seeds that do not provide a membrane hydraulic split, choose the split
        from the SRT and nominal RAS balance so the default model can close WAS
        and RAS simultaneously. Explicit seed values below 95% are preserved.
        """
        configured = max(0.0001, min(0.9999, float(self.membrane_soluble_permeate_fraction)))
        if configured < 0.95:
            return configured
        srt_days = float(self.target_srt_days or 0.0)
        feed_flow = float(self.feed_flow_m3_per_day or 0.0)
        total_volume = float(self.anoxic_volume_m3 or 0.0) + float(self.aerobic_volume_m3 or 0.0)
        if self.post_anoxic_volume_m3:
            total_volume += float(self.post_anoxic_volume_m3)
        if srt_days > 0 and feed_flow > 0 and (total_volume > 0):
            ras_lower, ras_upper = self.ras_recycle_ratio_bounds
            if self.ras_recycle_ratio is not None:
                nominal_ras = float(self.ras_recycle_ratio)
            else:
                nominal_ras = (float(ras_lower) + float(ras_upper)) / 2.0
            nominal_ras = max(0.0, nominal_ras)
            solids_inventory_days = total_volume / feed_flow
            srt_alpha = min(max(solids_inventory_days / srt_days, 0.0001), 0.95)
            denominator = 1.0 + nominal_ras - srt_alpha
            if denominator > 1e-08:
                srt_balanced_split = (1.0 - srt_alpha) / denominator
                return max(0.0001, min(configured, srt_balanced_split, 0.9))
        crossflow_ratio = max(float(self.membrane_default_crossflow_ratio or 1.0), 1.0)
        crossflow_split = 1.0 / crossflow_ratio
        return max(0.0001, min(configured, crossflow_split))

    def _bounded_ratio_payload(self, *, ratio_name: str, target_ratio: Optional[float], bounds: Tuple[float, float], initial_value: float) -> Dict[str, Any]:
        lower, upper = bounds
        lower = float(lower)
        upper = float(upper)
        target = None if target_ratio is None else float(target_ratio)
        if target is not None:
            lower = min(lower, target)
            upper = max(upper, target)
            initial_value = target
        return {'type': 'recycle_ratio', 'name': ratio_name, 'ratio_name': ratio_name, 'target_ratio': target, 'lower_bound': lower, 'upper_bound': upper, 'initial_value': max(lower, min(upper, float(initial_value)))}

    def recycle_ratio_process_constraints(self, units: 'ASM2DTemplateUnits', *, include_mlr: bool) -> List[Dict[str, Any]]:
        """Return bounded hydraulic recycle-ratio controls.

        ``units`` only needs its ``ras_pressure`` / ``feed`` / ``internal_pressure``
        fields populated with the resolved unit ids — the provider passes a shim
        carrying the role-resolved ids so this stays name-agnostic.
        """
        ras_lower, ras_upper = self.ras_recycle_ratio_bounds
        mlr_lower, mlr_upper = self.mlr_recycle_ratio_bounds
        constraints = [{**self._bounded_ratio_payload(ratio_name='asm2d_ras_recycle_ratio', target_ratio=self.ras_recycle_ratio, bounds=self.ras_recycle_ratio_bounds, initial_value=(float(ras_lower) + float(ras_upper)) / 2.0), 'numerator_unit': units.ras_pressure, 'numerator_port': 'outlet', 'denominator_unit': units.feed, 'denominator_port': 'outlet'}]
        if include_mlr:
            constraints.append({**self._bounded_ratio_payload(ratio_name='asm2d_mlr_recycle_ratio', target_ratio=self.mlr_recycle_ratio, bounds=self.mlr_recycle_ratio_bounds, initial_value=(float(mlr_lower) + float(mlr_upper)) / 2.0), 'numerator_unit': units.internal_pressure, 'numerator_port': 'outlet', 'denominator_unit': units.feed, 'denominator_port': 'outlet'})
        return constraints

@dataclass
class ASM2DTemplateUnits:
    """Unit IDs used by ASM2d biological templates.

    Field names map 1:1 to structural roles (see ``FIELD_ROLES``). Defaults are
    the canonical template names; callers may override the ids to emit the same
    role-tagged priors for a from-scratch train with project-specific names.
    """
    feed: str = 'FeedWater'
    mixer: str = 'MX1'
    anoxic: str = 'Anoxic'
    aerobic: str = 'Aerobic'
    post_anoxic: str = 'PostAnoxic'
    internal_splitter: str = 'IRSplitter'
    internal_pressure: str = 'IRPressure'
    clarifier: str = 'Clarifier'
    mbr: str = 'MBR'
    alkalinity: str = 'AlkalinityDose'
    chemical_p: str = 'ChemicalP'
    sludge_splitter: str = 'SludgeSplitter'
    ras_pressure: str = 'RASPressure'
    effluent: str = 'TreatedEffluent'
    waste: str = 'WAS'

    def role_for(self, unit_id: str) -> Optional[str]:
        """Return the structural role string for a unit id in this template."""
        for field_name, role in FIELD_ROLES.items():
            if getattr(self, field_name) == unit_id:
                return role.value
        return None
FIELD_ROLES = {'feed': UnitRole.FEED, 'mixer': UnitRole.MIXER, 'anoxic': UnitRole.PRE_ANOXIC, 'aerobic': UnitRole.AEROBIC, 'post_anoxic': UnitRole.POST_ANOXIC, 'internal_splitter': UnitRole.INTERNAL_RECYCLE_SPLITTER, 'internal_pressure': UnitRole.INTERNAL_RECYCLE_PRESSURE, 'clarifier': UnitRole.SECONDARY_CLARIFIER, 'mbr': UnitRole.MBR_SEPARATOR, 'alkalinity': UnitRole.ALKALINITY, 'chemical_p': UnitRole.CHEMICAL_P, 'sludge_splitter': UnitRole.SLUDGE_SPLITTER, 'ras_pressure': UnitRole.RAS_PRESSURE, 'effluent': UnitRole.PRODUCT_EFFLUENT, 'waste': UnitRole.PRODUCT_WASTE}

def _uses_modified_asm2d(config: ASM2DBiologicalConfig) -> bool:
    """Return True for the ModifiedASM2d property/reaction package."""
    return config.property_package == PropertyPackageType.MODIFIED_ASM2D

def _has_alkalinity_target(config: ASM2DBiologicalConfig) -> bool:
    return config.target_residual_alkalinity_mol_per_m3 is not None or config.target_residual_alkalinity_mg_caco3_l is not None

def _has_chemical_p_target(config: ASM2DBiologicalConfig) -> bool:
    return config.target_effluent_tp_mg_l is not None

def _feed_conc_for_property_package(config: ASM2DBiologicalConfig) -> Dict[str, float]:
    """Return feed concentration keys valid for the selected ASM2d package."""
    feed = dict(config.feed_conc_kg_m3)
    if not _uses_modified_asm2d(config):
        return feed
    adapted = {component: float(feed.get(component, 1e-09) or 1e-09) for component in MODIFIED_ASM2D_COMPONENTS}
    if adapted['S_IC'] <= 1e-08:
        adapted['S_IC'] = max(float(config.alkalinity_mol_per_m3) * 0.012, 1e-09)
    adapted['S_K'] = max(adapted['S_K'], 0.015)
    adapted['S_Mg'] = max(adapted['S_Mg'], 0.015)
    return adapted

def _separator_component_ids(config: ASM2DBiologicalConfig) -> Tuple[str, ...]:
    if _uses_modified_asm2d(config):
        return MODIFIED_ASM2D_COMPONENTS
    return tuple(ASM2D_COMPONENTS)

def _particulate_component_ids(config: ASM2DBiologicalConfig) -> Tuple[str, ...]:
    if _uses_modified_asm2d(config):
        return MODIFIED_ASM2D_PARTICULATE_COMPONENTS
    return tuple(ASM2D_PARTICULATE_COMPONENTS)

def _has_mlr_recycle(profile: 'Asm2dFamilyProfile', config: ASM2DBiologicalConfig) -> bool:
    """Return True when this topology has a distinct mixed-liquor recycle.

    A combined-recycle MBR (the MBR retentate IS the mixed-liquor return) carries
    no separate internal-recycle splitter; otherwise an MLR or post-anoxic train
    does.
    """
    if profile.include_mbr and config.combined_mbr_recycle:
        return False
    return bool(profile.include_mlr_recycle or profile.include_post_anoxic)

@dataclass(frozen=True)
class Asm2dFamilyProfile:
    """The structural family knobs of an ASM2D preset.

    These are the parts of a preset that the role graph cannot recover on its
    own: the topology family the preset *intends* (whether it carries an MBR, a
    post-anoxic zone, a distinct MLR) plus the design recycle/MLSS bounds for the
    aerobic technology. The provider derives the *present* structural family from
    the graph's roles for emission; the profile supplies the design bounds and is
    the basis for config normalization at construction time.
    """
    include_mbr: bool = False
    include_mlr_recycle: bool = False
    include_post_anoxic: bool = False
    include_anoxic: bool = True
    ras_recycle_ratio_bounds: Tuple[float, float] = ASM2D_CONVENTIONAL_RAS_RECYCLE_RATIO_BOUNDS
    mlr_recycle_ratio_bounds: Optional[Tuple[float, float]] = None
    mlss_bounds_mg_l: Tuple[float, float] = CONVENTIONAL_AEROBIC_BOUNDS.mlss_mg_l

class Asm2dPresetProvider(DefaultProvider):
    """Default provider for the ASM2D biological recycle family.

    Holds the preset config + :class:`Asm2dFamilyProfile` and, given a
    :class:`RoleResolvedTopology`, yields the full :class:`DefaultBundle` keyed on
    the graph's roles. The same provider instance defaults a template-built
    topology (canonical names) and a from-graph build (project names) to a
    role-identical bundle — the topology-source-agnostic contract the role-graph
    refactor (roadmap A1b) establishes for A4 / A-build to build on.

    Construction normalizes the config in place (recycle-ratio bounds, SRT
    components) exactly as the legacy template ``__init__`` did; the
    normalization is idempotent.
    """

    def __init__(self, profile: Asm2dFamilyProfile, config: ASM2DBiologicalConfig):
        self.profile = profile
        self.config = config
        self._normalize_config_inplace()

    def _normalize_config_inplace(self) -> None:
        config = self.config
        profile = self.profile
        config.ras_recycle_ratio_bounds = profile.ras_recycle_ratio_bounds
        if _has_mlr_recycle(profile, config):
            config.mlr_recycle_ratio_bounds = profile.mlr_recycle_ratio_bounds or ASM2D_AEROBIC_MLR_RECYCLE_RATIO_BOUNDS
        else:
            config.mlr_recycle_ratio = None
            config.mlr_recycle_ratio_bounds = ASM2D_UNIVERSAL_RECYCLE_RATIO_BOUNDS
            config.internal_recycle_fraction = None
            config.post_anoxic_volume_m3 = 0.0
        if not profile.include_anoxic:
            config.anoxic_volume_m3 = 0.0
        if _uses_modified_asm2d(config) and list(config.srt_components) == list(ASM2D_DEFAULT_SRT_COMPONENTS):
            config.srt_components = list(MODIFIED_ASM2D_PARTICULATE_COMPONENTS)

    @staticmethod
    def _family(topology: RoleResolvedTopology) -> Dict[str, bool]:
        """Derive the *present* structural family from the role tags.

        ``include_mbr`` ⟺ an ``mbr_separator`` role is present (vs a secondary
        clarifier); ``include_post_anoxic`` ⟺ a ``post_anoxic`` reactor role is
        present; ``has_mlr_recycle`` ⟺ a distinct ``internal_recycle_splitter``
        role is present. This is the "family pattern" axis of rule resolution.
        """
        return {'include_mbr': topology.has_role(UnitRole.MBR_SEPARATOR), 'include_anoxic': topology.has_role(UnitRole.PRE_ANOXIC), 'include_post_anoxic': topology.has_role(UnitRole.POST_ANOXIC), 'has_mlr_recycle': topology.has_role(UnitRole.INTERNAL_RECYCLE_SPLITTER)}

    @staticmethod
    def _reactor_ids(topology: RoleResolvedTopology) -> List[str]:
        """Return the reactor train in GLOBAL stage order (pre/aerobic/post)."""
        return topology.ordered_stage_ids([UnitRole.PRE_ANOXIC, UnitRole.AEROBIC, UnitRole.POST_ANOXIC])

    @staticmethod
    def _aerobic_id(topology: RoleResolvedTopology) -> Optional[str]:
        """Return the (first) aerobic reactor id the KLa/DO/MLSS rules key on."""
        return topology.first_id(UnitRole.AEROBIC)

    def provide(self, topology: RoleResolvedTopology) -> DefaultBundle:
        family = self._family(topology)
        return DefaultBundle(dof_fixes=self._dof_fixes(topology, family), scaling_factors=self._scaling_factors(topology), feed_states=self._feed_states(topology), process_constraints=self._process_constraints(topology, family), design_parameters=self._design_parameters(topology, family), metadata=self._metadata(family), solver_options=self.config.strength_solver_options(), initialization_order=list(topology.ordered_unit_ids), recycle_ratio_declarations=self._recycle_ratio_declarations(family))

    def effective_mlss_bounds_mg_l(self, reactor_volumes_fixed: Optional[bool]=None, *, include_mbr: Optional[bool]=None) -> Tuple[float, float]:
        """Return the aerobic-reactor MLSS (``X_TSS``) envelope, mode-aware.

        ``reactor_volumes_fixed`` selects the DOF formulation (defaults to the
        config flag); ``include_mbr`` selects the technology band (defaults to the
        profile's MBR flag). See the module ``Mode-aware MLSS envelope priors``
        comment for the rating/design distinction. An explicit tender
        ``target_mlss_mg_l`` (exact lock) overrides both modes.
        """
        config = self.config
        if reactor_volumes_fixed is None:
            reactor_volumes_fixed = config.reactor_volumes_fixed
        if include_mbr is None:
            include_mbr = self.profile.include_mbr
        if config.target_mlss_mg_l is not None:
            target = float(config.target_mlss_mg_l)
            return (target, target)
        if reactor_volumes_fixed:
            return (RATING_MODE_MLSS_FLOOR_MG_L, RATING_MODE_MLSS_CEILING_MG_L)
        if include_mbr:
            strength = config.detected_feed_strength_class()
            if 'industrial' in strength or 'high_strength' in strength:
                return DESIGN_MODE_MBR_MLSS_BAND_MG_L
            if strength == 'municipal':
                return DESIGN_MODE_MUNICIPAL_MBR_MLSS_BAND_MG_L
            return tuple((float(value) for value in self.profile.mlss_bounds_mg_l))
        return tuple((float(value) for value in self.profile.mlss_bounds_mg_l))

    def _effective_mbr_cage_mlss_bounds_mg_l(self, basin_bounds: Tuple[float, float]) -> Tuple[float, float]:
        config = self.config
        ras_lower, ras_upper = config.ras_recycle_ratio_bounds
        if config.ras_recycle_ratio is not None:
            ras_lower = ras_upper = float(config.ras_recycle_ratio)
        ras_lower = max(float(ras_lower), 1e-06)
        ras_upper = max(float(ras_upper), 1e-06)
        lower = float(basin_bounds[0]) * (1.0 + 1.0 / ras_upper)
        upper = float(basin_bounds[1]) * (1.0 + 1.0 / ras_lower)
        if config.detected_feed_strength_class() == 'high_strength_industrial':
            lower = max(lower, 10000.0)
        upper = min(upper, 25000.0)
        return (lower, upper)

    def _component_split_fixes(self, unit_id: str, outlet: str, soluble_fraction: float, particulate_fraction: float) -> List[Dict[str, Any]]:
        fixes = []
        particulates = set(_particulate_component_ids(self.config))
        for component in _separator_component_ids(self.config):
            if component in particulates:
                value = particulate_fraction
            else:
                value = soluble_fraction
            fixes.append({'unit_id': unit_id, 'var_name': f'split_fraction[0,{outlet},{component}]', 'value': value})
        return fixes

    def _dof_fixes(self, topology: RoleResolvedTopology, family: Dict[str, bool]) -> List[Dict[str, Any]]:
        config = self.config
        include_mbr = family['include_mbr']
        include_post_anoxic = family['include_post_anoxic']
        has_mlr_recycle = family['has_mlr_recycle']
        anoxic = topology.single_id(UnitRole.PRE_ANOXIC)
        aerobic = self._aerobic_id(topology)
        ras_pressure = topology.single_id(UnitRole.RAS_PRESSURE)
        direct_modified_mbr_do = _uses_modified_asm2d(config) and include_mbr and config.combined_mbr_recycle and (config.target_do_mg_l is not None)
        fixes = [*([{'unit_id': anoxic, 'var_name': 'volume[0]', 'value': config.anoxic_volume_m3}] if family['include_anoxic'] else []), {'unit_id': aerobic, 'var_name': 'volume[0]', 'value': config.aerobic_volume_m3}, {'unit_id': ras_pressure, 'var_name': 'outlet.pressure[0]', 'value': config.feed_pressure_Pa}]
        if not direct_modified_mbr_do:
            fixes.append({'unit_id': aerobic, 'var_name': 'KLa', 'value': config.aerobic_kla_per_hr})
        if has_mlr_recycle:
            fixes.append({'unit_id': topology.single_id(UnitRole.INTERNAL_RECYCLE_PRESSURE), 'var_name': 'outlet.pressure[0]', 'value': config.feed_pressure_Pa})
        if include_post_anoxic:
            fixes.append({'unit_id': topology.single_id(UnitRole.POST_ANOXIC), 'var_name': 'volume[0]', 'value': config.post_anoxic_volume_m3})
        if direct_modified_mbr_do:
            fixes.append({'unit_id': aerobic, 'var_name': 'outlet.conc_mass_comp[0,S_O2]', 'value': float(config.target_do_mg_l) / 1000.0})
        if include_mbr:
            fixes.extend(self._component_split_fixes(topology.single_id(UnitRole.MBR_SEPARATOR), 'permeate', config.effective_membrane_soluble_permeate_fraction(), config.membrane_particulate_permeate_fraction))
        else:
            fixes.extend(self._component_split_fixes(topology.single_id(UnitRole.SECONDARY_CLARIFIER), 'effluent', config.clarifier_soluble_effluent_fraction, config.clarifier_particulate_effluent_fraction))
        if _has_alkalinity_target(config):
            fixes.append({'unit_id': topology.single_id(UnitRole.ALKALINITY), 'var_name': 'caustic_dose[0]', 'value': 0.0})
        if _has_chemical_p_target(config):
            fixes.append({'unit_id': topology.single_id(UnitRole.CHEMICAL_P), 'var_name': 'metal_dose[0]', 'value': 0.0075})
        if has_mlr_recycle and config.internal_recycle_fraction is not None:
            fixes.append({'unit_id': topology.single_id(UnitRole.INTERNAL_RECYCLE_SPLITTER), 'var_name': 'split_fraction[0,internal_recycle]', 'value': config.internal_recycle_fraction})
        if config.target_srt_days is None:
            fixes.append({'unit_id': topology.single_id(UnitRole.SLUDGE_SPLITTER), 'var_name': 'split_fraction[0,waste]', 'value': config.default_waste_fraction})
        return fixes

    def _scaling_factors(self, topology: RoleResolvedTopology) -> List[Dict[str, Any]]:
        return [{'unit_id': unit_id, 'var_name': 'volume', 'factor': 0.001} for unit_id in self._reactor_ids(topology)]

    def _feed_states(self, topology: RoleResolvedTopology) -> Dict[str, Dict[str, Any]]:
        config = self.config
        state_args: Dict[str, Any] = {'flow_vol': {0: config.feed_flow_m3_per_day / 86400}, 'temperature': {0: config.feed_temperature_K}, 'pressure': {0: config.feed_pressure_Pa}, 'conc_mass_comp': _feed_conc_for_property_package(config)}
        if not _uses_modified_asm2d(config):
            state_args['alkalinity'] = {0: config.alkalinity_mol_per_m3}
        return {topology.single_id(UnitRole.FEED): {'state_args': state_args}}

    def _recycle_ratio_declarations(self, family: Dict[str, bool]) -> Dict[str, Dict[str, Any]]:
        config = self.config
        include_mbr = family['include_mbr']
        has_mlr_recycle = family['has_mlr_recycle']
        declarations: Dict[str, Dict[str, Any]] = {'ras_recycle_ratio': {'ratio_name': 'asm2d_ras_recycle_ratio', 'bounds': tuple((float(v) for v in config.ras_recycle_ratio_bounds)), 'physical_stream': 'MBR cage drain to anoxic basin' if include_mbr else 'clarifier underflow to anoxic basin', 'target_ratio': config.ras_recycle_ratio, 'mode': 'fixed' if config.ras_recycle_ratio is not None else 'free'}}
        if has_mlr_recycle:
            declarations['mlr_recycle_ratio'] = {'ratio_name': 'asm2d_mlr_recycle_ratio', 'bounds': tuple((float(v) for v in config.mlr_recycle_ratio_bounds)), 'physical_stream': 'end-of-aerobic mixed liquor to anoxic basin', 'target_ratio': config.mlr_recycle_ratio, 'mode': 'fixed' if config.mlr_recycle_ratio is not None else 'free'}
        return declarations

    def _recycle_ratio_process_constraints(self, topology: RoleResolvedTopology, *, include_mlr: bool) -> List[Dict[str, Any]]:
        shim = ASM2DTemplateUnits(feed=topology.single_id(UnitRole.FEED), ras_pressure=topology.single_id(UnitRole.RAS_PRESSURE), internal_pressure=topology.single_id(UnitRole.INTERNAL_RECYCLE_PRESSURE))
        return self.config.recycle_ratio_process_constraints(shim, include_mlr=include_mlr)

    def _process_constraints(self, topology: RoleResolvedTopology, family: Dict[str, bool]) -> List[Dict[str, Any]]:
        config = self.config
        include_mbr = family['include_mbr']
        has_mlr_recycle = family['has_mlr_recycle']
        aerobic = self._aerobic_id(topology)
        constraints: List[Dict[str, Any]] = []
        if config.target_srt_days is not None:
            constraints.append({'type': 'srt', 'name': 'target_srt', 'target_days': config.target_srt_days, 'reactor_units': self._reactor_ids(topology), 'waste_unit': topology.single_id(UnitRole.SLUDGE_SPLITTER), 'waste_port': 'waste', 'biomass_components': list(config.srt_components)})
        mlss_bounds = self.effective_mlss_bounds_mg_l(config.reactor_volumes_fixed, include_mbr=include_mbr)
        mlss_constraint: Dict[str, Any] = {'type': 'mlss_bounds', 'name': 'template_mlss_envelope', 'reactor_units': [aerobic], 'component': 'X_TSS', 'lower_mg_l': mlss_bounds[0], 'upper_mg_l': mlss_bounds[1], 'mlss_mode': 'rating_solved_for' if config.reactor_volumes_fixed else 'design_target_band'}
        if not _uses_modified_asm2d(config):
            if config.target_mlss_mg_l is not None:
                mlss_constraint['target_mg_l'] = float(config.target_mlss_mg_l)
            if include_mbr:
                cage_lower, cage_upper = self._effective_mbr_cage_mlss_bounds_mg_l(mlss_bounds)
                mlss_constraint.update({'cage_unit': topology.single_id(UnitRole.MBR_SEPARATOR), 'cage_port': 'retentate', 'cage_lower_mg_l': None, 'cage_upper_mg_l': cage_upper})
            constraints.append(mlss_constraint)
        constraints.extend(self._recycle_ratio_process_constraints(topology, include_mlr=has_mlr_recycle))
        if config.target_do_mg_l is not None and (not (_uses_modified_asm2d(config) and include_mbr and config.combined_mbr_recycle)):
            constraints.append({'type': 'do_target', 'name': 'aerobic_do', 'target_unit': aerobic, 'target_port': 'outlet', 'target_mg_l': float(config.target_do_mg_l), 'kla_lower': 0.0, 'kla_upper': 100.0})
        if _has_chemical_p_target(config):
            constraints.append({'type': 'chemical_p_target', 'name': 'target_effluent_tp', 'dose_unit': topology.single_id(UnitRole.CHEMICAL_P), 'target_unit': topology.single_id(UnitRole.PRODUCT_EFFLUENT), 'target_port': 'inlet', 'target_mg_l': float(config.target_effluent_tp_mg_l), 'p_basis': 'total_p', 'dose_lower': 0.0, 'dose_upper': 0.5})
        if _has_alkalinity_target(config):
            alkalinity_constraint: Dict[str, Any] = {'type': 'alkalinity_target', 'name': 'target_residual_alkalinity', 'dose_unit': topology.single_id(UnitRole.ALKALINITY), 'target_unit': topology.single_id(UnitRole.PRODUCT_EFFLUENT), 'target_port': 'inlet', 'constraint_kind': 'min', 'dose_lower': 0.0, 'dose_upper': 50.0}
            if config.target_residual_alkalinity_mol_per_m3 is not None:
                alkalinity_constraint['target_mol_per_m3'] = float(config.target_residual_alkalinity_mol_per_m3)
            else:
                alkalinity_constraint['target_mg_caco3_l'] = float(config.target_residual_alkalinity_mg_caco3_l)
            constraints.append(alkalinity_constraint)
        return constraints

    def _design_recycle_payload(self, family: Dict[str, bool]) -> Dict[str, Any]:
        """Return design metadata for only the recycle variables in this topology."""
        config = self.config
        has_mlr_recycle = family['has_mlr_recycle']
        declarations = self._recycle_ratio_declarations(family)
        payload: Dict[str, Any] = {'ras': config.ras_recycle_ratio, 'ras_bounds': list(config.ras_recycle_ratio_bounds), 'mode': {'ras': 'fixed' if config.ras_recycle_ratio is not None else 'free'}, 'declared_variables': sorted(declarations), 'topology': {key: {**value, 'bounds': list(value['bounds'])} for key, value in declarations.items()}}
        if has_mlr_recycle:
            payload['mlr'] = config.mlr_recycle_ratio
            payload['mlr_bounds'] = list(config.mlr_recycle_ratio_bounds)
            payload['mode']['mlr'] = 'fixed' if config.mlr_recycle_ratio is not None else 'free'
        return payload

    def _design_parameters(self, topology: RoleResolvedTopology, family: Dict[str, bool]) -> Dict[str, Any]:
        config = self.config
        include_mbr = family['include_mbr']
        include_post_anoxic = family['include_post_anoxic']
        anoxic = topology.single_id(UnitRole.PRE_ANOXIC)
        aerobic = self._aerobic_id(topology)
        reactor_volumes: Dict[str, Any] = {**({anoxic: config.anoxic_volume_m3} if family['include_anoxic'] else {}), aerobic: config.aerobic_volume_m3}
        if include_post_anoxic:
            reactor_volumes[topology.single_id(UnitRole.POST_ANOXIC)] = config.post_anoxic_volume_m3
        return {'target_srt_days': config.target_srt_days, 'target_mlss_mg_l': config.target_mlss_mg_l, 'target_do_mg_l': config.target_do_mg_l, 'target_effluent_tp_mg_l': config.target_effluent_tp_mg_l, 'target_residual_alkalinity_mol_per_m3': config.target_residual_alkalinity_mol_per_m3, 'target_residual_alkalinity_mg_caco3_l': config.target_residual_alkalinity_mg_caco3_l, 'recycle_ratios': self._design_recycle_payload(family), 'asm2d_model_variant': config.property_package.name, 'feed_strength_class': config.detected_feed_strength_class(), 'feed_cod_kg_m3': config.feed_cod_kg_m3(), 'reactor_volumes_m3': reactor_volumes, 'membrane_soluble_permeate_fraction_effective': config.effective_membrane_soluble_permeate_fraction() if include_mbr else None, 'property_parameter_fixes': dict(config.property_parameter_fixes), 'reaction_parameter_fixes': dict(config.reaction_parameter_fixes)}

    def _metadata(self, family: Dict[str, bool]) -> Dict[str, Any]:
        config = self.config
        declarations = self._recycle_ratio_declarations(family)
        return {'feed_strength_class': config.detected_feed_strength_class(), 'solver_options': config.strength_solver_options(), 'asm2d_initialization_solver_options': config.initialization_solver_options(), 'asm2d_near_feasible_residual_tolerance': config.residual_tolerance(), 'asm2d_sd_almost_equal_tolerance': config.sd_almost_equal_tolerance(), 'asm2d_free_recycle_ratios': {key.replace('_recycle_ratio', ''): value['mode'] == 'free' for key, value in declarations.items()}, 'asm2d_recycle_topology': declarations, 'property_parameter_fixes': dict(config.property_parameter_fixes), 'reaction_parameter_fixes': dict(config.reaction_parameter_fixes)}

class _ASM2DBaseTemplate:
    """Shared topology emitter for ASM2d MLE proposal presets.

    The template owns the structural graph (units + connections) for its preset;
    the solver defaults are delegated to an :class:`Asm2dPresetProvider` bound to
    the same config + :class:`Asm2dFamilyProfile`. The legacy ``get_*`` /
    ``recycle_ratio_declarations`` / ``_effective_mlss_bounds_mg_l`` methods are
    preserved as thin provider delegators so the public surface is unchanged.
    """
    include_mbr = False
    include_mlr_recycle = False
    include_post_anoxic = False
    include_anoxic = True
    ras_recycle_ratio_bounds = ASM2D_CONVENTIONAL_RAS_RECYCLE_RATIO_BOUNDS
    mlr_recycle_ratio_bounds: Tuple[float, float] | None = None
    mlss_bounds_mg_l: Tuple[float, float] = CONVENTIONAL_AEROBIC_BOUNDS.mlss_mg_l

    def __init__(self, config: Optional[ASM2DBiologicalConfig]=None, units: Optional[ASM2DTemplateUnits]=None):
        self.config = config or ASM2DBiologicalConfig()
        if not is_asm2d_property_package_type(self.config.property_package):
            raise ValueError('ASM2d templates require property_package=ASM2D or MODIFIED_ASM2D')
        self.units = units or ASM2DTemplateUnits()
        self.profile = self.family_profile()
        self._provider = Asm2dPresetProvider(self.profile, self.config)

    @classmethod
    def family_profile(cls) -> Asm2dFamilyProfile:
        """Return the structural family profile this preset binds the provider to."""
        return Asm2dFamilyProfile(include_mbr=cls.include_mbr, include_mlr_recycle=cls.include_mlr_recycle, include_post_anoxic=cls.include_post_anoxic, include_anoxic=cls.include_anoxic, ras_recycle_ratio_bounds=cls.ras_recycle_ratio_bounds, mlr_recycle_ratio_bounds=cls.mlr_recycle_ratio_bounds, mlss_bounds_mg_l=cls.mlss_bounds_mg_l)

    @property
    def provider(self) -> Asm2dPresetProvider:
        """The default provider this preset is bound to."""
        return self._provider

    @property
    def has_mlr_recycle(self) -> bool:
        """Return True when this topology has a distinct mixed-liquor recycle."""
        return _has_mlr_recycle(self.profile, self.config)

    def _uses_modified_asm2d(self) -> bool:
        """Return True for the ModifiedASM2d property/reaction package."""
        return _uses_modified_asm2d(self.config)

    def _has_chemical_p_target(self) -> bool:
        return _has_chemical_p_target(self.config)

    def _has_alkalinity_target(self) -> bool:
        return _has_alkalinity_target(self.config)

    def _pre_separator_treatment_units(self) -> List[str]:
        units: List[str] = []
        if self._has_alkalinity_target():
            units.append(self.units.alkalinity)
        if self._has_chemical_p_target():
            units.append(self.units.chemical_p)
        return units

    def _append_separator_feed_connections(self, connections: List[Dict[str, str]], *, src_unit: str, src_port: str, solids_separator: str) -> None:
        current_unit = src_unit
        current_port = src_port
        for treatment_unit in self._pre_separator_treatment_units():
            connections.append({'src_unit': current_unit, 'src_port': current_port, 'dest_unit': treatment_unit, 'dest_port': 'inlet'})
            current_unit = treatment_unit
            current_port = 'outlet'
        connections.append({'src_unit': current_unit, 'src_port': current_port, 'dest_unit': solids_separator, 'dest_port': 'inlet'})

    def get_units(self) -> List[Dict[str, Any]]:
        """Return unit specifications for the template."""
        units = [{'unit_id': self.units.feed, 'unit_type': 'Feed', 'config': {}}, {'unit_id': self.units.mixer, 'unit_type': 'Mixer', 'config': {'inlet_list': ['feed_water', 'ras', 'internal_recycle'] if self.has_mlr_recycle else ['feed_water', 'ras'], 'momentum_mixing_type': 'equality' if self.include_mbr and self.config.combined_mbr_recycle else 'none', 'deactivate_pressure_equality_inlets': [2] if self.include_mbr and self.config.combined_mbr_recycle else []}}, *([{'unit_id': self.units.anoxic, 'unit_type': 'CSTR', 'config': {}}] if self.include_anoxic else []), {'unit_id': self.units.aerobic, 'unit_type': 'CSTR_Injection', 'config': {'has_aeration': True}}]
        if self.include_post_anoxic:
            units.append({'unit_id': self.units.post_anoxic, 'unit_type': 'CSTR', 'config': {}})
        if self.has_mlr_recycle:
            units.extend([{'unit_id': self.units.internal_splitter, 'unit_type': 'Separator', 'config': {'outlet_list': ['forward', 'internal_recycle'], 'split_basis': 'totalFlow'}}, {'unit_id': self.units.internal_pressure, 'unit_type': 'PressureChanger', 'config': {}}])
        if self._has_alkalinity_target():
            units.append({'unit_id': self.units.alkalinity, 'unit_type': 'AlkalinityDoseASM2D', 'config': {}})
        if self._has_chemical_p_target():
            units.append({'unit_id': self.units.chemical_p, 'unit_type': 'ChemicalPhosphorusRemovalASM2D', 'config': {}})
        if self.include_mbr:
            units.append({'unit_id': self.units.mbr, 'unit_type': 'MBRSeparator', 'config': {}})
        else:
            units.append({'unit_id': self.units.clarifier, 'unit_type': 'SecondaryClarifierASM', 'config': {}})
        units.extend([{'unit_id': self.units.sludge_splitter, 'unit_type': 'Separator', 'config': {'outlet_list': ['waste', 'recycle'], 'split_basis': 'totalFlow'}}, {'unit_id': self.units.ras_pressure, 'unit_type': 'PressureChanger', 'config': {}}, {'unit_id': self.units.effluent, 'unit_type': 'Product', 'config': {}}, {'unit_id': self.units.waste, 'unit_type': 'Product', 'config': {}}])
        for unit in units:
            unit['role'] = self.units.role_for(unit['unit_id'])
        return units

    def get_connections(self) -> List[Dict[str, str]]:
        """Return directed connections for the template."""
        solids_separator = self.units.mbr if self.include_mbr else self.units.clarifier
        solids_port = 'retentate' if self.include_mbr else 'underflow'
        treated_port = 'permeate' if self.include_mbr else 'effluent'
        connections = [{'src_unit': self.units.feed, 'src_port': 'outlet', 'dest_unit': self.units.mixer, 'dest_port': 'feed_water'}, {'src_unit': self.units.mixer, 'src_port': 'outlet', 'dest_unit': self.units.anoxic if self.include_anoxic else self.units.aerobic, 'dest_port': 'inlet'}, *([{'src_unit': self.units.anoxic, 'src_port': 'outlet', 'dest_unit': self.units.aerobic, 'dest_port': 'inlet'}] if self.include_anoxic else [])]
        if self.has_mlr_recycle:
            treatment_units = self._pre_separator_treatment_units()
            forward_dest = self.units.post_anoxic if self.include_post_anoxic else treatment_units[0] if treatment_units else solids_separator
            connections.extend([{'src_unit': self.units.aerobic, 'src_port': 'outlet', 'dest_unit': self.units.internal_splitter, 'dest_port': 'inlet'}, {'src_unit': self.units.internal_splitter, 'src_port': 'internal_recycle', 'dest_unit': self.units.internal_pressure, 'dest_port': 'inlet'}, {'src_unit': self.units.internal_pressure, 'src_port': 'outlet', 'dest_unit': self.units.mixer, 'dest_port': 'internal_recycle'}, {'src_unit': self.units.internal_splitter, 'src_port': 'forward', 'dest_unit': forward_dest, 'dest_port': 'inlet'}])
            if not self.include_post_anoxic and treatment_units:
                for src_unit, dest_unit in zip(treatment_units, treatment_units[1:]):
                    connections.append({'src_unit': src_unit, 'src_port': 'outlet', 'dest_unit': dest_unit, 'dest_port': 'inlet'})
                connections.append({'src_unit': treatment_units[-1], 'src_port': 'outlet', 'dest_unit': solids_separator, 'dest_port': 'inlet'})
            elif self.include_post_anoxic:
                self._append_separator_feed_connections(connections, src_unit=self.units.post_anoxic, src_port='outlet', solids_separator=solids_separator)
        else:
            self._append_separator_feed_connections(connections, src_unit=self.units.aerobic, src_port='outlet', solids_separator=solids_separator)
        connections.extend([{'src_unit': solids_separator, 'src_port': treated_port, 'dest_unit': self.units.effluent, 'dest_port': 'inlet'}, {'src_unit': solids_separator, 'src_port': solids_port, 'dest_unit': self.units.sludge_splitter, 'dest_port': 'inlet'}, {'src_unit': self.units.sludge_splitter, 'src_port': 'waste', 'dest_unit': self.units.waste, 'dest_port': 'inlet'}, {'src_unit': self.units.sludge_splitter, 'src_port': 'recycle', 'dest_unit': self.units.ras_pressure, 'dest_port': 'inlet'}, {'src_unit': self.units.ras_pressure, 'src_port': 'outlet', 'dest_unit': self.units.mixer, 'dest_port': 'ras'}])
        return connections

    def _topology(self) -> RoleResolvedTopology:
        """Return the role-resolved view of this preset's topology graph."""
        return RoleResolvedTopology.from_unit_specs(self.get_units())

    def default_bundle(self) -> DefaultBundle:
        """Return the provider-resolved default bundle for this preset."""
        return self._provider.provide(self._topology())

    def recycle_ratio_declarations(self) -> Dict[str, Dict[str, Any]]:
        """Return declared system-level recycle ratio variables."""
        return self.default_bundle().recycle_ratio_declarations

    def _effective_mlss_bounds_mg_l(self, reactor_volumes_fixed: Optional[bool]=None) -> Tuple[float, float]:
        """Return the aerobic-reactor MLSS (``X_TSS``) envelope, mode-aware."""
        return self._provider.effective_mlss_bounds_mg_l(reactor_volumes_fixed)

    def get_dof_fixes(self) -> List[Dict[str, Any]]:
        """Return fixed variables required for the template model."""
        return self.default_bundle().dof_fixes

    def get_scaling_factors(self) -> List[Dict[str, Any]]:
        """Return recommended scaling factors for biological variables."""
        return self.default_bundle().scaling_factors

    def get_feed_states(self) -> Dict[str, Dict[str, Any]]:
        """Return default feed state for a tender-scale ASM2d wastewater feed."""
        return self.default_bundle().feed_states

    def get_process_constraints(self) -> List[Dict[str, Any]]:
        """Return algebraic process constraints for the template."""
        return self.default_bundle().process_constraints

    def get_design_parameters(self) -> Dict[str, Any]:
        """Return user-facing biological design parameters represented here."""
        return self.default_bundle().design_parameters

    def get_initialization_order(self) -> List[str]:
        """Return a deterministic initialization order hint."""
        return self.default_bundle().initialization_order

    def to_session_spec(self) -> Dict[str, Any]:
        """Convert template to a session specification.

        Topology (units + connections) is the template's; the solver defaults are
        the provider's, resolved against the role-tagged graph.
        """
        units = self.get_units()
        connections = self.get_connections()
        bundle = self._provider.provide(RoleResolvedTopology.from_unit_specs(units))
        return {'property_package': self.config.property_package.name, 'units': units, 'connections': connections, 'dof_fixes': bundle.dof_fixes, 'scaling_factors': bundle.scaling_factors, 'feed_states': bundle.feed_states, 'process_constraints': bundle.process_constraints, 'design_parameters': bundle.design_parameters, 'metadata': bundle.metadata, 'solver_options': bundle.solver_options, 'initialization_order': bundle.initialization_order}

class ASM2DMLETemplate(_ASM2DBaseTemplate):
    """CAS MLE template with secondary clarifier, RAS, and WAS."""
    include_mlr_recycle = True
    mlr_recycle_ratio_bounds = ASM2D_AEROBIC_MLR_RECYCLE_RATIO_BOUNDS

class ASM2DMLEPOSTTemplate(_ASM2DBaseTemplate):
    """CAS MLE_POST template with post-anoxic mixed-liquor recycle."""
    include_post_anoxic = True
    mlr_recycle_ratio_bounds = ASM2D_AEROBIC_MLR_RECYCLE_RATIO_BOUNDS

class ASM2DMBRMLETemplate(_ASM2DBaseTemplate):
    """MBR MLE template with algebraic permeate/retentate split."""
    include_mbr = True
    include_mlr_recycle = True
    ras_recycle_ratio_bounds = ASM2D_MBR_RAS_RECYCLE_RATIO_BOUNDS
    mlss_bounds_mg_l = MBR_AEROBIC_BOUNDS.mlss_mg_l

class ASM2DMBRAerobicTemplate(_ASM2DBaseTemplate):
    """Aerobic-only MBR template: the feed and the membrane return mix into aeration; no anoxic zone, no MLR."""
    include_mbr = True
    include_anoxic = False
    ras_recycle_ratio_bounds = ASM2D_MBR_RAS_RECYCLE_RATIO_BOUNDS
    mlss_bounds_mg_l = MBR_AEROBIC_BOUNDS.mlss_mg_l

class ASM2DMBRMLEPOSTTemplate(_ASM2DBaseTemplate):
    """MBR MLE_POST template with post-anoxic zone and two recycle paths."""
    include_mbr = True
    include_post_anoxic = True
    ras_recycle_ratio_bounds = ASM2D_MBR_RAS_RECYCLE_RATIO_BOUNDS
    mlr_recycle_ratio_bounds = ASM2D_AEROBIC_MLR_RECYCLE_RATIO_BOUNDS
    mlss_bounds_mg_l = MBR_AEROBIC_BOUNDS.mlss_mg_l
