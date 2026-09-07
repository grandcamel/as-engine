"""Input conversion and bounded JSON-schema validation for the generic surface."""

# ruff: noqa: TRY004
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from .index import Operation

_UNSUPPORTED_VALIDATORS = {
    "const",
    "contains",
    "dependentRequired",
    "exclusiveMaximum",
    "exclusiveMinimum",
    "maxProperties",
    "minProperties",
    "multipleOf",
    "not",
    "pattern",
    "patternProperties",
    "propertyNames",
    "uniqueItems",
}


def kebab_case(name: str) -> str:
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name)
    words = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", words)
    return re.sub(r"[^a-zA-Z0-9]+", "-", words).strip("-").lower()


def _resolve(
    schema: Any, schemas: Mapping[str, Any], seen: frozenset[str] = frozenset()
) -> dict[str, Any]:
    if not isinstance(schema, dict):
        raise ValueError("schema must be an object")
    ref = schema.get("$ref")
    if ref is None:
        return schema
    if not isinstance(ref, str) or not ref.startswith("#/components/schemas/"):
        raise ValueError("only local component schema references are supported")
    name = ref.removeprefix("#/components/schemas/").replace("~1", "/").replace("~0", "~")
    if name in seen:
        raise ValueError(f"cyclic schema reference: {ref}")
    target = schemas.get(name)
    if not isinstance(target, dict):
        raise ValueError(f"unknown schema reference: {ref}")
    return _resolve(target, schemas, seen | {name})


def _bounds_errors(value: Any, schema: Mapping[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for key, predicate, message in (
            ("minimum", lambda a, b: a < b, "below minimum"),
            ("maximum", lambda a, b: a > b, "above maximum"),
        ):
            if key in schema and predicate(value, schema[key]):
                errors.append(f"{path}: {message}")
    if isinstance(value, (list, str)):
        keys = ("minItems", "maxItems") if isinstance(value, list) else ("minLength", "maxLength")
        for key, predicate, message in (
            (keys[0], lambda a, b: a < b, "too short"),
            (keys[1], lambda a, b: a > b, "too long"),
        ):
            if key in schema and predicate(len(value), schema[key]):
                errors.append(f"{path}: {message}")
    return errors


def _value_errors(value: Any, schema: Any, schemas: Mapping[str, Any], path: str) -> list[str]:
    try:
        resolved = _resolve(schema, schemas)
    except ValueError as exc:
        return [f"{path}: {exc}"]
    unsupported = _UNSUPPORTED_VALIDATORS.intersection(resolved)
    if unsupported:
        return [f"{path}: unsupported schema validator: {min(unsupported)}"]
    if isinstance(resolved.get("additionalProperties"), dict):
        return [f"{path}: unsupported schema validator: additionalProperties"]
    if value is None and resolved.get("nullable") is True:
        return []
    errors: list[str] = []
    for keyword in ("allOf", "anyOf", "oneOf"):
        branches = resolved.get(keyword)
        if branches is not None and not isinstance(branches, list):
            return [f"{path}: {keyword} must be an array"]
        if isinstance(branches, list):
            branch_errors = [_value_errors(value, branch, schemas, path) for branch in branches]
            if keyword == "allOf":
                errors.extend(error for group in branch_errors for error in group)
            elif (keyword == "oneOf" and sum(not group for group in branch_errors) != 1) or (
                keyword == "anyOf" and not any(not group for group in branch_errors)
            ):
                errors.append(f"{path}: does not match {keyword}")
    kind = resolved.get("type")
    valid = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value),
        "boolean": isinstance(value, bool),
        None: True,
        "unknown": True,
    }
    if kind not in valid:
        return [f"{path}: unsupported schema type: {kind}"]
    if not valid[kind]:
        return [f"{path}: must be {kind}"]
    if "enum" in resolved and (
        not isinstance(resolved["enum"], list) or value not in resolved["enum"]
    ):
        errors.append(f"{path}: is not an allowed value")
    errors.extend(_bounds_errors(value, resolved, path))
    if isinstance(value, dict):
        required, properties = resolved.get("required", []), resolved.get("properties", {})
        if not isinstance(required, list) or not isinstance(properties, dict):
            return [f"{path}: invalid object schema"]
        errors.extend(f"{path}.{name}: is required" for name in required if name not in value)
        if resolved.get("additionalProperties") is False:
            errors.extend(
                f"{path}.{name}: is not allowed" for name in value if name not in properties
            )
        for name, child in properties.items():
            if name in value:
                errors.extend(_value_errors(value[name], child, schemas, f"{path}.{name}"))
    if isinstance(value, list):
        items = resolved.get("items")
        if items is not None and not isinstance(items, dict):
            return [f"{path}: items must be an object"]
        if isinstance(items, dict):
            for index, child in enumerate(value):
                errors.extend(_value_errors(child, items, schemas, f"{path}[{index}]"))
    return errors


