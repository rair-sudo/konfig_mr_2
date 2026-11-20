"""Загрузка и проверка конфигурации из CSV."""

import csv
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
import re
from urllib.parse import urlsplit

from src.errors import ToolError

KEYS = {"package", "repository", "test_mode", "output", "filter"}
TEST_NAME = re.compile(r"[A-Z]+\Z")
GROUP = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\Z")
PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*\Z")
CONTROL_LIMIT = 32
MAX_FILTER_LENGTH = 256
FIELD_COUNT = 2
COORDINATE_COUNT = 3


@dataclass(frozen=True)
class Config:
    """Проверенные параметры и папка файла настроек."""

    package: str
    repository: str
    test_mode: bool
    output: str
    filter: str
    base_dir: Path

    def repository_path(self) -> Path:
        """Разрешает относительный путь к тестовому репозиторию."""
        return self.base_dir / self.repository

    def output_path(self) -> Path:
        """Разрешает относительный путь к создаваемому SVG."""
        return self.base_dir / self.output


def _read_rows(path: Path) -> dict[str, str]:
    """Читает ровно две CSV-колонки и отклоняет повторы и лишние ключи."""
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream, strict=True)
            if next(reader, None) != ["key", "value"]:
                raise ToolError("config: ожидается заголовок key,value")
            values: dict[str, str] = {}
            for row in reader:
                _add_row(values, row, reader.line_num)
    except (OSError, UnicodeError, csv.Error) as error:
        raise ToolError(f"config: {path}: {error}") from error
    missing = KEYS - values.keys()
    if missing:
        names = ", ".join(sorted(missing))
        raise ToolError(f"config: отсутствуют ключи: {names}")
    return values


def _add_row(values: dict[str, str], row: list[str], line: int) -> None:
    """Проверяет одну строку настроек и добавляет её в словарь."""
    if len(row) != FIELD_COUNT:
        raise ToolError(f"config: строка {line}: ожидаются два поля")
    key, value = row[0].strip(), row[1].strip()
    if key not in KEYS:
        raise ToolError(f"config: строка {line}: неизвестный ключ {key!r}")
    if key in values:
        raise ToolError(f"config: строка {line}: повторный ключ {key!r}")
    values[key] = value


def _test_mode(value: str) -> bool:
    """Преобразует строго true или false в логический признак."""
    if value.lower() not in ("true", "false"):
        raise ToolError("test_mode: требуется true или false")
    return value.lower() == "true"


def _validate_package(package: str, test_mode: bool) -> None:
    """Проверяет имя A-Z либо координаты group:artifact:version."""
    if test_mode:
        if not TEST_NAME.fullmatch(package):
            raise ToolError("package: в тестовом режиме нужны заглавные A-Z")
        return
    parts = package.split(":")
    if len(parts) != COORDINATE_COUNT or not GROUP.fullmatch(parts[0]):
        raise ToolError("package: ожидается group:artifact:version")
    if any(not PART.fullmatch(part) or ".." in part for part in parts[1:]):
        raise ToolError("package: недопустимые координаты Maven")


def _validate_url(url: str) -> None:
    """Проверяет HTTP(S) URL без учётных данных, параметров и фрагмента."""
    try:
        parsed = urlsplit(url)
        valid = (not any(char.isspace() for char in url)
                 and parsed.scheme in ("http", "https") and parsed.hostname
                 and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment)
        _port = parsed.port
    except ValueError as error:
        raise ToolError(f"repository: неверный URL: {error}") from error
    if not valid:
        raise ToolError("repository: нужен HTTP(S) URL без логина и параметров")


def _validate_repository(value: str, test_mode: bool, base: Path) -> None:
    """Проверяет URL Maven или доступность файла тестового репозитория."""
    if not value:
        raise ToolError("repository: путь или URL не задан")
    if test_mode:
        if not (base / value).is_file():
            raise ToolError(f"repository: файл не найден: {value}")
    else:
        _validate_url(value)


def _validate_output(value: str) -> None:
    """Проверяет имя SVG, в том числе недопустимые символы Windows."""
    path = PureWindowsPath(value)
    if not value or path.suffix.lower() != ".svg" or path.is_reserved():
        raise ToolError("output: укажите допустимое имя файла .svg")
    parts = [part for part in path.parts if part != path.anchor]
    if any(any(char in '<>:"|?*' for char in part) for part in parts):
        raise ToolError("output: в имени есть недопустимые символы Windows")
    if any(ord(char) < CONTROL_LIMIT for char in value):
        raise ToolError("output: управляющие символы запрещены")


def _validate_filter(value: str) -> None:
    """Разрешает пустой фильтр и проверяет длину и управляющие символы."""
    if len(value) > MAX_FILTER_LENGTH:
        raise ToolError("filter: слишком длинная подстрока")
    if any(ord(char) < CONTROL_LIMIT for char in value):
        raise ToolError("filter: управляющие символы запрещены")


def load_config(path: str | Path) -> Config:
    """Возвращает проверенные настройки из CSV или сообщает об ошибке."""
    file = Path(path)
    values = _read_rows(file)
    mode = _test_mode(values["test_mode"])
    base = file.resolve().parent
    _validate_package(values["package"], mode)
    _validate_repository(values["repository"], mode, base)
    _validate_output(values["output"])
    _validate_filter(values["filter"])
    return Config(values["package"], values["repository"], mode,
                  values["output"], values["filter"], base)


def describe(config: Config) -> str:
    """Возвращает пять параметров в виде ключ=значение."""
    mode = str(config.test_mode).lower()
    return (f"package={config.package}\nrepository={config.repository}\n"
            f"test_mode={mode}\noutput={config.output}\n"
            f"filter={config.filter}")
