# WaterTAP Engine MCP Server

MCP server for WaterTAP water treatment process modeling. Provides atomic, explicit operations for flowsheet construction, DOF management, scaling, initialization, and solving.

## Architecture

**Dual Adapter Pattern:**
- `server.py` - FastMCP server (51 tools) for MCP clients
- `cli.py` - Typer CLI with same functionality

**Companion Skill:**
The `watertap-flowsheet-builder` skill (in `~/skills/watertap-skill/`) provides domain intelligence. This server is intentionally atomic - the skill orchestrates and suggests.

## Key Design Principles

1. **Explicit Operations** - No hidden automation. User/skill controls every fix/scale/init.
2. **Property Package Awareness** - Only ASM↔ADM translators exist. Other packages require same-package flowsheets.
3. **Orchestrate, Don't Replace** - Wraps WaterTAP/IDAES utilities (check_dof, calculate_scaling_factors, DiagnosticsToolbox).

## Directory Structure

```
watertap-engine-mcp/
├── server.py              # FastMCP server (51 tools)
├── cli.py                 # Typer CLI adapter
├── worker.py              # Background job worker
├── core/
│   ├── property_registry.py    # 13 property packages with module paths
│   ├── translator_registry.py  # 8 ASM/ADM translators
│   ├── unit_registry.py        # 21+ unit specs with DOF, scaling, init
│   ├── water_state.py          # Feed state abstraction
│   └── session.py              # Session management
├── solver/
│   ├── pipeline.py             # Hygiene pipeline state machine
│   ├── dof_resolver.py         # DOF analysis
│   ├── scaler.py               # Scaling tools
│   ├── initializer.py          # Sequential initialization (IDAES)
│   ├── diagnostics.py          # DiagnosticsToolbox wrapper
│   └── recovery.py             # Failure recovery
├── utils/
│   ├── model_builder.py        # Session → Pyomo model
│   ├── auto_translator.py      # Translator insertion
│   ├── job_manager.py          # Background job execution
│   ├── state_translator.py     # Feed state conversion
│   └── topo_sort.py            # Initialization order (planning)
├── templates/                  # Pre-built flowsheet templates
│   ├── ro_train.py
│   ├── nf_softening.py
│   └── mvc_crystallizer.py
├── jobs/                       # Session/job persistence (runtime)
└── tests/                      # 206 unit tests
```

## Property Package Gotchas

**Class-Name Collisions:**
- `NaClParameterBlock` exists in both `NaCl_prop_pack.py` AND `NaCl_T_dep_prop_pack.py`
- `WaterParameterBlock` exists in both `water_prop_pack.py` AND `zero_order_properties.py`
- Always use `module_path` from registry, not class name alone

**No Cross-Package Translators:**
These DO NOT exist:
- ZO → Seawater
- Seawater → NaCl
- MCAS → Seawater

For non-biological flowsheets, use the SAME property package throughout.

## Tool Categories (51 Total)

### Session (4)
- `create_session` - New session with property package
- `get_session` - Session details
- `list_sessions` - All sessions
- `delete_session` - Remove session

### Registry (4)
- `list_units` - Available unit types
- `list_property_packages` - Package compatibility
- `list_translators` - ASM/ADM translators
- `get_unit_spec` - DOF requirements, scaling hints

### Build (5)
- `create_feed` - Simplified feed state
- `create_unit` - Add unit to flowsheet
- `create_translator` - Add ASM/ADM translator
- `connect_ports` - Connect unit ports
- `delete_unit` - Remove unit

### DOF (4)
- `get_dof_status` - DOF count per unit
- `fix_variable` - Fix variable to value
- `unfix_variable` - Release variable
- `list_unfixed_vars` - Show unfixed variables

### Scaling (4)
- `set_scaling_factor` - Set explicit scaling
- `calculate_scaling_factors` - Run IDAES scaling
- `report_scaling_issues` - Find scaling problems
- `autoscale_large_jac` - Jacobian-based scaling

### Solver (6)
- `initialize_unit` - Initialize single unit
- `initialize_flowsheet` - Sequential initialization
- `propagate_state` - State between ports
- `solve` - Background solve job
- `get_solve_status` - Job status
- `run_diagnostics` - DiagnosticsToolbox

### Zero-Order (3)
- `load_zo_parameters` - Load from database
- `list_zo_databases` - Available databases
- `get_zo_unit_parameters` - Unit parameters

### Results (3)
- `get_stream_results` - Stream tables
- `get_unit_results` - Unit performance
- `get_costing` - LCOW, CapEx, OpEx

## Running

### MCP Server
```bash
# With venv312 activated
python server.py
```

### CLI
```bash
python cli.py create-session --property-package SEAWATER
python cli.py create-feed --flow 100 --tds 35000
python cli.py create-unit RO --id RO1
python cli.py get-dof-status
python cli.py fix-variable RO1.A_comp 4.2e-12
python cli.py solve
```

## Testing

```bash
pytest tests/ -v
```

## Development Notes

- Sessions persist to `jobs/sessions/` as JSON
- Background jobs persist to `jobs/running/` and `jobs/completed/`
- worker.py handles solve jobs in subprocess (IPOPT isolation)
- All tools return structured dicts for MCP; CLI formats with Rich

## Initialization Approach

**Standard:** IDAES SequentialDecomposition (WaterTAP best practice)
- Uses `pyomo.network.SequentialDecomposition` for proper order computation
- Integrated with IDAES constraint decomposition
- Handles complex recycle scenarios with tear stream selection

**Fail Loudly Policy:** If SequentialDecomposition is unavailable or fails, we return an error - we do NOT silently fall back to custom implementations.

**Session Planning:** For order estimation before model build, a simple topological sort is used with clear messaging that this is planning only, not actual initialization.
