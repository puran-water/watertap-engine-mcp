"""Registry for supported and quarantined flowsheet templates."""
from __future__ import annotations
from dataclasses import asdict, dataclass, fields, is_dataclass
from typing import Any, Callable, Dict, Optional, Type
from watertap_engine_mcp.core.property_registry import PropertyPackageType
from puran_mcp_support.engineering.heuristics.flowsheet_templates.registry import CONVENTIONAL_AEROBIC_BOUNDS, MBR_AEROBIC_BOUNDS
from .adm1 import ADM1DigestionConfig, Adm1PresetProvider, AnaerobicContactDigestionConfig, AnaerobicContactTemplate, AnMBRDigestionConfig, AnMBRTemplate, EGSBDigestionConfig, EGSBTemplate, KernelCSTRTemplate, TwoStageDigestionConfig, TwoStageTemplate, UASBTemplate
from .asm2d import ASM2DBiologicalConfig, ASM2DMBRMLEPOSTTemplate, ASM2DMBRMLETemplate, ASM2DMLEPOSTTemplate, ASM2DMBRAerobicTemplate, ASM2DMLETemplate, Asm2dPresetProvider
from .ed_multi_ion import EDKStageSeriesConfig, EDKStageSeriesTemplate, EDStackConcRecircConfig, EDStackConcRecircTemplate
from .mvc_crystallizer import MVCCrystallizerConfig, MVCCrystallizerTemplate
from .mvc_evaporator import MVCEvaporatorConfig, MVCEvaporatorTemplate
from .nf_softening import NFSofteningConfig, NFSofteningTemplate
from .ro_train import ROTrainConfig, ROTrainTemplate

@dataclass(frozen=True)
class TemplateSpec:
    """Metadata for a flowsheet template.

    For the role-based families (ASM2D today; ADM1/carbon under roadmap A4/A7),
    a "template" is a **named preset**: ``template_class`` emits the structural
    topology while ``provider_class`` (a ``DefaultProvider``) owns the solver
    defaults, resolved against the topology's role tags. ``provider_class`` is
    ``None`` for legacy templates that still inline their own defaults (RO / NF /
    MVC); those will migrate onto their own family providers as A4/A-build land.
    """
    name: str
    summary: str
    template_class: type
    config_class: type
    recycle_ratio_vars: tuple[str, ...] = ()
    recycle_ratio_bounds: dict[str, tuple[float, float]] | None = None
    supported: bool = True
    reason: Optional[str] = None
    provider_class: Optional[type] = None
