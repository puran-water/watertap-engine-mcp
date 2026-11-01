"""ADM1 / mADM1 anaerobic-digestion steady-state proposal templates (roadmap A4).

A registered ADM1 "template" is a **named preset**, exactly like the ASM2D
families: a binding of the anaerobic-digestion *family provider*
(:class:`Adm1PresetProvider`) to a specific config (digester volumes, feed, the
design headspace pressure) and a structural *family profile*
(:class:`Adm1FamilyProfile` — which structural digestion roles the topology
carries). The provider, not the template, owns the solver defaults: the digester
DOF fixes (liquid/vapor volumes), the feed boundary state (including the
strong-ion difference ``cations`` / ``anions`` and the ``S_K`` / ``S_Mg`` trace
ions), the design headspace-pressure boundary, the scaling hint, and metadata.

PR1 ships the UASB only: a single methanogenic digester (one combined
acid/methane reactor) with an optional biogas take-off. The acid/methane phase
split, EGSB/AnMBR/contact/two-stage trains, and the full multi-family provider
are PR2/PR3.

The hard structural contract this provider establishes — and the A4 oracle
proves — is that the digester builds to DOF=0 with **no fixture pins**: the vapor
``S_h2``, the reaction-block H+ (``S_H`` / pH), and the product trace ions are all
SOLVER-DETERMINED (by the gas-phase property equations, the ADM1 acid/base
electroneutrality constraint, and the component balances respectively), while the
gas-phase headspace pressure is preserved by name through the build's pressure
demotion. The feed strong-ion composition (``cations`` / ``anions`` / ``S_K`` /
``S_Mg``) is the user boundary spec the provider fixes at the feed.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from watertap_engine_mcp.core.default_provider import DefaultBundle, DefaultProvider, RoleResolvedTopology
from watertap_engine_mcp.core.property_registry import PropertyPackageType
from watertap_engine_mcp.core.roles import UnitRole
MODIFIED_ADM1_COMPONENTS = ('S_su', 'S_aa', 'S_fa', 'S_va', 'S_bu', 'S_pro', 'S_ac', 'S_h2', 'S_ch4', 'S_IC', 'S_IN', 'S_IP', 'S_I', 'X_ch', 'X_pr', 'X_li', 'X_su', 'X_aa', 'X_fa', 'X_c4', 'X_pro', 'X_ac', 'X_h2', 'X_I', 'X_PHA', 'X_PP', 'X_PAO', 'S_K', 'S_Mg')
MODIFIED_ADM1_SFE_COMPONENTS = ('S_SO4', 'S_IS', 'X_hSRB', 'X_aSRB', 'X_pSRB', 'X_c4SRB', 'S_S0', 'S_Fe3', 'S_Fe2', 'X_HFO_H', 'X_HFO_L', 'X_HFO_old', 'X_HFO_HP', 'X_HFO_LP', 'X_HFO_HP_old', 'X_HFO_LP_old', 'X_FeS')
MODIFIED_ADM1_IRON_MINERAL_COMPONENTS = ('X_Fe3PO42', 'X_Siderite', 'X_Mackinawite', 'X_struv')
MODIFIED_ADM1_ORGANIC_S_COMPONENTS = ('X_Sorg',)
MODIFIED_ADM1_MICROAERATION_COMPONENTS = ('S_O2',)
MODIFIED_ADM1_FULL_COMPONENTS = ('H2O', *MODIFIED_ADM1_COMPONENTS, 'S_cat', 'S_an')

def _adm1_is_particulate(component: str) -> bool:
    """Return True for a particulate (biomass/solids) mADM1 component.

    The particulate species (``X_*``: carbohydrate/protein/lipid substrate, the
    acidogen/acetogen/methanogen biomass, inert particulates, and the PAO storage
    pools) are the fraction a thickener / membrane retains in the underflow /
    retentate. Everything else — the solvent, the soluble substrates/products, the
    inorganic carbon/nitrogen/phosphorus, the inert solubles, and the strong ions —
    follows the clarified (overflow / permeate) stream.
    """
    return component.startswith('X_')

def _srt_consistent_waste_fraction(*, feed_flow_m3_per_day: float, volume_liquid_m3: float, target_srt_days: float, soluble_clarified_fraction: float, particulate_clarified_fraction: float, fallback: float) -> float:
    """Return the feasible WAS split implied by SRT + separator hydraulics.

    For the retained-solids ADM1 families, the digester inlet flow is the fresh
    feed plus recycled retained liquid. With clarified soluble split ``c_s`` and
    particulate split ``c_p``:

        Q_dig = Q_f / (c_s + (1 - c_s) * w)
        V / SRT = w * (1 - c_p) * Q_dig

    Solving for the waste split ``w`` gives the seed used while the finite-SRT
    equation is relaxed for initialization. If the requested SRT is outside the
    physical range, return the configured fallback so the downstream structured
    infeasibility report remains honest.
    """
    try:
        q_feed = float(feed_flow_m3_per_day)
        volume = float(volume_liquid_m3)
        target = float(target_srt_days)
        c_soluble = float(soluble_clarified_fraction)
        c_particulate = float(particulate_clarified_fraction)
        fallback_value = float(fallback)
    except (TypeError, ValueError):
        return fallback
    if q_feed <= 0 or volume <= 0 or target <= 0:
        return fallback_value
    retained_soluble = 1.0 - c_soluble
    retained_particulate = 1.0 - c_particulate
    required_purge = volume / target
    denominator = retained_particulate * q_feed - required_purge * retained_soluble
    if denominator <= 0:
        return fallback_value
    seed = required_purge * c_soluble / denominator
    return max(0.0001, min(0.95, seed))
ADM1_RETAINED_SOLIDS_COMPONENTS = tuple((component for component in MODIFIED_ADM1_COMPONENTS if _adm1_is_particulate(component)))
EGSB_ZERO_RECIRCULATION_EPS = 1e-09
ADM1_FEED_DERIVATION_SOURCE = 'generic non-EBPR municipal primary/WAS mesophilic digester seed: low soluble VFA, particulate carbohydrate/protein/lipid/inert COD, small PAO/PHA/XPP, 2500 mg/L as CaCO3 strong-cation alkalinity, 20 d HRT'
ADM1_FEED_COMPONENT_BASIS: Dict[str, str] = {'S_su': 'kgCOD/m3', 'S_aa': 'kgCOD/m3', 'S_fa': 'kgCOD/m3', 'S_va': 'kgCOD/m3', 'S_bu': 'kgCOD/m3', 'S_pro': 'kgCOD/m3', 'S_ac': 'kgCOD/m3', 'S_h2': 'kgCOD/m3', 'S_ch4': 'kgCOD/m3', 'S_IC': 'kgC/m3', 'S_IN': 'kgN/m3', 'S_IP': 'kgP/m3', 'S_I': 'kgCOD/m3', 'X_ch': 'kgCOD/m3', 'X_pr': 'kgCOD/m3', 'X_li': 'kgCOD/m3', 'X_su': 'kgCOD/m3', 'X_aa': 'kgCOD/m3', 'X_fa': 'kgCOD/m3', 'X_c4': 'kgCOD/m3', 'X_pro': 'kgCOD/m3', 'X_ac': 'kgCOD/m3', 'X_h2': 'kgCOD/m3', 'X_I': 'kgCOD/m3', 'X_PHA': 'kgCOD/m3', 'X_PP': 'kgP/m3', 'X_PAO': 'kgCOD/m3', 'S_K': 'kgK/m3', 'S_Mg': 'kgMg/m3'}
ADM1_FEED_BOUNDARY_BASIS: Dict[str, str] = {'cations': 'kmol charge-equivalents/m3', 'anions': 'kmol charge-equivalents/m3'}
DEFAULT_ADM1_FEED_CONC_KG_M3: Dict[str, float] = {'S_su': 0.02, 'S_aa': 0.01, 'S_fa': 1e-09, 'S_va': 1e-09, 'S_bu': 1e-09, 'S_pro': 1e-09, 'S_ac': 0.005, 'S_h2': 1e-09, 'S_ch4': 1e-09, 'S_IC': 0.6, 'S_IN': 0.4, 'S_IP': 0.08, 'S_I': 0.03, 'X_ch': 2.0, 'X_pr': 2.0, 'X_li': 2.0, 'X_su': 1e-09, 'X_aa': 1e-09, 'X_fa': 1e-09, 'X_c4': 1e-09, 'X_pro': 1e-09, 'X_ac': 1e-09, 'X_h2': 1e-09, 'X_I': 3.0, 'X_PHA': 0.0005, 'X_PP': 0.002, 'X_PAO': 0.1, 'S_K': 0.12, 'S_Mg': 0.01}
DEFAULT_ADM1_TEMPERATURE_K = 308.15
ADM1_CREDIBILITY_NOTE: Dict[str, Any] = {'status': 'preliminary', 'grade': 'screening', 'basis': 'literature', 'model_scope': 'steady_state_digester_model', 'design_grade_heated_digester': False, 'temperature_model': 'adiabatic_feed_at_mesophilic_setpoint_or_heat_duty_controlled_target', 'kinetics_temperature_correction': False, 'heat_duty_control_available': True, 'temperature_target_governs': ['energy_balance', 'acid_base_equilibria', 'henry_law_gas_transfer'], 'deferred_to_a5': ['temperature_corrected_madm1_kinetics'], 'thermal_duty_scope': 'sensible/process thermal duty to bring the streams to setpoint (AD energy balance: heat + enthalpy transfer only); EXCLUDES heat of digestion / reaction and ambient shell losses -- NOT full heater/chiller sizing.', 'note': 'mADM1 digestion preset: a structurally valid steady-state digester model (DOF=0, solver-determined pH / vapor H2 / trace ions, preserved headspace pressure). It is NOT a design-grade heated-digester sizing. By default the reactor is adiabatic (feed enters at the mesophilic setpoint). When an adm1_temperature_target is applied (A5-PR1) the outlet temperature is held at the setpoint by SOLVING the external thermal duty (reported as a SIGNED thermal duty in kW -- positive = heating, negative = cooling when the setpoint is below the inlet temperature), not by pinning the temperature; the setpoint governs the energy balance, the acid/base equilibria, and Henry-law gas transfer, while the modified-ADM1 BIOLOGICAL kinetics remain constant-parameter (Arrhenius temperature correction is the remaining A5 deferral). The reported duty is a sensible/process duty to setpoint -- it EXCLUDES heat of digestion and ambient losses, so it is not full heater sizing. Do not present as a design-grade heated-digester result.'}
DEFAULT_ADM1_HEADSPACE_PRESSURE_PA = 101325.0

@dataclass
class ADM1DigestionConfig:
    """Configuration for an ADM1 / mADM1 anaerobic-digestion preset (UASB)."""
    property_package: PropertyPackageType = PropertyPackageType.MODIFIED_ADM1
    feed_flow_m3_per_day: float = 259.2
    feed_temperature_K: float = DEFAULT_ADM1_TEMPERATURE_K
    feed_pressure_Pa: float = 101325.0
    feed_conc_kg_m3: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_ADM1_FEED_CONC_KG_M3))
    feed_cations_kmol_m3: float = 0.05
    feed_anions_kmol_m3: float = 0.0285714
    volume_liquid_m3: float = 5200.0
    volume_vapor_m3: float = 300.0
    reactor_height_m: Optional[float] = None
    headspace_pressure_Pa: float = DEFAULT_ADM1_HEADSPACE_PRESSURE_PA
    sulfur_iron: Optional[bool] = None
    organic_sulfur: Optional[bool] = None
    oxidant_inlet: Optional[Dict[str, Any]] = None
    headspace_wetted_area_m2: Optional[float] = None

    def hydraulic_retention_time_days(self) -> Optional[float]:
        """Return the digester HRT (= SRT for a CSTR-style AD), in days."""
        flow = float(self.feed_flow_m3_per_day or 0.0)
        if flow <= 0.0:
            return None
        return float(self.volume_liquid_m3) / flow

@dataclass
class EGSBDigestionConfig(ADM1DigestionConfig):
    """EGSB (expanded granular sludge bed) preset config.

    Adds the high upflow liquid recirculation that distinguishes an EGSB from a
    plain UASB: the digester liquid outlet is split back to the inlet mixer at
    ``recirculation_ratio`` times the fresh feed flow. The recirculation is closed
    by the ADM1 recycle tear (damped successive substitution). It is composition-
    neutral for a single perfectly mixed reactor, so it is the cleanest recycle
    family to reach strict-optimal; the structural value is the upflow topology +
    the closed tear.
    """
    recirculation_ratio: float = 2.0

    def recirculation_split_fraction(self) -> float:
        """Fraction of the digester liquid outlet routed to recirculation.

        At steady state the digester liquid outlet ≈ fresh feed + recirculation,
        so the recirculation split is ``R / (R + 1)`` for a recirculation:feed
        ratio ``R``; the complementary ``forward`` outlet carries the net effluent.
        """
        r = max(float(self.recirculation_ratio), 0.0)
        return r / (r + 1.0)

@dataclass
class AnMBRDigestionConfig(ADM1DigestionConfig):
    """AnMBR (anaerobic membrane bioreactor) preset config.

    A biomass-retaining membrane on the digester liquid outlet (permeate =
    biomass-free effluent, retentate = concentrated biomass) plus a small waste
    bleed off the retentate that sets the solids retention; the rest of the
    retentate recycles to the inlet mixer (the membrane-retentate tear). The
    membrane split is the solids-separation spec; the bleed split is the SRT/WAS
    spec — exactly one spec per separation/SRT DOF.
    """
    membrane_soluble_permeate_fraction: float = 0.25
    membrane_particulate_permeate_fraction: float = 0.15
    membrane_particulate_passage: Optional[float] = None
    waste_fraction: float = 0.94
    target_srt_days: float = 25.0
    retained_solids_max_kg_m3: float = 25.0

    def solids_clarified_fractions(self) -> tuple[float, float]:
        """Return ``(soluble, particulate)`` permeate component-flow split fractions.

        With a supplied ``membrane_particulate_passage`` the particulate split is
        soluble_split x passage (permeate particulate concentration = passage x the
        digester's); otherwise the flow-split field is used as given.
        """
        soluble = float(self.membrane_soluble_permeate_fraction)
        if self.membrane_particulate_passage is not None:
            return (soluble, soluble * float(self.membrane_particulate_passage))
        return (soluble, float(self.membrane_particulate_permeate_fraction))

    def srt_consistent_waste_fraction(self) -> float:
        """Return the WAS split that satisfies the finite-SRT hydraulic balance."""
        soluble, particulate = self.solids_clarified_fractions()
        return _srt_consistent_waste_fraction(feed_flow_m3_per_day=float(self.feed_flow_m3_per_day), volume_liquid_m3=float(self.volume_liquid_m3), target_srt_days=float(self.target_srt_days), soluble_clarified_fraction=soluble, particulate_clarified_fraction=particulate, fallback=float(self.waste_fraction))

@dataclass
class AnaerobicContactDigestionConfig(ADM1DigestionConfig):
    """Anaerobic-contact (CSTR + thickener + sludge return) preset config.

    A gravity thickener on the digester liquid outlet (clarified overflow =
    effluent, thickened underflow = concentrated biomass) with a sludge splitter
    on the underflow returning biomass to the inlet mixer (RAS) and bleeding the
    remainder to waste. The thickener split is the solids-separation spec; the
    sludge-return split is the RAS/WAS spec.
    """
    thickener_soluble_overflow_fraction: float = 0.25
    thickener_particulate_overflow_fraction: float = 0.15
    waste_fraction: float = 0.94
    target_srt_days: float = 25.0
    retained_solids_max_kg_m3: float = 25.0

    def solids_clarified_fractions(self) -> tuple[float, float]:
        """Return ``(soluble, particulate)`` overflow split fractions."""
        return (float(self.thickener_soluble_overflow_fraction), float(self.thickener_particulate_overflow_fraction))

    def srt_consistent_waste_fraction(self) -> float:
        """Return the WAS split that satisfies the finite-SRT hydraulic balance."""
        return _srt_consistent_waste_fraction(feed_flow_m3_per_day=float(self.feed_flow_m3_per_day), volume_liquid_m3=float(self.volume_liquid_m3), target_srt_days=float(self.target_srt_days), soluble_clarified_fraction=float(self.thickener_soluble_overflow_fraction), particulate_clarified_fraction=float(self.thickener_particulate_overflow_fraction), fallback=float(self.waste_fraction))

@dataclass
class TwoStageDigestionConfig(ADM1DigestionConfig):
    """Phase-separated two-stage (acidogenic -> methanogenic) preset config.

    An acidogenic reactor (hydrolysis/acidogenesis) feeds a methanogenic reactor;
    each reactor carries its OWN biogas headspace. The base ``volume_liquid_m3`` /
    ``volume_vapor_m3`` size the methanogenic reactor; the acid reactor is sized
    separately below. The base case is a forward acid->methane cascade (no recycle).

    BOTH stages = explicitly ISOTHERMAL, SEPARATELY-HEATED reactors (A5-PR1). A real
    phase-separated two-stage digester temperature-controls the acidogenic stage
    INDEPENDENTLY of the methanogenic stage (each phase is held at its own setpoint —
    e.g. a warmer thermophilic-leaning hydrolysis stage feeding a mesophilic methane
    stage). We model that physically-legitimate control as a HEAT-DUTY-CONTROLLED
    isothermal target on EACH stage: the acid reactor's liquid-outlet temperature is
    held at ``acid_temperature_K`` and the methanogenic reactor's at
    ``feed_temperature_K``, each by SOLVING the reactor's external thermal duty
    (``heat[0]``, reported as a thermal duty in kW — signed, so a stage held BELOW its
    inlet temperature reports a COOLING duty) rather than by pinning the temperature
    variable. These are INTENTIONAL setpoints, surfaced in the credibility/metadata
    stamp. This is NOT a hidden boundary-state pin: each reactor's vapor ``S_h2``,
    reaction-block H+ (pH), and product trace ions stay solver-determined. So the
    honest claim is "both stages = heat-duty-controlled isothermal setpoints
    (intentional, each reporting a solved thermal duty); all OTHER boundary states
    solver-determined" — NOT "no pins" and NOT "methanogenic stage adiabatic".

    Acid-reactor sizing & the strict-optimal regime: a genuine phase-separated
    digester runs the acid stage at a SHORT HRT so the slow methanogens wash out and
    the stage soures (VFA-rich, depressed pH). That washed-out / soured basin is
    numerically stiff (near-zero methanogen biomass in the Monod kinetics) and the
    steady-state solve converges to a locally infeasible point from the warm start
    — a KNOWN convergence gap. The packaged default therefore sizes the acid stage
    at the methanogenic scale (a conservative, lightly phase-separated staged
    digester that the cascade solves to a strict-optimal healthy basin); set a short
    ``acid_volume_liquid_m3`` to request an aggressively phase-separated train
    (structurally valid, DOF=0, but expect a sour/infeasible solve until the
    multi-basin AD warm-start is hardened in a follow-up).
    """
    acid_volume_liquid_m3: float = 3400.0
    acid_volume_vapor_m3: float = 300.0
    acid_temperature_K: float = DEFAULT_ADM1_TEMPERATURE_K
ADM1_ACTIVE_BIOMASS_FLOOR_KG_M3 = 0.005
ADM1_SOLUBLE_PRODUCT_CEILING_KG_M3 = 0.5
ADM1_DEGRADABLE_COD_CONVERSION_FLOOR = 0.35
ADM1_COD_BASIS_FEED_COMPONENTS = tuple((component for component, basis in ADM1_FEED_COMPONENT_BASIS.items() if basis == 'kgCOD/m3'))
ADM1_TECHNOLOGY_ENVELOPES: Dict[str, Dict[str, Any]] = {'uasb': {'olr_kg_cod_m3_d': (4.0, 15.0), 'hrt_days': (0.25, 2.0), 'upflow_velocity_m_h': (0.5, 1.5)}, 'egsb': {'olr_kg_cod_m3_d': (10.0, 30.0), 'hrt_days': (0.08, 0.5), 'upflow_velocity_m_h': (4.0, 10.0)}, 'cstr': {'olr_kg_cod_m3_d': (1.0, 5.0), 'hrt_days': (10.0, 40.0), 'upflow_velocity_m_h': None}}
ADM1_TECHNOLOGY_ENVELOPE_NOTES: Dict[str, str] = {'cstr': 'adm1_uasb is a completely-mixed mADM1 CSTR (no granular-bed retention physics); held to the CSTR design envelope. Supply reactor_height_m to claim (and be checked against) the UASB envelope.', 'uasb': 'UASB envelope applied because bed geometry (reactor_height_m) was supplied; note the engine model still carries NO granular-bed retention physics (SRT = HRT).', 'egsb': 'EGSB envelope; upflow velocity includes the recirculation flow (Q_up = Q_feed * (1 + recirculation_ratio)). The engine model carries NO granular-bed retention physics (SRT = HRT).'}

def adm1_feed_total_cod_kg_m3(feed_conc_kg_m3: Dict[str, float]) -> float:
    """Sum the kg COD/m3-basis feed states (the OLR numerator)."""
    total = 0.0
    for component in ADM1_COD_BASIS_FEED_COMPONENTS:
        try:
            total += float(feed_conc_kg_m3.get(component, 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
    return total

def _adm1_envelope_technology(config: ADM1DigestionConfig) -> Optional[str]:
    """Resolve which technology envelope a preset config is held to.

    EGSB configs carry the EGSB envelope. The retained-biomass / two-stage
    families (AnMBR / contact / two-stage) have no high-rate upflow envelope
    defined yet — the gate abstains rather than inventing one. A plain
    ADM1DigestionConfig (the adm1_uasb preset) is honestly a CSTR unless real
    bed geometry is supplied (see ADM1_TECHNOLOGY_ENVELOPE_NOTES).
    """
    if isinstance(config, EGSBDigestionConfig):
        return 'egsb'
    if isinstance(config, (AnMBRDigestionConfig, AnaerobicContactDigestionConfig, TwoStageDigestionConfig)):
        return None
    if getattr(config, 'reactor_height_m', None) is not None:
        return 'uasb'
    return 'cstr'

def _envelope_finding(*, check: str, technology: str, value: Optional[float], bounds: Optional[tuple], units: str, reason: Optional[str]=None) -> Dict[str, Any]:
    if value is None or bounds is None:
        status = 'not_evaluated'
    else:
        status = 'pass' if bounds[0] <= value <= bounds[1] else 'fail'
    finding: Dict[str, Any] = {'check': check, 'status': status, 'value': value, 'bounds': list(bounds) if bounds is not None else None, 'technology': technology, 'units': units}
    if status == 'not_evaluated':
        finding['reason'] = reason or 'insufficient design basis to evaluate'
    return finding

def check_adm1_technology_envelope(config: ADM1DigestionConfig, solved_concentrations_or_payload: Optional[Dict[str, Any]]=None) -> List[Dict[str, Any]]:
    """Post-solve technology-envelope gate for an ADM1 digestion config (A-5).

    Screens the digester DESIGN BASIS (feed COD load, geometry, hydraulics)
    against the routed technology's design envelope: OLR (feed COD * Q / V_liq),
    HRT (:meth:`ADM1DigestionConfig.hydraulic_retention_time_days`), and — when
    ``reactor_height_m`` is supplied — the superficial upflow velocity
    v_up = Q_up * height / V_liq [m/h] (EGSB includes the recirculation flow in
    Q_up). Absent geometry is SURFACED as a not_evaluated finding, never
    silently passed.

    ``solved_concentrations_or_payload`` may carry the session's stamped
    ``design_parameters`` payload; a numeric ``feed_total_cod_kg_m3`` there
    takes precedence over recomputing from ``config.feed_conc_kg_m3`` (a
    reconstructed config may not see session-side feed overrides). The solved
    effluent concentrations are accepted through the same argument for future
    effluent-coupled envelope checks without an API break; the current three
    checks are design-basis checks.

    Returns a list of structured findings:
    ``{"check", "status": "pass"|"fail"|"not_evaluated", "value", "bounds",
    "technology", "units"[, "reason"]}``. Empty for families with no envelope
    defined (AnMBR / contact / two-stage).
    """
    technology = _adm1_envelope_technology(config)
    if technology is None:
        return []
    envelope = ADM1_TECHNOLOGY_ENVELOPES[technology]
    payload = dict(solved_concentrations_or_payload or {})
    feed_cod = payload.get('feed_total_cod_kg_m3')
    try:
        feed_cod = float(feed_cod) if feed_cod is not None else None
    except (TypeError, ValueError):
        feed_cod = None
    if feed_cod is None:
        feed_cod = adm1_feed_total_cod_kg_m3(config.feed_conc_kg_m3 or {})
    flow_m3_d = float(config.feed_flow_m3_per_day or 0.0)
    volume_m3 = float(config.volume_liquid_m3 or 0.0)
    olr = feed_cod * flow_m3_d / volume_m3 if volume_m3 > 0.0 else None
    findings = [_envelope_finding(check='olr', technology=technology, value=olr, bounds=envelope['olr_kg_cod_m3_d'], units='kg COD/m3/d', reason='digester liquid volume not positive'), _envelope_finding(check='hrt', technology=technology, value=config.hydraulic_retention_time_days(), bounds=envelope['hrt_days'], units='d', reason='feed flow not positive')]
    height_m = getattr(config, 'reactor_height_m', None)
    v_up_bounds = envelope['upflow_velocity_m_h']
    if height_m is None or v_up_bounds is None or volume_m3 <= 0.0:
        findings.append(_envelope_finding(check='upflow_velocity', technology=technology, value=None, bounds=v_up_bounds, units='m/h', reason='reactor geometry not supplied' if height_m is None else 'digester liquid volume not positive' if volume_m3 <= 0.0 else 'upflow velocity is not a design basis for this technology'))
    else:
        recirculation = max(float(getattr(config, 'recirculation_ratio', 0.0) or 0.0), 0.0) if technology == 'egsb' else 0.0
        q_up_m3_h = flow_m3_d / 24.0 * (1.0 + recirculation)
        findings.append(_envelope_finding(check='upflow_velocity', technology=technology, value=q_up_m3_h * float(height_m) / volume_m3, bounds=v_up_bounds, units='m/h'))
    return findings

def adm1_technology_envelope_from_design_parameters(design_parameters: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Build the ``technology_fidelity`` block from a session's stamped
    ``design_parameters`` (the ``Adm1PresetProvider`` bundle).

    Reconstructs the envelope-relevant config slice (flow, liquid volume,
    optional bed geometry, EGSB recirculation) and runs
    :func:`check_adm1_technology_envelope` with the stamped payload (so the
    stamped ``feed_total_cod_kg_m3`` — which reflects any feed overrides —
    feeds the OLR). Returns ``None`` for families with no envelope defined
    (AnMBR / contact / two-stage), or when the stamped parameters cannot
    reconstruct a numeric design basis.

    Any "fail" finding forces ``design_grade_convergence=False`` in the block:
    the solve may still be a strict IPOPT optimum (``solution_quality`` stays
    truthful about the solver), but the converged basis is outside the routed
    technology's design envelope and must not be presented as a design-grade
    result for that technology.
    """
    params = dict(design_parameters or {})
    family = str(params.get('adm1_family') or '').strip().lower()
    if family not in ('uasb', 'egsb'):
        return None
    try:
        kwargs: Dict[str, Any] = {'feed_flow_m3_per_day': float(params.get('feed_flow_m3_per_day') or 0.0), 'volume_liquid_m3': float(params.get('digester_volume_liquid_m3') or 0.0)}
    except (TypeError, ValueError):
        return None
    height = params.get('reactor_height_m')
    if height is not None:
        try:
            kwargs['reactor_height_m'] = float(height)
        except (TypeError, ValueError):
            pass
    if family == 'egsb':
        recirculation = params.get('recirculation_ratio')
        if recirculation is not None:
            try:
                kwargs['recirculation_ratio'] = float(recirculation)
            except (TypeError, ValueError):
                pass
        config: ADM1DigestionConfig = EGSBDigestionConfig(**kwargs)
    else:
        config = ADM1DigestionConfig(**kwargs)
    findings = check_adm1_technology_envelope(config, params)
    if not findings:
        return None
    technology = findings[0]['technology']
    failed_checks = [f['check'] for f in findings if f['status'] == 'fail']
    return {'technology': technology, 'findings': findings, 'failed_checks': failed_checks, 'design_grade_convergence': not failed_checks, 'note': ADM1_TECHNOLOGY_ENVELOPE_NOTES[technology]}

@dataclass(frozen=True)
class Adm1FamilyProfile:
    """The structural family knobs of an ADM1 digestion preset.

    These are the parts of a preset the role graph cannot recover on its own:
    which structural digestion roles the preset *intends* to carry (a biogas
    take-off gas phase, a recuperation/solids-return loop, a biomass-retaining
    solids separation, a separate acidogenic stage) beyond the always-present
    methanogenic reactor, plus the outlet names the recycle/solids splitters use.
    The provider derives the *present* structural family from the graph's roles for
    emission; this profile is the design intent the role graph cannot recover.

    ``family`` is the preset identity (``uasb`` / ``egsb`` / ``anmbr`` /
    ``contact`` / ``two_stage``). ``recycle_recycle_outlet`` /
    ``recycle_waste_outlet`` name the totalFlow recuperation splitter's outlets:
    EGSB fixes the recirculation outlet directly; the AnMBR/contact bleed splitter
    fixes the waste (WAS) outlet and the recycle is the complement.
    ``solids_clarified_outlet`` names the componentFlow solids separator's
    clarified (overflow / permeate) outlet — the side the biomass-retention split
    is fixed on.
    """
    include_gas_phase: bool = True
    include_recuperation: bool = False
    include_solids_separation: bool = False
    include_acidogenic: bool = False
    family: str = 'uasb'
    recycle_recycle_outlet: str = 'recirculation'
    recycle_waste_outlet: Optional[str] = None
    solids_clarified_outlet: Optional[str] = None

@dataclass
class ADM1TemplateUnits:
    """Unit IDs used by the UASB digestion template.

    Field names map 1:1 to structural roles (see ``FIELD_ROLES``). Defaults are
    the canonical preset names; callers may override the ids to emit the same
    role-tagged priors for a from-graph train with project-specific names.
    """
    feed: str = 'Feed'
    digester: str = 'Digester'
    effluent: str = 'LiquidEffluent'
    biogas: str = 'Biogas'

    def role_for(self, unit_id: str) -> Optional[str]:
        """Return the structural role string for a unit id in this template."""
        for field_name, role in FIELD_ROLES.items():
            if getattr(self, field_name) == unit_id:
                return role.value
        return None
FIELD_ROLES = {'feed': UnitRole.FEED, 'digester': UnitRole.ANAEROBIC_METHANOGENIC, 'effluent': UnitRole.PRODUCT_EFFLUENT, 'biogas': UnitRole.ANAEROBIC_GAS_PHASE}

def _feed_conc_for_property_package(config: ADM1DigestionConfig) -> Dict[str, float]:
    """Return the feed concentration dict for the configured ADM1 package."""
    feed = dict(config.feed_conc_kg_m3)
    components = MODIFIED_ADM1_COMPONENTS
    sfe = MODIFIED_ADM1_COMPONENTS + MODIFIED_ADM1_SFE_COMPONENTS + MODIFIED_ADM1_IRON_MINERAL_COMPONENTS
    if config.property_package == PropertyPackageType.MODIFIED_ADM1_SFE:
        components = sfe
    elif config.property_package == PropertyPackageType.MODIFIED_ADM1_SFE_ORGANIC_S:
        components = sfe + MODIFIED_ADM1_ORGANIC_S_COMPONENTS
    elif config.property_package == PropertyPackageType.MODIFIED_ADM1_SFE_MICROAERATION:
        components = sfe + MODIFIED_ADM1_ORGANIC_S_COMPONENTS + MODIFIED_ADM1_MICROAERATION_COMPONENTS
    for component in components:
        feed.setdefault(component, 1e-09)
    return feed

class Adm1PresetProvider(DefaultProvider):
    """Default provider for the ADM1 / mADM1 anaerobic-digestion family.

    Holds the preset config + :class:`Adm1FamilyProfile` and, given a
    :class:`RoleResolvedTopology`, yields the full :class:`DefaultBundle` keyed on
    the graph's roles. The same provider instance defaults a template-built
    topology (canonical names) and a from-graph build (project names) to a
    role-identical bundle — the topology-source-agnostic contract A1b establishes.
    """

    def __init__(self, profile: Adm1FamilyProfile, config: ADM1DigestionConfig):
        self.profile = profile
        self.config = config

    @staticmethod
    def _digester_ids(topology: RoleResolvedTopology) -> List[str]:
        """Return every anaerobic digester id in acidogenic -> methanogenic order.

        PR1 carries one methanogenic reactor; the acidogenic role is resolved
        first so a future phase-separated train (PR2) inherits the same ordering
        without changing the volume-fix or scaling logic.
        """
        return topology.ordered_stage_ids([UnitRole.ANAEROBIC_ACIDOGENIC, UnitRole.ANAEROBIC_METHANOGENIC])

    def provide(self, topology: RoleResolvedTopology) -> DefaultBundle:
        return DefaultBundle(dof_fixes=self._dof_fixes(topology), scaling_factors=self._scaling_factors(topology), feed_states=self._feed_states(topology), process_constraints=self._process_constraints(topology), design_parameters=self._design_parameters(topology), metadata=self._metadata(), solver_options=self._solver_options(), initialization_order=list(topology.ordered_unit_ids), recycle_ratio_declarations={})

    def _dof_fixes(self, topology: RoleResolvedTopology) -> List[Dict[str, Any]]:
        """Fix the digester geometry + one spec per recycle/SRT DOF.

        The digester equipment fixes are the per-reactor liquid + vapor volumes
        (acidogenic reactors use the ``acid_*`` sizing, the methanogenic reactor
        uses the base sizing). The vapor S_h2, the reaction-block H+, and the
        product trace ions are deliberately NOT fixed here on ANY reactor: they are
        solver-determined by the gas-phase property equations, the ADM1
        electroneutrality constraint, and the component balances. The methanogenic
        reactor's liquid-outlet temperature is likewise not fixed (the AD runs
        adiabatically, so the energy balance closes without it; pinning it would
        over-constrain the assembled flowsheet). The ONE documented exception is the
        two-stage ACIDOGENIC reactor: it is modeled as an explicitly isothermal,
        separately-heated stage whose liquid-outlet temperature IS fixed to the
        intentional ``acid_temperature_K`` setpoint (see ``TwoStageDigestionConfig``
        and the credibility stamp). That is a legitimate process control spec, not a
        hidden boundary pin — the acid reactor's vapor S_h2 / reaction H+ / trace
        ions still stay solver-determined.

        For the recycle families this also fixes one separation spec per recycle/SRT
        degree of freedom: the biomass-retaining solids separator's per-component
        split (the separation property, analogous to the ASM2D clarifier splits).
        EGSB fixes its recirculation split directly. The AnMBR/contact WAS split is
        deliberately not fixed in the base template: the provider emits a finite-SRT
        retained-solids process constraint that frees that split and closes the same
        DOF with a physical inert-purge equation.
        """
        config = self.config
        fixes: List[Dict[str, Any]] = []
        acid_ids = set(topology.all_ids(UnitRole.ANAEROBIC_ACIDOGENIC))
        for digester_id in self._digester_ids(topology):
            if digester_id in acid_ids:
                volume_liquid = getattr(config, 'acid_volume_liquid_m3', config.volume_liquid_m3)
                volume_vapor = getattr(config, 'acid_volume_vapor_m3', config.volume_vapor_m3)
            else:
                volume_liquid = config.volume_liquid_m3
                volume_vapor = config.volume_vapor_m3
            fixes.append({'unit_id': digester_id, 'var_name': 'volume_liquid[0]', 'value': volume_liquid})
            fixes.append({'unit_id': digester_id, 'var_name': 'volume_vapor[0]', 'value': volume_vapor})
        fixes.extend(self._oxidant_inlet_fixes(topology))
        fixes.extend(self._solids_separation_fixes(topology))
        fixes.extend(self._recuperation_fixes(topology))
        return fixes

    def _oxidant_inlet_fixes(self, topology: RoleResolvedTopology) -> List[Dict[str, Any]]:
        """The dosed oxidant gas and the headspace wetted area, fixed the way the volumes are (micro-aeration only).

        The ``vapor_inlet`` port is a DEFINED state: flow, temperature, pressure and every gas concentration are the
        stated oxidant's (``oxidant_inlet_state``), and the wetted area is stage (b)'s geometry. Without a stated
        oxidant there is no port and no fix. The flow is the STATED dose: ``native_digestion.solve_digester`` holds it
        at zero while it walks the digester onto its branch and steps it in afterwards (the server's own path)."""
        config = self.config
        if getattr(config, 'oxidant_inlet', None) is None:
            return []
        from watertap_engine_mcp.chemistry.modified_adm1_microaeration import oxidant_inlet_state
        state = oxidant_inlet_state(config.oxidant_inlet)
        fixes: List[Dict[str, Any]] = []
        for digester_id in topology.all_ids(UnitRole.ANAEROBIC_METHANOGENIC):
            fixes.extend([{'unit_id': digester_id, 'var_name': 'vapor_inlet_state[0].flow_vol', 'value': state['flow_vol_m3_s']}, {'unit_id': digester_id, 'var_name': 'vapor_inlet_state[0].temperature', 'value': state['temperature_K']}, {'unit_id': digester_id, 'var_name': 'vapor_inlet_state[0].pressure', 'value': state['pressure_Pa']}])
            fixes.extend(({'unit_id': digester_id, 'var_name': f'vapor_inlet_state[0].conc_mass_comp[{gas}]', 'value': value} for gas, value in state['conc_kg_m3'].items()))
            fixes.append({'unit_id': digester_id, 'var_name': 'headspace_wetted_area[0]', 'value': float(config.headspace_wetted_area_m2)})
        return fixes

    def _solids_separation_fixes(self, topology: RoleResolvedTopology) -> List[Dict[str, Any]]:
        """Fix the biomass-retaining (componentFlow) solids separator's splits.

        For the AnMBR membrane / contact thickener: the clarified (permeate /
        overflow) outlet keeps the solubles + solvent + strong ions and rejects the
        particulate biomass. One ``split_fraction[0, clarified, comp]`` per
        component is fixed (the separation property); the thickened/retentate side
        is the complement (``sum == 1``). This is the single solids-separation spec.
        """
        if not self.profile.include_solids_separation:
            return []
        separator_id = topology.single_id(UnitRole.ANAEROBIC_THICKENER)
        clarified = self.profile.solids_clarified_outlet
        if separator_id is None or clarified is None:
            return []
        soluble, particulate = self.config.solids_clarified_fractions()
        fixes: List[Dict[str, Any]] = []
        for component in MODIFIED_ADM1_FULL_COMPONENTS:
            value = particulate if _adm1_is_particulate(component) else soluble
            fixes.append({'unit_id': separator_id, 'var_name': f'split_fraction[0,{clarified},{component}]', 'value': value})
        return fixes

    def _recuperation_fixes(self, topology: RoleResolvedTopology) -> List[Dict[str, Any]]:
        """Fix the totalFlow recuperation splitter's single recycle/SRT spec.

        EGSB: the recirculation outlet split fraction (``R / (R + 1)``) — the
        recirculation:feed ratio. AnMBR / contact: the WAS outlet is closed by the
        default SRT process constraint, so no fixed split is emitted here unless a
        custom config explicitly disables ``target_srt_days``.
        """
        if not self.profile.include_recuperation:
            return []
        separator_id = topology.single_id(UnitRole.ANAEROBIC_RECUPERATION)
        if separator_id is None:
            return []
        if self.profile.recycle_waste_outlet is not None:
            if getattr(self.config, 'target_srt_days', None) is not None:
                return []
            return [{'unit_id': separator_id, 'var_name': f'split_fraction[0,{self.profile.recycle_waste_outlet}]', 'value': float(getattr(self.config, 'waste_fraction', 0.05))}]
        recirc = self.config.recirculation_split_fraction()
        return [{'unit_id': separator_id, 'var_name': f'split_fraction[0,{self.profile.recycle_recycle_outlet}]', 'value': recirc}]

    def _process_constraints(self, topology: RoleResolvedTopology) -> List[Dict[str, Any]]:
        """Emit finite-SRT recycle closures and heat-duty temperature targets.

        A phase-separated two-stage digester is a SEPARATELY-HEATED train: the
        acidogenic and the methanogenic stage are each held at their own isothermal
        setpoint. A4-PR2 anchored only the acid stage with an outlet-temperature
        dof_fix (the acid liquid-outlet temperature PINNED to ``acid_temperature_K``)
        and left the methanogenic stage adiabatic. A5-PR1 models BOTH stages
        physically as heat-duty-controlled isothermal reactors: the
        ``adm1_temperature_target`` process constraint deactivates each AD's adiabatic
        energy closure, frees its thermal duty (``heat[0]``), and holds its outlet
        temperature at the setpoint by SOLVING that duty (each reported as a SIGNED
        thermal duty -- heating if positive, cooling if the setpoint is below the
        stage inlet) rather than pinning the temperature variable. The acid stage uses
        ``acid_temperature_K``; the methanogenic stage uses the global mADM1 design
        temperature (``feed_temperature_K``). The setpoints govern the energy balance
        + acid/base equilibria + Henry-law gas transfer; the mADM1 kinetics stay
        constant-parameter (not Arrhenius temperature-corrected).

        AnMBR/contact also carry a default finite-SRT retained-solids closure: the
        WAS split is freed and one SRT equation purges retained solids, including
        inert ``X_I``, at a finite inventory residence time. That is the structural
        replacement for the old fixed bleed split, which forced pathological retained
        solids concentrations. Only the explicitly phase-separated two-stage train is
        heated by default; the single-reactor (UASB) and recycle families stay
        adiabatic unless a caller supplies an ``adm1_temperature_target`` of its own.
        """
        config = self.config
        constraints: List[Dict[str, Any]] = []
        constraints.extend(self._srt_process_constraints(topology))
        if not self.profile.include_acidogenic:
            return constraints
        for acid_id in topology.all_ids(UnitRole.ANAEROBIC_ACIDOGENIC):
            constraints.append({'type': 'adm1_temperature_target', 'name': f'acid_isothermal_{acid_id}', 'target_unit': acid_id, 'target_K': float(getattr(config, 'acid_temperature_K', config.feed_temperature_K))})
        for meth_id in topology.all_ids(UnitRole.ANAEROBIC_METHANOGENIC):
            constraints.append({'type': 'adm1_temperature_target', 'name': f'methanogenic_isothermal_{meth_id}', 'target_unit': meth_id, 'target_K': float(config.feed_temperature_K)})
        return constraints

    def _srt_process_constraints(self, topology: RoleResolvedTopology) -> List[Dict[str, Any]]:
        """Return the default finite-SRT closure for biomass-retaining recycle trains."""
        target_days = getattr(self.config, 'target_srt_days', None)
        if target_days is None or self.profile.recycle_waste_outlet is None:
            return []
        waste_unit = topology.single_id(UnitRole.ANAEROBIC_RECUPERATION)
        if waste_unit is None:
            return []
        reactor_units = list(topology.all_ids(UnitRole.ANAEROBIC_METHANOGENIC))
        if not reactor_units:
            return []
        retained_streams: List[Dict[str, str]] = []
        separator_id = topology.single_id(UnitRole.ANAEROBIC_THICKENER)
        retained_outlet = {'permeate': 'retentate', 'overflow': 'underflow'}.get(str(self.profile.solids_clarified_outlet))
        if separator_id is not None and retained_outlet is not None:
            retained_streams.append({'unit': separator_id, 'port': retained_outlet})
        max_solids = float(getattr(self.config, 'retained_solids_max_kg_m3', 25.0))
        components = list(ADM1_RETAINED_SOLIDS_COMPONENTS)
        waste_seed = float(getattr(self.config, 'waste_fraction', 0.2))
        srt_seed = getattr(self.config, 'srt_consistent_waste_fraction', None)
        if callable(srt_seed):
            waste_seed = float(srt_seed())
        return [{'type': 'srt', 'name': f'{self.profile.family}_finite_srt_inert_purge', 'target_days': float(target_days), 'reactor_units': reactor_units, 'biomass_components': components, 'waste_unit': waste_unit, 'waste_port': self.profile.recycle_waste_outlet, 'waste_split_seed': waste_seed, 'inventory_scale_kg': float(self.config.volume_liquid_m3) * max_solids, 'retained_streams': retained_streams, 'retained_component_caps_kg_m3': {component: max_solids for component in components}}]

    def _scaling_factors(self, topology: RoleResolvedTopology) -> List[Dict[str, Any]]:
        """Return reactor-volume scaling hints.

        The digester state-variable conditioning (per-species concentrations,
        flow/temperature/pressure, the vapor headspace) is owned by the ADM1
        scaler (``solver.scaler.apply_adm1_species_scaling``), applied after the
        recursive IDAES pass so it overrides the property-block defaults. The
        provider only hints the reactor volume magnitude here.
        """
        return [{'unit_id': digester_id, 'var_name': 'volume_AD', 'factor': 0.01} for digester_id in self._digester_ids(topology)]

    def _feed_states(self, topology: RoleResolvedTopology) -> Dict[str, Dict[str, Any]]:
        """Return the feed boundary state, including the strong-ion difference.

        ``cations`` / ``anions`` are separate ADM1 state vars (not in
        conc_mass_comp); fixing them at the feed is what makes the reaction-block
        H+ (pH) solver-determined via electroneutrality rather than a pinned
        boundary. ``S_K`` / ``S_Mg`` are carried in conc_mass_comp.
        """
        config = self.config
        feed_id = topology.single_id(UnitRole.FEED)
        if feed_id is None:
            return {}
        state_args: Dict[str, Any] = {'flow_vol': {0: config.feed_flow_m3_per_day / 86400.0}, 'temperature': {0: config.feed_temperature_K}, 'pressure': {0: config.feed_pressure_Pa}, 'conc_mass_comp': _feed_conc_for_property_package(config), 'cations': {0: config.feed_cations_kmol_m3}, 'anions': {0: config.feed_anions_kmol_m3}}
        return {feed_id: {'state_args': state_args}}

    def _credibility(self) -> Dict[str, Any]:
        """Return the honest credibility stamp for this preset.

        Base note is the shared steady-state-digester scope (NOT heated-design
        grade). For the two-stage train BOTH the acidogenic and the methanogenic
        stage are HEAT-DUTY-CONTROLLED isothermal reactors (A5-PR1): each stage's
        liquid-outlet temperature is held at its setpoint (acid =
        ``acid_temperature_K``, methanogenic = ``feed_temperature_K``) by SOLVING the
        stage's external thermal duty (reported as a signed thermal duty in kW —
        cooling if the stage is held below its inlet temperature) rather than pinning
        the temperature variable. The stamp records this as a documented design spec
        for a separately-heated phase-separated digester -- not a hidden boundary pin.
        This keeps the "no design-grade heated-digester claim" contract honest: the
        setpoints govern the energy balance + acid/base equilibria + Henry-law gas
        transfer, while the modified-ADM1 biological kinetics stay constant-parameter.
        """
        note = dict(ADM1_CREDIBILITY_NOTE)
        note['thermal_duty_scope'] = 'sensible/process thermal duty to bring the streams to setpoint (AD energy balance: heat + enthalpy transfer only); EXCLUDES heat of digestion / reaction and ambient shell losses. NOT full heater/chiller sizing -- installed-duty sizing with digestion heat + losses is a mechanical-design step.'
        if self.profile.include_acidogenic:
            note['two_stage_both_stages_isothermal_setpoint'] = {'intentional': True, 'acid_temperature_setpoint_K': getattr(self.config, 'acid_temperature_K', None), 'methanogenic_temperature_setpoint_K': getattr(self.config, 'feed_temperature_K', None), 'is_boundary_state_pin': False, 'control_mode': 'heat_duty_controlled_isothermal', 'stages_heat_duty_controlled': ['acidogenic', 'methanogenic'], 'thermal_duty_solved_and_reported': True, 'thermal_duty_signed': True, 'kinetics_temperature_correction': False, 'note': "BOTH the acidogenic and the methanogenic stage are heat-duty-controlled isothermal, separately-heated reactors (a phase-separated digester with independently heated phases): each stage's liquid-outlet temperature is held at its setpoint (acid = acid_temperature_K, methanogenic = feed_temperature_K) by SOLVING the stage's external thermal duty (heat[0], reported as a SIGNED thermal duty in kW -- a stage held below its inlet temperature reports a cooling duty), NOT by pinning the outlet-temperature variable. This is an intentional design spec, NOT a hidden boundary-state pin -- each reactor's vapor S_h2 / reaction H+ (pH) / product trace ions stay solver-determined. The setpoints govern the energy balance, the acid/base equilibria, and Henry-law gas transfer; the modified-ADM1 biological kinetics remain constant-parameter (not Arrhenius temperature-corrected). The reported duty is a sensible/process duty to setpoint -- it EXCLUDES heat of digestion and ambient losses, so it is not full heater sizing."}
        return note

    def _design_parameters(self, topology: RoleResolvedTopology) -> Dict[str, Any]:
        config = self.config
        params = {'adm1_family': self.profile.family, 'adm1_model_variant': config.property_package.name, 'digester_volume_liquid_m3': config.volume_liquid_m3, 'digester_volume_vapor_m3': config.volume_vapor_m3, 'hydraulic_retention_time_days': config.hydraulic_retention_time_days(), 'feed_flow_m3_per_day': config.feed_flow_m3_per_day, 'feed_total_cod_kg_m3': adm1_feed_total_cod_kg_m3(config.feed_conc_kg_m3 or {}), 'reactor_height_m': getattr(config, 'reactor_height_m', None), 'feed_temperature_K': config.feed_temperature_K, 'feed_cations_kmol_m3': config.feed_cations_kmol_m3, 'feed_anions_kmol_m3': config.feed_anions_kmol_m3, 'headspace_pressure_design_Pa': config.headspace_pressure_Pa, 'headspace_pressure_provenance': "adm1_provider_design_default; solver-determined in-model via the AD outlet_P partial-pressure sum and the flow_vol_vap friction/back-pressure relation (k_p), preserved by name through the build's gas-coupled pressure protection", 'digesters_in_train': list(self._digester_ids(topology)), 'acidogenic_digesters': list(topology.all_ids(UnitRole.ANAEROBIC_ACIDOGENIC)), 'methanogenic_digesters': list(topology.all_ids(UnitRole.ANAEROBIC_METHANOGENIC)), 'has_recuperation_recycle': self.profile.include_recuperation, 'has_solids_separation': self.profile.include_solids_separation, 'credibility': self._credibility()}
        if self.profile.include_acidogenic:
            params['acid_volume_liquid_m3'] = getattr(config, 'acid_volume_liquid_m3', None)
            params['acid_volume_vapor_m3'] = getattr(config, 'acid_volume_vapor_m3', None)
        if self.profile.family == 'egsb':
            params['recirculation_ratio'] = getattr(config, 'recirculation_ratio', None)
        if self.profile.recycle_waste_outlet is not None:
            params['waste_fraction'] = getattr(config, 'waste_fraction', None)
        return params

    def _metadata(self) -> Dict[str, Any]:
        config = self.config
        return {'adm1_model_variant': config.property_package.name, 'adm1_temperature_setpoint_K': config.feed_temperature_K, 'adm1_headspace_pressure_design_Pa': config.headspace_pressure_Pa, 'adm1_strong_ion_boundary': {'cations_kmol_m3': config.feed_cations_kmol_m3, 'anions_kmol_m3': config.feed_anions_kmol_m3}, 'adm1_credibility': self._credibility(), 'solver_options': self._solver_options()}

    def _solver_options(self) -> Dict[str, Any]:
        """Return ADM1-appropriate IPOPT options.

        The stiff acid/base + gas-transfer digester solves cleanly with the
        monotone barrier; the shared ASM2D-tuned adaptive barrier tips the
        warm-started digester into an IPOPT evaluation error. The pipeline also
        applies this defensively (``_with_adm1_solver_defaults``); carrying it on
        the preset keeps a direct caller consistent.
        """
        return {'mu_strategy': 'monotone'}

class _ADM1BaseTemplate:
    """Shared machinery for the mADM1 digestion presets (roadmap A4).

    A preset = a role-tagged structural topology (``get_units`` / ``get_connections``,
    family-specific) + a :class:`Adm1FamilyProfile` (``family_profile``, the
    structural intent the role graph cannot recover) + a config. The solver defaults
    are delegated to a single :class:`Adm1PresetProvider` bound to that config +
    profile and resolved against the role-tagged graph — exactly like the ASM2D
    presets, so a named-preset build and a from-graph (project-named) build inherit
    role-identical defaults. Subclasses supply the topology + profile + a default
    config/units; everything below (provider binding, default-bundle delegation,
    ``to_session_spec``) is common.
    """
    config_class: type = ADM1DigestionConfig
    carries_organic_sulfur: bool = False
    carries_oxidant_inlet: bool = False

    def __init__(self, config: Optional[ADM1DigestionConfig]=None, units: Optional[Any]=None):
        self.config = config or self.config_class()
        if not self.carries_organic_sulfur and organic_sulfur_stated(self.config):
            raise ValueError(f"{type(self).__name__} carries no organic sulfur: organic_sulfur / a feed X_Sorg is the adm1_cstr S/Fe digester's (KernelCSTRTemplate) only")
        if not self.carries_oxidant_inlet and oxidant_inlet_stated(self.config):
            raise ValueError(f"{type(self).__name__} takes no oxidant inlet: oxidant_inlet / headspace_wetted_area_m2 (digester micro-aeration) are the adm1_cstr S/Fe digester's (KernelCSTRTemplate) only")
        self.units = units or self.default_units()
        self.profile = self.family_profile()
        self._provider = Adm1PresetProvider(self.profile, self.config)

    @classmethod
    def default_units(cls) -> Any:
        raise NotImplementedError

    @classmethod
    def family_profile(cls) -> Adm1FamilyProfile:
        raise NotImplementedError

    def get_units(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def get_connections(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def _ad_unit_spec(self, unit_id: str, role: str, stage_index: int) -> Dict[str, Any]:
        """Return an AD (anaerobic digester) unit spec on the configured package."""
        return {'unit_id': unit_id, 'unit_type': 'AD', 'config': {'liquid_property_package': self.config.property_package.name, 'vapor_property_package': PropertyPackageType.ADM1_VAPOR.name}, 'role': role, 'role_stage_index': stage_index}

    def _biogas_unit_spec(self, unit_id: str) -> Dict[str, Any]:
        """Return a biogas take-off Product sink on the ADM1 vapor package."""
        return {'unit_id': unit_id, 'unit_type': 'Product', 'config': {'property_package': PropertyPackageType.ADM1_VAPOR.name}, 'role': UnitRole.ANAEROBIC_GAS_PHASE.value}

    def _recycle_mixer_spec(self, unit_id: str) -> Dict[str, Any]:
        """Return the fresh-feed + recycle Mixer on the liquid mADM1 package.

        ``momentum_mixing_type='none'`` removes the Mixer-side pressure-equality
        constraints (the digestion train is atmospheric-class and the mass balance
        is pressure-irrelevant); the build's biological pressure demotion fixes the
        liquid-side hydraulic pressures to the feed datum.
        """
        return {'unit_id': unit_id, 'unit_type': 'Mixer', 'config': {'inlet_list': ['feed_water', 'recycle'], 'momentum_mixing_type': 'none'}, 'role': UnitRole.MIXER.value}

    def _separator_spec(self, unit_id: str, role: str, outlet_list: List[str], split_basis: str) -> Dict[str, Any]:
        """Return a Separator spec on the liquid mADM1 package."""
        return {'unit_id': unit_id, 'unit_type': 'Separator', 'config': {'outlet_list': outlet_list, 'split_basis': split_basis}, 'role': role}

    @property
    def provider(self) -> Adm1PresetProvider:
        """The default provider this preset is bound to."""
        return self._provider

    def _topology(self) -> RoleResolvedTopology:
        return RoleResolvedTopology.from_unit_specs(self.get_units())

    def default_bundle(self) -> DefaultBundle:
        return self._provider.provide(self._topology())

    def get_dof_fixes(self) -> List[Dict[str, Any]]:
        return self.default_bundle().dof_fixes

    def get_scaling_factors(self) -> List[Dict[str, Any]]:
        return self.default_bundle().scaling_factors

    def get_feed_states(self) -> Dict[str, Dict[str, Any]]:
        return self.default_bundle().feed_states

    def get_process_constraints(self) -> List[Dict[str, Any]]:
        return self.default_bundle().process_constraints

    def get_design_parameters(self) -> Dict[str, Any]:
        return self.default_bundle().design_parameters

    def get_initialization_order(self) -> List[str]:
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

class UASBTemplate(_ADM1BaseTemplate):
    """UASB anaerobic-digestion preset (single methanogenic mADM1 digester).

    Topology: feed -> anaerobic digester (methanogenic, combined acid/methane) ->
    liquid effluent product, with a biogas take-off product on the digester vapor
    outlet (the gas-phase boundary). No recycle — the PR1 single-CSTR baseline.
    """
    config_class = ADM1DigestionConfig
    include_gas_phase = True

    @classmethod
    def default_units(cls) -> ADM1TemplateUnits:
        return ADM1TemplateUnits()

    @classmethod
    def family_profile(cls) -> Adm1FamilyProfile:
        return Adm1FamilyProfile(include_gas_phase=cls.include_gas_phase, include_recuperation=False, family='uasb')

    def get_units(self) -> List[Dict[str, Any]]:
        units: List[Dict[str, Any]] = [{'unit_id': self.units.feed, 'unit_type': 'Feed', 'config': {}, 'role': UnitRole.FEED.value}, self._ad_unit_spec(self.units.digester, UnitRole.ANAEROBIC_METHANOGENIC.value, 0), {'unit_id': self.units.effluent, 'unit_type': 'Product', 'config': {}, 'role': UnitRole.PRODUCT_EFFLUENT.value}]
        if self.include_gas_phase:
            units.append(self._biogas_unit_spec(self.units.biogas))
        return units

    def get_connections(self) -> List[Dict[str, Any]]:
        connections = [{'src_unit': self.units.feed, 'src_port': 'outlet', 'dest_unit': self.units.digester, 'dest_port': 'inlet'}, {'src_unit': self.units.digester, 'src_port': 'liquid_outlet', 'dest_unit': self.units.effluent, 'dest_port': 'inlet'}]
        if self.include_gas_phase:
            connections.append({'src_unit': self.units.digester, 'src_port': 'vapor_outlet', 'dest_unit': self.units.biogas, 'dest_port': 'inlet'})
        return connections
KERNEL_REACTION_PACKAGE = 'MADM1_KERNEL'
SFE_KERNEL_REACTION_PACKAGE = 'MADM1_SFE_KERNEL'
ORGANIC_S_KERNEL_REACTION_PACKAGE = 'MADM1_SFE_ORGANIC_S_KERNEL'
MICROAERATION_KERNEL_REACTION_PACKAGE = 'MADM1_SFE_MICROAERATION_KERNEL'

class KernelCSTRTemplate(UASBTemplate):
    'Completely mixed mADM1 digester (SRT = HRT) on the SHARED biokinetic kernel WITH SULFUR AND IRON.'
    carries_organic_sulfur = True
    carries_oxidant_inlet = True

    def __init__(self, config: Optional[ADM1DigestionConfig]=None, units: Optional[Any]=None):
        config = config or self.config_class()
        self.sulfur_iron = sulfur_iron_requested(config)
        self.organic_sulfur = organic_sulfur_requested(config)
        if self.organic_sulfur and (not self.sulfur_iron):
            raise ValueError("organic_sulfur needs the sulfur/iron build: X_Sorg is released to the S/Fe digester's S_IS (state sulfur_iron=True, or feed the sulfur)")
        self.oxidant_inlet = oxidant_inlet_requested(config)
        if self.oxidant_inlet is not None and (not self.sulfur_iron):
            raise ValueError("an oxidant inlet needs the sulfur/iron build: micro-aeration oxidises the S/Fe digester's headspace H2S (state sulfur_iron=True, or feed the sulfur)")
        config.property_package = PropertyPackageType.MODIFIED_ADM1_SFE_MICROAERATION if self.oxidant_inlet is not None else PropertyPackageType.MODIFIED_ADM1_SFE_ORGANIC_S if self.organic_sulfur else PropertyPackageType.MODIFIED_ADM1_SFE if self.sulfur_iron else PropertyPackageType.MODIFIED_ADM1
        super().__init__(config, units)

    @classmethod
    def family_profile(cls) -> Adm1FamilyProfile:
        return Adm1FamilyProfile(include_gas_phase=cls.include_gas_phase, include_recuperation=False, family='cstr')

    def _ad_unit_spec(self, unit_id: str, role: str, stage_index: int) -> Dict[str, Any]:
        spec = super()._ad_unit_spec(unit_id, role, stage_index)
        if self.oxidant_inlet is not None:
            spec['config']['vapor_property_package'] = PropertyPackageType.ADM1_SFE_MICROAERATION_VAPOR.name
            spec['config']['reaction_package'] = MICROAERATION_KERNEL_REACTION_PACKAGE
            spec['config']['oxidant_inlet'] = True
        elif self.sulfur_iron:
            spec['config']['vapor_property_package'] = PropertyPackageType.ADM1_SFE_VAPOR.name
            spec['config']['reaction_package'] = ORGANIC_S_KERNEL_REACTION_PACKAGE if self.organic_sulfur else SFE_KERNEL_REACTION_PACKAGE
        else:
            spec['config']['reaction_package'] = KERNEL_REACTION_PACKAGE
        return spec

    def _biogas_unit_spec(self, unit_id: str) -> Dict[str, Any]:
        spec = super()._biogas_unit_spec(unit_id)
        if self.oxidant_inlet is not None:
            spec['config']['property_package'] = PropertyPackageType.ADM1_SFE_MICROAERATION_VAPOR.name
        elif self.sulfur_iron:
            spec['config']['property_package'] = PropertyPackageType.ADM1_SFE_VAPOR.name
        return spec

    def to_session_spec(self) -> Dict[str, Any]:
        session_spec = super().to_session_spec()
        from watertap_engine_mcp.chemistry.modified_adm1_kernel import KERNEL, design_parameter_values
        _values, identity = design_parameter_values()
        design = {'adm1_reaction_package': KERNEL_REACTION_PACKAGE, 'kinetics_kernel_id': KERNEL.kernel_id, 'kinetics_kernel_sha256': KERNEL.sha256, 'kinetics_parameter_set': identity, 'adm1_sulfur_iron': False}
        if self.sulfur_iron:
            from watertap_engine_mcp.chemistry.modified_adm1_sfe import SFE_KERNEL, sfe_design_parameter_values
            _sfe_values, sfe_identity = sfe_design_parameter_values()
            design.update({'adm1_reaction_package': SFE_KERNEL_REACTION_PACKAGE, 'kinetics_kernel_id': SFE_KERNEL.kernel_id, 'kinetics_kernel_sha256': SFE_KERNEL.sha256, 'kinetics_sfe_parameter_set': sfe_identity, 'adm1_sulfur_iron': True})
        if self.organic_sulfur:
            from watertap_engine_mcp.chemistry.modified_adm1_sfe import organic_sulfur_identity
            ext = organic_sulfur_identity()
            design.update({'adm1_reaction_package': ORGANIC_S_KERNEL_REACTION_PACKAGE, 'kinetics_organic_sulfur_extension_id': ext['organic_sulfur_extension_id'], 'kinetics_organic_sulfur_extension_sha256': ext['organic_sulfur_extension_sha256'], 'adm1_organic_sulfur': True})
        if self.oxidant_inlet is not None:
            from watertap_engine_mcp.chemistry.modified_adm1_microaeration import microaeration_identity
            from watertap_engine_mcp.chemistry.modified_adm1_sfe import organic_sulfur_identity
            organic, micro = (organic_sulfur_identity(), microaeration_identity())
            design.update({'adm1_reaction_package': MICROAERATION_KERNEL_REACTION_PACKAGE, 'kinetics_organic_sulfur_extension_id': organic['organic_sulfur_extension_id'], 'kinetics_organic_sulfur_extension_sha256': organic['organic_sulfur_extension_sha256'], 'kinetics_microaeration_extension_id': micro['microaeration_extension_id'], 'kinetics_microaeration_extension_sha256': micro['microaeration_extension_sha256'], 'adm1_microaeration': True, 'adm1_microaeration_standing': 'NON-governing bracket model until calibrated on a dosed digester (PUBLIC-RULE-6dce80fc160d)', 'oxidant_inlet': dict(self.oxidant_inlet), 'headspace_wetted_area_m2': float(self.config.headspace_wetted_area_m2)})
        session_spec['design_parameters'] = {**session_spec.get('design_parameters', {}), **design}
        return session_spec

def sulfur_iron_requested(config: ADM1DigestionConfig) -> bool:
    "Whether the adm1_cstr digester is built with its sulfur/iron subsystem: the config's explicit ``sulfur_iron``,\n    else whether the feed carries any S/Fe state above the 1e-9 floor."
    if config.sulfur_iron is not None:
        return bool(config.sulfur_iron)
    feed = config.feed_conc_kg_m3 or {}
    return any((float(feed.get(k, 0.0) or 0.0) > 1e-09 for k in MODIFIED_ADM1_SFE_COMPONENTS + MODIFIED_ADM1_IRON_MINERAL_COMPONENTS + MODIFIED_ADM1_ORGANIC_S_COMPONENTS))

def _feed_organic_sulfur(config: ADM1DigestionConfig) -> bool:
    feed = config.feed_conc_kg_m3 or {}
    return any((float(feed.get(k, 0.0) or 0.0) > 1e-09 for k in MODIFIED_ADM1_ORGANIC_S_COMPONENTS))

def organic_sulfur_requested(config: ADM1DigestionConfig) -> bool:
    """Whether the adm1_cstr S/Fe digester compiles the opt-in organic-sulfur extension: the config's explicit
    ``organic_sulfur``, else whether the feed carries X_Sorg above the 1e-9 floor ``sulfur_iron_requested`` uses. A
    feed X_Sorg with ``organic_sulfur=False`` is refused (its sulfur would be dropped, not digested)."""
    if config.organic_sulfur is not None:
        if not config.organic_sulfur and _feed_organic_sulfur(config):
            raise ValueError('the feed carries organic sulfur (X_Sorg) but organic_sulfur=False; refused rather than dropped')
        return bool(config.organic_sulfur)
    return _feed_organic_sulfur(config)

def organic_sulfur_stated(config: ADM1DigestionConfig) -> bool:
    """Whether a config asks for organic sulfur at all (``organic_sulfur=True`` or a feed X_Sorg above the floor)."""
    return bool(getattr(config, 'organic_sulfur', None)) or _feed_organic_sulfur(config)

def oxidant_inlet_stated(config: ADM1DigestionConfig) -> bool:
    """Whether a config names anything of micro-aeration (an oxidant inlet, or a headspace wetted area)."""
    return getattr(config, 'oxidant_inlet', None) is not None or getattr(config, 'headspace_wetted_area_m2', None) is not None

def oxidant_inlet_requested(config: ADM1DigestionConfig) -> Optional[Dict[str, Any]]:
    """The validated ``vapor_inlet`` state of the adm1_cstr digester's stated oxidant, or None without one.

    The two fields come together or not at all: an oxidant inlet without its headspace wetted area is refused (a
    rating may not invent the geometry it rates), and a wetted area without an oxidant inlet is refused too (a stage-(b)
    specification the model would silently drop). A feed that carries dissolved O2 without an oxidant inlet is refused
    by name: S_O2 exists only in the micro-aeration variant."""
    feed = config.feed_conc_kg_m3 or {}
    oxidant, area = (config.oxidant_inlet, config.headspace_wetted_area_m2)
    if oxidant is None:
        if area is not None:
            raise ValueError("headspace_wetted_area_m2 is stated without an oxidant_inlet: the wetted area is the micro-aeration variant's geometry and would be dropped; refused")
        named = [k for k in MODIFIED_ADM1_MICROAERATION_COMPONENTS if float(feed.get(k, 0.0) or 0.0) > 1e-09]
        if named:
            raise ValueError(f'the feed carries {named} but the digester has no oxidant_inlet; dissolved O2 is a state of the micro-aeration variant only; refused rather than dropped')
        return None
    from watertap_engine_mcp.chemistry.modified_adm1_microaeration import oxidant_inlet_state
    state = oxidant_inlet_state(oxidant)
    try:
        area_ok = area is not None and (not isinstance(area, bool)) and (float(area) > 0.0)
    except (TypeError, ValueError):
        area_ok = False
    if not area_ok:
        raise ValueError(f"an oxidant_inlet needs headspace_wetted_area_m2 [m2] from stage (b) (the anaerobic sizing's digester_rated_headspace_wetted_area_m2, per train); got {area!r}. It is never defaulted")
    return state

@dataclass
class EGSBTemplateUnits:
    """Unit ids for the EGSB recirculation template."""
    feed: str = 'Feed'
    mixer: str = 'RecycleMixer'
    digester: str = 'Digester'
    recuperation: str = 'Recirculation'
    effluent: str = 'LiquidEffluent'
    biogas: str = 'Biogas'

class EGSBTemplate(_ADM1BaseTemplate):
    """EGSB preset: methanogenic digester with high liquid recirculation.

    Topology: feed -> recycle Mixer -> AD (methanogenic) -> recuperation Separator;
    the recuperation splits the digestate to a high recirculation stream (back to
    the Mixer — the recycle tear) and a forward effluent. A biogas take-off rides
    the digester vapor outlet. The recirculation ratio is the single recycle spec.
    """
    config_class = EGSBDigestionConfig

    @classmethod
    def default_units(cls) -> EGSBTemplateUnits:
        return EGSBTemplateUnits()

    @classmethod
    def family_profile(cls) -> Adm1FamilyProfile:
        return Adm1FamilyProfile(include_gas_phase=True, include_recuperation=True, family='egsb', recycle_recycle_outlet='recirculation', recycle_waste_outlet=None)

    def _zero_recirculation(self) -> bool:
        """Return True when EGSB is configured as the no-recycle UASB-equivalent branch."""
        try:
            return float(self.config.recirculation_ratio) <= EGSB_ZERO_RECIRCULATION_EPS
        except (TypeError, ValueError):
            return False

    def get_units(self) -> List[Dict[str, Any]]:
        u = self.units
        if self._zero_recirculation():
            return [{'unit_id': u.feed, 'unit_type': 'Feed', 'config': {}, 'role': UnitRole.FEED.value}, self._ad_unit_spec(u.digester, UnitRole.ANAEROBIC_METHANOGENIC.value, 0), {'unit_id': u.effluent, 'unit_type': 'Product', 'config': {}, 'role': UnitRole.PRODUCT_EFFLUENT.value}, self._biogas_unit_spec(u.biogas)]
        return [{'unit_id': u.feed, 'unit_type': 'Feed', 'config': {}, 'role': UnitRole.FEED.value}, self._recycle_mixer_spec(u.mixer), self._ad_unit_spec(u.digester, UnitRole.ANAEROBIC_METHANOGENIC.value, 0), self._separator_spec(u.recuperation, UnitRole.ANAEROBIC_RECUPERATION.value, ['forward', 'recirculation'], 'totalFlow'), {'unit_id': u.effluent, 'unit_type': 'Product', 'config': {}, 'role': UnitRole.PRODUCT_EFFLUENT.value}, self._biogas_unit_spec(u.biogas)]

    def get_connections(self) -> List[Dict[str, Any]]:
        u = self.units
        if self._zero_recirculation():
            return [{'src_unit': u.feed, 'src_port': 'outlet', 'dest_unit': u.digester, 'dest_port': 'inlet'}, {'src_unit': u.digester, 'src_port': 'liquid_outlet', 'dest_unit': u.effluent, 'dest_port': 'inlet'}, {'src_unit': u.digester, 'src_port': 'vapor_outlet', 'dest_unit': u.biogas, 'dest_port': 'inlet'}]
        return [{'src_unit': u.feed, 'src_port': 'outlet', 'dest_unit': u.mixer, 'dest_port': 'feed_water'}, {'src_unit': u.mixer, 'src_port': 'outlet', 'dest_unit': u.digester, 'dest_port': 'inlet'}, {'src_unit': u.digester, 'src_port': 'liquid_outlet', 'dest_unit': u.recuperation, 'dest_port': 'inlet'}, {'src_unit': u.recuperation, 'src_port': 'forward', 'dest_unit': u.effluent, 'dest_port': 'inlet'}, {'src_unit': u.recuperation, 'src_port': 'recirculation', 'dest_unit': u.mixer, 'dest_port': 'recycle', 'tear_stream': True, 'tear_rationale': 'EGSB upflow liquid recirculation loop'}, {'src_unit': u.digester, 'src_port': 'vapor_outlet', 'dest_unit': u.biogas, 'dest_port': 'inlet'}]

@dataclass
class AnMBRTemplateUnits:
    """Unit ids for the AnMBR membrane-retentate-recycle template."""
    feed: str = 'Feed'
    mixer: str = 'RecycleMixer'
    digester: str = 'Digester'
    membrane: str = 'Membrane'
    bleed: str = 'RetentateBleed'
    effluent: str = 'Permeate'
    waste: str = 'WasteSludge'
    biogas: str = 'Biogas'

class AnMBRTemplate(_ADM1BaseTemplate):
    """AnMBR preset: methanogenic digester + biomass-retaining membrane recycle.

    Topology: feed -> recycle Mixer -> AD (methanogenic) -> membrane Separator
    (permeate = biomass-free effluent; retentate = concentrated biomass) ->
    retentate bleed Separator (waste bleed sets SRT; the recycle returns biomass to
    the Mixer — the membrane-retentate tear). The membrane split is the solids
    spec; the bleed split is the SRT/WAS spec.
    """
    config_class = AnMBRDigestionConfig

    @classmethod
    def default_units(cls) -> AnMBRTemplateUnits:
        return AnMBRTemplateUnits()

    @classmethod
    def family_profile(cls) -> Adm1FamilyProfile:
        return Adm1FamilyProfile(include_gas_phase=True, include_recuperation=True, include_solids_separation=True, family='anmbr', recycle_recycle_outlet='recycle', recycle_waste_outlet='waste', solids_clarified_outlet='permeate')

    def get_units(self) -> List[Dict[str, Any]]:
        u = self.units
        return [{'unit_id': u.feed, 'unit_type': 'Feed', 'config': {}, 'role': UnitRole.FEED.value}, self._recycle_mixer_spec(u.mixer), self._ad_unit_spec(u.digester, UnitRole.ANAEROBIC_METHANOGENIC.value, 0), self._separator_spec(u.membrane, UnitRole.ANAEROBIC_THICKENER.value, ['permeate', 'retentate'], 'componentFlow'), self._separator_spec(u.bleed, UnitRole.ANAEROBIC_RECUPERATION.value, ['waste', 'recycle'], 'totalFlow'), {'unit_id': u.effluent, 'unit_type': 'Product', 'config': {}, 'role': UnitRole.PRODUCT_EFFLUENT.value}, {'unit_id': u.waste, 'unit_type': 'Product', 'config': {}, 'role': UnitRole.PRODUCT_WASTE.value}, self._biogas_unit_spec(u.biogas)]

    def get_connections(self) -> List[Dict[str, Any]]:
        u = self.units
        return [{'src_unit': u.feed, 'src_port': 'outlet', 'dest_unit': u.mixer, 'dest_port': 'feed_water'}, {'src_unit': u.mixer, 'src_port': 'outlet', 'dest_unit': u.digester, 'dest_port': 'inlet'}, {'src_unit': u.digester, 'src_port': 'liquid_outlet', 'dest_unit': u.membrane, 'dest_port': 'inlet'}, {'src_unit': u.membrane, 'src_port': 'permeate', 'dest_unit': u.effluent, 'dest_port': 'inlet'}, {'src_unit': u.membrane, 'src_port': 'retentate', 'dest_unit': u.bleed, 'dest_port': 'inlet'}, {'src_unit': u.bleed, 'src_port': 'waste', 'dest_unit': u.waste, 'dest_port': 'inlet'}, {'src_unit': u.bleed, 'src_port': 'recycle', 'dest_unit': u.mixer, 'dest_port': 'recycle', 'tear_stream': True, 'tear_rationale': 'AnMBR membrane-retentate biomass recycle loop'}, {'src_unit': u.digester, 'src_port': 'vapor_outlet', 'dest_unit': u.biogas, 'dest_port': 'inlet'}]

@dataclass
class AnaerobicContactTemplateUnits:
    """Unit ids for the anaerobic-contact (thickener + sludge-return) template."""
    feed: str = 'Feed'
    mixer: str = 'RecycleMixer'
    digester: str = 'Digester'
    thickener: str = 'Thickener'
    sludge: str = 'SludgeSplitter'
    effluent: str = 'LiquidEffluent'
    waste: str = 'WasteSludge'
    biogas: str = 'Biogas'

class AnaerobicContactTemplate(_ADM1BaseTemplate):
    """Anaerobic-contact preset: CSTR digester + thickener + sludge return.

    Topology: feed -> recycle Mixer -> AD (methanogenic) -> thickener Separator
    (clarified overflow = effluent; thickened underflow = concentrated biomass) ->
    sludge Separator (waste bleed sets SRT; the recycle (RAS) returns biomass to
    the Mixer — the recuperation tear). The thickener split is the solids spec; the
    sludge-return split is the RAS/WAS spec.
    """
    config_class = AnaerobicContactDigestionConfig

    @classmethod
    def default_units(cls) -> AnaerobicContactTemplateUnits:
        return AnaerobicContactTemplateUnits()

    @classmethod
    def family_profile(cls) -> Adm1FamilyProfile:
        return Adm1FamilyProfile(include_gas_phase=True, include_recuperation=True, include_solids_separation=True, family='contact', recycle_recycle_outlet='recycle', recycle_waste_outlet='waste', solids_clarified_outlet='overflow')

    def get_units(self) -> List[Dict[str, Any]]:
        u = self.units
        return [{'unit_id': u.feed, 'unit_type': 'Feed', 'config': {}, 'role': UnitRole.FEED.value}, self._recycle_mixer_spec(u.mixer), self._ad_unit_spec(u.digester, UnitRole.ANAEROBIC_METHANOGENIC.value, 0), self._separator_spec(u.thickener, UnitRole.ANAEROBIC_THICKENER.value, ['overflow', 'underflow'], 'componentFlow'), self._separator_spec(u.sludge, UnitRole.ANAEROBIC_RECUPERATION.value, ['waste', 'recycle'], 'totalFlow'), {'unit_id': u.effluent, 'unit_type': 'Product', 'config': {}, 'role': UnitRole.PRODUCT_EFFLUENT.value}, {'unit_id': u.waste, 'unit_type': 'Product', 'config': {}, 'role': UnitRole.PRODUCT_WASTE.value}, self._biogas_unit_spec(u.biogas)]

    def get_connections(self) -> List[Dict[str, Any]]:
        u = self.units
        return [{'src_unit': u.feed, 'src_port': 'outlet', 'dest_unit': u.mixer, 'dest_port': 'feed_water'}, {'src_unit': u.mixer, 'src_port': 'outlet', 'dest_unit': u.digester, 'dest_port': 'inlet'}, {'src_unit': u.digester, 'src_port': 'liquid_outlet', 'dest_unit': u.thickener, 'dest_port': 'inlet'}, {'src_unit': u.thickener, 'src_port': 'overflow', 'dest_unit': u.effluent, 'dest_port': 'inlet'}, {'src_unit': u.thickener, 'src_port': 'underflow', 'dest_unit': u.sludge, 'dest_port': 'inlet'}, {'src_unit': u.sludge, 'src_port': 'waste', 'dest_unit': u.waste, 'dest_port': 'inlet'}, {'src_unit': u.sludge, 'src_port': 'recycle', 'dest_unit': u.mixer, 'dest_port': 'recycle', 'tear_stream': True, 'tear_rationale': 'Anaerobic-contact sludge-return (RAS) recycle loop'}, {'src_unit': u.digester, 'src_port': 'vapor_outlet', 'dest_unit': u.biogas, 'dest_port': 'inlet'}]

@dataclass
class TwoStageTemplateUnits:
    """Unit ids for the phase-separated two-stage template."""
    feed: str = 'Feed'
    acid: str = 'AcidReactor'
    methanogen: str = 'MethReactor'
    effluent: str = 'LiquidEffluent'
    acid_biogas: str = 'AcidBiogas'
    meth_biogas: str = 'MethBiogas'

class TwoStageTemplate(_ADM1BaseTemplate):
    """Two-stage preset: acidogenic reactor feeding a methanogenic reactor.

    Topology: feed -> AD (acidogenic, short HRT, VFA-rich low-pH) -> AD
    (methanogenic) -> liquid effluent. EACH reactor carries its own biogas
    headspace (an acid-phase off-gas and the methane-phase biogas). The base case
    is a forward acid->methane cascade (no recycle), so each stage solves its own
    pH and two headspaces close independently.
    """
    config_class = TwoStageDigestionConfig

    @classmethod
    def default_units(cls) -> TwoStageTemplateUnits:
        return TwoStageTemplateUnits()

    @classmethod
    def family_profile(cls) -> Adm1FamilyProfile:
        return Adm1FamilyProfile(include_gas_phase=True, include_recuperation=False, include_acidogenic=True, family='two_stage')

    def get_units(self) -> List[Dict[str, Any]]:
        u = self.units
        return [{'unit_id': u.feed, 'unit_type': 'Feed', 'config': {}, 'role': UnitRole.FEED.value}, self._ad_unit_spec(u.acid, UnitRole.ANAEROBIC_ACIDOGENIC.value, 0), self._ad_unit_spec(u.methanogen, UnitRole.ANAEROBIC_METHANOGENIC.value, 1), {'unit_id': u.effluent, 'unit_type': 'Product', 'config': {}, 'role': UnitRole.PRODUCT_EFFLUENT.value}, self._biogas_unit_spec(u.acid_biogas), self._biogas_unit_spec(u.meth_biogas)]

    def get_connections(self) -> List[Dict[str, Any]]:
        u = self.units
        return [{'src_unit': u.feed, 'src_port': 'outlet', 'dest_unit': u.acid, 'dest_port': 'inlet'}, {'src_unit': u.acid, 'src_port': 'liquid_outlet', 'dest_unit': u.methanogen, 'dest_port': 'inlet'}, {'src_unit': u.acid, 'src_port': 'vapor_outlet', 'dest_unit': u.acid_biogas, 'dest_port': 'inlet'}, {'src_unit': u.methanogen, 'src_port': 'liquid_outlet', 'dest_unit': u.effluent, 'dest_port': 'inlet'}, {'src_unit': u.methanogen, 'src_port': 'vapor_outlet', 'dest_unit': u.meth_biogas, 'dest_port': 'inlet'}]
