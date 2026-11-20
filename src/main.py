"""Командный интерфейс программы."""

import argparse
import sys

from src.config import describe, load_config
from src.errors import ToolError


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """Читает расположение CSV-конфигурации."""
    parser = argparse.ArgumentParser(description="Граф зависимостей Maven")
    parser.add_argument("--config", default="config.csv", help="файл настроек")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Загружает и показывает все настройки первого этапа."""
    args = arguments(argv)
    try:
        config = load_config(args.config)
        print(describe(config))
        return 0
    except ToolError as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