TEMPLATES: dict[str, TemplateSpec] = {'asm2d_mle': TemplateSpec(name='asm2d_mle', summary='ASM2d MLE steady-state topology with RAS, MLR, WAS, and SRT constraint.', template_class=ASM2DMLETemplate, config_class=ASM2DBiologicalConfig, recycle_ratio_vars=('ras_recycle_ratio', 'mlr_recycle_ratio'), recycle_ratio_bounds={'ras_recycle_ratio': CONVENTIONAL_AEROBIC_BOUNDS.ras_recycle_ratio, 'mlr_recycle_ratio': CONVENTIONAL_AEROBIC_BOUNDS.mlr_recycle_ratio}, provider_class=Asm2dPresetProvider, supported=True), 'asm2d_mle_post': TemplateSpec(name='asm2d_mle_post', summary='ASM2d MLE_POST steady-state topology with RAS, MLR, WAS, and SRT constraint.', template_class=ASM2DMLEPOSTTemplate, config_class=ASM2DBiologicalConfig, recycle_ratio_vars=('ras_recycle_ratio', 'mlr_recycle_ratio'), recycle_ratio_bounds={'ras_recycle_ratio': CONVENTIONAL_AEROBIC_BOUNDS.ras_recycle_ratio, 'mlr_recycle_ratio': CONVENTIONAL_AEROBIC_BOUNDS.mlr_recycle_ratio}, provider_class=Asm2dPresetProvider, supported=True), 'asm2d_mbr': TemplateSpec(name='asm2d_mbr', summary='ASM2d MBR MLE steady-state topology with algebraic membrane split, RAS, and MLR; the direct ModifiedASM2d rating puts a reacting, scour-aerated membrane tank before the split where it states one (design_targets.membrane_tank_reactor).', template_class=ASM2DMBRMLETemplate, config_class=ASM2DBiologicalConfig, recycle_ratio_vars=('ras_recycle_ratio', 'mlr_recycle_ratio'), recycle_ratio_bounds={'ras_recycle_ratio': MBR_AEROBIC_BOUNDS.ras_recycle_ratio, 'mlr_recycle_ratio': MBR_AEROBIC_BOUNDS.mlr_recycle_ratio}, provider_class=Asm2dPresetProvider, supported=True), 'asm2d_mbr_aerobic': TemplateSpec(name='asm2d_mbr_aerobic', summary='ASM2d aerobic-only MBR steady-state topology (no anoxic zone) with algebraic membrane split and RAS to aeration; the direct ModifiedASM2d rating puts a reacting, scour-aerated membrane tank before the split where it states one (design_targets.membrane_tank_reactor).', template_class=ASM2DMBRAerobicTemplate, config_class=ASM2DBiologicalConfig, recycle_ratio_vars=('ras_recycle_ratio',), recycle_ratio_bounds={'ras_recycle_ratio': MBR_AEROBIC_BOUNDS.ras_recycle_ratio}, provider_class=Asm2dPresetProvider, supported=True), 'asm2d_mbr_post': TemplateSpec(name='asm2d_mbr_post', summary='ASM2d MBR MLE_POST steady-state topology with post-anoxic zone, RAS, and MLR.', template_class=ASM2DMBRMLEPOSTTemplate, config_class=ASM2DBiologicalConfig, recycle_ratio_vars=('ras_recycle_ratio', 'mlr_recycle_ratio'), recycle_ratio_bounds={'ras_recycle_ratio': MBR_AEROBIC_BOUNDS.ras_recycle_ratio, 'mlr_recycle_ratio': MBR_AEROBIC_BOUNDS.mlr_recycle_ratio}, provider_class=Asm2dPresetProvider, supported=True), 'adm1_uasb': TemplateSpec(name='adm1_uasb', summary='mADM1 UASB anaerobic digester (single methanogenic reactor) with a biogas take-off gas phase; DOF=0 with solver-determined vapor H2 / pH / trace ions and a preserved gas-phase headspace pressure.', template_class=UASBTemplate, config_class=ADM1DigestionConfig, provider_class=Adm1PresetProvider, supported=True), 'adm1_cstr': TemplateSpec(name='adm1_cstr', summary='Literature-based modified ADM1 kernel (QSDsan lineage); kinetic parameters are not site fits.', template_class=KernelCSTRTemplate, config_class=ADM1DigestionConfig, provider_class=Adm1PresetProvider, supported=True), 'adm1_egsb': TemplateSpec(name='adm1_egsb', summary='mADM1 EGSB digester (single methanogenic reactor) with high upflow liquid recirculation closed by the ADM1 recycle tear; biogas take-off; DOF=0 with solver-determined vapor H2 / pH / trace ions.', template_class=EGSBTemplate, config_class=EGSBDigestionConfig, provider_class=Adm1PresetProvider, supported=True), 'adm1_anmbr': TemplateSpec(name='adm1_anmbr', summary='mADM1 AnMBR: methanogenic reactor + biomass-retaining membrane with a retentate recycle and WAS bleed (SRT decoupling) closed by the ADM1 recycle tear; biogas take-off; DOF=0 with solver-determined pH/ions.', template_class=AnMBRTemplate, config_class=AnMBRDigestionConfig, provider_class=Adm1PresetProvider, supported=True), 'adm1_contact': TemplateSpec(name='adm1_contact', summary='mADM1 anaerobic-contact: CSTR digester + gravity thickener with a sludge-return (RAS) recycle and WAS bleed closed by the ADM1 recycle tear; biogas take-off; DOF=0 with solver-determined pH/ions.', template_class=AnaerobicContactTemplate, config_class=AnaerobicContactDigestionConfig, provider_class=Adm1PresetProvider, supported=True), 'adm1_two_stage': TemplateSpec(name='adm1_two_stage', summary='mADM1 phase-separated two-stage: acidogenic reactor feeding a methanogenic reactor (forward acid->methane cascade), each with its own biogas headspace and solver-determined per-stage pH; DOF=0.', template_class=TwoStageTemplate, config_class=TwoStageDigestionConfig, provider_class=Adm1PresetProvider, supported=True), 'ro_train': TemplateSpec(name='ro_train', summary='Single-stage RO train without ERD topology.', template_class=ROTrainTemplate, config_class=ROTrainConfig, supported=True), 'nf_softening': TemplateSpec(name='nf_softening', summary='Nanofiltration softening train with pinned default MCAS config.', template_class=NFSofteningTemplate, config_class=NFSofteningConfig, supported=True), 'ed_k_stage_series': TemplateSpec(name='ed_k_stage_series', summary='Multi-ion EDR rating: k Electrodialysis1D stacks in series on the diluate (one pass), concentrate fed from the common feed-and-bleed loop state; constant current, Nernst layer, Donnan potential, theoretical limiting current, Darcy-Weisbach (profile constant_current_multi_ion_1d); datasheet geometry bound by rating_geometry.', template_class=EDKStageSeriesTemplate, config_class=EDKStageSeriesConfig, supported=False, reason="not solve-qualified: at datasheet (MEGA EDR-II/250) geometry the pipeline's Electrodialysis1D initialisation converges to a locally infeasible point (1-stage NaCl probe); rate EDR duties with engineering_utils.heuristics.electrodialysis until an electrodialysis initializer recipe qualifies it"), 'ed_stack_conc_recirc': TemplateSpec(name='ed_stack_conc_recirc', summary="Multi-ion EDR rating: one Electrodialysis1D stack with feed split to diluate and concentrate make-up and a recirculated, bled concentrate loop (tear stream); port of WaterTAP's electrodialysis_1stack_conc_recirc on the constant_current_multi_ion_1d profile.", template_class=EDStackConcRecircTemplate, config_class=EDStackConcRecircConfig, supported=False, reason="not solve-qualified: the engine's Mixer carries no MCAS contract, so the concentrate-loop model does not build, and a direct Electrodialysis1D at datasheet geometry fails initialisation; rate EDR duties with engineering_utils.heuristics.electrodialysis until the Mixer contract and an initializer recipe qualify it"), 'mvc_evaporator': TemplateSpec(name='mvc_evaporator', summary='MVC evaporator: SEAWATER feed, WATER compressor/condenser, auto-translator distillate.', template_class=MVCEvaporatorTemplate, config_class=MVCEvaporatorConfig, supported=True), 'mvc_crystallizer': TemplateSpec(name='mvc_crystallizer', summary='ZLD train: MVC evaporator brine to NaCl crystallizer.', template_class=MVCCrystallizerTemplate, config_class=MVCCrystallizerConfig, supported=True)}

