"""Детерминированное представление графа в D2 и автономном SVG.

Рёбра и вершины сортируются; повторные рёбра сохраняются. Размер холста
и высота строк адаптируются к графу, а длинные метки переносятся без
усечения. Узлы образуют столбец, а каждое ребро имеет свою полосу справа;
оно не проходит через посторонние узлы, но рёбра могут пересекаться между
собой в коридоре. Не существует встроенного ограничения числа вершин или рёбер:
очень большие графы ограничены доступной памятью и возможностями SVG-просмотра.
Недопустимые в XML управляющие символы и одиночные суррогаты показываются
как видимые последовательности вида ``\\u0001``; табуляция и CR — как
``\\t`` и ``\\r`` соответственно.
"""

from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from xml.sax.saxutils import escape

from src.errors import ToolError


Graph = dict[str, list[str]]
Edge = tuple[str, str]
FileState = tuple[int, ...]
Snapshot = tuple[Path | None, FileState | None]
Replacement = tuple[Path, Path | None, tuple[int, int]]
_LINE_LIMIT = 26
_NODE_WIDTH = 320
_LINE_HEIGHT = 20
_PORT_GAP = 16
_LANE_GAP = 16
_MARGIN = 40
_NODE_GAP = 48
_CONTROL_C0_END = 0x20
_CONTROL_C1_START = 0x7F
_CONTROL_C1_END = 0xA0
_SURROGATE_START = 0xD800
_SURROGATE_END = 0xDFFF
_XML_INVALID_CODES = frozenset((0xFFFE, 0xFFFF))


@dataclass(frozen=True)
class Node:
    """Положение узла и строки его видимой метки."""

    ident: str
    x: int
    y: int
    height: int
    lines: list[str]
    ports: int


def _parts(graph: Graph) -> tuple[list[str], list[Edge]]:
    """Возвращает канонический порядок вершин и рёбер, включая повторы."""
    edges = sorted((src, dst) for src, targets in graph.items()
                   for dst in targets)
    vertices = set(graph)
    vertices.update(dst for _, dst in edges)
    return sorted(vertices), edges


def _visible(text: str) -> str:
    """Заменяет непечатаемые символы видимыми безопасными обозначениями."""
    result = []
    for char in text:
        code = ord(char)
        if char == "\n":
            result.append(char)
        elif char == "\t":
            result.append("\\t")
        elif char == "\r":
            result.append("\\r")
        elif (code < _CONTROL_C0_END or
              _CONTROL_C1_START <= code < _CONTROL_C1_END):
            result.append(f"\\u{code:04x}")
        elif (_SURROGATE_START <= code <= _SURROGATE_END or
              code in _XML_INVALID_CODES):
            result.append(f"\\u{code:04x}")
        else:
            result.append(char)
    return "".join(result)


def _d2_label(text: str) -> str:
    """Экранирует строку как JSON и защищает интерполяцию D2."""
    label = json.dumps(_visible(text), ensure_ascii=False)
    return label.replace("${", "\\${")


def to_d2(graph: Graph) -> str:
    """Возвращает D2 с безопасными идентификаторами и всеми рёбрами."""
    vertices, edges = _parts(graph)
    ids = {vertex: f"n{index}" for index, vertex in enumerate(vertices)}
    lines = [f"{ids[vertex]}: {_d2_label(vertex)}" for vertex in vertices]
    lines.extend(f"{ids[src]} -> {ids[dst]}" for src, dst in edges)
    return "\n".join(lines) + ("\n" if lines else "")


def _lines(label: str) -> list[str]:
    """Переносит текст по символам, не теряя пробелы и содержимое."""
    lines = []
    for line in _visible(label).split("\n"):
        chunks = [line[i:i + _LINE_LIMIT]
                  for i in range(0, len(line), _LINE_LIMIT)]
        lines.extend(chunks or [""])
    return lines if label else ['""']


