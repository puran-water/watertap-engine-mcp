"""Multi-ion electrodialysis (EDR) rating templates on WaterTAP's Electrodialysis1D.

Two structures, both rating a stated stack (geometry bound from the stage (b) / datasheet record through
``core.rating_geometry``; a rating never invents geometry) on the ``constant_current_multi_ion_1d`` profile:

* ``ed_k_stage_series``: k stacks in series on the diluate (one pass), each stack's concentrate fed from the common
  concentrate loop at its loop state (a feed-and-bleed brine loop at steady state presents the loop composition to every
  stack inlet). Ported from watertap-org/Electrodialysis_Experiment ``processes/k_stage_single_pass.py``
  (``KStageSinglePassData._wire_arcs``: stage-to-stage diluate arcs), BSD-style WaterTAP licence, with the concentrate
  side changed from series to the parallel loop feed MEGA's EWTU 1L3S describes.
* ``ed_stack_conc_recirc``: one stack with feed split to diluate and concentrate make-up, the concentrate recirculated
  through a mixer and bled at a stated fraction (a tear stream on the recycle). Ported from WaterTAP 1.8
  ``flowsheets/electrodialysis/electrodialysis_1stack_conc_recirc.py`` (``build``: sepa0 / mix0 / EDstack / sepa1 arcs),
  without its pumps (pressure is not a degree of freedom of the rating) and with the multi-ion profile.

WaterTAP's ion transport numbers are one constant per (membrane, ion) per unit; the templates take them per stage from
the caller (the EDR layer evaluates its selectivity surrogate at each stack's mean composition). That limitation, and
the EDR layer's composition-dependent kernel that closes it, are recorded in
``engineering_utils.heuristics.electrodialysis`` (GAPS of the lane report).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from watertap_engine_mcp.core.property_registry import PropertyPackageType
PROFILE_NAME = 'constant_current_multi_ion_1d'
GEOMETRY_FIELDS = (('cell_width_m', 'cell_width', 'm'), ('cell_length_m', 'cell_length', 'm'), ('channel_height_m', 'channel_height', 'm'), ('spacer_porosity', 'spacer_porosity', 'fraction'), ('membrane_thickness_m', 'membrane_thickness', 'm'))
ED_UNIT_CONFIG = {'operation_mode': 'Constant_Current', 'has_nonohmic_potential_membrane': True, 'has_Nernst_diffusion_layer': True, 'limiting_current_density_method': 'Theoretical', 'has_pressure_change': True, 'pressure_drop_method': 'Darcy_Weisbach', 'friction_factor_method': 'Gurreri', 'hydraulic_diameter_method': 'conventional'}

def _package_config(species: List[str]) -> Dict[str, Any]:
    from puran_mcp_support.engineering.heuristics.electrodialysis.ions import mcas_package_config
    return mcas_package_config(species)

def _mcas(key: str) -> str:
    from puran_mcp_support.engineering.heuristics.electrodialysis.ions import species
    return species(key).mcas

def _ions(species: List[str]) -> List[str]:
    from puran_mcp_support.engineering.heuristics.electrodialysis.ions import species as sp
    return [k for k in species if sp(k).charge != 0]

@dataclass
class EDMembraneConfig:
    """Membrane transport parameters; every value is the caller's (calibrated or sourced), none is defaulted here."""
    areal_resistance_cem_ohm_m2: float
    areal_resistance_aem_ohm_m2: float
    water_trans_number_cem: float
    water_trans_number_aem: float
    water_permeability_cem_m_s_pa: float
    water_permeability_aem_m_s_pa: float
    ion_membrane_diffusivity_m2_s: float
    neutral_membrane_diffusivity_m2_s: float
    salt_diffusivity_m2_s: float
    current_utilization: float = 1.0
    electrodes_resistance_ohm_m2: float = 0.0
    spacer_conductivity_coefficient: float = 1.0

