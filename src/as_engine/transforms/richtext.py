"""Representation-aware rich-text request and response conversion."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any, NoReturn

from ..converters import check_document, convert, render
from . import Context, Transform
from .values import MISSING, parts, pointer

_REPRESENTATIONS = {("adf", "object"), ("adf", "json-string"), ("storage", "string")}
_REQUEST_SHAPES = {"value", "envelope"}
_RESPONSE_SHAPES = {"value", "envelope", "representation-map"}


def _fail(message: str) -> NoReturn:
    raise ValueError(message)


def _json_adf(value: Any) -> str:
    if not isinstance(value, str):
        _fail("json-string rich-text value must be a string")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("json-string rich-text value must contain ADF JSON") from exc
    check_document(parsed, source="adf")
    return value


def _encoded(value: Any, descriptor: Mapping[str, Any], *, allow_adf_object: bool = False) -> Any:
    encoding = descriptor["encoding"]
    if encoding == "object":
        check_document(value, source="adf")
        return value
    elif encoding == "json-string":
        if isinstance(value, dict) and allow_adf_object:
            check_document(value, source="adf")
            return json.dumps(value, separators=(",", ":"))
        _json_adf(value)
        return value
    elif not isinstance(value, str):
        _fail("string rich-text value must be a string")
    return value


def _descriptors(operation: Any) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    extensions = getattr(operation, "extensions", None)
    if not isinstance(extensions, Mapping):
        _fail("operation extensions must be an object")
    if "x-as-richtext" not in extensions:
        return [], None
    raw = extensions["x-as-richtext"]
    if not isinstance(raw, list) or not raw:
        _fail("x-as-richtext must be a nonempty list")
    result: list[dict[str, Any]] = []
    for tag in raw:
        if not isinstance(tag, dict):
            _fail("rich-text descriptor must be an object")
        representations = tag.get("representations")
        if not isinstance(representations, dict) or not representations:
            _fail("rich-text descriptor requires representations")
        for name, value in representations.items():
            if not isinstance(name, str) or not name or not isinstance(value, dict):
                _fail("invalid rich-text representation")
            if (
                not all(isinstance(value.get(key), str) for key in ("converter", "encoding"))
                or (value["converter"], value["encoding"]) not in _REPRESENTATIONS
            ):
                _fail("unsupported rich-text representation")
        for hook, shapes in (("request", _REQUEST_SHAPES), ("response", _RESPONSE_SHAPES)):
            if hook not in tag:
                continue
            value = tag[hook]
            if not isinstance(value, dict) or not isinstance(value.get("path"), str):
                _fail(f"rich-text {hook} requires a JSON Pointer path")
            parts(value["path"])
            if not isinstance(value.get("shape"), str) or value["shape"] not in shapes:
                _fail(f"unknown rich-text {hook} shape")
            if "itemsPath" in value:
                if not isinstance(value["itemsPath"], str):
                    _fail(f"rich-text {hook} itemsPath must be a JSON Pointer")
                parts(value["itemsPath"])
            if "nullable" in value:
                if not isinstance(value["nullable"], bool):
                    _fail("rich-text nullable must be a boolean")
                if value["nullable"] and (
                    value["shape"] != "value"
                    or any(
                        rep != {"converter": "adf", "encoding": "object"}
                        for rep in representations.values()
                    )
                ):
                    _fail("nullable rich text requires a direct ADF object value")
        if "customFields" in tag and (tag["customFields"] != "textarea" or "request" not in tag):
            _fail("customFields must declare the textarea request rule")
        if "request" not in tag and "response" not in tag:
            _fail("rich-text descriptor needs a request or response")
        result.append(tag)

    representation_tag = extensions.get("x-as-representation")
    if "x-as-representation" in extensions:
        if not isinstance(representation_tag, dict):
            _fail("x-as-representation must be an object")
        default = representation_tag.get("default")
        alternatives = representation_tag.get("alternatives", [])
        if (
            not isinstance(default, str)
            or not isinstance(alternatives, list)
            or not all(isinstance(item, str) for item in alternatives)
        ):
            _fail("invalid x-as-representation")
        accepted = {default, *alternatives}
        if len(accepted) != len(alternatives) + 1:
            _fail("duplicate x-as-representation alternative")
        for tag in result:
            if not accepted.issubset(tag["representations"]):
                _fail("x-as-representation names an undeclared representation")
    elif any(len(tag["representations"]) != 1 for tag in result):
        _fail("multiple rich-text representations require x-as-representation")
    return result, representation_tag


def _selected(
    tag: Mapping[str, Any], representation_tag: Mapping[str, Any] | None, override: str | None
) -> str:
    if override is not None:
        if not isinstance(override, str) or override not in tag["representations"]:
            _fail("unsupported rich-text representation")
        return override
    if representation_tag is not None:
        return representation_tag["default"]
    return next(iter(tag["representations"]))


def _request_envelope(
    value: Any,
    tag: Mapping[str, Any],
    chosen: str,
    override: str | None,
    *,
    encode_value: bool = True,
) -> Any:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("representation"), str)
        or "value" not in value
    ):
        _fail("rich-text envelope requires representation and value")
    actual = value["representation"]
    if actual not in tag["representations"]:
        _fail("unsupported rich-text representation")
    if override is not None and actual != chosen:
        _fail("rich-text envelope conflicts with representation override")
    if (
        not encode_value
        and tag["representations"][actual]["encoding"] == "json-string"
        and isinstance(value["value"], dict)
    ):
        check_document(value["value"], source="adf")
        return value
    changed = copy.deepcopy(value)
    changed["value"] = _encoded(
        changed["value"], tag["representations"][actual], allow_adf_object=True
    )
    return changed


def _markdown_envelope(value: str, tag: Mapping[str, Any], chosen: str) -> dict[str, Any]:
    descriptor = tag["representations"][chosen]
    try:
        converted = convert(value, target=descriptor["converter"])
    except TypeError as exc:
        raise ValueError("rich-text Markdown value must be a string") from exc
    return {
        "representation": chosen,
        "value": _encoded(converted, descriptor, allow_adf_object=True),
    }


def validate_options(
    operation: Any, body: Any, *, representation: str | None = None, raw: bool = False
) -> None:
    """Validate rich-text tags and call options before prerequisite lookups."""
    if not isinstance(raw, bool):
        _fail("raw must be a boolean")
    descriptors, representation_tag = _descriptors(operation)
    if not descriptors:
        if representation is not None or raw:
            _fail("operation has no rich-text descriptors")
        return
    if raw and not any("response" in tag for tag in descriptors):
        _fail("raw requires a rich-text response descriptor")
    for tag in descriptors:
        chosen = _selected(tag, representation_tag, representation)
        request = tag.get("request")
        if request is None:
            continue
        for path in _request_targets(body, request):
            value = pointer(body, path)
            if (
                value is not MISSING
                and request["shape"] == "envelope"
                and not isinstance(value, str)
            ):
                _request_envelope(value, tag, chosen, representation, encode_value=False)


def _request_targets(body: Any, request: Mapping[str, Any]) -> list[str]:
    """Expand a declared request collection without guessing field names."""
    if "itemsPath" not in request:
        return [request["path"]]
    items_path = request["itemsPath"]
    items = pointer(body, items_path)
    if items is MISSING:
        return []
    if not isinstance(items, list):
        _fail("rich-text request itemsPath must identify an array")
    return [f"{items_path}/{index}{request['path']}" for index in range(len(items))]


def request_paths(operation: Any) -> list[str]:
    """Return validated rich-text destinations for CLI field parsing."""
    descriptors, _ = _descriptors(operation)
    # Bulk callers supply JSON with --body or --field collection=[...]. Dotted
    # field parsing cannot address array items; never mistake a relative item
    # path for a top-level destination.
    return [
        tag["request"]["path"]
        for tag in descriptors
        if "request" in tag and "itemsPath" not in tag["request"]
    ]


def _replace(value: Any, path: str, replacement: Any) -> Any:
    keys = parts(path)
    if not keys:
        return replacement
    current = value
    for key in keys[:-1]:
        if isinstance(current, list):
            if not key.isdigit() or int(key) >= len(current):
                _fail("rich-text path identifies a malformed value")
            current = current[int(key)]
        elif isinstance(current, dict) and key in current:
            current = current[key]
        else:
            _fail("rich-text path identifies a malformed value")
    key = keys[-1]
    if isinstance(current, list):
        if not key.isdigit() or int(key) >= len(current):
            _fail("rich-text path identifies a malformed value")
        current[int(key)] = replacement
    elif isinstance(current, dict) and key in current:
        current[key] = replacement
    else:
        _fail("rich-text path identifies a malformed value")
    return value


def _markdown(value: Any, descriptor: Mapping[str, Any]) -> str:
    try:
        _encoded(value, descriptor)
        return render(value, source=descriptor["converter"])
    except TypeError as exc:
        raise ValueError("malformed rich-text value") from exc


class RichText(Transform):
    """Convert tagged literal Markdown on writes and render tagged reads."""

    def request(self, context: Context, tag: Any) -> None:
        if context.operation.extensions.get("x-as-resolution-read"):
            return
        descriptors, representation_tag = _descriptors(context.operation)
        if tag != context.operation.extensions.get("x-as-richtext"):
            _fail("invalid rich-text transform tag")
        override = getattr(context, "representation", None)
        body = copy.deepcopy(context.body)
        for descriptor in descriptors:
            request = descriptor.get("request")
            if request is None:
                continue
            chosen = _selected(descriptor, representation_tag, override)
            for path in _request_targets(body, request):
                value = pointer(body, path)
                if value is MISSING or (value is None and request.get("nullable", False)):
                    continue
                if request["shape"] == "envelope":
                    replacement = (
                        _markdown_envelope(value, descriptor, chosen)
                        if isinstance(value, str)
                        else _request_envelope(value, descriptor, chosen, override)
                    )
                elif isinstance(value, str):
                    # A direct string is always literal Markdown; never sniff JSON here.
                    replacement = _markdown_envelope(value, descriptor, chosen)["value"]
                else:
                    replacement = _encoded(
                        value, descriptor["representations"][chosen], allow_adf_object=True
                    )
                body = _replace(body, path, replacement)
        context.body = body

    def response(self, context: Context, tag: Any, response: Any) -> Any:
        if context.operation.extensions.get("x-as-resolution-read") or getattr(
            context, "raw", False
        ):
            return response
        descriptors, representation_tag = _descriptors(context.operation)
        if tag != context.operation.extensions.get("x-as-richtext"):
            _fail("invalid rich-text transform tag")
        body = copy.deepcopy(response.body)
        for descriptor in descriptors:
            response_tag = descriptor.get("response")
            if response_tag is None:
                continue
            items_path = response_tag.get("itemsPath")
            if isinstance(body, list):
                items = body
            elif items_path is None:
                items = [body]
            else:
                items = pointer(body, items_path)
                if items is MISSING:
                    continue
                if not isinstance(items, list):
                    _fail("rich-text itemsPath must identify an array")
            for item_index, item in enumerate(items):
                value = pointer(item, response_tag["path"])
                if value is MISSING or (value is None and response_tag.get("nullable", False)):
                    continue
                shape = response_tag["shape"]
                replacement: Any
                if shape == "value":
                    chosen = _selected(
                        descriptor, representation_tag, getattr(context, "representation", None)
                    )
                    replacement = _markdown(value, descriptor["representations"][chosen])
                elif shape == "envelope":
                    checked = _request_envelope(
                        value, descriptor, _selected(descriptor, representation_tag, None), None
                    )
                    changed = copy.deepcopy(checked)
                    changed["value"] = _markdown(
                        checked["value"], descriptor["representations"][checked["representation"]]
                    )
                    replacement = changed
                else:
                    if not isinstance(value, dict):
                        _fail("rich-text representation-map must be an object")
                    changed = copy.deepcopy(value)
                    for name, envelope in value.items():
                        if name not in descriptor["representations"]:
                            _fail("unsupported rich-text representation")
                        if not isinstance(envelope, dict) or "value" not in envelope:
                            _fail("rich-text envelope requires value")
                        actual = envelope.get("representation", name)
                        if actual != name:
                            _fail("representation-map envelope key conflicts with representation")
                        checked_value = _encoded(
                            envelope["value"], descriptor["representations"][name]
                        )
                        changed[name]["value"] = _markdown(
                            checked_value, descriptor["representations"][name]
                        )
                    replacement = changed
                updated = _replace(item, response_tag["path"], replacement)
                if response_tag["path"] == "":
                    if items_path is None and not isinstance(body, list):
                        body = updated
                    else:
                        items[item_index] = updated
        return type(response)(response.status, body, response.headers)
