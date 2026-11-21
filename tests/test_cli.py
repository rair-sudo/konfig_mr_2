"""Интеграционные проверки CLI с локальным CSV-репозиторием."""

import csv
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as element_tree


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
        self.prepare_case(self.base)

    def prepare_case(self, directory):
        """Создаёт независимую конфигурацию и репозиторий в каталоге."""
        self.config = directory / "config.csv"
        self.repo = directory / "repository.csv"
        self.svg = directory / "results" / "graph.svg"
        self.d2 = self.svg.with_suffix(".d2")
        self.values = {
            "package": "ROOT", "repository": self.repo.name,
            "test_mode": "true", "output": "results/graph.svg", "filter": "",
        }
        self.write_config()
        self.write_repository(ACYCLIC)

    def copy_project(self):
        """Создаёт изолированную копию исходников с тестовыми данными."""
        project = self.base / "проект с пробелами"
        project.mkdir()
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
        shutil.copytree(PROJECT / "src", project / "src", ignore=ignore)
        self.prepare_case(project)
        return project

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
            variants = [str(path)]
            for start in (PROJECT, self.base):
                try:
                    variants.append(os.path.relpath(path, start))
                except ValueError:
                    pass
            patterns = [r"(?<![\w/\\])" + re.escape(value) + r"(?![\w/\\])"
                        for value in variants]
            flags = re.IGNORECASE if os.name == "nt" else 0
            self.assertTrue(any(re.search(p, result.stdout, flags)
                                for p in patterns),
                            result.stdout)

    def assert_graph_files(self, graph, excluded=()):
        """Сверяет XML, подписи SVG и вершины и рёбра D2 с ожидаемым графом."""
        self.assertTrue(self.svg.is_file())
        self.assertTrue(self.d2.is_file())
        root = element_tree.parse(self.svg).getroot()
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

    def prepare_collision(self, source_name, suffix, link=None):
        """Настраивает совпадение результата с входом или его ссылкой."""
        source = self.config if source_name == "config" else self.repo
        target = source.with_suffix(suffix)
        if link is None:
            source.rename(target)
            if source_name == "config":
                self.config = target
            else:
                self.repo = target
                self.values["repository"] = target.name
        else:
            target = source.parent / ("alias" + suffix)
        self.values["output"] = target.with_suffix(".svg").name
        self.svg = target.with_suffix(".svg")
        self.d2 = self.svg.with_suffix(".d2")
        self.write_config()
        if link is not None:
            try:
                link(source, target)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"Ссылки недоступны: {error}")
        return target

    def assert_collision_blocked(self):
        """Проверяет ошибку защиты и отсутствие записей с флагом и без."""
        for args in ((), ("--force",)):
            with self.subTest(args=args):
                before = _snapshot(self.base)
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("совпадает с исходным файлом", result.stderr)
                self.assertEqual(_snapshot(self.base), before)

    def assert_collisions(self, link=None):
        """Проверяет конфигурацию и репозиторий для обоих результатов."""
        for source in ("config", "repository"):
            for suffix in (".svg", ".d2"):
                with self.subTest(source=source, suffix=suffix):
                    with tempfile.TemporaryDirectory(dir=self.base) as name:
                        self.prepare_case(Path(name))
                        target = self.prepare_collision(source, suffix, link)
                        self.assert_collision_blocked()
                        original = (self.config if source == "config"
                                    else self.repo)
                        self.assertTrue(target.samefile(original))
                        if link is os.symlink:
                            self.assertTrue(target.is_symlink())

    def test_successful_graph_and_reported_paths(self):
        """Граф из корня сохраняется в два файла, пути выводятся в stdout."""
        result = self.run_cli()
        self.assert_success(result)
        self.assert_output_paths(result)
        graph = {node: deps for node, deps in ACYCLIC.items()
                 if node != "ISOLATED"}
        self.assert_graph_files(graph, {"ISOLATED"})

    def test_output_path_assertion_accepts_different_drives(self):
        """Ошибка relpath для разных дисков не мешает проверке абсолютного."""
        result = self.run_cli()
        self.assert_success(result)
        error = ValueError("different drive")
        with patch("os.path.relpath", side_effect=error):
            self.assert_output_paths(result)

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

    def test_no_overwrite_overrides_launcher_force(self):
        """Явная защита отменяет --force, переданный скриптом запуска."""
        self.assert_success(self.run_cli("--force"))
        before = _snapshot(self.base)
        result = self.run_cli("--force", "--no-overwrite")
        self.assert_error(result)
        self.assertEqual(_snapshot(self.base), before)

    def test_default_config_is_found_from_another_directory(self):
        """Стандартная конфигурация ищется рядом с проектом, не в cwd."""
        project = self.copy_project()
        other = self.base / "другой каталог"
        other.mkdir()
        (other / "config.csv").write_text("wrong config", encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(project),
                   PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
                   PYTHONNOUSERSITE="1")
        env.pop("PYTHONHOME", None)
        before = _snapshot(self.base)
        result = subprocess.run(
            [sys.executable, "-m", "src.main", "--reverse", "TARGET"],
            cwd=other, env=env, capture_output=True, text=True,
            encoding="utf-8", timeout=15, check=False,
        )
        expected = {"ROOT", "ALPHA", "MIDDLE", "BETA"}
        self.assert_ancestors(result, "TARGET", expected, {"ISOLATED"})
        self.assertEqual(_snapshot(self.base), before)

    def test_windows_launcher_content_uses_pushd_and_force(self):
        """Содержимое BAT указывает каталог проекта и включает замену."""
        script = (PROJECT / "run.bat").read_text(encoding="utf-8")
        self.assertIn('pushd "%~dp0"', script)
        self.assertIn("-m src.main --force %*", script)

    @unittest.skipUnless(os.name == "nt", "Требуется Windows и cmd.exe")
    def test_windows_launcher_executes_twice_from_another_directory(self):
        """Настоящий cmd.exe запускает копию BAT дважды из другого cwd."""
        project = self.copy_project()
        launcher = project / "run.bat"
        shutil.copy2(PROJECT / "run.bat", launcher)
        other = self.base / "другой каталог"
        other.mkdir()
        python_dir = Path(sys.executable).parent
        if not (python_dir / "python.exe").is_file():
            self.skipTest("Рядом с интерпретатором нет python.exe")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                   PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1")
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env["PATH"] = str(python_dir) + os.pathsep + env.get("PATH", "")
        command = f'"{env.get("COMSPEC", "cmd.exe")}" /d /s /c '
        command += f'""{launcher}""'
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                if attempt:
                    self.svg.write_bytes(b"old svg")
                    self.d2.write_bytes(b"old d2")
                result = subprocess.run(
                    command, cwd=other, env=env, capture_output=True,
                    text=True, encoding="utf-8", errors="replace",
                    timeout=30, check=False,
                )
                self.assert_success(result)
                self.assert_output_paths(result)
                graph = {node: deps for node, deps in ACYCLIC.items()
                         if node != "ISOLATED"}
                self.assert_graph_files(graph, {"ISOLATED"})

    def test_source_paths_cannot_be_outputs(self):
        """Оба результата не могут перезаписать конфигурацию и репозиторий."""
        self.assert_collisions()

    def test_symlinked_source_aliases_cannot_be_outputs(self):
        """Символические ссылки на исходники тоже запрещены как результаты."""
        self.assert_collisions(os.symlink)

    def test_hardlinked_source_aliases_cannot_be_outputs(self):
        """Жёсткие ссылки на исходники тоже запрещены как результаты."""
        self.assert_collisions(os.link)

    def test_input_config_symlink_is_protected(self):
        """Конфигурация, указанная через симлинк, сохраняется целой."""
        source = self.prepare_collision("config", ".svg")
        alias = self.base / "config-link.csv"
        try:
            os.symlink(source, alias)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Символические ссылки недоступны: {error}")
        self.config = alias
        self.assert_collision_blocked()
        self.assertTrue(alias.is_symlink())
        self.assertTrue(alias.samefile(source))

    def test_input_repository_symlink_is_protected(self):
        """Репозиторий, указанный через симлинк, сохраняется целым."""
        source = self.prepare_collision("repository", ".d2")
        alias = self.base / "repo-link.csv"
        try:
            os.symlink(source, alias)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Символические ссылки недоступны: {error}")
        self.values["repository"] = alias.name
        self.write_config()
        self.assert_collision_blocked()
        self.assertTrue(alias.is_symlink())
        self.assertTrue(alias.samefile(source))

    def test_normalized_output_path_cannot_replace_config(self):
        """Сегменты пути не позволяют обойти защиту конфигурации."""
        source = self.prepare_collision("config", ".svg")
        (self.base / "nested").mkdir()
        self.values["output"] = f"nested/../{source.name}"
        self.write_config()
        self.assert_collision_blocked()

    @unittest.skipUnless(os.name == "nt", "Регистр Windows-путей")
    def test_windows_case_insensitive_collision(self):
        """Разный регистр выходного и исходного имени не обходит защиту."""
        source = self.prepare_collision("config", ".svg")
        other_case = source.with_name(source.name.upper())
        if not other_case.exists():
            self.skipTest("Каталог чувствителен к регистру")
        self.values["output"] = other_case.name
        self.write_config()
        self.assert_collision_blocked()

    def test_reverse_ignores_source_output_collisions(self):
        """Обратный поиск не записывает файлы и допускает коллизии."""
        for source in ("config", "repository"):
            for suffix in (".svg", ".d2"):
                with self.subTest(source=source, suffix=suffix):
                    with tempfile.TemporaryDirectory(dir=self.base) as name:
                        self.prepare_case(Path(name))
                        self.prepare_collision(source, suffix)
                        before = _snapshot(self.base)
                        result = self.run_cli("--reverse", "TARGET", "--force")
                        self.assert_ancestors(
                            result, "TARGET", {"ROOT", "ALPHA", "MIDDLE",
                                               "BETA"}, {"ISOLATED"})
                        self.assertEqual(_snapshot(self.base), before)

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
