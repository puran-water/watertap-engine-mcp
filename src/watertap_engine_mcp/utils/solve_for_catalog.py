"""Data-driven catalog for the general solve-for-X-given-Y (design-spec) tools.

Roadmap task A7 PR1. This module is PURE DATA + small lookups. It maps the
engine's already-implemented target-driven process controllers onto a single
discoverable ``solve_for`` mechanism:

    SRT            <- WAS split        (``srt`` controller)
    outlet DO      <- aeration KLa     (``do_target`` controller)
    residual alk.  <- caustic dose     (``alkalinity_target`` controller)
    effluent TP    <- metal dose       (``chemical_p_target`` controller)
    flow ratio     <- recycle ratio    (``recycle_ratio`` controller)

plus a fully general ``raw_var`` target (pin any model Var/expression to a value
while freeing an explicit actuator).

Nothing here builds a Pyomo model or runs a solver. The catalog only describes
(a) which structural ROLE carries each actuator/target, (b) how a preset fills
the ``free``/``target`` request, and (c) the remediation text for capabilities
that are registered but not yet wired (carbon->NO3 is roadmap A7 PR2). The
DOF-safe specification swap itself is performed in ``actions.solve_for`` by
ROUTING the resolved request to the existing
``ModelBuilder._add_*_target_constraint`` builders through a session
``process_constraints`` entry — the physics is never reimplemented here.
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional
ERR_ACTUATOR_NOT_FOUND = 'ACTUATOR_NOT_FOUND'
ERR_ACTUATOR_AMBIGUOUS = 'ACTUATOR_AMBIGUOUS'
ERR_TARGET_NOT_COMPUTABLE = 'TARGET_NOT_COMPUTABLE'
ERR_DOF_NOT_NEUTRAL = 'DOF_NOT_NEUTRAL'
ERR_STRUCTURALLY_DISCONNECTED = 'STRUCTURALLY_DISCONNECTED'
ERR_STRUCTURALLY_SINGULAR = 'STRUCTURALLY_SINGULAR'
ERR_TARGET_AT_BOUND_UNREACHABLE = 'TARGET_AT_BOUND_UNREACHABLE'
ERR_INEQUALITY_REQUIRES_OPTIMIZATION_MODE = 'INEQUALITY_REQUIRES_OPTIMIZATION_MODE'
ERR_PRESET_UNAVAILABLE = 'PRESET_UNAVAILABLE'
ERR_SOLVE_FOR_BUILD_ERROR = 'SOLVE_FOR_BUILD_ERROR'
ERR_INVALID_REQUEST = 'INVALID_SOLVE_FOR_REQUEST'

class SolveForError(Exception):
    """A structured solve_for resolution/validation failure.

    Carries a stable ``code`` (one of the ``ERR_*`` constants) and an optional
    ``details`` payload so ``actions.solve_for`` can convert it to the canonical
    ``api.errors`` envelope without losing the failure taxonomy.
    """

    def __init__(self, code: str, message: str, *, details: Optional[Dict[str, Any]]=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
EXACT_KIND = 'exact'
MIN_KIND = 'min'
MAX_KIND = 'max'
INEQUALITY_KINDS = frozenset({MIN_KIND, MAX_KIND})
VALID_KINDS = frozenset({EXACT_KIND, MIN_KIND, MAX_KIND})
TARGET_QUANTITIES: Dict[str, Dict[str, Any]] = {'srt_days': {'label': 'Sludge retention time', 'constraint_type': 'srt', 'value_key': 'target_days', 'value_units': 'days', 'default_constraint_kind': EXACT_KIND, 'supported_kinds': [EXACT_KIND], 'reactor_roles': ['pre_anoxic', 'aerobic', 'post_anoxic'], 'waste_role': 'sludge_splitter', 'waste_port': 'waste', 'biomass_components': ['X_AUT', 'X_H', 'X_PAO'], 'actuator_named': 'was', 'description': 'Pin the sludge retention time and free the WAS split fraction (sum(V*X) = SRT * Q_was * X_was).'}, 'do_mg_l': {'label': 'Aerobic outlet dissolved oxygen', 'constraint_type': 'do_target', 'value_key': 'target_mg_l', 'value_units': 'mg/L', 'default_constraint_kind': EXACT_KIND, 'supported_kinds': [EXACT_KIND], 'target_role': 'aerobic', 'target_port': 'outlet', 'component': 'S_O2', 'actuator_role': 'aerobic', 'actuator_named': 'kla', 'kla_lower': 0.0, 'kla_upper': 100.0, 'description': 'Pin the aerobic outlet S_O2 and free the aeration KLa.'}, 'residual_alkalinity': {'label': 'Effluent residual alkalinity', 'constraint_type': 'alkalinity_target', 'value_key': 'target_mol_per_m3', 'value_units': 'mol/m^3 (as alkalinity)', 'default_constraint_kind': EXACT_KIND, 'supported_kinds': [EXACT_KIND, MIN_KIND], 'target_role': 'product_effluent', 'target_port': 'inlet', 'dose_role': 'alkalinity', 'actuator_named': 'caustic_dose', 'dose_lower': 0.0, 'dose_upper': 50.0, 'description': "Pin (exact) or floor (min) the effluent residual alkalinity and free the caustic/NaOH-equivalent dose. 'min' needs optimization_mode."}, 'effluent_tp_mg_l': {'label': 'Effluent total phosphorus', 'constraint_type': 'chemical_p_target', 'value_key': 'target_mg_l', 'value_units': 'mg/L (total P)', 'default_constraint_kind': EXACT_KIND, 'supported_kinds': [EXACT_KIND], 'target_role': 'product_effluent', 'target_port': 'inlet', 'dose_role': 'chemical_p', 'actuator_named': 'fecl3_dose', 'p_basis': 'total_p', 'dose_lower': 0.0, 'dose_upper': 0.5, 'description': 'Pin the effluent total P (asm2d_total_p_expr) and free the metal (Fe/Al) dose on the ChemicalPhosphorusRemovalASM2D unit.'}, 'effluent_no3_mg_l': {'label': 'Effluent nitrate (NO3-N)', 'constraint_type': 'no3_target', 'value_key': 'target_mg_l', 'value_units': 'mg/L (NO3-N)', 'default_constraint_kind': EXACT_KIND, 'supported_kinds': [EXACT_KIND], 'target_role': 'product_effluent', 'target_port': 'inlet', 'component': 'S_NO3', 'dose_role': 'supplemental_carbon', 'actuator_named': 'carbon_dose', 'dose_lower': 0.0, 'dose_upper': 2.0, 'description': 'Pin the effluent (or anoxic-zone) S_NO3 and free the external supplemental-carbon (rbCOD/S_A) dose on the SupplementalCarbonDoseASM2D unit; the ASM2D denitrification kinetics consume the added carbon to reduce nitrate.'}, 'flow_ratio': {'label': 'Hydraulic recycle ratio', 'constraint_type': 'recycle_ratio', 'value_key': 'target_ratio', 'value_units': 'dimensionless (Q_numerator / Q_denominator)', 'default_constraint_kind': EXACT_KIND, 'supported_kinds': [EXACT_KIND], 'numerator_role': 'ras_pressure', 'numerator_port': 'outlet', 'denominator_role': 'feed', 'denominator_port': 'outlet', 'ratio_name': 'solve_for_recycle_ratio', 'actuator_named': 'ras_ratio', 'description': 'Pin a hydraulic recycle ratio (Q_num = ratio * Q_den) by adding the bounded recycle-ratio process variable and fixing it to the target.'}, 'raw_var': {'label': 'Arbitrary model variable / expression', 'constraint_type': 'raw_var_target', 'value_key': 'target_value', 'value_units': '(variable native units)', 'default_constraint_kind': EXACT_KIND, 'supported_kinds': [EXACT_KIND], 'requires_target_locator': True, 'description': "Pin any resolvable model Var via 'target.unit_id' + 'target.var_path' while freeing an explicit 'free' actuator (DOF-neutral exact swap). The fully general escape hatch when no named target quantity fits. PR1 supports 'exact' only; min/max floors/caps are not wired for raw_var yet."}}
NAMED_ACTUATORS: Dict[str, Dict[str, Any]] = {'was': {'label': 'WAS split fraction', 'role': 'sludge_splitter', 'var_kind': 'was_split', 'var_hint': 'split_fraction[0,waste]', 'units': 'dimensionless', 'owned_by': 'srt'}, 'kla': {'label': 'Aeration mass-transfer coefficient', 'role': 'aerobic', 'var_kind': 'kla', 'var_hint': 'KLa', 'units': '1/hr', 'owned_by': 'do_target'}, 'caustic_dose': {'label': 'Caustic / NaOH-equivalent dose', 'role': 'alkalinity', 'var_kind': 'caustic_dose', 'var_hint': 'caustic_dose[0]', 'units': 'mol/m^3', 'owned_by': 'alkalinity_target'}, 'fecl3_dose': {'label': 'Metal (Fe/Al) salt dose', 'role': 'chemical_p', 'var_kind': 'metal_dose', 'var_hint': 'metal_dose[0]', 'units': 'kg/m^3', 'owned_by': 'chemical_p_target'}, 'carbon_dose': {'label': 'Supplemental external-carbon (rbCOD) dose', 'role': 'supplemental_carbon', 'var_kind': 'carbon_dose', 'var_hint': 'carbon_dose[0]', 'units': 'kg/m^3', 'owned_by': 'no3_target'}, 'ras_ratio': {'label': 'RAS (return activated sludge) recycle ratio', 'role': 'ras_pressure', 'var_kind': 'recycle_ratio', 'var_hint': 'process_var_solve_for_recycle_ratio', 'units': 'dimensionless', 'owned_by': 'recycle_ratio'}, 'mlr_ratio': {'label': 'MLR (mixed-liquor internal) recycle ratio', 'role': 'internal_recycle_pressure', 'var_kind': 'recycle_ratio', 'var_hint': 'process_var_solve_for_recycle_ratio', 'units': 'dimensionless', 'owned_by': 'recycle_ratio'}}
PRESETS: Dict[str, Dict[str, Any]] = {'srt_to_was': {'label': 'SRT -> WAS split', 'target_quantity': 'srt_days', 'actuator': 'was', 'default_constraint_kind': EXACT_KIND, 'default_seed': None, 'value_hint': 18.0, 'available': True, 'description': 'Solve for the WAS split fraction that yields a target SRT (days).'}, 'do_to_kla': {'label': 'Outlet DO -> aeration KLa', 'target_quantity': 'do_mg_l', 'actuator': 'kla', 'default_constraint_kind': EXACT_KIND, 'default_seed': 5.0, 'value_hint': 2.0, 'available': True, 'description': 'Solve for the aeration KLa that yields a target aerobic outlet DO (mg/L).'}, 'alkalinity_to_caustic': {'label': 'Residual alkalinity -> caustic dose', 'target_quantity': 'residual_alkalinity', 'actuator': 'caustic_dose', 'default_constraint_kind': EXACT_KIND, 'default_seed': None, 'value_hint': 3.5, 'available': True, 'description': "Solve for the caustic dose that yields a target effluent residual alkalinity (mol/m^3). Default kind is exact; 'min' needs optimization_mode."}, 'tp_to_fecl3': {'label': 'Effluent TP -> metal dose (legacy diagnostic)', 'target_quantity': 'effluent_tp_mg_l', 'actuator': 'fecl3_dose', 'default_constraint_kind': EXACT_KIND, 'default_seed': 0.0075, 'value_hint': 1.0, 'available': True, 'calculation_class': 'legacy_stoichiometric', 'recommendation_eligible': False, 'operational_use': 'diagnostic_inverse_solve_only', 'recommendation_blockers': ['legacy_stoichiometric_chemical_p_surrogate', 'full_Fe_S_P_inventory_not_closed', 'plant_calibration_and_holdout_validation_missing'], 'description': 'Solve the legacy ASM2d stoichiometric metal-salt surrogate for a target modeled effluent TP. This is a diagnostic inverse solve, not a ferric or alum operating-dose recommendation.'}, 'target_to_recycle_ratio': {'label': 'Pin a hydraulic recycle ratio', 'target_quantity': 'flow_ratio', 'actuator': 'ras_ratio', 'default_constraint_kind': EXACT_KIND, 'default_seed': None, 'value_hint': 1.0, 'available': True, 'description': 'Pin the RAS recycle ratio (Q_RAS / Q_feed) by adding+fixing the ratio variable.'}, 'carbon_to_no3': {'label': 'Effluent NO3 -> supplemental carbon dose', 'target_quantity': 'effluent_no3_mg_l', 'actuator': 'carbon_dose', 'default_constraint_kind': EXACT_KIND, 'default_seed': None, 'value_hint': 10.0, 'available': True, 'description': 'Solve for the supplemental carbon dose that yields a target effluent nitrate (mg/L).'}}
REPORT_ONLY_QUANTITIES: Dict[str, Dict[str, Any]] = {'mlss': {'label': 'Mixed liquor suspended solids (MLSS)', 'constraint_type': 'mlss_bounds', 'controllable': False, 'note': 'MLSS is a SOLVED / REPORT quantity: when SRT and reactor volumes are fixed, MLSS is the steady-state result of SRT, influent load and WAS/RAS hydraulics, bounded by an envelope (mlss_bounds), not driven by a controller. To target a biomass inventory, prescribe SRT (preset srt_to_was) and reactor volumes instead of pinning MLSS.'}}

def actuator_named_ids() -> List[str]:
    """Return the recognized ``named_actuator`` selector ids."""
    return list(NAMED_ACTUATORS.keys())

def available_presets() -> Dict[str, Dict[str, Any]]:
    """Return only the wired (available) presets."""
    return {name: row for name, row in PRESETS.items() if row.get('available')}

def unavailable_presets() -> Dict[str, Dict[str, Any]]:
    """Return the registered-but-unavailable presets (with remediation)."""
    return {name: row for name, row in PRESETS.items() if not row.get('available')}
