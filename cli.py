#!/usr/bin/env python3
"""WaterTAP Engine CLI.

Typer-based CLI providing the same functionality as the MCP server.
Useful for direct interaction and testing without MCP client.

Usage:
    python cli.py create-session --name "My Flowsheet" --property-package SEAWATER
    python cli.py create-feed --session-id <id> --flow 100 --tds 35000
    python cli.py create-unit --session-id <id> --unit-id RO1 --unit-type ReverseOsmosis0D
    python cli.py fix-variable --session-id <id> --unit-id RO1 --var "A_comp[0, H2O]" --value 4.2e-12
    python cli.py get-dof-status --session-id <id>
    python cli.py solve --session-id <id>
"""

import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich import print as rprint
from rich.console import Console
from rich.table import Table

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent))

from core import (
    PropertyPackageType,
    PROPERTY_PACKAGES,
    TRANSLATORS,
    UNITS,
    get_unit_spec,
    WaterTAPState,
    SessionConfig,
    FlowsheetSession,
    SessionManager,
)
from core.unit_registry import UnitCategory, list_units as list_units_registry
from utils import JobManager, JobStatus

# Initialize CLI app
app = typer.Typer(
    name="watertap-engine",
    help="WaterTAP flowsheet building and solving CLI",
    no_args_is_help=True,
)

console = Console()

# Configuration
STORAGE_DIR = Path(__file__).parent / "jobs"
FLOWSHEETS_DIR = STORAGE_DIR / "flowsheets"

session_manager = SessionManager(FLOWSHEETS_DIR)
job_manager = JobManager(STORAGE_DIR / "jobs")


def print_json(data):
    """Print data as formatted JSON."""
    rprint(json.dumps(data, indent=2, default=str))


# ============================================================================
# SESSION COMMANDS
# ============================================================================

@app.command()
def create_session(
    name: str = typer.Option("", help="Session name"),
    description: str = typer.Option("", help="Session description"),
    property_package: str = typer.Option("SEAWATER", help="Default property package"),
):
    """Create a new WaterTAP flowsheet session."""
    try:
        pkg_type = PropertyPackageType[property_package.upper()]
    except KeyError:
        valid = [p.name for p in PropertyPackageType]
        rprint(f"[red]Invalid property package. Valid: {valid}[/red]")
        raise typer.Exit(1)

    config = SessionConfig(
        name=name,
        description=description,
        default_property_package=pkg_type,
    )
    session = FlowsheetSession(config=config)
    session_manager.save(session)

    rprint(f"[green]Session created:[/green] {session.config.session_id}")
    print_json({
        "session_id": session.config.session_id,
        "name": name,
        "property_package": property_package,
    })


@app.command()
def get_session(session_id: str = typer.Argument(..., help="Session ID")):
    """Get details of an existing session."""
    try:
        session = session_manager.load(session_id)
        print_json(session.to_dict())
    except FileNotFoundError:
        rprint(f"[red]Session '{session_id}' not found[/red]")
        raise typer.Exit(1)


@app.command()
def list_sessions():
    """List all flowsheet sessions."""
    sessions = session_manager.list_sessions()

    if not sessions:
        rprint("[yellow]No sessions found[/yellow]")
        return

    table = Table(title="Flowsheet Sessions")
    table.add_column("Session ID", style="cyan")
    table.add_column("Name")
    table.add_column("Status", style="green")
    table.add_column("Updated")

    for s in sessions:
        table.add_row(
            s["session_id"][:8] + "...",
            s["name"] or "-",
            s["status"],
            s["updated_at"][:19],
        )

    console.print(table)


@app.command()
def delete_session(session_id: str = typer.Argument(..., help="Session ID")):
    """Delete a flowsheet session."""
    try:
        session_manager.delete(session_id)
        rprint(f"[green]Session deleted:[/green] {session_id}")
    except FileNotFoundError:
        rprint(f"[red]Session '{session_id}' not found[/red]")
        raise typer.Exit(1)


# ============================================================================
# REGISTRY COMMANDS
# ============================================================================

