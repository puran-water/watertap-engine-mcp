# WaterTAP-Engine-MCP Server Implementation Plan

**STATUS: COMPLETED** - Server fully implemented with 57 tools.

---

## Overview

Build a WaterTAP-engine-MCP server analogous to qsdsan-engine-mcp, exposing the full WaterTAP unit library with explicit solver operations. Domain intelligence (DOF suggestions, translator selection, failure diagnosis) resides in a **companion agent skill**, not the server.

**Key Design Principles:**
- Dual adapter pattern (FastMCP + typer CLI)
- **Explicit operations** - Server exposes atomic tools, NO hidden automation
- **Companion skill** - Domain expertise + workflow orchestration (suggests, user approves)
- Orchestrate WaterTAP/IDAES utilities (`check_dof`, `calculate_scaling_factors`, `DiagnosticsToolbox`) rather than replace them
- Simplified feed state interface for MCP clients

---

## Architecture: MCP Server + Companion Skill

### Why This Separation?

**Codex Review Findings (High Risk):**
1. Auto-DOF fixing masks modeling errors
2. Auto-translator insertion assumes translators that don't exist
3. Global scaling transforms risk double-scaling

**Solution:** Move "intelligence" to a companion skill where Claude explains reasoning and user approves.

| Component | Responsibility | Example |
|-----------|---------------|---------|
| **MCP Server** | Atomic, explicit operations | `fix_variable("RO.A_comp", 4.2e-12)` |
| **Companion Skill** | Domain expertise, suggestions | "RO0D typically needs A_comp=3-5×10⁻¹² for SWRO. Fix it?" |

### MCP Server (watertap-engine-mcp)

**Principle:** Comprehensive API coverage with explicit operations. NO hidden automation.

| Category | Tools |
|----------|-------|
| Session | `create_session`, `get_session`, `list_sessions`, `delete_session` |
| Registry | `list_units`, `list_property_packages`, `list_translators`, `get_unit_spec` |
| Build | `create_feed`, `create_unit`, `create_translator`, `connect_ports` |
| DOF | `get_dof_status`, `fix_variable`, `unfix_variable`, `list_unfixed_vars` |
| Scaling | `get_scaling_status`, `set_scaling_factor`, `calculate_scaling_factors`, `report_scaling_issues` |
| Init | `initialize_unit`, `initialize_flowsheet`, `propagate_state`, `check_solve` |
| Solve | `solve`, `get_solve_status`, `get_results` |
| Diagnostics | `run_diagnostics`, `get_constraint_residuals`, `get_bound_violations` |

### Companion Skill (watertap-flowsheet-builder)

**Principle:** Domain expertise encoded as guidance. Suggests, user approves.

```
watertap-flowsheet-builder/
├── SKILL.md                    # Core workflow + triggering
└── references/
    ├── property_packages.md    # Package selection guidance
    ├── dof_requirements.md     # Unit DOF specs + typical values
    ├── translators.md          # Translator selection rules
    ├── scaling_best_practices.md
    ├── common_failures.md      # Diagnosis patterns
    └── flowsheet_patterns/
        ├── ro_desalination.md
        ├── nf_softening.md
        └── mvc_crystallizer.md
```

**Skill Responsibilities:**
1. **Process Selection** - "For seawater RO, use SeawaterParameterBlock"
2. **DOF Recommendations** - "RO0D needs A_comp, B_comp, area, permeate.pressure. Typical A_comp for SWRO: 3-5×10⁻¹² m/s/Pa. Want me to fix these?"
3. **Translator Selection** - "ZO → RO requires Translator_ZO_Seawater. Create it?"
4. **Failure Diagnosis** - "Infeasible: Feed pressure (5 bar) < osmotic pressure (25 bar). Increase to 30+ bar?"
5. **Workflow Orchestration** - Multi-step procedures for common flowsheets

---

## Directory Structure

```
watertap-engine-mcp/
├── server.py                      # MCP Adapter (FastMCP)
├── cli.py                         # CLI Adapter (typer)
├── pyproject.toml
├── README.md
├── CLAUDE.md
│
├── core/
│   ├── __init__.py
│   ├── water_state.py             # WaterTAPState dataclass
│   ├── property_registry.py       # PropertyPackageSpec + compatibility
│   ├── unit_registry.py           # Full WaterTAP unit specs
│   ├── translator_registry.py     # Translator specs + auto-insertion
│   └── session.py                 # SessionConfig dataclass
│
├── solver/
│   ├── __init__.py
│   ├── pipeline.py                # HygienePipeline state machine
│   ├── dof_resolver.py            # Auto-DOF resolution
│   ├── scaler.py                  # Auto-scaling application
│   ├── initializer.py             # Sequential initialization
│   ├── diagnostics.py             # Pre/post-solve diagnostics
│   └── recovery.py                # Failure recovery strategies
│
├── utils/
│   ├── __init__.py
│   ├── job_manager.py             # Background job execution
│   ├── auto_translator.py         # Translator auto-insertion
│   ├── state_translator.py        # Feed state → property pkg
│   └── topo_sort.py               # Topological sort
│
├── templates/                     # Pre-built flowsheet templates
│   ├── __init__.py
│   ├── ro_train.py
│   ├── nf_softening.py
│   └── mvc_crystallizer.py
│
├── jobs/flowsheets/               # Session storage (runtime)
└── tests/
```

