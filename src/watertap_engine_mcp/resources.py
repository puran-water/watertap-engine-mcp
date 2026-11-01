"""MCP resources for the WaterTAP Engine.

Resources are read-only, cacheable data surfaces. They complement tools
(which mutate state) and prompts (which guide workflows). Resources resolve
``session_manager`` and ``job_manager`` at read-time via the ``actions``
module so that test-injected managers propagate correctly.
"""
from __future__ import annotations
import json
from dataclasses import asdict, fields, is_dataclass
from enum import Enum
from typing import Any, Dict, List, Optional
from fastmcp import FastMCP
import watertap_engine_mcp.actions as action_module
from watertap_engine_mcp.core.downstream_handoff_contract import list_downstream_handoff_profiles
from watertap_engine_mcp.core.property_registry import PROPERTY_PACKAGES, PropertyPackageType
from watertap_engine_mcp.core.session import _serialize_dict_keys
from watertap_engine_mcp.core.session_presets import SESSION_PRESETS
from watertap_engine_mcp.core.unit_registry import UNITS, UnitSpec

def _sync_server_state() -> None:
    """Propagate adapter-level manager overrides into the action module.

    Mirrors the tool-side _sync_shared_state() in server.py so that
    resources honor test-injected managers set on the server module.
    Only syncs if the server module is already imported (avoids circular imports).
    """
    import sys
    _srv = sys.modules.get('server')
    if _srv is not None:
        action_module.session_manager = _srv.session_manager
        action_module.job_manager = _srv.job_manager