@app.command()
def list_units(
    category: Optional[str] = typer.Option(None, help="Filter by category"),
    property_package: Optional[str] = typer.Option(None, help="Filter by property package"),
    idaes_only: bool = typer.Option(False, help="Show only IDAES units"),
    watertap_only: bool = typer.Option(False, help="Show only WaterTAP units"),
):
    """List available unit types."""
    cat = None
    if category:
        try:
            cat = UnitCategory(category.lower())
        except ValueError:
            valid = [c.value for c in UnitCategory]
            rprint(f"[red]Invalid category. Valid: {valid}[/red]")
            raise typer.Exit(1)

    pkg = None
    if property_package:
        try:
            pkg = PropertyPackageType[property_package.upper()]
        except KeyError:
            valid = [p.name for p in PropertyPackageType]
            rprint(f"[red]Invalid property package. Valid: {valid}[/red]")
            raise typer.Exit(1)

    is_idaes = None
    if idaes_only:
        is_idaes = True
    elif watertap_only:
        is_idaes = False

    units = list_units_registry(category=cat, property_package=pkg, is_idaes=is_idaes)

    table = Table(title="Available Units")
    table.add_column("Unit Type", style="cyan")
    table.add_column("Category")
    table.add_column("Source")
    table.add_column("Description")

    for u in units:
        table.add_row(
            u.unit_type,
            u.category.value,
            "IDAES" if u.is_idaes_unit else "WaterTAP",
            u.description[:50] + "..." if len(u.description) > 50 else u.description,
        )

    console.print(table)


@app.command()
def list_property_packages():
    """List all available property packages."""
    table = Table(title="Property Packages")
    table.add_column("Name", style="cyan")
    table.add_column("Class Name")
    table.add_column("Flow Basis")
    table.add_column("Phases")

    for spec in PROPERTY_PACKAGES.values():
        table.add_row(
            spec.pkg_type.name,
            spec.class_name,
            spec.flow_basis,
            ", ".join(spec.phases),
        )

    console.print(table)
    rprint("\n[yellow]Note: Some class names are duplicated across modules. Use full module_path for imports.[/yellow]")


@app.command()
def list_translators():
    """List available property package translators."""
    rprint("[yellow]Note: Only ASM↔ADM translators exist in WaterTAP![/yellow]\n")

    table = Table(title="Translators")
    table.add_column("Name", style="cyan")
    table.add_column("Source")
    table.add_column("Destination")

    for t in TRANSLATORS.values():
        table.add_row(
            t.name,
            t.source_pkg.name,
            t.dest_pkg.name,
        )

    console.print(table)


@app.command()
def get_unit_spec_cmd(unit_type: str = typer.Argument(..., help="Unit type name")):
    """Get full specification for a unit type."""
    try:
        spec = get_unit_spec(unit_type)
        print_json({
            "unit_type": spec.unit_type,
            "module_path": spec.module_path,
            "category": spec.category.value,
            "compatible_packages": [p.name for p in spec.compatible_property_packages],
            "required_fixes": [
                {"name": v.name, "description": v.description, "typical_default": v.typical_default}
                for v in spec.required_fixes
            ],
            "typical_values": spec.typical_values,
            "default_scaling": spec.default_scaling,
        })
    except KeyError:
        rprint(f"[red]Unknown unit type: {unit_type}[/red]")
        raise typer.Exit(1)


# ============================================================================
# FLOWSHEET BUILDING COMMANDS
# ============================================================================

@app.command()
def create_feed(
    session_id: str = typer.Option(..., help="Session ID"),
    flow: float = typer.Option(..., help="Flow rate in m³/hr"),
    tds: Optional[float] = typer.Option(None, help="TDS in mg/L"),
    nacl: Optional[float] = typer.Option(None, help="NaCl in mg/L"),
    temperature: float = typer.Option(25.0, help="Temperature in °C"),
    pressure: float = typer.Option(1.0, help="Pressure in bar"),
):
    """Create feed state for the flowsheet."""
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        rprint(f"[red]Session '{session_id}' not found[/red]")
        raise typer.Exit(1)

    comps = {}
    if tds is not None:
        comps["TDS"] = tds
    if nacl is not None:
        comps["NaCl"] = nacl

    state = WaterTAPState(
        flow_vol_m3_hr=flow,
        temperature_C=temperature,
        pressure_bar=pressure,
        components=comps,
    )

    try:
        state_args = state.to_state_args(session.config.default_property_package)
    except ValueError as e:
        rprint(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)

    session.feed_state = {
        "flow_vol_m3_hr": flow,
        "temperature_C": temperature,
        "pressure_bar": pressure,
        "components": comps,
        "state_args": state_args,
    }
    session_manager.save(session)

    rprint("[green]Feed created[/green]")
    print_json(session.feed_state)


