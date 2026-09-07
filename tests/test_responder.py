from __future__ import annotations

from typing import Any

import pytest

from as_engine.index import Operation, OperationIndex
from as_engine.output import render_output
from as_engine.responder import Responder
from as_engine.transport import Response


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


def test_responder_seed_serves_response_sequence_and_preserves_explicit_status():
    operation = _operation()
    responder = Responder(OperationIndex({operation.operationId: operation}, {}), status=201)
    responder.seed(
        operation.operationId,
        [{"step": 1}, Response(status=409, body={"step": 2}, headers={"X-Trace": "two"})],
    )

    first = responder.call(operation, {}, None)
    second = responder.call(operation, {}, None)

    assert first == Response(status=201, body={"step": 1})
    assert second == Response(status=409, body={"step": 2}, headers={"X-Trace": "two"})


def test_responder_seeded_queue_exhaustion_does_not_fall_back_and_records_request():
    operation = _operation()
    responder = Responder(OperationIndex({operation.operationId: operation}, {}))
    responder.seed(operation.operationId, [])
    parameters = {"filter": {"labels": ["new"]}}
    body = {"widget": {"name": "one"}}

    try:
        responder.call(operation, parameters, body)
    except ValueError as exc:
        assert str(exc) == "seeded responses exhausted for operation getWidget"
    else:
        raise AssertionError("an exhausted seeded queue must fail")

    parameters["filter"]["labels"].append("changed")
    body["widget"]["name"] = "changed"
    assert responder.requests == [
        ("getWidget", {"filter": {"labels": ["new"]}}, {"widget": {"name": "one"}})
    ]


def test_responder_seeded_queues_are_isolated_and_defensively_copied():
    first_operation = _operation(operationId="first")
    second_operation = _operation(operationId="second")
    responder = Responder(
        OperationIndex(
            {first_operation.operationId: first_operation, second_operation.operationId: second_operation},
            {},
        )
    )
    queued = {"items": ["original"]}
    responder.seed("first", [queued])
    responder.seed("second", [{"items": ["second"]}])
    queued["items"].append("changed before call")

    first = responder.call(first_operation, {}, None)
    first.body["items"].append("changed after call")
    second = responder.call(second_operation, {}, None)

    assert first.body == {"items": ["original", "changed after call"]}
    assert second.body == {"items": ["second"]}


def test_responder_records_multipart_wire_metadata_without_file_contents(tmp_path):
    upload = tmp_path / "private-upload.txt"
    upload.write_bytes(b"not-for-the-wire")
    operation = _operation(
        operationId="uploadWidget",
        method="POST",
        request_media_types=["multipart/form-data"],
    )
    responder = Responder(OperationIndex({operation.operationId: operation}, {}))

    responder.call(operation, {"id": 4}, {"file": "@" + str(upload), "comment": "hello"})

    wire = responder.wire_requests
    assert responder.requests[0][0] == "uploadWidget"
    assert wire[0]["headers"] == {"X-Atlassian-Token": "nocheck"}
    part = next(part for part in wire[0]["parts"] if part["name"] == "file")
    assert part["filename"] == "private-upload.txt"
    assert part["size"] == len(b"not-for-the-wire")
    assert "not-for-the-wire" not in repr(wire)
    assert str(upload) not in repr(wire)


def test_responder_binary_mode_writes_deterministic_bytes_and_honors_seed(tmp_path):
    operation = _operation(
        operationId="downloadWidget", extensions={"x-as-response": {"kind": "binary"}}
    )
    responder = Responder(OperationIndex({operation.operationId: operation}, {}))
    target = tmp_path / "generated.bin"

    response = responder.call(operation, {}, None, output=target)

    assert target.read_bytes() == b"as-engine responder binary downloadWidget\n"
    assert response.body["path"] == str(target)
    responder.seed(operation.operationId, [Response(200, b"seeded"), Response(404, {"error": "no"})])
    responder.call(operation, {}, None, output=tmp_path / "seeded.bin")
    assert (tmp_path / "seeded.bin").read_bytes() == b"seeded"
    assert responder.call(operation, {}, None, output=tmp_path / "missing.bin") == Response(
        404, {"error": "no"}
    )


def test_responder_binary_seed_and_explicit_body_require_bytes(tmp_path):
    operation = _operation(
        operationId="downloadWidget", extensions={"x-as-response": {"kind": "binary"}}
    )
    responder = Responder(OperationIndex({operation.operationId: operation}, {}), body=b"override")
    responder.call(operation, {}, None, output=tmp_path / "override.bin")
    assert (tmp_path / "override.bin").read_bytes() == b"override"

    responder.seed(operation.operationId, ["not bytes"])
    with pytest.raises(ValueError, match="binary response body must be bytes"):
        responder.call(operation, {}, None, output=tmp_path / "bad-seed.bin")
    with pytest.raises(ValueError, match="binary response body must be bytes"):
        Responder(OperationIndex({operation.operationId: operation}, {}), body="not bytes").call(
            operation, {}, None, output=tmp_path / "bad-body.bin"
        )
