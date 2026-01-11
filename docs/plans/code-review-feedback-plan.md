# Plan: Address Code Review Feedback

## Executive Summary

After thorough investigation verified independently by both Claude and Codex:
- **8 of 10 feedback items are INACCURATE** - code already handles these correctly
- **2 issues require action** (confirmed by Codex):
  1. Recovery module not integrated into pipeline failure handling (TODO stub)
  2. Two solve paths exist but lack documentation

**Additional enhancement**: Add source indication to results fallback for clarity.

### watertap-ui Comparison
watertap-ui uses a simpler approach: display errors to users, let them manually retry. No automatic recovery logic. This makes sense for a GUI, but our MCP server (used by AI agents) benefits from automatic recovery attempts since agents can't easily interpret and fix solver failures.

## Feedback Accuracy Assessment

### INACCURATE (No Action Required)

| Issue | Claim | Reality |
|-------|-------|---------|
| Arc expansion | "Never called" | `_expand_arcs()` IS called in `ModelBuilder.build()` line 67, uses `TransformationFactory('network.expand_arcs')` at lines 442-454 |
| Translator insertion | "Not implemented" | `_create_connection()` lines 417-435 DOES route through translators, creating two arcs |
| solve(background=False) | "Returns stub" | Current `solve()` tool ALWAYS uses background jobs - no `background` param exists |
| Diagnostics counting | "False positives" | Code correctly uses `issues_found=len(issues)`, "No issues" goes only to `details` |
| ZO process_subtype | "Not used" | IS set on `unit_block.config.process_subtype` before `load_parameters_from_database()` |
| Unit discovery | "Broken" | `_discover_units()` correctly looks under `model.fs` (IDAES convention) |
| Dead code | "flowsheet_session.py exists" | File has been removed (only stale .pyc remains) |

### ACCURATE (Action Required)

#### Issue 1: Recovery Module Not Integrated (Codex Claim 4)
- **Location**: `solver/pipeline.py` lines 521-522
- **Current**: `# TODO: Implement relaxed solve in recovery.py` followed by `pass`
- **Reality**: `solver/recovery.py` is fully implemented (449 lines) with `FailureAnalyzer`, `RecoveryExecutor`, but never called from pipeline

#### Issue 2: Two Solve Paths Undocumented (Codex Claim 10)
- **Location**: `worker.py`
- `run_solve()` (lines 295-500) - Direct path used by `solve()` tool
- `run_full_pipeline()` (lines 160-280) - HygienePipeline used by `build_and_solve()`
- **Recommendation**: Document distinction rather than consolidate (different use cases)

#### Enhancement: Results Fallback Source Indication
- **Location**: `server.py` `get_stream_results()` / `get_unit_results()`
- **Current**: Primary path returns `"source": "solved"`, fallback returns no source field
- **Enhancement**: Add `"source": "unsolved_model"` and warning to fallback responses

---

## Implementation Plan

### Phase 1: Integrate Recovery Module (High Priority)

**File**: `solver/pipeline.py`

1. Import recovery module:
```python
from .recovery import RecoveryExecutor, analyze_and_suggest_recovery
```

2. Add `RecoveryExecutor` to `__init__`:
```python
self._recovery = RecoveryExecutor(model)
```

3. Replace TODO at lines 521-522:
```python
if (
    self._config.enable_relaxed_solve
    and result.state == PipelineState.SOLVING
):
    recovery_result = self._recovery.attempt_recovery(
        termination_condition=result.details.get("termination_condition", "unknown"),
        max_attempts=3,
    )

    if recovery_result.success:
        self._transition(
            PipelineState.RELAXED_SOLVE,
            True,
            f"Recovery succeeded: {recovery_result.message}",
            details={
                "strategy": recovery_result.strategy.value,
                "actions_taken": recovery_result.actions_taken,
            },
        )
        return self.run_post_solve_diagnostics()
    else:
        result.details["recovery_attempted"] = True
        result.details["recovery_actions"] = recovery_result.actions_taken
```

### Phase 2: Add Source Field to Results Fallback (Medium Priority)

**File**: `server.py`

Update `get_stream_results()` fallback return (~line 2748):
```python
return {
    "session_id": session_id,
    "source": "unsolved_model",
    "warning": "Values are from an unsolved model. Run solve() first.",
    "streams": stream_data,
    "count": len(stream_data),
}
```

Update `get_unit_results()` fallback return similarly.

### Phase 3: Document Solve Path Distinction (Low Priority)

**File**: `CLAUDE.md`

Add section:
```markdown
## Solve Paths

1. **`solve()` tool** - Direct path (`worker.run_solve()`)
   - DOF check, scaling, init, IPOPT solve
   - Best for: Simple flowsheets, iterative development

2. **`build_and_solve()` tool** - Full pipeline (`worker.run_full_pipeline()`)
   - HygienePipeline state machine with diagnostics
   - Supports recovery on failure
   - Best for: Complex flowsheets, production use
```

### Phase 4: Add Tests

**File**: `tests/test_solver.py` - Add recovery integration tests
**File**: `tests/e2e/test_results_source.py` - Add results source indication tests

---

## Critical Files

| File | Changes |
|------|---------|
| `solver/pipeline.py` | Integrate RecoveryExecutor into failure handling |
| `solver/recovery.py` | No changes (already complete) |
| `server.py` | Add `source` and `warning` fields to results fallback |
| `CLAUDE.md` | Document solve path distinction |
| `tests/test_solver.py` | Add recovery integration tests |

## Verification

1. Run existing tests: `pytest tests/ -v`
2. Test recovery integration manually:
   - Create a flowsheet with a unit that will fail to solve
   - Call `build_and_solve()` with `enable_relaxed_solve=True`
   - Verify recovery is attempted
3. Test results source indication:
   - Call `get_stream_results()` before solve - verify `source: "unsolved_model"`
   - Call `get_stream_results()` after solve - verify `source: "solved"`
