"""WaterTAP per-engine GSA evaluate-adapter.

Bridges the engine-agnostic Global Sensitivity Analysis core
(``engineering_utils.sensitivity``) to the WaterTAP steady-state twin. The GSA
core drives an ``evaluate(inputs: dict) -> dict | None`` callable; this module
manufactures that callable from a *base solved* WaterTAP session:

1. A declarative **factor -> perturbation map** turns each sampled GSA factor
   value into ``evaluate_flowsheet`` ``perturbed_inputs`` (feed concentrations,
   feed scalars, fixed vars, process constraints / design parameters).
2. ``make_watertap_evaluate`` **bootstraps a complete variable snapshot** of the
   base design point once (full-init solve), then warm-starts every perturbed
   sample from that snapshot with the Initialization stage skipped so IPOPT
   polishes the neighbour instead of re-converging from a cold modular init.
   A failed warm solve falls back to a robust cold full-init solve; a solve
   that still does not converge is returned as ``None`` (failed-solve-as-outcome
   — the GSA core records it, nothing is imputed).
3. The nested ``evaluate_flowsheet`` KPI payload is translated into the flat
   effluent / MLSS / SRT scalars the GSA core decomposes, reusing the canonical
   ``engineering_utils.heuristics.asm2d_influent_fractionation`` aggregators
   (COD / TN / NO3 / TKN / TP) so the bulk parameters match the simulation
   report exactly.

The module imports WaterTAP-side ``actions`` lazily and is meant to run
in-process inside the watertap-engine-mcp server (the ``run_global_sensitivity``
action), never from the engine-agnostic ``engineering_utils`` library.
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

__all__ = [
    "DANONE_TWIN_TOPOLOGY",
    "BIOMASS_COMPONENTS",
    "default_danone_factor_map",
    "build_perturbed_inputs",
    "translate_kpis",
    "make_watertap_evaluate",
]

# ASM2D biomass components used for the SRT inventory / wasting balance.
BIOMASS_COMPONENTS = ("X_H", "X_PAO", "X_AUT")

# Organic-strength feed components scaled coherently by the ``influent_COD``
# factor (COD-bearing soluble + particulate organics, plus X_TSS so the
# particulate solids track the COD they carry).
_ORGANIC_STRENGTH_COMPONENTS = ("S_F", "S_A", "S_I", "X_I", "X_S", "X_TSS")
# COD basis used to convert an absolute target COD into a scale factor (matches
# ``asm2d_cod_kg_m3``: S_F + S_A + S_I + X_I + X_S, the X_AUT term is trace).
_COD_BASIS_COMPONENTS = ("S_F", "S_A", "S_I", "X_I", "X_S")


# --------------------------------------------------------------------------- #
# Danone Nutricia Lalru MBR-MLE-post twin topology binding
# --------------------------------------------------------------------------- #
DANONE_TWIN_TOPOLOGY: Dict[str, Any] = {
    "feed": "RawFeed",
    "effluent_unit": "MembraneTrain",
    "effluent_port": "permeate",
    "effluent_fallback": ("Permeate", "inlet"),
    "aeration_unit": "AerationTK05",
    "aeration_port": "outlet",
    "reactor_units": ["PreAnoxicTK04", "AerationTK05", "PostAnoxicTK05B"],
    "was_unit": "WASSplit",
    "was_port": "waste",
}


def default_danone_factor_map() -> Dict[str, Dict[str, Any]]:
    """Built-in factor -> perturbation map for the Danone MBR-MLE-post twin.

    Each factor name maps to a perturbation *action* (or list of actions). The
    action ``kind`` selects how the scalar factor value becomes an
    ``evaluate_flowsheet`` perturbation. Concentration factor values are mg/L,
    temperature is degC, SRT is days, metal dose / carbon dose are mg/L.
    """

    feed = DANONE_TWIN_TOPOLOGY["feed"]
    return {
        # Influent organic strength: scale COD-bearing components (+X_TSS) to a
        # target total COD [mg/L], preserving the soluble/particulate split.
        "influent_COD": {
            "kind": "feed_conc_scale",
            "feed": feed,
            "components": list(_ORGANIC_STRENGTH_COMPONENTS),
            "basis_components": list(_COD_BASIS_COMPONENTS),
            "value_units": "mg_L",
        },
        # Influent nitrate -> S_NO3 absolute [mg/L].
        "NO3_N": {
            "kind": "feed_conc_set",
            "feed": feed,
            "component": "S_NO3",
            "value_to_kg_m3": 1e-3,
        },
        # Influent TKN handle -> S_NH4 absolute [mg/L] (ammonifiable N proxy).
        "TKN_N": {
            "kind": "feed_conc_set",
            "feed": feed,
            "component": "S_NH4",
            "value_to_kg_m3": 1e-3,
        },
        # Feed temperature [degC] -> feed-state temperature [K].
        "temperature_C": {
            "kind": "feed_scalar",
            "feed": feed,
            "arg": "temperature",
            "scale": 1.0,
            "offset": 273.15,
        },
        # Solids retention time [d] -> SRT process-constraint target (mirrored
        # into the design-parameter warm-seed).
        "SRT_d": {
            "kind": "process_constraint",
            "match": "srt",
            "field": "target_days",
            "mirror_design_parameter": "target_srt_days",
        },
        # Supplemental external carbon [mg COD/L] -> additive readily
        # biodegradable acetate (S_A) at the boundary. NOTE: evaluate_flowsheet
        # perturbs existing inputs only and cannot add a discrete post-anoxic
        # dosing stream/unit, so supplemental carbon is modelled as added feed
        # rbCOD feeding the pre-anoxic denitrification zone (MLR recycle).
        "supplemental_carbon_mgCOD_L": {
            "kind": "feed_conc_add",
            "feed": feed,
            "component": "S_A",
            "value_to_kg_m3": 1e-3,
        },
        # Chemical-P metal dose [kg/m3] on the ASM2D chemical-P block.
        "metal_dose": {
            "kind": "fixed_var",
            "unit": "ChemPFeCl3",
            "var": "metal_dose[0]",
            "scale": 1.0,
        },
        # Internal mixed-liquor recycle ratio (MLR) -> recycle-ratio constraint.
        "R_IR_mlr": {
            "kind": "process_constraint",
            "match": "asm2d_mlr_recycle_ratio",
            "field": "target_ratio",
        },
        # Return activated sludge recycle ratio (RAS).
        "R_IR_ras": {
            "kind": "process_constraint",
            "match": "asm2d_ras_recycle_ratio",
            "field": "target_ratio",
        },
        # Aeration intensity (oxygen transfer) [1/hr].
        "KLa": {
            "kind": "fixed_var",
            "unit": "AerationTK05",
            "var": "KLa",
            "scale": 1.0,
        },
    }


# --------------------------------------------------------------------------- #
# Perturbation builder
# --------------------------------------------------------------------------- #
def _coerce_actions(spec: Any) -> List[Dict[str, Any]]:
    if isinstance(spec, dict):
        return [spec]
    if isinstance(spec, (list, tuple)):
        return [s for s in spec if isinstance(s, dict)]
    return []


def build_perturbed_inputs(
    inputs: Mapping[str, float],
    factor_map: Mapping[str, Any],
    base_feed_conc: Mapping[str, Mapping[str, float]],
) -> Dict[str, Any]:
    """Translate one GSA sample (factor -> value) into ``perturbed_inputs``.

    ``base_feed_conc`` is ``{feed_id: {component: base_conc_kg_m3}}`` captured
    from the base session; it backs the scale / add actions. Unknown factors
    (no map entry) are ignored so a problem can carry annotation-only factors.
    """

    pert: Dict[str, Any] = {}
    feed_conc: Dict[str, Dict[str, float]] = {}
    feed_scalar: Dict[str, Dict[str, float]] = {}
    fixed_vars: Dict[str, Dict[str, float]] = {}
    process_constraints: Dict[str, Dict[str, Any]] = {}
    design_parameters: Dict[str, Any] = {}

    for name, value in inputs.items():
        spec = factor_map.get(name)
        if spec is None:
            continue
        fval = float(value)
        for action in _coerce_actions(spec):
            kind = action.get("kind")

            if kind == "feed_conc_set":
                feed = action["feed"]
                comp = action["component"]
                mult = float(action.get("value_to_kg_m3", 1.0))
                feed_conc.setdefault(feed, {})[comp] = fval * mult

            elif kind == "feed_conc_add":
                feed = action["feed"]
                comp = action["component"]
                mult = float(action.get("value_to_kg_m3", 1.0))
                base = float(base_feed_conc.get(feed, {}).get(comp, 0.0))
                feed_conc.setdefault(feed, {})[comp] = base + fval * mult

            elif kind == "feed_conc_scale":
                feed = action["feed"]
                comps = action.get("components", [])
                basis = action.get("basis_components", comps)
                base_map = base_feed_conc.get(feed, {})
                # Target is an absolute total over the basis components.
                target_kg = fval * (1e-3 if action.get("value_units") == "mg_L" else 1.0)
                base_total = sum(float(base_map.get(c, 0.0)) for c in basis)
                if base_total <= 0.0:
                    continue
                scale = target_kg / base_total
                for c in comps:
                    feed_conc.setdefault(feed, {})[c] = float(base_map.get(c, 0.0)) * scale

            elif kind == "feed_scalar":
                feed = action["feed"]
                arg = action["arg"]
                scale = float(action.get("scale", 1.0))
                offset = float(action.get("offset", 0.0))
                feed_scalar.setdefault(feed, {})[arg] = fval * scale + offset

            elif kind == "fixed_var":
                unit = action["unit"]
                var = action["var"]
                scale = float(action.get("scale", 1.0))
                fixed_vars.setdefault(unit, {})[var] = fval * scale

            elif kind == "process_constraint":
                match = action["match"]
                field = action["field"]
                scale = float(action.get("scale", 1.0))
                process_constraints.setdefault(match, {})[field] = fval * scale
                mirror = action.get("mirror_design_parameter")
                if mirror:
                    design_parameters[mirror] = fval * scale

            elif kind == "design_parameter":
                key = action["key"]
                scale = float(action.get("scale", 1.0))
                design_parameters[key] = fval * scale

    if feed_conc:
        pert["feed_conc"] = feed_conc
    if feed_scalar:
        pert["feed_scalar"] = feed_scalar
    if fixed_vars:
        pert["fixed_vars"] = fixed_vars
    if process_constraints:
        pert["process_constraints"] = process_constraints
    if design_parameters:
        pert["design_parameters"] = design_parameters
    return pert


# --------------------------------------------------------------------------- #
# KPI translation (nested evaluate_flowsheet KPIs -> flat bulk scalars)
# --------------------------------------------------------------------------- #
def _component_key(raw_key: Any) -> str:
    """Return the ASM2D component name from a stream conc index key.

    Keys arrive as ``"S_F"`` (component-only index) or ``"0.0,S_F"`` (time +
    component); the component is always the last comma-separated token.
    """

    text = str(raw_key)
    return text.split(",")[-1].strip()


def _conc_mass_comp_kg_m3(stream: Mapping[str, Any]) -> Dict[str, float]:
    raw = stream.get("conc_mass_comp")
    out: Dict[str, float] = {}
    if isinstance(raw, Mapping):
        for key, val in raw.items():
            if val is None:
                continue
            try:
                out[_component_key(key)] = float(val)
            except (TypeError, ValueError):
                continue
    return out


def _scalar_value(obj: Any) -> Optional[float]:
    if isinstance(obj, Mapping):
        # time-indexed {"0": v}
        for v in obj.values():
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
        return None
    if obj is None:
        return None
    try:
        return float(obj)
    except (TypeError, ValueError):
        return None


def _stream(kpis: Mapping[str, Any], unit: str, port: str) -> Optional[Mapping[str, Any]]:
    streams = kpis.get("streams")
    if not isinstance(streams, Mapping):
        return None
    unit_streams = streams.get(unit)
    if not isinstance(unit_streams, Mapping):
        return None
    stream = unit_streams.get(port)
    return stream if isinstance(stream, Mapping) else None


def translate_kpis(
    kpis: Mapping[str, Any],
    topology: Mapping[str, Any],
    reactor_volumes_m3: Mapping[str, float],
) -> Dict[str, Optional[float]]:
    """Flatten the nested evaluate_flowsheet KPI payload into bulk scalars.

    Returns effluent TN / NO3 / NH3 / TKN / COD / TP / TSS / BOD5 [mg/L], MLSS
    [mg/L] at the aeration reactor, achieved SRT [d], and ``feasible``=1.0.
    Any sub-KPI that cannot be computed is ``None`` (the GSA core masks it).
    """

    from puran_mcp_support.engineering.heuristics.asm2d_influent_fractionation import asm2d_cod_kg_m3, asm2d_no3_kg_m3, asm2d_tkn_kg_m3, asm2d_tp_kg_m3

    out: Dict[str, Optional[float]] = {
        "effluent_COD": None,
        "effluent_TN": None,
        "effluent_NO3": None,
        "effluent_NH3": None,
        "effluent_TKN": None,
        "effluent_TP": None,
        "effluent_TSS": None,
        "effluent_BOD5": None,
        "MLSS": None,
        "SRT_d": None,
        "feasible": 1.0,
    }

    # --- effluent bulk parameters ---
    eff = _stream(kpis, topology["effluent_unit"], topology["effluent_port"])
    if eff is None:
        fb = topology.get("effluent_fallback")
        if fb:
            eff = _stream(kpis, fb[0], fb[1])
    if eff is not None:
        kg_m3 = _conc_mass_comp_kg_m3(eff)
        if kg_m3:
            mg_l = {c: v * 1000.0 for c, v in kg_m3.items()}
            # Regulatory N excludes dissolved nitrogen gas (S_N2, the
            # denitrification end-product that off-gasses): asm2d_tkn_kg_m3
            # folds S_N2 into its "soluble" term, so subtract it. TKN here is
            # organic-bound + ammonia N; TN = TKN + NO3.
            no3_kg = asm2d_no3_kg_m3(kg_m3)
            tkn_kg = asm2d_tkn_kg_m3(kg_m3) - float(kg_m3.get("S_N2", 0.0))
            out["effluent_COD"] = asm2d_cod_kg_m3(kg_m3) * 1000.0
            out["effluent_TN"] = (tkn_kg + no3_kg) * 1000.0
            out["effluent_NO3"] = no3_kg * 1000.0
            out["effluent_TKN"] = tkn_kg * 1000.0
            out["effluent_NH3"] = mg_l.get("S_NH4")
            out["effluent_TP"] = asm2d_tp_kg_m3(kg_m3) * 1000.0
            try:
                from watertap_engine_mcp.ontology.asm2d import asm2d_to_watertap_tss_equivalent

                out["effluent_TSS"] = asm2d_to_watertap_tss_equivalent(mg_l).get("TSS_mg_L")
            except Exception:
                out["effluent_TSS"] = mg_l.get("X_TSS")
            out["effluent_BOD5"] = 1000.0 * (
                0.60 * float(kg_m3.get("S_F", 0.0))
                + 0.40 * float(kg_m3.get("S_A", 0.0))
                + 0.25 * float(kg_m3.get("X_S", 0.0))
            )

    # --- MLSS at the aeration reactor (X_TSS) ---
    aer = _stream(kpis, topology["aeration_unit"], topology["aeration_port"])
    if aer is not None:
        aer_kg = _conc_mass_comp_kg_m3(aer)
        if "X_TSS" in aer_kg:
            out["MLSS"] = aer_kg["X_TSS"] * 1000.0

    # --- achieved SRT = biomass inventory / WAS biomass mass flow ---
    try:
        inventory_kg = 0.0
        for reactor in topology["reactor_units"]:
            vol = float(reactor_volumes_m3.get(reactor, 0.0))
            if vol <= 0.0:
                continue
            r_stream = _stream(kpis, reactor, "outlet")
            if r_stream is None:
                continue
            r_kg = _conc_mass_comp_kg_m3(r_stream)
            biomass = sum(float(r_kg.get(c, 0.0)) for c in BIOMASS_COMPONENTS)
            inventory_kg += vol * biomass  # kg/m3 * m3 = kg
        was = _stream(kpis, topology["was_unit"], topology["was_port"])
        if was is not None and inventory_kg > 0.0:
            was_kg = _conc_mass_comp_kg_m3(was)
            q_was = _scalar_value(was.get("flow_vol"))  # m3/s
            was_biomass = sum(float(was_kg.get(c, 0.0)) for c in BIOMASS_COMPONENTS)
            if q_was and was_biomass > 0.0:
                was_rate_kg_d = q_was * was_biomass * 86400.0
                if was_rate_kg_d > 0.0:
                    out["SRT_d"] = inventory_kg / was_rate_kg_d
    except Exception:
        out["SRT_d"] = None

    return out


# --------------------------------------------------------------------------- #
# Evaluate factory
# --------------------------------------------------------------------------- #
def _capture_base_feed_conc(session: Any) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    for feed_id, feed_state in (session.feed_states or {}).items():
        if not isinstance(feed_state, dict):
            continue
        cmc = (feed_state.get("state_args") or {}).get("conc_mass_comp") or {}
        conc: Dict[str, float] = {}
        for k, v in cmc.items():
            sv = _scalar_value(v) if isinstance(v, Mapping) else None
            if sv is None:
                try:
                    sv = float(v)
                except (TypeError, ValueError):
                    sv = None
            if sv is not None:
                conc[str(k)] = sv
        out[feed_id] = conc
    return out


def _capture_reactor_volumes(session: Any, reactor_units: List[str]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for unit_id in reactor_units:
        unit = (session.units or {}).get(unit_id)
        if unit is None:
            continue
        vol = (unit.fixed_vars or {}).get("volume[0]")
        if vol is not None:
            try:
                out[unit_id] = float(vol)
            except (TypeError, ValueError):
                pass
    return out


def make_watertap_evaluate(
    base_session_id: str,
    kpi_names: List[str],
    factor_to_perturbation_map: Optional[Mapping[str, Any]] = None,
    *,
    factors: Optional[Sequence[Any]] = None,
    local_basin_threshold: Optional[float] = None,
    topology: Optional[Mapping[str, Any]] = None,
    warm_max_iter: int = 1000,
    warm_max_wall_time_s: int = 70,
    cold_max_wall_time_s: int = 160,
    cold_fallback: bool = True,
    diagnostics: Optional[Dict[str, Any]] = None,
) -> Callable[[Mapping[str, float]], Optional[Dict[str, float]]]:
    """Build a GSA ``evaluate(inputs) -> kpis | None`` over a WaterTAP twin.

    Bootstraps a complete variable snapshot of ``base_session_id`` (full-init
    solve) for warm-starting, then returns a closure that, per GSA sample:
      * maps factor values to ``evaluate_flowsheet`` perturbations,
      * decides warm-vs-cold by the sample's distance from the nominal design
        point: a sample inside the local basin (small Morris-screen perturbation)
        warm-starts from the base snapshot with init skipped (fast path); a wide
        perturbation (Sobol design crossing the local basin) is FORCED to a cold
        full-init solve so the warm start cannot pull IPOPT onto a non-physical
        iterate outside the basin,
      * for in-basin samples, still falls back to a cold solve if the warm solve
        fails,
      * returns the flat KPI dict, or ``None`` if the solve never converged
        (failed-solve-as-outcome — the GSA core records it; nothing imputed).

    ``factors`` are the GSA :class:`Factor` specs (carrying nominal / low / high);
    when provided, each sample's normalised displacement from nominal is compared
    against ``local_basin_threshold`` (fraction of a factor's span; default
    ``engineering_utils.sensitivity.DEFAULT_LOCAL_BASIN_THRESHOLD``) to gate the
    cold-solve forcing. With no factors the legacy warm-then-cold path is kept.

    ``diagnostics`` (optional) is populated with bootstrap + per-eval timing,
    warm/cold/forced-cold path counts, per-eval ``solution_quality``, the failure
    fraction, and failure accounting for reporting.
    """

    import watertap_engine_mcp.actions as actions  # watertap-side, in-process

    from puran_mcp_support.engineering.sensitivity import DEFAULT_LOCAL_BASIN_THRESHOLD, relative_displacement

    topo = dict(topology or DANONE_TWIN_TOPOLOGY)
    factor_map = dict(factor_to_perturbation_map or default_danone_factor_map())
    diag = diagnostics if diagnostics is not None else {}
    factor_list = list(factors or [])
    threshold = (
        float(local_basin_threshold)
        if local_basin_threshold is not None
        else float(DEFAULT_LOCAL_BASIN_THRESHOLD)
    )

    base_session = actions.session_manager.load(base_session_id)
    base_feed_conc = _capture_base_feed_conc(base_session)
    reactor_volumes = _capture_reactor_volumes(base_session, list(topo["reactor_units"]))

    # Bootstrap a COMPLETE variable snapshot (state + derived property vars) via
    # a full-init solve. The persisted solved_state holds only state vars, which
    # leaves the material/enthalpy balances inconsistent on a warm start.
    t0 = time.time()
    boot = actions.evaluate_flowsheet(
        base_session_id,
        perturbed_inputs=None,
        skip_initialization=False,
        max_wall_time_s=cold_max_wall_time_s,
    )
    boot_dt = time.time() - t0
    complete_snapshot = boot.get("solved_values") if isinstance(boot, dict) else None
    diag["bootstrap"] = {
        "converged": bool(boot.get("converged")) if isinstance(boot, dict) else False,
        "wall_time_s": round(boot_dt, 2),
        "snapshot_vars": len(complete_snapshot) if complete_snapshot else 0,
        "status": boot.get("status") if isinstance(boot, dict) else None,
    }
    diag["local_basin_threshold"] = threshold
    diag["cold_solve_forcing_enabled"] = bool(factor_list)
    diag.setdefault("evals", [])
    diag.setdefault(
        "counters",
        {"warm_ok": 0, "cold_ok": 0, "forced_cold": 0, "failed": 0, "n": 0},
    )

    def evaluate(inputs: Mapping[str, float]) -> Optional[Dict[str, float]]:
        pert = build_perturbed_inputs(inputs, factor_map, base_feed_conc)
        diag["counters"]["n"] += 1

        # Distance from the nominal design point governs whether the warm start
        # (seeded from the base solved snapshot) is trustworthy. Beyond the local
        # basin we FORCE a cold solve so a stale warm seed cannot drag IPOPT onto
        # a non-physical iterate on a wide perturbation.
        rel = relative_displacement(inputs, factor_list) if factor_list else 0.0
        force_cold = bool(factor_list) and rel > threshold
        rec: Dict[str, Any] = {
            "path": None,
            "warm_time_s": None,
            "cold_time_s": None,
            "max_rel_displacement": round(float(rel), 4),
            "forced_cold": force_cold,
            "solution_quality": None,
        }

        result = None
        if complete_snapshot and not force_cold:
            t = time.time()
            r = actions.evaluate_flowsheet(
                base_session_id,
                perturbed_inputs=pert,
                warm_start_values=complete_snapshot,
                skip_initialization=True,
                solver_options={"max_iter": int(warm_max_iter)},
                max_wall_time_s=int(warm_max_wall_time_s),
            )
            rec["warm_time_s"] = round(time.time() - t, 2)
            if isinstance(r, dict):
                rec["solution_quality"] = r.get("solution_quality")
                if r.get("converged"):
                    result = r
                    rec["path"] = "warm"
                    diag["counters"]["warm_ok"] += 1

        # Cold solve: the primary path for a forced-cold (out-of-basin) sample,
        # and the fallback for an in-basin sample whose warm solve failed.
        if result is None and (cold_fallback or force_cold):
            t = time.time()
            r = actions.evaluate_flowsheet(
                base_session_id,
                perturbed_inputs=pert,
                skip_initialization=False,
                max_wall_time_s=int(cold_max_wall_time_s),
            )
            rec["cold_time_s"] = round(time.time() - t, 2)
            if isinstance(r, dict):
                rec["solution_quality"] = r.get("solution_quality")
                if r.get("converged"):
                    result = r
                    rec["path"] = "cold_forced" if force_cold else "cold"
                    diag["counters"]["cold_ok"] += 1
                    if force_cold:
                        diag["counters"]["forced_cold"] += 1

        if result is None or not isinstance(result.get("kpis"), dict):
            diag["counters"]["failed"] += 1
            rec["path"] = rec["path"] or "failed"
            diag["evals"].append(rec)
            _update_failure_fraction(diag)
            return None  # failed-solve-as-outcome — never imputed

        flat = translate_kpis(result["kpis"], topo, reactor_volumes)
        diag["evals"].append(rec)
        _update_failure_fraction(diag)
        # Return the full flat KPI dict; the GSA core selects problem.outputs.
        return {k: v for k, v in flat.items() if v is not None}

    return evaluate


def _update_failure_fraction(diag: Dict[str, Any]) -> None:
    """Keep a running per-evaluate failure fraction on the adapter diagnostics."""

    counters = diag.get("counters")
    if not isinstance(counters, dict):
        return
    n = counters.get("n") or 0
    failed = counters.get("failed") or 0
    diag["failure_fraction"] = (failed / n) if n else 0.0
