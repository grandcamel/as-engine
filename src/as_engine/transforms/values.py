"""JSON Pointer and tag destination helpers (no operation-specific names)."""

# Invalid tag/data types use the public ValueError usage contract.
# ruff: noqa: TRY004
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..params import _coerce, _resolve
from . import Context

MISSING = object()


def parts(path: str) -> list[str]:
    if not isinstance(path, str) or (path and not path.startswith("/")):
        raise ValueError("tag path must be a JSON Pointer")
    if re.search(r"~(?![01])", path):
        raise ValueError("invalid JSON Pointer escape")
    return [p.replace("~1", "/").replace("~0", "~") for p in path[1:].split("/")] if path else []


def pointer(value: Any, path: str, default: Any = MISSING) -> Any:
    try:
        for part in parts(path):
            if isinstance(value, list):
                if not part.isdigit():
                    return default
                value = value[int(part)]
            else:
                value = value[part]
        return value
    except (KeyError, IndexError, TypeError):
        return default


def required(value: Any, path: str) -> Any:
    result = pointer(value, path)
    if result is MISSING:
        raise ValueError(f"missing required response metadata: {path}")
    return result


def parameter(context: Context, target: Mapping[str, Any]) -> dict[str, Any]:
    matches = [
        p
        for p in context.operation.parameters
        if p["name"] == target.get("name") and p.get("in") == target.get("in")
    ]
    if len(matches) != 1:
        raise ValueError("tag names an unknown or ambiguous parameter")
    return matches[0]


def target_value(context: Context, target: Mapping[str, Any]) -> Any:
    if target.get("in") == "body":
        return pointer(context.body, target["path"])
    parameter(context, target)
    return context.parameters.get(target["name"], MISSING)


def set_target(context: Context, target: Mapping[str, Any], value: Any) -> None:
    if target.get("in") != "body":
        p = parameter(context, target)
        context.parameters[p["name"]] = _coerce(
            value, p.get("schema", p), context.index.schemas, p["name"]
        )
        return
    keys = parts(target["path"])
    if not keys:
        raise ValueError("body target must name a field")
    request = context.operation.requestBody or {}
    schema = request.get("schema", {})
    if "ref" in request:
        schema = context.index.schemas.get(request["ref"], {})
    for key in keys:
        resolved = _resolve(schema, context.index.schemas)
        properties = dict(resolved.get("properties", {}))
        for child in resolved.get("allOf", []):
            properties.update(_resolve(child, context.index.schemas).get("properties", {}))
        schema = properties.get(key, {})
    value = _coerce(value, schema, context.index.schemas, target["path"])
    if context.body is None:
        context.body = {}
    current = context.body
    for key in keys[:-1]:
        if not isinstance(current, dict):
            raise ValueError("body target collides with a non-object")
        current = current.setdefault(key, {})
    if not isinstance(current, dict):
        raise ValueError("body target collides with a non-object")
    current[keys[-1]] = value