---

## Phase 1: Core Infrastructure

### 1.1 Property Package Registry (Expanded per Codex Review)

```python
# core/property_registry.py
class PropertyPackageType(Enum):
    # Desalination packages
    SEAWATER = "SeawaterParameterBlock"      # H2O + TDS, Liq, mass basis
    NACL = "NaClParameterBlock"              # H2O + NaCl, Liq, mass basis
    NACL_T_DEP = "NaCl_T_dep_ParameterBlock" # Temperature-dependent NaCl
    WATER = "WaterParameterBlock"            # Pure H2O, Liq + Vap
    MCAS = "MCASParameterBlock"              # Multi-component ions, molar basis

    # Zero-order (NOTE: in watertap/core/, not property_models/)
    ZERO_ORDER = "WaterParameterBlock"       # watertap.core.zero_order_properties
    # Requires: database, water_source, solute_list config

    # Biological treatment
    ASM1 = "ASM1ParameterBlock"              # Activated Sludge Model 1
    ASM2D = "ASM2dParameterBlock"            # Activated Sludge Model 2d
    ASM3 = "ASM3ParameterBlock"              # Activated Sludge Model 3 (NEW)
    MODIFIED_ASM2D = "ModifiedASM2dParameterBlock"
    ADM1 = "ADM1ParameterBlock"              # Anaerobic Digestion Model 1
    MODIFIED_ADM1 = "ModifiedADM1ParameterBlock"  # (NEW)
    ADM1_VAPOR = "ADM1PropertiesVapor"       # For vapor phase in AD (NEW)

@dataclass
class PropertyPackageSpec:
    pkg_type: PropertyPackageType
    watertap_class: str
    module_path: str                        # CRITICAL: Full module path to avoid collisions!
    phases: Set[str]
    required_components: List[str]
    optional_components: List[str]          # User-configurable solutes
    state_vars: List[str]
    flow_basis: str                         # "mass", "molar", "volumetric"
    requires_reaction_package: bool
    compatible_reaction_packages: List[str]
    charge_balance_required: bool           # For MCAS/ASM/ADM
    default_scaling: Dict[str, float]
```

**⚠️ CLASS-NAME COLLISION WARNING (per Codex):**
- `NaClParameterBlock` exists in BOTH `NaCl_prop_pack.py` AND `NaCl_T_dep_prop_pack.py`
- `WaterParameterBlock` exists in BOTH `water_prop_pack.py` AND `zero_order_properties.py`
- **MUST select by full module path, not class name alone!**

**Compatibility Matrix** (CORRECTED per Final Codex DeepWiki Review):

**Translators that ACTUALLY EXIST in WaterTAP:**
| Source | Destination | Translator | Exists? |
|--------|-------------|------------|---------|
| ASM1 | ADM1 | `Translator_ASM1_ADM1` | ✓ YES |
| ADM1 | ASM1 | `Translator_ADM1_ASM1` | ✓ YES |
| ASM2D | ADM1 | `Translator_ASM2d_ADM1` | ✓ YES |
| ADM1 | ASM2D | `Translator_ADM1_ASM2d` | ✓ YES |

**Translators that DO NOT EXIST (previous plan was WRONG):**
| Source | Destination | Status |
|--------|-------------|--------|
| ZERO_ORDER | SEAWATER | ❌ Does NOT exist |
| SEAWATER | WATER | ❌ Does NOT exist |
| MCAS | SEAWATER | ❌ Does NOT exist |
| SEAWATER | NACL | ❌ Does NOT exist |

**⚠️ CRITICAL:** For non-biological flowsheets, users must use the SAME property package throughout OR manually handle stream compatibility. The MCP server should:
1. Warn when connecting units with different (non-ASM/ADM) property packages
2. NOT auto-insert translators that don't exist
3. Let the companion skill guide users to use compatible packages

### 1.2 Unit Registry with Hygiene Configs

