# WaterTAP Engine MCP

A water treatment process simulation engine exposing [WaterTAP](https://github.com/watertap-org/watertap) capabilities through dual adapters for AI agent integration.

## Motivation

WaterTAP provides sophisticated equation-oriented models for water treatment processes (RO, NF, crystallizers, evaporators, biological treatment), but the modeling workflow requires substantial domain expertise: DOF management, proper scaling, sequential initialization, and solver diagnostics.

WaterTAP Engine MCP inverts this paradigm by making **natural language the primary interface**. Instead of manually managing DOF, scaling, and initialization, engineers can work with a companion skill that orchestrates these operations:

> "Build a seawater RO system with 100 m3/hr feed at 35,000 mg/L TDS, 50 m2 membrane area, and explain why recovery is limited"

This enables:

- **Collapsed iteration cycles**: Build -> solve -> diagnose -> patch -> re-solve without manual DOF tracking
- **Explicit hygiene operations**: Every fix, scale, and init is visible and controllable
- **Structured diagnostics**: DOF suggestions, scaling issues, and failure explanations surfaced directly to agents
- **Reproducible sessions**: Version-controlled flowsheet definitions with deterministic metadata

The goal is not to replace domain expertise, but to **remove friction** so engineers can focus on design decisions rather than solver mechanics.

## WaterTAP vs QSDsan: Solver Paradigms

This server is a sibling to [qsdsan-engine-mcp](../qsdsan-engine-mcp/) but uses a fundamentally different solver approach:

| Aspect | WaterTAP (This Server) | QSDsan |
|--------|------------------------|--------|
| **Solver Type** | Equation-oriented NLP (IPOPT) | ODE integration (solve_ivp) |
| **Modeling Framework** | Pyomo + IDAES | BioSTEAM |
| **Simulation Mode** | Steady-state algebraic | Dynamic ODE time-stepping |
| **Simulation Speed** | ~seconds to minutes per solve | ~milliseconds to seconds per run |
| **Optimization** | Native gradient-based (pyomo.environ.SolverFactory) | Requires external optimizer |
| **DOF Management** | Explicit - requires DOF=0 before solve | Implicit - feed-forward sequential |
| **Scaling** | Critical for convergence | Not required |
| **Sensitivities** | Automatic via NLP solver | Finite-difference approximation |

### Steady-State vs Dynamic Simulation

**WaterTAP (Equation-Oriented, Steady-State)**
- Solves a system of nonlinear algebraic equations: `F(x) = 0`
- All variables solved simultaneously to find steady-state directly
- Requires DOF = 0 (number of equations = number of unknowns)
- Ideal for **design optimization**: "What membrane area minimizes LCOW?"
- Supports native **sensitivity analysis** and **gradient-based optimization**
- Slower per solve but faster for optimization (exact gradients)

**QSDsan (ODE Integration, Dynamic)**
- Integrates differential equations forward in time: `dx/dt = f(x,t)`
- Simulates transient behavior from t=0 to t=T
- Variables update sequentially; no DOF constraint
- Ideal for **dynamic behavior**: "How does effluent quality change during startup?"
- Supports **Monte Carlo sampling** and **scenario enumeration** (fast per run)
- Faster per run but requires many runs for optimization (finite-difference)

### When to Use WaterTAP

- **Membrane process optimization**: RO/NF with detailed concentration polarization, fouling models
- **Gradient-based optimization**: Minimize LCOW, maximize recovery, optimal pressure
- **Rigorous thermodynamics**: Multi-component phase equilibria, crystallization
- **Design decisions**: Optimal membrane area, stage configuration, pressure setpoints
- **Sensitivity analysis**: How does recovery change with feed salinity?

### When to Use QSDsan

- **Biological treatment dynamics**: Activated sludge transients, digester startup/upset
- **Rapid scenario enumeration**: Monte Carlo uncertainty quantification, DOE
- **Time-series analysis**: Diurnal patterns, storm events, process disturbances
- **Simpler mass balances**: When steady-state is sufficient but dynamics are informative
- **Training simulators**: Interactive "what-if" exploration with fast feedback

## Architecture: Dual Adapters

The engine exposes identical functionality through two adapters:

```
                    +-------------------------------------+
                    |       WaterTAP Engine Core          |
                    |  (Registries, Pipeline, Sessions)   |
                    +-----------------+-------------------+
                                      |
              +-----------------------+---------------------+
              |                       |                     |
              v                       v                     v
     +----------------+      +----------------+     +----------------+
     |   MCP Adapter  |      |   CLI Adapter  |     |  Python API    |
     |   (server.py)  |      |   (cli.py)     |     |  (direct use)  |
     +----------------+      +----------------+     +----------------+
              |                       |
              v                       v
     +----------------+      +----------------+
     |  MCP Clients   |      |  Agent Skills  |
     |  (Claude, etc) |      |  (Claude Code) |
     +----------------+      +----------------+
```

### MCP Adapter (`server.py`)

For MCP-compatible clients (Claude Desktop, Cline, etc.):

```bash
python server.py
```

### CLI Adapter (`cli.py`)

For CLI-based agent runtimes and Agent Skills:

```bash
python cli.py --help
```

## Tool Surface

### Session Management Tools

| Tool | MCP | CLI | Description |
|------|-----|-----|-------------|
| `create_session` | `create_session` | `session new` | Create new flowsheet session |
| `get_session` | `get_session` | `session show` | Get session details |
| `list_sessions` | `list_sessions` | `session list` | List all sessions |
| `delete_session` | `delete_session` | `session delete` | Remove session |

### Registry & Discovery Tools

| Tool | MCP | CLI | Description |
|------|-----|-----|-------------|
| `list_units` | `list_units` | `units list` | List available unit types |
| `list_property_packages` | `list_property_packages` | `packages list` | List property packages |
| `list_translators` | `list_translators` | `translators list` | List ASM/ADM translators |
| `get_unit_spec` | `get_unit_spec` | `units spec` | Get DOF, scaling, init hints |

### Flowsheet Construction Tools

| Tool | MCP | CLI | Description |
|------|-----|-----|-------------|
| `create_feed` | `create_feed` | `flowsheet add-feed` | Add feed with state |
| `create_unit` | `create_unit` | `flowsheet add-unit` | Add unit operation |
| `create_translator` | `create_translator` | `flowsheet add-translator` | Add ASM/ADM translator |
| `connect_ports` | `connect_ports` | `flowsheet connect` | Wire units together |
| `update_unit` | `update_unit` | `flowsheet update-unit` | Modify unit parameters |
| `delete_unit` | `delete_unit` | `flowsheet delete-unit` | Remove unit |
| `validate_flowsheet` | `validate_flowsheet` | `flowsheet validate` | Pre-build validation |

### DOF Management Tools

| Tool | MCP | CLI | Description |
|------|-----|-----|-------------|
| `get_dof_status` | `get_dof_status` | `dof status` | Get DOF count per unit |
| `fix_variable` | `fix_variable` | `dof fix` | Fix variable to value |
| `unfix_variable` | `unfix_variable` | `dof unfix` | Release variable |
| `list_unfixed_vars` | `list_unfixed_vars` | `dof unfixed` | Show unfixed variables |

### Scaling Tools

| Tool | MCP | CLI | Description |
|------|-----|-----|-------------|
| `set_scaling_factor` | `set_scaling_factor` | `scale set` | Set explicit scaling |
| `calculate_scaling_factors` | `calculate_scaling_factors` | `scale calculate` | Run IDAES scaling |
| `report_scaling_issues` | `report_scaling_issues` | `scale report` | Find scaling problems |

### Solver Operations Tools

| Tool | MCP | CLI | Description |
|------|-----|-----|-------------|
| `initialize_unit` | `initialize_unit` | `init unit` | Initialize single unit |
| `initialize_flowsheet` | `initialize_flowsheet` | `init flowsheet` | Sequential initialization |
| `propagate_state` | `propagate_state` | `init propagate` | State between ports |
| `build_and_solve` | `build_and_solve` | `solve` | Full hygiene pipeline |
| `get_solve_status` | `get_solve_status` | `status` | Job status |
| `run_diagnostics` | `run_diagnostics` | `diagnose` | DiagnosticsToolbox |

### Zero-Order Tools

| Tool | MCP | CLI | Description |
|------|-----|-----|-------------|
| `load_zo_parameters` | `load_zo_parameters` | `zo load` | Load from database |
| `list_zo_databases` | `list_zo_databases` | `zo databases` | Available databases |
| `get_zo_unit_parameters` | `get_zo_unit_parameters` | `zo params` | Unit parameters |

### Results Tools

| Tool | MCP | CLI | Description |
|------|-----|-----|-------------|
| `get_results` | `get_results` | `results` | Overall solve results |
| `get_stream_results` | `get_stream_results` | `results streams` | Stream tables |
| `get_unit_results` | `get_unit_results` | `results units` | Unit performance |

## Property Packages

13 supported property packages:

| Package | Components | Use Case |
|---------|------------|----------|
| **SEAWATER** | H2O + TDS | Seawater RO/NF, mass basis |
| **NACL** | H2O + NaCl | Brackish RO, mass basis |
| **NACL_T_DEP** | H2O + NaCl | Temperature-dependent thermal |
| **WATER** | H2O | Pure water, Liq + Vap |
| **MCAS** | Multi-ion | Ion-specific NF, molar basis |
| **ZERO_ORDER** | Database-driven | Simple ZO models |
| **ASM1** | 13 | Activated sludge (basic) |
| **ASM2D** | 19 | Activated sludge with bio-P |
| **ASM3** | - | Activated sludge extended |
| **MODIFIED_ASM2D** | - | Modified ASM2d |
| **ADM1** | 63 | Anaerobic digestion |
| **MODIFIED_ADM1** | - | Modified ADM1 |
| **ADM1_VAPOR** | - | ADM1 vapor phase |

**Warning - Class-Name Collisions:**
- `NaClParameterBlock` exists in both `NaCl_prop_pack.py` AND `NaCl_T_dep_prop_pack.py`
- `WaterParameterBlock` exists in both `water_prop_pack.py` AND `zero_order_properties.py`

Always use the full module path from the registry, not the class name alone.

## Translators

**Only ASM↔ADM translators exist in WaterTAP!** The registry includes 8 translators:

**Core Translators (4):**
| Source | Destination | Translator |
|--------|-------------|------------|
| ASM1 | ADM1 | Translator_ASM1_ADM1 |
| ADM1 | ASM1 | Translator_ADM1_ASM1 |
| ASM2D | ADM1 | Translator_ASM2d_ADM1 |
| ADM1 | ASM2D | Translator_ADM1_ASM2d |

**Modified Model Translators (4):**
| Source | Destination | Translator |
|--------|-------------|------------|
| ModifiedASM2D | ADM1 | Translator_ModifiedASM2d_ADM1 |
| ADM1 | ModifiedASM2D | Translator_ADM1_ModifiedASM2d |
| ASM2D | ModifiedADM1 | Translator_ASM2d_ModifiedADM1 |
| ModifiedADM1 | ASM2D | Translator_ModifiedADM1_ASM2d |

For non-biological flowsheets, use the **same property package** throughout.

## Unit Registry

21+ unit operations available across categories:

- **Membrane:** ReverseOsmosis0D, ReverseOsmosis1D, Nanofiltration0D, NanofiltrationDSPMDE0D
- **Zero-Order:** NanofiltrationZO, UltraFiltrationZO, PumpZO, FeedZO
- **Thermal:** Evaporator, Condenser, Compressor, Crystallization
- **Pumps/ERD:** Pump, EnergyRecoveryDevice, PressureExchanger
- **Utilities:** Feed, Product, Mixer, Separator (from IDAES)
- **Biological:** (Use property package compatibility for ASM/ADM)

```bash
# List all units
python cli.py units list --json-out

# Filter by category
python cli.py units list --category membrane
```

## Solver Hygiene Pipeline

The `build_and_solve` tool runs the full hygiene pipeline:

```
IDLE -> DOF_CHECK -> SCALING -> INITIALIZATION -> PRE_SOLVE_DIAGNOSTICS -> SOLVING
                                                                              |
COMPLETED <-- POST_SOLVE_DIAGNOSTICS <----------------------------------------+
    ^              | (if failed)                                              |
    +------- RELAXED_SOLVE ---------------------------------------------------+
                   | (if still fails)
                FAILED
```

Each stage provides structured output for diagnosis:
- **DOF_CHECK:** Underspecified/overspecified analysis with suggestions
- **SCALING:** Unscaled variables and constraint warnings
- **INITIALIZATION:** Per-unit init status and DOF after init
- **DIAGNOSTICS:** Constraint residuals, bound violations

## Quick Start

### Using CLI

```bash
# Create session
python cli.py session new --property-package SEAWATER --id my_ro

# Add feed and units
python cli.py flowsheet add-feed --session my_ro --flow 100 \
  --tds 35000 --temp 25 --pressure 1.01

python cli.py flowsheet add-unit --session my_ro --type ReverseOsmosis0D \
  --id RO1 --config '{"has_pressure_change": true}'

# Fix DOF
python cli.py dof fix --session my_ro --unit RO1 --var "A_comp[0, H2O]" --value 4.2e-12
python cli.py dof fix --session my_ro --unit RO1 --var "B_comp[0, TDS]" --value 3.5e-8
python cli.py dof fix --session my_ro --unit RO1 --var area --value 50
python cli.py dof fix --session my_ro --unit RO1 --var "permeate.pressure[0]" --value 101325

# Check DOF
python cli.py dof status --session my_ro

# Solve
python cli.py solve --session my_ro --run-full-pipeline
```

### Using MCP

Configure in your MCP client (e.g., Claude Desktop `config.json`):

```json
{
  "mcpServers": {
    "watertap-engine": {
      "command": "python",
      "args": ["/path/to/watertap-engine-mcp/server.py"]
    }
  }
}
```

Then use natural language with the companion skill:
> "Create a seawater RO system treating 100 m3/hr at 35,000 mg/L TDS and solve for 50% recovery"

## Installation

```bash
# Clone repository
git clone https://github.com/puran-water/watertap-engine-mcp.git
cd watertap-engine-mcp

# Create virtual environment
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows

# Install dependencies
pip install -r requirements.txt

# For full WaterTAP support (optional, for solve operations):
pip install watertap idaes-pse pyomo
```

### Dependencies

- Python 3.10+
- WaterTAP 1.0+ (optional, for solve)
- IDAES-PSE 2.0+ (optional, for solve)
- FastMCP (for MCP adapter)
- Typer (for CLI adapter)

## File Structure

```
watertap-engine-mcp/
├── server.py              # MCP Adapter (FastMCP) - 51 tools
├── cli.py                 # CLI Adapter (typer)
├── worker.py              # Background job worker
├── core/
│   ├── property_registry.py    # 13 property packages
│   ├── translator_registry.py  # ASM/ADM translators
│   ├── unit_registry.py        # Unit specs with DOF, scaling
│   ├── water_state.py          # Feed state abstraction
│   └── session.py              # Session management
├── solver/
│   ├── pipeline.py             # Hygiene pipeline state machine
│   ├── dof_resolver.py         # DOF analysis
│   ├── scaler.py               # Scaling tools
│   ├── initializer.py          # Sequential initialization
│   ├── diagnostics.py          # DiagnosticsToolbox wrapper
│   └── recovery.py             # Failure recovery
├── utils/
│   ├── model_builder.py        # Session -> Pyomo model
│   ├── auto_translator.py      # Translator insertion
│   ├── job_manager.py          # Background jobs
│   ├── state_translator.py     # Feed state conversion
│   └── topo_sort.py            # Topological sort
├── templates/                  # Pre-built flowsheet templates
│   ├── ro_train.py
│   ├── nf_softening.py
│   └── mvc_crystallizer.py
├── jobs/                       # Session/job persistence (runtime)
└── tests/                      # 206 unit tests
```

## Companion Skill

The `watertap-flowsheet-builder` skill (at `~/skills/watertap-skill/`) provides domain intelligence:

- DOF suggestions with typical values for each unit type
- Property package selection guidance
- Translator selection for biological chains
- Failure diagnosis patterns
- Workflow orchestration for common flowsheets

The skill orchestrates the atomic server tools - server provides explicit operations, skill provides intelligence.

## Testing

```bash
# Run all tests (206)
pytest tests/ -v

# Skip slow tests
pytest tests/ -v -m "not slow"
```

## License

MIT

## Acknowledgments

Built on [WaterTAP](https://github.com/watertap-org/watertap) by the National Alliance for Water Innovation (NAWI).
