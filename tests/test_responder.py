from __future__ import annotations

from typing import Any

from as_engine.index import Operation, OperationIndex
from as_engine.output import render_output
from as_engine.responder import Responder


def _operation(**changes: Any) -> Operation:
    fields: dict[str, Any] = {
        "operationId": "getWidget",
        "method": "GET",
        "path": "/widgets/{id}",
        "tags": [],
        "summary": None,
        "description": None,
        "parameters": [],
        "requestBody": None,
        "response_200": "Widget",
        "extensions": {},
        "reachable_schemas": [],
    }
    fields.update(changes)
    return Operation(**fields)


def test_responder_serves_a_named_compositional_schema_through_transport_seam():
    operation = _operation()
    index = OperationIndex(
        operations={operation.operationId: operation},
        schemas={
            "Widget": {
                "allOf": [
                    {"type": "object", "properties": {"id": {"type": "integer"}}},
                    {
                        "type": "object",
                        "properties": {
                            "state": {"enum": ["new", "old"]},
                            "labels": {"type": "array", "items": {"type": "string"}},
                            "owner": {"$ref": "#/components/schemas/Owner"},
                        },
                    },
                ]
            },
            "Owner": {
                "type": "object",
                "properties": {
                    "name": {"default": "Ada"},
                    "next": {"$ref": "#/components/schemas/Owner"},
                },
            },
        },
    )

    response = Responder(index).call(operation, {"id": 4}, None)

    assert response.status == 200
    assert response.headers == {}
    assert response.body == {
        "id": 0,
        "state": "new",
        "labels": ["string"],
        "owner": {"name": "Ada", "next": None},
    }


def test_responder_prefers_explicit_examples_then_inline_schema_and_never_mutates_them():
    inline = {"type": "object", "properties": {"choice": {"oneOf": [{"example": "first"}]}}}
    operation = _operation(response_schema=inline, response_example={"items": [{"id": 1}]})
    responder = Responder(OperationIndex({operation.operationId: operation}, {"Widget": {}}))

    first = responder.call(operation, {}, None)
    first.body["items"][0]["id"] = 99
    second = responder.call(operation, {}, None)

    assert second.body == {"items": [{"id": 1}]}
    inline_operation = _operation(response_schema=inline, response_200="Widget")
    assert responder.call(inline_operation, {}, None).body == {"choice": "first"}


def test_responder_missing_schema_and_forced_statuses_are_predictable():
    operation = _operation(response_200=None)
    index = OperationIndex({operation.operationId: operation}, {})

    assert Responder(index).call(operation, {}, None).body is None
    assert Responder(index, status=400).call(operation, {}, None).body == {
        "message": "Responder forced HTTP 400"
    }
    assert Responder(index, status=400, body=None).call(operation, {}, None).body is None
    forced = Responder(index, status=503, body={"details": ["retry"]})
    response = forced.call(operation, {}, None)
    response.body["details"].append("changed")
    assert forced.call(operation, {}, None).body == {"details": ["retry"]}
    Responder(index).close()


def test_render_output_formats_top_level_values_and_escapes_markdown_cells():
    data = [{"name": "A|B\nC", "nested": {"ids": [1, 2]}}]

    assert (
        render_output(data)
        == '[\n  {\n    "name": "A|B\\nC",\n    "nested": {\n      "ids": [\n        1,\n        2\n      ]\n    }\n  }\n]'
    )
    assert "name" in render_output(data, "table", columns=["name"])
    assert render_output(data, "markdown") == (
        '| name | nested |\n| --- | --- |\n| A\\|B<br>C | {"ids":[1,2]} |'
    )
    assert render_output([], "markdown") == ""


def test_render_output_retains_later_columns_and_rejects_unknown_formats():
    data = [{"first": "one"}, {"later": "two|three\nfour", "nested": [1, 2]}]

    assert render_output(data, "markdown") == (
        "| first | later | nested |\n| --- | --- | --- |\n"
        "| one |  |  |\n|  | two\\|three<br>four | [1,2] |"
    )
    table = render_output(data, "table")
    assert "[1,2]" in table
    try:
        render_output([], "csv")
    except ValueError as exc:
        assert str(exc) == "unsupported output format: csv"
    else:
        raise AssertionError("unknown output format must fail")


def test_responder_honors_simple_schema_bounds_and_deep_copies_schema_examples():
    schema_example = {"nested": ["original"]}
    operation = _operation(
        response_schema={
            "type": "object",
            "properties": {
                "none": {"type": "array", "items": {"type": "string"}, "maxItems": 0},
                "many": {"type": "array", "items": {"type": "integer"}, "minItems": 2},
                "count": {"type": "integer", "minimum": 3, "exclusiveMinimum": True},
                "email": {"type": "string", "format": "email", "minLength": 20},
                "example": {"example": schema_example},
            },
        }
    )
    responder = Responder(OperationIndex({operation.operationId: operation}, {}))

    response = responder.call(operation, {}, None)
    response.body["example"]["nested"].append("changed")
    fresh = responder.call(operation, {}, None).body

    assert fresh["none"] == []
    assert fresh["many"] == [0, 0]
    assert fresh["count"] == 4
    assert fresh["email"].startswith("user@example.test") and len(fresh["email"]) == 20
    assert fresh["example"] == {"nested": ["original"]}


def test_explicit_null_media_example_wins():
    from as_engine.index import Operation, OperationIndex

    operation = Operation(
        "example",
        "GET",
        "/example",
        [],
        None,
        None,
        [],
        None,
        None,
        {},
        [],
        response_schema={"type": "string"},
        response_example=None,
    )
    assert (
        Responder(OperationIndex({"example": operation}, {})).call(operation, {}, None).body is None
    )