```python
# core/unit_registry.py
@dataclass
class UnitSpec:
    unit_type: str                         # "ReverseOsmosis0D"
    watertap_class: str                    # Full import path
    category: UnitCategory                 # RO, NF, EVAP, etc.
    required_property_packages: List[PropertyPackageType]

    # DOF Management
    required_fixes: List[VariableSpec]     # Must fix for DOF=0
    typical_values: Dict[str, float]       # Auto-fix defaults

    # Scaling
    default_scaling: Dict[str, float]      # Variable scaling factors

    # Initialization
    init_hints: InitHints                  # state_args, solver opts

    # Ports
    n_inlets: int
    n_outlets: int
    outlet_types: List[str]                # ["permeate", "retentate"]
```

**Unit Coverage** (initial release - CORRECTED per Codex):
| Category | Units | Import Path |
|----------|-------|-------------|
| RO | ReverseOsmosis0D, ReverseOsmosis1D | `watertap.unit_models` |
| NF | Nanofiltration0D, NanofiltrationDSPMDE0D | `watertap.unit_models` |
| NF-ZO | NanofiltrationZO | `watertap.unit_models.zero_order` |
| UF-ZO | UltraFiltrationZO | `watertap.unit_models.zero_order` |
| Evap | Evaporator, Condenser, Compressor | `watertap.unit_models.mvc.components` |
| Cryst | Crystallization | `watertap.unit_models` |
| Pumps | Pump, EnergyRecoveryDevice | `watertap.unit_models` (pressure_changer) |
| Pumps-ZO | PumpZO | `watertap.unit_models.zero_order` |
| ERD | PressureExchanger | `watertap.unit_models` |
| Mix/Split | Mixer, Separator | `idaes.models.unit_models` (IDAES, not WaterTAP!) |
| Feed | Feed, Product | `idaes.models.unit_models` (IDAES, not WaterTAP!) |
| Feed-ZO | FeedZO | `watertap.unit_models.zero_order` |

**⚠️ Note:** ROZO does NOT exist in WaterTAP. Zero-order only has NF/UF membranes.

### 1.3 Feed State Abstraction (EXPANDED per Codex)

**⚠️ Codex Warning:** A single mg/L "components" dict is too narrow for MCAS/ASM/ADM which need:
- Solute lists with charges (for electroneutrality)
- Molar vs mass basis specification
- Temperature-dependent parameters

```python
# core/water_state.py
@dataclass
class WaterTAPState:
    # Basic flow properties
    flow_vol_m3_hr: float
    temperature_C: float = 25.0
    pressure_bar: float = 1.0

    # Component specification (flexible)
    components: Dict[str, float] = field(default_factory=dict)
    concentration_units: str = "mg/L"        # "mg/L", "mol/L", "kg/m3"
    concentration_basis: str = "mass"        # "mass" or "molar"

    # For charged species (MCAS, ASM, ADM)
    component_charges: Dict[str, int] = None  # e.g., {"Na": 1, "Cl": -1}
    electroneutrality_species: str = None     # Species to adjust for balance

    # Target property package hint
    target_property_package: str = None

    def to_state_args(self, pkg: PropertyPackageType) -> Dict:
        """Convert to WaterTAP property package state_args."""
        if pkg == PropertyPackageType.MCAS:
            return self._to_mcas_state_args()
        elif pkg in (PropertyPackageType.ASM1, PropertyPackageType.ASM2D):
            return self._to_asm_state_args()
        elif pkg == PropertyPackageType.SEAWATER:
            return self._to_seawater_state_args()
        # ... other packages

    def _to_mcas_state_args(self) -> Dict:
        """Convert to MCAS format (molar, with charges)."""
        # Requires component_charges for electroneutrality
        if not self.component_charges:
            raise ValueError("MCAS requires component_charges for electroneutrality")
        # Convert mass conc → molar, check charge balance
        ...
```

**Skill Guidance (references/property_packages.md):**
- For seawater RO: Use SEAWATER with TDS, mass basis
- For ion-specific NF: Use MCAS with explicit ions and charges
- For biological: Use ASM2d/ADM1 with reaction packages
- For crystallization: Use NACL with temperature-dependent solubility

---

## Phase 2: Solver Hygiene Pipeline

### State Machine Flow

```
IDLE → DOF_CHECK → SCALING → INITIALIZATION → PRE_SOLVE_DIAGNOSTICS → SOLVING
                                                                         │
COMPLETED ←── POST_SOLVE_DIAGNOSTICS ←────────────────────────────────────┤
    ↑              │ (if failed)                                          │
    └───── RELAXED_SOLVE ─────────────────────────────────────────────────┘
                   │ (if still fails)
                FAILED
```

### 2.1 DOF Resolution

