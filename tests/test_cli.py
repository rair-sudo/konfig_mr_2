"""Интеграционные проверки CLI с локальным CSV-репозиторием."""

import csv
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


PROJECT = Path(__file__).resolve().parents[1]
SVG_NS = "{http://www.w3.org/2000/svg}"
ACYCLIC = {
    "ROOT": ["ALPHA", "BETA"],
    "ALPHA": ["MIDDLE"],
    "MIDDLE": ["TARGET"],
    "BETA": ["TARGET"],
    "TARGET": [],
    "ISOLATED": ["TARGET"],
}
CYCLE = {
    "ROOT": ["ALPHA", "BETA"],
    "ALPHA": ["MIDDLE"],
    "MIDDLE": ["ROOT", "TARGET"],
    "BETA": ["TARGET"],
    "TARGET": [],
    "ISOLATED": ["TARGET"],
}
FILTERED = {
    "ROOT": ["KEEP", "EXCLUDE", "BRANCH"],
    "KEEP": ["TARGET"],
    "EXCLUDE": ["HIDDEN", "TARGET"],
    "HIDDEN": [],
    "BRANCH": ["TARGET"],
    "TARGET": [],
    "ISOLATED": ["TARGET"],
}


def _snapshot(directory):
    """Сохраняет имена, содержимое файлов и наличие каталогов."""
    return {
        str(path.relative_to(directory)):
        path.read_bytes() if path.is_file() else None
        for path in directory.rglob("*")
    }


def _d2_parts(text):
    """Читает узлы и рёбра создаваемого проектом D2-представления."""
    nodes = dict(re.findall(r'^\s*(\w+):\s*"([A-Z]+)"\s*$', text, re.M))
    edges = re.findall(r'^\s*(\w+)\s*->\s*(\w+)\s*$', text, re.M)
    pairs = [(nodes[src], nodes[dst]) for src, dst in edges]
    return set(nodes.values()), pairs


