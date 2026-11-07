# watertap-engine-mcp

Build and solve steady-state process flowsheets using WaterTAP, IDAES and Pyomo, with structured solver diagnostics.

Experimental v0.1.0. These tools return calculations and structured evidence; the calling LLM supplies explanations and reporting. Engineering review remains necessary.

## Quick start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and run with Python 3.12:

```sh
uvx --python 3.12 --with 'puran-mcp-support @ git+https://github.com/puran-water/fluids-mcp.git@v0.1.0#subdirectory=support/puran-mcp-support' --from 'git+https://github.com/puran-water/watertap-engine-mcp.git@v0.1.0' watertap-engine-mcp
```

The server uses local MCP stdio. It requires no Puran account or private package. uv shares its dependency cache between isolated tool environments. PyPI publishing has not been configured; the pinned public Git release is the installation path.

## MCP client configuration

Use this configuration in a client supporting local stdio MCP servers:

```json
{
  "mcpServers": {
    "watertap-engine-mcp": {
      "command": "uvx",
      "args": [
        "--python",
        "3.12",
        "--with",
        "puran-mcp-support @ git+https://github.com/puran-water/fluids-mcp.git@v0.1.0#subdirectory=support/puran-mcp-support",
        "--from",
        "git+https://github.com/puran-water/watertap-engine-mcp.git@v0.1.0",
        "watertap-engine-mcp"
      ]
    }
  }
}
```

Run the server in a user-owned working directory. Local input and result files belong to that user. Hosted HTTP authentication and managed employee access are outside this release.

## Known issues

- **Complete solver and flowsheet regression qualification.** Broader source tests retained failures, errors, skips and timeouts. Solver binaries and model initialization remain environment-dependent; representative smoke cases do not qualify all flowsheets.
- **Qualify generic replacements for excluded application workflows.** Private site presets, vendor-fitted application workflows, product-bound membrane catalogs and database publication are excluded. Use general unit APIs with explicit inputs; no proprietary calibration parameters are bundled.
- **Complete resource-bound and comparative benchmarks.** Full graph/job resource coverage, cross-platform solvers and a common benchmark against competing wrappers remain open.
- **Resolve remaining installed-artifact qualification findings.** The v0.1.0 source/wheel qualification completed MCP startup but retained these failed checks:

See [known issue details](KNOWN-ISSUES.md), [test results](TEST-RESULTS.md), [content scan](SCAN-REPORT.md), and [licensing inventory](LICENSE-TABLE.md). These limitations are disclosed for early feedback.

## License and attribution

See LICENSE, NOTICE and THIRD-PARTY-NOTICES for the wrapper and retained upstream material. External dependencies retain their own licenses.

Built by [Puran Water](https://puranwater.com).

Historical source-suite snapshot: 1926 passed, 53 failed, 6 skipped, 2 error. These counts predate the cleaned standalone export; they are not its qualification result. Later repairs covered selected subsets only. See [historical quality scope](HISTORICAL-QUALITY.md).

Bundled feedstock input presets are excluded. Supply a complete influent basis; classification names alone do not provide a design input dataset.

## Following current development

The `main` branch receives approved development fixes through a one-way source
mirror. Publication still checks secrets, private content, redistribution rights
and package boundaries. Version tags such as `v0.1.0` remain immutable. Installed
copies do not update automatically. To upgrade the server and its public support
package together from the current branches in your existing environment:

```sh
uv pip install --upgrade --reinstall \
  "puran-mcp-support[simulation] @ git+https://github.com/puran-water/fluids-mcp.git@main#subdirectory=support/puran-mcp-support" \
  "watertap-engine-mcp @ git+https://github.com/puran-water/watertap-engine-mcp.git@main"
```

Pin both Git commits when repeatability is more important than following changes.
The mirror records the source commit in `.puran-mirror.json`.
