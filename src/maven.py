"""Чтение прямых зависимостей опубликованных Maven POM без запуска Maven.

Поддерживаются родители, свойства и унаследованный dependencyManagement.
Не поддерживаются импорт BOM, профили, диапазоны версий, LATEST/RELEASE,
привязка платформы,
свойства окружения/системы, локальные relativePath родителей и разрешение
SNAPSHOT по maven-metadata.xml. Свойства в координатах родителя разрешаются
только по текущему POM и доступным атрибутам проекта.
Тип и classifier учитываются при сопоставлении, но не входят в формат ответа.
Исключения транзитивных зависимостей не применяются: транзитивный обход не
выполняется. HTTP-перенаправления не разрешены: укажите конечный адрес.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as element_tree
from typing import TypeAlias

from src.errors import ToolError

_MAX_POM_BYTES = 2 * 1024 * 1024
_MAX_PARENTS = 64
_MAX_PROPERTY_DEPTH = 64
_MAX_PROPERTY_SIZE = 16384
_TIMEOUT = 20
_COORDINATE_PARTS = 3
_MAX_HOST_LABEL = 63
_CONTROL_LIMIT = 32
_MAX_CACHED_PROPERTIES = 256
_INVALID_PORT = 0
_REDIRECT_MIN = 300
_REDIRECT_MAX = 400
_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*\Z", re.ASCII)
_GROUP_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*\Z", re.ASCII)
_PROPERTY = re.compile(r"\$\{([^{}]+)\}")
_PATH = re.compile(r"/[A-Za-z0-9/_~.\-]*\Z", re.ASCII)
_HOST = re.compile(r"[A-Za-z0-9.-]+\Z", re.ASCII)
_FIELDS = ("groupId", "artifactId", "version", "scope", "optional", "type",
           "classifier")
Coordinate: TypeAlias = tuple[str, str, str]
Declaration: TypeAlias = dict[str, str]
Key: TypeAlias = tuple[str, str, str, str]


def _coordinate(value: str) -> Coordinate:
    """Проверить координату до использования её в пути URL."""
    if not isinstance(value, str):
        raise ToolError("Координата Maven должна иметь вид "
                        "group:artifact:version")
    parts = value.split(":")
    if len(parts) != _COORDINATE_PARTS:
        raise ToolError(f"Некорректная координата Maven: {value!r}")
    group, artifact, version = parts
    valid_group = all(_GROUP_PART.fullmatch(p) for p in group.split("."))
    if not valid_group or not _PART.fullmatch(artifact):
        raise ToolError(f"Недопустимая координата Maven: {value!r}")
    if (not _PART.fullmatch(version) or ".." in artifact or ".." in version
            or version.upper() in ("LATEST", "RELEASE")):
        raise ToolError(f"Недопустимая версия или artifactId: {value!r}")
    return group, artifact, version


def _valid_host(host: str) -> bool:
    """Разрешить обычные DNS-имена и буквальные IPv6-адреса."""
    if _HOST.fullmatch(host):
        return all(part and len(part) <= _MAX_HOST_LABEL
                   for part in host.split("."))
    try:
        ipaddress.IPv6Address(host)
        return True
    except ValueError:
        return False


def _url_parts(value: str) -> urllib.parse.SplitResult:
    """Разобрать URL, исключив управляющие символы и неверный порт."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ToolError("URL Maven-репозитория должен быть непустой строкой")
    if (any(c in value for c in "\\?#")
            or any(ord(c) < _CONTROL_LIMIT for c in value)):
        raise ToolError(f"Недопустимый URL Maven-репозитория: {value!r}")
    try:
        parsed = urllib.parse.urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ToolError(f"Некорректный URL репозитория: {value!r}") from exc
    return parsed


def _valid_path(path: str) -> bool:
    """Исключить неканонические и закодированные сегменты пути."""
    return not path or bool(_PATH.fullmatch(path)) and all(
        part not in (".", "..") for part in path.split("/")
    )