```python
# solver/dof_resolver.py
class DOFResolver:
    def resolve_all(self) -> Dict:
        for unit_id, unit in units:
            dof = degrees_of_freedom(unit)
            if dof > 0:
                # Auto-fix from registry typical_values
                for var_name, value in spec.typical_values.items():
                    if dof <= 0: break
                    var = get_variable(unit, var_name)
                    if not var.fixed:
                        var.fix(value)
                        dof = degrees_of_freedom(unit)
```

**Unit DOF Requirements:**
| Unit | Required Fixes | Typical Values |
|------|---------------|----------------|
| Pump | efficiency_pump, outlet.pressure | 0.80, - |
| RO0D | A_comp, B_comp, area, permeate.pressure | 4.2e-12, 3.5e-8, 50, 101325 |
| Evaporator | outlet_brine.T, U, area | -, 1000, 100 |
| Crystallizer | temp_operating, crystal_growth_rate | -, 5e-9 |

### 2.2 Scaling (REVISED per Codex - Align with WaterTAP Best Practices)

**⚠️ Codex Warning:** Global `constraint_scaling_transform(model)` risks double-scaling and ignores unit-specific constraint transforms.

**Correct Approach:**
1. Set case-specific scaling for extensive variables (flow, area, work)
2. Call `iscale.calculate_scaling_factors(model)` - this recursively calls unit/property methods
3. Call `iscale.report_scaling_issues(model)` to identify problems
4. Optionally use `iscale.constraint_autoscale_large_jac(model)` for remaining issues
5. **Do NOT call global `constraint_scaling_transform()`** - units handle their own constraint scaling

```python
# solver/scaler.py - MCP Server provides EXPLICIT tools, NOT auto-scaling
class ScalingTools:
    def set_scaling_factor(self, var_path: str, factor: float):
        """Explicit: Set scaling factor for a specific variable."""
        var = get_variable(model, var_path)
        iscale.set_scaling_factor(var, factor)

    def calculate_scaling_factors(self):
        """Orchestrate WaterTAP/IDAES scaling (calls unit methods internally)."""
        iscale.calculate_scaling_factors(model)

    def report_scaling_issues(self) -> Dict:
        """Return unscaled/badly-scaled vars and constraints."""
        return iscale.report_scaling_issues(model)

    def autoscale_large_jac(self):
        """Optional: Auto-scale based on Jacobian analysis."""
        iscale.constraint_autoscale_large_jac(model)
```

**Skill Guidance (references/scaling_best_practices.md):**
| Variable Pattern | Recommended Factor | When to Apply |
|-----------------|-------------------|---------------|
| A_comp | 1e12 | RO/NF membrane permeability |
| B_comp | 1e8 | RO/NF salt permeability |
| pressure | 1e-5 | All pressure variables |
| flow_mass | Case-specific | Based on actual flow magnitude |
| temperature | 1e-2 | All temperature variables |
| area | 1e-2 | Membrane/heat exchanger area |

**Note:** Flow scaling is case-specific - skill should suggest based on feed flow rate.

### 2.3 Sequential Initialization (REVISED per Codex)

**⚠️ Codex Warning:** Simple `propagate_state` won't handle:
- Mixers/splitters (multi-inlet/outlet)
- Multi-CV units (RO1D, NF DSPMDE, ED)
- Multi-phase units (Evaporator, Crystallizer)
- Biological models with reaction packages

**Correct Approach:**
1. Use WaterTAP/IDAES `SequentialDecomposition` with explicit tear stream selection
2. Call unit-specific `initialize()` or `initialize_build()` methods
3. Use `check_dof()` and `check_solve()` after each unit
4. Handle unit-specific state_args requirements

```python
# solver/initializer.py - MCP Server provides EXPLICIT tools
class InitializationTools:
    def get_initialization_order(self, tear_streams: List[str] = None) -> List[str]:
        """Return topological order with specified tear streams."""
        seq = SequentialDecomposition()
        seq.tear_set = tear_streams or []
        return seq.get_computation_order(model.fs)

    def propagate_state(self, source_port: str, dest_port: str):
        """Explicit state propagation between two ports."""
        src = get_port(model, source_port)
        dst = get_port(model, dest_port)
        propagate_state(arc=(src, dst))

    def initialize_unit(self, unit_id: str, state_args: Dict = None,
                        solver_options: Dict = None) -> Dict:
        """Initialize a single unit with explicit args."""
        unit = getattr(model.fs, unit_id)
        # Use unit's own initialize method
        if hasattr(unit, 'initialize_build'):
            unit.initialize_build(state_args=state_args, **solver_options or {})
        elif hasattr(unit, 'initialize'):
            unit.initialize(state_args=state_args)
        # Return status
        return {
            "unit_id": unit_id,
            "dof": degrees_of_freedom(unit),
            "solve_status": check_solve(unit),
        }
```