class CliTests(unittest.TestCase):
    """Проверяет внешний процесс и его взаимодействие с файлами."""

    def setUp(self):
        """Создаёт отдельную конфигурацию и локальный тестовый репозиторий."""
        temporary = tempfile.TemporaryDirectory(prefix="cli test ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.config = self.base / "config.csv"
        self.repo = self.base / "repository.csv"
        self.svg = self.base / "results" / "graph.svg"
        self.d2 = self.svg.with_suffix(".d2")
        self.values = {
            "package": "ROOT", "repository": self.repo.name,
            "test_mode": "true", "output": "results/graph.svg", "filter": "",
        }
        self.write_config()
        self.write_repository(ACYCLIC)

    def write_config(self):
        """Записывает CSV-конфигурацию в UTF-8."""
        with self.config.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["key", "value"])
            writer.writerows(self.values.items())

    def write_repository(self, graph):
        """Записывает заданный граф в локальный CSV-репозиторий."""
        with self.repo.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["package", "dependencies"])
            writer.writerows((node, ";".join(deps))
                             for node, deps in graph.items())

    def run_cli(self, *args):
        """Запускает модуль из каталога проекта без записи Python-кеша."""
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                   PYTHONIOENCODING="utf-8")
        return subprocess.run(
            [sys.executable, "-m", "src.main", "--config", str(self.config),
             *args],
            cwd=PROJECT, env=env, capture_output=True, text=True,
            encoding="utf-8", timeout=15, check=False,
        )

    def assert_success(self, result):
        """Проверяет код завершения, включая вывод для диагностики."""
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_error(self, result):
        """Проверяет ненулевой код и сообщение в стандартном потоке ошибок."""
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertTrue(result.stderr.strip(), "Ожидалось сообщение в stderr")

    def assert_output_paths(self, result):
        """Проверяет, что напечатаны пути к обоим результатам."""
        for path in (self.svg, self.d2):
            variants = (str(path), os.path.relpath(path, PROJECT),
                        os.path.relpath(path, self.base))
            patterns = [r"(?<![\w/\\])" + re.escape(value) + r"(?![\w/\\])"
                        for value in variants]
            self.assertTrue(any(re.search(p, result.stdout) for p in patterns),
                            result.stdout)

    def assert_graph_files(self, graph, excluded=()):
        """Сверяет XML, подписи SVG и вершины и рёбра D2 с ожидаемым графом."""
        self.assertTrue(self.svg.is_file())
        self.assertTrue(self.d2.is_file())
        root = ET.parse(self.svg).getroot()
        self.assertEqual(root.tag, f"{SVG_NS}svg")
        labels = {item.text for item in root.iter() if item.text}
        for node in graph:
            self.assertIn(node, labels)
        for node in excluded:
            self.assertNotIn(node, labels)
        nodes, edges = _d2_parts(self.d2.read_text(encoding="utf-8"))
        expected = [(node, child) for node, children in graph.items()
                    for child in children]
        self.assertEqual(nodes, set(graph))
        self.assertCountEqual(edges, expected)

    def assert_ancestors(self, result, target, expected, absent):
        """Проверяет предков по именам без зависимости от их порядка."""
        self.assert_success(result)
        words = re.findall(r"\b[A-Z]+\b", result.stdout)
        for node in expected:
            self.assertEqual(words.count(node), 1, result.stdout)
        for node in absent:
            self.assertNotIn(node, words, result.stdout)
        self.assertLessEqual(words.count(target), 1, result.stdout)
        self.assertNotIn(target, result.stdout.splitlines())

    def test_successful_graph_and_reported_paths(self):
        """Граф из корня сохраняется в два файла, пути выводятся в stdout."""
        result = self.run_cli()
        self.assert_success(result)
        self.assert_output_paths(result)
        graph = {node: deps for node, deps in ACYCLIC.items()
                 if node != "ISOLATED"}
        self.assert_graph_files(graph, {"ISOLATED"})

    def test_cycle_is_rendered_and_reverse_excludes_target(self):
        """Цикл завершается, сохраняется и не делает цель своим предком."""
        self.write_repository(CYCLE)
        result = self.run_cli()
        self.assert_success(result)
        graph = {node: deps for node, deps in CYCLE.items()
                 if node != "ISOLATED"}
        self.assert_graph_files(graph, {"ISOLATED"})
        self.svg.write_bytes(b"keep existing SVG")
        self.d2.write_bytes(b"keep existing D2")
        before = _snapshot(self.base)
        result = self.run_cli("--reverse", "ROOT", "--force")
        self.assert_ancestors(result, "ROOT", {"MIDDLE", "ALPHA"},
                              {"BETA", "TARGET", "ISOLATED"})
        self.assertEqual(_snapshot(self.base), before)

    def test_reverse_direct_transitive_and_no_files(self):
        """Предки включают прямых и транзитивных, но не посторонние узлы."""
        before = _snapshot(self.base)
        result = self.run_cli("--reverse", "TARGET")
        self.assert_ancestors(result, "TARGET",
                              {"ROOT", "ALPHA", "MIDDLE", "BETA"},
                              {"ISOLATED"})
        self.assertEqual(_snapshot(self.base), before)

    def test_filter_prunes_graph_and_reverse_search(self):
        """Фильтр удаляет ветку, сохраняя разделяемую зависимость."""
        self.write_repository(FILTERED)
        self.values["filter"] = "EXCLUDE"
        self.write_config()
        result = self.run_cli()
        self.assert_success(result)
        graph = {
            "ROOT": ["KEEP", "BRANCH"], "KEEP": ["TARGET"],
            "BRANCH": ["TARGET"], "TARGET": [],
        }
        self.assert_graph_files(graph, {"EXCLUDE", "HIDDEN", "ISOLATED"})
        before = _snapshot(self.base)
        result = self.run_cli("--reverse", "TARGET")
        self.assert_ancestors(result, "TARGET", {"ROOT", "KEEP", "BRANCH"},
                              {"EXCLUDE", "HIDDEN", "ISOLATED"})
        self.assertEqual(_snapshot(self.base), before)

    def test_filter_matching_root_yields_empty_graph(self):
        """Совпадение фильтра с корнем даёт пустой граф и неизвестную цель."""
        self.values["filter"] = "ROOT"
        self.write_config()
        result = self.run_cli()
        self.assert_success(result)
        self.assert_graph_files({}, set(ACYCLIC))
        before = _snapshot(self.base)
        result = self.run_cli("--reverse", "ROOT")
        self.assert_error(result)
        self.assertEqual(_snapshot(self.base), before)

    def test_unknown_reverse_targets_are_errors_without_writes(self):
        """Отсутствующая, недостижимая и отфильтрованная цели неизвестны."""
        self.write_repository(FILTERED)
        self.values["filter"] = "EXCLUDE"
        self.write_config()
        for target in ("UNKNOWN", "ISOLATED", "EXCLUDE", "HIDDEN"):
            with self.subTest(target=target):
                before = _snapshot(self.base)
                result = self.run_cli("--reverse", target)
                self.assert_error(result)
                self.assertEqual(_snapshot(self.base), before)

    def test_invalid_command_line_parameters(self):
        """Неизвестные аргументы и пропущенные значения приводят к ошибке."""
        cases = [("--unknown",), ("--config",), ("--reverse",),
                 ("--force", "extra")]
        for args in cases:
            with self.subTest(args=args):
                before = _snapshot(self.base)
                result = self.run_cli(*args)
                self.assert_error(result)
                self.assertEqual(_snapshot(self.base), before)

    def test_invalid_config_parameters(self):
        """Ошибки конфигурации не создают результатов и сообщаются в stderr."""
        cases = {"package": "lowercase", "repository": "missing.csv",
                 "test_mode": "maybe", "output": "graph.png",
                 "filter": "x" * 257}
        original = self.values.copy()
        for key, value in cases.items():
            with self.subTest(key=key):
                self.values = dict(original, **{key: value})
                self.write_config()
                before = _snapshot(self.base)
                result = self.run_cli()
                self.assert_error(result)
                self.assertEqual(_snapshot(self.base), before)

    def test_repeat_requires_force_and_force_replaces_both(self):
        """Повтор без флага сохраняет файлы; --force обновляет оба."""
        self.assert_success(self.run_cli())
        before = _snapshot(self.base)
        original_svg, original_d2 = self.svg.read_bytes(), self.d2.read_bytes()
        result = self.run_cli()
        self.assert_error(result)
        self.assertEqual(_snapshot(self.base), before)
        changed = dict(ACYCLIC, BETA=["TARGET", "NEW"], NEW=[])
        self.write_repository(changed)
        result = self.run_cli("--force")
        self.assert_success(result)
        self.assert_output_paths(result)
        graph = {node: deps for node, deps in changed.items()
                 if node != "ISOLATED"}
        self.assert_graph_files(graph)
        self.assertNotEqual(self.svg.read_bytes(), original_svg)
        self.assertNotEqual(self.d2.read_bytes(), original_d2)

    def test_either_existing_output_is_protected(self):
        """При конфликте одного файла второй не создаётся, первый цел."""
        self.svg.parent.mkdir()
        for existing, missing in ((self.svg, self.d2), (self.d2, self.svg)):
            with self.subTest(existing=existing.suffix):
                existing.write_bytes(b"original bytes\x00\xff")
                self.addCleanup(existing.unlink, missing_ok=True)
                before = _snapshot(self.base)
                result = self.run_cli()
                self.assert_error(result)
                self.assertEqual(_snapshot(self.base), before)
                self.assertFalse(missing.exists())
                self.assertEqual(existing.read_bytes(),
                                 b"original bytes\x00\xff")
                existing.unlink()


if __name__ == "__main__":
    unittest.main()