def _base_url(value: str) -> str:
    """Проверить HTTP(S) URL без учётных данных и опасных сегментов пути."""
    parsed = _url_parts(value)
    host = parsed.hostname
    if parsed.scheme not in ("http", "https") or not host:
        raise ToolError(f"Ожидается HTTP(S) URL Maven-репозитория: {value!r}")
    if (parsed.username is not None or not _valid_host(host)
            or parsed.port == _INVALID_PORT):
        raise ToolError(f"Недопустимый адрес Maven-репозитория: {value!r}")
    if not _valid_path(parsed.path):
        raise ToolError(f"Недопустимый путь Maven-репозитория: {value!r}")
    return value.rstrip("/")


def _child(
    node: element_tree.Element, name: str
) -> element_tree.Element | None:
    """Найти непосредственный дочерний элемент с любым XML namespace."""
    return node.find("{*}" + name)


def _text(node: element_tree.Element, name: str) -> str:
    """Прочитать необязательное значение дочернего элемента."""
    element = _child(node, name)
    return (element.text or "").strip() if element is not None else ""


def _properties(root: element_tree.Element) -> dict[str, str]:
    """Считать объявления свойств POM."""
    result = {}
    node = _child(root, "properties")
    if node is not None:
        for item in node:
            if isinstance(item.tag, str):
                result[item.tag.rsplit("}", 1)[-1]] = (item.text or "").strip()
    return result


def _aliases(coord: Coordinate, parent: Coordinate | None) -> dict[str, str]:
    """Создать поддерживаемые встроенные Maven-свойства проекта."""
    result = {}
    for prefix in ("project", "pom"):
        for name, value in zip(("groupId", "artifactId", "version"), coord):
            result[f"{prefix}.{name}"] = value
    if parent:
        for prefix in ("project.parent", "pom.parent", "parent"):
            names = ("groupId", "artifactId", "version")
            for name, value in zip(names, parent):
                result[f"{prefix}.{name}"] = value
    return result


class Resolver:
    """Разрешать свойства с обнаружением циклов и ограничением размера."""

    def __init__(self, values: dict[str, str], context: str):
        """Сохранить свойства и координату для сообщений об ошибках."""
        self.values = values
        self.context = context
        self.cache: dict[str, str] = {}

    def resolve(self, value: str, stack: tuple[str, ...] = ()) -> str:
        """Подставить свойства и сообщить о неизвестных или циклических."""
        if len(value) > _MAX_PROPERTY_SIZE:
            raise ToolError(f"Слишком большое свойство в {self.context}")
        chunks = []
        size = 0
        start = 0
        for match in _PROPERTY.finditer(value):
            prefix = value[start:match.start()]
            replacement = self._property(match.group(1), stack)
            size += len(prefix) + len(replacement)
            if size > _MAX_PROPERTY_SIZE:
                raise ToolError(f"Слишком большое свойство в {self.context}")
            chunks.extend((prefix, replacement))
            start = match.end()
        chunks.append(value[start:])
        result = "".join(chunks)
        if len(result) > _MAX_PROPERTY_SIZE:
            raise ToolError(f"Слишком большое свойство в {self.context}")
        if "${" in result:
            raise ToolError(f"Некорректная переменная {result!r} в "
                            f"{self.context}")
        return result

    def _property(self, name: str, stack: tuple[str, ...]) -> str:
        """Разрешить одно имя и проверить глубину подстановок."""
        if name in stack or len(stack) >= _MAX_PROPERTY_DEPTH:
            raise ToolError(f"Цикл или большая глубина свойства {name} в "
                            f"{self.context}")
        if name in self.cache:
            return self.cache[name]
        if name not in self.values:
            raise ToolError(f"Неизвестное свойство ${{{name}}} в "
                            f"{self.context}")
        result = self.resolve(self.values[name], stack + (name,))
        if len(self.cache) < _MAX_CACHED_PROPERTIES:
            self.cache[name] = result
        return result


def _parent(
    root: element_tree.Element, coord: Coordinate
) -> Coordinate | None:
    """Получить координату родителя из POM, разрешив локальные свойства."""
    node = _child(root, "parent")
    if node is None:
        return None
    values = _properties(root)
    values.update(_aliases(coord, None))
    resolver = Resolver(values, ":".join(coord))
    fields = [resolver.resolve(_text(node, n)) for n in _FIELDS[:3]]
    try:
        return _coordinate(":".join(fields))
    except ToolError as exc:
        raise ToolError(f"Некорректный родитель POM {resolver.context}: "
                        f"{exc}") from exc


