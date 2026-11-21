"""Tests for iterative dependency-graph construction."""

import unittest
from pathlib import Path
from unittest.mock import Mock

from src.graph import build_graph
from src.test_repository import TestRepository

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class BuildGraphTests(unittest.TestCase):
    """Exercise deterministic DFS, filtering, cycles and duplicates."""

    def test_depth_first_order_and_duplicate_edges(self) -> None:
        """Preserve first edge order and call each reachable node only once."""
        nodes = {
            "ROOT": ["A", "B", "A"],
            "A": ["C", "D", "C"],
            "B": ["C", "B"],
            "C": ["ROOT"],
            "D": ["D"],
            "OTHER": [],
        }
        get_dependencies = Mock(side_effect=nodes.__getitem__)
        graph = build_graph("ROOT", get_dependencies)
        self.assertEqual(list(graph), ["ROOT", "A", "C", "D", "B"])
        self.assertEqual(graph["ROOT"], ["A", "B"])
        self.assertEqual(graph["A"], ["C", "D"])
        self.assertEqual(graph["B"], ["C", "B"])
        self.assertEqual(graph["C"], ["ROOT"])
        self.assertEqual(graph["D"], ["D"])
        self.assertEqual(get_dependencies.call_count, len(graph))
        for children in graph.values():
            self.assertTrue(set(children).issubset(graph))

    def test_leaf_is_a_node(self) -> None:
        """A root with no dependencies still appears as a graph key."""
        self.assertEqual(build_graph("LEAF", lambda _: []), {"LEAF": []})

    def test_deep_cycle_without_recursion(self) -> None:
        """Build a cycle deeper than Python's normal recursion limit."""
        depth = 2500
        nodes = {f"N{i}": [f"N{i + 1}"] for i in range(depth)}
        nodes[f"N{depth}"] = ["N0"]
        graph = build_graph("N0", nodes.__getitem__)
        self.assertEqual(len(graph), depth + 1)
        self.assertEqual(graph[f"N{depth}"], ["N0"])

    def test_filtered_packages_are_not_expanded(self) -> None:
        """Remove matching packages and edges, but keep shared descendants."""
        repository = TestRepository(EXAMPLES / "test_filter.csv")
        get_dependencies = Mock(side_effect=repository.dependencies)
        graph = build_graph("ROOT", get_dependencies, "EXCLUDE")
        self.assertNotIn("EXCLUDE", graph)
        self.assertNotIn("HIDDEN", graph)
        self.assertEqual(graph["ROOT"], ["KEEP", "UPPER", "MID"])
        self.assertIn("SHARED", graph)
        self.assertIn("DEEP", graph)
        called = [call.args[0] for call in get_dependencies.call_args_list]
        self.assertNotIn("EXCLUDE", called)
        self.assertNotIn("HIDDEN", called)

    def test_uppercase_filter_is_case_sensitive(self) -> None:
        """Match substrings in uppercase package names without folding case."""
        repository = TestRepository(EXAMPLES / "test_filter.csv")
        graph = build_graph("ROOT", repository.dependencies, "UPPER")
        self.assertNotIn("UPPER", graph)
        self.assertNotIn("UPPERCASE", graph)
        self.assertNotIn("PRIVATE", graph)
        self.assertNotIn("SECRET", graph)
        self.assertEqual(graph["KEEP"], ["LEFT", "SHARED"])
        lower = build_graph("ROOT", repository.dependencies, "upper")
        self.assertIn("UPPER", lower)
        self.assertIn("UPPERCASE", lower)

    def test_matching_root_returns_empty_without_expansion(self) -> None:
        """A filtered root produces an empty graph and no callback calls."""
        get_dependencies = Mock(side_effect=AssertionError("must not call"))
        self.assertEqual(build_graph("ROOT", get_dependencies, "ROOT"), {})
        get_dependencies.assert_not_called()

    def test_whole_coordinate_is_matched(self) -> None:
        """Group, artifact and version are all part of the identifier."""
        nodes = {
            "org:root:1": ["UPPER.group:art:1", "org:UPPER:2"],
            "UPPER.group:art:1": [],
            "org:UPPER:2": [],
        }
        graph = build_graph("org:root:1", nodes.__getitem__, "UPPER")
        self.assertEqual(graph, {"org:root:1": []})
        self.assertEqual(build_graph("org:root:1", nodes.__getitem__, ":1"), {})

    def test_acyclic_and_cycle_fixtures(self) -> None:
        """Exercise branching, disconnected nodes, sharing and self-loops."""
        acyclic = TestRepository(EXAMPLES / "test_acyclic.csv")
        graph = build_graph("ROOT", acyclic.dependencies)
        self.assertIn("LAMBDA", graph)
        self.assertNotIn("ISOLATED", graph)
        cycle = TestRepository(EXAMPLES / "test_cycle.csv")
        graph = build_graph("ROOT", cycle.dependencies)
        self.assertEqual(graph["GAMMA"], ["ROOT"])
        self.assertEqual(graph["SELF"], ["SELF"])
        self.assertNotIn("ORPHAN", graph)
        self.assertNotIn("ISOLATED", graph)


if __name__ == "__main__":
    unittest.main()
