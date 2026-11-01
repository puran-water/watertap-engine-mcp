"""Compile shared simulation_seed/v2 payloads into WaterTAP session specs.

Two biological compilation routes exist (roadmap A-build):

* **Designed-graph route** — when the seeds (or a topology the caller hands in)
  carry an explicit structural-role tag on every unit, the seed GRAPH is the
  topology. The role-tagged units + connections (including recycle
  ``connects_to`` / ``tear_stream`` arcs) are resolved via the shared
  ``engineering_utils.topology.engine_dag`` resolver, and the solver defaults
  (DOF fixes, scaling, feed, constraints, solver metadata) are supplied by the
  A1b :class:`core.default_provider.DefaultProvider`, keyed on role / role-index
  / unit-type / family. No template instance is in the loop. The result is
  tagged ``topology_source="designed_graph"``.
* **Preset-fallback route** — when no trusted role-tagged graph is supplied
  (the legacy partial aerobic-emitter seeds carry reactor + separator markers
  only, not the full mechanical scaffolding), a named ASM2D preset SYNTHESIZES
  the topology from the seed-derived config. The result is tagged
  ``topology_source="preset_fallback"``.

A supplied graph that carries roles but FAILS role validation is NOT silently
replaced by a preset: :func:`validate_role_graph` raises a structured
diff-vs-nearest-preset error reporting the missing roles.
"""
from __future__ import annotations
from dataclasses import dataclass
from math import isclose
from typing import Any, Dict, List, Literal, Optional, Sequence
from puran_mcp_support.engineering.models.simulation_seed import SimulationSeed
from puran_mcp_support.engineering.simulation_role_graph import DESIGNED_GRAPH_FAMILY_ADM1, DESIGNED_GRAPH_FAMILY_ASM2D
from puran_mcp_support.engineering.topology.engine_dag import ResolvedSeedConnection, resolve_seed_connections
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from watertap_engine_mcp.core.default_provider import DefaultProvider, RoleResolvedTopology
from watertap_engine_mcp.core.property_registry import PropertyPackageType
from watertap_engine_mcp.core.roles import ADM1_SINGLE_STAGE_REQUIRED_ROLES, ADM1_TWO_STAGE_REQUIRED_ROLES, ASM2D_MBR_RETENTATE_RECYCLE_REQUIRED_ROLES, ASM2D_MLE_RECYCLE_REQUIRED_ROLES, RoleResolver, UnitRole, normalize_role
from watertap_engine_mcp.templates.adm1 import Adm1PresetProvider, AnaerobicContactTemplate, AnMBRTemplate, EGSBTemplate, TwoStageTemplate, UASBTemplate
from watertap_engine_mcp.templates.asm2d import ASM2DBiologicalConfig, ASM2DMBRMLEPOSTTemplate, ASM2DMBRAerobicTemplate, ASM2DMBRMLETemplate, ASM2DMLEPOSTTemplate, ASM2DMLETemplate, Asm2dPresetProvider
from watertap_engine_mcp.templates.registry import _coerce_config_kwargs, declared_recycle_ratio_vars
TOPOLOGY_SOURCE_DESIGNED_GRAPH = 'designed_graph'
TOPOLOGY_SOURCE_PRESET_FALLBACK = 'preset_fallback'
_ADM1_VAPOR_PACKAGE = PropertyPackageType.ADM1_VAPOR.name

@dataclass(frozen=True)
class WaterTAPSeedBuildPlan:
    """Compiled WaterTAP session plan from simulation seeds."""
    template_name: str | None
    session_spec: Dict[str, Any]
    seeds: List[SimulationSeed]
    warnings: List[str]
    topology_source: str = TOPOLOGY_SOURCE_PRESET_FALLBACK

class _ROExecutableBasis(BaseModel):
    """Typed, complete boundary accepted by the single-stage RO compiler."""
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    property_package: Literal['SEAWATER']
    solutes: tuple[Literal['TDS']]
    ro_topology: Literal['single_stage_no_recycle']
    n_stages: Literal[1]
    fresh_feed_flow_m3h: float = Field(gt=0.0)
    feed_flow_m3h: float = Field(gt=0.0)
    feed_salinity_ppm: float = Field(ge=0.0)
    feed_pressure_bar: float = Field(gt=0.0)
    feed_temperature_K: float = Field(gt=0.0)
    target_stage_recovery: float = Field(gt=0.0, lt=1.0)
    stage_permeate_flow_m3h: float = Field(ge=0.0)
    stage_concentrate_flow_m3h: float = Field(ge=0.0)
    total_permeate_flow_m3h: float = Field(ge=0.0)
    final_concentrate_flow_m3h: float = Field(ge=0.0)
    recycle_flow_m3h: Literal[0.0]
    disposal_flow_m3h: float = Field(ge=0.0)

    @model_validator(mode='after')
    def _close_complete_boundary(self) -> '_ROExecutableBasis':
        checks = (('stage feed = fresh feed + recycle', self.feed_flow_m3h, self.fresh_feed_flow_m3h + self.recycle_flow_m3h), ('stage feed = stage permeate + stage concentrate', self.feed_flow_m3h, self.stage_permeate_flow_m3h + self.stage_concentrate_flow_m3h), ('total permeate = stage permeate', self.total_permeate_flow_m3h, self.stage_permeate_flow_m3h), ('final concentrate = stage concentrate', self.final_concentrate_flow_m3h, self.stage_concentrate_flow_m3h), ('final concentrate = recycle + disposal', self.final_concentrate_flow_m3h, self.recycle_flow_m3h + self.disposal_flow_m3h), ('fresh feed = total permeate + disposal', self.fresh_feed_flow_m3h, self.total_permeate_flow_m3h + self.disposal_flow_m3h), ('target recovery = stage permeate / stage feed', self.target_stage_recovery, self.stage_permeate_flow_m3h / self.feed_flow_m3h))
        for label, declared, physical in checks:
            if not isclose(declared, physical):
                raise ValueError(f'RO executable boundary does not close: {label}; declared={declared!r}, physical={physical!r}')
        return self

