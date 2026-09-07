"""The public validation seam checks scope-filter arrays' uniqueness."""

import pytest

from as_engine.compiler import compile_document
from as_engine.index import Operation
from as_engine.params import body_errors, validate_parameters


def operation(unique=True, items=None):
    schema = {"type": "array", "items": {}, "uniqueItems": unique}
    if items is not None:
        schema["items"] = items
    document = compile_document(
        {
            "openapi": "3.0.3",
            "paths": {
                "/projects": {
                    "post": {
                        "operationId": "query",
                        "parameters": [{"in": "query", "name": "projects", "schema": schema}],
                        "requestBody": {"content": {"application/json": {"schema": schema}}},
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }
    )
    return Operation(**document["operations"]["query"])


@pytest.mark.parametrize(
    "values", [["SBX", "GC"], [True, 1], [False, 0], [{"a": True}, {"a": 1}], [1, "1"], []]
)
def test_unique_json_values_are_accepted(values):
    op = operation()
    assert body_errors(op, values, {}) == []
    assert validate_parameters(op, {"projects": values}, {}) == {"projects": values}


@pytest.mark.parametrize(
    "values",
    [
        ["SBX", "SBX"],
        [1, 1.0],
        [{"a": 1, "b": 2}, {"b": 2, "a": 1.0}],
        [[1, True], [1.0, True]],
        [None, None],
    ],
)
def test_duplicate_json_values_are_refused(values):
    op = operation()
    assert body_errors(op, values, {}) == ["body: array items must be unique"]
    with pytest.raises(ValueError, match="array items must be unique"):
        validate_parameters(op, {"projects": values}, {})


def test_false_preserves_duplicates_without_deduplicating():
    op = operation(False)
    assert body_errors(op, [1, 1.0], {}) == []
    assert validate_parameters(op, {"projects": [1, 1]}, {})["projects"] == [1, 1]


@pytest.mark.parametrize("metadata", [None, "true", 1, [], {}])
def test_malformed_unique_items_metadata_fails_closed(metadata):
    op = operation(metadata)
    assert body_errors(op, [], {}) == ["body: uniqueItems must be boolean"]
    with pytest.raises(ValueError, match="uniqueItems must be boolean"):
        validate_parameters(op, {"projects": []}, {})


def test_scalar_coercion_precedes_unique_comparison():
    op = operation(items={"type": "integer"})
    with pytest.raises(ValueError, match="array items must be unique"):
        validate_parameters(op, {"projects": ["1", "01"]}, {})
