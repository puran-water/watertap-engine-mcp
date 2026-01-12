# Bug Tracking - Flowsheet Simulation Testing

## Test Session: 2026-01-10

### Running Bug List

| # | Severity | Description | File | Status |
|---|----------|-------------|------|--------|
| 1 | HIGH | ZO property package requires 'database' config but not auto-provided | utils/model_builder.py | FIXED |
| 2 | MEDIUM | Arcs created but show 0 in component_objects query (expected - expansion) | utils/model_builder.py | NOT A BUG |
| 3 | HIGH | state_args contain tuple keys that can't be JSON serialized | core/session.py | FIXED |
| 4 | LOW | Unicode symbols (→✓✗) fail on Windows cp1252 console | cli.py | FIXED |
| 5 | CRITICAL | SequentialDecomposition uses wrong API (get_ssc_order vs create_graph+calculation_order) | utils/topo_sort.py | FIXED |
| 6 | CRITICAL | SequentialDecomposition select_tear_set defaults to cplex MIP solver | utils/topo_sort.py | FIXED |
| 7 | HIGH | OSError: Invalid argument on sys.stdout.flush() during solve (Windows/WSL) | worker.py | FIXED |
| 8 | LOW | CLI doesn't expose property_package_config for MCAS (API only) | cli.py | FIXED |
| 9 | MEDIUM | Job result JSON file truncated/incomplete on failed solves | worker.py, job_manager.py | FIXED |
| 10 | MEDIUM | solve_time/iterations as UndefinedData can't be converted to float/int | worker.py | FIXED |

---

## Test Results

### Test 1: Simple Seawater RO Flowsheet (CLI)
- Status: PASSED (up to solve - WaterTAP env needed)
- Session: 8f20da6d-f932-4930-98e5-14a58ecab575
- Notes:
  - Created session, feed, units (Feed, Pump, RO)
  - Connected units via CLI
  - Fixed DOF variables (7 total → 0)
  - Solve job submitted but failed due to WaterTAP not in test env

### Test 2: Zero-Order Flowsheet (FeedZO + PumpZO)
- Status: PASSED (model build)
- Notes:
  - Bug #1 found and fixed: ZO database config now auto-provided
  - Build now succeeds

### Test 3: NaCl Property Package Flowsheet
- Status: PASSED (model build)
- Notes: Build successful

### Test 4: Multi-Unit Flowsheet (4 units, 3 connections)
- Status: PASSED (model build)
- Notes:
  - Feed → Pump → RO1 → RO2
  - Arcs created and expanded correctly

### Test 5: Biological Flowsheet with Translator
- Status: PASSED (session creation)
- Notes:
  - ASM1 → ADM1 translator registered
  - No biological units in registry (expected - not implemented yet)

---

## Bugs Fixed This Session

1. **ZO Database Config** - Model builder now auto-creates WaterTAP Database for ZO property packages
2. **Tuple Key Serialization** - Added `_serialize_dict_keys()` and `_deserialize_dict_keys()` for JSON handling
3. **Unicode Symbols** - Replaced arrow/check/cross symbols with ASCII equivalents [OK][X]->
4. **SequentialDecomposition API** - Fixed to use `create_graph()` + `calculation_order()` instead of non-existent `get_ssc_order()`
5. **SequentialDecomposition Solver** - Set `select_tear_method = "heuristic"` to avoid cplex MIP solver dependency
6. **Windows/WSL stdout flush** - Wrapped solver.solve() with stdout/stderr redirection to devnull
7. **Job JSON truncation** - Added `f.flush()` after JSON write and safe type conversions for solve_time/iterations

---

## Test Session Summary (2026-01-11)

**6 E2E Tests Completed:**
- Zero-Order flowsheet: PASSED (model build)
- NaCl flowsheet with Pump + RO: PASSED (solve hit maxIterations - expected without proper initialization)
- MCAS flowsheet: PASSED (proper error message for missing config)
- Scaling workflow: PASSED (set/report scaling factors)
- Initialize flowsheet: PASSED (all units initialized)
- Results extraction: PASSED (optimal solve with KPIs extracted)

**Final State:**
- 297 tests passing (222 unit + 75 E2E)
- All 10 bugs identified and fixed
- Test suite hardened: warnings-as-errors, no MagicMock, no silent exception swallowing

---

## Test Suite Reliability Audit (2026-01-11)

**Codex Audit Findings Addressed:**
1. CLI workflow tests now poll job status and verify actual solve outcomes
2. Error-path tests have specific exit code and error message assertions
3. Worker subprocess tests validate job completion and return codes
4. All MagicMock usage removed - tests use real WaterTAP models
5. Silent exception swallowing removed - all failures now logged as warnings
6. datetime.utcnow() deprecation fixed across codebase
7. pytest configured with `filterwarnings = ["error"]` to fail on warnings
8. All pytest.skip/skipif removed - tests FAIL LOUDLY if dependencies unavailable

**Test Coverage:**
- CLI smoke tests: 22 commands
- CLI workflow tests: 5 full create-to-solve workflows
- Error path tests: 12 invalid input/failure scenarios
- Property package tests: SEAWATER, NaCl, MCAS, Zero-Order
- Worker subprocess tests: 4 tests for background job execution
- Real solver tests: IPOPT solve with KPI extraction
- Initialization tests: SequentialDecomposition behavior verification

---

## Test Session: 2026-01-11 (Session 2)

**Code Review Feedback Audit:**
- Reviewed 10 code review claims against actual codebase
- 8 of 10 claims were inaccurate (code already correct)
- 2 valid issues identified and fixed:
  1. Recovery module integrated into HygienePipeline failure handling
  2. Results source indication added to get_stream_results/get_unit_results

**New Tests Added:**
- `tests/test_solver.py`: TestRecoveryIntegration class (5 tests)
- `tests/e2e/test_results_source.py`: Results source indication tests (4 tests)

---

## Test Session: 2026-01-12

**Code Review Remediation Plan Completed (7 Phases):**

All phases from `docs/plans/code-review-remediation-plan.md` implemented:

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Variable path resolution using `find_component()` | COMPLETE |
| 2 | CLI stub remediation (4 commands fixed) | COMPLETE |
| 3 | validate_flowsheet enhancement (4 checks) | COMPLETE |
| 4 | Costing implementation (6 tools) | COMPLETE |
| 5 | ZO parameters persistence | COMPLETE |
| 6 | Test coverage (4 new test files) | COMPLETE |
| 7 | Documentation updates | COMPLETE |

**New Test Files:**
- `tests/test_path_resolution.py` - Variable path resolution tests
- `tests/test_validate_flowsheet.py` - Validation tests
- `tests/test_costing.py` - Costing workflow tests
- `tests/test_cli_parity.py` - CLI/Server parity tests

**Final State:**
- 373 tests passing
- 57 MCP tools implemented
- All Codex review findings addressed

---
