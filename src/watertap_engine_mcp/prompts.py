"""MCP prompts for the WaterTAP Engine.

Prompts are parameterized instruction templates that guide agents through
common WaterTAP workflows. Each returns a plain ``str``.
"""

from __future__ import annotations

from fastmcp import FastMCP


def register_prompts(mcp: FastMCP) -> None:
    """Register all MCP prompts onto the server instance."""

    # === P1: build_flowsheet ===
    @mcp.prompt()
    def build_flowsheet(
        topology: str = "ro_train",
        flow_vol_m3_hr: float = 100.0,
    ) -> str:
        """Step-by-step flowsheet construction recipe.

        Args:
            topology: One of ro_train, nf_softening, mvc_evaporator, manual, handoff_ingress
            flow_vol_m3_hr: Design flow rate in m³/hr (default 100)
        """
        t = topology.strip().lower().replace("-", "_")

        if t == "ro_train":
            return f"""## Build an RO Train Flowsheet

### Step 1 — Inspect the template
Read the template session spec to understand units, connections, and DOF:
```
Resource: watertap://templates/ro_train/session-spec
```

### Step 2 — Apply the template
Resolve stage (b) sizing first. `stage_b_config` must contain membrane_area_m2 [m2] or the complete stage_vessels / elements_per_vessel / element_area_m2 array,
plus `sizing_reference` (artifact_id, content_hash, artifact_type), `seed_bindings`
and unit-bearing `sizing_outputs`. Template examples are not sizing evidence.
For inverse design, choose `apply_design_template` explicitly; omission never selects design.
```
apply_template("ro_train", template_config=stage_b_config)
```
This creates a session with SEAWATER property package containing:
- Feed, Pump, ReverseOsmosis0D, Permeate (Product), Brine (Product)
- All connections pre-wired

### Step 3 — Set feed state
```
set_feed_state(
    session_id=SID,
    unit_id="Feed",
    flow_vol_m3_hr={flow_vol_m3_hr},
    tds_mg_L=35000,
)
```

### Step 4 — Pre-flight check
```
check_solve(session_id=SID)
```
Expect status "ready". If "underspecified", check missing engineering inputs.

### Step 5 — Solve
```
solve(session_id=SID)
```
Returns a job_id. Poll status:
```
Resource: watertap://jobs/{{job_id}}/status
```

### Step 6 — Read results
```
Resource: watertap://sessions/{{SID}}/manifest
```
Then navigate to:
- `watertap://sessions/{{SID}}/streams/RO.permeate` — permeate quality
- `watertap://sessions/{{SID}}/costing` — LCOW and cost breakdown
- `watertap://templates/ro_train/result-schema` — field paths and ranges

### Step 7 — Costing (optional)
```
enable_costing(session_id=SID)
add_unit_costing(session_id=SID, unit_id="HPPump")
add_unit_costing(session_id=SID, unit_id="RO")
compute_costing(session_id=SID)
```
Then re-solve to get LCOW.

### Engineering Ranges
- Recovery: 30–85%
- Rejection: >99%
- Flux: 15–30 LMH
- LCOW: $0.50–$3.00/m³
"""

        if t == "nf_softening":
            nf_flow = flow_vol_m3_hr if flow_vol_m3_hr != 100.0 else 3.6
            return f"""## Build an NF Softening Flowsheet

### Step 1 — Inspect the template
```
Resource: watertap://templates/nf_softening/session-spec
```

### Step 2 — Apply the template
Resolve stage (b) sizing first. `stage_b_config` must contain membrane_area_m2 [m2],
plus `sizing_reference` (artifact_id, content_hash, artifact_type), `seed_bindings`
and unit-bearing `sizing_outputs`. Template examples are not sizing evidence.
For inverse design, choose `apply_design_template` explicitly; omission never selects design.
```
apply_template("nf_softening", template_config=stage_b_config)
```
Creates a session with MCAS property package (5-ion brackish config).

### Step 3 — Set feed state
Read the reference feed for MCAS brackish water:
```
Resource: watertap://presets/MCAS_BRACKISH_5ION/reference-feed
```
Then:
```
set_feed_state(
    session_id=SID,
    unit_id="Feed",
    flow_vol_m3_hr={nf_flow},
    components={{
        "Na_+": 11122, "Ca_2+": 382, "Mg_2+": 1394,
        "Cl_-": 20101.6, "SO4_2-": 2136,
    }},
    component_charges={{"Na_+": 1, "Ca_2+": 2, "Mg_2+": 2, "Cl_-": -1, "SO4_2-": -2}},
    electroneutrality_species="Cl_-",
)
```

### Step 4–6 — Check, solve, read results
Same pattern as RO train. See:
```
Resource: watertap://templates/nf_softening/result-schema
```

### Engineering Ranges
- Ca removal: >90%
- Mg removal: >95%
- Operating pressure: 3–8 bar
- LCOW: $0.10–$1.50/m³
"""

        if t == "mvc_evaporator":
            return f"""## Build an MVC Evaporator Flowsheet

### Step 1 — Inspect the template
Read the template session spec:
```
Resource: watertap://templates/mvc_evaporator/session-spec
```

### Step 2 — Apply the template
Resolve stage (b) sizing first. `stage_b_config` must contain evaporator_area_m2 [m2],
plus `sizing_reference` (artifact_id, content_hash, artifact_type), `seed_bindings`
and unit-bearing `sizing_outputs`. Template examples are not sizing evidence.
For inverse design, choose `apply_design_template` explicitly; omission never selects design.
```
apply_template("mvc_evaporator", template_config=stage_b_config)
```
This creates a session with SEAWATER property package containing:
- Feed, Evaporator, Compressor (WATER), Condenser (WATER), Distillate (Product), Brine (Product)
- Thermal coupling: Evaporator ↔ Condenser (MVC self-heating via connect_to_condenser)
- Auto-translator: WATER → SEAWATER on Condenser → Distillate connection
- DOF fixes: brine outlet temp (333.15 K), U (1000 W/m²K), area (400 m²), pressure_ratio (2.0), efficiency (0.8)
- delta_temperature_in/out auto-removed (handled by condenser coupling)

### Step 3 — Set feed state
```
set_feed_state(
    session_id=SID,
    unit_id="Feed",
    component_mass_flow_kg_s={{"H2O": {flow_vol_m3_hr / 3.6:.1f}, "TDS": {flow_vol_m3_hr / 3.6 * 0.05:.3f}}},
    temperature_C=25,
    pressure_bar=1.0,
)
```

### Step 4 — Adjust engineering inputs (optional)
Override template defaults for your design case:
- `Evaporator.outlet_brine.temperature[0]` — brine outlet temperature (K)
- `Evaporator.U` — overall heat transfer coefficient (W/m²K)
- `Evaporator.area` — heat transfer area (m²)
- `Compressor.pressure_ratio` — vapor compression ratio
- `Compressor.efficiency` — isentropic efficiency

### Step 5 — Solve
```
check_solve(SID)  → should report "ready"
solve(SID)
get_solve_results(SID)
```

### Step 6 — Interpret results
Key outputs:
- `Evaporator.heat_transfer` — total heat duty (W)
- `Compressor.work_mechanical` — compressor power (W)
- Brine concentration from `Evaporator.outlet_brine` stream
- Distillate flow from Distillate Product

### Limitations
- SEAWATER BPE valid to ~120 g/kg. Not for ZLD (>200 g/kg) — use NACL_T_DEP + crystallizer.
- Condenser is non-costable: costing returns partial with condenser as blocker/skipped.
- Typical MVC SEC: 8–25 kWh/m³.
"""

        if t == "manual":
            return f"""## Build a Custom Flowsheet (Manual)

### Step 1 — Create a session
Choose a property package. Read the spec for available components and config:
```
Resource: watertap://packages/{{package_name}}/spec
```
Then:
```
create_session(property_package="...", property_package_config={{...}})
```
Or use a preset:
```
Resource: watertap://presets/{{preset_name}}/spec
```

### Step 2 — Add units
For each unit, read its spec first:
```
Resource: watertap://units/{{unit_type}}/spec
```
This shows engineering inputs with typical_min/typical_max/typical_default,
port configuration, DOF requirements, and costing support.
```
create_unit(session_id=SID, unit_id="MyUnit", unit_type="...")
```

### Step 3 — Connect units
```
connect_units(session_id=SID, source_unit="A", source_port="outlet",
              dest_unit="B", dest_port="inlet")
```
For explicit port-to-port wiring (e.g., translators or non-standard ports):
```
connect_ports(session_id=SID, source_unit="A", source_port="retentate",
              dest_unit="C", dest_port="inlet", translator_id="...")
```

### Step 4 — Set feed state and engineering inputs
```
set_feed_state(session_id=SID, unit_id="Feed", flow_vol_m3_hr={flow_vol_m3_hr}, ...)
set_unit_engineering_inputs(session_id=SID, unit_id="MyUnit", inputs={{...}})
```
Use the typical_default values from the unit spec as starting points.

### Step 5 — Validate and solve
```
validate_flowsheet(session_id=SID)
check_solve(session_id=SID)
solve(session_id=SID)
```

### Step 6 — Read results
```
Resource: watertap://sessions/{{SID}}/manifest
```
"""

        if t == "handoff_ingress":
            return """## Receive a QSDsan → WaterTAP Handoff

### Step 1 — Review available ingress profiles
```
Resource: watertap://handoff-profiles
```
Identifies target presets, required fields, and strict vs. approximate flags.

### Step 2 — Validate the handoff payload
```
validate_downstream_handoff(payload={...})
```
The payload must include: target_profile, property_package, property_package_config,
state_payload, completeness, missing_or_estimated_species, assumptions.

### Step 3 — Read the target preset spec
```
Resource: watertap://presets/{target_preset}/spec
```

### Step 4 — Create session from target preset and apply state
```
create_session(
    property_package=payload["property_package"],
    property_package_config=payload["property_package_config"],
    session_preset=payload["target_profile"],
)
```

### Step 5 — Add units and set feed
```
create_unit(session_id=SID, unit_id="Feed", unit_type="Feed")
set_feed_state(session_id=SID, unit_id="Feed", **payload["state_payload"])
```
Then add downstream process units, connect them, and set engineering inputs
following the manual workflow pattern.

### Step 6 — Solve and read results
```
check_solve(session_id=SID)
solve(session_id=SID)
```
Then:
```
Resource: watertap://sessions/{SID}/manifest
Resource: watertap://sessions/{SID}/costing
```
"""

        # Fallback
        return """## Available Flowsheet Topologies

| Topology | Description |
|----------|-------------|
| `ro_train` | Single-stage RO train via template (SEAWATER package) |
| `nf_softening` | NF softening train via template (MCAS 5-ion package) |
| `mvc_evaporator` | MVC evaporator via template (SEAWATER + WATER mixed-package) |
| `manual` | Step-by-step custom flowsheet construction |
| `handoff_ingress` | Receive QSDsan → WaterTAP cross-engine handoff |

Call this prompt with `topology="<name>"` for step-by-step instructions.
"""

    # === P2: analyze_results ===
    @mcp.prompt()
    def analyze_results(
        template_or_topology: str = "custom",
    ) -> str:
        """Guide for navigating and interpreting solve results.

        Args:
            template_or_topology: One of ro_train, nf_softening, mvc_evaporator, custom
        """
        t = template_or_topology.strip().lower().replace("-", "_")

        if t == "ro_train":
            return """## Analyzing RO Train Results

### Result Schema
```
Resource: watertap://templates/ro_train/result-schema
```

### Key KPI Paths (allowlist-based)
- `kpis.units.RO.recovery_frac_mass_H2O` — water recovery (fraction)
- `kpis.units.RO.area` — membrane area (m²)
- `kpis.units.RO.flux_mass_io_phase_comp` — mass flux (kg/m²/s), indexed
- `kpis.units.HPPump.work_mechanical` — pump power (W)

### Stream Data
- `solved_state.streams["RO.permeate"]` — permeate port snapshot
- `solved_state.streams["RO.retentate"]` — brine port snapshot
- Port entries keyed like: `flow_mass_phase_comp[Liq,H2O]` → `{value, fixed, lb, ub, units, stale}`

Or read individual streams:
```
Resource: watertap://sessions/{SID}/streams/RO.permeate
Resource: watertap://sessions/{SID}/streams/RO.retentate
```

### Costing
```
Resource: watertap://sessions/{SID}/costing
```
- `LCOW` — levelized cost of water ($/m³)
- `aggregate_capital_cost` — total CAPEX ($)
- `total_operating_cost` — annual OPEX ($/yr)
- Per-unit cost breakdowns

### Engineering Reasonableness
| Metric | Min | Max | Units |
|--------|-----|-----|-------|
| Recovery | 0.30 | 0.85 | fraction |
| Rejection | >0.99 | 0.9999 | fraction |
| Flux | 15 | 30 | LMH |
| LCOW | 0.50 | 3.00 | $/m³ |

### Note on KPI Coverage
KPI extraction uses an allowlist of port/state-var/metric names. Not all
unit-specific variables are captured. For full unit variable access, use
the `get_unit_results()` tool.
"""

        if t == "nf_softening":
            return """## Analyzing NF Softening Results

### Result Schema
```
Resource: watertap://templates/nf_softening/result-schema
```

### Key KPI Paths (allowlist-based)
- `kpis.units.NF.recovery_frac_mass_H2O` — water recovery (fraction)
- `kpis.units.NF.area` — membrane area (m²)
- `kpis.units.NFPump.work_mechanical` — pump power (W)
- `kpis.streams.NF.permeate.flow_mass_phase_comp` — permeate ion flows

### Key Stream Paths
- `solved_state.streams["NF.permeate"]` — softened water
- `solved_state.streams["NF.retentate"]` — concentrate

Or read individually:
```
Resource: watertap://sessions/{SID}/streams/NF.permeate
Resource: watertap://sessions/{SID}/streams/NF.retentate
```

MCAS component flows are in `flow_mass_phase_comp` (kg/s basis internally).
Convert to mg/L using flow_vol for reporting.

### Assessing Hardness Removal
Compare Ca²⁺ and Mg²⁺ concentrations between feed and permeate:
- Feed Ca²⁺/Mg²⁺ from `solved_state.streams["Feed.outlet"]`
- Permeate Ca²⁺/Mg²⁺ from `solved_state.streams["NF.permeate"]`
- Removal = 1 - (permeate / feed) for each ion

### Divalent Rejection
Divalent ions (Ca²⁺, Mg²⁺, SO₄²⁻) should be >90% rejected by NF membranes.
Calculate rejection for each: `rejection = 1 - (permeate_flow / feed_flow)`.
Compare `flow_mass_phase_comp` entries for each divalent ion between feed and
permeate streams.

### Monovalent Passage
Monovalent ions (Na⁺, Cl⁻) pass through NF at higher rates than divalents.
Typical monovalent rejection is 20–60% (much lower than divalent >90%).
This is expected NF behavior, not a defect — NF selectively removes hardness
while allowing monovalents through.

### Costing
```
Resource: watertap://sessions/{SID}/costing
```
- LCOW: $0.10–$1.50/m³ typical

### Engineering Reasonableness
| Metric | Min | Max | Units |
|--------|-----|-----|-------|
| Ca removal | >0.90 | 0.99 | fraction |
| Mg removal | >0.95 | 0.99 | fraction |
| Operating pressure | 3 | 8 | bar |
| LCOW | 0.10 | 1.50 | $/m³ |

### Note on KPI Coverage
NF-specific rejection variables (e.g., per-ion rejection coefficients) may
not appear in `kpis.units` due to the allowlist. Use
`get_unit_results(session_id, unit_id="NF")` for complete NF variable access.
"""

        if t == "mvc_evaporator":
            return """## Analyzing MVC Evaporator Results

### Result Schema
```
Resource: watertap://templates/mvc_evaporator/result-schema
```

### Key KPI Paths (allowlist-based)
- `kpis.units.Evaporator.heat_transfer` — total heat duty (W)
- `kpis.units.Evaporator.area` — heat transfer area (m²)
- `kpis.units.Evaporator.U` — overall HTC (W/m²K)
- `kpis.units.Compressor.work_mechanical` — compressor power (W)

### Stream Data
- `solved_state.streams["Evaporator.outlet_brine"]` — concentrated brine
- `solved_state.streams["Evaporator.outlet_vapor"]` — vapor to compressor
- Port entries keyed like: `flow_mass_phase_comp[Liq,H2O]`

Or read individually:
```
Resource: watertap://sessions/{SID}/streams/Evaporator.outlet_brine
```

### Costing
```
Resource: watertap://sessions/{SID}/costing
```
- Condenser is non-costable — LCOW is partial (condenser listed as skipped)
- Evaporator + Compressor costing should be present

### Engineering Reasonableness
| Metric | Min | Max | Units |
|--------|-----|-----|-------|
| SEC | 8 | 25 | kWh/m³ |
| Brine concentration | 70 | 120 | g/L |
| Heat transfer area | 50 | 2000 | m² |

### Calculating SEC
Specific energy consumption = Compressor.work_mechanical / (distillate_flow_vol * 3600)
Convert to kWh/m³ for comparison with heuristic ranges.
"""

        # custom / fallback
        return """## Analyzing Custom Flowsheet Results

### Step 1 — Read the manifest
```
Resource: watertap://sessions/{SID}/manifest
```
Shows available artifacts, stream keys, and KPI snapshots.

### Step 2 — Navigate solved state
The full solved state is at:
```
Resource: watertap://sessions/{SID}/results
```
Key structure:
- `results.solved_state.units.{unit_id}.variables` — all unit variables
- `results.solved_state.streams` — flat dict keyed as "UnitId.port_name"
- `results.solved_state.stream_roles` — port role annotations
- `results.kpis.streams.{unit_id}.{port_name}` — extracted stream KPIs
- `results.kpis.units.{unit_id}` — extracted unit KPIs

### Step 3 — Individual stream access
```
Resource: watertap://sessions/{SID}/streams/{stream_key}
```
Stream keys are flat dot-separated: "RO.permeate", "Feed.outlet".

### Step 4 — Costing
```
Resource: watertap://sessions/{SID}/costing
```

### Note on KPI Extraction
KPI extraction uses an allowlist of port names, state variables, and unit
metrics. For specialized units (ED, MD, etc.), some metrics may be absent
from the KPI dict. Use `get_unit_results()` for full variable access.
"""

    # === P3: troubleshoot_solver ===
    @mcp.prompt()
    def troubleshoot_solver(
        termination_condition: str = "general",
    ) -> str:
        """Diagnostic guide for IPOPT solver failures.

        Args:
            termination_condition: One of infeasible, maxIterations, unbounded,
                underspecified, locallyInfeasible, or other/general
        """
        tc = termination_condition.strip().lower()

        if tc == "infeasible":
            return """## Troubleshooting: Infeasible

No feasible solution exists with current constraints and bounds.

### Diagnostic Steps
1. **Check constraint residuals** to identify problematic constraints:
   ```
   get_constraint_residuals(session_id=SID)
   ```

2. **Verify physical feasibility:**
   - For RO: Is feed pressure above osmotic pressure?
   - Check that operating conditions are physically achievable
   - Verify mass balance closure

3. **Inspect bound violations:**
   ```
   get_bound_violations(session_id=SID)
   ```

4. **Suggested fixes:**
   - Relax variable bounds that may be too tight
   - Check fixed variable values against typical ranges
   - Read unit spec for valid engineering input ranges:
     ```
     Resource: watertap://units/{unit_type}/spec
     ```
"""

        if tc in ("maxiterations", "max_iterations"):
            return """## Troubleshooting: Max Iterations

Solver hit iteration limit — likely poor scaling or bad initialization.

### Diagnostic Steps
1. **Check scaling issues:**
   ```
   report_scaling_issues(session_id=SID)
   ```

2. **Apply recommended scaling:**
   - For RO membranes: scale A_comp by 1e12, B_comp by 1e8
   ```
   calculate_scaling_factors(session_id=SID)
   autoscale_large_jac(session_id=SID)
   ```

3. **Check initialization:**
   ```
   get_initialization_order(session_id=SID)
   ```
   Initialize units sequentially with state propagation:
   ```
   propagate_state(session_id=SID, source_port="Feed.outlet", dest_port="Pump.inlet")
   ```

4. **Re-solve after scaling/init fixes:**
   ```
   solve(session_id=SID)
   ```
"""

        if tc == "unbounded":
            return """## Troubleshooting: Unbounded

Variables going to infinity — missing constraints.

### Diagnostic Steps
1. **Verify all required variables are fixed:**
   ```
   check_solve(session_id=SID)
   ```
   Ensure DOF = 0.

2. **Check for missing engineering inputs:**
   ```
   list_unfixed_vars(session_id=SID, unit_id="<unit_id>")
   ```

3. **Run diagnostics:**
   ```
   run_diagnostics(session_id=SID)
   ```
   Look for structurally singular blocks.
"""

        if tc in ("underspecified", "dof"):
            return """## Troubleshooting: Underspecified (DOF > 0)

The flowsheet still has unresolved degrees of freedom.

### Diagnostic Steps
1. **Run solve check for detailed DOF breakdown:**
   ```
   check_solve(session_id=SID)
   ```
   This reports:
   - Unresolved required fixes per unit
   - Missing engineering inputs
   - Feed units without state

2. **Fix missing sizing/operating variables:**
   Read the unit spec for required inputs:
   ```
   Resource: watertap://units/{unit_type}/spec
   ```
   Then:
   ```
   set_unit_engineering_inputs(session_id=SID, unit_id="...", inputs={...})
   ```

3. **For zero-order units:** Load database parameters first:
   ```
   load_zo_parameters(session_id=SID, unit_id="...")
   ```
"""

        if tc in ("locallyinfeasible", "locally_infeasible"):
            return """## Troubleshooting: Locally Infeasible

Local minimum is infeasible — bad initial point.

### Diagnostic Steps
1. **Try different initial values:**
   ```
   set_unit_engineering_inputs(session_id=SID, unit_id="...", inputs={...})
   ```

2. **Initialize from known feasible solution:**
   ```
   get_initialization_order(session_id=SID)
   initialize_flowsheet(session_id=SID)
   ```

3. **Check for conflicting fixed variables:**
   ```
   list_unfixed_vars(session_id=SID, unit_id="<unit_id>")
   get_dof_status(session_id=SID)
   ```
"""

        # general / other / catch-all
        return f"""## Troubleshooting: {termination_condition}

### Diagnostic Steps
1. **Get last failure diagnostics (most detail):**
   ```
   get_last_failure_diagnostics(session_id=SID)
   ```

2. **Run full IDAES diagnostics:**
   ```
   run_diagnostics(session_id=SID)
   ```

3. **Use the diagnose_failure tool:**
   ```
   diagnose_failure(session_id=SID, termination_condition="...")
   ```

4. **For scaling-related failures:**
   ```
   report_scaling_issues(session_id=SID)
   autoscale_large_jac(session_id=SID)
   ```

5. **For DOF-related failures:**
   ```
   check_solve(session_id=SID)
   ```

### Common Termination Conditions
| Condition | Typical Cause |
|-----------|---------------|
| infeasible | Impossible constraints or bounds |
| maxIterations | Poor scaling or initialization |
| unbounded | Missing variable fixes (DOF > 0) |
| underspecified | Missing engineering inputs |
| locallyInfeasible | Bad initial point |
| infeasibleOrUnbounded | Combination — check DOF first |
| maxTimeLimit | Complex model — simplify or increase timeout |
| numerical_error | Ill-conditioned Jacobian — rescale |

Call this prompt with `termination_condition="<specific>"` for targeted guidance.
"""
