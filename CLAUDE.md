# WaterTAP Engine MCP Server

MCP server for WaterTAP water treatment process modeling. Provides atomic, explicit operations for flowsheet construction, DOF management, scaling, initialization, and solving.

## Architecture

**Dual Adapter Pattern:**
- `server.py` - FastMCP server (57 tools) for MCP clients
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
├── server.py              # FastMCP server (57 tools)
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
├── tests/                      # 373 tests
└── docs/plans/                 # Implementation plans
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

## Tool Categories (57 Total)

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

### Costing (6)
- `enable_costing` - Enable flowsheet costing (WaterTAP or ZeroOrder)
- `add_unit_costing` - Enable costing for specific unit
- `disable_unit_costing` - Disable unit costing
- `set_costing_parameters` - Set economic parameters
- `list_costed_units` - List units with costing status
- `compute_costing` - Calculate LCOW, CapEx, OpEx after solve

### Results (4)
- `get_stream_results` - Stream tables
- `get_unit_results` - Unit performance
- `get_costing` - LCOW, CapEx, OpEx
- `get_results` - All results

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

## Solve Paths

The server provides two solve approaches:

1. **`solve()` tool** - Direct path (`worker.run_solve()`)
   - DOF check (warns but continues)
   - Calculate scaling factors
   - Sequential initialization
   - IPOPT solve
   - Best for: Simple flowsheets, iterative development

2. **`build_and_solve()` tool** - Full pipeline (`worker.run_full_pipeline()`)
   - Uses HygienePipeline state machine
   - Includes pre/post-solve diagnostics
   - Supports recovery on failure (bound relaxation, scaling adjustment)
   - Progress callbacks for each stage
   - Best for: Complex flowsheets, production use

Both paths persist results to the session for retrieval via `get_stream_results` and `get_unit_results`.

## Development Progress

### 2026-01-12: Code Review Remediation Plan Complete

**Plan:** `docs/plans/code-review-remediation-plan.md` - All 7 phases implemented and verified by Codex

**Phase 1 - Variable Path Resolution:**
- Implemented comprehensive path resolution in `utils/model_builder.py`
- Handles dotted paths like `control_volume.properties_out[0].pressure`
- Supports wildcards like `feed_side.cp_modulus[0,*,*]`
- Uses Pyomo's `find_component()` with fallback to manual resolution
- 26 tests in `tests/test_path_resolution.py`

**Phase 2 - CLI Stub Remediation:**
- Fixed 4 CLI commands that were stubs (printing success without doing work)
- CLI now calls server functions directly for parity
- 12 tests in `tests/test_cli_parity.py`

**Phase 3 - validate_flowsheet Enhancement:**
- Added orphan port detection (unconnected inlet/outlet ports)
- Added connection-level property package compatibility checking
- Detects when translator is needed but missing
- 9 tests in `tests/test_validate_flowsheet.py`

**Phase 4 - Costing Implementation (6 new tools):**
- `enable_costing(session_id, costing_package)` - Enable WaterTAP or ZeroOrder costing
- `add_unit_costing(session_id, unit_id)` - Enable costing on specific units
- `disable_unit_costing(session_id, unit_id)` - Disable unit costing
- `set_costing_parameters(session_id, ...)` - Set economic parameters
- `list_costed_units(session_id)` - List units with costing status
- `compute_costing(session_id)` - Calculate LCOW, CapEx, OpEx after solve
- ModelBuilder now creates costing blocks during build if enabled
- 22 tests in `tests/test_costing.py`

**Phase 5 - Session Persistence:**
- Fixed `costing_config` not persisting (added to `to_dict`/`from_dict`)
- Fixed `load_zo_parameters` to persist database, process_subtype, and parameters

**Phase 6 - Test Coverage:**
- Total: 373 tests passing
- New test files: `test_path_resolution.py`, `test_validate_flowsheet.py`, `test_costing.py`, `test_cli_parity.py`

**Phase 7 - Documentation:**
- CLAUDE.md updated (57 tools)
- README.md updated with Costing section

### 2026-01-11 (Session 2): Code Review Feedback & Recovery Integration

**Code Review Audit:**
- Reviewed 10 code review claims against actual codebase
- **8 of 10 claims were inaccurate** (code already correct)
- **2 valid issues identified and fixed**
- Independent verification by Codex confirmed findings

**Recovery Module Integration:**
- Integrated `RecoveryExecutor` into `HygienePipeline` failure handling
- Pipeline now attempts automatic recovery on solve failure:
  - Bound relaxation strategy
  - Scaling adjustment strategy
  - Up to 3 recovery attempts before failing
- Location: `solver/pipeline.py` lines 523-545

**Results Source Indication:**
- Added `source` and `warning` fields to results fallback paths
- `get_stream_results()` returns `"source": "unsolved_model"` with warning before solve
- `get_unit_results()` returns same fields
- Agents now know if values are from solved or unsolved models

**New Tests:**
- `tests/test_solver.py`: Added `TestRecoveryIntegration` class (5 tests)
- `tests/e2e/test_results_source.py`: Results source indication tests (4 tests)

**Documentation:**
- `docs/plans/code-review-feedback-plan.md` - Feedback accuracy assessment and implementation plan
- Added "Solve Paths" section to CLAUDE.md documenting two solve approaches

### 2026-01-11 (Session 1): E2E Testing & Bug Fixes

**Completed:**
- **10 bugs fixed** during E2E testing (see BUGS.md for details)
- **75 E2E tests** covering CLI commands, workflows, error paths, property packages
- **Test suite hardened** with Codex audit recommendations:
  - All MagicMock removed - tests use real WaterTAP models
  - All silent exception swallowing removed - failures logged as warnings
  - pytest warnings-as-errors enabled (filterwarnings = ["error"])
  - All pytest.skip/skipif removed - tests FAIL LOUDLY if deps unavailable
  - datetime.utcnow() deprecation fixed across codebase

**Key Bug Fixes:**
1. ZO database config auto-provided for Zero-Order property packages
2. Tuple key serialization for JSON persistence
3. SequentialDecomposition API corrected (create_graph + calculation_order)
4. SequentialDecomposition uses heuristic tear selection (no cplex dependency)
5. Windows/WSL stdout flush error handling in solver subprocess
6. Job result JSON truncation fixed with proper flushing
7. UndefinedData type handling for solve metrics

### Documentation
- `BUGS.md` - Bug tracking and test session notes
- `docs/plans/implementation-plan.md` - Server architecture and design
- `docs/plans/e2e-test-suite-plan.md` - E2E test suite design
- `docs/plans/code-review-feedback-plan.md` - Code review audit and fixes