def _coerce_config_kwargs(config_class: type, raw_config: dict[str, Any]) -> dict[str, Any]:
    """Coerce string enum values in raw template config."""
    if not is_dataclass(config_class):
        return raw_config
    coerced = dict(raw_config)
    for field in fields(config_class):
        if field.name not in coerced:
            continue
        is_property_package_type = field.type is PropertyPackageType or field.type == 'PropertyPackageType'
        if is_property_package_type and isinstance(coerced[field.name], str):
            coerced[field.name] = PropertyPackageType[coerced[field.name].upper()]
    return coerced

def list_template_specs() -> list[TemplateSpec]:
    """Return all known template specs."""
    return list(TEMPLATES.values())

def get_template_spec(name: str) -> TemplateSpec:
    """Look up a template by registry name."""
    normalized = name.strip().lower().replace('-', '_')
    if normalized not in TEMPLATES:
        raise KeyError(name)
    return TEMPLATES[normalized]

def build_template(name: str, template_config: Optional[dict[str, Any]]=None):
    """Instantiate a template from the registry."""
    spec = get_template_spec(name)
    if not spec.supported:
        raise NotImplementedError(spec.reason or f"Template '{name}' is not supported")
    config_kwargs = _coerce_config_kwargs(spec.config_class, template_config or {})
    config = spec.config_class(**config_kwargs)
    return spec.template_class(config)

def get_preset_provider(name: str, template_config: Optional[dict[str, Any]]=None):
    """Return the default provider bound to a registered preset.

    The preset binds its family provider (``spec.provider_class``) to the
    template's specific config and structural family profile. A from-graph build
    (a role-tagged BFD / ``simulation_seed`` topology) runs the returned provider
    over its own :class:`core.default_provider.RoleResolvedTopology` to inherit
    the same role-resolved defaults as the named-preset path. Raises
    ``NotImplementedError`` for legacy templates that have no provider yet.
    """
    spec = get_template_spec(name)
    if spec.provider_class is None:
        raise NotImplementedError(f"Template '{name}' has no role-based default provider; it still inlines its own defaults.")
    return build_template(name, template_config).provider

def serialize_template_spec(spec: TemplateSpec) -> dict[str, Any]:
    """Convert a template spec into a JSON-safe payload."""
    return {'name': spec.name, 'summary': spec.summary, 'supported': spec.supported, 'reason': spec.reason, 'config_class': spec.config_class.__name__, 'template_class': spec.template_class.__name__, 'provider_class': spec.provider_class.__name__ if spec.provider_class is not None else None, 'recycle_ratio_vars': list(spec.recycle_ratio_vars), 'recycle_ratio_bounds': {key: list(value) for key, value in (spec.recycle_ratio_bounds or {}).items()}}

def declared_recycle_ratio_vars(name: str) -> tuple[str, ...]:
    """Return the recycle ratio variable keys declared by a template."""
    return get_template_spec(name).recycle_ratio_vars
