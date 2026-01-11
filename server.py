#!/usr/bin/env python3
"""WaterTAP Engine MCP Server.

FastMCP server exposing WaterTAP flowsheet building and solving capabilities.
Provides 51 atomic tools organized by category:

Core Tools (34):
- Session Management (5): create_session, create_watertap_session, get_session, list_sessions, delete_session
- Registry/Discovery (4): list_units, list_property_packages, list_translators, get_unit_spec, get_unit_requirements
- Flowsheet Building (8): create_feed, create_unit, create_translator, connect_ports, connect_units,
                          update_unit, delete_unit, validate_flowsheet, get_flowsheet_diagram
- DOF Management (5): get_dof_status, check_dof, fix_variable, unfix_variable, list_unfixed_vars
- Scaling (6): get_scaling_status, set_scaling_factor, apply_scaling, calculate_scaling_factors,
               report_scaling_issues, autoscale_large_jac

Solver Operations (10):
- Initialization (3): initialize_unit, initialize_flowsheet, get_initialization_order, propagate_state
- Solving (5): check_solve, solve, build_and_solve, get_solve_status, get_job_status, get_job_results
- Diagnostics (4): run_diagnostics, diagnose_failure, get_constraint_residuals, get_bound_violations

Domain-Specific (7):
- Zero-Order (3): load_zo_parameters, list_zo_databases, get_zo_unit_parameters
- Results (4): get_results, get_stream_results, get_unit_results, get_costing

Design Principles:
- Explicit operations only - NO hidden automation
- Domain intelligence delegated to companion skill
- Orchestrates WaterTAP/IDAES utilities rather than replacing them
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from core import (
    PropertyPackageType,
    PROPERTY_PACKAGES,
    get_property_package_spec,
    TRANSLATORS,
    get_translator,
    check_compatibility,
    find_translator_chain,
    UnitCategory,
    UNITS,
    get_unit_spec,
    list_units as list_units_registry,
    WaterTAPState,
    SessionConfig,
    FlowsheetSession,
    SessionManager,
)
from utils import JobManager, JobStatus


# Initialize FastMCP server
mcp = FastMCP(
    "watertap-engine-mcp",
    description="WaterTAP flowsheet building and solving engine",
)

# Configuration
STORAGE_DIR = Path(__file__).parent / "jobs"
FLOWSHEETS_DIR = STORAGE_DIR / "flowsheets"

# Initialize managers
session_manager = SessionManager(FLOWSHEETS_DIR)
job_manager = JobManager(STORAGE_DIR / "jobs")


# ============================================================================
# SESSION MANAGEMENT TOOLS (4)
# ============================================================================

@mcp.tool()
def create_session(
    name: str = "",
    description: str = "",
    property_package: str = "SEAWATER",
    property_package_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a new WaterTAP flowsheet session.

    Args:
        name: Optional session name
        description: Optional session description
        property_package: Default property package (SEAWATER, NACL, NACL_T_DEP,
            WATER, MCAS, ZERO_ORDER, ASM1, ASM2D, ASM3, ADM1, etc.)
        property_package_config: Configuration for property packages that require it:
            - MCAS requires: {"solute_list": [...], "charge": {...}, "mw_data": {...}}
            - ZERO_ORDER requires: {"database": "default", "water_source": "..."}

    Returns:
        Dict with session_id and configuration details
    """
    try:
        pkg_type = PropertyPackageType[property_package.upper()]
    except KeyError:
        valid = [p.name for p in PropertyPackageType]
        return {"error": f"Invalid property_package. Valid: {valid}"}

    # Validate config for packages that require it
    pkg_spec = PROPERTY_PACKAGES.get(pkg_type)
    if pkg_spec and pkg_spec.requires_config:
        if not property_package_config:
            return {
                "error": f"Property package {property_package} requires configuration",
                "required_fields": pkg_spec.config_fields,
                "config_help": pkg_spec.config_kwargs,
            }

    config = SessionConfig(
        name=name,
        description=description,
        default_property_package=pkg_type,
        property_package_config=property_package_config or {},
    )
    session = FlowsheetSession(config=config)
    session_manager.save(session)

    return {
        "session_id": session.config.session_id,
        "name": name,
        "property_package": property_package,
        "property_package_config": property_package_config,
        "status": session.status.value,
        "created_at": session.config.created_at,
    }


@mcp.tool()
def get_session(session_id: str) -> Dict[str, Any]:
    """Get details of an existing session.

    Args:
        session_id: Session identifier

    Returns:
        Full session state including units, connections, and DOF status
    """
    try:
        session = session_manager.load(session_id)
        return session.to_dict()
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}


@mcp.tool()
def list_sessions() -> List[Dict[str, Any]]:
    """List all flowsheet sessions.

    Returns:
        List of session summaries (id, name, status, timestamps)
    """
    return session_manager.list_sessions()


@mcp.tool()
def delete_session(session_id: str) -> Dict[str, Any]:
    """Delete a flowsheet session.

    Args:
        session_id: Session to delete

    Returns:
        Confirmation or error message
    """
    try:
        session_manager.delete(session_id)
        return {"deleted": session_id}
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}


# ============================================================================
# REGISTRY/DISCOVERY TOOLS (4)
# ============================================================================