def _ed_fixes(unit_id: str, *, geometry: Dict[str, float], cell_pairs: int, current_a: float, membrane: EDMembraneConfig, species: List[str], transport_numbers: Dict[str, Dict[str, float]]) -> List[Dict[str, Any]]:
    m = membrane
    fixes = [('cell_pair_num', float(cell_pairs)), ('cell_width', geometry['cell_width_m']), ('cell_length', geometry['cell_length_m']), ('channel_height', geometry['channel_height_m']), ('spacer_porosity', geometry['spacer_porosity']), ('membrane_thickness[cem]', geometry['membrane_thickness_m']), ('membrane_thickness[aem]', geometry['membrane_thickness_m']), ('current_applied[0]', float(current_a)), ('electrodes_resistance', m.electrodes_resistance_ohm_m2), ('current_utilization', m.current_utilization), ('spacer_conductivity_coefficient', m.spacer_conductivity_coefficient), ('membrane_areal_resistance[cem]', m.areal_resistance_cem_ohm_m2), ('membrane_areal_resistance[aem]', m.areal_resistance_aem_ohm_m2), ('membrane_areal_resistance_coef[cem]', 0.0), ('membrane_areal_resistance_coef[aem]', 0.0), ('water_trans_number_membrane[cem]', m.water_trans_number_cem), ('water_trans_number_membrane[aem]', m.water_trans_number_aem), ('water_permeability_membrane[cem]', m.water_permeability_cem_m_s_pa), ('water_permeability_membrane[aem]', m.water_permeability_aem_m_s_pa), ('diffus_mass', m.salt_diffusivity_m2_s)]
    for key in species:
        name = _mcas(key)
        is_ion = key in _ions(species)
        for mem in ('cem', 'aem'):
            fixes.append((f'solute_diffusivity_membrane[{mem},{name}]', m.ion_membrane_diffusivity_m2_s if is_ion else m.neutral_membrane_diffusivity_m2_s))
            if is_ion:
                try:
                    value = float(transport_numbers[mem][key])
                except KeyError as exc:
                    raise ValueError(f'{unit_id}: transport number {mem}/{key} not supplied; the multi-ion profile fixes one per membrane and ion') from exc
                fixes.append((f'ion_trans_number_membrane[{mem},{name}]', value))
    return [{'unit_id': unit_id, 'var_name': var, 'value': value} for var, value in fixes]

def _check_transport_numbers(species: List[str], transport_numbers: Dict[str, Dict[str, float]], where: str):
    from puran_mcp_support.engineering.heuristics.electrodialysis.ions import species as sp
    for mem in ('cem', 'aem'):
        total = sum((float(transport_numbers[mem][k]) for k in _ions(species)))
        if abs(total - 1.0) > 1e-06:
            raise ValueError(f'{where}: {mem} transport numbers over the ions sum to {total:.6f}, not 1')
        counter = sum((float(transport_numbers[mem][k]) for k in _ions(species) if (sp(k).charge > 0) == (mem == 'cem')))
        if counter < 0.5:
            raise ValueError(f'{where}: {mem} counter-ion transport numbers sum to {counter:.3f} (< 0.5)')

def _feed_state(unit_id: str, flow_m3_h: float, mg_l: Dict[str, float], species: List[str], temperature_c: float, pressure_bar: float, package_config: Dict[str, Any]) -> Dict[str, Any]:
    from watertap_engine_mcp.flowsheets.feed_states import build_feed_state_payload
    return build_feed_state_payload(unit_id=unit_id, pkg=PropertyPackageType.MCAS, flow_vol_m3_hr=flow_m3_h, components={_mcas(k): float(mg_l.get(k, 0.0)) for k in species}, temperature_C=temperature_c, pressure_bar=pressure_bar, component_charges=package_config['charge'], property_package_config=package_config)

@dataclass
class EDKStageSeriesConfig:
    """k stacks in series on the diluate; geometry per the stage (b) / datasheet record (rating_geometry binds it)."""
    membrane: Dict[str, Any] | EDMembraneConfig
    stage_cell_pairs: List[int]
    stage_current_a: List[float]
    transport_numbers: List[Dict[str, Dict[str, float]]]
    feed_flow_m3_h: float
    feed_mg_l: Dict[str, float]
    concentrate_flow_m3_h: float
    concentrate_mg_l: Dict[str, float]
    cell_width_m: Optional[float] = None
    cell_length_m: Optional[float] = None
    channel_height_m: Optional[float] = None
    spacer_porosity: Optional[float] = None
    membrane_thickness_m: Optional[float] = None
    species: List[str] = field(default_factory=lambda: ['Na', 'Cl'])
    temperature_c: float = 25.0
    inlet_pressure_bar: float = 2.5
    finite_elements: int = 10
    property_package: PropertyPackageType = PropertyPackageType.MCAS
    operating_mode: str = 'rating'

