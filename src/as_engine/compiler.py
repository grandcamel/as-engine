"""Compile an enriched OpenAPI document into a compact operation index."""
# Malformed serialized input is reported consistently as ValueError.
# ruff: noqa: TRY004

from __future__ import annotations

import re
from collections.abc import Iterable
from copy import deepcopy
from typing import Any

from .normalize import Normalizer, normalize_document
from .overlay import apply_overlay

_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


def _pointer(document: dict[str, Any], ref: str, context: str) -> Any:
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise ValueError(f"external {context} reference is unsupported: {ref!r}")
    value: Any = document
    try:
        for part in ref[2:].split("/"):
            value = value[part.replace("~1", "/").replace("~0", "~")]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"missing {context} reference: {ref!r}") from exc
    return value


def _resolve(document: dict[str, Any], value: Any, context: str) -> Any:
    seen: set[str] = set()
    while isinstance(value, dict) and "$ref" in value:
        ref = value["$ref"]
        if not isinstance(ref, str) or ref in seen:
            raise ValueError(f"cyclic {context} reference: {ref!r}")
        seen.add(ref)
        resolved = _pointer(document, ref, context)
        if not isinstance(resolved, dict):
            raise ValueError(f"{context} reference does not resolve to an object: {ref!r}")
        value = resolved
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    return value


def _schema_name(document: dict[str, Any], schema: Any) -> str | None:
    if not isinstance(schema, dict) or "$ref" not in schema:
        return None
    ref = schema["$ref"]
    prefixes = ("#/components/schemas/",)
    prefix = next(
        (item for item in prefixes if isinstance(ref, str) and ref.startswith(item)), None
    )
    if prefix is None:
        _pointer(document, ref, "schema")
        raise ValueError(f"schema reference must name a component schema: {ref!r}")
    name = ref[len(prefix) :].replace("~1", "/").replace("~0", "~")
    _pointer(document, ref, "schema")
    return name


def _schema_refs(document: dict[str, Any], value: Any, found: set[str]) -> None:
    """Follow schema positions, never literal examples, defaults or extensions."""
    if not isinstance(value, dict):
        return
    name = _schema_name(document, value)
    if name:
        found.add(name)
    for keyword in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
        children = value.get(keyword, {})
        if isinstance(children, dict):
            for child in children.values():
                _schema_refs(document, child, found)
    for keyword in (
        "items",
        "additionalItems",
        "additionalProperties",
        "not",
        "contains",
        "propertyNames",
        "unevaluatedProperties",
        "unevaluatedItems",
        "if",
        "then",
        "else",
    ):
        _schema_refs(document, value.get(keyword), found)
    for keyword in ("allOf", "oneOf", "anyOf", "prefixItems"):
        children = value.get(keyword, [])
        if isinstance(children, list):
            for child in children:
                _schema_refs(document, child, found)


