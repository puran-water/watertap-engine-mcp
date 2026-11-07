"""Curated session presets for public WaterTAP workflows."""
from dataclasses import dataclass, field
from typing import Any, Dict, List
from .property_registry import PropertyPackageType

@dataclass(frozen=True)
class SessionPreset:
    """Discovery and merge contract for a curated session preset."""
    name: str
    description: str
    property_package: PropertyPackageType
    property_package_config_defaults: Dict[str, Any] = field(default_factory=dict)
    required_override_keys: List[str] = field(default_factory=list)

    def merged_package_config(self, overrides: Dict[str, Any] | None=None) -> Dict[str, Any]:
        """Return the preset config merged with caller overrides."""
        payload = dict(self.property_package_config_defaults)
        payload.update(overrides or {})
        return payload

def _ed_multi_ion_config() -> Dict[str, Any]:
    from puran_mcp_support.engineering.heuristics.electrodialysis.ions import SPECIES, mcas_package_config
    return mcas_package_config(SPECIES)
SESSION_PRESETS: Dict[str, SessionPreset] = {'ZERO_ORDER_WATER': SessionPreset(name='ZERO_ORDER_WATER', description='Zero-order wastewater/reuse profile with municipal source defaults. Override solute_list to the curated constituent set needed by the workflow.', property_package=PropertyPackageType.ZERO_ORDER, property_package_config_defaults={'water_source': 'municipal'}, required_override_keys=['solute_list']), 'MCAS_SINGLE_TARGET': SessionPreset(name='MCAS_SINGLE_TARGET', description='Single-target MCAS profile for adsorption, ion exchange, and selective polishing. Override solute_list and molecular data for the target species.', property_package=PropertyPackageType.MCAS, property_package_config_defaults={'ignore_neutral_charge': True}, required_override_keys=['solute_list', 'mw_data']), 'MCAS_BRACKISH_5ION': SessionPreset(name='MCAS_BRACKISH_5ION', description='Five-ion brackish-water MCAS profile for membrane, ED, and softening workflows.', property_package=PropertyPackageType.MCAS, property_package_config_defaults={'solute_list': ['Na_+', 'Ca_2+', 'Mg_2+', 'Cl_-', 'SO4_2-'], 'charge': {'Na_+': 1, 'Ca_2+': 2, 'Mg_2+': 2, 'Cl_-': -1, 'SO4_2-': -2}, 'mw_data': {'Na_+': 0.02299, 'Ca_2+': 0.040078, 'Mg_2+': 0.024305, 'Cl_-': 0.03545, 'SO4_2-': 0.09606}, 'stokes_radius_data': {'Na_+': 1.84e-10, 'Ca_2+': 3.09e-10, 'Mg_2+': 3.47e-10, 'Cl_-': 1.21e-10, 'SO4_2-': 2.3e-10}, 'diffusivity_data': {'Na_+': 1.33e-09, 'Ca_2+': 9.2e-10, 'Mg_2+': 7.06e-10, 'Cl_-': 2.03e-09, 'SO4_2-': 1.06e-09}, 'elec_mobility_data': {'Na_+': 5.176592017916012e-08, 'Ca_2+': 7.161600987192078e-08, 'Mg_2+': 5.495750322780007e-08, 'Cl_-': 7.90111413260865e-08, 'SO4_2-': 8.251409833069132e-08}, 'activity_coefficient_model': 'davies', 'density_calculation': 'constant'}), 'ED_NACL_PAIR': SessionPreset(name='ED_NACL_PAIR', description='Approximate two-ion NaCl-equivalent MCAS profile for electrodialysis ingress. Use when an upstream or cross-engine boundary intentionally collapses broader chemistry to a sodium/chloride pair.', property_package=PropertyPackageType.MCAS, property_package_config_defaults={'solute_list': ['Na_+', 'Cl_-'], 'charge': {'Na_+': 1, 'Cl_-': -1}, 'mw_data': {'Na_+': 0.02299, 'Cl_-': 0.03545}, 'elec_mobility_data': {'Na_+': 5.19e-08, 'Cl_-': 7.92e-08}}), 'ED_MULTI_ION': SessionPreset(name='ED_MULTI_ION', description='Multi-ion MCAS profile for electrodialysis / EDR rating: Na, K, NH4, Ca, Mg, Sr, Ba, Cl, SO4, HCO3, NO3, F, H2PO4, HPO4 as migrating ions and SiO2, CO2 and neutral TOC as z=0 solutes. Charge, formula weight and the phreeqc.dat -dw diffusivities come from engineering_utils.heuristics.electrodialysis.ions (one table for the engine and the EDR layer); mobilities by the Einstein relation at 298.15 K. Override solute_list (and the data dicts) to the subset a duty carries.', property_package=PropertyPackageType.MCAS, property_package_config_defaults=_ed_multi_ion_config()), 'NACL_BRINE_THERMAL': SessionPreset(name='NACL_BRINE_THERMAL', description='NaCl thermal-brine profile for crystallization and thermal concentration workflows.', property_package=PropertyPackageType.CRYST_NACL), 'MD_THERMAL': SessionPreset(name='MD_THERMAL', description='Membrane-distillation thermal profile. Session default package is seawater; the curated MD unit profiles add water-side vapor/cold packages internally.', property_package=PropertyPackageType.SEAWATER), 'NDMA_AOP': SessionPreset(name='NDMA_AOP', description='NDMA trace-organic profile for ultraviolet advanced oxidation workflows.', property_package=PropertyPackageType.NDMA)}

def get_session_preset(name: str) -> SessionPreset:
    """Return one preset by name."""
    try:
        return SESSION_PRESETS[name.upper()]
    except KeyError as exc:
        valid = ', '.join(sorted(SESSION_PRESETS))
        raise KeyError(f"Unknown session preset '{name}'. Valid presets: {valid}") from exc

def list_session_presets() -> List[SessionPreset]:
    """Return all curated session presets."""
    return [SESSION_PRESETS[name] for name in sorted(SESSION_PRESETS)]
