# Installed-artifact qualification

Python 3.12 on Linux x86-64. Each source distribution and wheel was installed in an isolated uv environment with shared cached dependencies. No private package or database credential was supplied. The stdio client initialized the installed entrypoint and called the tools below on synthetic inputs.

These are targeted packaging and representative domain checks, not a full numerical or cross-platform qualification. Historical broader test counts are in [HISTORICAL-QUALITY.md](HISTORICAL-QUALITY.md).

## Source installation

MCP discovery: PASS; 94 tools.

| Check | Tool | Result |
|---|---|---|
| upstream-unit-registry | `get_unit_spec` | PASS |
| create-nacl-session | `create_session` | PASS |
| unknown-unit-rejected | `get_unit_spec` | PASS |
| fixed-dose-chemistry-screen | `screen_madm1_fe_s_p_steady_state` | PASS |
| geometry-required | `solve_flowsheet` | KNOWN ISSUE |

## Wheel installation

MCP discovery: PASS; 94 tools.

| Check | Tool | Result |
|---|---|---|
| upstream-unit-registry | `get_unit_spec` | PASS |
| create-nacl-session | `create_session` | PASS |
| unknown-unit-rejected | `get_unit_spec` | PASS |
| fixed-dose-chemistry-screen | `screen_madm1_fe_s_p_steady_state` | PASS |
| geometry-required | `solve_flowsheet` | KNOWN ISSUE |
