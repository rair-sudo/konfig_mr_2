"""Проверки именования и числовых литералов в сравнениях."""

import ast
from pathlib import Path
import re
import unittest


PROJECT = Path(__file__).resolve().parents[1]
CLASS_NAME = re.compile(r"[A-Z][A-Za-z0-9]*\Z")
IMPORT_ALIAS = re.compile(r"[a-z][a-z0-9_]*\Z")
TYPE_CONTAINERS = {"dict", "list", "tuple", "set", "frozenset"}


def python_files():
    """Возвращает исходные Python-файлы и тесты проекта."""
    return sorted(path for folder in ("src", "tests")
                  for path in (PROJECT / folder).glob("*.py"))


def parsed_file(path):
    """Разбирает Python-файл для проверки его структуры."""
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def container_alias_names(node):
    """Извлекает имена алиасов встроенных контейнерных типов."""
    if not isinstance(node.value, ast.Subscript):
        return []
    base = node.value.value
    if not isinstance(base, ast.Name) or base.id not in TYPE_CONTAINERS:
        return []
    return [(target.id, node.lineno) for target in node.targets
            if isinstance(target, ast.Name)]


def type_alias_names(tree):
    """Возвращает имена простых алиасов типов верхнего уровня."""
    names = []
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            annotation = node.annotation
            if (isinstance(node.target, ast.Name)
                    and isinstance(annotation, ast.Name)
                    and annotation.id == "TypeAlias"):
                names.append((node.target.id, node.lineno))
        elif isinstance(node, ast.Assign):
            names.extend(container_alias_names(node))
    return names


class CodeStyleTests(unittest.TestCase):
    """Проверяет требования М1 и отмеченные случаи правила И1."""

    def test_comparisons_use_named_numbers(self):
        """В выражениях сравнения числовые значения имеют имена."""
        problems = []
        for path in python_files():
            for node in ast.walk(parsed_file(path)):
                if not isinstance(node, ast.Compare):
                    continue
                for part in ast.walk(node):
                    if (isinstance(part, ast.Constant)
                            and type(part.value) in (int, float, complex)):
                        problems.append(f"{path.name}:{part.lineno}")
        self.assertEqual(problems, [])

    def test_class_names_use_pascal_case(self):
        """Имена объявленных классов соответствуют ожидаемому PascalCase."""
        problems = []
        for path in python_files():
            for node in ast.walk(parsed_file(path)):
                if isinstance(node, ast.ClassDef):
                    if not CLASS_NAME.fullmatch(node.name):
                        message = f"{path.name}:{node.lineno} {node.name}"
                        problems.append(message)
        self.assertEqual(problems, [])

    def test_import_aliases_use_snake_case(self):
        """Псевдонимы импортированных модулей записываются в snake_case."""
        problems = []
        for path in python_files():
            for node in ast.walk(parsed_file(path)):
                if not isinstance(node, ast.Import):
                    continue
                for alias in node.names:
                    name = alias.asname
                    if name and not IMPORT_ALIAS.fullmatch(name):
                        problems.append(f"{path.name}:{node.lineno}")
        self.assertEqual(problems, [])

    def test_type_alias_names_use_pascal_case(self):
        """Алиасы типов используют имена без начального подчёркивания."""
        problems = []
        for path in python_files():
            for name, line in type_alias_names(parsed_file(path)):
                if not CLASS_NAME.fullmatch(name):
                    problems.append(f"{path.name}:{line} {name}")
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
