"""Командный интерфейс программы."""

import argparse
import sys

from src.config import Config, load_config
from src.errors import ToolError
from src.graph import build_graph
from src.maven import MavenRepository
from src.reverse import reverse_dependencies
from src.test_repository import TestRepository


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """Читает расположение CSV-конфигурации и цель обратного поиска."""
    parser = argparse.ArgumentParser(description="Граф зависимостей Maven")
    parser.add_argument("--config", default="config.csv", help="файл настроек")
    parser.add_argument("--reverse", metavar="PACKAGE", help="обратные связи")
    return parser.parse_args(argv)


def graph_from_config(config: Config) -> dict[str, list[str]]:
    """Загружает доступные зависимости и обходит граф без рекурсии."""
    if config.test_mode:
        repository = TestRepository(config.repository_path())
    else:
        repository = MavenRepository(config.repository)
    return build_graph(config.package, repository.dependencies, config.filter)


def show_graph(graph: dict[str, list[str]]) -> None:
    """Печатает вершины и рёбра на третьем этапе разработки."""
    for package, children in graph.items():
        print(f"{package}: {', '.join(children) if children else '-'}")


def show_reverse(graph: dict[str, list[str]], target: str) -> None:
    """Выводит предков узла по рёбрам построенного графа."""
    print(f"Обратные зависимости {target}:")
    for package in reverse_dependencies(graph, target):
        print(package)


def main(argv: list[str] | None = None) -> int:
    """Строит граф и при запросе показывает обратные зависимости."""
    args = arguments(argv)
    try:
        config = load_config(args.config)
        graph = graph_from_config(config)
        if args.reverse is None:
            show_graph(graph)
        else:
            show_reverse(graph, args.reverse)
        return 0
    except ToolError as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