def _serialize_value(v: Any) -> Any:
    """Recursively convert dataclasses, enums, sets, and tuples to JSON-safe types."""
    if isinstance(v, PropertyPackageType):
        return v.name
    if isinstance(v, Enum):
        return v.value
    if is_dataclass(v) and (not isinstance(v, type)):
        return {f.name: _serialize_value(getattr(v, f.name)) for f in fields(v)}
    if isinstance(v, dict):
        return {str(k): _serialize_value(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [_serialize_value(item) for item in v]
    if isinstance(v, set):
        return sorted((_serialize_value(item) for item in v))
    if isinstance(v, type):
        return v.__name__
    return v

def _serialize_unit_spec(spec: UnitSpec) -> Dict[str, Any]:
    """Convert a UnitSpec (with nested dataclasses/enums) to a JSON-safe dict."""
    payload = _serialize_value(spec)
    payload['canonical_unit_op_type'] = spec.canonical_unit_op_type.value
    return payload

def _serialize_preset(preset) -> Dict[str, Any]:
    return {'name': preset.name, 'description': preset.description, 'property_package': preset.property_package.name, 'property_package_config_defaults': preset.property_package_config_defaults, 'required_override_keys': list(preset.required_override_keys)}

def _serialize_package_spec(spec) -> Dict[str, Any]:
    return {'pkg_type': spec.pkg_type.name, 'class_name': spec.class_name, 'module_path': spec.module_path, 'phases': sorted(spec.phases), 'required_components': spec.required_components, 'optional_components': spec.optional_components, 'state_vars': spec.state_vars, 'flow_basis': spec.flow_basis, 'requires_reaction_package': spec.requires_reaction_package, 'compatible_reaction_packages': spec.compatible_reaction_packages, 'charge_balance_required': spec.charge_balance_required, 'database_required': spec.database_required, 'default_scaling': spec.default_scaling, 'config_kwargs': spec.config_kwargs, 'requires_config': spec.requires_config, 'config_fields': spec.config_fields}
_REFERENCE_FEEDS: Dict[str, Dict[str, Any]] = {'MCAS_BRACKISH_5ION': {'flow_vol_m3_hr': 100.0, 'temperature_C': 25.0, 'pressure_bar': 1.0, 'components': {'Na_+': 11122.0, 'Ca_2+': 382.0, 'Mg_2+': 1394.0, 'Cl_-': 20101.6, 'SO4_2-': 2136.0}, 'concentration_units': 'mg/L', 'concentration_basis': 'mass', 'component_charges': {'Na_+': 1, 'Ca_2+': 2, 'Mg_2+': 2, 'Cl_-': -1, 'SO4_2-': -2}, 'electroneutrality_species': 'Cl_-'}, 'ED_NACL_PAIR': {'flow_vol_m3_hr': 100.0, 'temperature_C': 25.0, 'pressure_bar': 1.0, 'components': {'Na_+': 1970.0, 'Cl_-': 3030.0}, 'concentration_units': 'mg/L', 'concentration_basis': 'mass', 'component_charges': {'Na_+': 1, 'Cl_-': -1}, 'electroneutrality_species': 'Cl_-'}}
_KPI_PORT_ALLOWLIST = ['inlet', 'outlet', 'permeate', 'retentate', 'feed', 'product', 'brine', 'feed_inlet', 'feed_outlet', 'product_outlet', 'waste_outlet', 'inlet_feed', 'outlet_brine', 'outlet_vapor']
_KPI_STATE_VAR_ALLOWLIST = ['flow_mass_phase_comp', 'flow_mol_phase_comp', 'flow_vol', 'temperature', 'pressure', 'conc_mass_phase_comp', 'conc_mol_phase_comp', 'mass_frac_phase_comp', 'mole_frac_phase_comp']
_KPI_UNIT_METRIC_ALLOWLIST = ['recovery_frac_mass_H2O', 'recovery_vol_phase', 'specific_energy_consumption', 'area', 'flux_mass_io_phase_comp', 'dens_mass_phase', 'work_mechanical', 'deltaP', 'efficiency_pump', 'recovery_frac', 'heat_transfer', 'U', 'delta_temperature', 'lmtd']
_RESULT_SCHEMAS: Dict[str, Dict[str, Any]] = {'ro_train': {'template': 'ro_train', 'property_package': 'SEAWATER', 'notes': ['KPI extraction uses an allowlist of port/state-var/metric names — not all unit-specific variables are captured.', "Stream keys in solved_state.streams are flat dot-separated: 'RO.permeate', 'Feed.outlet'.", 'Port snapshot entries are keyed like flow_mass_phase_comp[Liq,H2O] with snapshot dict values {value, fixed, lb, ub, units, stale}.'], 'kpi_allowlists': {'port_names': _KPI_PORT_ALLOWLIST, 'state_vars': _KPI_STATE_VAR_ALLOWLIST, 'unit_metrics': _KPI_UNIT_METRIC_ALLOWLIST}, 'key_result_paths': {'kpis.units.RO.recovery_frac_mass_H2O': {'type': 'float', 'units': 'fraction', 'range': [0.3, 0.85]}, 'kpis.units.RO.area': {'type': 'float', 'units': 'm²'}, 'kpis.units.RO.flux_mass_io_phase_comp': {'type': 'dict', 'units': 'kg/m²/s', 'description': 'Indexed by (time, phase, component)'}, 'kpis.units.HPPump.work_mechanical': {'type': 'float', 'units': 'W'}, 'kpis.streams.RO.permeate.flow_mass_phase_comp': {'type': 'dict', 'description': 'Permeate mass flows by (phase, component)'}, 'kpis.streams.RO.retentate.flow_mass_phase_comp': {'type': 'dict', 'description': 'Brine mass flows by (phase, component)'}, 'solved_state.streams["RO.permeate"]': {'type': 'dict', 'description': 'Full permeate port snapshot (flat key)'}, 'solved_state.streams["RO.retentate"]': {'type': 'dict', 'description': 'Full brine port snapshot (flat key)'}, 'watertap_engine_mcp.costing.LCOW': {'type': 'float', 'units': '$/m³', 'range': [0.3, 3.0]}, 'watertap_engine_mcp.costing.aggregate_capital_cost': {'type': 'float', 'units': '$'}, 'watertap_engine_mcp.costing.total_operating_cost': {'type': 'float', 'units': '$/yr'}}, 'engineering_ranges': {'recovery': {'min': 0.3, 'max': 0.85, 'units': 'fraction'}, 'rejection': {'min': 0.99, 'max': 0.9999, 'units': 'fraction'}, 'flux_LMH': {'min': 15.0, 'max': 30.0, 'units': 'L/m²/hr'}, 'LCOW': {'min': 0.5, 'max': 3.0, 'units': '$/m³'}}}, 'nf_softening': {'template': 'nf_softening', 'property_package': 'MCAS', 'notes': ['Uses MCAS property package with 5-ion brackish config.', 'KPI extraction uses the same allowlist as RO — NF-specific rejection variables may not appear in kpis.units. Use get_unit_results() for full coverage.', "Stream keys: 'NF.permeate', 'NF.retentate', 'Feed.outlet'.", 'Hardness removal: compare Ca²⁺/Mg²⁺ in Feed.outlet vs NF.permeate.', 'Divalent rejection: Ca²⁺/Mg²⁺/SO₄²⁻ should be >90% rejected.', 'Monovalent passage: Na⁺/Cl⁻ pass through at higher rates than divalents.'], 'kpi_allowlists': {'port_names': _KPI_PORT_ALLOWLIST, 'state_vars': _KPI_STATE_VAR_ALLOWLIST, 'unit_metrics': _KPI_UNIT_METRIC_ALLOWLIST}, 'key_result_paths': {'kpis.streams.NF.permeate.flow_mass_phase_comp': {'type': 'dict', 'description': 'Permeate mass flows (MCAS components)'}, 'kpis.streams.NF.retentate.flow_mass_phase_comp': {'type': 'dict', 'description': 'Concentrate mass flows'}, 'solved_state.streams["NF.permeate"]': {'type': 'dict', 'description': 'Full permeate port snapshot (flat key)'}, 'solved_state.streams["NF.retentate"]': {'type': 'dict', 'description': 'Full concentrate port snapshot (flat key)'}, 'solved_state.streams["Feed.outlet"]': {'type': 'dict', 'description': 'Feed outlet — compare with permeate for removal calc'}, 'watertap_engine_mcp.costing.LCOW': {'type': 'float', 'units': '$/m³', 'range': [0.1, 1.5]}}, 'analysis_guidance': {'hardness_removal': 'Compare Ca_2+ and Mg_2+ flow_mass_phase_comp between Feed.outlet and NF.permeate', 'divalent_rejection': 'Ca_2+, Mg_2+, SO4_2- rejection = 1 - (permeate / feed) for each', 'monovalent_passage': 'Na+, Cl- pass through at higher rates — lower rejection than divalents'}, 'engineering_ranges': {'Ca_removal': {'min': 0.9, 'max': 0.99, 'units': 'fraction'}, 'Mg_removal': {'min': 0.95, 'max': 0.99, 'units': 'fraction'}, 'operating_pressure_bar': {'min': 3.0, 'max': 8.0, 'units': 'bar'}}}, 'mvc_evaporator': {'template': 'mvc_evaporator', 'property_package': 'SEAWATER', 'notes': ['MVC evaporator: Evaporator (SEAWATER) + Compressor/Condenser (WATER).', 'Distillate uses auto-translator WATER → SEAWATER.', 'Condenser is non-costable — costing returns partial with condenser as blocker/skipped.', 'SEAWATER validity: vapor-pressure to 160 g/kg, BPE to 120 g/kg. Not for ZLD.', 'Thermal coupling (Evaporator ↔ Condenser) set via template thermal_couplings.'], 'kpi_allowlists': {'port_names': _KPI_PORT_ALLOWLIST, 'state_vars': _KPI_STATE_VAR_ALLOWLIST, 'unit_metrics': _KPI_UNIT_METRIC_ALLOWLIST}, 'key_result_paths': {'kpis.units.Evaporator.heat_transfer': {'type': 'float', 'units': 'W'}, 'kpis.units.Evaporator.area': {'type': 'float', 'units': 'm²'}, 'kpis.units.Evaporator.U': {'type': 'float', 'units': 'W/m²/K'}, 'kpis.units.Compressor.work_mechanical': {'type': 'float', 'units': 'W'}, 'kpis.streams.Evaporator.outlet_brine.flow_mass_phase_comp': {'type': 'dict', 'description': 'Brine mass flows by (phase, component)'}, 'solved_state.streams["Evaporator.outlet_brine"]': {'type': 'dict', 'description': 'Full brine port snapshot (flat key)'}, 'watertap_engine_mcp.costing.LCOW': {'type': 'float', 'units': '$/m³', 'description': 'Partial — excludes non-costable Condenser.'}}, 'engineering_ranges': {'specific_energy_kWh_m3': {'min': 8.0, 'max': 25.0, 'units': 'kWh/m³'}, 'brine_concentration_gL': {'min': 70, 'max': 120, 'units': 'g/L'}, 'heat_transfer_area_m2': {'min': 50, 'max': 2000, 'units': 'm²'}}}}

def register_resources(mcp: FastMCP) -> None:
    """Register all MCP resources onto the server instance."""

    @mcp.resource('watertap://presets/{preset_name}/spec')
    def get_preset_spec(preset_name: str) -> str:
        """Session preset specification (property package, config defaults, required overrides)."""
        key = preset_name.upper()
        if key not in SESSION_PRESETS:
            valid = sorted(SESSION_PRESETS)
            return json.dumps({'error': f"Unknown preset '{preset_name}'", 'valid_presets': valid})
        return json.dumps(_serialize_preset(SESSION_PRESETS[key]), indent=2)

    @mcp.resource('watertap://presets/{preset_name}/reference-feed')
    def get_reference_feed(preset_name: str) -> str:
        """Reference feed-water state for set_feed_state(). Only available for presets with sufficient chemistry data."""
        key = preset_name.upper()
        if key in _REFERENCE_FEEDS:
            return json.dumps(_REFERENCE_FEEDS[key], indent=2)
        if key in SESSION_PRESETS:
            preset = SESSION_PRESETS[key]
            return json.dumps({'error': f"Preset '{key}' does not have a canonical reference feed state.", 'reason': 'Insufficient chemistry defaults to construct a complete feed.', 'required_override_keys': list(preset.required_override_keys), 'available_presets_with_feeds': sorted(_REFERENCE_FEEDS)})
        valid = sorted(SESSION_PRESETS)
        return json.dumps({'error': f"Unknown preset '{preset_name}'", 'valid_presets': valid})

    @mcp.resource('watertap://packages/{package_name}/spec')
    def get_package_spec(package_name: str) -> str:
        """Property package specification (components, state vars, scaling, config)."""
        try:
            pkg_type = PropertyPackageType[package_name.upper()]
        except KeyError:
            valid = sorted((p.name for p in PropertyPackageType))
            return json.dumps({'error': f"Unknown package '{package_name}'", 'valid_packages': valid})
        spec = PROPERTY_PACKAGES.get(pkg_type)
        if spec is None:
            return json.dumps({'error': f"Package '{package_name}' has no spec in registry"})
        return json.dumps(_serialize_package_spec(spec), indent=2)

    @mcp.resource('watertap://units/{unit_type}/spec')
    def get_unit_spec_resource(unit_type: str) -> str:
        """Full unit specification (engineering inputs, ports, costing, init hints, profiles)."""
        spec = UNITS.get(unit_type)
        if spec is None:
            return json.dumps({'error': f"Unknown unit type '{unit_type}'", 'hint': 'Use list_units() tool for available types'})
        return json.dumps(_serialize_unit_spec(spec), indent=2)

    @mcp.resource('watertap://handoff-profiles')
    def get_handoff_profiles() -> str:
        """Downstream QSDsan → WaterTAP ingress profiles with required fields."""
        from watertap_engine_mcp.core.downstream_handoff_contract import INGRESS_PROFILES
        profiles = []
        for profile in INGRESS_PROFILES.values():
            profiles.append({'name': profile.name, 'target_profile': profile.name, 'session_preset': profile.session_preset, 'property_package': profile.property_package.name, 'description': profile.description, 'public_supported': profile.public_supported, 'public_reason': profile.public_reason, 'strict_contract': profile.strict_contract, 'estimated_by_design': profile.estimated_by_design, 'ingress_mode': 'exact' if profile.strict_contract else 'approximate', 'required_state_fields': list(profile.required_state_fields), 'required_config_fields': list(profile.required_config_fields)})
        return json.dumps(sorted(profiles, key=lambda p: p['name']), indent=2)

    @mcp.resource('watertap://templates/{template_name}/result-schema')
    def get_result_schema(template_name: str) -> str:
        """Machine-readable result field paths, types, units, and engineering ranges."""
        key = template_name.strip().lower().replace('-', '_')
        if key not in _RESULT_SCHEMAS:
            return json.dumps({'error': f"No result schema for template '{template_name}'", 'available': sorted(_RESULT_SCHEMAS)})
        return json.dumps(_RESULT_SCHEMAS[key], indent=2)

    @mcp.resource('watertap://templates/{template_name}/session-spec')
    def get_template_session_spec(template_name: str) -> str:
        """Static session spec from a template (units, connections, DOF fixes, scaling, init order)."""
        from watertap_engine_mcp.templates.registry import get_template_spec, build_template
        try:
            spec = get_template_spec(template_name)
        except KeyError:
            from watertap_engine_mcp.templates.registry import TEMPLATES
            return json.dumps({'error': f"Unknown template '{template_name}'", 'available': sorted(TEMPLATES)})
        if not spec.supported:
            return json.dumps({'error': f"Template '{template_name}' is not supported", 'reason': spec.reason})
        try:
            template = build_template(template_name)
            session_spec = template.to_session_spec()
            return json.dumps(_serialize_value(session_spec), indent=2)
        except Exception as e:
            return json.dumps({'error': f'Failed to build session spec: {e}'})

    @mcp.resource('watertap://sessions/{session_id}/manifest')
    def get_session_manifest(session_id: str) -> str:
        """Single-read artifact index for a solved session. Generated on read."""
        _sync_server_state()
        sm = action_module.session_manager
        try:
            session = sm.load(session_id)
        except FileNotFoundError:
            return json.dumps({'error': f"Session '{session_id}' not found"})
        artifacts: Dict[str, Any] = {}
        results = session.results or {}
        for key in ('state', 'details', 'history', 'certification', 'kpis', 'solved_state', 'costing'):
            if key in results:
                artifacts[key] = {'available': True, 'resource': f'watertap://sessions/{session_id}/results'}
        if 'solved_state' in results:
            streams = results['solved_state'].get('streams', {})
            artifacts['streams'] = {'available': True, 'count': len(streams), 'keys': sorted(streams.keys()), 'resource_pattern': f'watertap://sessions/{session_id}/streams/{{stream_key}}'}
        if 'costing' in results:
            artifacts['costing'] = {'available': True, 'resource': f'watertap://sessions/{session_id}/costing'}
        kpi_snapshot = {}
        kpis = results.get('kpis', {})
        for unit_id, unit_kpis in kpis.get('units', {}).items():
            interesting = {}
            for k, v in unit_kpis.items():
                if isinstance(v, (int, float)):
                    interesting[k] = v
            if interesting:
                kpi_snapshot[unit_id] = interesting
        manifest = {'session_id': session_id, 'status': session.status.value, 'artifacts': artifacts, 'kpi_snapshot': kpi_snapshot, 'diagram_resource': f'watertap://sessions/{session_id}/diagram'}
        return json.dumps(manifest, indent=2)

    @mcp.resource('watertap://sessions/{session_id}/results')
    def get_session_results(session_id: str) -> str:
        """Full session results (state, details, history, certification, kpis, solved_state, costing)."""
        _sync_server_state()
        sm = action_module.session_manager
        try:
            session = sm.load(session_id)
        except FileNotFoundError:
            return json.dumps({'error': f"Session '{session_id}' not found"})
        if not session.results:
            return json.dumps({'session_id': session_id, 'status': session.status.value, 'warning': 'No results available. Run solve() first.'})
        payload = {'session_id': session_id, 'status': session.status.value, 'solve_status': session.solve_status, 'solve_message': session.solve_message, 'results': _serialize_dict_keys(session.results)}
        try:
            from watertap_engine_mcp.actions import _flowsheet_certification
            payload['certification'] = _flowsheet_certification(session)
        except Exception:
            pass
        return json.dumps(payload, indent=2, default=str)

    @mcp.resource('watertap://sessions/{session_id}/streams/{stream_key}')
    def get_session_stream(session_id: str, stream_key: str) -> str:
        """Solved port snapshot for a single stream (e.g., 'RO.permeate').

        Stream keys are flat dot-separated: 'RO.permeate', 'Feed.outlet'.
        For indexed ports (e.g., 'Mixer.outlet[0]'), URL-encode the key
        when constructing the URI path segment.
        """
        _sync_server_state()
        sm = action_module.session_manager
        try:
            session = sm.load(session_id)
        except FileNotFoundError:
            return json.dumps({'error': f"Session '{session_id}' not found"})
        solved_state = (session.results or {}).get('solved_state', {})
        streams = solved_state.get('streams', {})
        if stream_key not in streams:
            return json.dumps({'error': f"Stream '{stream_key}' not found", 'available_streams': sorted((str(k) for k in streams.keys()))})
        return json.dumps(_serialize_dict_keys(streams[stream_key]), indent=2, default=str)

    @mcp.resource('watertap://sessions/{session_id}/costing')
    def get_session_costing(session_id: str) -> str:
        """Costing report (LCOW, CAPEX/OPEX, per-unit breakdown)."""
        _sync_server_state()
        sm = action_module.session_manager
        try:
            session = sm.load(session_id)
        except FileNotFoundError:
            return json.dumps({'error': f"Session '{session_id}' not found"})
        costing = (session.results or {}).get('costing')
        if costing is None:
            return json.dumps({'error': 'No costing data available. Enable costing and solve first.', 'session_id': session_id})
        return json.dumps(_serialize_dict_keys(costing), indent=2, default=str)

    @mcp.resource('watertap://sessions/{session_id}/diagram')
    def get_session_diagram(session_id: str) -> str:
        """ASCII flowsheet diagram (JSON dict with diagram, units, connections)."""
        _sync_server_state()
        sm = action_module.session_manager
        try:
            session = sm.load(session_id)
        except FileNotFoundError:
            return json.dumps({'error': f"Session '{session_id}' not found"})
        lines = ['Flowsheet Diagram', '=' * 40]
        if session.feed_states:
            for feed_unit in sorted(session.feed_states):
                lines.append(f'{feed_unit} → ...')
        for conn in session.connections:
            arrow = ' → '
            if conn.translator_id:
                arrow = f' →[{conn.translator_id}]→ '
            lines.append(f'  {conn.source_unit}.{conn.source_port}{arrow}{conn.dest_unit}.{conn.dest_port}')
        return json.dumps({'diagram': '\n'.join(lines), 'units': list(session.units.keys()), 'connections': len(session.connections)}, indent=2)

    @mcp.resource('watertap://jobs/{job_id}/results')
    def get_job_results_resource(job_id: str) -> str:
        """Raw job result dict for a completed background job."""
        _sync_server_state()
        jm = action_module.job_manager
        job = jm.get_status(job_id)
        if job is None:
            return json.dumps({'error': f"Job '{job_id}' not found"})
        if job.result is None:
            return json.dumps({'error': f"Job '{job_id}' has no results (status: {job.status.value})", 'job_id': job_id, 'status': job.status.value, 'progress': job.progress, 'message': job.message})
        return json.dumps(_serialize_dict_keys(job.result), indent=2, default=str)

    @mcp.resource('watertap://jobs/{job_id}/status')
    def get_job_status_resource(job_id: str) -> str:
        """Lightweight job status (status, progress, message, timing)."""
        _sync_server_state()
        jm = action_module.job_manager
        job = jm.get_status(job_id)
        if job is None:
            return json.dumps({'error': f"Job '{job_id}' not found"})
        return json.dumps({'job_id': job_id, 'status': job.status.value, 'progress': job.progress, 'message': job.message, 'created_at': job.created_at, 'started_at': job.started_at, 'completed_at': job.completed_at}, indent=2)
