"""ASM2D flowsheet template exports."""
from __future__ import annotations
import uuid
from typing import Any, Dict, List, Optional
from watertap_engine_mcp.templates.asm2d import ASM2DBiologicalConfig, ASM2DMBRMLEPOSTTemplate, ASM2DMBRMLETemplate, ASM2DMLEPOSTTemplate, ASM2DMLETemplate

class DirectAsm2DSolveResult:
    """Small attribute-style result for synchronous ASM2D smoke solves."""

    def __init__(self, *, status: str, session_id: str, diagnostics: Dict[str, Any], artifact_persistence: Optional[Dict[str, Any]]=None) -> None:
        self.status = status
        self.session_id = session_id
        self.diagnostics = diagnostics
        self.artifact_persistence = artifact_persistence or {}

    def to_dict(self) -> Dict[str, Any]:
        return {'status': self.status, 'session_id': self.session_id, 'diagnostics': self.diagnostics, 'artifact_persistence': self.artifact_persistence}

def template_config_from_influent(*, flow_m3_d: float, cod_mg_l: float, tkn_mg_l: float, tp_mg_l: float, tss_mg_l: float, influent_character: str, anoxic_volume_m3: float, aerobic_volume_m3: float, post_anoxic_volume_m3: float, target_srt_days: float, target_mlss_mg_l: Optional[float], bod5_mg_l: Optional[float]=None, alkalinity_mg_caco3_l: Optional[float]=None) -> Dict[str, Any]:
    """Build ASM2D template config from public influent metrics."""
    from puran_mcp_support.engineering.heuristics.asm2d_influent_fractionation import TenderInfluentBasis, fractionate_to_asm2d
    basis = TenderInfluentBasis(flow_m3_d=flow_m3_d, COD=cod_mg_l, BOD5=bod5_mg_l, alkalinity=alkalinity_mg_caco3_l, TKN=tkn_mg_l, TP=tp_mg_l, TSS=tss_mg_l, influent_character=influent_character)
    return {'feed_flow_m3_per_day': flow_m3_d, 'feed_temperature_K': basis.temperature_K, 'alkalinity_mol_per_m3': basis.alkalinity_mol_per_m3, 'feed_conc_kg_m3': fractionate_to_asm2d(basis), 'anoxic_volume_m3': anoxic_volume_m3, 'aerobic_volume_m3': aerobic_volume_m3, 'post_anoxic_volume_m3': post_anoxic_volume_m3, 'target_srt_days': target_srt_days, 'target_mlss_mg_l': target_mlss_mg_l}

def _stream_metric_from_port(port: Any) -> Dict[str, float]:
    from puran_mcp_support.engineering.heuristics.asm2d_influent_fractionation import asm2d_cod_kg_m3, asm2d_tp_kg_m3
    from pyomo.environ import value
    species: Dict[str, float] = {}
    concentrations = getattr(port, 'conc_mass_comp', None)
    if concentrations is not None:
        for key in list(concentrations.keys()):
            component = key[-1] if isinstance(key, tuple) else key
            if not isinstance(component, str) or component == 'H2O':
                continue
            try:
                species[component] = float(value(concentrations[0, component]))
            except Exception:
                continue
    flow_m3_d = None
    flow_vol = getattr(port, 'flow_vol', None)
    if flow_vol is not None:
        try:
            flow_m3_d = float(value(flow_vol[0])) * 86400.0
        except Exception:
            flow_m3_d = None
    cod_mg_l = asm2d_cod_kg_m3(species) * 1000.0 if species else None
    bod5_mg_l = None
    tn_mg_l = None
    if species:
        bod5_mg_l = 1000.0 * (0.6 * float(species.get('S_F', 0.0)) + 0.4 * float(species.get('S_A', 0.0)) + 0.25 * float(species.get('X_S', 0.0)))
        tn_mg_l = 1000.0 * (float(species.get('S_NH4', 0.0)) + float(species.get('S_NO3', 0.0)) + 0.07 * (float(species.get('X_H', 0.0)) + float(species.get('X_PAO', 0.0)) + float(species.get('X_AUT', 0.0))) + 0.03 * float(species.get('X_I', 0.0)) + 0.04 * float(species.get('X_S', 0.0)))
    return {'flow_m3_d': flow_m3_d, 'COD_mg_L': cod_mg_l, 'BOD5_mg_L': bod5_mg_l, 'TN_mg_L': tn_mg_l, 'TP_mg_L': asm2d_tp_kg_m3(species) * 1000.0 if species else None}

def _flow_ratio_from_ports(numerator_port: Any, denominator_port: Any) -> float | None:
    from pyomo.environ import value
    try:
        numerator = float(value(numerator_port.flow_vol[0]))
        denominator = float(value(denominator_port.flow_vol[0]))
    except Exception:
        return None
    if denominator <= 0:
        return None
    return numerator / denominator