**Unit-Specific Initialization Notes (Skill Guidance):**
| Unit Type | Initialization Method | Special Requirements |
|-----------|----------------------|---------------------|
| RO0D/RO1D | `initialize_build()` | Needs `state_args` with flow_mass_phase_comp |
| Evaporator | `initialize()` | Separate feed/vapor property packages |
| Crystallizer | `initialize_build()` | Interval arithmetic pre-solve |
| Mixer | `initialize()` | Must init after ALL inlets connected |
| ED/NF DSPMDE | `initialize_build()` | Multi-CV, needs outlet guesses |
| ASM/ADM reactors | `initialize()` | Reaction package must be configured |

### 2.4 Diagnostics & Failure Explanation

```python
# solver/diagnostics.py
@dataclass
class FailureExplanation:
    summary: str                    # Natural language summary
    likely_causes: List[str]
    suggested_fixes: List[str]
    related_variables: List[str]
    related_constraints: List[str]

def generate_failure_explanation(outcome, residuals, violations):
    if outcome == INFEASIBLE:
        if "flux_mass" in top_residual.name:
            return FailureExplanation(
                summary="RO cannot achieve permeate flow - insufficient pressure",
                likely_causes=["Feed pressure below osmotic pressure"],
                suggested_fixes=["Increase feed pressure to 30+ bar"]
            )
```

---

## Phase 3: Auto-Translator Subsystem

### Connection Algorithm

```python
# utils/auto_translator.py
def connect_units(source, source_port, dest, dest_port, flowsheet_state):
    # 1. Detect property packages
    source_pkg = detect_package(source, source_port)
    dest_pkg = detect_package(dest, dest_port)

    # 2. Check compatibility
    if source_pkg == dest_pkg:
        return direct_connection(source, dest)

    # 3. Get translator chain
    chain = translator_registry.get_chain(source_pkg, dest_pkg)
    if not chain:
        return error_incompatible(source_pkg, dest_pkg)

    # 4. Insert translators
    arcs = []
    prev = (source, source_port)
    for spec in chain:
        translator = create_translator(spec)
        arcs.append(Arc(prev → translator.inlet))
        prev = (translator, "outlet")
    arcs.append(Arc(prev → (dest, dest_port)))

    return ConnectionResult(arcs, translators_created)
```

### Translator Registry (CORRECTED per Codex DeepWiki Review)

**⚠️ CRITICAL CORRECTION:** Only ASM↔ADM translators exist in WaterTAP. No ZO→Seawater, Seawater→Water, or MCAS→Seawater translators exist!

| Source | Destination | Translator | File Path |
|--------|-------------|------------|-----------|
| ASM1 | ADM1 | Translator_ASM1_ADM1 | `watertap/unit_models/translators/translator_asm1_adm1.py` |
| ADM1 | ASM1 | Translator_ADM1_ASM1 | `watertap/unit_models/translators/translator_adm1_asm1.py` |
| ASM2D | ADM1 | Translator_ASM2d_ADM1 | `watertap/unit_models/translators/translator_asm2d_adm1.py` |
| ADM1 | ASM2D | Translator_ADM1_ASM2d | `watertap/unit_models/translators/translator_adm1_asm2d.py` |

