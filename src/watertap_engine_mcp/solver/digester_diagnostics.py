"""Post-solve basin classification for ADM1 / mADM1 anaerobic digesters (roadmap A4).

ADM1 is multi-steady-state: the same digester model admits a healthy methanogenic
basin, an acidified / sour basin (VFA accumulation + depressed pH + collapsed CH4),
and a washout basin (methanogen biomass lost, substrate passes through). IPOPT can
report ``optimal`` on ANY of them, so a converged "optimal" tells you the equations
are satisfied, NOT that the design is a healthy digester.

:func:`classify_digester_basin` reads the converged digester state (pH, VFA
accumulation, CH4-vs-H2 split, headspace pressure, methanogen biomass) and returns
an explicit ``classification`` + ``is_sour`` flag + warnings so a sour or washed-out
"optimal" is surfaced, never silently presented as a healthy methanogenic design.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Dict, List, Optional

# Healthy mesophilic methanogenic digesters sit near-neutral / mildly alkaline and
# run CH4-rich with low residual VFA. These bands are deliberately generous: the
# point is to DISCRIMINATE a healthy basin from a sour / washout one, not to grade
# a healthy digester finely.
HEALTHY_PH_LOW = 6.5
HEALTHY_PH_HIGH = 8.2
HEALTHY_CH4_FRACTION_MIN = 0.45
# Total VFA (acetate + propionate + butyrate + valerate, kg COD/m3). A well-buffered
# digester keeps total VFA well under ~1; sustained accumulation above this band is
# the acidification signature.
SOUR_TOTAL_VFA_KG_COD_M3 = 2.0
# Residual acetate (kg COD/m3) above which a digester is NOT healthy. Hill, Cobb &
# Bolte (1987), "Using volatile fatty acid relationships to predict anaerobic
# digester failure", Transactions of the ASAE (1987), doi:10.13031/2013.31977: across
# 70 literature observations, acetic acid above 800 mg/L (or a propionic:acetic ratio
# above 1.4) indicated impending digester failure.
# 800 mg/L acetic acid x 64/60.05 kg COD per kg CH3COOH = 0.8526 kg COD/m3, the ADM1
# S_ac basis (Batstone et al. 2002, IWA STR13, doi:10.2166/wst.2002.0292).
ACETATE_FAILURE_ACETIC_ACID_KG_M3 = 0.8
ACETIC_ACID_COD_KG_PER_KG = 64.0 / 60.05
FAILURE_ACETATE_KG_COD_M3 = ACETATE_FAILURE_ACETIC_ACID_KG_M3 * ACETIC_ACID_COD_KG_PER_KG
# Acetoclastic methanogen (X_ac) biomass below this is effectively washed out. The
# rule keys on X_ac ALONE: hydrogenotrophs (X_h2) can keep the headspace CH4-rich
# (~60 %) after the acetoclasts wash out, because the gas-phase fraction is a
# composition, not a production rate. A washed-out digester at a hydrogenotroph-
# held CH4 fraction was read as healthy until 2026-09-29 (HOUSE-ADM1-BENCHMARK-
# STAGEB-GEOMETRY-20260929). Known conservative limit: a very low-strength
# completely-mixed digester (degradable COD below ~2 kg/m3 at SRT = HRT ~ 20 d)
# carries a live X_ac below this band and now reads washout rather than healthy.
WASHOUT_BIOMASS_KG_M3 = 0.05
# A near-empty headspace CH4 fraction is the washout signal used ONLY when the
# acetoclastic biomass cannot be read off the block.
WASHOUT_CH4_FRACTION_MAX = 0.15


def _val(obj: Any) -> Optional[float]:
    """Read a Pyomo scalar/expression to a finite float, or None."""

    if obj is None:
        return None
    try:
        from pyomo.environ import value

        raw = value(obj, exception=False)
    except Exception:
        return None
    if raw is None:
        return None
    try:
        raw = float(raw)
    except (TypeError, ValueError):
        return None
    return raw if isfinite(raw) else None


def _first_time_index(indexed: Any) -> Optional[Any]:
    try:
        for t in indexed:
            return t
    except Exception:
        return None
    return None


def classify_digester_basin(digester: Any, time: Optional[float] = None) -> Dict[str, Any]:
    """Classify the (converged) digester steady state into a basin.

    Args:
        digester: A WaterTAP anaerobic-digester (AD) unit block exposing
            ``liquid_phase.reactions`` / ``liquid_phase.properties_out`` and
            ``vapor_phase`` with ``pressure`` / ``pressure_sat``.
        time: Optional time index; defaults to the first liquid-phase reaction
            index found.

    Returns:
        A JSON-safe dict with ``classification`` (one of
        ``healthy_methanogenic`` / ``acidified_sour`` / ``washout`` /
        ``indeterminate``), an explicit ``is_sour`` flag, the metrics it keyed on,
        and human-readable ``warnings`` (non-empty whenever the basin is not the
        healthy one).
    """

    warnings: List[str] = []

    reactions = getattr(getattr(digester, "liquid_phase", None), "reactions", None)
    t = time if time is not None else _first_time_index(reactions)

    # pH from the ADM1 acid/base electroneutrality reaction block.
    ph_value: Optional[float] = None
    if reactions is not None and t is not None:
        try:
            ph_value = _val(getattr(reactions[t], "pH", None))
        except Exception:
            ph_value = None

    # VFA accumulation + acetoclastic biomass from the liquid outlet.
    props_out = getattr(getattr(digester, "liquid_phase", None), "properties_out", None)
    total_vfa: Optional[float] = None
    acetate: Optional[float] = None
    methanogen_biomass: Optional[float] = None
    hydrogenotroph_biomass: Optional[float] = None
    if props_out is not None:
        lt = t if (t is not None and t in props_out) else _first_time_index(props_out)
        if lt is not None:
            conc = getattr(props_out[lt], "conc_mass_comp", None)
            if conc is not None:
                vfa_sum = 0.0
                vfa_seen = False
                for comp in ("S_ac", "S_pro", "S_bu", "S_va"):
                    if comp in conc:
                        v = _val(conc[comp])
                        if v is not None:
                            vfa_sum += max(v, 0.0)
                            vfa_seen = True
                            if comp == "S_ac":
                                acetate = v
                if vfa_seen:
                    total_vfa = vfa_sum
                if "X_ac" in conc:
                    methanogen_biomass = _val(conc["X_ac"])
                if "X_h2" in conc:
                    hydrogenotroph_biomass = _val(conc["X_h2"])

    # CH4 / H2 split + headspace pressure from the vapor phase.
    ch4_fraction: Optional[float] = None
    h2_partial: Optional[float] = None
    headspace_pressure: Optional[float] = None
    vapor = getattr(digester, "vapor_phase", None)
    if vapor is not None:
        vt = t if (t is not None and t in vapor) else _first_time_index(vapor)
        if vt is not None:
            headspace_pressure = _val(getattr(vapor[vt], "pressure", None))
            psat = getattr(vapor[vt], "pressure_sat", None)
            if psat is not None:
                partials: Dict[str, float] = {}
                for j in psat:
                    label = j if isinstance(j, str) else (j[-1] if isinstance(j, tuple) else str(j))
                    v = _val(psat[j])
                    if v is not None:
                        partials[label] = v
                total = sum(partials.values())
                if total > 0.0:
                    if "S_ch4" in partials:
                        ch4_fraction = partials["S_ch4"] / total
                    if "S_h2" in partials:
                        h2_partial = partials["S_h2"]

    # ---- Classification ---------------------------------------------------
    sour_signals: List[str] = []
    if ph_value is not None and ph_value < HEALTHY_PH_LOW:
        sour_signals.append(f"pH {ph_value:.2f} < {HEALTHY_PH_LOW}")
    if total_vfa is not None and total_vfa > SOUR_TOTAL_VFA_KG_COD_M3:
        sour_signals.append(
            f"total VFA {total_vfa:.2f} kg COD/m3 > {SOUR_TOTAL_VFA_KG_COD_M3}"
        )
    if acetate is not None and acetate > FAILURE_ACETATE_KG_COD_M3:
        sour_signals.append(
            f"acetate {acetate:.2f} kg COD/m3 > {FAILURE_ACETATE_KG_COD_M3:.3f} "
            "(Hill, Cobb & Bolte 1987: acetic acid > 800 mg/L)"
        )
    if ch4_fraction is not None and ch4_fraction < HEALTHY_CH4_FRACTION_MIN:
        sour_signals.append(
            f"CH4 fraction {ch4_fraction:.2f} < {HEALTHY_CH4_FRACTION_MIN}"
        )

    # Acetoclastic washout is keyed on X_ac alone, never gated on the CH4 fraction:
    # surviving hydrogenotrophs hold the headspace CH4-rich after X_ac is gone.
    if methanogen_biomass is not None:
        is_washout = methanogen_biomass < WASHOUT_BIOMASS_KG_M3
    else:
        is_washout = ch4_fraction is not None and ch4_fraction < WASHOUT_CH4_FRACTION_MAX

    if is_washout:
        classification = "washout"
        is_sour = True
        if methanogen_biomass is not None:
            detail = (
                "acetoclastic biomass X_ac "
                f"{methanogen_biomass:.3g} kg COD/m3 < {WASHOUT_BIOMASS_KG_M3}"
            )
            if acetate is not None:
                detail += f"; acetate {acetate:.3g} kg COD/m3 passes through"
            if ch4_fraction is not None and ch4_fraction >= HEALTHY_CH4_FRACTION_MIN:
                detail += (
                    f"; the {ch4_fraction:.0%} CH4 headspace is held by "
                    "hydrogenotrophs and does not indicate acetoclastic activity"
                )
        else:
            detail = (
                f"CH4 fraction {ch4_fraction:.2f} < {WASHOUT_CH4_FRACTION_MAX} and "
                "acetoclastic biomass unavailable"
            )
        warnings.append(
            f"Digester appears WASHED OUT ({detail}); the converged 'optimal' is "
            "not a viable methanogenic design."
        )
    elif sour_signals:
        classification = "acidified_sour"
        is_sour = True
        warnings.append(
            "Digester converged to an ACIDIFIED / SOUR basin ("
            + "; ".join(sour_signals)
            + "); this 'optimal' is not a healthy methanogenic design."
        )
    elif ph_value is None and total_vfa is None and ch4_fraction is None:
        classification = "indeterminate"
        is_sour = False
        warnings.append(
            "Digester basin could not be classified (pH / VFA / biogas state "
            "unavailable); treat the result as unverified."
        )
    else:
        # Healthy only when the metrics we DO have are all in the healthy band.
        healthy = True
        if ph_value is not None and not (HEALTHY_PH_LOW <= ph_value <= HEALTHY_PH_HIGH):
            healthy = False
        if ch4_fraction is not None and ch4_fraction < HEALTHY_CH4_FRACTION_MIN:
            healthy = False
        if total_vfa is not None and total_vfa > SOUR_TOTAL_VFA_KG_COD_M3:
            healthy = False
        if acetate is not None and acetate > FAILURE_ACETATE_KG_COD_M3:
            healthy = False
        if healthy:
            classification = "healthy_methanogenic"
            is_sour = False
        else:
            classification = "indeterminate"
            is_sour = False
            warnings.append(
                "Digester basin is marginal / indeterminate (not clearly healthy "
                "nor clearly sour); inspect pH, VFA, and biogas composition."
            )

    activity = None
    if reactions is not None and t is not None:
        # Keep biological classification separate from thermodynamic validity.
        # Reading the class avoids Pyomo's missing-property construction path.
        reporter = getattr(type(reactions[t]), "activity_report", None)
        if reporter is not None:
            activity = reporter(reactions[t])
            if not activity["activity_model_in_domain"]:
                warnings.append("Davies acid-base result is outside its declared I/temperature domain; "
                                "use an independent thermodynamic audit. Ionic strength was not clamped.")
    return {
        "classification": classification,
        "is_sour": is_sour,
        "is_healthy": classification == "healthy_methanogenic",
        "pH": ph_value,
        "ch4_fraction": ch4_fraction,
        "h2_partial_pressure_pa": h2_partial,
        "headspace_pressure_pa": headspace_pressure,
        "total_vfa_kg_cod_m3": total_vfa,
        "acetate_kg_cod_m3": acetate,
        "methanogen_biomass_kg_m3": methanogen_biomass,
        "hydrogenotroph_biomass_kg_m3": hydrogenotroph_biomass,
        "warnings": warnings,
        "activity_model": activity,
    }