class EDKStageSeriesTemplate:

    def __init__(self, config: EDKStageSeriesConfig):
        if isinstance(config.membrane, dict):
            config.membrane = EDMembraneConfig(**config.membrane)
        self.config = config
        k = len(config.stage_cell_pairs)
        if k < 1 or len(config.stage_current_a) != k or len(config.transport_numbers) != k:
            raise ValueError('ed_k_stage_series: stage_cell_pairs, stage_current_a and transport_numbers must have one entry per stage')
        for index, numbers in enumerate(config.transport_numbers):
            _check_transport_numbers(config.species, numbers, f'ed_k_stage_series stage {index + 1}')
        self.package_config = _package_config(config.species)

    @property
    def stages(self) -> List[int]:
        return list(range(1, len(self.config.stage_cell_pairs) + 1))

    def _geometry(self) -> Dict[str, float]:
        geometry = {name: getattr(self.config, name) for name, _, _ in GEOMETRY_FIELDS}
        missing = [name for name, value in geometry.items() if value is None]
        if missing:
            raise ValueError(f'ed_k_stage_series: stage (b) geometry missing {missing}; a rating never invents it')
        return geometry

    def get_units(self) -> List[Dict[str, Any]]:
        units = [{'unit_id': 'FeedD', 'unit_type': 'Feed', 'config': {}}, {'unit_id': 'Product', 'unit_type': 'Product', 'config': {}}]
        for k in self.stages:
            units += [{'unit_id': f'FeedC_{k}', 'unit_type': 'Feed', 'config': {}}, {'unit_id': f'ED_{k}', 'unit_type': 'Electrodialysis1D', 'config': {**ED_UNIT_CONFIG, 'finite_elements': self.config.finite_elements}}, {'unit_id': f'ConcOut_{k}', 'unit_type': 'Product', 'config': {}}]
        return units

    def get_connections(self) -> List[Dict[str, str]]:
        stages = self.stages
        conns = [{'src_unit': 'FeedD', 'src_port': 'outlet', 'dest_unit': 'ED_1', 'dest_port': 'inlet_diluate'}]
        for k in stages:
            conns.append({'src_unit': f'FeedC_{k}', 'src_port': 'outlet', 'dest_unit': f'ED_{k}', 'dest_port': 'inlet_concentrate'})
            conns.append({'src_unit': f'ED_{k}', 'src_port': 'outlet_concentrate', 'dest_unit': f'ConcOut_{k}', 'dest_port': 'inlet'})
            nxt = (f'ED_{k + 1}', 'inlet_diluate') if k < stages[-1] else ('Product', 'inlet')
            conns.append({'src_unit': f'ED_{k}', 'src_port': 'outlet_diluate', 'dest_unit': nxt[0], 'dest_port': nxt[1]})
        return conns

    def get_dof_fixes(self) -> List[Dict[str, Any]]:
        geometry = self._geometry()
        fixes: List[Dict[str, Any]] = []
        for k in self.stages:
            fixes += _ed_fixes(f'ED_{k}', geometry=geometry, cell_pairs=self.config.stage_cell_pairs[k - 1], current_a=self.config.stage_current_a[k - 1], membrane=self.config.membrane, species=self.config.species, transport_numbers=self.config.transport_numbers[k - 1])
        return fixes

    def get_feed_states(self) -> Dict[str, Dict[str, Any]]:
        c = self.config
        states = {'FeedD': _feed_state('FeedD', c.feed_flow_m3_h, c.feed_mg_l, c.species, c.temperature_c, c.inlet_pressure_bar, self.package_config)}
        for k in self.stages:
            states[f'FeedC_{k}'] = _feed_state(f'FeedC_{k}', c.concentrate_flow_m3_h, c.concentrate_mg_l, c.species, c.temperature_c, c.inlet_pressure_bar, self.package_config)
        return states

    def to_session_spec(self) -> Dict[str, Any]:
        return {'property_package': self.config.property_package.name, 'property_package_config': self.package_config, 'units': self.get_units(), 'connections': self.get_connections(), 'dof_fixes': self.get_dof_fixes(), 'feed_states': self.get_feed_states(), 'metadata': {'ed_profile': PROFILE_NAME, 'ed_template': 'ed_k_stage_series'}}

@dataclass
class EDStackConcRecircConfig:
    """One stack, feed split to diluate and concentrate make-up, concentrate recirculated and bled."""
    membrane: Dict[str, Any] | EDMembraneConfig
    cell_pairs: int
    current_a: float
    transport_numbers: Dict[str, Dict[str, float]]
    feed_flow_m3_h: float
    feed_mg_l: Dict[str, float]
    feed_split_to_diluate: float
    bleed_fraction: float
    cell_width_m: Optional[float] = None
    cell_length_m: Optional[float] = None
    channel_height_m: Optional[float] = None
    spacer_porosity: Optional[float] = None
    membrane_thickness_m: Optional[float] = None
    species: List[str] = field(default_factory=lambda: ['Na', 'Cl'])
    temperature_c: float = 25.0
    inlet_pressure_bar: float = 2.5
    finite_elements: int = 10
    property_package: PropertyPackageType = PropertyPackageType.MCAS
    operating_mode: str = 'rating'

