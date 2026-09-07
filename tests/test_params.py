from __future__ import annotations

from io import StringIO

import pytest

from as_engine.index import Operation
from as_engine.params import body_errors, build_body, kebab_case, validate_parameters


def _operation(parameters: list[dict] | None = None, request_body: dict | None = None) -> Operation:
    return Operation(
        "op", "GET", "/x", [], None, None, parameters or [], request_body, None, {}, []
    )


def test_validate_parameters_converts_strict_types_enums_and_bounds():
    operation = _operation(
        [
            {"name": "id", "required": True, "type": "integer", "minimum": 2},
            {"name": "enabled", "type": "boolean"},
            {
                "name": "tags",
                "schema": {"type": "array", "items": {"$ref": "#/components/schemas/Tag"}},
            },
            {"name": "kind", "schema": {"$ref": "#/components/schemas/Kind"}},
        ]
    )
    values = validate_parameters(
        operation,
        {"id": "3", "enabled": "true", "tags": "a,b", "kind": "one"},
        {
            "Kind": {"type": "string", "enum": ["one"]},
            "Tag": {"type": "string", "enum": ["a", "b"]},
        },
    )
    assert values == {"id": 3, "enabled": True, "tags": ["a", "b"], "kind": "one"}
    with pytest.raises(ValueError, match="invalid parameter id"):
        validate_parameters(operation, {"id": True}, {"Kind": {"type": "string"}})
    with pytest.raises(ValueError, match="unknown parameter"):
        validate_parameters(operation, {"id": "3", "extra": "x"}, {})
    with pytest.raises(ValueError, match="missing required"):
        validate_parameters(operation, {}, {})


def test_build_body_supports_only_file_or_stdin_and_rejects_collisions(tmp_path):
    path = tmp_path / "body.json"
    path.write_text('{"saved": true}', encoding="utf-8")
    assert build_body(f"@{path}", ["nested.count=2"]) == {"saved": True, "nested": {"count": 2}}
    assert build_body("-", ["name=value"], StringIO("{}")) == {"name": "value"}
    with pytest.raises(ValueError, match="must be @file or -"):
        build_body('{"bad": true}', [])
    with pytest.raises(ValueError, match="collides"):
        build_body(None, ["a=1", "a.b=2"])
    with pytest.raises(ValueError, match="object body"):
        build_body("-", ["a=1"], StringIO("[]"))


def test_body_errors_handles_required_refs_and_compositions():
    operation = _operation(request_body={"ref": "Payload", "required": True})
    assert (
        body_errors(
            operation,
            {"name": "ok", "choice": 1},
            {
                "Payload": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "choice": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
                    },
                }
            },
        )
        == []
    )
    errors = body_errors(
        operation,
        {"choice": False},
        {
            "Payload": {
                "type": "object",
                "required": ["name"],
                "properties": {"choice": {"anyOf": [{"type": "integer"}, {"type": "string"}]}},
            }
        },
    )
    assert "body.name: is required" in errors and "body.choice: does not match anyOf" in errors
    assert body_errors(operation, None, {}) == ["body: is required"]


def test_kebab_case():
    assert kebab_case("getHTTPPages_v2") == "get-http-pages-v2"


@pytest.mark.parametrize(
    ("raw", "schema", "schemas", "expected"),
    [
        (
            '["ok", "no"]',
            {"type": "array", "items": {"$ref": "#/components/schemas/Tag"}},
            {"Tag": {"type": "string", "enum": ["ok"]}},
            "allowed value",
        ),
        ("[1]", {"type": "array", "items": {"type": "integer", "minimum": 2}}, {}, "below minimum"),
        (
            '{"count": 2}',
            {"type": "object", "properties": {"count": {"type": "integer", "minimum": 3}}},
            {},
            "below minimum",
        ),
        ("opaque", {"type": "unknown"}, {}, None),
    ],
)
def test_parameter_recursive_validation_and_unknown_type(raw, schema, schemas, expected):
    operation = _operation([{"name": "value", "schema": schema}])
    if expected is None:
        assert validate_parameters(operation, {"value": raw}, schemas) == {"value": raw}
    else:
        with pytest.raises(ValueError, match=expected):
            validate_parameters(operation, {"value": raw}, schemas)


def test_parameter_cycle_is_a_controlled_value_error():
    operation = _operation([{"name": "value", "schema": {"$ref": "#/components/schemas/A"}}])
    with pytest.raises(ValueError, match="cyclic schema reference"):
        validate_parameters(
            operation,
            {"value": "x"},
            {"A": {"$ref": "#/components/schemas/B"}, "B": {"$ref": "#/components/schemas/A"}},
        )


def test_body_validation_keeps_allof_siblings_and_enforces_constraints():
    operation = _operation(
        request_body={
            "schema": {
                "type": "object",
                "required": ["name"],
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "minLength": 2},
                    "items": {
                        "type": "array",
                        "minItems": 2,
                        "items": {"type": "number", "maximum": 3},
                    },
                    "optional": {"type": "string", "nullable": True},
                },
                "allOf": [{"properties": {"name": {"type": "string", "maxLength": 3}}}],
            }
        }
    )
    errors = body_errors(operation, {"name": "", "items": [4], "extra": True, "optional": None}, {})
    assert "body.name: too short" in errors
    assert "body.items: too short" in errors
    assert "body.items[0]: above maximum" in errors
    assert "body.extra: is not allowed" in errors
    assert body_errors(operation, {"name": "yes", "items": [1, 2], "optional": None}, {}) == []


def test_body_ref_escapes_component_name_and_does_not_coerce():
    operation = _operation(request_body={"ref": "a/b~c"})
    assert body_errors(operation, "3", {"a/b~c": {"type": "integer"}}) == ["body: must be integer"]


def test_body_rejects_unsupported_validation_keywords_instead_of_ignoring_them():
    operation = _operation(request_body={"schema": {"type": "string", "pattern": "[a-z]+"}})
    assert body_errors(operation, "123", {}) == ["body: unsupported schema validator: pattern"]
