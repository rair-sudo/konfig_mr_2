"""Find reverse dependencies in an already-built dependency graph."""

from src.errors import ToolError


def reverse_dependencies(graph: dict, target: str) -> list[str]:
    """Return unique ancestors in deterministic iterative DFS order.

    Only graph keys are considered nodes; the target is never its own
    ancestor, even if a cycle or self-loop reaches it.
    """
    if target not in graph:
        raise ToolError(f"Unknown graph node: {target}")
    parents: dict[str, list[str]] = {node: [] for node in graph}
    for node, children in graph.items():
        for child in children:
            if child in parents:
                parents[child].append(node)
    result: list[str] = []
    visited = {target}
    stack = list(reversed(parents[target]))
    while stack:
        node = stack.pop()
        if node in visited:
            continue
        visited.add(node)
        result.append(node)
        stack.extend(reversed(parents[node]))
    return result