def _identity(root: element_tree.Element, coord: Coordinate,
              parent: Coordinate | None, resolver: Resolver) -> None:
    """Убедиться, что загруженный POM описывает запрошенный артефакт."""
    group = _text(root, "groupId") or (parent[0] if parent else "")
    artifact = _text(root, "artifactId")
    version = _text(root, "version") or (parent[2] if parent else "")
    actual = _coordinate(":".join(resolver.resolve(x) for x in
                                  (group, artifact, version)))
    if actual != coord:
        raise ToolError(f"Координаты POM {':'.join(actual)} не совпадают "
                        f"с запрошенными {':'.join(coord)}")


def _declarations(
    root: element_tree.Element, managed: bool
) -> list[Declaration]:
    """Получить непосредственно объявленные зависимости одного POM."""
    node = _child(root, "dependencyManagement") if managed else root
    if node is None:
        return []
    dependencies = _child(node, "dependencies")
    if dependencies is None:
        return []
    result = []
    for item in dependencies.findall("{*}dependency"):
        result.append({name: _text(item, name) for name in _FIELDS
                       if _text(item, name)})
    return result


def _key(dependency: Declaration, resolver: Resolver) -> Key:
    """Определить Maven-ключ для управления и наследования зависимостей."""
    group = resolver.resolve(dependency.get("groupId", ""))
    artifact = resolver.resolve(dependency.get("artifactId", ""))
    try:
        _coordinate(f"{group}:{artifact}:1")
    except ToolError as exc:
        raise ToolError(f"Некорректная зависимость в {resolver.context}: "
                        f"{exc}") from exc
    kind = resolver.resolve(dependency.get("type", "jar"))
    classifier = resolver.resolve(dependency.get("classifier", ""))
    return group, artifact, kind, classifier


def _merge_declarations(roots: list[element_tree.Element], resolver: Resolver,
                        managed: bool) -> dict[Key, Declaration]:
    """Объединить родительские и дочерние объявления по Maven-ключу."""
    result: dict[Key, Declaration] = {}
    for root in roots:
        for item in _declarations(root, managed):
            if managed and resolver.resolve(item.get("scope", "")) == "import":
                continue
            key = _key(item, resolver)
            previous = result.get(key, {})
            result[key] = {**previous, **item}
    return result


def _included(item: Declaration, resolver: Resolver) -> bool:
    """Отобрать необязательные и неиспользуемые для исполнения зависимости."""
    try:
        scope = resolver.resolve(item.get("scope", "compile"))
    except ToolError:
        optional = resolver.resolve(item.get("optional", "false"))
        if optional.lower() == "true":
            return False
        raise
    if scope in ("test", "provided", "system", "import"):
        return False
    if scope not in ("compile", "runtime"):
        raise ToolError(f"Неизвестный scope {scope!r} в {resolver.context}")
    optional = resolver.resolve(item.get("optional", "false"))
    if optional.lower() not in ("true", "false"):
        raise ToolError(f"Некорректный optional {optional!r} в "
                        f"{resolver.context}")
    return optional.lower() != "true"


def _dependency_coordinate(key: Key, item: Declaration,
                           resolver: Resolver) -> str:
    """Разрешить и проверить итоговую координату прямой зависимости."""
    version = resolver.resolve(item.get("version", ""))
    if not version:
        group, artifact = key[:2]
        raise ToolError(f"Неизвестна версия зависимости {group}:{artifact} в "
                        f"{resolver.context}; импорт BOM не поддерживается")
    try:
        return ":".join(_coordinate(f"{key[0]}:{key[1]}:{version}"))
    except ToolError as exc:
        raise ToolError(f"Некорректная версия в {resolver.context}: "
                        f"{exc}") from exc


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Запретить перенаправления, в том числе на другие протоколы и серверы."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Вернуть отказ по контракту стандартного HTTPRedirectHandler."""
        return None


