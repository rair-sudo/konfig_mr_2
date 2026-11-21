"""Проверки автономной визуализации и совместимости с D2."""

from collections import Counter
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

from src.errors import ToolError
from src.render import save_graph, to_d2, to_svg


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
            input=to_d2(graph), text=True, capture_output=True,
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


if __name__ == "__main__":
    unittest.main()
