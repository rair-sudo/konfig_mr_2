"""Проверки автономной визуализации и совместимости с D2."""

from collections import Counter
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

from src.errors import ToolError
from src.render import _rollback, save_graph, to_d2, to_svg


SVG = "{http://www.w3.org/2000/svg}"


def _svg_nodes(root):
    """Возвращает идентификаторы узлов и их метки из SVG."""
    return {
        node.attrib["id"]: node.find(f"{SVG}title").text or ""
        for node in root.iter(f"{SVG}g")
        if node.get("class") == "node"
    }


def _svg_edges(root, nodes):
    """Читает направленные рёбра, восстанавливая исходные метки."""
    return Counter(
        (nodes[path.get("data-from")], nodes[path.get("data-to")])
        for path in root.iter(f"{SVG}path") if path.get("class") == "edge"
    )


def _rectangles(root):
    """Возвращает точные границы прямоугольников по идентификаторам."""
    result = {}
    for group in root.iter(f"{SVG}g"):
        if group.get("class") != "node":
            continue
        rect = group.find(f"{SVG}rect")
        x, y = float(rect.get("x")), float(rect.get("y"))
        result[group.get("id")] = (x, y, x + float(rect.get("width")),
                                   y + float(rect.get("height")))
    return result


def _path_segments(path):
    """Читает линии и кривые с контрольными точками SVG."""
    tokens = iter(re.findall(r"[MHVCQ]|-?\d+(?:\.\d+)?", path))
    segments = []
    current = None
    for command in tokens:
        if command == "M":
            current = (float(next(tokens)), float(next(tokens)))
            continue
        if command == "H":
            points = [(float(next(tokens)), current[1])]
        elif command == "V":
            points = [(current[0], float(next(tokens)))]
        elif command == "C":
            points = [(float(next(tokens)), float(next(tokens)))
                      for _ in range(3)]
        elif command == "Q":
            points = [(float(next(tokens)), float(next(tokens)))
                      for _ in range(2)]
        else:
            raise ValueError(f"Неизвестная команда SVG: {command}")
        segments.append([current, *points])
        current = points[-1]
    return segments


def _bounds(points):
    """Ограничивающий прямоугольник сегмента, включая контрольные точки."""
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def _intersects(first, second):
    """Проверяет даже касание границ, включая вырожденные линии."""
    return not (first[2] < second[0] or first[0] > second[2] or
                first[3] < second[1] or first[1] > second[3])


def _d2_parts(text):
    """Восстанавливает метки и рёбра из детерминированного D2."""
    nodes, edges = {}, []
    for line in text.splitlines():
        if " -> " in line:
            edges.append(line.split(" -> "))
        else:
            ident, label = line.split(": ", 1)
            nodes[ident] = json.loads(label)
    return set(nodes.values()), Counter((nodes[a], nodes[b]) for a, b in edges)


class _BrokenWriter:
    """Контекстный поток, имитирующий ошибку записи в файл."""

    def __init__(self, stream):
        """Сохраняет настоящий открытый файл."""
        self.stream = stream

    def __enter__(self):
        """Предоставляет поток контекстному менеджеру."""
        return self

    def __exit__(self, *args):
        """Закрывает настоящий файл."""
        self.stream.close()

    def fileno(self):
        """Возвращает файловый дескриптор."""
        return self.stream.fileno()

    def write(self, text):
        """Имитирует ошибку диска при записи содержимого."""
        raise OSError("disk failure")