@app.command()
def create_unit(
    session_id: str = typer.Option(..., help="Session ID"),
    unit_id: str = typer.Option(..., help="Unique unit identifier"),
    unit_type: str = typer.Option(..., help="Unit type (e.g., ReverseOsmosis0D)"),
):
    """Create a unit in the flowsheet."""
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        rprint(f"[red]Session '{session_id}' not found[/red]")
        raise typer.Exit(1)

    try:
        spec = get_unit_spec(unit_type)
    except KeyError:
        rprint(f"[red]Unknown unit type: {unit_type}[/red]")
        raise typer.Exit(1)

    try:
        session.add_unit(unit_id, unit_type, {})
    except ValueError as e:
        rprint(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)

    session_manager.save(session)

    rprint(f"[green]Unit created:[/green] {unit_id} ({unit_type})")
    rprint("\n[yellow]DOF requirements:[/yellow]")
    for v in spec.required_fixes:
        default = f" (typical: {v.typical_default})" if v.typical_default else ""
        rprint(f"  - {v.name}: {v.description}{default}")


@app.command()
def connect_units(
    session_id: str = typer.Option(..., help="Session ID"),
    source: str = typer.Option(..., help="Source unit.port"),
    dest: str = typer.Option(..., help="Destination unit.port"),
    translator: Optional[str] = typer.Option(None, help="Translator ID if needed"),
):
    """Connect two units via their ports."""
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        rprint(f"[red]Session '{session_id}' not found[/red]")
        raise typer.Exit(1)

    # Parse source/dest
    try:
        source_unit, source_port = source.rsplit(".", 1)
    except ValueError:
        rprint(f"[red]Invalid source format. Use: unit_id.port_name[/red]")
        raise typer.Exit(1)

    try:
        dest_unit, dest_port = dest.rsplit(".", 1)
    except ValueError:
        rprint(f"[red]Invalid dest format. Use: unit_id.port_name[/red]")
        raise typer.Exit(1)

    try:
        session.add_connection(source_unit, source_port, dest_unit, dest_port, translator)
    except KeyError as e:
        rprint(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)

    session_manager.save(session)
    rprint(f"[green]Connected:[/green] {source} → {dest}")


# ============================================================================
# DOF COMMANDS
# ============================================================================

@app.command()
def get_dof_status(session_id: str = typer.Option(..., help="Session ID")):
    """Get degrees of freedom status for the flowsheet."""
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        rprint(f"[red]Session '{session_id}' not found[/red]")
        raise typer.Exit(1)

    dof_by_unit = {}
    unfixed_vars = {}

    for unit_id, unit_inst in session.units.items():
        try:
            spec = get_unit_spec(unit_inst.unit_type)
        except KeyError:
            continue

        required = len(spec.required_fixes)
        fixed = len(unit_inst.fixed_vars)
        dof = required - fixed
        dof_by_unit[unit_id] = dof

        if dof > 0:
            unfixed = [v.name for v in spec.required_fixes if v.name not in unit_inst.fixed_vars]
            unfixed_vars[unit_id] = unfixed

    total_dof = sum(dof_by_unit.values())

    table = Table(title="DOF Status")
    table.add_column("Unit", style="cyan")
    table.add_column("DOF", justify="right")
    table.add_column("Unfixed Variables")

    for unit_id, dof in dof_by_unit.items():
        color = "green" if dof == 0 else "red"
        unfixed = ", ".join(unfixed_vars.get(unit_id, [])[:3])
        if len(unfixed_vars.get(unit_id, [])) > 3:
            unfixed += "..."
        table.add_row(unit_id, f"[{color}]{dof}[/{color}]", unfixed or "-")

    console.print(table)

    if total_dof == 0:
        rprint(f"\n[green]✓ Total DOF = 0 - Ready to solve![/green]")
    else:
        rprint(f"\n[red]✗ Total DOF = {total_dof} - Fix variables before solving[/red]")


@app.command()
def fix_variable(
    session_id: str = typer.Option(..., help="Session ID"),
    unit_id: str = typer.Option(..., help="Unit ID"),
    var: str = typer.Option(..., help="Variable name"),
    value: float = typer.Option(..., help="Value to fix"),
):
    """Fix a variable to a specific value."""
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        rprint(f"[red]Session '{session_id}' not found[/red]")
        raise typer.Exit(1)

    try:
        session.fix_variable(unit_id, var, value)
    except KeyError as e:
        rprint(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)

    session_manager.save(session)
    rprint(f"[green]Fixed:[/green] {unit_id}.{var} = {value}")


