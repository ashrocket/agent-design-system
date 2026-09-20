from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .model import Match


class SelectorError(ValueError):
    pass


def parse_selector(selector: str) -> tuple[str, ...]:
    if not isinstance(selector, str):
        raise SelectorError("selector must be a string")
    if selector == "$":
        return ()
    if not selector.startswith("$."):
        raise SelectorError(f"selector must start with '$.': {selector}")
    parts = tuple(selector[2:].split("."))
    if any(not part for part in parts):
        raise SelectorError(f"selector contains an empty segment: {selector}")
    return parts


def _path_string(segments: tuple[str | int, ...]) -> str:
    output = "$"
    for segment in segments:
        if isinstance(segment, int):
            output += f"[{segment}]"
        else:
            output += f".{segment}"
    return output


def select(document: Any, selector: str) -> list[Match]:
    pattern = parse_selector(selector)
    matches: list[Match] = []

    def walk(value: Any, index: int, path: tuple[str | int, ...]) -> None:
        if index == len(pattern):
            matches.append(Match(_path_string(path), path, value))
            return

        segment = pattern[index]
        if segment == "**":
            walk(value, index + 1, path)
            for key, child in _children(value):
                walk(child, index, path + (key,))
            return

        if segment == "*":
            for key, child in _children(value):
                walk(child, index + 1, path + (key,))
            return

        if isinstance(value, dict) and segment in value:
            walk(value[segment], index + 1, path + (segment,))
        elif isinstance(value, list) and segment.isdigit():
            child_index = int(segment)
            if 0 <= child_index < len(value):
                walk(value[child_index], index + 1, path + (child_index,))

    walk(document, 0, ())
    return matches


def _children(value: Any) -> Iterator[tuple[str | int, Any]]:
    if isinstance(value, dict):
        yield from value.items()
    elif isinstance(value, list):
        yield from enumerate(value)


def assign(document: Any, segments: tuple[str | int, ...], value: Any) -> None:
    if not segments:
        raise SelectorError("cannot replace the document root")
    parent = document
    for segment in segments[:-1]:
        parent = parent[segment]
    parent[segments[-1]] = value
