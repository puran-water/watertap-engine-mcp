"""RO Train Flowsheet Template.

Provides a pre-configured RO desalination train with:
- Feed pump
- High-pressure pump
- RO membrane
- Energy recovery device (optional)
- Permeate and brine outlets
"""
import math
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from watertap_engine_mcp.core.property_registry import PropertyPackageType
from watertap_engine_mcp.public_catalog_boundary import validate_product_transport
STANDARD_ATMOSPHERE_BAR = 1.01325
LEGACY_DEFAULT_FEED_PRESSURE_BARA = 60.0
LEGACY_DEFAULT_A_COMP = 4.2e-12
LEGACY_DEFAULT_B_COMP = 3.5e-08
RO_OPERATING_MODES = frozenset({'rating', 'design'})

@dataclass
class ROTrainConfig:
    """Configuration for RO train template."""
    property_package: PropertyPackageType = PropertyPackageType.SEAWATER
    membrane_type: str = 'SWRO'
    membrane_model: Optional[str] = None
    transport_basis: Optional[str] = None
    transport_provenance: Optional[str] = None
    provenance_reference: Optional[str] = None
    membrane_area_m2: float = 50.0
    n_stages: int = 1
    stage_vessels: Optional[List[int]] = None
    elements_per_vessel: int = 7
    element_area_m2: float = 34.4
    operating_mode: str = 'rating'
    feed_pressure_barg: float = LEGACY_DEFAULT_FEED_PRESSURE_BARA - STANDARD_ATMOSPHERE_BAR
    permeate_pressure_bara: float = STANDARD_ATMOSPHERE_BAR
    stage_pressures_barg: Optional[List[float]] = None
    pressure_drop_bar: float = 0.5
    stage_pressure_drops_bar: Optional[List[float]] = None
    pressure_input_basis: Optional[str] = None
    stage_recovery_targets: Optional[List[float]] = None
    stage_recovery_verification_targets: Optional[List[float]] = None
    target_system_recovery: Optional[float] = None
    system_recovery_verification_target: Optional[float] = None
    property_validity_ceiling_g_L: Optional[float] = None
    A_comp: Optional[float] = None
    B_comp: Optional[float] = None
    coefficient_temperature_K: float = 298.15
    include_erd: bool = False
    erd_efficiency: float = 0.95
    hp_pump_efficiency: float = 0.8
    booster_pump_efficiency: float = 0.8

    def __post_init__(self) -> None:
        """Validate tapered-array dimensions."""
        explicit_transport = self.A_comp is not None or self.B_comp is not None
        self.operating_mode = str(self.operating_mode).strip().lower()
        if self.operating_mode not in RO_OPERATING_MODES:
            raise ValueError(f'operating_mode must be one of {sorted(RO_OPERATING_MODES)}')
        if self.n_stages < 1:
            raise ValueError('n_stages must be at least 1')
        if self.stage_vessels is not None:
            if len(self.stage_vessels) != self.n_stages:
                raise ValueError('stage_vessels length must equal n_stages')
            if any((int(vessels) <= 0 for vessels in self.stage_vessels)):
                raise ValueError('stage_vessels values must be positive')
        if int(self.elements_per_vessel) <= 0:
            raise ValueError('elements_per_vessel must be positive')
        if float(self.element_area_m2) <= 0:
            raise ValueError('element_area_m2 must be positive')
        if not math.isfinite(float(self.membrane_area_m2)) or self.membrane_area_m2 <= 0:
            raise ValueError('membrane_area_m2 must be finite and positive')
        if self.stage_pressures_barg is not None and len(self.stage_pressures_barg) != self.n_stages:
            raise ValueError('stage_pressures_barg length must equal n_stages')
        if self.stage_pressure_drops_bar is not None and len(self.stage_pressure_drops_bar) != self.n_stages:
            raise ValueError('stage_pressure_drops_bar length must equal n_stages')
        if self.stage_recovery_targets is not None and len(self.stage_recovery_targets) != self.n_stages:
            raise ValueError('stage_recovery_targets length must equal n_stages')
        if self.stage_recovery_verification_targets is not None and len(self.stage_recovery_verification_targets) != self.n_stages:
            raise ValueError('stage_recovery_verification_targets length must equal n_stages')
        for name, pressure in (('feed_pressure_barg', self.feed_pressure_barg), *((f'stage_pressures_barg[{index}]', pressure) for index, pressure in enumerate(self.stage_pressures_barg or []))):
            if not math.isfinite(float(pressure)) or float(pressure) < 0:
                raise ValueError(f'{name} must be finite and non-negative')
        if not math.isfinite(float(self.permeate_pressure_bara)) or self.permeate_pressure_bara <= 0:
            raise ValueError('permeate_pressure_bara must be finite and positive')
        for name, pressure_drop in (('pressure_drop_bar', self.pressure_drop_bar), *((f'stage_pressure_drops_bar[{index}]', pressure_drop) for index, pressure_drop in enumerate(self.stage_pressure_drops_bar or []))):
            if not math.isfinite(float(pressure_drop)) or float(pressure_drop) < 0:
                raise ValueError(f'{name} must be finite and non-negative')
        if self.membrane_model is not None and (self.A_comp is None or self.B_comp is None):
            raise ValueError('a product-specific membrane_model requires explicit A_comp and B_comp; the template will not retain generic SWRO defaults')
        if explicit_transport and self.membrane_model is None:
            raise ValueError('explicit A_comp/B_comp requires a named membrane_model; unbound custom transport is not permitted')
        if self.A_comp is None:
            self.A_comp = LEGACY_DEFAULT_A_COMP
        if self.B_comp is None:
            self.B_comp = LEGACY_DEFAULT_B_COMP
        if not math.isfinite(float(self.A_comp)) or self.A_comp <= 0:
            raise ValueError('A_comp must be finite and positive')
        if not math.isfinite(float(self.B_comp)) or self.B_comp < 0:
            raise ValueError('B_comp must be finite and non-negative')
        if not math.isfinite(float(self.coefficient_temperature_K)) or self.coefficient_temperature_K <= 0:
            raise ValueError('coefficient_temperature_K must be finite and positive')
        if self.membrane_model is not None:
            validated_transport = validate_product_transport(membrane_model=self.membrane_model, A_w=self.A_comp, B_s=self.B_comp, coefficient_temperature_K=self.coefficient_temperature_K, transport_provenance=self.transport_provenance, provenance_reference=self.provenance_reference)
            derived_basis = validated_transport['transport_basis']
            if self.transport_basis not in (None, derived_basis):
                raise ValueError(f'transport_basis does not match the validated named-product transport basis {derived_basis!r}')
            self.membrane_model = validated_transport['membrane_model']
            self.transport_basis = derived_basis
            self.transport_provenance = validated_transport['transport_provenance']
            self.provenance_reference = validated_transport['provenance_reference']
        recovery_values = [*(self.stage_recovery_targets or []), *(self.stage_recovery_verification_targets or []), *([self.target_system_recovery] if self.target_system_recovery is not None else []), *([self.system_recovery_verification_target] if self.system_recovery_verification_target is not None else [])]
        if any((not math.isfinite(float(recovery)) or not 0 < float(recovery) < 1 for recovery in recovery_values)):
            raise ValueError('RO recovery targets must be finite fractions in (0, 1)')
        if self.operating_mode == 'rating':
            if self.stage_recovery_targets is not None or self.target_system_recovery is not None:
                raise ValueError('rating mode fixes the supplied membrane geometry; recovery must remain a solved result. Use system_recovery_verification_target for a comparison target.')
        else:
            if self.stage_vessels is not None:
                raise ValueError('design mode solves continuous membrane area and therefore cannot retain discrete stage_vessels metadata')
            if self.stage_recovery_targets is None and self.target_system_recovery is None:
                raise ValueError('design mode requires stage_recovery_targets or target_system_recovery')
            if self.system_recovery_verification_target is not None:
                raise ValueError('system_recovery_verification_target is only valid in rating mode')
            if self.stage_recovery_verification_targets is not None:
                raise ValueError('stage_recovery_verification_targets is only valid in rating mode')

