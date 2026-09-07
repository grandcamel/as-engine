"""Build and public surface contracts using offline transports."""

import json

import pytest

from as_engine.compiler import compile_document
from as_engine.errors import SurfaceError
from as_engine.index import ProductIndexes
from as_engine.responder import Responder
from as_engine.surface import Surface, describe_markdown, parse_call_flags


def product(tmp_path, **changes):
    operation = {
        "operationId": "createThing",
        "summary": "Create a page",
        "tags": ["Page"],
        "description": "Short.\n\nLong.",
        "parameters": [
            {
                "name": "spaceId",
                "in": "query",
                "required": True,
                "schema": {"type": "integer"},
                "style": "form",
                "explode": False,
            }
        ],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["title"],
                        "properties": {
                            "title": {"type": "string"},
                            "body": {"type": "object", "properties": {"value": {"type": "string"}}},
                        },
                    }
                }
            },
        },
        "responses": {
            "200": {
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {"id": {"type": "integer", "example": 42}},
                        }
                    }
                }
            }
        },
        "x-as-note": "a useful gotcha",
        "x-as-topic": ["pages", "auth"],
        "x-as-scope": "document",
    }
    operation.update(changes)
    doc = {"openapi": "3.0.3", "paths": {"/things": {"post": operation}}}
    compiled = compile_document(doc)
    (tmp_path / "primary.json").write_text(json.dumps(compiled))
    (tmp_path / "catalog.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "documents": [
                    {"id": "primary", "tier": "primary", "file": "primary.json"},
                    {"id": "lower", "tier": "lower", "file": "lower.json"},
                ],
            }
        )
    )
    return ProductIndexes(tmp_path)


def test_inline_projection_and_old_records_load(tmp_path):
    indexes = product(tmp_path, deprecated=False)
    doc = json.loads((tmp_path / "primary.json").read_text())["operations"]["createThing"]
    assert doc["response_200"] is None and doc["response_schema"]["type"] == "object"
    assert doc["deprecated"] is False and doc["request_body_required"] is True
    assert doc["request_media_types"] == ["application/json"]
    assert doc["parameters"][0]["style"] == "form" and doc["parameters"][0]["explode"] is False
    surface = Surface(indexes, lambda _, index: Responder(index))
    assert surface.call("create-thing", {"spaceId": "4"}, {"title": "yes"}).body == {"id": 42}
    # No lower file exists: primary discovery and resolution must remain lazy.
    assert surface.search(["GOTCHA"])[0]["operationId"] == "createThing"
    assert surface.topics() == ["auth", "pages"]


def test_validation_precedes_factory_and_body_validation_is_opt_in(tmp_path):
    calls = []

    def factory(_, index):
        calls.append(1)
        return Responder(index)

    surface = Surface(product(tmp_path), factory)
    for params in ({}, {"spaceId": "bad"}, {"spaceId": True}, {"unknown": "1"}):
        with pytest.raises(SurfaceError) as caught:
            surface.call("createThing", params)
        assert caught.value.code == 2 and caught.value.note == "a useful gotcha"
    assert calls == []
    with pytest.raises(SurfaceError, match="title"):
        surface.call("createThing", {"spaceId": "4"}, {}, validate_body=True)
    assert calls == []
    surface.call("createThing", {"spaceId": "4"}, {})
    assert calls == [1]


def test_400_preserves_messages_and_adds_body_detail(tmp_path):
    surface = Surface(
        product(tmp_path),
        lambda _, index: Responder(
            index,
            status=400,
            body={
                "errors": [
                    {"title": "Bad", "detail": "Required field missing"},
                    {"message": "Second"},
                ]
            },
        ),
    )
    with pytest.raises(SurfaceError) as caught:
        surface.call("createThing", {"spaceId": "5"}, {})
    error = caught.value.as_dict()
    assert error == {
        "status": 400,
        "messages": ["Bad", "Required field missing", "Second", "body.title: is required"],
        "operation": "createThing",
        "note": "a useful gotcha",
    }
    assert caught.value.code == 2


def test_deprecation_describe_topics_and_lower_lookup(tmp_path):
    indexes = product(
        tmp_path, deprecated=True, **{"x-as-deprecation": {"replacement": "newThing"}}
    )
    warnings = []
    surface = Surface(indexes, lambda _, index: Responder(index))
    assert surface.search(["page"]) == []
    assert surface.search(["page"], include_deprecated=True)[0]["path"] == "/things"
    value = surface.describe("create-thing")
    rendered = describe_markdown(value)
    assert "--space-id" in rendered and "(required)" in rendered
    assert "newThing" in rendered and "scope:" in rendered and "gotcha" in rendered
    assert value["description"] == "Short." and value["body"][0]["name"] == "title"
    surface.call("createThing", {"spaceId": "4"}, warn=warnings.append)
    assert len(warnings) == 1 and "newThing" in warnings[0]
    lower = json.loads((tmp_path / "primary.json").read_text())
    record = lower["operations"].pop("createThing")
    record["operationId"] = "getOldThing"
    lower["operations"]["getOldThing"] = record
    (tmp_path / "lower.json").write_text(json.dumps(lower))
    assert surface.describe("get-old-thing")["operationId"] == "getOldThing"
    with pytest.raises(SurfaceError) as caught:
        surface.describe("absent")
    assert caught.value.code == 5


def test_media_example_projection_and_parameter_flags(tmp_path):
    indexes = product(
        tmp_path,
        responses={
            "200": {
                "content": {
                    "application/json": {
                        "schema": {"type": "array", "items": {"type": "integer"}},
                        "example": [7, 8],
                    }
                }
            }
        },
    )
    surface = Surface(indexes, lambda _, index: Responder(index))
    assert surface.call("createThing", {"spaceId": 1}).body == [7, 8]
    _, _, operation = indexes.find("createThing")
    parameters, options = parse_call_flags(
        operation, ["--space-id=4", "--field", "title=hi", "--validate-body"]
    )
    assert parameters == {"spaceId": "4"} and options["validate_body"] is True
    for flags in (
        ["--bad", "4"],
        ["--space-id"],
        ["--space-id", "1", "--space-id", "2"],
        ["--format", "bad"],
    ):
        with pytest.raises(ValueError):
            parse_call_flags(operation, flags)
