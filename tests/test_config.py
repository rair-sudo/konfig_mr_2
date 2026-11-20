"""Проверки настроек, ошибок и разрешения относительных путей."""

import csv
from pathlib import Path
import tempfile
import unittest

from src.config import describe, load_config
from src.errors import ToolError


class ConfigTests(unittest.TestCase):
    """Проверки пяти обязательных параметров CSV."""

    def setUp(self):
        """Создаёт временный файл репозитория и стандартные параметры."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / "repo.csv").write_text("package,dependencies\nA,\n")
        self.values = {
            "package": "A", "repository": "repo.csv", "test_mode": "true",
            "output": "results/graph.svg", "filter": "",
        }

    def write_config(self, values=None):
        """Записывает словарь настроек и возвращает путь."""
        path = self.base / "config.csv"
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["key", "value"])
            writer.writerows((values or self.values).items())
        return path

    def test_test_mode_and_relative_paths(self):
        """Все параметры показываются, пути относятся к каталогу настроек."""
        config = load_config(self.write_config())
        self.assertTrue(config.test_mode)
        self.assertEqual(config.repository_path(), self.base / "repo.csv")
        self.assertEqual(config.output_path(), self.base / "results/graph.svg")
        for key in self.values:
            self.assertIn(f"{key}=", describe(config))

    def test_real_mode(self):
        """В реальном режиме принимаются Maven-координаты и HTTPS URL."""
        self.values.update({
            "package": "junit:junit:4.13.2",
            "repository": "https://repo.maven.apache.org/maven2/",
            "test_mode": "false",
        })
        config = load_config(self.write_config())
        self.assertFalse(config.test_mode)

    def test_bad_parameters(self):
        """Каждый из пяти параметров способен выдать понятную ошибку."""
        cases = {
            "package": "a1", "repository": "missing.csv", "test_mode": "yes",
            "output": "bad?.png", "filter": "bad\x00text",
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                values = dict(self.values, **{key: value})
                with self.assertRaisesRegex(ToolError, key):
                    load_config(self.write_config(values))

    def test_bad_maven_coordinates_and_urls(self):
        """Некорректные координаты и небезопасные адреса отклоняются."""
        self.values.update({"test_mode": "false", "package": "g:a:1",
                            "repository": "https://example.org/maven"})
        for package in ("g:a", "g:a:../1", "g:a:", "g/a:a:1"):
            with self.subTest(package=package), self.assertRaises(ToolError):
                values = dict(self.values, package=package)
                load_config(self.write_config(values))
        for url in ("file:///repo", "http://", "https://user@host/",
                    "https://host/?q=1", "https://host:bad/", "https://a b/"):
            with self.subTest(url=url), self.assertRaises(ToolError):
                values = dict(self.values, repository=url)
                load_config(self.write_config(values))

    def test_bad_output_and_filter(self):
        """Проверяет расширение SVG, правила имён Windows и длину фильтра."""
        for output in ("", "graph.png", "bad?.svg", "CON.svg", "x\x00.svg"):
            with self.subTest(output=output), self.assertRaises(ToolError):
                load_config(self.write_config(dict(self.values, output=output)))
        with self.assertRaisesRegex(ToolError, "filter"):
            load_config(self.write_config(dict(self.values, filter="x" * 257)))

    def test_malformed_file(self):
        """Отсутствующий файл, ключ, повтор и неверный заголовок отклоняются."""
        path = self.base / "config.csv"
        with self.assertRaises(ToolError):
            load_config(path)
        path.write_text("wrong,header\n", encoding="utf-8")
        with self.assertRaisesRegex(ToolError, "заголовок"):
            load_config(path)
        path.write_text("key,value\npackage,A\npackage,B\n", encoding="utf-8")
        with self.assertRaisesRegex(ToolError, "повторный"):
            load_config(path)
        path.write_text("key,value\npackage,A\n", encoding="utf-8")
        with self.assertRaisesRegex(ToolError, "отсутствуют"):
            load_config(path)
        path.write_text("key,value\nunknown,x\n", encoding="utf-8")
        with self.assertRaisesRegex(ToolError, "неизвестный"):
            load_config(path)
