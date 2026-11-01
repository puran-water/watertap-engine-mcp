# Known issues

## Complete solver and flowsheet regression qualification

Broader source tests retained failures, errors, skips and timeouts. Solver binaries and model initialization remain environment-dependent; representative smoke cases do not qualify all flowsheets.

This is an acknowledged v0.1.0 limitation. See README.md and TEST-RESULTS.md for scope and evidence.

Historical native-source snapshot: 1,926 passed, 53 failed, 2 errors and 6 skipped. Those counts predate this cleaned public export. Later repairs exercised subsets only; TEST-RESULTS.md reports the separate installed-artifact qualification. The full source suite has not been repeated against the standalone public package.

## Qualify generic replacements for excluded application workflows

Private site presets, vendor-fitted application workflows, product-bound membrane catalogs and database publication are excluded. Use general unit APIs with explicit inputs; no proprietary calibration parameters are bundled.

This is an acknowledged v0.1.0 limitation. See README.md and TEST-RESULTS.md for scope and evidence.

Bundled influent input presets are also excluded. Supply the full influent basis explicitly; choosing a feedstock classification does not supply a dataset.

## Complete resource-bound and comparative benchmarks

Full graph/job resource coverage, cross-platform solvers and a common benchmark against competing wrappers remain open.

This is an acknowledged v0.1.0 limitation. See README.md and TEST-RESULTS.md for scope and evidence.

## Resolve remaining installed-artifact qualification findings

The v0.1.0 source/wheel qualification completed MCP startup but retained these failed checks:

- `geometry-required` on `solve_flowsheet`.

See TEST-RESULTS.md. These failures are disclosed for early feedback; they are not claimed as passing validation.
