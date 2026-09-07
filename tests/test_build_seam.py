import hashlib
import json

import pytest

from as_engine.build import compile_product
from as_engine.compiler import compile_document
from as_engine.index import ProductIndexes, load_index
from as_engine.overlay import apply_overlay


def _document():
    return {
        "openapi": "3.0.0",
        "info": {"version": "3.0.0"},
        "x-atlassian-narrative": {"discard": True},
        "components": {
            "parameters": {
                "Thing": {
                    "name": "thing",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                }
            },
            "requestBodies": {
                "ThingBody": {
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}
                    }
                }
            },
            "responses": {
                "Created": {
                    "description": "ok",
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Created"}}
                    },
                }
            },
            "schemas": {
                "Thing": {
                    "type": "object",
                    "properties": {"child": {"$ref": "#/components/schemas/Child"}},
                },
                "Child": {"allOf": [{"$ref": "#/components/schemas/Thing"}]},
                "Created": {"type": "object"},
                "Unused": {"type": "string"},
            },
        },
        "paths": {
            "/things/{thing}": {
                "parameters": [{"$ref": "#/components/parameters/Thing"}],
                "get": {
                    "operationId": "getThing",
                    "tags": ["things"],
                    "summary": "Get thing",
                    "description": "First paragraph.\n\nSecond paragraph.",
                    "parameters": [
                        {
                            "name": "thing",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Thing"}
                                }
                            },
                        },
                        "201": {"$ref": "#/components/responses/Created"},
                    },
                    "x-test": {"yes": True},
                },
                "post": {
                    "operationId": "postThing",
                    "requestBody": {"$ref": "#/components/requestBodies/ThingBody"},
                    "responses": {"200": {"description": "none"}},
                },
            }
        },
    }


def test_compile_golden_bytes_and_schema_closure():
    actual = compile_document(_document())
    expected = {
        "format_version": 1,
        "operations": {
            "getThing": {
                "operationId": "getThing",
                "method": "GET",
                "path": "/things/{thing}",
                "tags": ["things"],
                "summary": "Get thing",
                "description": "First paragraph.",
                "parameters": [
                    {"name": "thing", "in": "path", "required": True, "type": "integer"}
                ],
                "requestBody": None,
                "response_200": "Thing",
                "extensions": {"x-test": {"yes": True}},
                "reachable_schemas": ["Child", "Created", "Thing"],
            },
            "postThing": {
                "operationId": "postThing",
                "method": "POST",
                "path": "/things/{thing}",
                "tags": [],
                "summary": None,
                "description": None,
                "parameters": [{"name": "thing", "in": "path", "required": True, "type": "string"}],
                "requestBody": {"ref": "Thing"},
                "request_media_types": ["application/json"],
                "response_200": None,
                "extensions": {},
                "reachable_schemas": ["Child", "Thing"],
            },
        },
        "schemas": {
            "Child": _document()["components"]["schemas"]["Child"],
            "Created": _document()["components"]["schemas"]["Created"],
            "Thing": _document()["components"]["schemas"]["Thing"],
        },
    }
    assert (
        json.dumps(actual, sort_keys=True, separators=(",", ":")) + "\n"
        == json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n"
    )


def test_overlay_is_pure_supports_chained_brackets_and_preserves_metadata():
    document = {"paths": {"/a.b": {"parameters": [{"name": "before"}]}}}
    overlay = {
        "actions": [
            {
                "target": "$.paths['/a.b'].parameters[0]",
                "update": {"name": "after", "x": [1]},
                "description": "metadata",
            }
        ]
    }
    result = apply_overlay(document, overlay)
    assert result["paths"]["/a.b"]["parameters"][0] == {"name": "after", "x": [1]}
    assert document["paths"]["/a.b"]["parameters"][0] == {"name": "before"}
    assert overlay["actions"][0]["description"] == "metadata"


@pytest.mark.parametrize(
    "target", ["$.paths[*]", "$..paths", "$.paths[0:1]", "$.paths[?(@.x)]", "$.paths[-1]"]
)
def test_overlay_rejects_unsupported_or_missing_targets(target):
    with pytest.raises(ValueError):
        apply_overlay({"paths": {}}, {"actions": [{"target": target, "update": {}}]})
    with pytest.raises(ValueError, match="does not exist"):
        apply_overlay(
            {"paths": {}}, {"actions": [{"target": "$.paths.missing.value", "update": 1}]}
        )