def _layout(vertices: list[str], edges: list[Edge]):
    """Размещает узлы в столбце, резервируя справа коридор для рёбер."""
    ports = Counter(vertex for edge in edges for vertex in edge)
    positions = {}
    top = _MARGIN
    for index, vertex in enumerate(vertices):
        lines = _lines(vertex)
        label_height = 42 + _LINE_HEIGHT * (len(lines) - 1)
        port_height = 24 + _PORT_GAP * max(0, ports[vertex] - 1)
        height = max(label_height, port_height)
        positions[vertex] = Node(f"n{index}", _MARGIN + _NODE_WIDTH // 2,
                                 top + height // 2, height, lines,
                                 ports[vertex])
        top += height + _NODE_GAP
    lane_width = 32 + _LANE_GAP * (len(edges) - 1) if edges else 0
    width = 2 * _MARGIN + _NODE_WIDTH + lane_width
    height = top - _NODE_GAP + _MARGIN if vertices else 2 * _MARGIN
    return positions, width, height


def _port_y(node: Node, index: int) -> float:
    """Выделяет отдельную точку на правой границе для каждого конца ребра."""
    return node.y + _PORT_GAP * (index - (node.ports - 1) / 2)


def _edge_path(src: Node, dst: Node, first: int, last: int,
               lane: int) -> str:
    """Проводит дугу справа от всех узлов, скругляя её повороты."""
    x = src.x + _NODE_WIDTH // 2
    y1, y2 = _port_y(src, first), _port_y(dst, last)
    sign = 1 if y2 > y1 else -1
    radius = min(10, abs(y2 - y1) / 2)
    return (f"M {x} {y1:.2f} H {lane - radius:.2f} "
            f"C {lane - radius / 2:.2f} {y1:.2f}, {lane} {y1:.2f}, "
            f"{lane} {y1 + sign * radius:.2f} "
            f"V {y2 - sign * radius:.2f} "
            f"C {lane} {y2:.2f}, {lane - radius / 2:.2f} {y2:.2f}, "
            f"{lane - radius:.2f} {y2:.2f} H {x}")


def _edge_elements(edges: list[Edge], nodes: dict[str, Node]) -> list[str]:
    """Даёт каждому ребру собственную полосу и две разные точки касания."""
    used: Counter[str] = Counter()
    elements = []
    for index, (src, dst) in enumerate(edges):
        start, end = nodes[src], nodes[dst]
        first = used[src]
        used[src] += 1
        last = used[dst]
        used[dst] += 1
        lane = start.x + _NODE_WIDTH // 2 + 32 + index * _LANE_GAP
        path = _edge_path(start, end, first, last, lane)
        elements.append(
            f'<path class="edge" data-from="{start.ident}" '
            f'data-to="{end.ident}" d="{path}"/>')
    return elements


def _node_element(node: Node, label: str) -> str:
    """Возвращает размеченный узел с полностью видимой меткой."""
    left = node.x - _NODE_WIDTH // 2
    top = node.y - node.height / 2
    first_y = node.y - (len(node.lines) - 1) * _LINE_HEIGHT / 2 + 5
    result = [f'<g class="node" id="{node.ident}">',
              f'<title>{escape(_visible(label))}</title>',
              f'<rect x="{left}" y="{top:.2f}" width="{_NODE_WIDTH}" '
              f'height="{node.height}" rx="10" fill="#ffffff" '
              'stroke="#94a3b8"/>']
    for index, line in enumerate(node.lines):
        if not line:
            continue
        y = first_y + index * _LINE_HEIGHT
        size = len(line) * 10
        result.append(
            f'<text x="{node.x}" y="{y:.2f}" text-anchor="middle" '
            f'xml:space="preserve" textLength="{size}" '
            f'lengthAdjust="spacingAndGlyphs">{escape(line)}</text>')
    result.append("</g>")
    return "\n".join(result)