class MavenRepository:
    """Maven-репозиторий с загрузкой POM по HTTP(S) и кэшем в памяти."""

    def __init__(self, base_url: str):
        """Проверить базовый URL и инициализировать кэши репозитория."""
        self.base_url = _base_url(base_url)
        self._poms: dict[Coordinate, element_tree.Element] = {}
        self._results: dict[Coordinate, tuple[str, ...]] = {}
        self._opener = urllib.request.build_opener(NoRedirect())

    def _url(self, coord: Coordinate) -> str:
        """Построить стандартный путь к опубликованному POM."""
        group, artifact, version = coord
        path = group.replace(".", "/")
        return (f"{self.base_url}/{path}/{artifact}/{version}/"
                f"{artifact}-{version}.pom")

    def _download(self, coord: Coordinate) -> element_tree.Element:
        """Загрузить ограниченный POM и разобрать его как безопасный XML."""
        url = self._url(coord)
        try:
            with self._opener.open(url, timeout=_TIMEOUT) as response:
                data = response.read(_MAX_POM_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if _REDIRECT_MIN <= exc.code < _REDIRECT_MAX:
                raise ToolError(f"HTTP {exc.code} для POM {url}: "
                                "перенаправления запрещены") from exc
            raise ToolError(f"HTTP {exc.code} при загрузке POM {url}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout,
                OSError, ValueError) as exc:
            raise ToolError(f"Не удалось загрузить POM {url}: {exc}") from exc
        if len(data) > _MAX_POM_BYTES:
            raise ToolError(f"POM превышает допустимый размер: {url}")
        lowered = data.replace(b"\x00", b"").lower()
        if b"<!doctype" in lowered or b"<!entity" in lowered:
            raise ToolError(f"DTD/ENTITY запрещены в POM: {url}")
        try:
            root = element_tree.fromstring(data)
        except (element_tree.ParseError, ValueError) as exc:
            raise ToolError(f"Повреждённый XML POM {url}: {exc}") from exc
        root_name = root.tag.rsplit("}", 1)[-1]
        if root_name != "project":
            raise ToolError(f"Корневой элемент POM не project: {url}")
        return root

    def _chain(
        self, coord: Coordinate
    ) -> list[tuple[Coordinate, element_tree.Element]]:
        """Загрузить родителей, обнаруживая циклы и чрезмерную глубину."""
        chain = []
        seen = set()
        while coord is not None:
            if coord in seen:
                raise ToolError(f"Цикл родителей POM: {':'.join(coord)}")
            if len(chain) >= _MAX_PARENTS:
                raise ToolError("Превышена допустимая глубина родителей POM")
            seen.add(coord)
            if coord not in self._poms:
                self._poms[coord] = self._download(coord)
            root = self._poms[coord]
            chain.append((coord, root))
            coord = _parent(root, coord)
        return list(reversed(chain))

    def _resolver(
        self, chain: list[tuple[Coordinate, element_tree.Element]]
    ) -> Resolver:
        """Объединить свойства предков и проверить координаты каждого POM."""
        values: dict[str, str] = {}
        parent = None
        for coord, root in chain:
            values.update(_properties(root))
            values.update(_aliases(coord, parent))
            resolver = Resolver(values, ":".join(coord))
            _identity(root, coord, parent, resolver)
            parent = coord
        return resolver

    def dependencies(self, coordinate: str) -> list[str]:
        """Вернуть зависимости compile/runtime как group:artifact:version.

        HTTP и XML ошибки, циклы, неподдерживаемые диапазоны и неразрешённые
        переменные или версии сопровождаются исключением ToolError.
        """
        coord = _coordinate(coordinate)
        if coord in self._results:
            return list(self._results[coord])
        chain = self._chain(coord)
        resolver = self._resolver(chain)
        roots = [root for _, root in chain]
        managed = _merge_declarations(roots, resolver, True)
        direct = _merge_declarations(roots, resolver, False)
        result = []
        for key, item in direct.items():
            merged = {**managed.get(key, {}), **item}
            if _included(merged, resolver):
                value = _dependency_coordinate(key, merged, resolver)
                if value not in result:
                    result.append(value)
        self._results[coord] = tuple(result)
        return result
