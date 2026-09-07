"""A deterministic, stateless transport double for indexed operations."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from copy import deepcopy
from math import floor
from typing import Any

from .index import NO_EXAMPLE, Operation, OperationIndex
from .transport import Response


class _Unset:
    pass


UNSET = _Unset()


class Responder:
    """Serve examples or small representative values through the transport seam.

    A missing response schema deliberately produces ``None``.  The double
    never attempts to infer a response from an operation's reachable schemas.
    """

    def __init__(self, index: OperationIndex, *, status: int = 200, body: Any = UNSET) -> None:
        self._index = index
        self._status = status
        self._body = body
        self._seeded: dict[str, deque[Response]] = {}
        self.requests: list[tuple[str, dict[str, Any], Any]] = []

    def seed(self, operation_id: str, responses: Sequence[Response | Any]) -> None:
        """Replace an operation's response queue with explicit responses.

        Raw response bodies use this responder's default status.  A supplied
        ``Response`` retains its status and headers.
        """

        self._seeded[operation_id] = deque(
            deepcopy(response)
            if isinstance(response, Response)
            else Response(status=self._status, body=deepcopy(response))
            for response in responses
        )

    def call(
        self,
        operation: Operation,
        parameters: Mapping[str, Any],
        body: Any,
    ) -> Response:
        self.requests.append((operation.operationId, deepcopy(dict(parameters)), deepcopy(body)))
        seeded = self._seeded.get(operation.operationId)
        if seeded is not None:
            if not seeded:
                raise ValueError(f"seeded responses exhausted for operation {operation.operationId}")
            return deepcopy(seeded.popleft())
        if self._body is not UNSET:
            response_body = deepcopy(self._body)
        elif self._status >= 400:
            response_body = {"message": f"Responder forced HTTP {self._status}"}
        else:
            response_body = self._response_body(operation)
        return Response(status=self._status, body=response_body)

    def close(self) -> None:
        """Match the live transport lifecycle; no resources are held."""

    def _response_body(self, operation: Operation) -> Any:
        example = operation.response_example
        if example is not NO_EXAMPLE:
            return deepcopy(example)

        schema = getattr(operation, "response_schema", None)
        if not isinstance(schema, dict):
            response_name = operation.response_200
            schema = self._index.schemas.get(response_name) if response_name else None
        if not isinstance(schema, dict):
            return None
        return self._generate(schema, active_refs=set(), depth=0)

    def _generate(self, schema: dict[str, Any], *, active_refs: set[str], depth: int) -> Any:
        if "example" in schema:
            return deepcopy(schema["example"])
        examples = schema.get("examples")
        if isinstance(examples, list) and examples:
            return deepcopy(examples[0])
        if isinstance(examples, dict) and examples:
            first = examples[min(examples)]
            if isinstance(first, dict) and "value" in first:
                first = first["value"]
            return deepcopy(first)
        if "default" in schema:
            return deepcopy(schema["default"])
        enum = schema.get("enum")
        if isinstance(enum, list) and enum:
            return deepcopy(enum[0])

        ref = schema.get("$ref")
        if isinstance(ref, str):
            name = self._reference_name(ref)
            if name is None or name in active_refs or depth >= 20:
                return None
            target = self._index.schemas.get(name)
            if not isinstance(target, dict):
                return None
            return self._generate(target, active_refs=active_refs | {name}, depth=depth + 1)

        for keyword in ("allOf", "oneOf", "anyOf"):
            choices = schema.get(keyword)
            if not isinstance(choices, list) or not choices:
                continue
            if keyword == "allOf":
                values: list[Any] = []
                own_schema = {key: value for key, value in schema.items() if key != "allOf"}
                if own_schema.get("properties") or own_schema.get("type") == "object":
                    values.append(
                        self._generate(own_schema, active_refs=active_refs, depth=depth + 1)
                    )
                values.extend(
                    self._generate(item, active_refs=active_refs, depth=depth + 1)
                    for item in choices
                    if isinstance(item, dict)
                )
                if values and all(isinstance(value, dict) for value in values):
                    merged: dict[str, Any] = {}
                    for value in values:
                        merged.update(value)
                    return merged
                return values[0] if values else None
            first = choices[0]
            return (
                self._generate(first, active_refs=active_refs, depth=depth + 1)
                if isinstance(first, dict)
                else None
            )

        schema_type = schema.get("type")
        properties = schema.get("properties")
        if schema_type == "object" or isinstance(properties, dict):
            if depth >= 20:
                return {}
            if not isinstance(properties, dict):
                return {}
            return {
                name: self._generate(value, active_refs=active_refs, depth=depth + 1)
                for name, value in properties.items()
                if isinstance(value, dict)
            }
        if schema_type == "array":
            item = schema.get("items")
            count = _array_count(schema)
            if not isinstance(item, dict):
                return []
            return [
                self._generate(item, active_refs=active_refs, depth=depth + 1) for _ in range(count)
            ]
        if schema_type == "string":
            return _string_value(schema)
        if schema_type == "integer":
            return _number_value(schema, integer=True)
        if schema_type == "number":
            return _number_value(schema, integer=False)
        if schema_type == "boolean":
            return False
        return None

    @staticmethod
    def _reference_name(ref: str) -> str | None:
        prefix = "#/components/schemas/"
        if not ref.startswith(prefix):
            return None
        return ref[len(prefix) :].replace("~1", "/").replace("~0", "~")


def _array_count(schema: dict[str, Any]) -> int:
    minimum = schema.get("minItems", 0)
    maximum = schema.get("maxItems")
    lower = minimum if isinstance(minimum, int) and not isinstance(minimum, bool) else 0
    count = max(1, lower)
    if isinstance(maximum, int) and not isinstance(maximum, bool):
        count = min(count, max(0, maximum))
    return count


def _number_value(schema: dict[str, Any], *, integer: bool) -> int | float:
    minimum = schema.get("minimum", 0)
    value: int | float = (
        minimum if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) else 0
    )
    exclusive = schema.get("exclusiveMinimum")
    if exclusive is True:
        value += 1
    elif isinstance(exclusive, (int, float)) and not isinstance(exclusive, bool):
        value = exclusive + 1
    if integer:
        return floor(value) if value == floor(value) else floor(value) + 1
    return value


def _string_value(schema: dict[str, Any]) -> str:
    format_name = schema.get("format")
    if not isinstance(format_name, str):
        format_name = ""
    value = {
        "date": "1970-01-01",
        "date-time": "1970-01-01T00:00:00Z",
        "email": "user@example.test",
        "uri": "https://example.test",
        "uuid": "00000000-0000-0000-0000-000000000000",
    }.get(format_name, "string")
    minimum = schema.get("minLength", 0)
    if isinstance(minimum, int) and not isinstance(minimum, bool):
        value = value + "x" * max(0, minimum - len(value))
    maximum = schema.get("maxLength")
    if isinstance(maximum, int) and not isinstance(maximum, bool):
        value = value[: max(0, maximum)]
    return value
