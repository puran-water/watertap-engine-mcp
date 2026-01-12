# WaterTAP Engine MCP Server - Development Guide

MCP server exposing WaterTAP water treatment process modeling. Server usage and workflows are documented in a separate agent skill.

## Architecture

```
watertap-engine-mcp/
├── server.py              # FastMCP server (57 @mcp.tool decorators)
├── cli.py                 # Typer CLI - calls server functions directly
├── worker.py              # Background job subprocess (IPOPT isolation)
├── core/
│   ├── property_registry.py    # PropertyPackageType enum, PROPERTY_PACKAGES dict
│   ├── translator_registry.py  # TRANSLATORS dict (ASM↔ADM only)
│   ├── unit_registry.py        # UNITS dict with UnitSpec dataclasses
│   ├── water_state.py          # WaterTAPState feed abstraction
│   └── session.py              # FlowsheetSession, SessionManager, persistence
├── solver/
│   ├── pipeline.py             # HygienePipeline state machine
│   ├── dof_resolver.py         # DOF analysis utilities
│   ├── scaler.py               # IDAES scaling wrappers
│   ├── initializer.py          # SequentialDecomposition wrapper
│   ├── diagnostics.py          # DiagnosticsToolbox wrapper
│   └── recovery.py             # RecoveryExecutor (bound relaxation, scaling)
├── utils/
│   ├── model_builder.py        # Session → Pyomo model conversion
│   ├── auto_translator.py      # Translator insertion logic
│   ├── job_manager.py          # Background job queue
│   ├── state_translator.py     # Feed state to property package conversion
│   └── topo_sort.py            # Topological sort for planning
├── templates/                  # Pre-built flowsheet templates
├── jobs/                       # Runtime: sessions/, running/, completed/
├── tests/                      # 373 tests
└── docs/plans/                 # Implementation plans
```

## Critical Constraints

### Property Package Class-Name Collisions

```python
# WRONG - ambiguous class names exist in multiple modules
from watertap.property_models import NaClParameterBlock  # Which one?

# RIGHT - use module_path from registry
from core.property_registry import PROPERTY_PACKAGES
pkg = PROPERTY_PACKAGES[PropertyPackageType.NACL]
module = importlib.import_module(pkg.module_path)
ParamBlock = getattr(module, pkg.param_block_class)
```

Collisions:
- `NaClParameterBlock`: `NaCl_prop_pack.py` vs `NaCl_T_dep_prop_pack.py`
- `WaterParameterBlock`: `water_prop_pack.py` vs `zero_order_properties.py`

### No Cross-Package Translators

Only ASM↔ADM biological translators exist. These do NOT exist:
- ZO → Seawater, Seawater → NaCl, MCAS → Seawater

For non-biological flowsheets: use SAME property package throughout entire flowsheet.

### Variable Path Resolution

`utils/model_builder.py` handles dotted paths and wildcards:
```python
# Simple: "area", "A_comp[0,H2O]"
# Dotted: "control_volume.properties_out[0].pressure"
# Wildcard: "feed_side.cp_modulus[0,*,*]"

# Uses find_component() with manual fallback
var, idx = builder._resolve_variable_path(unit, path)
```

## Key Implementation Patterns

### Server Tool Pattern

```python
@mcp.tool()
def tool_name(session_id: str, ...) -> Dict[str, Any]:
    """Docstring for MCP tool description."""
    try:
        session = session_manager.load(session_id)
    except FileNotFoundError:
        return {"error": f"Session '{session_id}' not found"}

    # ... implementation ...

    session_manager.save(session)
    return {"session_id": session_id, "result": ...}
```

### CLI Parity Pattern

CLI commands call server functions directly (no separate implementation):
```python
@app.command()
def cli_command(session_id: str = typer.Option(...)):
    import server as srv
    result = srv.server_tool(session_id)
    # Format result with Rich
```

### Session Persistence

```python
# FlowsheetSession fields that persist:
session.to_dict()  # Serializes to JSON
# - config (SessionConfig with property_package, solver options)
# - units (Dict[str, UnitInstance] with fixed_vars, scaling_factors, costing_enabled)
# - connections (List[Connection])
# - feed_state (Dict with tuple keys serialized as strings)
# - costing_config (Dict for enable_costing settings)
# - results (Dict with tuple keys serialized as strings)
```

### Model Building

```python
from utils.model_builder import ModelBuilder

builder = ModelBuilder(session)
m = builder.build()  # Returns ConcreteModel

# Build steps:
# 1. Create flowsheet block with property package
# 2. Create feed block from session.feed_state
# 3. Create units from session.units
# 4. Apply fixed_vars and scaling_factors
# 5. Create connections via Arc
# 6. Create costing if session.costing_config enabled
```

### Solve Pipeline

Two paths in `worker.py`:
1. `run_solve()` - Direct: DOF check → scale → init → IPOPT
2. `run_full_pipeline()` - HygienePipeline with recovery on failure

Both persist results to session for `get_*_results()` retrieval.

## Testing

```bash
# Full suite (373 tests)
pytest tests/ -v

# Specific test files
pytest tests/test_path_resolution.py -v
pytest tests/test_costing.py -v
pytest tests/test_cli_parity.py -v
pytest tests/test_validate_flowsheet.py -v
pytest tests/e2e/ -v
```

Test conventions:
- Real WaterTAP models (no MagicMock)
- Fail loudly (no pytest.skip, no silent exceptions)
- `filterwarnings = ["error"]` in pyproject.toml

## Development Progress

### 2026-01-12: Code Review Remediation Complete

**Plan:** `docs/plans/code-review-remediation-plan.md`

- Variable path resolution with find_component + wildcards (`utils/model_builder.py`)
- CLI stub remediation - all CLI calls server functions directly
- validate_flowsheet enhancement - orphan ports + connection-level property package check
- Costing tools (6): enable_costing, add_unit_costing, disable_unit_costing, set_costing_parameters, list_costed_units, compute_costing
- Session persistence fixes (costing_config, ZO parameters)
- Test coverage: 373 tests across 4 new test files

### 2026-01-11: Recovery Integration & E2E Testing

- RecoveryExecutor integrated into HygienePipeline (`solver/pipeline.py:523-545`)
- Results source indication (`"source": "unsolved_model"` warning)
- 75 E2E tests, 10 bug fixes
- Test suite hardened (no MagicMock, fail loudly policy)

### Documentation

- `docs/plans/code-review-remediation-plan.md` - Latest remediation plan
- `docs/plans/implementation-plan.md` - Original architecture
- `docs/plans/e2e-test-suite-plan.md` - E2E test design
- `BUGS.md` - Bug tracking