def to_svg(graph: Graph) -> str:
    """Возвращает самостоятельный XML/SVG без стороннего ПО и библиотек."""
    vertices, edges = _parts(graph)
    nodes, width, height = _layout(vertices, edges)
    elements = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img">',
        '<title>Граф зависимостей</title>',
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="10" '
        'refY="5" markerWidth="8" markerHeight="8" orient="auto" '
        'markerUnits="userSpaceOnUse"><path d="M 0 0 L 10 5 L 0 10 Z" '
        'fill="#475569"/></marker></defs>',
        '<rect width="100%" height="100%" fill="#f8fafc"/>',
        '<g fill="none" stroke="#475569" stroke-width="2" '
        'marker-end="url(#arrow)">',
    ]
    elements.extend(_edge_elements(edges, nodes))
    elements.append('</g><g font-family="sans-serif" font-size="16" '
                    'fill="#0f172a">')
    for vertex in vertices:
        elements.append(_node_element(nodes[vertex], vertex))
    elements.extend(["</g>", "</svg>"])
    return "\n".join(elements) + "\n"


def _remove_created(created: list[tuple[Path, os.stat_result]]) -> None:
    """Удаляет только файлы, созданные текущим вызовом и не заменённые."""
    for path, original in reversed(created):
        try:
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (original.st_dev,
                                                    original.st_ino):
                path.unlink()
        except OSError:
            pass


def _write_temporary(target: Path, text: str, files: list[Path]) -> Path:
    """Записывает результат рядом с целью и сразу учитывает временный файл."""
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", prefix=".graph-",
        dir=target.parent, delete=False,
    ) as stream:
        path = Path(stream.name)
        files.append(path)
        stream.write(text)
    return path


def _file_state(info: os.stat_result) -> FileState:
    """Возвращает идентичность и изменяемые признаки обычного файла."""
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns)


def _check_target(target: Path, expected: FileState | None) -> None:
    """Отклоняет заметные изменения пути после подготовки его копии."""
    try:
        actual = _file_state(target.lstat())
    except FileNotFoundError:
        actual = None
    if actual != expected:
        raise OSError(f"Файл результата изменился во время записи: {target}")


def _backup_original(target: Path, backups: list[Path]) -> Snapshot:
    """Копирует обычный исходный файл, отклоняя символические ссылки."""
    try:
        info = target.lstat()
    except FileNotFoundError:
        return None, None
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"Результат не является обычным файлом: {target}")
    expected = _file_state(info)
    with target.open("rb") as source:
        if _file_state(os.fstat(source.fileno())) != expected:
            raise OSError(f"Файл изменился до копирования: {target}")
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".graph-backup-", dir=target.parent,
            delete=False,
        ) as copy:
            backup = Path(copy.name)
            backups.append(backup)
            shutil.copyfileobj(source, copy)
        if _file_state(os.fstat(source.fileno())) != expected:
            raise OSError(f"Файл изменился при копировании: {target}")
    _check_target(target, expected)
    shutil.copystat(target, backup, follow_symlinks=False)
    _check_target(target, expected)
    return backup, expected


def _restore_one(target: Path, backup: Path | None,
                 identity: tuple[int, int]) -> None:
    """Восстанавливает исходный файл либо удаляет созданный этим вызовом."""
    try:
        current = target.lstat()
    except FileNotFoundError:
        current = None
    if current is not None and (current.st_dev, current.st_ino) != identity:
        raise OSError(f"Результат изменён извне, откат невозможен: {target}")
    if backup is not None:
        os.replace(backup, target)
    elif current is not None:
        target.unlink()


def _rollback(replaced: list[Replacement]) -> list[str]:
    """Пытается откатить все замены даже после отдельной ошибки."""
    failures = []
    for target, backup, identity in reversed(replaced):
        try:
            _restore_one(target, backup, identity)
        except Exception as error:
            failures.append(f"{target}: {error}")
    return failures


def _cleanup_files(files: list[Path]) -> list[str]:
    """Пытается удалить все служебные файлы и сообщает о неудачах."""
    failures = []
    for path in files:
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            failures.append(f"{path}: {error}")
    return failures