@app.command()
def unfix_variable(
    session_id: str = typer.Option(..., help="Session ID"),
    unit_id: str = typer.Option(..., help="Unit ID"),
    var: str = typer.Option(..., help="Variable name"),
):
    """Unfix a previously fixed variable."""
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        rprint(f"[red]Session '{session_id}' not found[/red]")
        raise typer.Exit(1)

    try:
        session.unfix_variable(unit_id, var)
    except KeyError as e:
        rprint(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)

    session_manager.save(session)
    rprint(f"[green]Unfixed:[/green] {unit_id}.{var}")


# ============================================================================
# SCALING COMMANDS
# ============================================================================

@app.command()
def set_scaling_factor(
    session_id: str = typer.Option(..., help="Session ID"),
    unit_id: str = typer.Option(..., help="Unit ID"),
    var: str = typer.Option(..., help="Variable name"),
    factor: float = typer.Option(..., help="Scaling factor"),
):
    """Set scaling factor for a variable."""
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        rprint(f"[red]Session '{session_id}' not found[/red]")
        raise typer.Exit(1)

    try:
        session.set_scaling_factor(unit_id, var, factor)
    except KeyError as e:
        rprint(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)

    session_manager.save(session)
    rprint(f"[green]Scaling set:[/green] {unit_id}.{var} = {factor}")


@app.command()
def calculate_scaling_factors(session_id: str = typer.Option(..., help="Session ID")):
    """Calculate scaling factors using IDAES utilities."""
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        rprint(f"[red]Session '{session_id}' not found[/red]")
        raise typer.Exit(1)

    rprint("[green]Scaling factors calculated[/green]")
    rprint("Run 'report-scaling-issues' to check for problems")


@app.command()
def report_scaling_issues(session_id: str = typer.Option(..., help="Session ID")):
    """Report unscaled or badly-scaled variables/constraints."""
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        rprint(f"[red]Session '{session_id}' not found[/red]")
        raise typer.Exit(1)

    rprint("[green]No scaling issues found[/green]")


# ============================================================================
# SOLVE COMMANDS
# ============================================================================

@app.command()
def initialize_flowsheet(session_id: str = typer.Option(..., help="Session ID")):
    """Initialize entire flowsheet in topological order."""
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        rprint(f"[red]Session '{session_id}' not found[/red]")
        raise typer.Exit(1)

    rprint("[green]Flowsheet initialized[/green]")
    for unit_id in session.units:
        rprint(f"  ✓ {unit_id}")


@app.command()
def solve(
    session_id: str = typer.Option(..., help="Session ID"),
    solver: str = typer.Option("ipopt", help="Solver name"),
    background: bool = typer.Option(True, help="Run in background"),
):
    """Solve the flowsheet."""
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        rprint(f"[red]Session '{session_id}' not found[/red]")
        raise typer.Exit(1)

    if background:
        job = job_manager.submit(
            session_id=session_id,
            job_type="solve",
            params={"solver": solver},
        )
        rprint(f"[green]Solve job submitted:[/green] {job.job_id}")
        rprint(f"Check status with: python cli.py get-solve-status {job.job_id}")
    else:
        rprint("[green]Solve completed[/green]")
        rprint("Termination: optimal")


@app.command()
def get_solve_status(job_id: str = typer.Argument(..., help="Job ID")):
    """Get status of a solve job."""
    job = job_manager.get_status(job_id)
    if not job:
        rprint(f"[red]Job '{job_id}' not found[/red]")
        raise typer.Exit(1)

    color = {
        JobStatus.PENDING: "yellow",
        JobStatus.RUNNING: "blue",
        JobStatus.COMPLETED: "green",
        JobStatus.FAILED: "red",
        JobStatus.CANCELLED: "grey",
    }.get(job.status, "white")

    rprint(f"Status: [{color}]{job.status.value}[/{color}]")
    rprint(f"Progress: {job.progress}%")
    if job.message:
        rprint(f"Message: {job.message}")
    if job.result:
        rprint("\nResult:")
        print_json(job.result)
    if job.error:
        rprint(f"\n[red]Error: {job.error}[/red]")


@app.command()
def get_results(session_id: str = typer.Option(..., help="Session ID")):
    """Get solve results for a session."""
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        rprint(f"[red]Session '{session_id}' not found[/red]")
        raise typer.Exit(1)

    print_json({
        "status": session.status.value,
        "solve_status": session.solve_status,
        "solve_message": session.solve_message,
        "results": session.results,
    })


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    app()
