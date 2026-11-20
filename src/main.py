"""Командный интерфейс программы."""

import argparse
import sys

from src.config import load_config
from src.errors import ToolError
from src.maven import MavenRepository


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """Читает расположение CSV-конфигурации."""
    parser = argparse.ArgumentParser(description="Граф зависимостей Maven")
    parser.add_argument("--config", default="config.csv", help="файл настроек")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """На втором этапе показывает прямые зависимости Maven-пакета."""
    args = arguments(argv)
    try:
        config = load_config(args.config)
        if config.test_mode:
            raise ToolError("test_mode: поддержка появится на третьем этапе")
        repository = MavenRepository(config.repository)
        dependencies = repository.dependencies(config.package)
        print(f"Прямые зависимости {config.package}:")
        for dependency in dependencies:
            print(dependency)
        return 0
    except ToolError as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
