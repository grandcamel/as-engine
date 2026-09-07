"""Operation-oriented calls and discovery shared by product CLI adapters."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from assistant_skills_lib.error_handler import BaseAPIError  # type: ignore[import-untyped]

from .errors import SurfaceError, messages_from
from .index import Operation, OperationIndex, ProductIndexes
from .params import body_errors, kebab_case, validate_parameters
from .transport import Response, Transport


def deprecation(operation: Operation) -> tuple[bool, Any]:
    tag = operation.extensions.get("x-as-deprecated", operation.extensions.get("x-as-deprecation"))
    replacement = operation.extensions.get("x-as-replacement")
    if isinstance(tag, dict):
        replacement = tag.get("replacement", replacement)
    elif isinstance(tag, str):
        replacement = tag
    return bool(tag) or operation.deprecated, replacement


def _resolved(
    schema: dict[str, Any], schemas: Mapping[str, Any], seen: frozenset[str] = frozenset()
) -> dict[str, Any]:
    if "$ref" in schema:
        ref = schema["$ref"]
        name = ref.removeprefix("#/components/schemas/").replace("~1", "/").replace("~0", "~")
        if not ref.startswith("#/components/schemas/") or name in seen:
            return {"type": "reference", "$ref": ref}
        return _resolved(schemas.get(name, {}), schemas, seen | {name})
    result = dict(schema)
    for part in schema.get("allOf", []):
        child = _resolved(part, schemas, seen)
        result["properties"] = {**result.get("properties", {}), **child.get("properties", {})}
        result["required"] = list(
            dict.fromkeys(result.get("required", []) + child.get("required", []))
        )
    return result


def body_outline(operation: Operation, index: OperationIndex) -> list[dict[str, Any]]:
    request = operation.requestBody
    if not request:
        return []
    schema = (
        index.schemas.get(request["ref"], {}) if "ref" in request else request.get("schema", {})
    )
    schema = _resolved(schema, index.schemas)
    result = []
    for name, raw in schema.get("properties", {}).items():
        child = _resolved(raw, index.schemas)
        entry = {
            "name": name,
            "required": name in schema.get("required", []),
            "type": child.get(
                "type", "oneOf" if "oneOf" in child else "anyOf" if "anyOf" in child else "unknown"
            ),
        }
        if "enum" in child:
            entry["enum"] = child["enum"]
        if "properties" in child:
            entry["properties"] = [
                {
                    "name": key,
                    "type": _resolved(value, index.schemas).get("type", "unknown"),
                    "required": key in child.get("required", []),
                }
                for key, value in child["properties"].items()
            ]
        for union in ("oneOf", "anyOf"):
            if union in child:
                entry[union] = child[union]
        result.append(entry)
    return result


def describe_operation(operation: Operation, index: OperationIndex) -> dict[str, Any]:
    deprecated, replacement = deprecation(operation)
    return {
        "operationId": operation.operationId,
        "method": operation.method,
        "path": operation.path,
        "summary": operation.summary,
        "description": operation.description,
        "parameters": operation.parameters,
        "body": body_outline(operation, index),
        "body_required": operation.request_body_required,
        "media_types": operation.request_media_types,
        "response_200": operation.response_200,
        "response_schema": operation.response_schema,
        "extensions": operation.extensions,
        "deprecated": deprecated,
        "replacement": replacement,
    }


def describe_markdown(value: Mapping[str, Any]) -> str:
    lines = [
        f"# {value['operationId']}",
        "",
        f"{value['method']} `{value['path']}`",
        "",
        value.get("summary") or "",
        "",
        value.get("description") or "",
        "",
        "## Parameters",
    ]
    for parameter in value["parameters"]:
        required = " (required)" if parameter["required"] else ""
        enum = f"; enum: {json.dumps(parameter['enum'])}" if "enum" in parameter else ""
        lines.append(
            f"- `--{kebab_case(parameter['name'])}` ({parameter['in']}, {parameter['type']}){required}{enum}"
        )
    if not value["parameters"]:
        lines.append("None.")
    lines += ["", "## Body" + (" (required)" if value["body_required"] else "")]
    for item in value["body"]:
        lines.append(
            f"- `{item['name']}`: {item['type']}"
            + (" (required)" if item["required"] else "")
            + (f"; enum: {json.dumps(item['enum'])}" if "enum" in item else "")
        )
        for child in item.get("properties", []):
            lines.append(
                f"  - `{child['name']}`: {child['type']}"
                + (" (required)" if child["required"] else "")
            )
        for union in ("oneOf", "anyOf"):
            if union in item:
                lines.append(f"  - {union}: {json.dumps(item[union], ensure_ascii=False)}")
    if not value["body"]:
        lines.append("No top-level body properties.")
    lines += [
        "",
        "200 response schema: "
        + (
            value["response_200"]
            or ("inline" if value["response_schema"] is not None else "unspecified")
        ),
    ]
    for key, tag in value["extensions"].items():
        if key.startswith("x-as-") or "scope" in key:
            lines.append(f"{key.removeprefix('x-as-')}: {json.dumps(tag, ensure_ascii=False)}")
    if value["deprecated"]:
        lines.append("Deprecated. Replacement: " + str(value["replacement"] or "not specified"))
    return "\n".join(lines).strip()


class Surface:
    def __init__(
        self, indexes: ProductIndexes, transport_factory: Callable[[str, OperationIndex], Transport]
    ):
        self.indexes = indexes
        self.transport_factory = transport_factory

    def resolve(self, name: str) -> tuple[str, OperationIndex, Operation]:
        try:
            return self.indexes.find(name)
        except KeyError as exc:
            raise SurfaceError(404, [f"Unknown operation: {name}"], name) from exc
        except ValueError as exc:
            raise SurfaceError(None, [str(exc)], name, code=2) from exc

    def call(
        self,
        name: str,
        parameters: Mapping[str, Any],
        body: Any = None,
        *,
        validate_body: bool = False,
        warn: Callable[[str], None] | None = None,
    ) -> Response:
        document, index, operation = self.resolve(name)
        note = operation.extensions.get("x-as-note")
        try:
            checked = validate_parameters(operation, parameters, index.schemas)
            if validate_body:
                problems = body_errors(operation, body, index.schemas)
                if problems:
                    raise SurfaceError(None, problems, code=2)
            deprecated, replacement = deprecation(operation)
            if deprecated and warn:
                warn(
                    f"Warning: {operation.operationId} is deprecated; replacement: {replacement or 'not specified'}"
                )
            transport = self.transport_factory(document, index)
            try:
                response = transport.call(operation, checked, body)
            finally:
                close = getattr(transport, "close", None)
                if close:
                    close()
            if not 200 <= response.status < 300:
                raise SurfaceError(
                    response.status, messages_from(response.body) or [f"HTTP {response.status}"]
                )
            return response
        except BaseAPIError as exc:
            error = SurfaceError.from_domain(exc)
        except SurfaceError as exc:
            error = exc
        except ValueError as exc:
            error = SurfaceError(None, [str(exc)], code=2)
        error.operation = operation.operationId
        error.note = note
        if error.status == 400:
            error.messages = list(
                dict.fromkeys(error.messages + body_errors(operation, body, index.schemas))
            )
        raise error

    def search(
        self, words: Sequence[str], *, include_deprecated: bool = False
    ) -> list[dict[str, Any]]:
        results = []
        for _, index in self.indexes.primary():
            for operation in index.operations.values():
                haystack = " ".join(
                    [
                        operation.operationId,
                        operation.summary or "",
                        operation.path,
                        *operation.tags,
                        json.dumps(operation.extensions.get("x-as-note", "")),
                    ]
                ).casefold()
                if all(word.casefold() in haystack for word in words) and (
                    include_deprecated or not deprecation(operation)[0]
                ):
                    results.append(
                        {
                            key: getattr(operation, key)
                            for key in ("operationId", "method", "path", "summary")
                        }
                    )
        return sorted(results, key=lambda row: row["operationId"])

    def describe(self, name: str) -> dict[str, Any]:
        _, index, operation = self.resolve(name)
        return describe_operation(operation, index)

    def topics(self) -> list[str]:
        topics: set[str] = set()
        for _, index in self.indexes.primary():
            for operation in index.operations.values():
                tag = operation.extensions.get("x-as-topic", [])
                topics.update(
                    [tag] if isinstance(tag, str) else tag if isinstance(tag, list) else []
                )
        return sorted(topics)


def parse_call_flags(
    operation: Operation, arguments: Sequence[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive flags from the record; leave type validation to the engine checker."""
    reserved = {"body", "field", "format", "validate-body", "help"}
    flags = {
        "--"
        + ("parameter-" if kebab_case(p["name"]) in reserved else "")
        + kebab_case(p["name"]): p
        for p in operation.parameters
    }
    if len(flags) != len(operation.parameters):
        raise ValueError("operation has ambiguous parameter flags")
    parameters: dict[str, Any] = {}
    options: dict[str, Any] = {
        "body": None,
        "field": [],
        "format": "json",
        "validate_body": False,
        "help": False,
    }
    i = 0
    while i < len(arguments):
        token = arguments[i]
        key, equal, value = token.partition("=")
        i += 1
        if key in ("--validate-body", "--help") and not equal:
            options[key[2:].replace("-", "_")] = True
            continue
        if key not in flags and key not in ("--body", "--field", "--format"):
            raise ValueError(f"Unknown flag: {key}")
        if not equal:
            if i == len(arguments) or arguments[i].startswith("--"):
                raise ValueError(f"Missing value for {key}")
            value = arguments[i]
            i += 1
        if key in flags:
            parameter = flags[key]
            name = parameter["name"]
            if parameter["type"] == "array":
                if value.lstrip().startswith("["):
                    try:
                        parsed = json.loads(value)
                    except ValueError as exc:
                        raise ValueError(f"Invalid JSON array for {key}") from exc
                else:
                    parsed = value.split(",")
                values = parsed if isinstance(parsed, list) else [parsed]
                parameters.setdefault(name, []).extend(values)
            elif name in parameters:
                raise ValueError(f"Duplicate flag: {key}")
            else:
                parameters[name] = value
        elif key == "--field":
            options["field"].append(value)
        else:
            options[key[2:]] = value
    if options["format"] not in ("json", "table", "markdown"):
        raise ValueError("--format must be json, table or markdown")
    return parameters, options