class _ROEngineeringInputs(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    A_comp: float = Field(gt=0.0)
    B_comp: float = Field(ge=0.0)
    area: float = Field(gt=0.0)
    permeate_pressure: float = Field(gt=0.0)
    feed_side_cp_modulus_all_all: float = Field(gt=0.0)
    feed_side_deltaP: float = Field(lt=0.0)

class _ROConstructorBasis(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    property_package: Literal['SEAWATER']
    has_pressure_change: Literal[True]
    concentration_polarization_type: Literal['fixed']
    mass_transfer_coefficient: Literal['none']

class _IXExecutableBasis(BaseModel):
    """Only the certified single-target sodium-form SAC profile."""
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    property_package: Literal['MCAS']
    target_ion: Literal['Ca_2+']
    resin_type: Literal['SAC']
    resin_form: Literal['Na']
    ix_topology: Literal['single_target_sac']

class _IXEngineeringInputs(BaseModel):
    model_config = ConfigDict(extra='forbid', populate_by_name=True, allow_inf_nan=False)
    service_flow_rate: float = Field(gt=0.0)
    langmuir_ca: float = Field(gt=0.0, alias='langmuir_Ca_2+')
    resin_max_capacity: float = Field(gt=0.0)
    bed_depth: float = Field(gt=0.0)
    number_columns: float = Field(gt=0.0)

class _IXConstructorBasis(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    property_package: Literal['MCAS']
    target_ion: Literal['Ca_2+']

class RoleGraphValidationError(ValueError):
    """A role-tagged designed graph failed role validation.

    Carries a structured ``payload`` (a diff against the nearest registered ASM2D
    preset: present roles, the nearest preset, and the roles it is missing) so the
    caller can surface a diff/oracle to the user rather than silently substituting
    a preset topology. Subclasses ``ValueError`` so existing ``except ValueError``
    boundaries in the compile path still catch it.
    """

    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload
        super().__init__(payload.get('message', 'role graph validation failed'))

def _parse_seeds(simulation_seeds: Sequence[Dict[str, Any]]) -> List[SimulationSeed]:
    parsed = [SimulationSeed(**seed) for seed in simulation_seeds]
    active = [seed for seed in parsed if seed.engine_key != 'none']
    if not active:
        raise ValueError('No active simulation seeds were provided.')
    return active
_ASM2D_BIO_MARKERS = ('asm2d', 'masm2d', 'activated', 'mbr')
_ADM1_BIO_MARKERS = ('adm1', 'anaerobic', 'methanogenic', 'acidogenic')

def _seed_marker_token(seed: SimulationSeed) -> str:
    return ' '.join((str(value or '') for value in (seed.engine_key, seed.kinetic_model, seed.unit_type, seed.qsdsan_class, seed.watertap_class))).lower()

def _seed_bio_family(seed: SimulationSeed) -> Optional[str]:
    """Return which biological family a seed belongs to, or ``None``.

    ``"asm2d"`` (:data:`DESIGNED_GRAPH_FAMILY_ASM2D`) for an aerobic activated-sludge
    seed, ``"adm1"`` (:data:`DESIGNED_GRAPH_FAMILY_ADM1`) for an anaerobic-digestion
    seed, ``None`` for a non-biological (RO/IX/evap/gas) seed.
    """
    token = _seed_marker_token(seed)
    if any((marker in token for marker in _ASM2D_BIO_MARKERS)):
        return DESIGNED_GRAPH_FAMILY_ASM2D
    if any((marker in token for marker in _ADM1_BIO_MARKERS)):
        return DESIGNED_GRAPH_FAMILY_ADM1
    return None

def _is_biological(seed: SimulationSeed) -> bool:
    """True for an ASM2D aerobic activated-sludge seed.

    Kept ASM2D-scoped (name + semantics unchanged) because the preset-fallback
    branch of :func:`build_plan_from_simulation_seeds` synthesizes an ASM2D preset
    topology. Anaerobic seeds are detected separately via :func:`_is_anaerobic` /
    :func:`_seed_bio_family`.
    """
    return _seed_bio_family(seed) == DESIGNED_GRAPH_FAMILY_ASM2D

def _is_anaerobic(seed: SimulationSeed) -> bool:
    """True for an ADM1 / mADM1 anaerobic-digestion seed."""
    return _seed_bio_family(seed) == DESIGNED_GRAPH_FAMILY_ADM1

def _seed_values(seed: SimulationSeed) -> Dict[str, Any]:
    values = dict(seed.constructor_kwargs)
    values.update(seed.engineering_inputs)
    return values

def _first_float(values: Dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = values.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None

def _route_generic_recycle_ratio(seed_id: str, hint: str) -> str | None:
    token = f'{seed_id} {hint}'.lower()
    if any((marker in token for marker in ('mlr', 'internal_recycle', 'mixed_liquor'))):
        return 'mlr_recycle_ratio'
    if any((marker in token for marker in ('ras', 'return_activated', 'return_sludge', 'sludge_splitter'))):
        return 'ras_recycle_ratio'
    return None

def _infer_template_name(seeds: Sequence[SimulationSeed], template_hint: str | None) -> str:
    if template_hint:
        return template_hint.strip().lower().replace('-', '_')
    seed_text = ' '.join((' '.join((str(value or '') for value in (seed.seed_id, seed.unit_type, seed.unit_id_hint))) for seed in seeds)).lower()
    has_mbr = 'mbr' in seed_text or 'completelymixedmbr' in seed_text
    has_post = 'post_anoxic' in seed_text or 'post' in seed_text or 'px' in seed_text
    aerobic_only = 'aerobic.zone.aerobic' in seed_text and 'anoxic' not in seed_text
    if has_mbr and has_post:
        return 'asm2d_mbr_post'
    if has_mbr and aerobic_only:
        return 'asm2d_mbr_aerobic'
    if has_mbr:
        return 'asm2d_mbr'
    if has_post:
        return 'asm2d_mle_post'
    return 'asm2d_mle'

def _stream_payload(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return {key: val for key, val in value.items() if val is not None}
    payload: Dict[str, Any] = {}
    for attr in ('stream_id', 'role', 'composition_hint'):
        if hasattr(value, attr):
            attr_value = getattr(value, attr)
            if attr_value is not None:
                payload[attr] = attr_value
    return payload

def _has_composition_hint(stream: Dict[str, Any]) -> bool:
    hint = stream.get('composition_hint')
    return isinstance(hint, dict) and bool(hint)

def _external_feed_override(external_streams: Optional[Dict[str, Dict[str, Any]]]) -> Dict[str, Any]:
    if not external_streams:
        return {}
    normalized = {key: _stream_payload(value) for key, value in external_streams.items()}
    for key in ('influent', 'feed', 'plant:feed', 'plant:influent'):
        if key in normalized and _has_composition_hint(normalized[key]):
            return dict(normalized[key])
    for stream in normalized.values():
        if _has_composition_hint(stream):
            return dict(stream)
    for key in ('influent', 'feed', 'plant:feed', 'plant:influent'):
        if key in normalized:
            return dict(normalized[key])
    return dict(next(iter(normalized.values()), {}))

def _concentrations_kg_m3(stream: Dict[str, Any]) -> Dict[str, float]:
    composition_hint = stream.get('composition_hint')
    if isinstance(composition_hint, dict):
        return {str(component): float(value) for component, value in composition_hint.items() if value is not None}
    concentrations = stream.get('concentrations') or stream.get('conc_mass_comp') or {}
    if not isinstance(concentrations, dict):
        return {}
    units = str(stream.get('concentration_units') or 'kg/m3').lower()
    factor = 1.0
    if units in {'mg/l', 'mg_l', 'g/m3', 'g_m3'}:
        factor = 0.001
    return {str(component): float(value) * factor for component, value in concentrations.items() if value is not None}

def _feed_temperature_k(feed: Dict[str, Any]) -> Optional[float]:
    """The feed temperature a seed or external stream states, in K: ``temperature_K``, else ``temperature_C``."""
    temperature_k = _first_float(feed, 'temperature_K')
    if temperature_k is not None:
        return temperature_k
    temperature_c = _first_float(feed, 'temperature_C')
    return None if temperature_c is None else temperature_c + 273.15

def _seed_influent_override(seeds: Sequence[SimulationSeed]) -> Dict[str, Any]:
    for seed in seeds:
        composition = seed.influent_composition
        if composition is None:
            continue
        return {'flow_m3_d': composition.flow_m3_d, 'temperature_K': composition.temperature_K, 'alkalinity_mol_per_m3': composition.alkalinity_mol_per_m3, 'composition_hint': dict(composition.species_kg_m3)}
    return {}
_ASM2D_PACKAGE_BY_ENGINE_KEY = {'WaterTAP-ASM2D': PropertyPackageType.ASM2D, 'qsdsan-ASM2D': PropertyPackageType.ASM2D, 'WaterTAP-ModifiedASM2D': PropertyPackageType.MODIFIED_ASM2D, 'qsdsan-ModifiedASM2D': PropertyPackageType.MODIFIED_ASM2D}

def _asm2d_property_package(seeds: Sequence[SimulationSeed]) -> PropertyPackageType:
    """The package the biological seeds' engine keys name: ModifiedASM2d seeds build ModifiedASM2d, never plain ASM2d;
    seeds naming both refuse by name; none naming either keep ASM2d."""
    named = {_ASM2D_PACKAGE_BY_ENGINE_KEY[seed.engine_key] for seed in seeds if _is_biological(seed) and seed.engine_key in _ASM2D_PACKAGE_BY_ENGINE_KEY}
    if len(named) > 1:
        raise ValueError(f'the biological seeds name both ASM2d and ModifiedASM2d engine keys ({sorted({seed.engine_key for seed in seeds if seed.engine_key in _ASM2D_PACKAGE_BY_ENGINE_KEY})}); one flowsheet builds one ASM2d model: state one')
    return next(iter(named), PropertyPackageType.ASM2D)

def _biological_config_from_seeds(seeds: Sequence[SimulationSeed], external_streams: Optional[Dict[str, Dict[str, Any]]], template_name: str) -> tuple[ASM2DBiologicalConfig, List[str]]:
    from watertap_engine_mcp.core.modified_asm2d_kinetics import SESSION_ROUTE_RULING_ID, ModifiedAsm2dKineticsRefused
    kwargs: Dict[str, Any] = {}
    warnings: List[str] = []
    property_package = _asm2d_property_package(seeds)
    if property_package != PropertyPackageType.ASM2D:
        kwargs['property_package'] = property_package
    seed_feed = _seed_influent_override(seeds)
    feed = {**seed_feed, **_external_feed_override(external_streams)}
    if feed:
        flow = _first_float(feed, 'flow_m3_d', 'flow_m3_per_day')
        if flow is not None:
            kwargs['feed_flow_m3_per_day'] = flow
        temperature = _feed_temperature_k(feed)
        if temperature is not None:
            kwargs['feed_temperature_K'] = temperature
    if property_package == PropertyPackageType.MODIFIED_ASM2D and 'feed_temperature_K' not in kwargs:
        raise ModifiedAsm2dKineticsRefused(f'modified_asm2d_kinetics_refused: the ModifiedASM2d seeds state no feed temperature (no seed influent_composition.temperature_K, no external-stream temperature_K or temperature_C), and a ModifiedASM2d feed fixes the temperature its kinetic set is taken at: state one ({SESSION_ROUTE_RULING_ID} G5)')
    if feed:
        pressure = _first_float(feed, 'pressure_Pa')
        if pressure is not None:
            kwargs['feed_pressure_Pa'] = pressure
        alkalinity = _first_float(feed, 'alkalinity_mol_per_m3', 'alkalinity')
        if alkalinity is not None:
            kwargs['alkalinity_mol_per_m3'] = alkalinity
        concentrations = _concentrations_kg_m3(feed)
        if concentrations:
            defaults = ASM2DBiologicalConfig().feed_conc_kg_m3
            kwargs['feed_conc_kg_m3'] = {**defaults, **concentrations}
    declared_recycles = set(declared_recycle_ratio_vars(template_name))

    def apply_recycle_ratio(seed: SimulationSeed, key: str, value: float) -> None:
        if key in declared_recycles:
            kwargs[key] = value
            return
        warnings.append(f"Ignored {key}={value} from seed '{seed.seed_id}' because template '{template_name}' does not declare that recycle variable.")
    for seed in seeds:
        values = _seed_values(seed)
        seed_id = seed.seed_id.lower()
        hint = (seed.unit_id_hint or '').lower()
        volume = _first_float(values, 'volume_m3', 'V_max', 'V', 'volume')
        is_separator_seed = any((marker in seed_id for marker in ('separation', 'separator', 'clarifier', 'mbr', 'sludge_splitter')))
        if volume is not None and (not is_separator_seed):
            if 'post_anoxic' in seed_id or hint.startswith('px'):
                kwargs['post_anoxic_volume_m3'] = volume
            elif 'pre_anoxic' in seed_id or 'anoxic' in seed_id or hint.startswith('ax'):
                kwargs['anoxic_volume_m3'] = volume
            elif 'aerobic' in seed_id or hint.startswith('ae'):
                kwargs['aerobic_volume_m3'] = volume
        target_srt = _first_float(values, 'target_srt_days', 'srt_d', 'srt_design_d')
        if target_srt is not None:
            kwargs['target_srt_days'] = target_srt
        target_mlss = _first_float(values, 'target_mlss_mg_l', 'mlss_mg_l', 'MLSS_mg_L')
        if target_mlss is not None:
            kwargs['target_mlss_mg_l'] = target_mlss
        kla = _first_float(values, 'kla_per_hr', 'KLa', 'kla')
        if kla is not None:
            kwargs['aerobic_kla_per_hr'] = kla
        ras_ratio = _first_float(values, 'ras_recycle_ratio', 'target_ras_recycle_ratio', 'return_activated_sludge_recycle_ratio', 'return_sludge_recycle_ratio')
        if ras_ratio is not None:
            apply_recycle_ratio(seed, 'ras_recycle_ratio', ras_ratio)
        mlr_ratio = _first_float(values, 'mlr_recycle_ratio', 'target_mlr_recycle_ratio', 'mixed_liquor_recycle_ratio', 'target_mixed_liquor_recycle_ratio', 'internal_recycle_ratio', 'target_internal_recycle_ratio')
        if mlr_ratio is not None:
            apply_recycle_ratio(seed, 'mlr_recycle_ratio', mlr_ratio)
        generic_recycle_ratio = _first_float(values, 'target_recycle_ratio', 'recycle_ratio')
        if generic_recycle_ratio is not None:
            routed_key = _route_generic_recycle_ratio(seed_id, hint)
            if routed_key is not None:
                apply_recycle_ratio(seed, routed_key, generic_recycle_ratio)
        fixed_ir = _first_float(values, 'fixed_internal_recycle_fraction')
        if fixed_ir is not None and 'internal_recycle' in seed_id:
            if 'mlr_recycle_ratio' in declared_recycles:
                kwargs['internal_recycle_fraction'] = max(0.0, min(0.99, fixed_ir))
            else:
                warnings.append(f"Ignored fixed_internal_recycle_fraction from seed '{seed.seed_id}' because template '{template_name}' has no MLR/internal recycle.")
        permeate_split = _first_float(values, 'permeate_split_fraction')
        if permeate_split is not None and 'mbr' in seed_id:
            kwargs['membrane_soluble_permeate_fraction'] = max(0.0001, min(0.9999, permeate_split))
        solids_capture = _first_float(values, 'solids_capture_rate')
        if solids_capture is not None and 'mbr' in seed_id:
            kwargs['membrane_particulate_permeate_fraction'] = max(1e-06, min(0.9999, 1.0 - solids_capture))
    return (ASM2DBiologicalConfig(**kwargs), warnings)

def _template_for_name(name: str, config: ASM2DBiologicalConfig):
    if name == 'asm2d_mbr_post':
        return ASM2DMBRMLEPOSTTemplate(config)
    if name == 'asm2d_mbr':
        return ASM2DMBRMLETemplate(config)
    if name == 'asm2d_mbr_aerobic':
        return ASM2DMBRAerobicTemplate(config)
    if name == 'asm2d_mle_post':
        return ASM2DMLEPOSTTemplate(config)
    if name == 'asm2d_mle':
        return ASM2DMLETemplate(config)
    raise ValueError(f'Unsupported biological WaterTAP template: {name}')

def _require_terminal_seed_topology(seed: SimulationSeed, *, inlet_roles: tuple[str, ...], outlet_roles: tuple[str, ...]) -> None:
    actual_inlets = tuple((port.role for port in seed.stream_topology.inlets))
    actual_outlets = tuple((port.role for port in seed.stream_topology.outlets))
    if actual_inlets != inlet_roles or actual_outlets != outlet_roles:
        raise ValueError(f'{seed.seed_id} executable topology must have inlet roles {inlet_roles!r} and outlet roles {outlet_roles!r}; received {actual_inlets!r} and {actual_outlets!r}')
    if not seed.stream_topology.inlets[0].stream_id.startswith('plant:'):
        raise ValueError(f'{seed.seed_id} executable feed must be an explicit plant boundary')
    connected = [port.stream_id for port in seed.stream_topology.inlets + seed.stream_topology.outlets if port.connects_to is not None]
    if connected:
        raise ValueError(f'{seed.seed_id} carries topology connections {connected!r}, but the generic non-biological compiler would ignore those arcs; use a supported explicit topology or vendor route.')

def _validated_ro_executable_basis(seed: SimulationSeed) -> _ROExecutableBasis:
    _require_terminal_seed_topology(seed, inlet_roles=('feed',), outlet_roles=('permeate', 'concentrate'))
    try:
        _ROEngineeringInputs.model_validate(seed.engineering_inputs)
        _ROConstructorBasis.model_validate(seed.constructor_kwargs)
        return _ROExecutableBasis.model_validate(seed.influent_component_hints)
    except ValidationError as exc:
        raise ValueError(f'{seed.seed_id} is not a complete typed single-stage RO seed: {exc}') from exc

def _validate_ix_executable_basis(seed: SimulationSeed) -> None:
    _require_terminal_seed_topology(seed, inlet_roles=('feed',), outlet_roles=('effluent', 'regen_waste'))
    try:
        _IXEngineeringInputs.model_validate(seed.engineering_inputs)
        _IXConstructorBasis.model_validate(seed.constructor_kwargs)
        _IXExecutableBasis.model_validate(seed.influent_component_hints)
    except ValidationError as exc:
        raise ValueError(f'{seed.seed_id} is not the typed certified sodium-form SAC WaterTAP profile: {exc}') from exc

def _non_bio_unit_config(seed: SimulationSeed) -> Dict[str, Any]:
    if seed.unit_type == 'ReverseOsmosis0D':
        cfg = _ROConstructorBasis.model_validate(seed.constructor_kwargs).model_dump()
        return {'has_pressure_change': cfg['has_pressure_change'], 'concentration_polarization_type': cfg['concentration_polarization_type'], 'mass_transfer_coefficient': cfg['mass_transfer_coefficient']}
    if seed.unit_type == 'IonExchange0D':
        return _IXConstructorBasis.model_validate(seed.constructor_kwargs).model_dump()
    return dict(seed.constructor_kwargs)

def _non_bio_property_package(seed: SimulationSeed) -> str:
    if seed.unit_type == 'IonExchange0D':
        return 'MCAS'
    if seed.unit_type == 'Evaporator':
        return 'NACL_T_DEP'
    if seed.unit_type == 'Crystallizer':
        return 'CRYST_NACL'
    return 'SEAWATER'

def _compile_non_bio_plan(seeds: Sequence[SimulationSeed]) -> WaterTAPSeedBuildPlan:
    first = seeds[0]
    ro_seeds = [seed for seed in seeds if seed.unit_type == 'ReverseOsmosis0D']
    ix_seeds = [seed for seed in seeds if seed.unit_type == 'IonExchange0D']
    if len(ro_seeds) > 1:
        raise ValueError('Multi-stage WaterTAP-RO seed compilation is unsupported: the current non-biological compiler cannot represent the serial concentrate arcs and interstage booster pumps.')
    if ro_seeds and len(seeds) != 1:
        raise ValueError('The executable WaterTAP-RO compiler accepts one standalone single-stage seed only; it cannot silently drop connections to other separation units.')
    if ix_seeds and len(seeds) != 1:
        raise ValueError('The executable WaterTAP-IX compiler accepts one standalone single-target SAC seed only; it cannot silently drop connections to other separation units.')
    units = []
    for seed in seeds:
        if seed.unit_type is None:
            continue
        if seed.unit_type == 'ReverseOsmosis0D':
            _validated_ro_executable_basis(seed)
        elif seed.unit_type == 'IonExchange0D':
            _validate_ix_executable_basis(seed)
        units.append({'unit_id': seed.unit_id_hint or seed.seed_id.replace('.', '_'), 'unit_type': seed.unit_type, 'config': _non_bio_unit_config(seed), 'engineering_inputs': dict(seed.engineering_inputs)})
    feed_states: Dict[str, Dict[str, Any]] = {}
    if len(ro_seeds) == 1:
        ro_seed = ro_seeds[0]
        basis = _validated_ro_executable_basis(ro_seed)
        flow_m3h = basis.feed_flow_m3h
        salinity_ppm = basis.feed_salinity_ppm
        pressure_bar = basis.feed_pressure_bar
        temperature_K = basis.feed_temperature_K
        flow_m3_s = flow_m3h / 3600.0
        tds_density_kg_m3 = salinity_ppm / 1000.0
        water_density_kg_m3 = max(1000.0 - tds_density_kg_m3, 0.0)
        feed_states[_seed_unit_id(ro_seed)] = {'state_args': {'flow_mass_phase_comp': {('Liq', 'H2O'): flow_m3_s * water_density_kg_m3, ('Liq', 'TDS'): flow_m3_s * tds_density_kg_m3}, 'temperature': temperature_K, 'pressure': pressure_bar * 100000.0}}
    return WaterTAPSeedBuildPlan(template_name=None, session_spec={'property_package': _non_bio_property_package(first), 'units': units, 'connections': [], 'dof_fixes': [], 'scaling_factors': [], 'feed_states': feed_states, 'process_constraints': [], 'initialization_order': [unit['unit_id'] for unit in units]}, seeds=list(seeds), warnings=['Single-stage WaterTAP-RO seed compiled with its explicit feed state; stage recovery remains a refinement result of pressure, area, and membrane transport.'] if ro_seeds else ['Generic WaterTAP seed compilation instantiated units only; use templates or explicit connections for multi-unit separation trains.'])
_GAS_ENGINE_KEY = 'WaterTAP-membrane.gas_separation'

def _is_gas_separation(seed: SimulationSeed) -> bool:
    return seed.engine_key == _GAS_ENGINE_KEY or seed.unit_type == 'GasMembraneStage1D'

def _seed_unit_id(seed: SimulationSeed) -> str:
    return seed.unit_id_hint or seed.seed_id.replace('.', '_')

def _gas_src_port(unit_type: str, role: str) -> str:
    """Map an outlet PortRole to the IDAES port name for a gas unit."""
    if unit_type == 'GasMembraneStage1D':
        if role == 'retentate':
            return 'feed_side_outlet'
        if role == 'permeate':
            return 'sweep_side_outlet'
        raise ValueError(f"GasMembraneStage1D has no outlet role '{role}'")
    return 'outlet'

def _compile_gas_plan(seeds: Sequence[SimulationSeed]) -> WaterTAPSeedBuildPlan:
    """Compile a CO2/CH4 gas-separation cascade (membrane stages + recycle) into a
    full WaterTAP session spec: units + connections (from connects_to) + recycle tear
    + gas feed-state, on the natural_gas_PR property package.

    Seed conventions (engine_key='WaterTAP-membrane.gas_separation'):
      - Feed boundary: unit_type='Feed', engineering_inputs carry the gas feed state
        (flow_mol_s OR flow_nm3_hr[+std_T_C/std_P_bar_abs], mole_frac, temperature_C,
        pressure_barg), and one outlet (role 'feed') connecting downstream.
      - Membrane stage: unit_type='GasMembraneStage1D', engineering_inputs carry
        permeance/area_cm2/length_cm/permeate_pressure_bar/finite_elements; one 'feed'
        inlet and 'retentate'/'permeate' outlets.
      - Mixer (recycle): unit_type='Mixer'. Recycle arc marked tear_stream=True.
      - Vent/Product: unit_type='Product'.
    """
    from watertap_engine_mcp.core.gas_state import GasState
    by_id: Dict[str, SimulationSeed] = {s.seed_id: s for s in seeds}
    warnings: List[str] = []
    units: List[Dict[str, Any]] = []
    feed_states: Dict[str, Dict[str, Any]] = {}
    components: set[str] = set()
    mixer_inlet_count: Dict[str, int] = {}
    mixer_ids: List[str] = []
    compressor_ids: List[str] = []
    feed_pressure_bar_abs: Optional[float] = None
    from watertap_engine_mcp.core.property_registry import PropertyPackageType as _PPT
    for seed in seeds:
        if seed.unit_type is None:
            continue
        unit_id = _seed_unit_id(seed)
        ei = dict(seed.engineering_inputs)
        config: Dict[str, Any] = {}
        if seed.unit_type == 'GasMembraneStage1D':
            perm = ei.get('permeance') or ei.get('permeance_gpu') or {}
            components.update(perm.keys())
            config.update({'finite_elements': int(ei.get('finite_elements', 5)), 'flow_type': ei.get('flow_type', 'COUNTERCURRENT'), 'sweep_flow': bool(ei.get('sweep_flow', False)), 'permeance': perm, 'area_cm2': ei.get('area_cm2') or ei.get('membrane_area_cm2'), 'length_cm': ei.get('length_cm'), 'permeate_pressure_bar': ei.get('permeate_pressure_bar')})
        elif seed.unit_type == 'Mixer':
            n_in = sum((1 for s2 in seeds for out in s2.stream_topology.outlets if out.connects_to and out.connects_to.downstream_seed_id == seed.seed_id))
            config['num_inlets'] = max(2, n_in)
            config['momentum_mixing_type'] = ei.get('momentum_mixing_type', 'none')
            mixer_ids.append(unit_id)
        elif seed.unit_type == 'Feed':
            feed_pressure_bar_abs = float(ei.get('pressure_barg', 0.0)) + 1.013
            mf = ei.get('mole_frac') or {}
            components.update(mf.keys())
            if ei.get('flow_mol_s') is not None:
                gs = GasState(flow_mol_s=float(ei['flow_mol_s']), mole_frac=dict(mf), temperature_C=float(ei.get('temperature_C', 25.0)), pressure_bar_abs=float(ei.get('pressure_barg', 0.0)) + 1.013)
            else:
                gs = GasState.from_normal_flow(float(ei['flow_nm3_hr']), dict(mf), temperature_C=float(ei.get('temperature_C', 25.0)), pressure_barg=float(ei.get('pressure_barg', 0.0)), std_T_C=float(ei.get('std_T_C', 0.0)), std_P_bar_abs=float(ei.get('std_P_bar_abs', 1.013)))
            feed_states[unit_id] = {'state_args': gs.to_state_args(_PPT.NATURAL_GAS_PR)}
        elif seed.unit_type == 'GasScrewCompressor':
            config.update({'swept_vol_m3_s': ei.get('swept_vol_m3_s'), 'capacity_slide': ei.get('capacity_slide', 1.0), 'volume_ratio_Vi': ei.get('volume_ratio_Vi'), 'k_isentropic': ei.get('k_isentropic'), 'cp_mol': ei.get('cp_mol'), 'eta_vol': ei.get('eta_vol'), 'eta_mech': ei.get('eta_mech'), 'eta_motor': ei.get('eta_motor'), 'f_oil': ei.get('f_oil'), 'discharge_pressure_bar': ei.get('discharge_pressure_bar'), 'saturated_capacity': bool(ei.get('saturated_capacity', False))})
            compressor_ids.append(unit_id)
        elif seed.unit_type == 'GasHeater':
            config.update({'has_pressure_change': bool(ei.get('has_pressure_change', False)), 'outlet_temperature_C': ei.get('outlet_temperature_C'), 'heat_duty_W': ei.get('heat_duty_W'), 'delta_p_bar': ei.get('delta_p_bar')})
        units.append({'unit_id': unit_id, 'unit_type': seed.unit_type, 'config': config, 'engineering_inputs': {}})
    connections: List[Dict[str, Any]] = []
    for seed in seeds:
        src_id = _seed_unit_id(seed)
        for out in seed.stream_topology.outlets:
            conn = out.connects_to
            if conn is None:
                continue
            dest_seed = by_id.get(conn.downstream_seed_id)
            if dest_seed is None:
                warnings.append(f"gas seed '{seed.seed_id}' outlet references unknown downstream '{conn.downstream_seed_id}'")
                continue
            dest_id = _seed_unit_id(dest_seed)
            src_port = _gas_src_port(seed.unit_type, out.role)
            if dest_seed.unit_type == 'Mixer':
                idx = mixer_inlet_count.get(dest_id, 0) + 1
                mixer_inlet_count[dest_id] = idx
                dest_port = f'inlet_{idx}'
            elif dest_seed.unit_type == 'GasMembraneStage1D':
                dest_port = 'feed_side_inlet'
            else:
                dest_port = 'inlet'
            connections.append({'src_unit': src_id, 'src_port': src_port, 'dest_unit': dest_id, 'dest_port': dest_port, 'stream_id': out.stream_id, 'tear_stream': bool(conn.tear_stream), 'tear_rationale': getattr(conn, 'tear_rationale', None)})
    comp_list = ','.join(sorted(components)) if components else 'CH4,CO2,N2'
    dof_fixes: List[Dict[str, Any]] = []
    if mixer_ids and feed_pressure_bar_abs is not None:
        for mid in mixer_ids:
            dof_fixes.append({'unit_id': mid, 'var_name': 'mixed_state[0.0].pressure', 'value': feed_pressure_bar_abs * 100000.0})
    session_spec = {'property_package': 'NATURAL_GAS_PR', 'property_package_config': {'components': comp_list}, 'units': units, 'connections': connections, 'dof_fixes': dof_fixes, 'scaling_factors': [], 'feed_states': feed_states, 'process_constraints': [], 'initialization_order': [u['unit_id'] for u in units], 'metadata': {'engine': 'watertap', 'family': 'gas_separation', 'gas_compressor_ids': compressor_ids, 'gas_saturated_capacity': any((bool(u['config'].get('saturated_capacity')) for u in units if u['unit_type'] == 'GasScrewCompressor'))}}
    return WaterTAPSeedBuildPlan(template_name='gas_separation', session_spec=session_spec, seeds=list(seeds), warnings=warnings)
_ASM2D_PRESET_CLASSES = {'asm2d_mle': ASM2DMLETemplate, 'asm2d_mle_post': ASM2DMLEPOSTTemplate, 'asm2d_mbr': ASM2DMBRMLETemplate, 'asm2d_mbr_aerobic': ASM2DMBRAerobicTemplate, 'asm2d_mbr_post': ASM2DMBRMLEPOSTTemplate}

def _asm2d_preset_name_from_topology(topology: RoleResolvedTopology) -> str:
    """Select the matching registered ASM2D preset name from the graph's roles.

    Role-driven (NOT name inference): an MBR-separator role selects the MBR
    family; a post-anoxic reactor role selects the post-anoxic variant.
    """
    has_mbr = topology.has_role(UnitRole.MBR_SEPARATOR)
    has_post = topology.has_role(UnitRole.POST_ANOXIC)
    if has_mbr and has_post:
        return 'asm2d_mbr_post'
    if has_mbr and (not topology.has_role(UnitRole.PRE_ANOXIC)):
        return 'asm2d_mbr_aerobic'
    if has_mbr:
        return 'asm2d_mbr'
    if has_post:
        return 'asm2d_mle_post'
    return 'asm2d_mle'

def _asm2d_provider_for_topology(topology: RoleResolvedTopology, config: ASM2DBiologicalConfig) -> Asm2dPresetProvider:
    """Build the ASM2D default provider for a designed role-tagged graph.

    The family *profile* (design bounds + intended family) is taken from the
    registered preset whose structural family the graph's roles match; the
    *config* is the seed-derived sizing. Constructing the provider normalizes the
    config in place exactly as the named-preset path does, so a designed graph and
    its named preset yield a role-identical default bundle.
    """
    preset_name = _asm2d_preset_name_from_topology(topology)
    profile = _ASM2D_PRESET_CLASSES[preset_name].family_profile()
    return Asm2dPresetProvider(profile, config)
_ADM1_PRESET_CLASSES = {'uasb': UASBTemplate, 'egsb': EGSBTemplate, 'anmbr': AnMBRTemplate, 'contact': AnaerobicContactTemplate, 'two_stage': TwoStageTemplate}
_ANMBR_OUTLET_NAMES = frozenset({'permeate', 'retentate'})
_CONTACT_OUTLET_NAMES = frozenset({'overflow', 'underflow', 'clarified'})

def _adm1_separator_outlet_names(graph: Dict[str, Any]) -> frozenset[str]:
    """Collect the candidate solids-separator outlet names from a designed graph.

    Reads both the per-unit ``config['outlet_list']`` (the Separator construction
    spec) and the connection ``src_port`` names — the AnMBR-vs-contact discriminating
    signal :func:`_adm1_family_from_topology` keys on. Lower-cased; the recycle/waste
    bleed names (shared by both families) are harmless because they match neither
    discriminating signature.
    """
    names: set[str] = set()
    for unit in graph.get('units', []) or []:
        config = unit.get('config') or {}
        for name in config.get('outlet_list') or []:
            if name:
                names.add(str(name).strip().lower())
    for conn in graph.get('connections', []) or []:
        port = conn.get('src_port')
        if port:
            names.add(str(port).strip().lower())
    return frozenset(names)

def _adm1_family_from_topology(topology: RoleResolvedTopology, *, separator_outlets: frozenset[str]=frozenset()) -> str:
    """Select the matching ADM1 digestion family from the graph's roles + outlets.

    Role-driven (NOT name inference), mirroring ``_asm2d_preset_name_from_topology``:
    an acidogenic reactor role selects the phase-separated two-stage cascade; a
    recuperation/return splitter WITHOUT a thickener selects the EGSB recirculation
    family; otherwise the single-reactor UASB baseline.

    AnMBR and anaerobic-contact BOTH carry the biomass-retaining thickener role plus
    a recuperation bleed splitter, so they are indistinguishable from the role tags
    alone. They are discriminated by the solids-separator OUTLET names
    (``separator_outlets``, gathered via :func:`_adm1_separator_outlet_names`): a
    ``permeate``/``retentate`` membrane split is AnMBR; an ``overflow``/``underflow``
    gravity split is anaerobic-contact. When the outlet names carry neither signature
    (e.g. a bare role-only topology with no separator config), the contact profile is
    the conservative default — its clarified/recycle outlet names are used and the
    recycle solve is stiff (``infeasible_structured``) for either family.
    """
    if topology.has_role(UnitRole.ANAEROBIC_ACIDOGENIC):
        return 'two_stage'
    if topology.has_role(UnitRole.ANAEROBIC_THICKENER):
        names = {str(name).strip().lower() for name in separator_outlets}
        if names & _ANMBR_OUTLET_NAMES:
            return 'anmbr'
        if names & _CONTACT_OUTLET_NAMES:
            return 'contact'
        return 'contact'
    if topology.has_role(UnitRole.ANAEROBIC_RECUPERATION):
        return 'egsb'
    return 'uasb'

def _adm1_provider_for_topology(topology: RoleResolvedTopology, template_config: Optional[Dict[str, Any]], *, separator_outlets: frozenset[str]=frozenset()) -> Adm1PresetProvider:
    """Build the ADM1 default provider for a designed role-tagged anaerobic graph.

    The family *profile* (gas/recuperation/solids/acidogenic intent + outlet names)
    is taken from the registered preset whose digestion family the graph's roles
    (and, for AnMBR-vs-contact, its solids-separator ``separator_outlets``) match;
    the *config* is the family-correct config class (UASB/EGSB/AnMBR/contact/
    two-stage) built from the caller's optional overrides. The same
    ``Adm1PresetProvider`` defaults a template-built topology (canonical names) and
    this from-graph build (project names) to a role-identical bundle — exactly the
    topology-source-agnostic contract the ASM2D path establishes.
    """
    family = _adm1_family_from_topology(topology, separator_outlets=separator_outlets)
    preset_class = _ADM1_PRESET_CLASSES[family]
    config_kwargs = _coerce_config_kwargs(preset_class.config_class, dict(template_config or {}))
    config = preset_class.config_class(**config_kwargs)
    profile = preset_class.family_profile()
    return Adm1PresetProvider(profile, config)

def _nearest_adm1_preset(present_roles: set[str]) -> tuple[str, list[str], list[str]]:
    """Return ``(nearest_label, required_roles, missing_roles)`` for an anaerobic
    diff — the ADM1 analog of :func:`_nearest_asm2d_preset`.

    Compares the present role set against the minimal digestion role sets
    ``has_adm1_digestion_roles`` checks (single-reactor UASB vs phase-separated
    two-stage), returning the closest one plus the roles it is missing. Surfaced
    when a graph carries anaerobic roles but does not (yet) form a complete
    digestion family.
    """
    candidates = {'adm1_two_stage': {role.value for role in ADM1_TWO_STAGE_REQUIRED_ROLES}, 'adm1_uasb': {role.value for role in ADM1_SINGLE_STAGE_REQUIRED_ROLES}}
    best: tuple[str, set[str], set[str]] | None = None
    for label, required in candidates.items():
        missing = required - present_roles
        if best is None or len(missing) < len(best[2]):
            best = (label, required, missing)
    assert best is not None
    return (best[0], sorted(best[1]), sorted(best[2]))
_ANAEROBIC_ROLE_VALUES = frozenset({UnitRole.ANAEROBIC_ACIDOGENIC.value, UnitRole.ANAEROBIC_METHANOGENIC.value, UnitRole.ANAEROBIC_GAS_PHASE.value, UnitRole.ANAEROBIC_RECUPERATION.value, UnitRole.ANAEROBIC_THICKENER.value})

def _incomplete_adm1_payload(seeds: Sequence[SimulationSeed]) -> Dict[str, Any]:
    """Build the structured incomplete-ADM1 diff for a raw anaerobic seed list.

    The seed-list analog of :func:`validate_role_graph`'s anaerobic branch: an
    anaerobic seed list that does NOT form a complete ADM1 digestion family is
    reported as a diff against the nearest digestion preset (present roles, nearest
    preset, missing roles) rather than silently routed to the generic non-bio
    compile. Roles are read off the seeds (a raw anaerobic emission that carries no
    structural role tag at all reports an empty present-role set, which still diffs
    cleanly against the single-stage UASB minimum).
    """
    present = sorted({role for seed in seeds if (role := _seed_structural_role(seed)) is not None})
    nearest_label, required, missing = _nearest_adm1_preset(set(present))
    return {'code': 'incomplete_adm1_seed_graph', 'status': 'infeasible_structured', 'message': f"Anaerobic (ADM1/mADM1) simulation seeds are present but do NOT form a complete digestion family. Nearest preset '{nearest_label}' is missing roles: {missing}. Supply the missing role-tagged anaerobic units (or route this train to the named adm1_* preset explicitly); the incomplete ADM1 seed graph is NOT compiled as a generic non-biological train.", 'present_roles': present, 'nearest_preset': nearest_label, 'required_roles': required, 'missing_roles': missing}

def _nearest_asm2d_preset(present_roles: set[str]) -> tuple[str, list[str], list[str]]:
    """Return ``(nearest_label, required_roles, missing_roles)`` for a diff.

    Compares the present role set against the minimal required role sets the
    ASM2D recycle-family classifier checks, and returns the closest one plus the
    roles it is missing. This is the diff/oracle a failed designed graph surfaces
    instead of a silent preset substitution.
    """
    candidates = {'asm2d_mbr_retentate_recycle': {role.value for role in ASM2D_MBR_RETENTATE_RECYCLE_REQUIRED_ROLES}, 'asm2d_clarifier_recycle': {role.value for role in ASM2D_MLE_RECYCLE_REQUIRED_ROLES} | {UnitRole.SECONDARY_CLARIFIER.value}, 'asm2d_mbr_separator_recycle': {role.value for role in ASM2D_MLE_RECYCLE_REQUIRED_ROLES} | {UnitRole.MBR_SEPARATOR.value}}
    best: tuple[str, set[str], set[str]] | None = None
    for label, required in candidates.items():
        missing = required - present_roles
        if best is None or len(missing) < len(best[2]):
            best = (label, required, missing)
    assert best is not None
    return (best[0], sorted(best[1]), sorted(best[2]))

def validate_role_graph(graph: Dict[str, Any]) -> str:
    """Validate a role-tagged designed graph as a complete biological family.

    A graph is valid when its role tags form EITHER a complete ASM2D biological
    recycle family OR a complete ADM1/mADM1 anaerobic digestion family — the same
    canonical rule (:func:`RoleResolver.designed_graph_family`) the Stage-7b
    dispatch routes on. Returns the resolved family (``"asm2d"`` /``"adm1"``) so the
    compiler can confirm it matches the provider it was handed.

    Raises :class:`RoleGraphValidationError` (carrying a diff against the nearest
    registered preset: present roles, nearest preset, missing roles) when the graph
    forms neither family. This is the no-silent-substitution gate: a role-tagged
    graph that fails validation is reported as a structured diff, never quietly
    replaced by a preset topology. The diff is anchored to the family the present
    roles most resemble — an anaerobic-tagged graph diffs against the nearest ADM1
    digestion preset, everything else against the nearest ASM2D preset.
    """
    units = graph.get('units') or []
    roles: Dict[str, Optional[str]] = {}
    for unit in units:
        unit_id = unit.get('unit_id') or unit.get('id')
        if not unit_id:
            raise RoleGraphValidationError({'code': 'designed_graph_unit_missing_id', 'status': 'infeasible_structured', 'message': 'Designed role graph has a unit without a unit_id.', 'unit': unit})
        roles[str(unit_id)] = normalize_role(unit.get('role'))
    resolver = RoleResolver(roles)
    family = resolver.designed_graph_family()
    if family is not None:
        return family
    present = sorted({role for role in roles.values() if role})
    if _ANAEROBIC_ROLE_VALUES & set(present):
        nearest_label, required, missing = _nearest_adm1_preset(set(present))
        family_label = 'ADM1 anaerobic digestion'
    else:
        nearest_label, required, missing = _nearest_asm2d_preset(set(present))
        family_label = 'ASM2D biological recycle'
    raise RoleGraphValidationError({'code': 'designed_graph_role_validation_failed', 'status': 'infeasible_structured', 'message': f"Designed role-tagged graph does not form a complete {family_label} family. Nearest preset '{nearest_label}' is missing roles: {missing}. Supply the missing role-tagged units or route this tender to the named preset explicitly; the graph is NOT silently replaced by a preset.", 'present_roles': present, 'nearest_preset': nearest_label, 'required_roles': required, 'missing_roles': missing})

def compile_role_graph(*, graph: Dict[str, Any], provider: DefaultProvider, topology_source: str=TOPOLOGY_SOURCE_DESIGNED_GRAPH, seeds: Optional[Sequence[SimulationSeed]]=None, warnings: Optional[Sequence[str]]=None) -> WaterTAPSeedBuildPlan:
    """Compile a role-tagged designed graph + provider into a session spec.

    ``graph`` carries the structural topology only: ``property_package``,
    ``units`` (each ``{unit_id, unit_type, config, role, role_stage_index?,
    role_group?}``), and ``connections`` (``{src_unit, src_port, dest_unit,
    dest_port, tear_stream?}``). The provider reconstructs every solver default
    (DOF fixes, scaling, feed, constraints, solver metadata, init order) from the
    role graph. No template instance is involved.
    """
    validate_role_graph(graph)
    units = list(graph['units'])
    topology = RoleResolvedTopology.from_unit_specs(units)
    bundle = provider.provide(topology)
    metadata = {**dict(bundle.metadata), 'topology_source': topology_source}
    session_spec = {'property_package': graph['property_package'], 'units': units, 'connections': list(graph.get('connections', [])), 'dof_fixes': bundle.dof_fixes, 'scaling_factors': bundle.scaling_factors, 'feed_states': bundle.feed_states, 'process_constraints': bundle.process_constraints, 'design_parameters': bundle.design_parameters, 'metadata': metadata, 'solver_options': bundle.solver_options, 'initialization_order': bundle.initialization_order, 'topology_source': topology_source}
    return WaterTAPSeedBuildPlan(template_name=None, session_spec=session_spec, seeds=list(seeds or []), warnings=list(warnings or []), topology_source=topology_source)

def session_spec_to_build_topology(session_spec: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a flat session spec into a ``build_from_topology`` topology dict.

    Folds the flat ``dof_fixes`` / ``scaling_factors`` / ``feed_states`` onto the
    per-unit ``fixed_vars`` / ``scaling_factors`` / ``feed_state`` and rewrites
    ``src/dest`` connections into the ``from/to`` shape, preserving the role tags
    and recycle ``tear_stream`` flags so the built session keeps its roles (the
    near-feasible gate + role-driven initializer key on them).
    """
    fixed_by_unit: Dict[str, Dict[str, Any]] = {}
    scale_by_unit: Dict[str, Dict[str, Any]] = {}
    for fix in session_spec.get('dof_fixes', []):
        fixed_by_unit.setdefault(fix['unit_id'], {})[fix['var_name']] = fix['value']
    for scale in session_spec.get('scaling_factors', []):
        scale_by_unit.setdefault(scale['unit_id'], {})[scale['var_name']] = scale['factor']
    feed_states = session_spec.get('feed_states', {}) or {}
    topo_units = []
    for unit in session_spec.get('units', []):
        unit_id = unit.get('unit_id') or unit.get('id')
        topo_units.append({'unit_id': unit_id, 'unit_type': unit.get('unit_type') or unit.get('type'), 'config': unit.get('config', {}), 'role': unit.get('role'), 'role_stage_index': unit.get('role_stage_index'), 'role_group': unit.get('role_group'), 'fixed_vars': fixed_by_unit.get(unit_id, {}), 'scaling_factors': scale_by_unit.get(unit_id, {}), 'feed_state': feed_states.get(unit_id)})
    topo_conns = []
    for conn in session_spec.get('connections', []):
        topo_conns.append({'from': {'unit': conn['src_unit'], 'port': conn['src_port']}, 'to': {'unit': conn['dest_unit'], 'port': conn['dest_port']}, 'tear_stream': bool(conn.get('tear_stream', False)), 'tear_rationale': conn.get('tear_rationale')})
    metadata = dict(session_spec.get('metadata', {}))
    metadata.setdefault('topology_source', session_spec.get('topology_source'))
    metadata['design_parameters'] = session_spec.get('design_parameters', {})
    if isinstance(session_spec.get('solver_options'), dict):
        metadata['solver_options'] = session_spec['solver_options']
    return {'property_package': session_spec['property_package'], 'property_package_config': session_spec.get('property_package_config'), 'units': topo_units, 'connections': topo_conns, 'process_constraints': session_spec.get('process_constraints', []), 'metadata': metadata}
_STRUCTURAL_ROLE_KEYS = ('structural_role', 'unit_role')

def _seed_structural_role(seed: SimulationSeed) -> Optional[str]:
    """Return the structural :class:`UnitRole` tag a seed carries, if any.

    A designed seed graph carries its structural role on each seed's
    ``engineering_inputs`` / ``constructor_kwargs`` (key ``structural_role`` or
    ``unit_role``); the legacy partial aerobic-emitter seeds carry none, so they
    route to the preset-fallback path.
    """
    for source in (seed.engineering_inputs, seed.constructor_kwargs):
        for key in _STRUCTURAL_ROLE_KEYS:
            value = source.get(key)
            if value:
                return normalize_role(value)
    return None

def _designed_role_graph_family(seeds: Sequence[SimulationSeed]) -> Optional[str]:
    """Return which COMPLETE designed family the seed graph forms, or ``None``.

    Two conditions, both required:

    1. **Designed-emission entry condition** — at least one biological seed (of
       EITHER family: ASM2D aerobic or ADM1/mADM1 anaerobic), and EVERY biological
       seed carries an explicit structural role. A partial/legacy emission (reactor
       + separator markers only, or a mix of tagged + untagged bio seeds) fails here
       and routes to the preset path.
    2. **Full role-family completeness** — the full structural-role set (across ALL
       seeds, including the non-biological feed/mixer/splitter scaffolding) forms a
       complete family under the SAME rule the engine enforces at build time,
       :func:`RoleResolver.designed_graph_family` (the canonical leaf shared with the
       Stage-7b dispatch and ``validate_role_graph``).

    Returns ``"asm2d"`` / ``"adm1"`` for a complete family (the builder selects the
    family config + provider from it) or ``None`` for an incomplete / non-bio graph
    that falls back to the preset path.
    """
    bio = [seed for seed in seeds if _seed_bio_family(seed) is not None]
    if not bio:
        return None
    if any((_seed_structural_role(seed) is None for seed in bio)):
        return None
    roles = {_seed_unit_id(seed): _seed_structural_role(seed) for seed in seeds}
    return RoleResolver(roles).designed_graph_family()

def _seeds_form_designed_role_graph(seeds: Sequence[SimulationSeed]) -> bool:
    """True when the seeds form a COMPLETE role-tagged designed graph of either
    biological family (ASM2D recycle or ADM1 digestion).

    Thin boolean over :func:`_designed_role_graph_family`; the routing in
    :func:`build_plan_from_simulation_seeds` keys on the family string, while this
    predicate preserves the original yes/no contract.

    Routing on the entry condition alone (the prior behavior) let a fully-tagged but
    INCOMPLETE graph reach the designed-graph build, where ``validate_role_graph``
    then raised ``RoleGraphValidationError``. Keying the route on the canonical
    completeness classifier means such a graph falls back to the preset path
    (unchanged behavior) instead of routing to a build that is destined to fail.
    """
    return _designed_role_graph_family(seeds) is not None

def _watertap_src_port(unit_type: str, role: Optional[str], stream_role: str) -> str:
    """Map a seed outlet (unit_type + structural role + stream PortRole) to its
    WaterTAP source port name."""
    if role == UnitRole.INTERNAL_RECYCLE_SPLITTER.value:
        return 'internal_recycle' if stream_role in {'return', 'makeup'} else 'forward'
    if role == UnitRole.SLUDGE_SPLITTER.value:
        return 'recycle' if stream_role in {'return'} else 'waste'
    if unit_type == 'MBRSeparator' or role == UnitRole.MBR_SEPARATOR.value:
        return 'permeate' if stream_role in {'permeate', 'effluent'} else 'retentate'
    if unit_type == 'SecondaryClarifierASM' or role == UnitRole.SECONDARY_CLARIFIER.value:
        return 'effluent' if stream_role in {'effluent', 'permeate'} else 'underflow'
    return 'outlet'

def _watertap_dest_port(dst_unit_type: str, dst_role: Optional[str], src_role: Optional[str], inlet_role: str) -> str:
    """Map a seed inlet (downstream unit + the upstream structural role) to its
    WaterTAP dest port name."""
    if dst_role == UnitRole.MIXER.value or dst_unit_type == 'Mixer':
        if src_role == UnitRole.FEED.value:
            return 'feed_water'
        if src_role == UnitRole.INTERNAL_RECYCLE_PRESSURE.value:
            return 'internal_recycle'
        if src_role == UnitRole.RAS_PRESSURE.value:
            return 'ras'
        if inlet_role in {'feed'}:
            return 'feed_water'
        return 'ras'
    return 'inlet'

def _adm1_watertap_src_port(unit_type: str, role: Optional[str], stream_role: str) -> str:
    """Map an ADM1 / mADM1 seed outlet to its WaterTAP source port name.

    The anaerobic digester (``AD``) carries TWO outlets — the liquid effluent
    (``liquid_outlet``) and the biogas headspace take-off (``vapor_outlet``) — so
    the gas/biogas stream PortRole selects the vapor outlet and everything else the
    liquid outlet. The recuperation/return splitter and the biomass-retaining
    thickener resolve their recycle/clarified vs waste/underflow outlets from the
    stream PortRole (the recycle families honestly return ``infeasible_structured``
    from the stiff recycle solve regardless of the exact split outcome).
    """
    if stream_role in {'gas', 'biogas', 'vapor', 'vapor_outlet'}:
        return 'vapor_outlet'
    if unit_type == 'AD' or role in {UnitRole.ANAEROBIC_METHANOGENIC.value, UnitRole.ANAEROBIC_ACIDOGENIC.value}:
        return 'liquid_outlet'
    if role == UnitRole.ANAEROBIC_RECUPERATION.value:
        if stream_role == 'recirculation':
            return 'recirculation'
        if stream_role == 'forward':
            return 'forward'
        return 'recycle' if stream_role in {'return', 'recycle'} else 'waste'
    if role == UnitRole.ANAEROBIC_THICKENER.value:
        sr = (stream_role or '').lower()
        if sr in {'permeate', 'retentate'}:
            return sr
        if sr in {'overflow', 'underflow'}:
            return sr
        return 'overflow' if sr in {'clarified', 'effluent'} else 'underflow'
    return 'outlet'

def _adm1_watertap_dest_port(dst_unit_type: str, dst_role: Optional[str], src_role: Optional[str], inlet_role: str) -> str:
    """Map an ADM1 / mADM1 seed inlet to its WaterTAP dest port name.

    The recycle-family Mixer blends the fresh feed (``feed_water``) with the
    returned liquid recycle (``recycle``); a returned stream from the recuperation
    splitter or thickener — or any inlet PortRole marked return/recycle — lands on
    the recycle inlet, everything else on the fresh-feed inlet. Every other
    anaerobic unit (digester, biogas/effluent Product) takes its single ``inlet``.
    """
    if dst_role == UnitRole.MIXER.value or dst_unit_type == 'Mixer':
        if src_role in {UnitRole.ANAEROBIC_RECUPERATION.value, UnitRole.ANAEROBIC_THICKENER.value} or inlet_role in {'return', 'recycle', 'recirculation'}:
            return 'recycle'
        return 'feed_water'
    return 'inlet'

def _seed_unit_role_topology(seeds: Sequence[SimulationSeed], *, property_package: str, src_port_fn=_watertap_src_port, dest_port_fn=_watertap_dest_port) -> Dict[str, Any]:
    """Build a role-tagged graph dict from a designed seed list via engine_dag.

    Units carry the seed's structural role + WaterTAP unit_type + filtered
    config; connections are the resolved seed-to-seed arcs (including recycle
    ``tear_stream``) mapped to WaterTAP ports from the role + stream-role. The
    port mappers default to the ASM2D aerobic mapping; the anaerobic-digestion
    path passes the ADM1 mappers (``_adm1_watertap_src_port`` /
    ``_adm1_watertap_dest_port``) for the digester's liquid/vapor outlets.
    """
    seed_by_id = {seed.seed_id: seed for seed in seeds}
    role_by_id: Dict[str, Optional[str]] = {seed.seed_id: _seed_structural_role(seed) for seed in seeds}
    unit_type_by_id = {seed.seed_id: seed.unit_type or seed.watertap_class or 'Feed' for seed in seeds}
    units: List[Dict[str, Any]] = []
    for seed in seeds:
        unit_id = _seed_unit_id(seed)
        ei = dict(seed.engineering_inputs)
        config = {key: value for key, value in seed.constructor_kwargs.items() if key not in _STRUCTURAL_ROLE_KEYS and key != 'role_stage_index'}
        units.append({'unit_id': unit_id, 'unit_type': unit_type_by_id[seed.seed_id], 'config': config, 'role': role_by_id[seed.seed_id], 'role_stage_index': ei.get('role_stage_index'), 'role_group': ei.get('role_group')})
    connections: List[Dict[str, Any]] = []
    resolved: tuple[ResolvedSeedConnection, ...] = resolve_seed_connections(seeds)
    for edge in resolved:
        src_seed = seed_by_id[edge.source_seed_id]
        dst_seed = seed_by_id[edge.sink_seed_id]
        src_role = role_by_id[edge.source_seed_id]
        dst_role = role_by_id[edge.sink_seed_id]
        outlet_role = next((out.role for out in src_seed.stream_topology.outlets if out.stream_id == edge.stream_id), 'effluent')
        connections.append({'src_unit': _seed_unit_id(src_seed), 'src_port': src_port_fn(unit_type_by_id[edge.source_seed_id], src_role, outlet_role), 'dest_unit': _seed_unit_id(dst_seed), 'dest_port': dest_port_fn(unit_type_by_id[edge.sink_seed_id], dst_role, src_role, edge.downstream_inlet_role), 'stream_id': edge.stream_id, 'tear_stream': bool(edge.tear_stream), 'tear_rationale': edge.tear_rationale})
    return {'property_package': property_package, 'units': units, 'connections': connections}

def _compile_designed_biological_plan(seeds: Sequence[SimulationSeed], external_streams: Optional[Dict[str, Dict[str, Any]]]) -> WaterTAPSeedBuildPlan:
    """Compile a designed role-tagged biological seed graph (A-build route)."""
    bio_seeds = [seed for seed in seeds if _is_biological(seed)]
    roles = {_seed_unit_id(seed): _seed_structural_role(seed) for seed in bio_seeds}
    role_topology = RoleResolvedTopology.from_unit_specs([{'unit_id': uid, 'unit_type': None, 'role': role} for uid, role in roles.items()])
    preset_name = _asm2d_preset_name_from_topology(role_topology)
    config, warnings = _biological_config_from_seeds(seeds, external_streams, preset_name)
    property_package = config.property_package.name
    graph = _seed_unit_role_topology(seeds, property_package=property_package)
    topology = RoleResolvedTopology.from_unit_specs(graph['units'])
    provider = _asm2d_provider_for_topology(topology, config)
    return compile_role_graph(graph=graph, provider=provider, topology_source=TOPOLOGY_SOURCE_DESIGNED_GRAPH, seeds=seeds, warnings=warnings)

def _adm1_seed_separator_outlet_names(seeds: Sequence[SimulationSeed]) -> frozenset[str]:
    """Collect the thickener-role seeds' outlet PortRoles — the AnMBR-vs-contact
    discriminator read off the RAW seeds (independent of port-mapper normalization).

    An AnMBR membrane seed carries ``permeate``/``retentate`` outlet roles; an
    anaerobic-contact thickener seed carries ``overflow``/``underflow``. Gathering
    them from the seeds (not the built graph) lets the family be selected before the
    graph's ports are mapped.
    """
    names: set[str] = set()
    for seed in seeds:
        if _seed_structural_role(seed) != UnitRole.ANAEROBIC_THICKENER.value:
            continue
        for outlet in seed.stream_topology.outlets:
            if outlet.role:
                names.add(str(outlet.role).strip().lower())
    return frozenset(names)

def _adm1_config_from_seeds(seeds: Sequence[SimulationSeed], external_streams: Optional[Dict[str, Dict[str, Any]]], config_class: type) -> Dict[str, Any]:
    """Build the family config overrides from the ADM1 seed list + external streams.

    The ADM1 analog of :func:`_biological_config_from_seeds`: threads the
    seed-derived feed boundary (flow / temperature / pressure / composition, with an
    ``external_streams`` feed override taking precedence) and the digester geometry
    (the AD seed's liquid/vapor volumes) into the family config so the designed build
    carries PRODUCTION basis instead of falling back to the ADM1 defaults. The result
    is filtered to the family config-class fields (UASB/EGSB/AnMBR/contact/two-stage
    configs differ), so an extra seed field never breaks the dataclass construction.
    """
    cfg: Dict[str, Any] = {}
    seed_feed = _seed_influent_override(seeds)
    feed = {**seed_feed, **_external_feed_override(external_streams)}
    if feed:
        flow = _first_float(feed, 'flow_m3_d', 'flow_m3_per_day')
        if flow is not None:
            cfg['feed_flow_m3_per_day'] = flow
        temperature = _feed_temperature_k(feed)
        if temperature is not None:
            cfg['feed_temperature_K'] = temperature
        pressure = _first_float(feed, 'pressure_Pa')
        if pressure is not None:
            cfg['feed_pressure_Pa'] = pressure
        concentrations = _concentrations_kg_m3(feed)
        if concentrations:
            cfg['feed_conc_kg_m3'] = concentrations
    for seed in seeds:
        if (seed.unit_type or '').upper() != 'AD':
            continue
        prefix = 'acid_' if _seed_structural_role(seed) == UnitRole.ANAEROBIC_ACIDOGENIC.value else ''
        values = _seed_values(seed)
        volume_liquid = _first_float(values, 'volume_liquid_m3', 'volume_liquid', 'V_liquid', 'volume_m3', 'V')
        if volume_liquid is not None:
            cfg[prefix + 'volume_liquid_m3'] = volume_liquid
        volume_vapor = _first_float(values, 'volume_vapor_m3', 'volume_vapor', 'V_vapor')
        if volume_vapor is not None:
            cfg[prefix + 'volume_vapor_m3'] = volume_vapor
        headspace = _first_float(values, 'headspace_pressure_Pa', 'headspace_pressure')
        if headspace is not None and (not prefix):
            cfg['headspace_pressure_Pa'] = headspace
    allowed = set(getattr(config_class, '__dataclass_fields__', {}))
    return {key: value for key, value in cfg.items() if key in allowed}

def _stamp_adm1_unit_configs(graph: Dict[str, Any], *, liquid_property_package: str) -> None:
    """Stamp the structural per-unit config a RAW ADM1 seed list omits, in place.

    A designed ADM1 seed list's units carry only ``constructor_kwargs`` (often
    empty), so the graph's AD / biogas-Product / recycle-Mixer / solids-Separator
    units lack the phase-package + structural config the ADM1 templates bake in. The
    model builder defaults the AD liquid/vapor packages, but NOT the biogas Product's
    vapor package, the recycle Mixer's pressure-free mixing, or a Separator's outlet
    list — so a raw seed-list build would mis-package the biogas sink and fail to
    square. This stamps exactly those configs (without overriding any seed-provided
    value) so the seed-list build matches the template build:

    * ``AD`` — liquid + ADM1-vapor property packages.
    * biogas Product (``anaerobic_gas_phase`` role) — the ADM1 vapor package.
    * recycle ``Mixer`` — ``feed_water``/``recycle`` inlets, ``momentum_mixing='none'``.
    * solids/recuperation ``Separator`` — the resolved outlet names + split basis
      (component-flow for the biomass-retaining thickener, total-flow for the
      recuperation/bleed splitter).
    """
    out_ports_by_unit: Dict[str, List[str]] = {}
    for conn in graph.get('connections', []) or []:
        src = conn.get('src_unit')
        port = conn.get('src_port')
        if not src or not port:
            continue
        ports = out_ports_by_unit.setdefault(src, [])
        if port not in ports:
            ports.append(port)
    for unit in graph.get('units', []) or []:
        config = dict(unit.get('config') or {})
        unit_type = unit.get('unit_type')
        role = unit.get('role')
        if unit_type == 'AD':
            config.setdefault('liquid_property_package', liquid_property_package)
            config.setdefault('vapor_property_package', _ADM1_VAPOR_PACKAGE)
        elif role == UnitRole.ANAEROBIC_GAS_PHASE.value:
            config.setdefault('property_package', _ADM1_VAPOR_PACKAGE)
        elif role == UnitRole.MIXER.value or unit_type == 'Mixer':
            config.setdefault('inlet_list', ['feed_water', 'recycle'])
            config.setdefault('momentum_mixing_type', 'none')
        elif unit_type == 'Separator':
            outlets = out_ports_by_unit.get(unit.get('unit_id'))
            if outlets and 'outlet_list' not in config:
                config['outlet_list'] = list(outlets)
            if role == UnitRole.ANAEROBIC_THICKENER.value:
                config.setdefault('split_basis', 'componentFlow')
            else:
                config.setdefault('split_basis', 'totalFlow')
        unit['config'] = config

def _compile_designed_adm1_plan(seeds: Sequence[SimulationSeed], external_streams: Optional[Dict[str, Dict[str, Any]]]) -> WaterTAPSeedBuildPlan:
    """Compile a designed role-tagged ADM1 / mADM1 anaerobic seed graph.

    The ADM1 analog of :func:`_compile_designed_biological_plan`: the seed graph IS
    the topology (engine_dag resolves the digester liquid/vapor + recycle arcs via
    the ADM1 port mappers), and the ``Adm1PresetProvider`` for the matching
    digestion family reconstructs the full default bundle (geometry fixes, the
    strong-ion feed boundary, the headspace-pressure design, scaling, the honest
    steady-state credibility) from the role graph. The seed-derived feed + digester
    geometry (plus an ``external_streams`` feed override) are threaded into the family
    config so the build carries production basis, and the per-unit phase-package /
    structural configs the raw seeds omit are stamped so the graph builds square. No
    template instance is in the loop; the plan is tagged
    ``topology_source="designed_graph"``.
    """
    roles = {_seed_unit_id(seed): _seed_structural_role(seed) for seed in seeds}
    role_topology = RoleResolvedTopology.from_unit_specs([{'unit_id': uid, 'unit_type': None, 'role': role} for uid, role in roles.items()])
    seed_outlets = _adm1_seed_separator_outlet_names(seeds)
    family = _adm1_family_from_topology(role_topology, separator_outlets=seed_outlets)
    preset_class = _ADM1_PRESET_CLASSES[family]
    property_package = preset_class.config_class().property_package.name
    template_config = _adm1_config_from_seeds(seeds, external_streams, preset_class.config_class)
    graph = _seed_unit_role_topology(seeds, property_package=property_package, src_port_fn=_adm1_watertap_src_port, dest_port_fn=_adm1_watertap_dest_port)
    _stamp_adm1_unit_configs(graph, liquid_property_package=property_package)
    topology = RoleResolvedTopology.from_unit_specs(graph['units'])
    provider = _adm1_provider_for_topology(topology, template_config, separator_outlets=_adm1_separator_outlet_names(graph) | seed_outlets)
    return compile_role_graph(graph=graph, provider=provider, topology_source=TOPOLOGY_SOURCE_DESIGNED_GRAPH, seeds=seeds, warnings=[])

def build_plan_from_simulation_seeds(simulation_seeds: Sequence[Dict[str, Any]], *, template_hint: str | None=None, external_streams: Optional[Dict[str, Dict[str, Any]]]=None) -> WaterTAPSeedBuildPlan:
    """Compile simulation_seed/v2 payloads into a WaterTAP session specification."""
    seeds = _parse_seeds(simulation_seeds)
    if any((_is_gas_separation(seed) for seed in seeds)):
        return _compile_gas_plan(seeds)
    has_asm2d = any((_is_biological(seed) for seed in seeds))
    has_adm1 = any((_is_anaerobic(seed) for seed in seeds))
    if has_asm2d and has_adm1:
        raise ValueError('Mixed biological families in one simulation_seed list: both an ASM2D aerobic seed and an ADM1/mADM1 anaerobic seed are present. These are two independent trains; dispatch each family as a SEPARATE solve_flowsheet / build_from_simulation_seeds call. The seed compiler never welds two families into one flowsheet.')
    designed_family = _designed_role_graph_family(seeds)
    if designed_family == DESIGNED_GRAPH_FAMILY_ADM1:
        return _compile_designed_adm1_plan(seeds, external_streams)
    if has_adm1:
        raise RoleGraphValidationError(_incomplete_adm1_payload(seeds))
    if not has_asm2d:
        return _compile_non_bio_plan(seeds)
    if designed_family == DESIGNED_GRAPH_FAMILY_ASM2D:
        return _compile_designed_biological_plan(seeds, external_streams)
    template_name = _infer_template_name(seeds, template_hint)
    config, warnings = _biological_config_from_seeds(seeds, external_streams, template_name)
    template = _template_for_name(template_name, config)
    return WaterTAPSeedBuildPlan(template_name=template_name, session_spec=template.to_session_spec(), seeds=list(seeds), warnings=warnings, topology_source=TOPOLOGY_SOURCE_PRESET_FALLBACK)
