"""A deliberately small, deterministic OpenAPI Overlay applier."""
# Malformed serialized input is reported consistently as ValueError.
# ruff: noqa: TRY004

from __future__ import annotations

from copy import deepcopy
from typing import Any

PathStep = tuple[str, str | int]


def _target_steps(target: str) -> list[PathStep]:
    """Parse the supported JSONPath subset without accepting partial paths."""
    if not isinstance(target, str) or not target.startswith("$"):
        raise ValueError("overlay target must start with '$'")
    steps: list[PathStep] = []
    position = 1
    while position < len(target):
        current = target[position]
        if current == ".":
            position += 1
            start = position
            if position >= len(target) or not (
                target[position].isalpha() or target[position] == "_"
            ):
                raise ValueError(f"unsupported JSONPath target: {target!r}")
            position += 1
            while position < len(target) and (
                target[position].isalnum() or target[position] == "_"
            ):
                position += 1
            steps.append(("key", target[start:position]))
            continue
        if current != "[":
            raise ValueError(f"unsupported JSONPath target: {target!r}")
        position += 1
        if position >= len(target):
            raise ValueError(f"unsupported JSONPath target: {target!r}")
        if target[position] in "'\"":
            quote = target[position]
            position += 1
            chars: list[str] = []
            while position < len(target) and target[position] != quote:
                if target[position] == "\\":
                    position += 1
                    if position >= len(target):
                        raise ValueError(f"unsupported JSONPath target: {target!r}")
                    if target[position] not in (quote, "\\"):
                        raise ValueError(f"unsupported JSONPath escape: {target!r}")
                chars.append(target[position])
                position += 1
            if position >= len(target) or target[position] != quote:
                raise ValueError(f"unsupported JSONPath target: {target!r}")
            position += 1
            steps.append(("key", "".join(chars)))
        else:
            start = position
            while position < len(target) and target[position].isdigit():
                position += 1
            if start == position:
                raise ValueError(f"unsupported JSONPath target: {target!r}")
            steps.append(("index", int(target[start:position])))
        if position >= len(target) or target[position] != "]":
            raise ValueError(f"unsupported JSONPath target: {target!r}")
        position += 1
    return steps


def _parent(document: Any, steps: list[PathStep], target: str) -> tuple[Any, PathStep]:
    if not steps:
        raise ValueError(f"overlay target has no element: {target!r}")
    node = document
    for kind, value in steps[:-1]:
        try:
            if kind == "key":
                if not isinstance(node, dict) or value not in node:
                    raise KeyError(value)
                node = node[value]
            else:
                if not isinstance(node, list) or not isinstance(value, int):
                    raise KeyError(value)
                node = node[value]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"overlay target does not exist: {target!r}") from exc
    return node, steps[-1]


def _merge(existing: Any, update: Any) -> Any:
    if isinstance(existing, dict) and isinstance(update, dict):
        merged = deepcopy(existing)
        for key, value in update.items():
            merged[key] = _merge(merged[key], value) if key in merged else deepcopy(value)
        return merged
    if isinstance(existing, list) and isinstance(update, list):
        return deepcopy(existing) + deepcopy(update)
    return deepcopy(update)


def _apply_action(document: Any, action: dict[str, Any]) -> None:
    target = action.get("target")
    if not isinstance(target, str):
        raise ValueError("overlay action target must be a string")
    steps = _target_steps(target)
    if "copy" in action:
        raise ValueError("overlay copy is unsupported")
    has_update = "update" in action
    has_remove = action.get("remove") is True
    if has_update == has_remove:
        raise ValueError("overlay action must contain exactly one of update or remove")
    if not steps:
        if has_remove:
            raise ValueError("cannot remove the overlay document root")
        if not isinstance(document, dict) or not isinstance(action["update"], dict):
            raise ValueError("root overlay update must be an object")
        merged = _merge(document, action["update"])
        document.clear()
        document.update(merged)
        return
    parent, (kind, value) = _parent(document, steps, target)
    if kind == "key":
        if not isinstance(parent, dict):
            raise ValueError(f"overlay target does not exist: {target!r}")
        exists = value in parent
    else:
        exists = isinstance(parent, list) and isinstance(value, int) and value < len(parent)
    if has_remove:
        if not exists:
            raise ValueError(f"overlay target does not exist: {target!r}")
        del parent[value]
        return
    if not exists:
        raise ValueError(f"overlay target does not exist: {target!r}")
    parent[value] = _merge(parent[value], action["update"])


def apply_overlay(document: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Return an enriched copy of *document* without changing either input."""
    result = deepcopy(document)
    actions = overlay.get("actions")
    if not isinstance(actions, list):
        raise ValueError("overlay actions must be a list")
    for action in actions:
        if not isinstance(action, dict):
            raise ValueError("overlay action must be an object")
        _apply_action(result, action)
    return result