class RenderTests(unittest.TestCase):
    """Проверяет точное соответствие двум форматам и запись на диск."""

    def assert_on_canvas(self, box, width, height):
        """Оставляет запас под стрелку, толщину линии и скругления."""
        self.assertGreaterEqual(min(box[0], box[1]), 8)
        self.assertLessEqual(box[2], width - 8)
        self.assertLessEqual(box[3], height - 8)

    def assert_node_geometry(self, rectangles, width, height):
        """Узлы не пересекаются друг с другом и границами холста."""
        for ident, rect in rectangles.items():
            self.assert_on_canvas(rect, width, height)
            for other, other_rect in rectangles.items():
                if ident != other:
                    self.assertFalse(_intersects(rect, other_rect))

    def assert_port(self, ident, point, rectangles, occupied):
        """Каждый конец ребра занимает свою точку границы нужного узла."""
        rect = rectangles[ident]
        self.assertEqual(point[0], rect[2])
        self.assertLess(rect[1], point[1])
        self.assertLess(point[1], rect[3])
        self.assertNotIn((ident, point), occupied)
        occupied.add((ident, point))

    def assert_path_geometry(self, path, rectangles, occupied, width, height):
        """Контрольные оболочки сегментов не касаются посторонних узлов."""
        src, dst = path.get("data-from"), path.get("data-to")
        segments = _path_segments(path.get("d"))
        self.assert_port(src, segments[0][0], rectangles, occupied)
        self.assert_port(dst, segments[-1][-1], rectangles, occupied)
        for index, points in enumerate(segments):
            box = _bounds(points)
            self.assert_on_canvas(box, width, height)
            allowed = set()
            if index == 0:
                allowed.add(src)
            if index == len(segments) - 1:
                allowed.add(dst)
            for ident, rect in rectangles.items():
                if ident not in allowed:
                    self.assertFalse(_intersects(box, rect),
                                     (path.get("d"), ident, rect))

    def assert_safe_geometry(self, root):
        """Проверяет узлы, концы рёбер и оболочки всех сегментов."""
        rectangles = _rectangles(root)
        width, height = float(root.get("width")), float(root.get("height"))
        self.assert_node_geometry(rectangles, width, height)
        occupied_ports = set()
        paths = [p for p in root.iter(f"{SVG}path")
                 if p.get("class") == "edge"]
        for path in paths:
            self.assert_path_geometry(path, rectangles, occupied_ports,
                                      width, height)

    def test_external_routes_avoid_foreign_nodes_and_canvas(self):
        """ALPHA→DELTA обходит BETA; петли и обратные связи тоже безопасны."""
        label = "Длинная зависимость <&> " * 45
        graph = {
            "ALPHA": ["DELTA", "DELTA", "BETA", "ALPHA", "ALPHA"],
            "BETA": ["ALPHA", "DELTA", "ALPHA", "BETA"],
            "DELTA": ["ALPHA", "BETA", "DELTA", label],
            "EPSILON": [label, "ALPHA"],
            label: ["ALPHA", label, label],
        }
        root = ET.fromstring(to_svg(graph))
        nodes = _svg_nodes(root)
        expected = Counter((src, dst) for src, targets in graph.items()
                           for dst in targets)
        self.assertEqual(_svg_edges(root, nodes), expected)
        self.assertEqual(_d2_parts(to_d2(graph)), (set(nodes.values()),
                                                   expected))
        paths = [p.get("d") for p in root.iter(f"{SVG}path")
                 if p.get("class") == "edge"]
        self.assertEqual(len(paths), len(set(paths)))
        lanes = [_path_segments(path)[2][0][0] for path in paths]
        self.assertEqual(len(lanes), len(set(lanes)))
        self.assertIn(label, nodes.values())
        self.assert_safe_geometry(root)

    def test_many_parallel_edges_and_loops_fit(self):
        """Множество петель и двусторонних повторов не выводит дуги за SVG."""
        graph = {"ALPHA": ["ALPHA"] * 40 + ["BETA"] * 35,
                 "BETA": ["ALPHA"] * 35 + ["BETA"] * 40}
        svg = to_svg(graph)
        root = ET.fromstring(svg)
        nodes = _svg_nodes(root)
        self.assertEqual(_svg_edges(root, nodes)["ALPHA", "ALPHA"], 40)
        self.assertEqual(_svg_edges(root, nodes)["BETA", "ALPHA"], 35)
        self.assertEqual(_svg_edges(root, nodes)["ALPHA", "BETA"], 35)
        self.assertEqual(_svg_edges(root, nodes)["BETA", "BETA"], 40)
        self.assertEqual(svg, to_svg(dict(reversed(list(graph.items())))))
        self.assert_safe_geometry(root)

    def test_nodes_edges_unicode_and_special_characters(self):
        """SVG и D2 включают все узлы, рёбра и экранированные метки."""
        graph = {
            'пакет <"&>\\': ["目标", 'пакет <"&>\\'],
            "目标": ["Зависимость", 'пакет <"&>\\'],
        }
        root = ET.fromstring(to_svg(graph))
        nodes = _svg_nodes(root)
        self.assertEqual(set(nodes.values()), set(graph) | {"Зависимость"})
        expected = Counter((src, dst) for src, deps in graph.items()
                           for dst in deps)
        self.assertEqual(_svg_edges(root, nodes), expected)
        source = to_d2(graph)
        for label in nodes.values():
            self.assertIn(json.dumps(label, ensure_ascii=False), source)
        self.assertEqual(source.count(" -> "), sum(expected.values()))
        self.assertIn("&lt;", to_svg(graph))
        self.assertIn("&amp;", to_svg(graph))

    def test_cycle_loops_parallel_and_mutual_edges(self):
        """Петли, циклы, повторы и обратные связи остаются различимыми."""
        graph = {"a": ["a", "a", "b", "b"], "b": ["a", "c"],
                 "c": ["a"]}
        root = ET.fromstring(to_svg(graph))
        nodes = _svg_nodes(root)
        edges = _svg_edges(root, nodes)
        self.assertEqual(edges["a", "a"], 2)
        self.assertEqual(edges["a", "b"], 2)
        self.assertEqual(edges["b", "a"], 1)
        self.assertEqual(sum(edges.values()), 7)
        paths = [item for item in root.iter(f"{SVG}path")
                 if item.get("class") == "edge"]
        self.assertEqual(len({item.get("d") for item in paths}), len(paths))
        self.assertTrue(any(" C " in item.get("d") for item in paths))
        self.assertIn('marker-end="url(#arrow)"', to_svg(graph))

    def test_empty_graph_and_determinism(self):
        """Пустой граф корректен, а порядок ввода не влияет на вывод."""
        self.assertEqual(to_d2({}), "")
        empty = to_svg({})
        self.assertEqual(_svg_nodes(ET.fromstring(empty)), {})
        self.assertNotIn("nan", empty.lower())
        first = {"b": ["a", "c"], "a": ["c"]}
        second = {"a": ["c"], "b": ["c", "a"]}
        self.assertEqual(to_d2(first), to_d2(second))
        self.assertEqual(to_svg(first), to_svg(second))

    def test_long_and_multiline_labels_are_not_truncated(self):
        """Длинные метки переносятся, высота и размер холста растут."""
        label = "Очень длинная зависимость <>" * 45
        result = to_svg({label: [], "строка\nвторая строка": []})
        root = ET.fromstring(result)
        nodes = _svg_nodes(root)
        self.assertIn(label, nodes.values())
        group = next(g for g in root.iter(f"{SVG}g")
                     if g.get("class") == "node"
                     and g.find(f"{SVG}title").text == label)
        lines = [text.text or "" for text in group.findall(f"{SVG}text")]
        self.assertEqual("".join(lines), label)
        self.assertTrue(all(len(line) <= 26 for line in lines))
        rect = group.find(f"{SVG}rect")
        self.assertGreater(int(rect.get("height")), 800)
        self.assertGreater(int(root.get("height")), int(rect.get("height")))

    def test_control_characters_produce_valid_xml_and_d2(self):
        """Непредставимые в XML символы показаны явными escape-кодами."""
        graph = {"A\x00B\tC\rD\ud800": []}
        xml = to_svg(graph)
        ET.fromstring(xml)
        self.assertIn(r"A\u0000B\tC\rD\ud800", xml)
        self.assertIn(r"\\u0000", to_d2(graph))

    def test_large_graph_and_canvas_bounds(self):
        """Холст и его узлы масштабируются без обрезания содержимого."""
        graph = {f"node-{i:03}": [f"node-{(i + 1) % 300:03}"]
                 for i in range(300)}
        root = ET.fromstring(to_svg(graph))
        nodes = _svg_nodes(root)
        self.assertEqual(len(nodes), 300)
        self.assertEqual(sum(_svg_edges(root, nodes).values()), 300)
        width, height = int(root.get("width")), int(root.get("height"))
        for group in root.iter(f"{SVG}g"):
            if group.get("class") != "node":
                continue
            rect = group.find(f"{SVG}rect")
            x, y = float(rect.get("x")), float(rect.get("y"))
            self.assertGreaterEqual(min(x, y), 0)
            self.assertLessEqual(x + float(rect.get("width")), width)
            self.assertLessEqual(y + float(rect.get("height")), height)

    def test_save_two_files_and_make_parents(self):
        """Создаёт оба точных файла в новом вложенном каталоге."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "deep" / "sub" / "граф.svg"
            graph = {"a": ["b"], "b": []}
            d2, svg = save_graph(graph, target)
            self.assertEqual((d2, svg), (target.with_suffix(".d2"), target))
            self.assertEqual(d2.read_text(encoding="utf-8"), to_d2(graph))
            self.assertEqual(svg.read_text(encoding="utf-8"), to_svg(graph))

    def test_collisions_and_existing_files_are_not_overwritten(self):
        """Совпадение путей и любые существующие результаты безопасны."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ToolError):
                save_graph({}, root / "same.d2")
            self.assertFalse((root / "same.d2").exists())
            svg = root / "existing.svg"
            svg.write_text("SVG original", encoding="utf-8")
            with self.assertRaises(ToolError):
                save_graph({}, svg)
            self.assertEqual(svg.read_text(encoding="utf-8"), "SVG original")
            self.assertFalse(svg.with_suffix(".d2").exists())
            svg.unlink()
            d2 = svg.with_suffix(".d2")
            d2.write_text("D2 original", encoding="utf-8")
            with self.assertRaises(ToolError):
                save_graph({}, svg)
            self.assertEqual(d2.read_text(encoding="utf-8"), "D2 original")
            self.assertFalse(svg.exists())

    def test_force_replaces_both_results(self):
        """Явное разрешение заменяет оба результата, не оставляя временных."""
        with tempfile.TemporaryDirectory() as directory:
            svg = Path(directory) / "graph.svg"
            save_graph({"old": []}, svg)
            d2, _ = save_graph({"new": ["leaf"], "leaf": []}, svg, True)
            self.assertIn('"new"', d2.read_text(encoding="utf-8"))
            self.assertIn("new", svg.read_text(encoding="utf-8"))
            self.assertEqual(len(list(Path(directory).iterdir())), 2)

    def test_force_directory_preserves_other_result(self):
        """Каталог вместо результата не удаляется даже при перезаписи."""
        with tempfile.TemporaryDirectory() as directory:
            svg = Path(directory) / "graph.svg"
            svg.mkdir()
            d2 = svg.with_suffix(".d2")
            d2.write_text("original", encoding="utf-8")
            with self.assertRaises(ToolError):
                save_graph({}, svg, True)
            self.assertEqual(d2.read_text(encoding="utf-8"), "original")
            self.assertTrue(svg.is_dir())

    def test_bad_path_and_directory_target(self):
        """Неверные пути и ошибки создания каталогов становятся ToolError."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ToolError):
                save_graph({}, str(root / "bad\x00.svg"))
            (root / "block").write_text("not a directory", encoding="utf-8")
            with self.assertRaises(ToolError):
                save_graph({}, root / "block" / "graph.svg")
            target = root / "target.svg"
            target.mkdir()
            with self.assertRaises(ToolError):
                save_graph({}, target)
            self.assertFalse(target.with_suffix(".d2").exists())

    def test_rollback_when_writing_fails(self):
        """Частично созданные результаты удаляются при ошибке записи."""
        with tempfile.TemporaryDirectory() as directory:
            svg = Path(directory) / "output.svg"
            original_open = Path.open

            def open_with_failure(path, *args, **kwargs):
                """Создаёт сломанный поток только для SVG."""
                stream = original_open(path, *args, **kwargs)
                return _BrokenWriter(stream) if path == svg else stream

            with mock.patch.object(Path, "open", open_with_failure):
                with self.assertRaisesRegex(ToolError, "disk failure"):
                    save_graph({"a": []}, svg)
            self.assertFalse(svg.exists())
            self.assertFalse(svg.with_suffix(".d2").exists())

    @unittest.skipUnless(shutil.which("d2"), "D2 не установлен")
    def test_real_d2_compiles_labels_and_all_edges(self):
        """Настоящий D2 компилирует синтаксис, циклы и спецсимволы."""
        names = ['пакет "&<\\', '${variable}', 'a\\${x}', "目标"]
        graph = {names[0]: [names[0], names[1], names[1]],
                 names[1]: [names[0], names[2]], names[2]: [names[3]]}
        result = subprocess.run(
            [shutil.which("d2"), "--stdout-format", "svg", "-", "-"],
            input=to_d2(graph), text=True, encoding="utf-8",
            capture_output=True,
            check=False, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        root = ET.fromstring(result.stdout)
        texts = ["".join(item.itertext()) for item in root.iter(f"{SVG}text")]
        for name in names:
            self.assertIn(name, texts)
        paths = [item for item in root.iter(f"{SVG}path")
                 if "connection" in item.get("class", "")]
        self.assertEqual(len(paths), 6)


class OverwriteRecoveryTests(unittest.TestCase):
    """Проверяет подготовку копий, откат и очистку после ошибок."""

    def setUp(self):
        """Создаёт два разных исходных файла с произвольными байтами."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.svg = self.root / "graph.svg"
        self.d2 = self.svg.with_suffix(".d2")
        self.original_d2 = b"old d2\x00\xff"
        self.original_svg = b"old svg\x00\xfe"
        self.d2.write_bytes(self.original_d2)
        self.svg.write_bytes(self.original_svg)

    def _replace_failing_on(self, *numbers, inspect=None):
        """Создаёт mock для конкретных вызовов настоящего os.replace."""
        original = os.replace
        calls = 0

        def replace(source, target):
            """Пропускает обычные вызовы и прерывает выбранные."""
            nonlocal calls
            calls += 1
            if inspect is not None:
                inspect(calls)
            if calls in numbers:
                raise OSError(f"replace failure {calls}")
            return original(source, target)

        return mock.patch("src.render.os.replace", side_effect=replace)

    def _assert_no_auxiliary_files(self):
        """Убеждается, что резервные и рабочие файлы удалены."""
        self.assertEqual(set(self.root.iterdir()), {self.d2, self.svg})

    def test_first_and_second_replace_failures_restore_both(self):
        """До первой замены готовы обе копии; ошибки не портят оригиналы."""
        def inspect(call):
            """Проверяет обе резервные копии непосредственно перед заменой."""
            if call == 1:
                copies = self.root.glob(".graph-backup-*")
                self.assertEqual({path.read_bytes() for path in copies},
                                 {self.original_d2, self.original_svg})

        for failing_call in (1, 2):
            with self.subTest(failing_call=failing_call):
                with self._replace_failing_on(failing_call, inspect=inspect):
                    with self.assertRaisesRegex(ToolError, "replace failure"):
                        save_graph({"новый": []}, self.svg, True)
                self.assertEqual(self.d2.read_bytes(), self.original_d2)
                self.assertEqual(self.svg.read_bytes(), self.original_svg)
                self._assert_no_auxiliary_files()

    def test_second_failure_removes_new_file(self):
        """При отсутствии старого D2 только что созданный D2 удаляется."""
        self.d2.unlink()
        with self._replace_failing_on(2):
            with self.assertRaisesRegex(ToolError, "replace failure 2"):
                save_graph({"a": []}, self.svg, True)
        self.assertFalse(self.d2.exists())
        self.assertEqual(self.svg.read_bytes(), self.original_svg)
        self.assertEqual(list(self.root.iterdir()), [self.svg])

    def test_failed_restore_keeps_all_backups_and_reports_paths(self):
        """При провале отката обе копии уцелели и перечислены в ошибке."""
        with self._replace_failing_on(2, 3):
            with self.assertRaises(ToolError) as caught:
                save_graph({"new": []}, self.svg, True)
        backups = list(self.root.glob(".graph-backup-*"))
        self.assertEqual({path.read_bytes() for path in backups},
                         {self.original_d2, self.original_svg})
        message = str(caught.exception)
        self.assertIn("replace failure 2", message)
        self.assertIn("replace failure 3", message)
        self.assertIn("частичное изменение", message)
        for path in backups:
            self.assertIn(str(path), message)
        self.assertEqual(self.d2.read_text(encoding="utf-8"),
                         to_d2({"new": []}))
        self.assertEqual(self.svg.read_bytes(), self.original_svg)
        expected = {self.d2, self.svg, *backups}
        self.assertEqual(set(self.root.iterdir()), expected)

    def test_failed_removal_keeps_other_original_backup(self):
        """Ошибка удаления созданного D2 сохраняет резервную копию SVG."""
        self.d2.unlink()
        original_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            """Имитирует запрет удаления только нового результата."""
            if path == self.d2:
                raise OSError("cannot remove created file")
            return original_unlink(path, *args, **kwargs)

        with self._replace_failing_on(2), mock.patch.object(Path, "unlink",
                                                            unlink):
            with self.assertRaises(ToolError) as caught:
                save_graph({"new": []}, self.svg, True)
        backups = list(self.root.glob(".graph-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original_svg)
        self.assertIn(str(backups[0]), str(caught.exception))
        self.assertIn("частичное изменение", str(caught.exception))
        self.assertEqual(self.svg.read_bytes(), self.original_svg)
        expected = {self.d2, self.svg, *backups}
        self.assertEqual(set(self.root.iterdir()), expected)

    def test_rollback_does_not_remove_another_process_file(self):
        """Откат не удаляет результат, заменённый посторонним файлом."""
        self.d2.unlink()
        foreign = self.root / "foreign"
        foreign.write_bytes(b"foreign data")
        original = os.replace
        calls = 0

        def replace(source, target):
            """Подменяет новый D2 до неудачной второй замены."""
            nonlocal calls
            calls += 1
            if calls == 2:
                original(foreign, self.d2)
                raise OSError("second replace failure")
            return original(source, target)

        with mock.patch("src.render.os.replace", side_effect=replace):
            with self.assertRaises(ToolError) as caught:
                save_graph({"new": []}, self.svg, True)
        backups = list(self.root.glob(".graph-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original_svg)
        self.assertEqual(self.d2.read_bytes(), b"foreign data")
        self.assertIn("частичное изменение", str(caught.exception))
        self.assertIn(str(backups[0]), str(caught.exception))

    def test_cleanup_failure_reports_path_and_cleans_other_files(self):
        """Ошибка очистки не мешает удалить остальные служебные файлы."""
        original_unlink = Path.unlink
        failed = []

        def unlink(path, *args, **kwargs):
            """Не удаляет только первую резервную копию."""
            if path.name.startswith(".graph-backup-") and not failed:
                failed.append(path)
                raise OSError("cleanup failure")
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", unlink):
            with self.assertRaises(ToolError) as caught:
                save_graph({"new": []}, self.svg, True)
        self.assertEqual(len(failed), 1)
        self.assertTrue(failed[0].exists())
        self.assertIn(str(failed[0]), str(caught.exception))
        self.assertIn("cleanup failure", str(caught.exception))
        expected = {self.d2, self.svg, *failed}
        self.assertEqual(set(self.root.iterdir()), expected)
        self.assertEqual(self.d2.read_text(encoding="utf-8"),
                         to_d2({"new": []}))

    def test_rollback_attempts_every_replaced_file(self):
        """Ошибка восстановления одного пути не блокирует оставшиеся."""
        backups = [self.root / "first.backup", self.root / "second.backup"]
        backups[0].write_bytes(self.original_d2)
        backups[1].write_bytes(self.original_svg)
        self.d2.write_bytes(b"new d2")
        self.svg.write_bytes(b"new svg")
        replaced = []
        for target, backup in zip((self.d2, self.svg), backups):
            info = target.lstat()
            replaced.append((target, backup, (info.st_dev, info.st_ino)))
        original = os.replace

        def replace(source, target):
            """Ломает первый откат, но разрешает следующий."""
            if target == self.svg:
                raise OSError("restore failure")
            return original(source, target)

        with mock.patch("src.render.os.replace", side_effect=replace):
            failures = _rollback(replaced)
        self.assertEqual(len(failures), 1)
        self.assertIn("restore failure", failures[0])
        self.assertEqual(self.d2.read_bytes(), self.original_d2)
        self.assertEqual(self.svg.read_bytes(), b"new svg")
        self.assertEqual(backups[1].read_bytes(), self.original_svg)

    def test_backup_failure_leaves_originals_and_no_auxiliary_files(self):
        """Ошибка копирования до замен не затрагивает исходные файлы."""
        original = shutil.copyfileobj
        calls = 0

        def copy(source, target, *args, **kwargs):
            """Ломает создание второй копии после первой успешной."""
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("backup failure")
            return original(source, target, *args, **kwargs)

        with mock.patch("src.render.shutil.copyfileobj", side_effect=copy):
            with self.assertRaisesRegex(ToolError, "backup failure"):
                save_graph({"new": []}, self.svg, True)
        self.assertEqual(self.d2.read_bytes(), self.original_d2)
        self.assertEqual(self.svg.read_bytes(), self.original_svg)
        self._assert_no_auxiliary_files()

    def test_externally_changed_target_is_not_overwritten(self):
        """Изменение второго файла после снимка обнаруживается до замены."""
        def inspect(call):
            """Имитирует стороннюю запись после подготовки обеих копий."""
            if call == 1:
                self.svg.write_bytes(b"external change")

        with self._replace_failing_on(inspect=inspect):
            with self.assertRaisesRegex(ToolError, "изменился"):
                save_graph({"new": []}, self.svg, True)
        self.assertEqual(self.d2.read_bytes(), self.original_d2)
        self.assertEqual(self.svg.read_bytes(), b"external change")
        self._assert_no_auxiliary_files()

    def test_symlink_is_not_overwritten(self):
        """Символическая ссылка и её целевой файл остаются нетронутыми."""
        self.svg.unlink()
        external = self.root / "external.txt"
        external.write_bytes(self.original_svg)
        try:
            self.svg.symlink_to(external)
        except (OSError, NotImplementedError):
            self.skipTest("Создание символических ссылок недоступно")
        with self.assertRaisesRegex(ToolError, "обычным файлом"):
            save_graph({"new": []}, self.svg, True)
        self.assertTrue(self.svg.is_symlink())
        self.assertEqual(external.read_bytes(), self.original_svg)
        self.assertEqual(self.d2.read_bytes(), self.original_d2)
        self.assertEqual(set(self.root.iterdir()),
                         {self.d2, self.svg, external})

    def test_collision_message_has_both_launch_options(self):
        """Конфликт каждого из файлов даёт подсказку и не меняет данные."""
        for target in (self.d2, self.svg):
            if target == self.svg:
                self.d2.unlink()
            with self.assertRaises(ToolError) as caught:
                save_graph({}, self.svg)
            message = str(caught.exception)
            self.assertIn(str(target), message)
            self.assertIn("уже существует", message)
            self.assertIn("--force", message)
            self.assertIn("run.bat", message)
            self.assertIn("--no-overwrite", message)
        self.assertFalse(self.d2.exists())
        self.assertEqual(self.svg.read_bytes(), self.original_svg)


if __name__ == "__main__":
    unittest.main()
