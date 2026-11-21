"""Build dependency graphs with an iterative depth-first traversal."""

from collections.abc import Callable


def build_graph(
    root: str,
    dependencies: Callable[[str], list[str]],
    substring: str = "",
) -> dict[str, list[str]]:
    """Return the reachable graph in deterministic DFS discovery order.

    Nonempty ``substring`` is matched case-sensitively against the whole
    package identifier, including all parts of Maven coordinates. Matching
    packages and their otherwise unreachable subgraphs are excluded.
    Dependencies retain their first-occurrence order; duplicates are removed.
    """
    if substring and substring in root:
        return {}
    graph: dict[str, list[str]] = {}
    stack = [root]
    while stack:
        package = stack.pop()
        if package in graph:
            continue
        children: list[str] = []
        seen: set[str] = set()
        for child in dependencies(package):
            if child in seen or (substring and substring in child):
                continue
            seen.add(child)
            children.append(child)
        graph[package] = children
        stack.extend(reversed(children))
    return graph
