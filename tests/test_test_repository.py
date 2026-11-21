"""Tests for the local UTF-8 CSV test repository."""

import io
import unittest
from pathlib import Path
from unittest.mock import patch

from src.errors import ToolError
from src.test_repository import TestRepository

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def load_text(text: str) -> TestRepository:
    """Load CSV text in memory without creating additional files."""
    with patch.object(Path, "open", return_value=io.StringIO(text)):
        return TestRepository("mock.csv")


class TestRepositoryTests(unittest.TestCase):
    """Verify complete CSV validation and safe dependency lookup."""

    def test_fixtures_and_both_path_types(self) -> None:
        """Read each provided fixture using path strings and Path objects."""
        expected = {
            "test_acyclic.csv": ["ALPHA", "BETA", "GAMMA"],
            "test_cycle.csv": ["ALPHA", "BETA", "SELF"],
            "test_filter.csv": ["KEEP", "EXCLUDE", "UPPER", "MID"],
        }
        for name, children in expected.items():
            for path in (EXAMPLES / name, str(EXAMPLES / name)):
                with self.subTest(path=path):
                    repo = TestRepository(path)
                    self.assertEqual(repo.dependencies("ROOT"), children)
                    self.assertEqual(repo.dependencies("ISOLATED"), [])

    def test_order_leaves_quotes_and_crlf(self) -> None:
        """Accept valid quoting, CRLF line endings, and empty leaf fields."""
        text = 'package,dependencies\r\nA,"B;C"\r\nB,\r\nC,""\r\n'
        repo = load_text(text)
        self.assertEqual(repo.dependencies("A"), ["B", "C"])
        self.assertEqual(repo.dependencies("B"), [])
        self.assertEqual(repo.dependencies("C"), [])

    def test_result_is_a_copy(self) -> None:
        """Mutating a returned list must not modify the repository."""
        repo = load_text("package,dependencies\nA,B\nB,\n")
        repo.dependencies("A").clear()
        self.assertEqual(repo.dependencies("A"), ["B"])

    def test_unknown_package_raises_tool_error(self) -> None:
        """Report missing package lookups rather than a raw KeyError."""
        repo = load_text("package,dependencies\nA,\n")
        with self.assertRaises(ToolError):
            repo.dependencies("MISSING")

    def test_missing_or_unreadable_file(self) -> None:
        """Wrap missing files and other file errors as ToolError."""
        missing = EXAMPLES / "no_such_test_repository.csv"
        with self.assertRaises(ToolError):
            TestRepository(missing)
        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            with self.assertRaises(ToolError):
                TestRepository("unreadable.csv")

    def test_invalid_utf8_raises_tool_error(self) -> None:
        """Wrap UTF-8 decoding failures as ToolError."""
        raw = io.BytesIO(b"package,dependencies\nA,\xff\n")
        source = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        with patch.object(Path, "open", return_value=source):
            with self.assertRaises(ToolError):
                TestRepository("invalid-utf8.csv")

    def test_invalid_csv_structure(self) -> None:
        """Reject malformed quotes, wrong headers, and malformed rows."""
        cases = [
            "",
            "package\nA,\n",
            "dependencies,package\n,A\n",
            "package,dependencies,extra\nA,,\n",
            "Package,dependencies\nA,\n",
            "package,dependencies\nA\n",
            "package,dependencies\nA,,extra\n",
            "package,dependencies\nA,\n\nB,\n",
            'package,dependencies\nA,"B\nB,\n',
            'package,dependencies\nA,"B"x\nB,\n',
        ]
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(ToolError):
                    load_text(text)

    def test_invalid_names_duplicates_and_missing_nodes(self) -> None:
        """Reject invalid identifiers, repeated records and broken edges."""
        rows = [
            ",\n",
            "a,\n",
            "A1,\n",
            "A_B,\n",
            " A,\n",
            "É,\n",
            "A,b\n",
            "A,B1\n",
            "A,B;\nB,\n",
            "A,;B\nB,\n",
            "A,B;;C\nB,\nC,\n",
            "A,B; B\nB,\n",
            "A,B;B\nB,\n",
            "A,\nA,\n",
            "A,B\n",
        ]
        for row in rows:
            with self.subTest(row=row):
                with self.assertRaises(ToolError):
                    load_text("package,dependencies\n" + row)


if __name__ == "__main__":
    unittest.main()