**For other property package combinations:**
- Same property package → Direct connection (no translator)
- Different packages without translator → **User must ensure stream compatibility manually** (IDAES #616 acknowledges this gap)
- Skill should warn user and suggest workarounds when mixing incompatible packages

---

## Phase 4: MCP Tool Surface

### Session & Discovery (6 tools)
1. `create_watertap_session` - New session with property package
2. `list_units` - Filter by category/property package
3. `list_property_packages` - Compatibility info
4. `get_unit_requirements` - DOF specs, scaling, init hints
5. `get_session` - Session details
6. `list_sessions` - All sessions

### Flowsheet Construction (7 tools)
7. `create_feed` - Simplified feed state input
8. `create_unit` - Add unit with auto-DOF
9. `connect_units` - Auto-translator insertion
10. `update_unit` - Modify parameters
11. `delete_unit` - Remove unit + connections
12. `validate_flowsheet` - Pre-build validation
13. `get_flowsheet_diagram` - Visual representation

### Solver Hygiene (6 tools)
14. `check_dof` - DOF analysis with suggestions
15. `apply_scaling` - Manual scaling override
16. `initialize_flowsheet` - Sequential init
17. `run_diagnostics` - Pre-solve checks
18. `build_and_solve` - Full hygiene pipeline (background job)
19. `diagnose_failure` - Post-failure analysis

### Results (5 tools)
20. `get_job_status` - Background job polling
21. `get_job_results` - Solver results
22. `get_stream_results` - Stream property tables
23. `get_unit_results` - Unit performance metrics
24. `get_costing` - LCOW, CapEx, OpEx (via WaterTAPCostingBlockData.add_LCOW)

### Zero-Order Specific (3 tools - NEW per Codex)
25. `load_zo_parameters` - Load params from WaterTAP database via `load_parameters_from_database()`
26. `list_zo_databases` - List available water source databases
27. `get_zo_unit_parameters` - Get database parameters for a ZO unit type

---

## Critical Files for Implementation

1. **`/mnt/c/Users/hvksh/mcp-servers/qsdsan-engine-mcp/server.py`** (1861 lines)
   - FastMCP tool patterns, background job integration, error handling

2. **`/mnt/c/Users/hvksh/mcp-servers/qsdsan-engine-mcp/utils/flowsheet_session.py`** (920 lines)
   - Session persistence pattern, directly reusable with modifications

3. **`/mnt/c/Users/hvksh/mcp-servers/qsdsan-engine-mcp/utils/job_manager.py`** (500 lines)
   - Background subprocess execution, crash recovery

4. **`/mnt/c/Users/hvksh/mcp-servers/qsdsan-engine-mcp/core/unit_registry.py`** (600 lines)
   - Registry pattern to extend with WaterTAP-specific metadata

5. **WaterTAP patterns** (from DeepWiki + GitHub analysis):
   - `watertap.unit_models.*` - DOF patterns per unit
   - `watertap.property_models.*` - Property package interfaces
   - `watertap.core.util.initialization` - propagate_state, check_dof
   - `idaes.core.util.scaling` - calculate_scaling_factors
   - `watertap.costing.watertap_costing_package` - WaterTAPCostingBlockData, add_LCOW
   - `watertap.costing.zero_order_costing` - ZeroOrderCosting
   - `watertap.core.zero_order_base` - load_parameters_from_database

---

## Final Codex Review Findings (DeepWiki + GitHub)

### VALIDATED ✓
- Property packages confirmed: `SeawaterParameterBlock`, `NaClParameterBlock`, `WaterParameterBlock`, `MCASParameterBlock`, ASM1/ASM2d/ModifiedASM2d, ADM1
- ASM↔ADM translators exist: `translator_adm1_asm1.py`, `translator_asm1_adm1.py`, `translator_adm1_asm2d.py`, `translator_asm2d_adm1.py`
- RO initialization: `ReverseOsmosisBaseData.initialize_build()` in `watertap/unit_models/reverse_osmosis_base.py`
- Unit classes correct: `ReverseOsmosis0D`, `ReverseOsmosis1D`, `Nanofiltration0D`, `NanofiltrationDSPMDE0D`, `Pump`, `EnergyRecoveryDevice`, `PressureExchanger`, `Crystallization`, `Evaporator`, `Compressor`, `Condenser`
- Costing + LCOW: `WaterTAPCostingBlockData.add_LCOW()` in `watertap/costing/watertap_costing_package.py`

### CORRECTIONS NEEDED ⚠️
1. **Translator list WRONG**: No ZO→Seawater, Seawater→Water, or MCAS→Seawater translators exist. Only ASM/ADM translators in `watertap/unit_models/translators/`
2. **ZERO_ORDER location**: Not in `property_models/` - it's `watertap/core/zero_order_properties.py` (class `WaterParameterBlock`)
3. **ROZO does not exist**: No `reverse_osmosis_zo.py` in WaterTAP zero-order units
4. **NFZO/UFZO names wrong**: Should be `NanofiltrationZO` and `UltraFiltrationZO`
5. **Feed/Product/Mixer/Separator are IDAES units**: Import from `idaes.models.unit_models.*` not WaterTAP

### ADDITIONS RECOMMENDED
1. **Additional property packages**: ASM3, ModifiedADM1, ADM1_vapor, NDMA, Coagulation, Cryst
2. **Zero-order database loading**: `ZeroOrderBaseData.load_parameters_from_database()`
3. **Property package config hooks**: MCAS needs `solute_list`, `charge`, `mw_data`; ZO needs `database`, `water_source`
4. **Costing tools**: `cost_process()`, unit-specific costing in `watertap/costing/unit_models/`

### WARNINGS ⚠️
1. **Class-name collisions**: `NaClParameterBlock` defined in both `NaCl_prop_pack.py` AND `NaCl_T_dep_prop_pack.py`. Must select by module path!
2. **Scaling API evolving**: IDAES scaling-v2 work (issues #1565, #1648, #1624) - avoid hard-wiring
3. **Init/state pain points**: WaterTAP #1481, #1123; IDAES #1622 - extra care needed
4. **MCAS changes pending**: WaterTAP #993 (mass flow state variable) affects DOF expectations
5. **Translator coverage gaps**: IDAES #616 - manual construction still required

---

## Verification Plan (EXPANDED per Codex Review)

### Unit Tests - Core Registry
- [ ] Property package compatibility matrix (all 11 packages)
- [ ] Translator registry completeness (including ASM/ADM translators)
- [ ] **SEAWATER→NACL requires translator** (not direct)
- [ ] Unit spec validation for each supported unit type
- [ ] Session CRUD operations

### Unit Tests - DOF Management
- [ ] `get_dof_status` returns correct count per unit
- [ ] `fix_variable` / `unfix_variable` explicit operations
- [ ] **Overspecified DOF detection** (DOF < 0)
- [ ] **Underspecified DOF detection** (DOF > 0)
- [ ] Indexed variable handling (A_comp[H2O], B_comp[NaCl])

### Unit Tests - Scaling (NEW per Codex)
- [ ] `set_scaling_factor` explicit application
- [ ] `calculate_scaling_factors` calls unit methods correctly
- [ ] `report_scaling_issues` identifies unscaled vars/constraints
- [ ] **No double-scaling** (verify unit constraint transforms not duplicated)
- [ ] `autoscale_large_jac` optional application

### Unit Tests - Initialization (NEW per Codex)
- [ ] **Multi-inlet units** (Mixer initialization after all inlets)
- [ ] **Multi-outlet units** (Splitter/Separator)
- [ ] **Multi-CV units** (RO1D, ED, NF DSPMDE discretization)
- [ ] **Multi-phase units** (Evaporator, Crystallizer vapor handling)
- [ ] Reaction package integration (ASM/ADM units)

### Integration Tests
- [ ] ZO Feed → RO → Evaporator chain (multi-translator)
- [ ] RO train with ERD (recycle handling with tear streams)
- [ ] MVC + Crystallizer (multi-domain, multi-phase)
- [ ] **ASM → AD → ASM biological chain** (NEW - biological translators)
- [ ] **Property package configuration errors** (missing solutes/charges)
- [ ] **Unsupported translator scenario** (graceful error)

### Integration Tests - Solver Failure Recovery (NEW per Codex)
- [ ] Bad scaling → `report_scaling_issues` identifies
- [ ] Infeasible bounds → `get_bound_violations` reports
- [ ] Ill-conditioned Jacobian → `DiagnosticsToolbox` integration
- [ ] Max iterations → diagnostics with constraint residuals

### Concurrency/Persistence Tests (NEW per Codex)
- [ ] Session persistence across server restart
- [ ] Concurrent session access
- [ ] Background job manager crash recovery
- [ ] Job status polling under load

### End-to-End Tests
- [ ] MCP client builds RO desalination flowsheet
- [ ] CLI builds and solves NF softening system
- [ ] Failure diagnosis for common scenarios:
  - Insufficient feed pressure for RO
  - Unachievable crystallization yield
  - Poor scaling causing max iterations
  - **Missing reaction package for biological unit**
  - **Translator not found for package pair**

### Companion Skill Tests
- [ ] DOF suggestion accuracy for each unit type
- [ ] Translator selection correctness
- [ ] Failure diagnosis pattern matching
- [ ] Workflow orchestration (RO train template)

### Manual Verification
```bash
# CLI smoke test
python cli.py create-session --property-package SEAWATER
python cli.py create-feed --flow 100 --tds 35000 --temp 25
python cli.py create-unit RO --id RO1 --area 50 --pressure 60
python cli.py connect-units Feed RO1
python cli.py get-dof-status  # Should show DOF > 0
python cli.py fix-variable RO1.A_comp 4.2e-12
python cli.py fix-variable RO1.B_comp 3.5e-8
python cli.py fix-variable RO1.permeate.pressure 101325
python cli.py get-dof-status  # Should show DOF = 0
python cli.py calculate-scaling-factors
python cli.py report-scaling-issues
python cli.py initialize-flowsheet
python cli.py solve --session-id <id>
python cli.py get-results --session-id <id>
```

---

## Implementation Risks (Codex Assessment)

| Risk | Severity | Mitigation |
|------|----------|------------|
| Incorrect DOF defaults mask modeling errors | HIGH | Move DOF intelligence to skill (suggests, user approves) |
| Translator auto-insertion for non-existent translators | HIGH | Explicit translator creation tool; skill suggests, server validates |
| Property/reaction package configuration complexity | HIGH | Expanded property registry with validation; skill guides selection |
| Unit-specific init/scaling differences | MEDIUM | Per-unit metadata in registry; skill references init requirements |
| Double-scaling from global transforms | MEDIUM | No global `constraint_scaling_transform`; use `report_scaling_issues` |
| Multi-CV/multi-phase unit initialization | MEDIUM | Unit-specific init tools with state_args; skill orchestrates |

---

## Implementation Order (REVISED)

### Phase 1: Core Infrastructure (Server)
1. Property package registry (11 packages + reaction package support)
2. Translator registry (explicit, including ASM/ADM translators)
3. Unit registry with metadata (DOF specs, scaling hints, init requirements)
4. Session management (adapt from qsdsan-engine-mcp)
5. Background job manager

### Phase 2: Explicit Tool Surface (Server)
6. Session CRUD tools
7. Registry query tools (`list_units`, `list_property_packages`, `list_translators`)
8. Build tools (`create_feed`, `create_unit`, `create_translator`, `connect_ports`)
9. DOF tools (`get_dof_status`, `fix_variable`, `unfix_variable`)
10. Scaling tools (`set_scaling_factor`, `calculate_scaling_factors`, `report_scaling_issues`)

### Phase 3: Solver Operations (Server)
11. Initialization tools (`initialize_unit`, `propagate_state`, `check_solve`)
12. Solve tools (`solve`, `get_solve_status`)
13. Diagnostics tools (`run_diagnostics`, `get_constraint_residuals`, `DiagnosticsToolbox` integration)
14. Results tools (`get_stream_results`, `get_unit_results`, `get_costing`)

### Phase 4: Companion Skill
15. SKILL.md with core workflow and triggering
16. references/property_packages.md - Package selection guidance
17. references/dof_requirements.md - Unit DOF specs + typical values
18. references/translators.md - Translator selection rules
19. references/scaling_best_practices.md
20. references/common_failures.md - Diagnosis patterns
21. references/flowsheet_patterns/ - RO, NF, MVC templates

### Phase 5: Testing & Polish
22. Unit tests (registry, DOF, scaling, init)
23. Integration tests (translator chains, failure recovery)
24. E2E tests (full workflows via MCP + skill)
25. Documentation

---

## Companion Skill Design: watertap-flowsheet-builder

### SKILL.md Frontmatter

```yaml
name: watertap-flowsheet-builder
description: |
  Guide for building and solving WaterTAP flowsheets. Use when user wants to:
  (1) Design a water treatment flowsheet (RO, NF, UF, evaporators, crystallizers, biological treatment)
  (2) Troubleshoot solver failures (infeasibility, poor scaling, initialization issues)
  (3) Select property packages, translators, or unit configurations
  Requires watertap-engine-mcp server to be running.
```

### Core Workflow (SKILL.md Body)

```markdown
## Workflow

1. **Session Creation**
   - Determine primary treatment type (desalination, biological, hybrid)
   - Select default property package
   - Call `create_session`

2. **Feed Specification**
   - Guide user through feed characterization
   - For MCAS: collect ion charges for electroneutrality
   - Call `create_feed` with complete state

3. **Unit Addition** (iterative)
   - For each unit:
     a. Check DOF requirements (load references/dof_requirements.md)
     b. Suggest typical values, explain reasoning
     c. Wait for user approval
     d. Call `create_unit` and `fix_variable` for each DOF

4. **Connection**
   - Check property package compatibility
   - If translator needed, explain and get approval
   - Call `create_translator` if needed, then `connect_ports`

5. **Scaling**
   - Call `calculate_scaling_factors`
   - Call `report_scaling_issues`
   - If issues found, suggest scaling factors (load references/scaling_best_practices.md)

6. **Initialization**
   - Get init order from `get_initialization_order`
   - For each unit, call `initialize_unit` with appropriate state_args
   - Check `get_dof_status` and `check_solve` after each

7. **Solve**
   - Call `solve`
   - If failure, call `run_diagnostics` and `get_constraint_residuals`
   - Load references/common_failures.md for diagnosis patterns
   - Suggest fixes, get approval, retry

8. **Results**
   - Present stream tables, key metrics, costing
```

### Reference Files

| File | Content |
|------|---------|
| `references/dof_requirements.md` | Per-unit DOF specs with typical values and ranges |
| `references/translators.md` | Compatibility matrix and translator selection rules |
| `references/scaling_best_practices.md` | Variable-specific scaling factors |
| `references/common_failures.md` | Failure patterns → diagnosis → suggested fixes |
| `references/flowsheet_patterns/ro_desalination.md` | RO train template with ERD |
| `references/flowsheet_patterns/nf_softening.md` | NF softening template |
| `references/flowsheet_patterns/mvc_crystallizer.md` | MVC + crystallizer template |