@dataclass
class ROTrainUnits:
    """Units in the RO train."""
    feed: str = 'Feed'
    hp_pump: str = 'HPPump'
    ro: str = 'RO'
    permeate: str = 'Permeate'
    brine: str = 'Brine'
    erd: Optional[str] = 'ERD'
    booster: Optional[str] = 'Booster'

class ROTrainTemplate:
    """Template for RO desalination train flowsheet."""

    def __init__(self, config: Optional[ROTrainConfig]=None):
        """Initialize RO train template.

        Args:
            config: Configuration options
        """
        self.config = config or ROTrainConfig()
        if self.config.include_erd:
            raise NotImplementedError('ROTrainTemplate no longer ships an ERD topology. Use include_erd=False.')
        self.units = ROTrainUnits()

    def _is_multistage(self) -> bool:
        return self.config.n_stages > 1

    def _ro_id(self, stage_index: int) -> str:
        return f'RO_{stage_index}' if self._is_multistage() else self.units.ro

    def _booster_id(self, stage_index: int) -> str:
        return f'Booster_{stage_index}'

    def _permeate_mixer_id(self) -> str:
        return 'PermeateMixer'

    def _stage_pressure_barg(self, stage_index: int) -> float:
        if self.config.stage_pressures_barg:
            return float(self.config.stage_pressures_barg[stage_index - 1])
        return float(self.config.feed_pressure_barg)

    def _stage_pressure_bara(self, stage_index: int) -> float:
        """Convert the Stage-(b) gauge-pressure convention to WaterTAP absolute."""
        return self._stage_pressure_barg(stage_index) + STANDARD_ATMOSPHERE_BAR

    def _stage_pressure_drop_bar(self, stage_index: int) -> float:
        if self.config.stage_pressure_drops_bar:
            return float(self.config.stage_pressure_drops_bar[stage_index - 1])
        return float(self.config.pressure_drop_bar)

    def _stage_area_m2(self, stage_index: int) -> float:
        if self.config.stage_vessels:
            return float(self.config.stage_vessels[stage_index - 1]) * float(self.config.elements_per_vessel) * float(self.config.element_area_m2)
        if self.config.n_stages == 1:
            return float(self.config.membrane_area_m2)
        return float(self.config.membrane_area_m2) / float(self.config.n_stages)

    def _solute_component_name(self) -> str:
        if self.config.property_package in {PropertyPackageType.NACL, PropertyPackageType.NACL_T_DEP}:
            return 'NaCl'
        return 'TDS'

    def get_units(self) -> List[Dict[str, Any]]:
        """Get list of units to create.

        Returns:
            List of unit specifications
        """
        units: List[Dict[str, Any]] = [{'unit_id': self.units.feed, 'unit_type': 'Feed', 'config': {}}, {'unit_id': self.units.hp_pump, 'unit_type': 'Pump', 'config': {}}]
        for stage in range(1, self.config.n_stages + 1):
            if stage > 1:
                units.append({'unit_id': self._booster_id(stage), 'unit_type': 'Pump', 'config': {}})
            units.append({'unit_id': self._ro_id(stage), 'unit_type': 'ReverseOsmosis0D', 'config': {'has_pressure_change': True, 'concentration_polarization_type': 'fixed', 'mass_transfer_coefficient': 'none'}})
        if self._is_multistage():
            units.append({'unit_id': self._permeate_mixer_id(), 'unit_type': 'Mixer', 'config': {'inlet_list': [f'permeate_{stage}' for stage in range(1, self.config.n_stages + 1)], 'momentum_mixing_type': 'none'}})
        units.extend([{'unit_id': self.units.permeate, 'unit_type': 'Product', 'config': {}}, {'unit_id': self.units.brine, 'unit_type': 'Product', 'config': {}}])
        return units

    def get_connections(self) -> List[Dict[str, str]]:
        """Get list of connections.

        Returns:
            List of connection specifications
        """
        connections = [{'src_unit': self.units.feed, 'src_port': 'outlet', 'dest_unit': self.units.hp_pump, 'dest_port': 'inlet'}, {'src_unit': self.units.hp_pump, 'src_port': 'outlet', 'dest_unit': self._ro_id(1), 'dest_port': 'inlet'}]
        for stage in range(1, self.config.n_stages + 1):
            ro_id = self._ro_id(stage)
            if self._is_multistage():
                connections.append({'src_unit': ro_id, 'src_port': 'permeate', 'dest_unit': self._permeate_mixer_id(), 'dest_port': f'permeate_{stage}'})
            else:
                connections.append({'src_unit': ro_id, 'src_port': 'permeate', 'dest_unit': self.units.permeate, 'dest_port': 'inlet'})
            if stage < self.config.n_stages:
                connections.extend([{'src_unit': ro_id, 'src_port': 'retentate', 'dest_unit': self._booster_id(stage + 1), 'dest_port': 'inlet'}, {'src_unit': self._booster_id(stage + 1), 'src_port': 'outlet', 'dest_unit': self._ro_id(stage + 1), 'dest_port': 'inlet'}])
            else:
                connections.append({'src_unit': ro_id, 'src_port': 'retentate', 'dest_unit': self.units.brine, 'dest_port': 'inlet'})
        if self._is_multistage():
            connections.append({'src_unit': self._permeate_mixer_id(), 'src_port': 'outlet', 'dest_unit': self.units.permeate, 'dest_port': 'inlet'})
        return connections

    def _stage_recovery_target(self, stage: int) -> Optional[float]:
        """Resolve the enforced per-stage volumetric recovery target.

        Recovery is a fraction of the *stage's own* inlet volumetric flow, matching
        the WaterTAP ``recovery_vol_phase[t, 'Liq']`` definition.

        Priority:
          1. Explicit ``stage_recovery_targets[stage-1]`` when supplied.
          2. Otherwise derive an equal per-stage split from
             ``target_system_recovery`` via ``r = 1 - (1 - R_sys)**(1/n)`` so the
             product of stage rejections yields the requested system recovery.

        Returns None when neither is configured, in which case the stage stays in
        area-fixed rating mode (recovery becomes a solved result).
        """
        if self.config.operating_mode != 'design':
            return None
        if self.config.stage_recovery_targets is not None:
            return float(self.config.stage_recovery_targets[stage - 1])
        if self.config.target_system_recovery is not None:
            r_sys = float(self.config.target_system_recovery)
            return 1.0 - (1.0 - r_sys) ** (1.0 / self.config.n_stages)
        return None

    def get_dof_fixes(self) -> List[Dict[str, Any]]:
        """Get DOF fix specifications.

        Returns:
            List of variable fixes
        """
        solute_component = self._solute_component_name()
        fixes = [{'unit_id': self.units.hp_pump, 'var_name': 'efficiency_pump[0]', 'value': self.config.hp_pump_efficiency}, {'unit_id': self.units.hp_pump, 'var_name': 'control_volume.properties_out[0].pressure', 'value': self._stage_pressure_bara(1) * 100000.0}]
        for stage in range(1, self.config.n_stages + 1):
            if stage > 1:
                fixes.extend([{'unit_id': self._booster_id(stage), 'var_name': 'efficiency_pump[0]', 'value': self.config.booster_pump_efficiency}, {'unit_id': self._booster_id(stage), 'var_name': 'control_volume.properties_out[0].pressure', 'value': self._stage_pressure_bara(stage) * 100000.0}])
            ro_id = self._ro_id(stage)
            fixes.extend([{'unit_id': ro_id, 'var_name': 'A_comp[0,H2O]', 'value': self.config.A_comp}, {'unit_id': ro_id, 'var_name': f'B_comp[0,{solute_component}]', 'value': self.config.B_comp}, {'unit_id': ro_id, 'var_name': 'permeate.pressure[0]', 'value': self.config.permeate_pressure_bara * 100000.0}, {'unit_id': ro_id, 'var_name': 'feed_side.cp_modulus[0,*,*]', 'value': 1.1}, {'unit_id': ro_id, 'var_name': 'feed_side.deltaP[0]', 'value': -self._stage_pressure_drop_bar(stage) * 100000.0}])
            recovery_target = self._stage_recovery_target(stage)
            if recovery_target is not None:
                fixes.append({'unit_id': ro_id, 'var_name': 'recovery_vol_phase[0,Liq]', 'value': recovery_target})
            else:
                fixes.append({'unit_id': ro_id, 'var_name': 'area', 'value': self._stage_area_m2(stage)})
        if self._is_multistage():
            fixes.append({'unit_id': self._permeate_mixer_id(), 'var_name': 'mixed_state[0].pressure', 'value': self.config.permeate_pressure_bara * 100000.0})
        return fixes

    def get_scaling_factors(self) -> List[Dict[str, Any]]:
        """Get recommended scaling factors.

        Returns:
            List of scaling factor specifications
        """
        factors = []
        for stage in range(1, self.config.n_stages + 1):
            ro_id = self._ro_id(stage)
            factors.extend([{'unit_id': ro_id, 'var_name': 'A_comp', 'factor': 1000000000000.0}, {'unit_id': ro_id, 'var_name': 'B_comp', 'factor': 100000000.0}, {'unit_id': ro_id, 'var_name': 'mixed_permeate[0].pressure', 'factor': 1e-05}, {'unit_id': ro_id, 'var_name': 'area', 'factor': 0.01}])
        pump_ids = [self.units.hp_pump] + [self._booster_id(stage) for stage in range(2, self.config.n_stages + 1)]
        factors.extend([{'unit_id': pump_id, 'var_name': 'control_volume.properties_out[0].pressure', 'factor': 1e-05} for pump_id in pump_ids])
        return factors

    def get_initialization_order(self) -> List[str]:
        """Get unit initialization order.

        Returns:
            List of unit IDs in init order
        """
        order = [self.units.feed, self.units.hp_pump]
        for stage in range(1, self.config.n_stages + 1):
            if stage > 1:
                order.append(self._booster_id(stage))
            order.append(self._ro_id(stage))
        if self._is_multistage():
            order.append(self._permeate_mixer_id())
        order.extend([self.units.permeate, self.units.brine])
        return order

    def to_session_spec(self) -> Dict[str, Any]:
        """Convert template to session specification.

        Returns:
            Dict with complete session spec
        """
        return {'property_package': self.config.property_package.name, 'units': self.get_units(), 'connections': self.get_connections(), 'dof_fixes': self.get_dof_fixes(), 'scaling_factors': self.get_scaling_factors(), 'initialization_order': self.get_initialization_order(), 'metadata': {'ro_train': {'n_stages': self.config.n_stages, 'operating_mode': self.config.operating_mode, 'geometry_basis': 'fixed_discrete_array' if self.config.operating_mode == 'rating' and self.config.stage_vessels else 'fixed_membrane_area' if self.config.operating_mode == 'rating' else 'solved_continuous_area', 'membrane_type': self.config.membrane_type, 'membrane_model': self.config.membrane_model, 'transport_basis': self.config.transport_basis, 'transport_provenance': self.config.transport_provenance, 'provenance_reference': self.config.provenance_reference, 'A_comp_m_s_Pa': self.config.A_comp, 'B_comp_m_s': self.config.B_comp, 'coefficient_temperature_K': self.config.coefficient_temperature_K, 'stage_vessels': self.config.stage_vessels, 'elements_per_vessel': self.config.elements_per_vessel if self.config.operating_mode == 'rating' and self.config.stage_vessels else None, 'element_area_m2': self.config.element_area_m2 if self.config.operating_mode == 'rating' and self.config.stage_vessels else None, 'stage_area_m2': [self._stage_area_m2(stage) if self.config.operating_mode == 'rating' else None for stage in range(1, self.config.n_stages + 1)], 'stage_pressures_barg': [self._stage_pressure_barg(stage) for stage in range(1, self.config.n_stages + 1)], 'stage_pressures_bara': [self._stage_pressure_bara(stage) for stage in range(1, self.config.n_stages + 1)], 'stage_pressure_drops_bar': [self._stage_pressure_drop_bar(stage) for stage in range(1, self.config.n_stages + 1)], 'pressure_input_basis': self.config.pressure_input_basis, 'stage_recovery_targets': self.config.stage_recovery_targets, 'stage_recovery_verification_targets': self.config.stage_recovery_verification_targets, 'target_system_recovery': self.config.target_system_recovery, 'system_recovery_verification_target': self.config.system_recovery_verification_target, 'property_validity_ceiling_g_L': self.config.property_validity_ceiling_g_L}}}