class EDStackConcRecircTemplate:

    def __init__(self, config: EDStackConcRecircConfig):
        if isinstance(config.membrane, dict):
            config.membrane = EDMembraneConfig(**config.membrane)
        if not 0 < config.feed_split_to_diluate < 1 or not 0 < config.bleed_fraction <= 1:
            raise ValueError('ed_stack_conc_recirc: feed_split_to_diluate in (0, 1) and bleed_fraction in (0, 1]')
        _check_transport_numbers(config.species, config.transport_numbers, 'ed_stack_conc_recirc')
        self.config = config
        self.package_config = _package_config(config.species)

    def get_units(self) -> List[Dict[str, Any]]:
        return [{'unit_id': 'Feed', 'unit_type': 'Feed', 'config': {}}, {'unit_id': 'SepFeed', 'unit_type': 'Separator', 'config': {'outlet_list': ['to_diluate', 'to_makeup']}}, {'unit_id': 'MixConc', 'unit_type': 'Mixer', 'config': {'inlet_list': ['from_makeup', 'from_recycle'], 'momentum_mixing_type': 'none'}}, {'unit_id': 'ED', 'unit_type': 'Electrodialysis1D', 'config': {**ED_UNIT_CONFIG, 'finite_elements': self.config.finite_elements}}, {'unit_id': 'SepBleed', 'unit_type': 'Separator', 'config': {'outlet_list': ['to_bleed', 'to_recycle']}}, {'unit_id': 'Product', 'unit_type': 'Product', 'config': {}}, {'unit_id': 'Bleed', 'unit_type': 'Product', 'config': {}}]

    def get_connections(self) -> List[Dict[str, Any]]:
        return [{'src_unit': 'Feed', 'src_port': 'outlet', 'dest_unit': 'SepFeed', 'dest_port': 'inlet'}, {'src_unit': 'SepFeed', 'src_port': 'to_diluate', 'dest_unit': 'ED', 'dest_port': 'inlet_diluate'}, {'src_unit': 'SepFeed', 'src_port': 'to_makeup', 'dest_unit': 'MixConc', 'dest_port': 'from_makeup'}, {'src_unit': 'MixConc', 'src_port': 'outlet', 'dest_unit': 'ED', 'dest_port': 'inlet_concentrate'}, {'src_unit': 'ED', 'src_port': 'outlet_diluate', 'dest_unit': 'Product', 'dest_port': 'inlet'}, {'src_unit': 'ED', 'src_port': 'outlet_concentrate', 'dest_unit': 'SepBleed', 'dest_port': 'inlet'}, {'src_unit': 'SepBleed', 'src_port': 'to_bleed', 'dest_unit': 'Bleed', 'dest_port': 'inlet'}, {'src_unit': 'SepBleed', 'src_port': 'to_recycle', 'dest_unit': 'MixConc', 'dest_port': 'from_recycle', 'tear_stream': True, 'tear_rationale': 'concentrate loop recirculation (feed-and-bleed brine loop)'}]

    def get_dof_fixes(self) -> List[Dict[str, Any]]:
        c = self.config
        geometry = {name: getattr(c, name) for name, _, _ in GEOMETRY_FIELDS}
        missing = [name for name, value in geometry.items() if value is None]
        if missing:
            raise ValueError(f'ed_stack_conc_recirc: stage (b) geometry missing {missing}; a rating never invents it')
        fixes = _ed_fixes('ED', geometry=geometry, cell_pairs=c.cell_pairs, current_a=c.current_a, membrane=c.membrane, species=c.species, transport_numbers=c.transport_numbers)
        fixes.append({'unit_id': 'SepFeed', 'var_name': 'split_fraction[0,to_diluate]', 'value': c.feed_split_to_diluate})
        fixes.append({'unit_id': 'SepBleed', 'var_name': 'split_fraction[0,to_bleed]', 'value': c.bleed_fraction})
        return fixes

    def to_session_spec(self) -> Dict[str, Any]:
        c = self.config
        return {'property_package': c.property_package.name, 'property_package_config': self.package_config, 'units': self.get_units(), 'connections': self.get_connections(), 'dof_fixes': self.get_dof_fixes(), 'feed_states': {'Feed': _feed_state('Feed', c.feed_flow_m3_h, c.feed_mg_l, c.species, c.temperature_c, c.inlet_pressure_bar, self.package_config)}, 'metadata': {'ed_profile': PROFILE_NAME, 'ed_template': 'ed_stack_conc_recirc'}}