def _coerce(raw: Any, schema: Any, schemas: Mapping[str, Any], path: str) -> Any:
    resolved = _resolve(schema, schemas)
    kind = resolved.get("type")
    if kind is None or kind == "unknown":
        value = raw
    elif kind == "string":
        if not isinstance(raw, str):
            raise ValueError("must be a string")
        value = raw
    elif kind == "integer":
        if isinstance(raw, bool) or not (
            isinstance(raw, int) or isinstance(raw, str) and re.fullmatch(r"[+-]?\d+", raw)
        ):
            raise ValueError("must be an integer")
        value = int(raw)
    elif kind == "number":
        if isinstance(raw, bool):
            raise ValueError("must be a finite number")
        try:
            value = float(raw) if isinstance(raw, str) else raw
        except ValueError:
            raise ValueError("must be a finite number") from None
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("must be a finite number")
    elif kind == "boolean":
        if isinstance(raw, bool):
            value = raw
        elif isinstance(raw, str) and raw in {"true", "false"}:
            value = raw == "true"
        else:
            raise ValueError("must be true or false")
    elif kind in {"array", "object"}:
        if isinstance(raw, str):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                if kind == "array":
                    value = raw.split(",") if raw else []
                else:
                    raise ValueError("must be a JSON object") from None
        else:
            value = raw
        if not isinstance(value, list if kind == "array" else dict):
            raise ValueError(f"must be a {kind}")
        if kind == "array" and isinstance(resolved.get("items"), dict):
            value = [
                _coerce(item, resolved["items"], schemas, f"{path}[{index}]")
                for index, item in enumerate(value)
            ]
        if kind == "object" and isinstance(resolved.get("properties"), dict):
            value = {
                name: _coerce(item, resolved["properties"][name], schemas, f"{path}.{name}")
                if name in resolved["properties"]
                else item
                for name, item in value.items()
            }
    else:
        raise ValueError(f"unsupported parameter type: {kind}")
    errors = _value_errors(value, resolved, schemas, path)
    if errors:
        raise ValueError(errors[0].removeprefix(f"{path}: "))
    return value


def validate_parameters(
    operation: Operation, parameters: Mapping[str, Any], schemas: Mapping[str, Any]
) -> dict[str, Any]:
    known = {parameter.get("name") for parameter in operation.parameters}
    unknown = set(parameters).difference(known)
    if unknown:
        raise ValueError(f"unknown parameter: {min(unknown)}")
    result: dict[str, Any] = {}
    for parameter in operation.parameters:
        name = parameter.get("name")
        if not isinstance(name, str):
            raise ValueError("parameter has no name")
        if name not in parameters:
            if parameter.get("required"):
                raise ValueError(f"missing required parameter: {name}")
            continue
        schema = (
            parameter.get("schema")
            if isinstance(parameter.get("schema"), dict)
            else {
                key: parameter[key]
                for key in ("type", "enum", "minimum", "maximum", "items", "properties")
                if key in parameter
            }
        )
        try:
            result[name] = _coerce(parameters[name], schema, schemas, name)
        except ValueError as exc:
            raise ValueError(f"invalid parameter {name}: {exc}") from exc
    return result


def build_body(source: str | None, fields: Sequence[str], stdin: TextIO | None = None) -> Any:
    body: Any = None
    if source is not None:
        if source == "-":
            content = stdin.read() if stdin is not None else None
        elif source.startswith("@") and len(source) > 1:
            try:
                content = Path(source[1:]).read_text(encoding="utf-8")
            except OSError as exc:
                raise ValueError(f"cannot read body file: {source[1:]}") from exc
        else:
            raise ValueError("body source must be @file or -")
        if content is None:
            raise ValueError("stdin body source requires stdin")
        try:
            body = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("body source is not valid JSON") from exc
    if not fields:
        return body
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise ValueError("dotted fields require an object body")
    for field in fields:
        if "=" not in field:
            raise ValueError("body field must be path=value")
        path, raw = field.split("=", 1)
        pieces = path.split(".")
        if not path or any(not piece for piece in pieces):
            raise ValueError("body field path is invalid")
        current = body
        for piece in pieces[:-1]:
            if piece not in current:
                current[piece] = {}
            if not isinstance(current[piece], dict):
                raise ValueError(f"body field collides at {piece}")
            current = current[piece]
        if pieces[-1] in current:
            raise ValueError(f"body field collides at {path}")
        try:
            current[pieces[-1]] = json.loads(raw)
        except json.JSONDecodeError:
            current[pieces[-1]] = raw
    return body


def body_errors(operation: Operation, body: Any, schemas: Mapping[str, Any]) -> list[str]:
    request_body = operation.requestBody
    required = bool(getattr(operation, "request_body_required", False)) or bool(
        isinstance(request_body, dict) and request_body.get("required")
    )
    if body is None:
        return ["body: is required"] if required else []
    if not isinstance(request_body, dict):
        return []
    schema = request_body.get("schema")
    if schema is None and isinstance(request_body.get("ref"), str):
        schema = {
            "$ref": "#/components/schemas/"
            + request_body["ref"].replace("~", "~0").replace("/", "~1")
        }
    return _value_errors(body, schema, schemas, "body") if schema is not None else []


def alias_flags(operation: Operation) -> dict[str, dict[str, Any]]:
    """Return key-form aliases exactly as declared by prerequisite tags."""
    rules = operation.extensions.get("x-as-prerequisites", [])
    if not isinstance(rules, list):
        raise ValueError("x-as-prerequisites must be an array")
    result: dict[str, dict[str, Any]] = {}
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("alias"), str):
            raise ValueError("prerequisite must declare an alias")
        alias = rule["alias"]
        if alias in {"body", "field", "format", "validate-body", "help", "all", "limit", "version"}:
            raise ValueError("prerequisite alias collides with a call option")
        target = rule.get("target")
        if not isinstance(target, dict) or target.get("in") not in {"body", "path", "query"}:
            raise ValueError("prerequisite target must name a body, path or query input")
        field = "path" if target["in"] == "body" else "name"
        if not isinstance(target.get(field), str) or not target[field]:
            raise ValueError("prerequisite target is missing its path or name")
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", alias) or alias in result:
            raise ValueError("invalid or duplicate prerequisite alias")
        result[alias] = rule
    return result
