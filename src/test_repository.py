"""Load a strict, local CSV repository for dependency-graph tests."""

import csv
import re
from pathlib import Path

from src.errors import ToolError

_NAME = re.compile(r"[A-Z]+")
_FIELD_COUNT = 2


def _valid_name(name: str) -> bool:
    """Accept only nonempty names made from uppercase Latin letters."""
    return _NAME.fullmatch(name) is not None


def _parse_row(row: list[str], data: dict[str, list[str]]) -> None:
    """Validate and add exactly one CSV row to the repository."""
    if len(row) != _FIELD_COUNT:
        raise ToolError("Each CSV row must have exactly two fields")
    package, field = row
    if not _valid_name(package):
        raise ToolError(f"Invalid package name: {package!r}")
    if package in data:
        raise ToolError(f"Duplicate package: {package}")
    children = field.split(";") if field else []
    if any(not _valid_name(child) for child in children):
        raise ToolError(f"Invalid dependency name for {package}")
    if len(children) != len(set(children)):
        raise ToolError(f"Duplicate dependency for {package}")
    data[package] = children


def _validate_references(data: dict[str, list[str]]) -> None:
    """Reject dependencies without a corresponding package row."""
    for package, children in data.items():
        for child in children:
            if child not in data:
                message = f"Missing package {child} required by {package}"
                raise ToolError(message)


def _load_repository(path: Path) -> dict[str, list[str]]:
    """Read and validate one UTF-8 CSV file, including all references."""
    data: dict[str, list[str]] = {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            rows = csv.reader(source, strict=True)
            if next(rows, None) != ["package", "dependencies"]:
                raise ToolError("CSV header must be package,dependencies")
            for row in rows:
                try:
                    _parse_row(row, data)
                except ToolError as exc:
                    raise ToolError(f"line {rows.line_num}: {exc}") from exc
        _validate_references(data)
    except ToolError as exc:
        raise ToolError(f"CSV repository {path}: {exc}") from exc
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ToolError(f"Cannot read CSV repository {path}: {exc}") from exc
    return data


class TestRepository:
    """A validated, in-memory repository backed by a UTF-8 CSV file.

    The header is exactly ``package,dependencies``. Each package appears once;
    the second field is empty or contains unique names separated by ``;``.
    Names contain only uppercase Latin letters, without whitespace or digits.
    Every referenced dependency must also have a row in the same file.
    """

    def __init__(self, path: str | Path) -> None:
        """Load the entire repository, reporting invalid input as ToolError."""
        self._data = _load_repository(Path(path))

    def dependencies(self, package: str) -> list[str]:
        """Return a copy of a package's ordered dependencies or raise."""
        if package not in self._data:
            raise ToolError(f"Unknown package: {package}")
        return self._data[package].copy()