def asm2d_direct_diagnostics(session: Any, units: Dict[str, Any], pipeline_result: Any, pipeline_history: List[Any]) -> Dict[str, Any]:
    """Extract diagnostics for direct ASM2D smoke solves.

    Unit lookups resolve by structural role tag (no name inference) so a
    from-scratch role-tagged train yields the same KPI extraction as the
    canonical template.
    """
    from pyomo.environ import value
    from watertap_engine_mcp.core.roles import RoleResolver, UnitRole
    roles = RoleResolver.from_session(session)

    def _single_unit(role: UnitRole) -> Optional[Any]:
        return roles.single_unit(role, units)
    mbr = _single_unit(UnitRole.MBR_SEPARATOR)
    clarifier = _single_unit(UnitRole.SECONDARY_CLARIFIER)
    feed = _single_unit(UnitRole.FEED)
    ras_pressure = _single_unit(UnitRole.RAS_PRESSURE)
    ir_pressure = _single_unit(UnitRole.INTERNAL_RECYCLE_PRESSURE)
    stage_bindings = roles.ordered_stages((UnitRole.PRE_ANOXIC, UnitRole.AEROBIC, UnitRole.POST_ANOXIC), units, default_role_order=(UnitRole.PRE_ANOXIC, UnitRole.AEROBIC, UnitRole.POST_ANOXIC))
    aerobic_ids = [binding.unit_id for binding in stage_bindings if binding.role == UnitRole.AEROBIC.value]
    pre_anoxic_ids = [binding.unit_id for binding in stage_bindings if binding.role == UnitRole.PRE_ANOXIC.value]
    aerobic = units.get(aerobic_ids[-1]) if aerobic_ids else None
    anoxic = units.get(pre_anoxic_ids[0]) if pre_anoxic_ids else None
    effluent_port = getattr(mbr, 'permeate', None) if mbr is not None else getattr(clarifier, 'effluent', None)
    mbr_cage = getattr(mbr, 'retentate', None) if mbr is not None else None
    effluent = _stream_metric_from_port(effluent_port) if effluent_port is not None else {}
    feed_port = getattr(feed, 'outlet', None) if feed is not None else None
    recycle_ratios = {}
    if feed_port is not None and ras_pressure is not None:
        ras_port = getattr(ras_pressure, 'outlet', None)
        recycle_ratios['RAS_q_over_qin'] = _flow_ratio_from_ports(ras_port, feed_port)
    if feed_port is not None and ir_pressure is not None:
        ir_port = getattr(ir_pressure, 'outlet', None)
        recycle_ratios['MLR_q_over_qin'] = _flow_ratio_from_ports(ir_port, feed_port)
    mlss_mg_l = None
    if mbr_cage is not None and hasattr(mbr_cage, 'conc_mass_comp'):
        try:
            mlss_mg_l = float(value(mbr_cage.conc_mass_comp[0, 'X_TSS'])) * 1000.0
        except Exception:
            mlss_mg_l = None
    basin_mlss_mg_l = None
    x_aut_mg_l = None
    x_h_mg_l = None
    anoxic_do_mg_l = None
    aerobic_outlet = getattr(aerobic, 'outlet', None) if aerobic is not None else None
    if aerobic_outlet is not None and hasattr(aerobic_outlet, 'conc_mass_comp'):
        for component, target in (('X_TSS', 'basin_mlss_mg_l'), ('X_AUT', 'x_aut_mg_l'), ('X_H', 'x_h_mg_l')):
            try:
                resolved = float(value(aerobic_outlet.conc_mass_comp[0, component])) * 1000.0
            except Exception:
                resolved = None
            if target == 'basin_mlss_mg_l':
                basin_mlss_mg_l = resolved
            elif target == 'x_aut_mg_l':
                x_aut_mg_l = resolved
            elif target == 'x_h_mg_l':
                x_h_mg_l = resolved
    anoxic_outlet = getattr(anoxic, 'outlet', None) if anoxic is not None else None
    if anoxic_outlet is not None and hasattr(anoxic_outlet, 'conc_mass_comp'):
        try:
            anoxic_do_mg_l = float(value(anoxic_outlet.conc_mass_comp[0, 'S_O2'])) * 1000.0
        except Exception:
            anoxic_do_mg_l = None
    cage_basin_ratio = None
    try:
        if mlss_mg_l is not None and basin_mlss_mg_l and (basin_mlss_mg_l > 0):
            cage_basin_ratio = mlss_mg_l / basin_mlss_mg_l
    except (TypeError, ValueError):
        cage_basin_ratio = None
    details = getattr(pipeline_result, 'details', {})
    solve_result = details.get('solve_result', {}) if isinstance(details, dict) else {}
    design_parameters = session.config.runtime_metadata.get('design_parameters')
    if not isinstance(design_parameters, dict):
        design_parameters = {}
    raw_termination_condition = solve_result.get('termination_condition') or (details.get('termination_condition') if isinstance(details, dict) else None)
    accepted_near_feasible = bool(solve_result.get('accepted_near_feasible'))
    solution_quality = 'near_feasible' if accepted_near_feasible else 'strict_optimal' if raw_termination_condition == 'optimal' else 'infeasible_structured' if raw_termination_condition else None
    return {'termination_condition': raw_termination_condition, 'solver_termination_condition': raw_termination_condition, 'accepted_near_feasible': accepted_near_feasible, 'solution_quality': solution_quality, 'pipeline_state': getattr(getattr(pipeline_result, 'state', None), 'value', None), 'history': [{'state': getattr(item.state, 'value', str(item.state)), 'success': item.success, 'message': item.message, 'warnings': item.warnings, 'errors': item.errors} for item in pipeline_history], 'MLSS_mg_L': mlss_mg_l, 'MLSS_cage_mg_L': mlss_mg_l, 'MLSS_basin_mg_L': basin_mlss_mg_l, 'MLSS_cage_to_basin_ratio': cage_basin_ratio, 'X_AUT_mg_L': x_aut_mg_l, 'X_H_mg_L': x_h_mg_l, 'anoxic_DO_mg_L': anoxic_do_mg_l, 'SRT_d': design_parameters.get('target_srt_days'), 'effluent': effluent, 'recycle_ratios': recycle_ratios, 'solver_infeasibility_report': details.get('solver_infeasibility_report') if isinstance(details, dict) else None}

