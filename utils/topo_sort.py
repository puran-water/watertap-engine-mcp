"""Initialization order utilities for WaterTAP flowsheets.

Uses IDAES SequentialDecomposition as the standard approach per WaterTAP best practices.

Two modes:
1. **Actual initialization** (model provided): Uses IDAES SequentialDecomposition.
   Fails loudly if SequentialDecomposition unavailable - NO silent fallback.
2. **Session planning** (no model yet): Uses simple topological sort for order estimation.
   This is acceptable since we're just planning, not actually initializing.
"""

from typing import Any, Dict, List, Optional, Tuple


class SequentialDecompositionError(Exception):
    """Raised when IDAES SequentialDecomposition is unavailable or fails."""
    pass


def compute_initialization_order(
    units: Dict[str, Any],
    connections: List[Dict[str, str]],
    tear_streams: Optional[List[Tuple[str, str]]] = None,
    model: Any = None,
) -> List[str]:
    """Compute unit initialization order using IDAES SequentialDecomposition.

    This is the WaterTAP standard approach. We fail loudly if SequentialDecomposition
    is not available - no silent fallback to custom implementations.

    Args:
        units: Dict of unit_id -> unit instance (can be None for session-only)
        connections: List of connection dicts with src_unit, src_port, dest_unit, dest_port
        tear_streams: Optional tear stream edges to break cycles
        model: Optional Pyomo model (required for SequentialDecomposition)

    Returns:
        List of unit IDs in initialization order

    Raises:
        SequentialDecompositionError: If IDAES SequentialDecomposition unavailable
    """
    # If we have a model, use the real IDAES SequentialDecomposition
    if model is not None and hasattr(model, 'fs'):
        return _compute_order_with_sequential_decomposition(model, tear_streams)

    # For session-only (no model built yet), use simple topological sort
    # This is acceptable since we're just planning, not actually initializing
    return _compute_order_from_connections(units, connections, tear_streams)


def _compute_order_with_sequential_decomposition(
    model: Any,
    tear_streams: Optional[List[Tuple[str, str]]] = None,
) -> List[str]:
    """Use IDAES SequentialDecomposition for initialization order.

    Args:
        model: Pyomo ConcreteModel with fs block
        tear_streams: Optional tear stream specification

    Returns:
        List of unit IDs in initialization order

    Raises:
        SequentialDecompositionError: If SequentialDecomposition fails
    """
    try:
        from idaes.core.util.initialization import (
            propagate_state,
        )
        from pyomo.network import Arc, SequentialDecomposition
    except ImportError as e:
        raise SequentialDecompositionError(
            f"IDAES SequentialDecomposition not available. "
            f"WaterTAP requires IDAES for proper initialization. "
            f"Import error: {e}"
        )

    try:
        # Create SequentialDecomposition instance
        seq = SequentialDecomposition()

        # Set tear streams if specified
        if tear_streams:
            # Convert unit ID pairs to Arc objects
            tear_arcs = []
            fs = model.fs
            for src_unit, dest_unit in tear_streams:
                # Find Arc connecting these units
                for arc in fs.component_objects(Arc, active=True):
                    src_port = arc.source
                    dst_port = arc.destination
                    if src_port and dst_port:
                        src_block = src_port.parent_block()
                        dst_block = dst_port.parent_block()
                        if (hasattr(src_block, 'name') and hasattr(dst_block, 'name')):
                            if src_unit in str(src_block.name) and dest_unit in str(dst_block.name):
                                tear_arcs.append(arc)
                                break
            if tear_arcs:
                seq.set_tear_set(tear_arcs)

        # Get computation order from SequentialDecomposition
        order_blocks = seq.get_ssc_order(model.fs)

        # Extract unit IDs from block names
        order = []
        for block in order_blocks:
            # Extract unit ID from block path
            name = str(block.name) if hasattr(block, 'name') else str(block)
            # Remove 'fs.' prefix if present
            if '.fs.' in name:
                name = name.split('.fs.')[-1]
            elif name.startswith('fs.'):
                name = name[3:]
            # Only include unit blocks, not arcs or other components
            if not name.startswith('arc_') and '.' not in name:
                order.append(name)

        return order

    except Exception as e:
        raise SequentialDecompositionError(
            f"IDAES SequentialDecomposition failed. "
            f"This may indicate a problem with the flowsheet structure. "
            f"Error: {e}"
        )


def _compute_order_from_connections(
    units: Dict[str, Any],
    connections: List[Dict[str, str]],
    tear_streams: Optional[List[Tuple[str, str]]] = None,
) -> List[str]:
    """Simple topological sort for session-only order planning.

    This is used ONLY when no model is built yet (session planning).
    For actual initialization, use _compute_order_with_sequential_decomposition.

    Args:
        units: Dict of unit_id -> unit instance (may be None)
        connections: List of connection dicts
        tear_streams: Optional tear stream edges

    Returns:
        List of unit IDs in topological order
    """
    from collections import defaultdict, deque

    tear_set = set(tear_streams) if tear_streams else set()

    # Build adjacency and in-degree
    in_degree = defaultdict(int)
    adj = defaultdict(list)

    # Initialize all units with 0 in-degree
    for unit_id in units:
        in_degree[unit_id] = 0

    # Build graph from connections
    for conn in connections:
        src = conn.get("src_unit")
        dst = conn.get("dest_unit")
        if src and dst:
            if (src, dst) not in tear_set:
                in_degree[dst] += 1
                adj[src].append(dst)

    # Kahn's algorithm
    queue = deque([uid for uid, deg in in_degree.items() if deg == 0])
    result = []

    while queue:
        unit_id = queue.popleft()
        result.append(unit_id)
        for downstream in adj[unit_id]:
            in_degree[downstream] -= 1
            if in_degree[downstream] == 0:
                queue.append(downstream)

    if len(result) != len(units):
        remaining = set(units.keys()) - set(result)
        raise SequentialDecompositionError(
            f"Cycle detected in flowsheet. Remaining units: {remaining}. "
            f"Specify tear_streams to break recycle loops."
        )

    return result


# Backward compatibility exports (but these raise errors if IDAES unavailable)
def get_sequential_decomposition_order(model: Any, tear_streams: Optional[List[Tuple[str, str]]] = None) -> List[str]:
    """Get initialization order using IDAES SequentialDecomposition.

    Args:
        model: Pyomo ConcreteModel with fs block
        tear_streams: Optional tear stream specification

    Returns:
        List of unit IDs in initialization order

    Raises:
        SequentialDecompositionError: If SequentialDecomposition unavailable or fails
    """
    return _compute_order_with_sequential_decomposition(model, tear_streams)