@mcp.tool()
def list_units(
    category: Optional[str] = None,
    property_package: Optional[str] = None,
    is_idaes: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """List available unit types with optional filtering.

    Args:
        category: Filter by category (membrane, thermal, pump, etc.)
        property_package: Filter by compatible property package
        is_idaes: Filter IDAES vs WaterTAP units

    Returns:
        List of unit specifications
    """
    cat = None
    if category:
        try:
            cat = UnitCategory(category.lower())
        except ValueError:
            valid = [c.value for c in UnitCategory]
            return [{"error": f"Invalid category. Valid: {valid}"}]

    pkg = None
    if property_package:
        try:
            pkg = PropertyPackageType[property_package.upper()]
        except KeyError:
            valid = [p.name for p in PropertyPackageType]
            return [{"error": f"Invalid property_package. Valid: {valid}"}]

    units = list_units_registry(category=cat, property_package=pkg, is_idaes=is_idaes)

    return [
        {
            "unit_type": u.unit_type,
            "category": u.category.value,
            "module_path": u.module_path,
            "is_idaes_unit": u.is_idaes_unit,
            "n_inlets": u.n_inlets,
            "n_outlets": u.n_outlets,
            "description": u.description,
        }
        for u in units
    ]


@mcp.tool()
def list_property_packages() -> List[Dict[str, Any]]:
    """List all available property packages with their specifications.

    Returns:
        List of property package details including module paths,
        components, flow basis, and compatibility info.

    Note: Some class names are duplicated across modules (NaClParameterBlock,
    WaterParameterBlock). Always use the full module_path for imports.
    """
    return [
        {
            "name": spec.pkg_type.name,
            "class_name": spec.class_name,
            "module_path": spec.module_path,
            "phases": list(spec.phases),
            "required_components": spec.required_components,
            "flow_basis": spec.flow_basis,
            "requires_reaction_package": spec.requires_reaction_package,
            "charge_balance_required": spec.charge_balance_required,
            "database_required": spec.database_required,
        }
        for spec in PROPERTY_PACKAGES.values()
    ]


@mcp.tool()
def list_translators() -> List[Dict[str, Any]]:
    """List available property package translators.

    IMPORTANT: Only ASM↔ADM translators exist in WaterTAP!
    For non-biological flowsheets, use the SAME property package throughout.

    Returns:
        List of translator specifications with source/destination packages
    """
    return [
        {
            "name": t.name,
            "source": t.source_pkg.name,
            "destination": t.dest_pkg.name,
            "module_path": t.module_path,
            "requires_reaction_packages": t.requires_reaction_packages,
            "description": t.description,
        }
        for t in TRANSLATORS.values()
    ]


@mcp.tool()
def get_unit_spec(unit_type: str) -> Dict[str, Any]:
    """Get full specification for a unit type.

    Args:
        unit_type: Unit type name (e.g., "ReverseOsmosis0D")

    Returns:
        Complete unit specification including DOF requirements,
        typical values, scaling defaults, and initialization hints
    """
    try:
        spec = get_unit_spec(unit_type)
        return {
            "unit_type": spec.unit_type,
            "class_name": spec.class_name,
            "module_path": spec.module_path,
            "category": spec.category.value,
            "compatible_property_packages": [p.name for p in spec.compatible_property_packages],
            "required_fixes": [
                {
                    "name": v.name,
                    "description": v.description,
                    "units": v.units,
                    "typical_min": v.typical_min,
                    "typical_max": v.typical_max,
                    "typical_default": v.typical_default,
                    "indexed": v.indexed,
                }
                for v in spec.required_fixes
            ],
            "typical_values": spec.typical_values,
            "default_scaling": spec.default_scaling,
            "init_hints": {
                "method": spec.init_hints.method.value if spec.init_hints else None,
                "requires_state_args": spec.init_hints.requires_state_args if spec.init_hints else False,
                "special_requirements": spec.init_hints.special_requirements if spec.init_hints else [],
            } if spec.init_hints else None,
            "n_inlets": spec.n_inlets,
            "n_outlets": spec.n_outlets,
            "inlet_names": spec.inlet_names,
            "outlet_names": spec.outlet_names,
            "is_idaes_unit": spec.is_idaes_unit,
            "description": spec.description,
        }
    except KeyError:
        return {"error": f"Unknown unit type: {unit_type}"}


# ============================================================================
# FLOWSHEET BUILDING TOOLS (5)
# ============================================================================

@mcp.tool()
def create_feed(
    session_id: str,
    flow_vol_m3_hr: float,
    tds_mg_L: Optional[float] = None,
    nacl_mg_L: Optional[float] = None,
    components: Optional[Dict[str, float]] = None,
    temperature_C: float = 25.0,
    pressure_bar: float = 1.0,
    concentration_units: str = "mg/L",
    concentration_basis: str = "mass",
    component_charges: Optional[Dict[str, int]] = None,
    electroneutrality_species: Optional[str] = None,
) -> Dict[str, Any]:
    """Create feed state for the flowsheet.

    Args:
        session_id: Session to add feed to
        flow_vol_m3_hr: Volumetric flow rate in m³/hr
        tds_mg_L: TDS concentration (for SEAWATER package)
        nacl_mg_L: NaCl concentration (for NACL package)
        components: Dict of component concentrations (for MCAS, etc.)
        temperature_C: Temperature in Celsius
        pressure_bar: Pressure in bar
        concentration_units: Units for concentrations ("mg/L", "mol/L", "kg/m3")
        concentration_basis: "mass" or "molar" basis for concentrations
        component_charges: Dict of component charges for MCAS (e.g., {"Na_+": 1, "Cl_-": -1})
        electroneutrality_species: Species to adjust for electroneutrality balance

    Returns:
        Feed state details and state_args for property package
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    # Build components dict
    comps = components or {}
    if tds_mg_L is not None:
        comps["TDS"] = tds_mg_L
    if nacl_mg_L is not None:
        comps["NaCl"] = nacl_mg_L

    # Validate MCAS requirements
    pkg = session.config.default_property_package
    if pkg == PropertyPackageType.MCAS:
        if not component_charges:
            return {
                "error": "MCAS property package requires component_charges dict (e.g., {'Na_+': 1, 'Cl_-': -1})"
            }
        if not comps:
            return {
                "error": "MCAS property package requires components dict with concentrations"
            }

    state = WaterTAPState(
        flow_vol_m3_hr=flow_vol_m3_hr,
        temperature_C=temperature_C,
        pressure_bar=pressure_bar,
        components=comps,
        concentration_units=concentration_units,
        concentration_basis=concentration_basis,
        component_charges=component_charges,
        electroneutrality_species=electroneutrality_species,
    )

    # Convert to state_args for default property package
    try:
        state_args = state.to_state_args(session.config.default_property_package)
    except ValueError as e:
        return {"error": str(e)}

    session.feed_state = {
        "flow_vol_m3_hr": flow_vol_m3_hr,
        "temperature_C": temperature_C,
        "pressure_bar": pressure_bar,
        "components": comps,
        "concentration_units": concentration_units,
        "concentration_basis": concentration_basis,
        "component_charges": component_charges,
        "electroneutrality_species": electroneutrality_species,
        "state_args": state_args,
    }
    session_manager.save(session)

    return {
        "session_id": session_id,
        "feed_state": session.feed_state,
    }


@mcp.tool()
def create_unit(
    session_id: str,
    unit_id: str,
    unit_type: str,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a unit in the flowsheet.

    Args:
        session_id: Session to add unit to
        unit_id: Unique identifier for this unit instance
        unit_type: Type of unit (e.g., "ReverseOsmosis0D")
        config: Unit-specific configuration options

    Returns:
        Unit details and DOF requirements
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    try:
        spec = get_unit_spec(unit_type)
    except KeyError:
        return {"error": f"Unknown unit type: {unit_type}"}

    try:
        unit = session.add_unit(unit_id, unit_type, config or {})
    except ValueError as e:
        return {"error": str(e)}

    session_manager.save(session)

    return {
        "session_id": session_id,
        "unit_id": unit_id,
        "unit_type": unit_type,
        "dof_requirements": [
            {
                "name": v.name,
                "description": v.description,
                "typical_default": v.typical_default,
            }
            for v in spec.required_fixes
        ],
        "typical_values": spec.typical_values,
    }


@mcp.tool()
def create_translator(
    session_id: str,
    translator_id: str,
    source_package: str,
    dest_package: str,
) -> Dict[str, Any]:
    """Create a translator block between property packages.

    IMPORTANT: Only ASM↔ADM translators exist in WaterTAP!

    Args:
        session_id: Session to add translator to
        translator_id: Unique identifier for this translator
        source_package: Source property package type
        dest_package: Destination property package type

    Returns:
        Translator details or error if no translator exists
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    try:
        source_pkg = PropertyPackageType[source_package.upper()]
        dest_pkg = PropertyPackageType[dest_package.upper()]
    except KeyError as e:
        return {"error": f"Invalid property package: {e}"}

    translator = get_translator(source_pkg, dest_pkg)
    if translator is None:
        compat = check_compatibility(source_pkg, dest_pkg)
        return {"error": compat["message"]}

    # Use source_pkg/dest_pkg keys to match ModelBuilder expectations
    session.translators[translator_id] = {
        "name": translator.name,
        "source_pkg": source_pkg.value,  # Store enum value string
        "dest_pkg": dest_pkg.value,      # Store enum value string
        "module_path": translator.module_path,
        "config": {},  # Additional config if needed
    }
    session_manager.save(session)

    return {
        "session_id": session_id,
        "translator_id": translator_id,
        "translator": translator.name,
        "source_pkg": source_pkg.value,
        "dest_pkg": dest_pkg.value,
    }


@mcp.tool()
def connect_ports(
    session_id: str,
    source_unit: str,
    source_port: str,
    dest_unit: str,
    dest_port: str,
    translator_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Connect two units via their ports.

    Args:
        session_id: Session containing the units
        source_unit: Source unit ID
        source_port: Source port name (e.g., "outlet", "permeate")
        dest_unit: Destination unit ID
        dest_port: Destination port name (e.g., "inlet")
        translator_id: Optional translator ID if packages differ

    Returns:
        Connection details with compatibility warning if applicable
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    # Check property package compatibility
    compatibility_warning = None
    if source_unit in session.units and dest_unit in session.units:
        # Both are regular units - check if they might need a translator
        # Note: Actual package detection requires runtime model inspection
        # This provides a reminder that only ASM/ADM translators exist
        if not translator_id:
            compatibility_warning = (
                "Connection created without translator. If units use different "
                "property packages, note that only ASM/ADM translators exist in WaterTAP. "
                "For non-biological flowsheets, use the same property package throughout."
            )

    try:
        conn = session.add_connection(
            source_unit=source_unit,
            source_port=source_port,
            dest_unit=dest_unit,
            dest_port=dest_port,
            translator_id=translator_id,
        )
    except KeyError as e:
        return {"error": str(e)}

    session_manager.save(session)

    result = {
        "session_id": session_id,
        "connection": {
            "source": f"{source_unit}.{source_port}",
            "dest": f"{dest_unit}.{dest_port}",
            "translator": translator_id,
        },
    }

    if compatibility_warning:
        result["compatibility_note"] = compatibility_warning

    return result


@mcp.tool()
def get_flowsheet_diagram(session_id: str) -> Dict[str, Any]:
    """Get ASCII diagram of flowsheet structure.

    Args:
        session_id: Session to visualize

    Returns:
        ASCII diagram and unit/connection lists
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    # Build simple ASCII representation
    lines = ["Flowsheet Diagram", "=" * 40]

    if session.feed_state:
        lines.append("Feed → ...")

    for conn in session.connections:
        arrow = " → "
        if conn.translator_id:
            arrow = f" →[{conn.translator_id}]→ "
        lines.append(f"  {conn.source_unit}.{conn.source_port}{arrow}{conn.dest_unit}.{conn.dest_port}")

    return {
        "diagram": "\n".join(lines),
        "units": list(session.units.keys()),
        "connections": len(session.connections),
    }


@mcp.tool()
def update_unit(
    session_id: str,
    unit_id: str,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """Update configuration for an existing unit.

    Args:
        session_id: Session containing the unit
        unit_id: Unit to update
        config: Configuration values to update

    Returns:
        Updated unit details
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    if unit_id not in session.units:
        return {"error": f"Unit '{unit_id}' not found"}

    session.units[unit_id].config.update(config)
    session_manager.save(session)

    return {
        "session_id": session_id,
        "unit_id": unit_id,
        "updated_config": config,
    }


@mcp.tool()
def delete_unit(session_id: str, unit_id: str) -> Dict[str, Any]:
    """Delete a unit from the flowsheet.

    Also removes any connections involving this unit.

    Args:
        session_id: Session containing the unit
        unit_id: Unit to delete

    Returns:
        Confirmation and list of removed connections
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    if unit_id not in session.units:
        return {"error": f"Unit '{unit_id}' not found"}

    # Remove the unit
    del session.units[unit_id]

    # Remove connections involving this unit
    removed_conns = []
    new_conns = []
    for conn in session.connections:
        if conn.source_unit == unit_id or conn.dest_unit == unit_id:
            removed_conns.append(f"{conn.source_unit}.{conn.source_port} → {conn.dest_unit}.{conn.dest_port}")
        else:
            new_conns.append(conn)
    session.connections = new_conns

    session_manager.save(session)

    return {
        "session_id": session_id,
        "deleted": unit_id,
        "removed_connections": removed_conns,
    }


@mcp.tool()
def validate_flowsheet(session_id: str) -> Dict[str, Any]:
    """Validate flowsheet structure before building.

    Checks for:
    - All units have connections (except feed/product)
    - No orphan ports
    - Property package compatibility
    - DOF status

    Args:
        session_id: Session to validate

    Returns:
        Validation results with any issues
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    issues = []
    warnings = []

    # Check for feed state
    if not session.feed_state:
        issues.append("No feed state defined")

    # Check DOF
    total_dof = 0
    for unit_id, unit_inst in session.units.items():
        try:
            spec = get_unit_spec(unit_inst.unit_type)
            required = len(spec.required_fixes)
            fixed = len(unit_inst.fixed_vars)
            unit_dof = required - fixed
            total_dof += unit_dof
            if unit_dof > 0:
                warnings.append(f"Unit '{unit_id}' has {unit_dof} unfixed DOF")
        except KeyError:
            pass

    if total_dof != 0:
        issues.append(f"Total DOF = {total_dof} (should be 0)")

    # Check for unconnected units
    connected_units = set()
    for conn in session.connections:
        connected_units.add(conn.source_unit)
        connected_units.add(conn.dest_unit)

    for unit_id in session.units:
        if unit_id not in connected_units and len(session.units) > 1:
            warnings.append(f"Unit '{unit_id}' has no connections")

    return {
        "session_id": session_id,
        "valid": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
        "total_dof": total_dof,
    }


# ============================================================================
# DOF MANAGEMENT TOOLS (4)
# ============================================================================

@mcp.tool()
def get_dof_status(session_id: str) -> Dict[str, Any]:
    """Get degrees of freedom status for the flowsheet.

    Args:
        session_id: Session to analyze

    Returns:
        DOF count per unit and total, plus unfixed variables
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    # Analyze DOF per unit based on registry specs
    dof_by_unit = {}
    unfixed_vars = {}

    for unit_id, unit_inst in session.units.items():
        try:
            spec = get_unit_spec(unit_inst.unit_type)
        except KeyError:
            continue

        # Count required fixes minus actually fixed
        required = len(spec.required_fixes)
        fixed = len(unit_inst.fixed_vars)
        dof = required - fixed

        dof_by_unit[unit_id] = dof

        if dof > 0:
            unfixed = [
                v.name for v in spec.required_fixes
                if v.name not in unit_inst.fixed_vars
            ]
            unfixed_vars[unit_id] = unfixed

    total_dof = sum(dof_by_unit.values())
    session.update_dof_status(dof_by_unit, total_dof)
    session_manager.save(session)

    return {
        "session_id": session_id,
        "total_dof": total_dof,
        "dof_by_unit": dof_by_unit,
        "unfixed_variables": unfixed_vars,
        "ready_to_solve": total_dof == 0,
    }


@mcp.tool()
def fix_variable(
    session_id: str,
    unit_id: str,
    var_name: str,
    value: float,
) -> Dict[str, Any]:
    """Fix a variable to a specific value.

    Args:
        session_id: Session containing the unit
        unit_id: Unit containing the variable
        var_name: Variable name (e.g., "A_comp[0, H2O]")
        value: Value to fix

    Returns:
        Updated fixed variables for the unit
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    try:
        session.fix_variable(unit_id, var_name, value)
    except KeyError as e:
        return {"error": str(e)}

    session_manager.save(session)

    return {
        "session_id": session_id,
        "unit_id": unit_id,
        "fixed": {var_name: value},
        "all_fixed_vars": session.units[unit_id].fixed_vars,
    }


@mcp.tool()
def unfix_variable(
    session_id: str,
    unit_id: str,
    var_name: str,
) -> Dict[str, Any]:
    """Unfix a previously fixed variable.

    Args:
        session_id: Session containing the unit
        unit_id: Unit containing the variable
        var_name: Variable name to unfix

    Returns:
        Updated fixed variables for the unit
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    try:
        session.unfix_variable(unit_id, var_name)
    except KeyError as e:
        return {"error": str(e)}

    session_manager.save(session)

    return {
        "session_id": session_id,
        "unit_id": unit_id,
        "unfixed": var_name,
        "remaining_fixed_vars": session.units[unit_id].fixed_vars,
    }


@mcp.tool()
def list_unfixed_vars(session_id: str, unit_id: str) -> Dict[str, Any]:
    """List unfixed variables that need values for a unit.

    Args:
        session_id: Session containing the unit
        unit_id: Unit to analyze

    Returns:
        List of unfixed variables with typical values
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    if unit_id not in session.units:
        return {"error": f"Unit '{unit_id}' not found"}

    unit_inst = session.units[unit_id]
    try:
        spec = get_unit_spec(unit_inst.unit_type)
    except KeyError:
        return {"error": f"Unknown unit type: {unit_inst.unit_type}"}

    unfixed = []
    for v in spec.required_fixes:
        if v.name not in unit_inst.fixed_vars:
            unfixed.append({
                "name": v.name,
                "description": v.description,
                "units": v.units,
                "typical_default": v.typical_default,
                "typical_min": v.typical_min,
                "typical_max": v.typical_max,
            })

    return {
        "session_id": session_id,
        "unit_id": unit_id,
        "unit_type": unit_inst.unit_type,
        "unfixed_variables": unfixed,
        "count": len(unfixed),
    }


# ============================================================================
# SCALING TOOLS (4)
# ============================================================================

@mcp.tool()
def get_scaling_status(session_id: str) -> Dict[str, Any]:
    """Get current scaling factor status.

    Args:
        session_id: Session to analyze

    Returns:
        Current scaling factors by unit and recommendations
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    scaling_by_unit = {}
    recommendations = {}

    for unit_id, unit_inst in session.units.items():
        scaling_by_unit[unit_id] = unit_inst.scaling_factors

        try:
            spec = get_unit_spec(unit_inst.unit_type)
            # Recommend defaults not yet applied
            recs = {
                k: v for k, v in spec.default_scaling.items()
                if k not in unit_inst.scaling_factors
            }
            if recs:
                recommendations[unit_id] = recs
        except KeyError:
            pass

    return {
        "session_id": session_id,
        "scaling_factors": scaling_by_unit,
        "recommendations": recommendations,
    }


@mcp.tool()
def set_scaling_factor(
    session_id: str,
    unit_id: str,
    var_name: str,
    factor: float,
) -> Dict[str, Any]:
    """Set scaling factor for a variable.

    Args:
        session_id: Session containing the unit
        unit_id: Unit containing the variable
        var_name: Variable name
        factor: Scaling factor (e.g., 1e12 for A_comp)

    Returns:
        Updated scaling factors for the unit
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    try:
        session.set_scaling_factor(unit_id, var_name, factor)
    except KeyError as e:
        return {"error": str(e)}

    session_manager.save(session)

    return {
        "session_id": session_id,
        "unit_id": unit_id,
        "set": {var_name: factor},
        "all_scaling_factors": session.units[unit_id].scaling_factors,
    }


@mcp.tool()
def apply_scaling(
    session_id: str,
    unit_id: str,
    var_name: str,
    factor: float,
) -> Dict[str, Any]:
    """Apply a scaling factor to a variable (alias for set_scaling_factor).

    Args:
        session_id: Session containing the unit
        unit_id: Unit containing the variable
        var_name: Variable name
        factor: Scaling factor (e.g., 1e12 for A_comp)

    Returns:
        Updated scaling factors for the unit
    """
    return set_scaling_factor(session_id, unit_id, var_name, factor)


@mcp.tool()
def calculate_scaling_factors(session_id: str) -> Dict[str, Any]:
    """Calculate scaling factors using IDAES utilities.

    This calls iscale.calculate_scaling_factors() which recursively
    invokes unit-specific scaling methods.

    Args:
        session_id: Session to scale

    Returns:
        Status and any auto-calculated factors
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    # Build the Pyomo model
    try:
        from utils.model_builder import ModelBuilder, ModelBuildError
        builder = ModelBuilder(session)
        m = builder.build()
    except ImportError as e:
        return {
            "session_id": session_id,
            "status": "error",
            "error": f"WaterTAP/IDAES not available: {e}",
        }
    except Exception as e:
        return {
            "session_id": session_id,
            "status": "error",
            "error": f"Model build failed: {e}",
        }

    # Call IDAES scaling utilities
    try:
        import idaes.core.util.scaling as iscale
        iscale.calculate_scaling_factors(m)
    except ImportError:
        return {
            "session_id": session_id,
            "status": "error",
            "error": "IDAES scaling utilities not available",
        }
    except Exception as e:
        return {
            "session_id": session_id,
            "status": "warning",
            "message": f"Scaling calculation completed with warnings: {e}",
        }

    return {
        "session_id": session_id,
        "status": "Scaling factors calculated",
        "note": "Call report_scaling_issues to identify remaining problems",
    }


@mcp.tool()
def report_scaling_issues(session_id: str) -> Dict[str, Any]:
    """Report unscaled or badly-scaled variables/constraints.

    This calls iscale.report_scaling_issues() to identify problems.

    Args:
        session_id: Session to analyze

    Returns:
        Lists of unscaled and badly-scaled variables/constraints
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    # Build the Pyomo model
    try:
        from utils.model_builder import ModelBuilder, ModelBuildError
        builder = ModelBuilder(session)
        m = builder.build()
    except ImportError as e:
        return {
            "session_id": session_id,
            "error": f"WaterTAP/IDAES not available: {e}",
        }
    except Exception as e:
        return {
            "session_id": session_id,
            "error": f"Model build failed: {e}",
        }

    # Get scaling issues
    unscaled_vars = []
    badly_scaled_vars = []
    unscaled_cons = []
    badly_scaled_cons = []

    try:
        import idaes.core.util.scaling as iscale
        from io import StringIO
        import sys

        # Capture report_scaling_issues output
        old_stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            iscale.report_scaling_issues(m)
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout

        # Parse output for issues (basic parsing)
        lines = output.strip().split('\n')
        current_section = None
        for line in lines:
            line = line.strip()
            if 'Unscaled Variable' in line:
                current_section = 'unscaled_vars'
            elif 'Badly Scaled Variable' in line:
                current_section = 'badly_scaled_vars'
            elif 'Unscaled Constraint' in line:
                current_section = 'unscaled_cons'
            elif 'Badly Scaled Constraint' in line:
                current_section = 'badly_scaled_cons'
            elif line and current_section and not line.startswith('='):
                if current_section == 'unscaled_vars':
                    unscaled_vars.append(line)
                elif current_section == 'badly_scaled_vars':
                    badly_scaled_vars.append(line)
                elif current_section == 'unscaled_cons':
                    unscaled_cons.append(line)
                elif current_section == 'badly_scaled_cons':
                    badly_scaled_cons.append(line)

    except ImportError:
        return {
            "session_id": session_id,
            "error": "IDAES scaling utilities not available",
        }
    except Exception as e:
        return {
            "session_id": session_id,
            "error": f"Scaling analysis failed: {e}",
        }

    return {
        "session_id": session_id,
        "unscaled_variables": unscaled_vars[:20],  # Limit to 20
        "badly_scaled_variables": badly_scaled_vars[:20],
        "unscaled_constraints": unscaled_cons[:20],
        "badly_scaled_constraints": badly_scaled_cons[:20],
        "total_unscaled_vars": len(unscaled_vars),
        "total_badly_scaled_vars": len(badly_scaled_vars),
        "total_unscaled_cons": len(unscaled_cons),
        "total_badly_scaled_cons": len(badly_scaled_cons),
    }


@mcp.tool()
def autoscale_large_jac(session_id: str) -> Dict[str, Any]:
    """Apply Jacobian-based auto-scaling to remaining unscaled constraints.

    Uses IDAES constraint_autoscale_large_jac to identify and fix
    remaining scaling issues based on Jacobian analysis. This is typically
    run after calculate_scaling_factors to handle edge cases.

    Args:
        session_id: Session to scale

    Returns:
        Dict with scaling status and any issues found
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    try:
        from utils.model_builder import ModelBuilder
        import idaes.core.util.scaling as iscale

        builder = ModelBuilder(session)
        model = builder.build()

        # Get count of scaling issues before
        import io
        import sys

        old_stdout = sys.stdout
        sys.stdout = buffer_before = io.StringIO()
        try:
            iscale.report_scaling_issues(model)
        except Exception:
            pass
        sys.stdout = old_stdout
        issues_before = buffer_before.getvalue().count("\n")

        # Apply Jacobian-based autoscaling
        try:
            iscale.constraint_autoscale_large_jac(model)
            autoscale_applied = True
        except Exception as e:
            autoscale_applied = False
            autoscale_error = str(e)

        # Get count of scaling issues after
        sys.stdout = buffer_after = io.StringIO()
        try:
            iscale.report_scaling_issues(model)
        except Exception:
            pass
        sys.stdout = old_stdout
        issues_after = buffer_after.getvalue().count("\n")

        if not autoscale_applied:
            return {
                "session_id": session_id,
                "status": "failed",
                "error": f"Jacobian autoscaling failed: {autoscale_error}",
            }

        return {
            "session_id": session_id,
            "status": "success",
            "issues_before": issues_before,
            "issues_after": issues_after,
            "issues_resolved": max(0, issues_before - issues_after),
            "message": f"Jacobian-based autoscaling applied. Issues: {issues_before} → {issues_after}",
        }

    except ImportError:
        return {
            "session_id": session_id,
            "error": "IDAES scaling utilities not available",
        }
    except Exception as e:
        return {
            "session_id": session_id,
            "error": f"Autoscaling failed: {e}",
        }


# ============================================================================
# SOLVER OPERATIONS TOOLS (6)
# ============================================================================

@mcp.tool()
def initialize_unit(
    session_id: str,
    unit_id: str,
    state_args: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Initialize a single unit.

    Args:
        session_id: Session containing the unit
        unit_id: Unit to initialize
        state_args: Optional state_args for initialization

    Returns:
        Initialization status and DOF after init
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    if unit_id not in session.units:
        return {"error": f"Unit '{unit_id}' not found"}

    # Build the Pyomo model
    try:
        from utils.model_builder import ModelBuilder, ModelBuildError
        builder = ModelBuilder(session)
        m = builder.build()
        units = builder.get_units()
    except ImportError as e:
        return {
            "session_id": session_id,
            "unit_id": unit_id,
            "status": "error",
            "error": f"WaterTAP/IDAES not available: {e}",
        }
    except Exception as e:
        return {
            "session_id": session_id,
            "unit_id": unit_id,
            "status": "error",
            "error": f"Model build failed: {e}",
        }

    # Get the unit block
    unit_block = units.get(unit_id)
    if unit_block is None:
        return {
            "session_id": session_id,
            "unit_id": unit_id,
            "status": "error",
            "error": f"Unit '{unit_id}' not found in built model",
        }

    # Initialize the unit
    try:
        from idaes.core.util.model_statistics import degrees_of_freedom

        if hasattr(unit_block, 'initialize_build'):
            unit_block.initialize_build(state_args=state_args)
        elif hasattr(unit_block, 'initialize'):
            unit_block.initialize(state_args=state_args)
        else:
            return {
                "session_id": session_id,
                "unit_id": unit_id,
                "status": "warning",
                "message": "Unit has no initialize method",
            }

        dof = degrees_of_freedom(unit_block)
    except Exception as e:
        return {
            "session_id": session_id,
            "unit_id": unit_id,
            "status": "error",
            "error": f"Initialization failed: {e}",
        }

    return {
        "session_id": session_id,
        "unit_id": unit_id,
        "status": "initialized",
        "dof_after_init": dof,
    }


@mcp.tool()
def get_initialization_order(
    session_id: str,
    tear_streams: Optional[List[str]] = None,
    use_sequential_decomposition: bool = True,
) -> Dict[str, Any]:
    """Get initialization order for flowsheet units using IDAES SequentialDecomposition.

    Uses WaterTAP standard approach (IDAES SequentialDecomposition) when a model
    can be built. Falls back to session-only topological sort for planning.
    Fails loudly if SequentialDecomposition is unavailable with a built model.

    Args:
        session_id: Session to analyze
        tear_streams: Optional list of arc names to treat as tear streams
        use_sequential_decomposition: If True (default), build model and use IDAES

    Returns:
        Ordered list of unit IDs for initialization
    """
    from utils.topo_sort import (
        compute_initialization_order,
        get_sequential_decomposition_order,
        SequentialDecompositionError,
    )

    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    unit_ids = list(session.units.keys())
    if not unit_ids:
        return {
            "session_id": session_id,
            "initialization_order": [],
            "message": "No units in session",
        }

    # Convert arc names to unit pairs for tear streams
    tear_pairs = None
    if tear_streams:
        tear_pairs = []
        for arc_name in tear_streams:
            # Parse "arc_Source_Dest" format
            if arc_name.startswith("arc_"):
                parts = arc_name[4:].rsplit("_", 1)
                if len(parts) == 2:
                    tear_pairs.append((parts[0], parts[1]))

    # Try to build model and use SequentialDecomposition
    if use_sequential_decomposition:
        try:
            from utils.model_builder import ModelBuilder, ModelBuildError

            builder = ModelBuilder(session)
            model = builder.build()

            # Use IDAES SequentialDecomposition
            order = get_sequential_decomposition_order(model, tear_pairs)

            return {
                "session_id": session_id,
                "initialization_order": order,
                "method": "IDAES_SequentialDecomposition",
                "tear_streams": tear_streams or [],
                "message": "Order determined using IDAES SequentialDecomposition",
            }

        except SequentialDecompositionError as e:
            # Fail loudly - don't silently fall back
            return {
                "session_id": session_id,
                "error": str(e),
                "initialization_order": [],
                "method": "FAILED",
                "message": "IDAES SequentialDecomposition failed. This is the WaterTAP standard approach.",
            }

        except Exception as e:
            # Model build or other error
            return {
                "session_id": session_id,
                "error": f"Model build failed: {e}",
                "initialization_order": [],
                "method": "FAILED",
                "message": "Could not build model for SequentialDecomposition",
            }

    # Session-only planning (no model built)
    # Use simple topological sort for order estimation only
    connections = [
        {
            "src_unit": c.source_unit,
            "src_port": c.source_port,
            "dest_unit": c.dest_unit,
            "dest_port": c.dest_port,
        }
        for c in session.connections
    ]

    try:
        order = compute_initialization_order(
            units={uid: None for uid in unit_ids},
            connections=connections,
            tear_streams=tear_pairs,
            model=None,  # No model - session-only planning
        )
        return {
            "session_id": session_id,
            "initialization_order": order,
            "method": "session_planning",
            "tear_streams": tear_streams or [],
            "message": "Order estimated from session (model not built). Use IDAES SequentialDecomposition for actual initialization.",
        }

    except SequentialDecompositionError as e:
        return {
            "session_id": session_id,
            "error": str(e),
            "initialization_order": [],
            "method": "FAILED",
            "message": str(e),
        }


@mcp.tool()
def check_dof(session_id: str) -> Dict[str, Any]:
    """Check degrees of freedom on REAL built model.

    Builds the Pyomo model and runs IDAES degrees_of_freedom on each unit
    and the overall flowsheet. This gives accurate DOF counts unlike
    get_dof_status which only estimates from registry specs.

    Args:
        session_id: Session to check

    Returns:
        DOF count per unit and overall, with status (ready/under/over specified)
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    # Build the Pyomo model
    try:
        from utils.model_builder import ModelBuilder, ModelBuildError

        builder = ModelBuilder(session)
        m = builder.build()
        units = builder.get_units()
    except ImportError as e:
        return {
            "session_id": session_id,
            "status": "error",
            "error": f"WaterTAP/IDAES not available: {e}",
        }
    except Exception as e:
        return {
            "session_id": session_id,
            "status": "error",
            "error": f"Model build failed: {e}",
        }

    # Get DOF for each unit and overall
    try:
        from idaes.core.util.model_statistics import degrees_of_freedom

        unit_dof = {}
        for unit_id, unit_block in units.items():
            try:
                dof = degrees_of_freedom(unit_block)
                unit_dof[unit_id] = dof
            except Exception as e:
                unit_dof[unit_id] = f"error: {str(e)[:50]}"

        # Overall flowsheet DOF
        fs = getattr(m, 'fs', m)
        total_dof = degrees_of_freedom(fs)

        # Determine status
        if total_dof == 0:
            status = "ready"
            message = "Flowsheet is properly specified (DOF = 0)"
        elif total_dof > 0:
            status = "underspecified"
            message = f"Need to fix {total_dof} more variable(s)"
        else:
            status = "overspecified"
            message = f"Too many fixed variables ({abs(total_dof)} extra)"

        return {
            "session_id": session_id,
            "total_dof": total_dof,
            "status": status,
            "message": message,
            "unit_dof": unit_dof,
        }

    except Exception as e:
        return {
            "session_id": session_id,
            "status": "error",
            "error": f"DOF check failed: {e}",
        }


@mcp.tool()
def initialize_flowsheet(
    session_id: str,
    tear_streams: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Initialize entire flowsheet using IDAES SequentialDecomposition order.

    Uses WaterTAP standard initialization approach:
    1. Build Pyomo model
    2. Get initialization order via IDAES SequentialDecomposition
    3. Initialize each unit using its appropriate method
    4. Fails loudly if SequentialDecomposition is unavailable

    Args:
        session_id: Session to initialize
        tear_streams: Optional list of tear stream names for recycles (format: ["src_unit:dest_unit"])

    Returns:
        Initialization status per unit
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    # Build the Pyomo model
    try:
        from utils.model_builder import ModelBuilder, ModelBuildError
        builder = ModelBuilder(session)
        m = builder.build()
        units = builder.get_units()
    except ImportError as e:
        return {
            "session_id": session_id,
            "status": "error",
            "error": f"WaterTAP/IDAES not available: {e}",
        }
    except Exception as e:
        return {
            "session_id": session_id,
            "status": "error",
            "error": f"Model build failed: {e}",
        }

    # Parse tear streams if provided
    tear_stream_tuples = None
    if tear_streams:
        tear_stream_tuples = []
        for ts in tear_streams:
            if ":" in ts:
                src, dst = ts.split(":", 1)
                tear_stream_tuples.append((src.strip(), dst.strip()))

    # Get initialization order using IDAES SequentialDecomposition (WaterTAP standard)
    init_order = []
    init_method = "IDAES_SequentialDecomposition"
    try:
        from utils.topo_sort import (
            compute_initialization_order,
            SequentialDecompositionError,
        )

        # Build connection list for topo_sort
        connections = [
            {
                "src_unit": conn.source_unit,
                "src_port": conn.source_port,
                "dest_unit": conn.dest_unit,
                "dest_port": conn.dest_port,
            }
            for conn in session.connections
        ]

        # Use SequentialDecomposition with built model
        init_order = compute_initialization_order(
            units={uid: units.get(uid) for uid in session.units.keys()},
            connections=connections,
            tear_streams=tear_stream_tuples,
            model=m,  # Pass the built model for SequentialDecomposition
        )

    except SequentialDecompositionError as e:
        # Fail loudly - do NOT silently fall back to custom implementation
        return {
            "session_id": session_id,
            "status": "error",
            "error": f"IDAES SequentialDecomposition failed: {e}. "
                     "This is the WaterTAP standard approach - cannot proceed.",
            "method": "FAILED",
        }
    except Exception as e:
        # Import or unexpected error - also fail loudly
        return {
            "session_id": session_id,
            "status": "error",
            "error": f"Failed to compute initialization order: {e}",
            "method": "FAILED",
        }

    # Initialize each unit in the computed order
    status_per_unit = {}
    overall_status = "success"
    try:
        from idaes.core.util.model_statistics import degrees_of_freedom

        for unit_id in init_order:
            unit_block = units.get(unit_id)
            if unit_block is None:
                status_per_unit[unit_id] = "not_found"
                continue

            try:
                # Use unit-specific initialization method
                if hasattr(unit_block, 'initialize_build'):
                    # RO, NF specific initialization
                    unit_block.initialize_build()
                elif hasattr(unit_block, 'initialize'):
                    # Standard IDAES initialization
                    unit_block.initialize()

                dof = degrees_of_freedom(unit_block)
                status_per_unit[unit_id] = f"initialized (DOF={dof})"
            except Exception as e:
                status_per_unit[unit_id] = f"failed: {str(e)[:50]}"
                overall_status = "partial"

    except Exception as e:
        return {
            "session_id": session_id,
            "status": "error",
            "error": f"Initialization failed: {e}",
        }

    return {
        "session_id": session_id,
        "initialization_order": init_order,
        "status_per_unit": status_per_unit,
        "overall_status": overall_status,
        "method": init_method,
        "tear_streams": tear_streams or [],
    }


@mcp.tool()
def propagate_state(
    session_id: str,
    source_port: str,
    dest_port: str,
) -> Dict[str, Any]:
    """Propagate state from one port to another.

    Args:
        session_id: Session containing the ports
        source_port: Source port (format: "unit_id.port_name")
        dest_port: Destination port (format: "unit_id.port_name")

    Returns:
        Propagation status
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    # Parse port specifications
    try:
        src_unit_id, src_port_name = source_port.split('.', 1)
        dst_unit_id, dst_port_name = dest_port.split('.', 1)
    except ValueError:
        return {
            "session_id": session_id,
            "error": "Port format must be 'unit_id.port_name'",
        }

    # Build the Pyomo model
    try:
        from utils.model_builder import ModelBuilder, ModelBuildError
        builder = ModelBuilder(session)
        m = builder.build()
        units = builder.get_units()
    except ImportError as e:
        return {
            "session_id": session_id,
            "status": "error",
            "error": f"WaterTAP/IDAES not available: {e}",
        }
    except Exception as e:
        return {
            "session_id": session_id,
            "status": "error",
            "error": f"Model build failed: {e}",
        }

    # Get units and ports
    src_unit = units.get(src_unit_id)
    dst_unit = units.get(dst_unit_id)

    if src_unit is None:
        return {"error": f"Source unit '{src_unit_id}' not found"}
    if dst_unit is None:
        return {"error": f"Destination unit '{dst_unit_id}' not found"}

    src_port_obj = getattr(src_unit, src_port_name, None)
    dst_port_obj = getattr(dst_unit, dst_port_name, None)

    if src_port_obj is None:
        return {"error": f"Source port '{src_port_name}' not found on '{src_unit_id}'"}
    if dst_port_obj is None:
        return {"error": f"Destination port '{dst_port_name}' not found on '{dst_unit_id}'"}

    # Propagate state
    try:
        from idaes.core.util.initialization import propagate_state as idaes_propagate
        idaes_propagate(arc=None, direction="forward",
                        source=src_port_obj, destination=dst_port_obj)
    except ImportError:
        # Try alternative import
        try:
            from watertap.core.util.initialization import propagate_state as wt_propagate
            wt_propagate(arc=None, direction="forward",
                        source=src_port_obj, destination=dst_port_obj)
        except ImportError:
            return {
                "session_id": session_id,
                "status": "error",
                "error": "propagate_state utility not available",
            }
    except Exception as e:
        return {
            "session_id": session_id,
            "status": "error",
            "error": f"State propagation failed: {e}",
        }

    return {
        "session_id": session_id,
        "propagated": f"{source_port} → {dest_port}",
        "status": "success",
    }


@mcp.tool()
def check_solve(session_id: str, unit_id: Optional[str] = None) -> Dict[str, Any]:
    """Check if model/unit is ready to solve.

    Args:
        session_id: Session to check
        unit_id: Optional specific unit to check

    Returns:
        Solve readiness status with any issues
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    issues = []

    # Check DOF
    if session.total_dof != 0:
        issues.append(f"Total DOF = {session.total_dof} (should be 0)")

    # Check feed state
    if not session.feed_state:
        issues.append("No feed state defined")

    # Check connections
    if not session.connections and len(session.units) > 1:
        issues.append("Multiple units but no connections")

    return {
        "session_id": session_id,
        "ready_to_solve": len(issues) == 0,
        "issues": issues,
    }


@mcp.tool()
def solve(
    session_id: str,
    solver: str = "ipopt",
    solver_options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Solve the flowsheet with automatic preprocessing.

    NOTE: This is NOT a pure solve operation. The solver workflow includes:
    1. DOF check (warns if DOF != 0 but continues)
    2. Calculate scaling factors (IDAES iscale.calculate_scaling_factors)
    3. Sequential initialization (uses IDAES SequentialDecomposition order)
    4. IPOPT solve

    For explicit step-by-step control, use the individual tools:
    - check_dof, fix_variable, unfix_variable
    - calculate_scaling_factors, set_scaling_factor
    - initialize_flowsheet, initialize_unit
    - Then call solve

    For the full hygiene pipeline with pre/post diagnostics, use build_and_solve.

    Results are persisted to the session state after completion.
    Solved KPIs (stream values, unit metrics) are extracted and stored
    for retrieval via get_stream_results and get_unit_results.

    Args:
        session_id: Session to solve
        solver: Solver name (default: ipopt)
        solver_options: Solver-specific options

    Returns:
        job_id for polling with get_solve_status
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    # Always use background job to avoid blocking MCP connection
    job = job_manager.submit(
        session_id=session_id,
        job_type="solve",
        params={"solver": solver, "solver_options": solver_options or {}},
    )
    return {
        "session_id": session_id,
        "job_id": job.job_id,
        "status": job.status.value,
        "message": "Solve job submitted. Poll with get_solve_status.",
    }


@mcp.tool()
def get_solve_status(job_id: str) -> Dict[str, Any]:
    """Get status of a solve job.

    Args:
        job_id: Job ID from solve()

    Returns:
        Job status, progress, and results if complete
    """
    job = job_manager.get_status(job_id)
    if not job:
        return {"error": f"Job '{job_id}' not found"}

    return {
        "job_id": job_id,
        "status": job.status.value,
        "progress": job.progress,
        "message": job.message,
        "result": job.result if job.status == JobStatus.COMPLETED else None,
        "error": job.error if job.status == JobStatus.FAILED else None,
    }


# ============================================================================
# DIAGNOSTICS TOOLS (4)
# ============================================================================

@mcp.tool()
def run_diagnostics(session_id: str) -> Dict[str, Any]:
    """Run comprehensive diagnostics on the flowsheet.

    Uses IDAES DiagnosticsToolbox to identify structural and
    numerical issues.

    Args:
        session_id: Session to diagnose

    Returns:
        Diagnostic results including structural and numerical issues
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    diagnostics = {
        "session_id": session_id,
        "structural_issues": [],
        "numerical_issues": [],
        "session_checks": {},
    }

    # Session-level checks
    unit_dof = {}
    for unit_id, unit_inst in session.units.items():
        fixed_count = len(unit_inst.fixed_vars)
        unit_dof[unit_id] = {"fixed_vars": fixed_count}

    diagnostics["session_checks"]["total_fixed_vars"] = sum(
        len(u.fixed_vars) for u in session.units.values()
    )
    diagnostics["session_checks"]["units"] = unit_dof
    diagnostics["session_checks"]["connections"] = len(session.connections)

    # Build the Pyomo model for runtime diagnostics
    try:
        from utils.model_builder import ModelBuilder, ModelBuildError
        builder = ModelBuilder(session)
        m = builder.build()
    except ImportError as e:
        diagnostics["note"] = f"WaterTAP/IDAES not available: {e}"
        return diagnostics
    except Exception as e:
        diagnostics["note"] = f"Model build failed: {e}"
        return diagnostics

    # Run DiagnosticsToolbox
    try:
        from idaes.core.util.model_diagnostics import DiagnosticsToolbox
        from idaes.core.util.model_statistics import degrees_of_freedom
        from io import StringIO
        import sys

        dt = DiagnosticsToolbox(m)

        # Capture structural issues
        old_stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            dt.report_structural_issues()
            structural_output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout

        # Capture numerical issues
        sys.stdout = StringIO()
        try:
            dt.report_numerical_issues()
            numerical_output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout

        # Parse outputs for issues
        structural_lines = [l.strip() for l in structural_output.split('\n')
                          if l.strip() and not l.startswith('=') and not l.startswith('-')]
        numerical_lines = [l.strip() for l in numerical_output.split('\n')
                         if l.strip() and not l.startswith('=') and not l.startswith('-')]

        diagnostics["structural_issues"] = structural_lines[:20]
        diagnostics["numerical_issues"] = numerical_lines[:20]

        # Add DOF info
        dof = degrees_of_freedom(m)
        diagnostics["degrees_of_freedom"] = dof
        if dof != 0:
            diagnostics["structural_issues"].insert(0, f"Degrees of freedom: {dof} (should be 0)")

    except ImportError:
        diagnostics["note"] = "IDAES DiagnosticsToolbox not available"
    except Exception as e:
        diagnostics["note"] = f"Diagnostics failed: {str(e)[:100]}"

    return diagnostics


@mcp.tool()
def get_constraint_residuals(
    session_id: str,
    threshold: float = 1e-6,
    max_results: int = 20,
) -> Dict[str, Any]:
    """Get constraints with largest residuals.

    Args:
        session_id: Session to analyze
        threshold: Only report residuals above this value
        max_results: Maximum number of results to return

    Returns:
        List of constraints with large residuals
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    # Build the Pyomo model
    try:
        from utils.model_builder import ModelBuilder, ModelBuildError
        builder = ModelBuilder(session)
        m = builder.build()
    except ImportError as e:
        return {
            "session_id": session_id,
            "error": f"WaterTAP/IDAES not available: {e}",
        }
    except Exception as e:
        return {
            "session_id": session_id,
            "error": f"Model build failed: {e}",
        }

    # Get constraint residuals
    residuals = []
    try:
        from pyomo.environ import Constraint, value

        for c in m.component_data_objects(Constraint, active=True):
            try:
                body_val = value(c.body, exception=False)
                if body_val is None:
                    continue

                # Calculate residual based on constraint type
                lb = value(c.lower, exception=False)
                ub = value(c.upper, exception=False)

                residual = 0.0
                if lb is not None and body_val < lb:
                    residual = abs(lb - body_val)
                elif ub is not None and body_val > ub:
                    residual = abs(body_val - ub)

                if residual > threshold:
                    residuals.append({
                        "constraint": str(c),
                        "residual": residual,
                        "body_value": body_val,
                    })
            except Exception:
                continue

        # Sort by residual descending
        residuals.sort(key=lambda x: x["residual"], reverse=True)
        residuals = residuals[:max_results]

    except Exception as e:
        return {
            "session_id": session_id,
            "error": f"Residual calculation failed: {e}",
        }

    return {
        "session_id": session_id,
        "threshold": threshold,
        "constraint_residuals": residuals,
        "count": len(residuals),
    }


@mcp.tool()
def get_bound_violations(
    session_id: str,
    tolerance: float = 1e-8,
    max_results: int = 20,
) -> Dict[str, Any]:
    """Get variables violating their bounds.

    Args:
        session_id: Session to analyze
        tolerance: Tolerance for bound violations
        max_results: Maximum number of results

    Returns:
        List of bound violations
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    # Build the Pyomo model
    try:
        from utils.model_builder import ModelBuilder, ModelBuildError
        builder = ModelBuilder(session)
        m = builder.build()
    except ImportError as e:
        return {
            "session_id": session_id,
            "error": f"WaterTAP/IDAES not available: {e}",
        }
    except Exception as e:
        return {
            "session_id": session_id,
            "error": f"Model build failed: {e}",
        }

    # Get bound violations
    violations = []
    try:
        from pyomo.environ import Var, value

        for v in m.component_data_objects(Var, active=True):
            try:
                val = value(v, exception=False)
                if val is None:
                    continue

                lb = value(v.lb, exception=False) if v.lb is not None else None
                ub = value(v.ub, exception=False) if v.ub is not None else None

                violation = None
                violation_type = None

                if lb is not None and val < lb - tolerance:
                    violation = lb - val
                    violation_type = "below_lower"
                elif ub is not None and val > ub + tolerance:
                    violation = val - ub
                    violation_type = "above_upper"

                if violation is not None:
                    violations.append({
                        "variable": str(v),
                        "value": val,
                        "lower_bound": lb,
                        "upper_bound": ub,
                        "violation": violation,
                        "type": violation_type,
                    })
            except Exception:
                continue

        # Sort by violation magnitude descending
        violations.sort(key=lambda x: abs(x["violation"]), reverse=True)
        violations = violations[:max_results]

    except Exception as e:
        return {
            "session_id": session_id,
            "error": f"Bound violation check failed: {e}",
        }

    return {
        "session_id": session_id,
        "tolerance": tolerance,
        "bound_violations": violations,
        "count": len(violations),
    }


@mcp.tool()
def diagnose_failure(
    session_id: str,
    termination_condition: str,
) -> Dict[str, Any]:
    """Diagnose a solver failure and suggest fixes.

    Args:
        session_id: Session that failed to solve
        termination_condition: The solver's termination condition

    Returns:
        Diagnosis with likely causes and suggested fixes
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    diagnosis = {
        "session_id": session_id,
        "termination_condition": termination_condition,
        "likely_causes": [],
        "suggested_fixes": [],
        "constraint_residuals": [],
        "bound_violations": [],
    }

    # Pattern matching for common failures
    if termination_condition == "infeasible":
        diagnosis["likely_causes"].append(
            "No feasible solution exists with current constraints and bounds"
        )
        diagnosis["suggested_fixes"].extend([
            "Check constraint residuals to identify problematic constraints",
            "Verify feed pressure is above osmotic pressure for RO",
            "Check that operating conditions are physically achievable",
        ])

    elif termination_condition == "maxIterations":
        diagnosis["likely_causes"].append(
            "Solver hit iteration limit - likely poor scaling or bad initialization"
        )
        diagnosis["suggested_fixes"].extend([
            "Run calculate_scaling_factors and report_scaling_issues",
            "Scale small parameters: A_comp (1e12), B_comp (1e8)",
            "Initialize units sequentially with propagate_state",
        ])

    elif termination_condition == "locallyInfeasible":
        diagnosis["likely_causes"].append(
            "Local minimum is infeasible - bad initial point"
        )
        diagnosis["suggested_fixes"].extend([
            "Try different initial values",
            "Initialize from a known feasible solution",
        ])

    elif termination_condition == "unbounded":
        diagnosis["likely_causes"].append(
            "Variables going to infinity - missing constraints"
        )
        diagnosis["suggested_fixes"].extend([
            "Check that all required variables are fixed",
            "Verify DOF = 0 before solving",
        ])

    return diagnosis


# ============================================================================
# ZERO-ORDER SPECIFIC TOOLS (3)
# ============================================================================

@mcp.tool()
def load_zo_parameters(
    session_id: str,
    unit_id: str,
    database: str = "default",
    process_subtype: Optional[str] = None,
) -> Dict[str, Any]:
    """Load parameters from WaterTAP database for zero-order unit.

    Args:
        session_id: Session containing the unit
        unit_id: Zero-order unit to configure
        database: Database name or path
        process_subtype: Process subtype for parameter lookup

    Returns:
        Loaded parameters
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    if unit_id not in session.units:
        return {"error": f"Unit '{unit_id}' not found"}

    unit_inst = session.units[unit_id]

    # Check if this is a zero-order unit
    if not unit_inst.unit_type.endswith("ZO"):
        return {
            "error": f"Unit '{unit_id}' is not a zero-order unit (type: {unit_inst.unit_type})"
        }

    # Build model and call load_parameters_from_database
    try:
        from utils.model_builder import ModelBuilder, ModelBuildError

        builder = ModelBuilder(session)
        m = builder.build()
        units = builder.get_units()
    except ImportError as e:
        return {
            "session_id": session_id,
            "unit_id": unit_id,
            "error": f"WaterTAP/IDAES not available: {e}",
        }
    except Exception as e:
        return {
            "session_id": session_id,
            "unit_id": unit_id,
            "error": f"Model build failed: {e}",
        }

    unit_block = units.get(unit_id)
    if unit_block is None:
        return {
            "session_id": session_id,
            "unit_id": unit_id,
            "error": f"Unit '{unit_id}' not found in built model",
        }

    # Try to load parameters from database
    parameters_loaded = {}
    try:
        if hasattr(unit_block, "load_parameters_from_database"):
            # Set database on unit config (CORRECT approach per Codex review)
            # Note: Database class is in watertap.core.wt_database, not zero_order_base
            try:
                from watertap.core.wt_database import Database

                if database != "default":
                    db = Database(database)
                    unit_block.config.database = db
            except ImportError:
                return {
                    "session_id": session_id,
                    "unit_id": unit_id,
                    "error": "WaterTAP database not available",
                }

            # Set process_subtype on config if provided
            if process_subtype:
                unit_block.config.process_subtype = process_subtype

            # Load parameters (method reads from config)
            unit_block.load_parameters_from_database(use_default_removal=True)

            # Extract loaded parameter values
            from pyomo.environ import value

            param_names = [
                "removal_frac_mass_comp",
                "recovery_frac_mass_H2O",
                "energy_electric_flow_vol_inlet",
                "electricity",
            ]
            for pname in param_names:
                param = getattr(unit_block, pname, None)
                if param is not None:
                    try:
                        if hasattr(param, "__iter__"):
                            for idx in param:
                                val = value(param[idx], exception=False)
                                if val is not None:
                                    parameters_loaded[f"{pname}[{idx}]"] = val
                        else:
                            val = value(param, exception=False)
                            if val is not None:
                                parameters_loaded[pname] = val
                    except Exception:
                        pass

        else:
            return {
                "session_id": session_id,
                "unit_id": unit_id,
                "error": "Unit does not support load_parameters_from_database",
            }

    except Exception as e:
        return {
            "session_id": session_id,
            "unit_id": unit_id,
            "error": f"Failed to load parameters: {e}",
        }

    return {
        "session_id": session_id,
        "unit_id": unit_id,
        "database": database,
        "process_subtype": process_subtype,
        "parameters_loaded": parameters_loaded,
        "success": True,
    }


@mcp.tool()
def list_zo_databases() -> List[Dict[str, Any]]:
    """List available zero-order parameter databases.

    Returns:
        List of available databases with water source types
    """
    # Try to get actual database info from WaterTAP
    databases = []

    try:
        from watertap.core.zero_order_base import Database as ZODatabase
        import os
        from pathlib import Path

        # Get default database path
        try:
            import watertap

            watertap_path = Path(watertap.__file__).parent
            data_path = watertap_path / "data" / "techno_economic"

            if data_path.exists():
                # List available yaml files as databases
                for f in data_path.glob("*.yaml"):
                    databases.append({
                        "name": f.stem,
                        "path": str(f),
                        "description": f"WaterTAP database: {f.stem}",
                    })
        except Exception:
            pass

        # Add default database
        databases.insert(0, {
            "name": "default",
            "description": "Default WaterTAP zero-order database",
            "water_sources": ["municipal", "industrial", "seawater", "surface_water", "groundwater"],
        })

    except ImportError:
        # WaterTAP not available, return static info
        databases = [
            {
                "name": "default",
                "description": "Default WaterTAP zero-order database (WaterTAP not installed)",
                "water_sources": ["municipal", "industrial", "seawater"],
            },
        ]

    return databases


@mcp.tool()
def get_zo_unit_parameters(
    unit_type: str,
    water_source: str = "municipal",
) -> Dict[str, Any]:
    """Get database parameters for a zero-order unit type.

    Args:
        unit_type: Zero-order unit type (e.g., "NanofiltrationZO")
        water_source: Water source type

    Returns:
        Available parameters from database
    """
    # Try to get actual parameters from WaterTAP database
    try:
        from watertap.core.zero_order_base import Database as ZODatabase

        db = ZODatabase()

        # Map unit type to database key
        unit_key = unit_type.replace("ZO", "").lower()

        # Try to get parameters from database
        params = {}
        try:
            # The database structure varies by unit type
            # Common parameters to look for
            unit_data = db.get(unit_key, {})
            if isinstance(unit_data, dict):
                # Extract water source specific data
                source_data = unit_data.get(water_source, unit_data.get("default", {}))
                if isinstance(source_data, dict):
                    params = source_data
                else:
                    params = {"raw_data": str(source_data)[:200]}
        except Exception as e:
            params = {"lookup_error": str(e)[:100]}

        return {
            "unit_type": unit_type,
            "water_source": water_source,
            "parameters": params,
            "source": "watertap_database",
        }

    except ImportError:
        # WaterTAP not available, return typical parameters
        typical_params = {
            "NanofiltrationZO": {
                "removal_frac_mass_comp": {"default": 0.9},
                "recovery_frac_mass_H2O": {"default": 0.8},
                "energy_electric_flow_vol_inlet": {"default": 0.05},
            },
            "UltrafiltrationZO": {
                "removal_frac_mass_comp": {"default": 0.99},
                "recovery_frac_mass_H2O": {"default": 0.95},
                "energy_electric_flow_vol_inlet": {"default": 0.03},
            },
            "PumpZO": {
                "lift_height": {"default": 100},
                "eta_pump": {"default": 0.8},
                "eta_motor": {"default": 0.9},
            },
        }

        params = typical_params.get(unit_type, {
            "note": "Unit type not in static database; install WaterTAP for full database access"
        })

        return {
            "unit_type": unit_type,
            "water_source": water_source,
            "parameters": params,
            "source": "static_defaults",
            "note": "WaterTAP not installed; showing typical values",
        }


# ============================================================================
# RESULTS TOOLS (3)
# ============================================================================

@mcp.tool()
def get_results(session_id: str) -> Dict[str, Any]:
    """Get solve results for a session.

    Args:
        session_id: Session to get results for

    Returns:
        Solve status and summary results
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    return {
        "session_id": session_id,
        "status": session.status.value,
        "solve_status": session.solve_status,
        "solve_message": session.solve_message,
        "results": session.results,
    }


@mcp.tool()
def get_stream_results(
    session_id: str,
    streams: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Get stream property tables.

    After a successful solve, returns persisted KPIs (solved values).
    Falls back to rebuilding the model if KPIs not available.

    Args:
        session_id: Session to get results for
        streams: Optional list of specific streams (unit.port format)

    Returns:
        Stream properties (flow, concentration, T, P) for each stream
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    # Check if we have persisted KPIs from a solved session
    if session.results and isinstance(session.results, dict) and "kpis" in session.results:
        kpis = session.results["kpis"]
        stream_kpis = kpis.get("streams", {})

        if stream_kpis:
            # Convert from unit-based to stream-based format if specific streams requested
            if streams is not None:
                filtered_streams = {}
                for unit_id, ports in stream_kpis.items():
                    for port_name, port_data in ports.items():
                        stream_key = f"{unit_id}.{port_name}"
                        if stream_key in streams:
                            filtered_streams[stream_key] = port_data
                return {
                    "session_id": session_id,
                    "source": "solved",
                    "streams": filtered_streams,
                }

            # Return all streams
            all_streams = {}
            for unit_id, ports in stream_kpis.items():
                for port_name, port_data in ports.items():
                    stream_key = f"{unit_id}.{port_name}"
                    all_streams[stream_key] = port_data

            return {
                "session_id": session_id,
                "source": "solved",
                "streams": all_streams,
            }

    # Fall back to rebuilding model (returns uninitialized/unsolved values)
    # This path is used when session hasn't been solved yet
    try:
        from utils.model_builder import ModelBuilder, ModelBuildError
        builder = ModelBuilder(session)
        m = builder.build()
        units = builder.get_units()
    except ImportError as e:
        return {
            "session_id": session_id,
            "error": f"WaterTAP/IDAES not available: {e}",
        }
    except Exception as e:
        return {
            "session_id": session_id,
            "error": f"Model build failed: {e}",
        }

    # Extract stream properties
    stream_data = {}
    try:
        from pyomo.environ import value

        for unit_id, unit_block in units.items():
            # Check common port names
            port_names = ["inlet", "outlet", "feed", "permeate", "retentate",
                         "brine", "product", "reject", "vapor", "liquid"]
            for port_name in port_names:
                port = getattr(unit_block, port_name, None)
                if port is None:
                    continue

                stream_key = f"{unit_id}.{port_name}"

                # Check if specific streams were requested
                if streams is not None and stream_key not in streams:
                    continue

                port_data = {}
                try:
                    # Try to extract state block properties
                    if hasattr(port, 'flow_mass_phase_comp'):
                        flow_vals = {}
                        for idx in port.flow_mass_phase_comp:
                            try:
                                flow_vals[str(idx)] = value(port.flow_mass_phase_comp[idx])
                            except Exception:
                                pass
                        if flow_vals:
                            port_data["flow_mass_phase_comp"] = flow_vals

                    if hasattr(port, 'flow_vol'):
                        for idx in port.flow_vol:
                            try:
                                port_data["flow_vol"] = value(port.flow_vol[idx])
                            except Exception:
                                pass

                    if hasattr(port, 'temperature'):
                        for idx in port.temperature:
                            try:
                                port_data["temperature_K"] = value(port.temperature[idx])
                            except Exception:
                                pass

                    if hasattr(port, 'pressure'):
                        for idx in port.pressure:
                            try:
                                port_data["pressure_Pa"] = value(port.pressure[idx])
                            except Exception:
                                pass

                    if hasattr(port, 'conc_mass_phase_comp'):
                        conc_vals = {}
                        for idx in port.conc_mass_phase_comp:
                            try:
                                conc_vals[str(idx)] = value(port.conc_mass_phase_comp[idx])
                            except Exception:
                                pass
                        if conc_vals:
                            port_data["conc_mass_phase_comp"] = conc_vals

                except Exception as e:
                    port_data["extraction_error"] = str(e)[:50]

                if port_data:
                    stream_data[stream_key] = port_data

    except Exception as e:
        return {
            "session_id": session_id,
            "error": f"Stream extraction failed: {e}",
        }

    return {
        "session_id": session_id,
        "source": "unsolved_model",
        "warning": "Values are from an unsolved model. Run solve() first.",
        "streams": stream_data,
        "count": len(stream_data),
    }


@mcp.tool()
def get_unit_results(
    session_id: str,
    unit_id: str,
) -> Dict[str, Any]:
    """Get performance results for a specific unit.

    After a successful solve, returns persisted KPIs (solved values).
    Falls back to rebuilding the model if KPIs not available.

    Args:
        session_id: Session containing the unit
        unit_id: Unit to get results for

    Returns:
        Unit-specific performance metrics
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    if unit_id not in session.units:
        return {"error": f"Unit '{unit_id}' not found"}

    unit_inst = session.units[unit_id]

    # Check if we have persisted KPIs from a solved session
    if session.results and isinstance(session.results, dict) and "kpis" in session.results:
        kpis = session.results["kpis"]

        # Get unit-level KPIs
        unit_kpis = kpis.get("units", {}).get(unit_id, {})

        # Get stream KPIs for this unit
        stream_kpis = kpis.get("streams", {}).get(unit_id, {})

        if unit_kpis or stream_kpis:
            return {
                "session_id": session_id,
                "unit_id": unit_id,
                "unit_type": unit_inst.unit_type,
                "source": "solved",
                "performance": unit_kpis,
                "streams": stream_kpis,
            }

    # Fall back to rebuilding model (returns uninitialized/unsolved values)
    try:
        from utils.model_builder import ModelBuilder, ModelBuildError

        builder = ModelBuilder(session)
        m = builder.build()
        units = builder.get_units()
    except ImportError as e:
        return {
            "session_id": session_id,
            "unit_id": unit_id,
            "unit_type": unit_inst.unit_type,
            "error": f"WaterTAP/IDAES not available: {e}",
        }
    except Exception as e:
        return {
            "session_id": session_id,
            "unit_id": unit_id,
            "unit_type": unit_inst.unit_type,
            "error": f"Model build failed: {e}",
        }

    unit_block = units.get(unit_id)
    if unit_block is None:
        return {
            "session_id": session_id,
            "unit_id": unit_id,
            "error": f"Unit '{unit_id}' not found in built model",
        }

    # Extract unit-specific performance metrics
    performance = {}

    try:
        from pyomo.environ import value

        # Common variables to extract for all units
        common_vars = [
            "work_mechanical",
            "work_fluid",
            "work_isentropic",
            "heat_duty",
            "heat",
            "electricity",
            "power",
        ]

        for var_name in common_vars:
            var = getattr(unit_block, var_name, None)
            if var is not None:
                try:
                    if hasattr(var, "__iter__"):
                        # Indexed variable
                        for idx in var:
                            val = value(var[idx], exception=False)
                            if val is not None:
                                performance[f"{var_name}[{idx}]"] = val
                    else:
                        val = value(var, exception=False)
                        if val is not None:
                            performance[var_name] = val
                except Exception:
                    pass

        # RO/NF specific metrics
        ro_vars = [
            "recovery_vol_phase",
            "recovery_mass_phase_comp",
            "rejection_phase_comp",
            "flux_mass_phase_comp",
            "area",
            "A_comp",
            "B_comp",
            "deltaP",
            "over_pressure_ratio",
        ]
        for var_name in ro_vars:
            var = getattr(unit_block, var_name, None)
            if var is not None:
                try:
                    if hasattr(var, "__iter__"):
                        for idx in var:
                            val = value(var[idx], exception=False)
                            if val is not None:
                                performance[f"{var_name}[{idx}]"] = val
                    else:
                        val = value(var, exception=False)
                        if val is not None:
                            performance[var_name] = val
                except Exception:
                    pass

        # Pump specific metrics
        pump_vars = [
            "efficiency_pump",
            "efficiency_isentropic",
            "control_volume",
            "deltaP",
        ]
        for var_name in pump_vars:
            var = getattr(unit_block, var_name, None)
            if var is not None:
                try:
                    if hasattr(var, "__iter__"):
                        for idx in var:
                            val = value(var[idx], exception=False)
                            if val is not None:
                                performance[f"{var_name}[{idx}]"] = val
                    else:
                        val = value(var, exception=False)
                        if val is not None:
                            performance[var_name] = val
                except Exception:
                    pass

        # Evaporator/crystallizer specific
        evap_vars = [
            "area",
            "U",
            "delta_temperature",
            "lmtd",
            "heat_transfer",
        ]
        for var_name in evap_vars:
            var = getattr(unit_block, var_name, None)
            if var is not None:
                try:
                    val = value(var, exception=False)
                    if val is not None:
                        performance[var_name] = val
                except Exception:
                    pass

    except Exception as e:
        performance["extraction_error"] = str(e)[:100]

    return {
        "session_id": session_id,
        "unit_id": unit_id,
        "unit_type": unit_inst.unit_type,
        "source": "unsolved_model",
        "warning": "Values are from an unsolved model. Run solve() first.",
        "performance": performance,
    }


@mcp.tool()
def get_costing(session_id: str) -> Dict[str, Any]:
    """Get costing results including LCOW.

    Args:
        session_id: Session to get costing for

    Returns:
        Costing breakdown (LCOW, CapEx, OpEx) if costing configured
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    # Build the Pyomo model to extract costing data
    try:
        from utils.model_builder import ModelBuilder, ModelBuildError

        builder = ModelBuilder(session)
        m = builder.build()
    except ImportError as e:
        return {
            "session_id": session_id,
            "costing_configured": False,
            "error": f"WaterTAP/IDAES not available: {e}",
        }
    except Exception as e:
        return {
            "session_id": session_id,
            "costing_configured": False,
            "error": f"Model build failed: {e}",
        }

    # Check if costing block exists and extract data
    costing_data = {
        "session_id": session_id,
        "costing_configured": False,
    }

    try:
        from pyomo.environ import value

        # Check for WaterTAP costing block on flowsheet
        fs = m.fs
        costing_block = getattr(fs, "costing", None)

        if costing_block is not None:
            costing_data["costing_configured"] = True

            # Extract LCOW if available
            lcow = getattr(costing_block, "LCOW", None)
            if lcow is not None:
                try:
                    costing_data["LCOW"] = value(lcow, exception=False)
                except Exception:
                    pass

            # Extract capital cost
            capital = getattr(costing_block, "total_capital_cost", None)
            if capital is not None:
                try:
                    costing_data["total_capital_cost"] = value(capital, exception=False)
                except Exception:
                    pass

            # Extract operating cost
            operating = getattr(costing_block, "total_operating_cost", None)
            if operating is not None:
                try:
                    costing_data["total_operating_cost"] = value(operating, exception=False)
                except Exception:
                    pass

            # Extract specific energy consumption
            sec = getattr(costing_block, "specific_energy_consumption", None)
            if sec is not None:
                try:
                    costing_data["specific_energy_consumption"] = value(sec, exception=False)
                except Exception:
                    pass

            # Extract electricity cost
            elec_cost = getattr(costing_block, "aggregate_flow_electricity", None)
            if elec_cost is not None:
                try:
                    costing_data["aggregate_electricity"] = value(elec_cost, exception=False)
                except Exception:
                    pass

            # Check for unit-level costing
            unit_costs = {}
            for unit_id in session.units:
                unit_block = getattr(fs, unit_id, None)
                if unit_block is None:
                    continue
                unit_costing = getattr(unit_block, "costing", None)
                if unit_costing is not None:
                    unit_cost_data = {}
                    # Capital cost
                    cap = getattr(unit_costing, "capital_cost", None)
                    if cap is not None:
                        try:
                            unit_cost_data["capital_cost"] = value(cap, exception=False)
                        except Exception:
                            pass
                    # Fixed operating cost
                    fixed_op = getattr(unit_costing, "fixed_operating_cost", None)
                    if fixed_op is not None:
                        try:
                            unit_cost_data["fixed_operating_cost"] = value(fixed_op, exception=False)
                        except Exception:
                            pass
                    if unit_cost_data:
                        unit_costs[unit_id] = unit_cost_data

            if unit_costs:
                costing_data["unit_costs"] = unit_costs

        else:
            costing_data["note"] = (
                "Costing block not found. To enable costing:\n"
                "1. Create costing block: m.fs.costing = WaterTAPCostingBlockData()\n"
                "2. Add unit costing: unit.costing = UnitModelCostingBlock()\n"
                "3. Calculate LCOW: costing.add_LCOW(flow_rate)\n"
                "4. Solve with costing equations"
            )

    except Exception as e:
        costing_data["extraction_error"] = str(e)[:100]

    return costing_data


# ============================================================================
# ADDITIONAL TOOLS (Plan Compliance)
# ============================================================================

@mcp.tool()
def create_watertap_session(
    name: str = "",
    description: str = "",
    property_package: str = "SEAWATER",
) -> Dict[str, Any]:
    """Create a new WaterTAP flowsheet session (alias for create_session).

    Args:
        name: Optional session name
        description: Optional session description
        property_package: Default property package

    Returns:
        Dict with session_id and configuration details
    """
    return create_session(name, description, property_package)


@mcp.tool()
def get_unit_requirements(unit_type: str) -> Dict[str, Any]:
    """Get DOF requirements, scaling hints, and init requirements for a unit type.

    Alias for get_unit_spec with additional formatting.

    Args:
        unit_type: Type of unit (e.g., "ReverseOsmosis0D", "Pump")

    Returns:
        Unit requirements including DOF, scaling, and initialization hints
    """
    spec = get_unit_spec(unit_type)
    if spec is None:
        return {"error": f"Unknown unit type: {unit_type}"}

    return {
        "unit_type": unit_type,
        "category": spec.category.name if spec.category else None,
        "dof_requirements": {
            "required_fixes": spec.required_fixes,
            "typical_values": spec.typical_values,
        },
        "scaling": {
            "default_factors": spec.default_scaling,
        },
        "initialization": {
            "init_hints": spec.init_hints,
        },
        "ports": {
            "inlets": spec.n_inlets,
            "outlets": spec.n_outlets,
            "inlet_names": spec.inlet_names,
            "outlet_names": spec.outlet_names,
        },
        "is_idaes_unit": spec.is_idaes_unit,
        "module_path": spec.module_path,
    }


@mcp.tool()
def connect_units(
    session_id: str,
    source_unit: str,
    source_port: str,
    dest_unit: str,
    dest_port: str,
    auto_create_translator: bool = False,
) -> Dict[str, Any]:
    """Connect two units with automatic translator detection.

    Checks property package compatibility and suggests translator if needed.
    For ASM↔ADM connections, can auto-create the required translator.

    Args:
        session_id: Session containing the units
        source_unit: Source unit ID
        source_port: Source port name
        dest_unit: Destination unit ID
        dest_port: Destination port name
        auto_create_translator: If True, auto-create ASM/ADM translator if needed

    Returns:
        Connection result with translator info if applicable
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    # Check if we can detect property packages for these units
    from utils.auto_translator import check_connection_compatibility

    # Get unit property packages from config if available
    src_unit_inst = session.units.get(source_unit)
    dst_unit_inst = session.units.get(dest_unit)

    source_pkg = None
    dest_pkg = None
    translator_id = None
    translator_note = None

    if src_unit_inst and dst_unit_inst:
        # Check if units have property package configured
        src_pkg_name = src_unit_inst.config.get("property_package")
        dst_pkg_name = dst_unit_inst.config.get("property_package")

        if src_pkg_name and dst_pkg_name:
            try:
                from core.property_registry import PropertyPackageType
                source_pkg = PropertyPackageType[src_pkg_name]
                dest_pkg = PropertyPackageType[dst_pkg_name]
            except (KeyError, ValueError):
                pass

        if source_pkg and dest_pkg:
            compat = check_connection_compatibility(source_pkg, dest_pkg)

            if compat["compatible"]:
                if compat["needs_translator"]:
                    # Translator is needed and exists
                    if auto_create_translator:
                        # Auto-create the translator and register it in session
                        translator_id = f"translator_{source_unit}_to_{dest_unit}"
                        translator_spec = compat.get("translator")
                        if translator_spec:
                            # Register translator in session with correct keys for ModelBuilder
                            session.translators[translator_id] = {
                                "name": translator_spec.name if hasattr(translator_spec, 'name') else str(translator_spec),
                                "source_pkg": source_pkg.value,
                                "dest_pkg": dest_pkg.value,
                                "module_path": translator_spec.module_path if hasattr(translator_spec, 'module_path') else "",
                                "config": {},
                            }
                            session_manager.save(session)
                        translator_note = (
                            f"Auto-created translator '{translator_id}' for "
                            f"{source_pkg.name} → {dest_pkg.name} connection."
                        )
                    else:
                        translator_note = (
                            f"Translator needed: {compat['translator']} "
                            f"for {source_pkg.name} → {dest_pkg.name}. "
                            "Set auto_create_translator=True or use create_translator."
                        )
                else:
                    translator_note = "Same property package - direct connection."
            else:
                translator_note = (
                    f"WARNING: No translator exists for {source_pkg.name} → {dest_pkg.name}. "
                    "Connection created but may fail at runtime. "
                    "Use the same property package for both units."
                )
        else:
            translator_note = (
                "Property packages not configured on units. "
                "If packages differ, create translator explicitly."
            )

    # Use connect_ports internally
    result = connect_ports(
        session_id, source_unit, source_port, dest_unit, dest_port,
        translator_id=translator_id,
    )

    if "error" not in result and translator_note:
        result["translator_note"] = translator_note

    return result


@mcp.tool()
def build_and_solve(
    session_id: str,
    solver_options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Execute full hygiene pipeline: DOF check, scaling, init, solve.

    Convenience wrapper that runs the complete solve pipeline:
    1. DOF check
    2. Calculate scaling factors
    3. Sequential initialization
    4. Solve

    Args:
        session_id: Session to build and solve
        solver_options: Optional solver configuration

    Returns:
        Job ID for background execution
    """
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    # Submit as background job with full pipeline
    params = {
        "run_full_pipeline": True,
        "solver_options": solver_options or {},
    }

    job = job_manager.submit(session_id, "solve", params)

    return {
        "job_id": job.job_id,
        "session_id": session_id,
        "status": job.status.value,
        "message": "Full hygiene pipeline submitted (DOF -> Scale -> Init -> Solve)",
    }


@mcp.tool()
def get_job_status(job_id: str) -> Dict[str, Any]:
    """Get status of a background job.

    Alias for get_solve_status for naming consistency with plan.

    Args:
        job_id: Job identifier

    Returns:
        Job status, progress, and result if complete
    """
    return get_solve_status(job_id)


@mcp.tool()
def get_job_results(job_id: str) -> Dict[str, Any]:
    """Get results of a completed background job.

    Args:
        job_id: Job identifier

    Returns:
        Job results if complete, status otherwise
    """
    status = job_manager.get_status(job_id)
    if status is None:
        return {"error": f"Job '{job_id}' not found"}

    if status.status != JobStatus.COMPLETED:
        return {
            "job_id": job_id,
            "status": status.status.value,
            "message": "Job not yet complete",
            "progress": status.progress,
        }

    return {
        "job_id": job_id,
        "status": "completed",
        "result": status.result,
    }


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    mcp.run()
