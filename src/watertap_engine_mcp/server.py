"""WaterTAP Engine MCP server.

The MCP adapter is intentionally thin. All tool logic lives in
`actions.py`, and the tool surface is registered from the shared catalog in
`action_catalog.py`.
"""
from __future__ import annotations
import os
from functools import wraps
from typing import Any, Callable
from fastmcp import FastMCP
from watertap_engine_mcp.engineering_utils_bridge import ensure_engineering_utils_importable
ensure_engineering_utils_importable()
import watertap_engine_mcp.actions as action_module
from watertap_engine_mcp.action_catalog import ACTIONS, list_actions
from watertap_engine_mcp.api.tools import output_schema_for_action, typed_action_wrapper
from watertap_engine_mcp.prompts import register_prompts
from watertap_engine_mcp.resources import register_resources
from watertap_engine_mcp.security.redaction import redact_public_payload
session_manager = action_module.session_manager
job_manager = action_module.job_manager

def _sync_shared_state() -> None:
    """Propagate adapter-level mutable state into the shared action module."""
    action_module.session_manager = session_manager
    action_module.job_manager = job_manager

def _make_action_wrapper(action_id: str) -> Callable[..., Any]:
    """Create a thin wrapper that keeps adapter-injected state in sync."""
    handler = getattr(action_module, action_id)

    @wraps(handler)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        _sync_shared_state()
        return redact_public_payload(getattr(action_module, action_id)(*args, **kwargs))
    return typed_action_wrapper(action_id, wrapper)
mcp = FastMCP('watertap-engine-mcp', instructions='WaterTAP steady-state flowsheet engine for process engineers and process-engineering agents. Use the full internal tool surface to plan or build flowsheets, set feed states and engineering inputs, run solves, inspect diagnostics, and extract results. Solver hygiene is handled by the server.')
for action in ACTIONS:
    wrapper = _make_action_wrapper(action.id)
    globals()[action.id] = wrapper
_configured_tool_surface = os.getenv('MCP_TOOL_SURFACE', 'all').strip().lower()
_registered_actions = tuple(list_actions(_configured_tool_surface))
for action in _registered_actions:
    wrapper = globals()[action.id]
    output_schema = output_schema_for_action(action.id)
    if output_schema is None:
        mcp.tool(name=action.id, description=action.summary)(wrapper)
    else:
        mcp.tool(name=action.id, description=action.summary, output_schema=output_schema)(wrapper)
register_resources(mcp)
register_prompts(mcp)

def main():
    mcp.run(transport='stdio')
if __name__ == '__main__':
    main()
