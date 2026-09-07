"""Schema-declared JSON container examples are not double-encoded by the double."""

import json

import pytest

from as_engine.compiler import compile_document
from as_engine.index import load_index
from as_engine.responder import Responder


@pytest.mark.parametrize(
    "schema,example,expected",
    [
        ({"type": "object"}, '{"key":"SBX-1"}', {"key": "SBX-1"}),
        ({"type": "array", "items": {"type": "integer"}}, "[1,2]", [1, 2]),
        ({"type": "string"}, '{"literal":true}', '{"literal":true}'),
        ({"type": "object"}, "not JSON", "not JSON"),
        ({"type": "array"}, '{"wrong":"container"}', '{"wrong":"container"}'),
        ({"type": "object"}, {"already": "decoded"}, {"already": "decoded"}),
    ],
)
def test_response_schema_controls_example_decoding(tmp_path, schema, example, expected):
    document = {
        "openapi": "3.0.1",
        "paths": {
            "/things": {
                "get": {
                    "operationId": "getThings",
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Result"},
                                    "example": example,
                                }
                            }
                        }
                    },
                }
            }
        },
        "components": {"schemas": {"Result": schema}},
    }
    path = tmp_path / "index.json"
    path.write_text(json.dumps(compile_document(document)))
    index = load_index(path)
    result = Responder(index).call(index.operations["getThings"], {}, None)
    assert result.body == expected