def _report_save_failure(error, rollback, cleanup, backups) -> None:
    """Сообщает о неудачном откате и оставленных резервных копиях."""
    if rollback:
        paths = [str(path) for path in backups if path.exists()]
        saved = ", ".join(paths) if paths else "отсутствуют"
        message = (f"Не удалось сохранить граф: {error}; ошибки отката: "
                   f"{'; '.join(rollback)}; резервные копии: {saved}; "
                   "возможно частичное изменение файлов")
    elif error is not None:
        message = f"Не удалось сохранить граф: {error}"
    else:
        message = "Граф записан, но не удалось очистить служебные файлы"
    if cleanup:
        message += f"; не удалены временные файлы: {'; '.join(cleanup)}"
    raise ToolError(message) from error


def _save_replacing(d2: Path, svg: Path, texts: tuple[str, str]) -> None:
    """Сначала готовит все данные и копии, затем заменяет пути с откатом."""
    targets = (d2, svg)
    temporary: list[Path] = []
    backups: list[Path] = []
    snapshots: list[Snapshot] = []
    replaced: list[Replacement] = []
    error = None
    rollback: list[str] = []
    try:
        for target, text in zip(targets, texts):
            _write_temporary(target, text, temporary)
        for target in targets:
            snapshots.append(_backup_original(target, backups))
        for target, source, (backup, expected) in zip(
            targets, temporary, snapshots
        ):
            _check_target(target, expected)
            info = source.lstat()
            os.replace(source, target)
            replaced.append((target, backup, (info.st_dev, info.st_ino)))
    except BaseException as caught:
        error = caught
        rollback = _rollback(replaced)
    cleanup = _cleanup_files(temporary + ([] if rollback else backups))
    if rollback or cleanup:
        _report_save_failure(error, rollback, cleanup, backups)
    if error is not None:
        raise error


def _open_new(stack: ExitStack, target: Path):
    """Создаёт новый файл и подсказывает варианты устранения конфликта."""
    try:
        return stack.enter_context(
            target.open("x", encoding="utf-8", newline="\n"))
    except FileExistsError as error:
        raise ToolError(
            f"Файл уже существует: {target}. Для прямого запуска используйте "
            "--force; при запуске через run.bat уберите --no-overwrite"
        ) from error


def _save_new(d2: Path, svg: Path, texts: tuple[str, str]) -> None:
    """Создаёт оба результата без перезаписи либо удаляет созданные файлы."""
    created: list[tuple[Path, os.stat_result]] = []
    try:
        with ExitStack() as stack:
            d2_file = _open_new(stack, d2)
            created.append((d2, os.fstat(d2_file.fileno())))
            svg_file = _open_new(stack, svg)
            created.append((svg, os.fstat(svg_file.fileno())))
            d2_file.write(texts[0])
            svg_file.write(texts[1])
    except BaseException:
        _remove_created(created)
        raise


def save_graph(graph: Graph, svg_path: str | Path,
               overwrite: bool = False) -> tuple[Path, Path]:
    """Создаёт D2 и SVG и возвращает (D2, SVG).

    Без overwrite существующие файлы защищены от перезаписи. С overwrite
    результаты заменяются по одному после подготовки резервных копий; при
    ошибке выполняется откат. Это не атомарная транзакция: пара может быть
    временно несогласованной, а откат или очистка могут завершиться ошибкой.
    Символические ссылки и другие необычные файлы не перезаписываются.
    Нет гарантии при системном сбое или одновременной записи извне.
    Ошибки файловой системы преобразуются в ToolError.
    """
    try:
        svg = Path(svg_path)
        d2 = svg.with_suffix(".d2")
        if os.path.normcase(os.path.abspath(d2)) == os.path.normcase(
            os.path.abspath(svg)
        ):
            raise ToolError("Пути D2 и SVG совпадают")
        d2_text, svg_text = to_d2(graph), to_svg(graph)
        svg.parent.mkdir(parents=True, exist_ok=True)
        if overwrite:
            _save_replacing(d2, svg, (d2_text, svg_text))
        else:
            _save_new(d2, svg, (d2_text, svg_text))
        return d2, svg
    except (OSError, ValueError, TypeError) as error:
        raise ToolError(f"Не удалось сохранить граф: {error}") from error