def solve_asm2d_template_sync(*, template_name: str, template_config: Dict[str, Any], session_prefix: str, project_ref: Optional[str]=None, plant_slug: Optional[str]=None, max_wall_time_s: int=300) -> DirectAsm2DSolveResult:
    """Run a synchronous ASM2D template smoke solve."""
    import watertap_engine_mcp.actions as actions
    from watertap_engine_mcp.solver.pipeline import HygienePipeline, PipelineConfig
    from watertap_engine_mcp.utils.model_builder import ModelBuilder
    session_id = f'{session_prefix}-{uuid.uuid4()}'
    created = actions.apply_template(template_name, session_id=session_id, template_config=template_config, name=session_prefix)
    if 'error' in created or 'code' in created:
        return DirectAsm2DSolveResult(status='build_error', session_id=session_id, diagnostics=created)
    session = actions.session_manager.load(session_id)
    if project_ref:
        session.config.runtime_metadata['project_ref'] = project_ref
    if plant_slug:
        session.config.runtime_metadata['plant_slug'] = plant_slug
    actions.session_manager.save(session)
    builder = ModelBuilder(session)
    model = builder.build()
    units = builder.get_units()
    runtime_solver_options = session.config.runtime_metadata.get('solver_options')
    solver_options = runtime_solver_options if isinstance(runtime_solver_options, dict) else {}
    pipeline = HygienePipeline(model, PipelineConfig(solver_options=solver_options, max_wall_time_s=max_wall_time_s, allow_near_feasible_asm2d=True), units=units, session=session)
    result = pipeline.run_all()
    diagnostics = asm2d_direct_diagnostics(session, units, result, pipeline.history)
    details = result.details if isinstance(result.details, dict) else {}
    solve_result = details.get('solve_result') if isinstance(details.get('solve_result'), dict) else {}
    termination = solve_result.get('termination_condition') or details.get('termination_condition')
    accepted_near_feasible = bool(solve_result.get('accepted_near_feasible'))
    if result.success and accepted_near_feasible:
        status = 'near_feasible'
    elif result.success and termination == 'optimal':
        status = 'optimal'
    else:
        status = details.get('convergence_status') or 'failed'
    artifact_persistence = {}
    report = diagnostics.get('solver_infeasibility_report')
    if status == 'solver_infeasible_with_diagnostic' and isinstance(report, dict) and project_ref:
        artifact_persistence = actions.persist_solver_infeasibility_report(report, project_ref=project_ref, plant_slug=plant_slug, session_id=session_id)
    return DirectAsm2DSolveResult(status=status, session_id=session_id, diagnostics=diagnostics, artifact_persistence=artifact_persistence)
__all__ = ['ASM2DBiologicalConfig', 'ASM2DMBRMLEPOSTTemplate', 'ASM2DMBRMLETemplate', 'ASM2DMLEPOSTTemplate', 'ASM2DMLETemplate', 'DirectAsm2DSolveResult', 'asm2d_direct_diagnostics', 'solve_asm2d_template_sync', 'template_config_from_influent']