def test_compiler_rejects_bad_structural_refs_and_duplicate_ids():
    document = _document()
    document["paths"]["/things/{thing}"]["get"]["parameters"] = [
        {"$ref": "https://example.test/parameter"}
    ]
    with pytest.raises(ValueError, match="external parameter"):
        compile_document(document)
    document = _document()
    document["paths"]["/things/{thing}"]["post"]["operationId"] = "getThing"
    with pytest.raises(ValueError, match="duplicate operationId"):
        compile_document(document)


def test_compile_product_writes_hand_authored_golden_bytes(tmp_path):
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    document = {
        "openapi": "3.0.0",
        "info": {"version": "1.2.3"},
        "paths": {
            "/ping": {"get": {"operationId": "ping", "responses": {"200": {"description": "ok"}}}}
        },
    }
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    (spec_dir / "api.json").write_bytes(raw)
    (spec_dir / "api.overlay.json").write_text(
        json.dumps(
            {
                "actions": [
                    {
                        "target": "$.paths['/ping'].get",
                        "update": {"x-added": True},
                        "description": "metadata stays in overlay",
                        "x-as-reason": "golden build provenance fixture",
                        "x-as-origin": "test_build_seam",
                        "x-as-test": "compile-product-ping",
                        "x-as-evidence": {
                            "url": "https://example.test/evidence",
                            "date": "2026-09-06",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    manifest = {
        "format_version": 1,
        "documents": [
            {
                "id": "api",
                "file": "api.json",
                "url": "https://example.test/api",
                "declared_version": "1.2.3",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "fetched_at": "2026-09-06",
                "tier": "primary",
                "overlays": ["api.overlay.json"],
                "strip_extensions": ["x-atlassian-narrative"],
            }
        ],
    }
    (spec_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    out_dir = tmp_path / "out"
    compile_product(spec_dir, out_dir)
    expected = {
        "format_version": 1,
        "operations": {
            "ping": {
                "operationId": "ping",
                "method": "GET",
                "path": "/ping",
                "tags": [],
                "summary": None,
                "description": None,
                "parameters": [],
                "requestBody": None,
                "response_200": None,
                "extensions": {"x-added": True},
                "reachable_schemas": [],
            }
        },
        "schemas": {},
    }
    assert (out_dir / "api.index.json").read_bytes() == (
        json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def test_normalizer_runs_after_overlay():
    document = _document()
    overlay = {"actions": [{"target": "$.components.schemas.Thing", "update": {"x-overlay": True}}]}

    def normalize(value):
        value["components"]["schemas"]["Thing"].pop("x-overlay")
        value["components"]["schemas"]["Thing"]["x-normalized"] = True

    assert (
        compile_document(document, [overlay], normalizers=[normalize])["schemas"]["Thing"][
            "x-normalized"
        ]
        is True
    )


def test_compile_product_hash_validation_and_lazy_loader(tmp_path, monkeypatch):
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    raw = json.dumps(_document(), sort_keys=True, separators=(",", ":")).encode()
    (spec_dir / "one.json").write_bytes(raw)
    manifest = {
        "format_version": 1,
        "documents": [
            {
                "id": "one",
                "file": "one.json",
                "url": "https://example.test/one",
                "declared_version": "3.0.0",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "fetched_at": "2026-09-06",
                "tier": "primary",
                "overlays": [],
                "strip_extensions": ["x-atlassian-narrative"],
            },
            {
                "id": "two",
                "file": "one.json",
                "url": "https://example.test/two",
                "declared_version": "3.0.0",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "fetched_at": "2026-09-06",
                "tier": "lower",
                "overlays": [],
                "strip_extensions": ["x-atlassian-narrative"],
            },
        ],
    }
    (spec_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    out_dir = tmp_path / "out"
    assert compile_product(spec_dir, out_dir)["documents"][1]["tier"] == "lower"
    lower = out_dir / "two.index.json"
    parked = out_dir / "two.parked.json"
    lower.rename(parked)
    product = ProductIndexes(out_dir)
    with pytest.raises(FileNotFoundError):
        product.get("two")
    parked.rename(lower)
    assert product.get("two").operations["getThing"].method == "GET"
    assert load_index(out_dir / "one.index.json").schemas["Thing"]["type"] == "object"
    manifest["documents"][0]["sha256"] = "0" * 64
    (spec_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="sha256 mismatch"):
        compile_product(spec_dir, tmp_path / "bad")


def test_overlay_remove_array_append_root_update_and_escaped_keys():
    original = {"items": [{"keep": 1, "drop": True}], "quote'key": {"list": [1]}}
    overlay = {
        "actions": [
            {"target": "$.items[0].drop", "remove": True},
            {"target": "$['quote\\'key']['list']", "update": [2]},
            {"target": "$", "update": {"added": True}},
            {"target": "$.items[0]", "remove": True},
        ]
    }
    assert apply_overlay(original, overlay) == {
        "items": [],
        "quote'key": {"list": [1, 2]},
        "added": True,
    }
    assert original["items"] == [{"keep": 1, "drop": True}]


@pytest.mark.parametrize(
    "action,message",
    [
        ({"target": "$.missing", "update": 1}, "does not exist"),
        ({"target": "$", "remove": True}, "cannot remove"),
        ({"target": "$", "update": {}, "copy": "$.a"}, "copy is unsupported"),
        ({"target": "$", "update": {}, "remove": True}, "exactly one"),
        ({"target": "$['bad\\n']", "update": {}}, "unsupported JSONPath escape"),
    ],
)
def test_overlay_errors_are_explicit(action, message):
    with pytest.raises(ValueError, match=message):
        apply_overlay({}, {"actions": [action]})


def test_all_media_schemas_and_escaped_refs_but_not_literal_refs():
    document = _document()
    schemas = document["components"]["schemas"]
    schemas["a/b~c"] = {"type": "string", "enum": ["yes"]}
    schemas["Extra"] = {"type": "object"}
    schemas["Thing"]["example"] = {"$ref": "literal-not-a-schema-reference"}
    schemas["Thing"]["properties"]["default"] = {"$ref": "#/components/schemas/Extra"}
    operation = document["paths"]["/things/{thing}"]["get"]
    operation["parameters"].append(
        {"name": "choice", "in": "query", "schema": {"$ref": "#/components/schemas/a~1b~0c"}}
    )
    operation["responses"]["200"]["content"]["application/xml"] = {
        "schema": {"$ref": "#/components/schemas/Extra"}
    }
    operation["responses"]["x-note"] = "not a response"
    operation["description"] = "First.\r\n \r\nSecond."
    index = compile_document(document)
    parameter = index["operations"]["getThing"]["parameters"][-1]
    assert parameter["type"] == "string" and parameter["enum"] == ["yes"]
    assert parameter["schema"]["$ref"] == "#/components/schemas/a~1b~0c"
    assert "a/b~c" in index["schemas"] and "Extra" in index["schemas"]
    assert index["schemas"]["Thing"]["example"]["$ref"] == "literal-not-a-schema-reference"
    assert index["operations"]["getThing"]["description"] == "First."


def test_structural_reference_chains_and_cycles():
    document = _document()
    parameters = document["components"]["parameters"]
    parameters["Alias"] = {"$ref": "#/components/parameters/Thing"}
    document["paths"]["/things/{thing}"]["parameters"] = [{"$ref": "#/components/parameters/Alias"}]
    assert compile_document(document)["operations"]["postThing"]["parameters"][0]["name"] == "thing"
    parameters["Thing"] = {"$ref": "#/components/parameters/Alias"}
    with pytest.raises(ValueError, match="cyclic parameter"):
        compile_document(document)


def test_narrative_strip_hook_observes_document_and_preserves_operation_tags():
    document = _document()

    def hook(value):
        assert "x-atlassian-narrative" not in value
        assert value["paths"]["/things/{thing}"]["get"]["x-test"] == {"yes": True}
        value["components"]["schemas"]["Thing"]["x-hook"] = True

    result = compile_document(document, normalizers=[hook])
    assert result["schemas"]["Thing"]["x-hook"]
    assert "x-atlassian-narrative" in document
    with pytest.raises(ValueError, match="only top-level x-"):
        compile_document(document, strip_extensions=["paths"])
