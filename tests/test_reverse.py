"""Tests for iterative reverse-dependency traversal."""

import unittest
from pathlib import Path

from src.errors import ToolError
from src.graph import build_graph
from src.reverse import reverse_dependencies
from src.test_repository import TestRepository

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class ReverseDependenciesTests(unittest.TestCase):
    """Exercise direct and transitive ancestors, ordering, and errors."""

    def test_branching_cycles_and_duplicate_edges(self) -> None:
        """Visit each ancestor once and never include the target itself."""
        graph = {
            "A": ["B", "C", "C"],
            "B": ["C", "D", "D"],
            "C": ["A", "D"],
            "D": ["D"],
            "ISOLATED": [],
        }
        self.assertEqual(reverse_dependencies(graph, "D"), ["B", "A", "C"])
        self.assertEqual(reverse_dependencies(graph, "ISOLATED"), [])
        self.assertNotIn("A", reverse_dependencies(graph, "A"))
        self.assertEqual(reverse_dependencies({"SELF": ["SELF"]}, "SELF"), [])

    def test_order_follows_input_graph(self) -> None:
        """Traverse direct parents in graph insertion order, depth first."""
        graph = {
            "TOP": ["LEFT", "RIGHT"],
            "LEFT": ["END"],
            "RIGHT": ["END"],
            "END": [],
        }
        self.assertEqual(reverse_dependencies(graph, "END"),
                         ["LEFT", "TOP", "RIGHT"])
        self.assertEqual(reverse_dependencies(graph, "TOP"), [])

    def test_target_must_be_a_graph_key(self) -> None:
        """Reject absent targets, even if they appear only as edge values."""
        for graph, target in [({}, "X"), ({"A": ["X"]}, "X")]:
            with self.subTest(graph=graph):
                with self.assertRaises(ToolError):
                    reverse_dependencies(graph, target)

    def test_filtered_graph_limits_reverse_reachability(self) -> None:
        """Never include an excluded parent or its hidden descendants."""
        repository = TestRepository(EXAMPLES / "test_filter.csv")
        graph = build_graph("ROOT", repository.dependencies, "EXCLUDE")
        self.assertEqual(reverse_dependencies(graph, "SHARED"),
                         ["KEEP", "ROOT", "MID", "DEEP"])
        with self.assertRaises(ToolError):
            reverse_dependencies(graph, "HIDDEN")

    def test_deep_reverse_cycle_without_recursion(self) -> None:
        """Traverse more than 1500 ancestor levels without recursion."""
        depth = 2500
        graph = {f"N{i}": [f"N{i + 1}"] for i in range(depth)}
        graph[f"N{depth}"] = ["N0"]
        result = reverse_dependencies(graph, f"N{depth}")
        self.assertEqual(len(result), depth)
        self.assertEqual(result[0], f"N{depth - 1}")
        self.assertEqual(result[-1], "N0")
        self.assertNotIn(f"N{depth}", result)


if __name__ == "__main__":
    unittest.main()