def _content_schemas(document: dict[str, Any], value: Any, context: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    resolved = _resolve(document, value, context)
    direct_schema = resolved.get("schema")
    if isinstance(direct_schema, dict):
        return [direct_schema]
    content = resolved.get("content", {})
    if not isinstance(content, dict):
        raise ValueError(f"{context} content must be an object")
    if not content:
        return []
    schemas: list[dict[str, Any]] = []
    for media_type in sorted(content, key=lambda item: (item != "application/json", item)):
        media = content[media_type]
        if not isinstance(media, dict):
            raise ValueError(f"{context} media type must be an object")
        schema = media.get("schema")
        if isinstance(schema, dict):
            schemas.append(schema)
    return schemas


def _content_schema(document: dict[str, Any], value: Any, context: str) -> dict[str, Any] | None:
    schemas = _content_schemas(document, value, context)
    return schemas[0] if schemas else None


def _parameter(document: dict[str, Any], value: Any) -> dict[str, Any]:
    parameter = _resolve(document, value, "parameter")
    schema = parameter.get("schema")
    if schema is None:
        schema = {
            key: deepcopy(parameter[key])
            for key in ("type", "enum", "format", "items", "properties")
            if key in parameter
        }
    if not isinstance(schema, dict):
        raise ValueError("parameter schema must be an object")
    name = _schema_name(document, schema)
    details = _resolve(document, schema, "parameter schema") if name else schema
    projection: dict[str, Any] = {
        "name": parameter.get("name"),
        "in": parameter.get("in"),
        "required": bool(parameter.get("required", False)),
        "type": details.get("type", "unknown"),
    }
    if "enum" in details:
        projection["enum"] = deepcopy(details["enum"])
    if name:
        projection["schema"] = {"$ref": schema["$ref"]}
    elif schema and ("format" in schema or "items" in schema or "properties" in schema):
        projection["schema"] = deepcopy(schema)
    for key in ("style", "explode"):
        if key in parameter:
            projection[key] = deepcopy(parameter[key])
    return projection


def _first_paragraph(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return re.split(r"\r?\n[ \t]*\r?\n", value, maxsplit=1)[0].strip() or None


def _operation_record(
    document: dict[str, Any],
    path: str,
    method: str,
    path_item: dict[str, Any],
    operation: dict[str, Any],
) -> tuple[str, dict[str, Any], set[str]]:
    op_id = operation.get("operationId")
    if not isinstance(op_id, str) or not op_id:
        raise ValueError(f"operation at {method.upper()} {path} has no operationId")
    combined: dict[tuple[Any, Any], Any] = {}
    raw_parameters = list(path_item.get("parameters", [])) + list(operation.get("parameters", []))
    for value in raw_parameters:
        parameter = _resolve(document, value, "parameter")
        combined[(parameter.get("name"), parameter.get("in"))] = value
    parameters = [_parameter(document, value) for value in combined.values()]
    roots: set[str] = set()
    for value in combined.values():
        _schema_refs(document, _resolve(document, value, "parameter").get("schema", {}), roots)
    request_schema = _content_schema(document, operation.get("requestBody"), "requestBody")
    request_body: dict[str, Any] | None = None
    if request_schema is not None:
        name = _schema_name(document, request_schema)
        request_body = (
            {"ref": name} if name else {"inline": True, "schema": deepcopy(request_schema)}
        )
        _schema_refs(document, request_schema, roots)
    for candidate in _content_schemas(document, operation.get("requestBody"), "requestBody"):
        _schema_refs(document, candidate, roots)
    responses = operation.get("responses") or {}
    if not isinstance(responses, dict):
        raise ValueError("responses must be an object")
    response = responses.get("200")
    response_schema = _content_schema(document, response, "response")
    response_200 = _schema_name(document, response_schema) if response_schema is not None else None
    for status, raw_response in responses.items():
        if str(status).startswith("x-"):
            continue
        for candidate in _content_schemas(document, raw_response, "response"):
            _schema_refs(document, candidate, roots)
    record = {
        "operationId": op_id,
        "method": method.upper(),
        "path": path,
        "tags": deepcopy(operation.get("tags", [])),
        "summary": operation.get("summary"),
        "description": _first_paragraph(operation.get("description")),
        "parameters": parameters,
        "requestBody": request_body,
        "response_200": response_200,
        "extensions": {
            key: deepcopy(value) for key, value in operation.items() if key.startswith("x-")
        },
    }
    full_description = operation.get("description")
    if isinstance(full_description, str) and full_description != record["description"]:
        record["full_description"] = full_description
    if response_schema is not None and response_200 is None:
        record["response_schema"] = deepcopy(response_schema)
    if response is not None:
        content = _resolve(document, response, "response").get("content", {})
        media: dict[str, Any] = next((content[k] for k in sorted(content, key=lambda k: (k != "application/json", k))), {})
        if "example" in media:
            record["response_example"] = deepcopy(media["example"])
        elif media.get("examples"):
            example = next(iter(media["examples"].values()))
            example = _resolve(document, example, "example")
            if "value" in example:
                record["response_example"] = deepcopy(example["value"])
    if "deprecated" in operation:
        record["deprecated"] = bool(operation["deprecated"])
    if operation.get("requestBody") is not None:
        request = _resolve(document, operation["requestBody"], "requestBody")
        if "required" in request:
            record["request_body_required"] = bool(request["required"])
        if "content" in request:
            record["request_media_types"] = sorted(request["content"])
    return op_id, record, roots


def compile_document(
    document: dict[str, Any],
    overlays: Iterable[dict[str, Any]] = (),
    *,
    normalizers: Iterable[Normalizer] = (),
    strip_extensions: Iterable[str] = ("x-atlassian-narrative",),
) -> dict[str, Any]:
    """Compile a document purely; overlay metadata and inputs remain untouched."""
    if not isinstance(document.get("openapi"), str) or not document["openapi"].startswith("3."):
        raise ValueError("compiler requires an OpenAPI 3 document")
    enriched = normalize_document(document, strip_extensions=strip_extensions)
    for overlay in overlays:
        enriched = apply_overlay(enriched, overlay)
    enriched = normalize_document(
        enriched, normalizers=normalizers, strip_extensions=strip_extensions
    )
    schemas = enriched.get("components", {}).get("schemas", {})
    if not isinstance(schemas, dict):
        raise ValueError("components.schemas must be an object")
    operations: dict[str, dict[str, Any]] = {}
    roots_by_operation: dict[str, set[str]] = {}
    paths = enriched.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("paths must be an object")
    for path, raw_path_item in paths.items():
        path_item = _resolve(enriched, raw_path_item, "path item")
        for method in _METHODS:
            raw_operation = path_item.get(method)
            if raw_operation is None:
                continue
            operation = _resolve(enriched, raw_operation, "operation")
            op_id, record, roots = _operation_record(enriched, path, method, path_item, operation)
            if op_id in operations:
                raise ValueError(f"duplicate operationId: {op_id}")
            operations[op_id] = record
            roots_by_operation[op_id] = roots
    reachable: set[str] = set()
    for op_id, roots in roots_by_operation.items():
        pending = set(roots)
        seen: set[str] = set()
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            if name not in schemas:
                raise ValueError(f"missing component schema: {name!r}")
            seen.add(name)
            nested: set[str] = set()
            _schema_refs(enriched, schemas[name], nested)
            pending.update(nested - seen)
        operations[op_id]["reachable_schemas"] = sorted(seen)
        reachable.update(seen)
    return {
        "format_version": 1,
        "operations": operations,
        "schemas": {name: deepcopy(schemas[name]) for name in sorted(reachable)},
    }
