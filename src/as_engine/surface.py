"""Operation-oriented calls and discovery shared by product CLI adapters."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from typing import Any

from assistant_skills_lib.error_handler import BaseAPIError  # type: ignore[import-untyped]

from .errors import SurfaceError, messages_from
from .index import Operation, OperationIndex, ProductIndexes
from .params import alias_flags, body_errors, kebab_case, validate_parameters
from .transforms import Context, Registry, default_registry
from .transforms.values import MISSING, set_target, target_value
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
        self,
        indexes: ProductIndexes,
        transport_factory: Callable[[str, OperationIndex], Transport],
        *,
        registry: Registry | None = None,
    ):
        self.indexes = indexes
        self.transport_factory = transport_factory
        self.registry = registry if registry is not None else default_registry()

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
        all_pages: bool = False,
        limit: int | None = None,
        aliases: Mapping[str, str] | None = None,
        version: int | None = None,
    ) -> Response:
        document, index, operation = self.resolve(name)
        note = operation.extensions.get("x-as-note")
        transports: dict[str, Transport] = {}
        active: set[str] = set()
        final_body = body

        def execute(
            op: Operation,
            values: Mapping[str, Any],
            payload: Any,
            *,
            merge: bool = False,
            cap: int | None = None,
            keys: Mapping[str, str] | None = None,
            supplied_version: int | None = None,
            notify: Callable[[str], None] | None = None,
        ) -> Response:
            nonlocal final_body
            if op.operationId in active:
                raise ValueError("cyclic transform operation reference")
            if cap is not None and (type(cap) is not int or cap < 1 or not merge):
                raise ValueError("aggregate limit requires all_pages and a positive integer")
            if merge and "x-as-paging" not in op.extensions:
                raise ValueError("operation has no declared paging contract")
            rules = alias_flags(op)
            keys = dict(keys or {})
            if set(keys) - rules.keys():
                raise ValueError("unknown prerequisite alias")
            if any(not isinstance(value, str) or not value for value in keys.values()):
                raise ValueError("prerequisite aliases require a nonempty string")
            # Defer only required parameters that the selected alias will supply.
            deferred = {
                r["target"]["name"]
                for alias, r in rules.items()
                if alias in keys and r["target"].get("in") != "body"
            }
            partial = replace(
                op,
                parameters=[
                    {**p, "required": False} if p["name"] in deferred else p for p in op.parameters
                ],
            )
            checked = validate_parameters(partial, values, index.schemas)

            def transport() -> Transport:
                if document not in transports:
                    transports[document] = self.transport_factory(document, index)
                return transports[document]

            def send(params: Mapping[str, Any], request_body: Any) -> Response:
                params = validate_parameters(op, params, index.schemas)
                if validate_body:
                    problems = body_errors(op, request_body, index.schemas)
                    if problems:
                        raise SurfaceError(None, problems, code=2)
                result = transport().call(op, params, request_body)
                if not 200 <= result.status < 300:
                    raise SurfaceError(
                        result.status, messages_from(result.body) or [f"HTTP {result.status}"]
                    )
                return result

            def invoke(
                child: str,
                params: Mapping[str, Any],
                request_body: Any = None,
                *,
                all_pages: bool = False,
            ) -> Response:
                if child not in index.operations:
                    raise ValueError("transform operation is not in the same document")
                return execute(index.operations[child], params, request_body, merge=all_pages)

            context = Context(
                document,
                index,
                op,
                checked,
                deepcopy(payload),
                keys,
                merge,
                cap,
                invoke,
                send,
                lambda: getattr(transport(), "base_url", None),
                notify,
            )
            if supplied_version is not None:
                tag = op.extensions.get("x-as-version")
                if tag is None or type(supplied_version) is not int or supplied_version < 1:
                    raise ValueError(
                        "--version requires a version-tagged operation and positive integer"
                    )
                if target_value(context, tag["target"]) is not MISSING:
                    raise ValueError("conflicting --version and body version")
                set_target(context, tag["target"], supplied_version)
            active.add(op.operationId)
            try:
                selected = self.registry.selected(op)
                for tag_name, transform in selected:
                    transform.request(context, op.extensions[tag_name])
                context.parameters = validate_parameters(op, context.parameters, index.schemas)
                if op is operation:
                    final_body = context.body
                deprecated, replacement = deprecation(op)
                if deprecated and notify:
                    notify(
                        f"Warning: {op.operationId} is deprecated; replacement: {replacement or 'not specified'}"
                    )
                response = send(context.parameters, context.body)
                for tag_name, transform in selected:
                    response = transform.response(context, op.extensions[tag_name], response)
                if notify and "count" in context.state:
                    notify(f"count={context.state['count']}")
                return response
            finally:
                active.remove(op.operationId)

        try:
            return execute(
                operation,
                parameters,
                body,
                merge=all_pages,
                cap=limit,
                keys=aliases,
                supplied_version=version,
                notify=warn,
            )
        except BaseAPIError as exc:
            error = SurfaceError.from_domain(exc)
        except SurfaceError as exc:
            error = exc
        except ValueError as exc:
            error = SurfaceError(None, [str(exc)], code=2)
        finally:
            for transport in transports.values():
                close = getattr(transport, "close", None)
                if close:
                    close()
        error.operation = operation.operationId
        error.note = note
        if error.status == 400:
            error.messages = list(
                dict.fromkeys(error.messages + body_errors(operation, final_body, index.schemas))
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
    rules = alias_flags(operation)
    all_pages = "--all" in arguments
    reserved = {"body", "field", "format", "validate-body", "help", "all"} | rules.keys()
    if all_pages:
        reserved.add("limit")
    if "x-as-version" in operation.extensions:
        reserved.add("version")
    flags = {
        "--"
        + ("parameter-" if kebab_case(p["name"]) in reserved else "")
        + kebab_case(p["name"]): p
        for p in operation.parameters
    }
    if not all_pages:
        for p in operation.parameters:
            if p["name"] == "limit":
                flags["--parameter-limit"] = p
    if len({id(p) for p in flags.values()}) != len(operation.parameters):
        raise ValueError("operation has ambiguous parameter flags")
    parameters: dict[str, Any] = {}
    options: dict[str, Any] = {
        "body": None,
        "all_pages": all_pages,
        "limit": None,
        "aliases": {},
        "version": None,
        "field": [],
        "format": "json",
        "validate_body": False,
        "help": False,
    }
    special = {"--body", "--field", "--format"} | {"--" + alias for alias in rules}
    if all_pages:
        special.add("--limit")
    if "x-as-version" in operation.extensions:
        special.add("--version")
    seen_options: set[str] = set()
    i = 0
    while i < len(arguments):
        token = arguments[i]
        key, equal, value = token.partition("=")
        i += 1
        if key in ("--validate-body", "--help", "--all") and not equal:
            options["all_pages" if key == "--all" else key[2:].replace("-", "_")] = True
            continue
        if key not in flags and key not in special:
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
        elif key == "--limit" or key == "--version":
            if key in seen_options:
                raise ValueError(f"Duplicate flag: {key}")
            seen_options.add(key)
            if not value.isdecimal() or int(value) < 1:
                raise ValueError(f"{key} must be a positive integer")
            options[key[2:]] = int(value)
        elif key[2:] in rules:
            if key[2:] in options["aliases"]:
                raise ValueError(f"Duplicate flag: {key}")
            options["aliases"][key[2:]] = value
        elif key == "--field":
            options["field"].append(value)
        else:
            options[key[2:]] = value
    if options["format"] not in ("json", "table", "markdown"):
        raise ValueError("--format must be json, table or markdown")
    return parameters, options
