"""Rich text at public build, Surface and recorded transport seams."""

import json
from copy import deepcopy

import pytest

from as_engine.compiler import compile_document
from as_engine.converters import convert, validate_adf
from as_engine.errors import SurfaceError
from as_engine.index import ProductIndexes
from as_engine.responder import Responder
from as_engine.surface import Surface
from as_engine.transport import Response

REPS = {
    "storage": {"converter": "storage", "encoding": "string"},
    "atlas_doc_format": {"converter": "adf", "encoding": "json-string"},
}
DEFAULT = {"default": "storage", "alternatives": ["atlas_doc_format"]}
MENTION = {"type": "mention", "attrs": {"id": "account-7", "text": "@Ada"}}
DOC = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [MENTION]}]}


def tags(*, request=True, response=True, items=False):
    rule = {"representations": deepcopy(REPS)}
    if request:
        rule["request"] = {"path": "/body", "shape": "envelope"}
    if response:
        rule["response"] = {"path": "/body", "shape": "representation-map"}
        if items:
            rule["response"]["itemsPath"] = "/results"
    return {"x-as-richtext": [rule], "x-as-representation": deepcopy(DEFAULT)}


def surface_for(tmp_path, operations):
    """Build caller-supplied operations, then exercise the normal public pipeline."""
    paths = {
        f"/{name}": {method: {"operationId": name, **op}}
        for name, (method, op) in operations.items()
    }
    (tmp_path / "index.json").write_text(
        json.dumps(compile_document({"openapi": "3.0.3", "paths": paths}))
    )
    (tmp_path / "catalog.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "documents": [{"id": "test", "tier": "primary", "file": "index.json"}],
            }
        )
    )
    indexes = ProductIndexes(tmp_path)
    responder = Responder(indexes.get("test"), body={"id": "sent"})
    return Surface(indexes, lambda *_: responder), responder


def test_markdown_envelopes_and_call_options_do_not_persist(tmp_path):
    surface, wire = surface_for(tmp_path, {"write": ("post", tags())})
    payload = {"body": "# Notes\n\nHello **world**", "title": "T"}
    before = deepcopy(payload)
    surface.call("write", {}, payload, representation="atlas_doc_format", raw=True)
    sent = wire.requests[-1][2]
    assert sent["title"] == "T" and sent["body"]["representation"] == "atlas_doc_format"
    document = json.loads(sent["body"]["value"])
    assert isinstance(document, dict) and validate_adf(document)
    assert document["content"][0]["type"] == "heading"
    surface.call("write", {}, payload)
    assert wire.requests[-1][2]["body"] == {
        "representation": "storage",
        "value": "<h1>Notes</h1><p>Hello <strong>world</strong></p>",
    }
    assert payload == before


@pytest.mark.parametrize("text", ["", "#", "````\n\n````", "null", '{"x": 1}', "123"])
def test_converted_adf_is_valid_including_empty_and_json_looking_markdown(tmp_path, text):
    surface, wire = surface_for(tmp_path, {"write": ("post", tags(response=False))})
    surface.call("write", {}, {"body": text}, representation="atlas_doc_format")
    document = json.loads(wire.requests[-1][2]["body"]["value"])
    assert validate_adf(document)
    if text in ("null", '{"x": 1}', "123"):
        assert document["content"][0]["content"][0]["text"] == text


@pytest.mark.parametrize("as_string", [True, False])
def test_encoded_envelope_is_not_reconverted_and_caller_is_unchanged(tmp_path, as_string):
    surface, wire = surface_for(tmp_path, {"write": ("post", tags())})
    value = json.dumps(DOC, indent=2) if as_string else DOC
    payload = {"body": {"representation": "atlas_doc_format", "value": value, "extra": "keep"}}
    before = deepcopy(payload)
    surface.call("write", {}, payload)
    encoded = wire.requests[-1][2]["body"]
    assert encoded["extra"] == "keep" and json.loads(encoded["value"]) == DOC
    if as_string:
        assert encoded["value"] == value
    assert payload == before


def test_direct_value_objects_and_multiple_descriptors_with_pointer_escapes(tmp_path):
    rule = {
        "representations": {"adf": {"converter": "adf", "encoding": "object"}},
        "request": {"path": "/fields/a~1b~0c", "shape": "value"},
        "response": {"path": "/fields/a~1b~0c", "shape": "value"},
    }
    other = deepcopy(rule)
    other["request"]["path"] = other["response"]["path"] = "/other"
    surface, wire = surface_for(tmp_path, {"write": ("post", {"x-as-richtext": [rule, other]})})
    payload = {"fields": {"a/b~c": DOC}, "other": "hello"}
    wire.seed("write", [{**payload, "other": convert("hello")}])
    result = surface.call("write", {}, payload)
    assert wire.requests[-1][2]["fields"]["a/b~c"] == DOC
    assert validate_adf(wire.requests[-1][2]["other"])
    assert "mention" in result.body["fields"]["a/b~c"]
    assert payload["fields"]["a/b~c"] == DOC


def test_read_metadata_raw_and_mention_round_trip(tmp_path):
    surface, wire = surface_for(
        tmp_path, {"read": ("get", tags(request=False)), "write": ("post", tags())}
    )
    original = {
        "id": "7",
        "body": {
            "atlas_doc_format": {
                "representation": "atlas_doc_format",
                "value": json.dumps(DOC),
                "links": {"self": "local"},
            }
        },
        "version": {"number": 2},
    }
    wire.seed("read", [Response(200, original, {"X-Tag": "7"})] * 3)
    rendered = surface.call("read", {})
    value = rendered.body["body"]["atlas_doc_format"]["value"]
    assert '{{as:1:adf:inline:mention:"@Ada":' in value
    assert rendered.headers == {"X-Tag": "7"} and rendered.body["version"] == {"number": 2}
    assert rendered.body["body"]["atlas_doc_format"]["links"] == {"self": "local"}
    assert surface.call("read", {}, raw=True).body == original
    assert surface.call("read", {}).body == rendered.body
    surface.call("write", {}, {"body": value}, representation="atlas_doc_format")
    assert json.loads(wire.requests[-1][2]["body"]["value"]) == DOC
    assert validate_adf(DOC)


def test_paging_renders_all_merged_items_after_last_page(tmp_path):
    op = tags(request=False, items=True)
    op.update(
        {
            "parameters": [{"name": "cursor", "in": "query", "schema": {"type": "string"}}],
            "x-as-paging": {
                "style": "cursor",
                "itemsPath": "/results",
                "request": {"token": {"in": "query", "name": "cursor"}},
                "next": {"kind": "token", "path": "/cursor"},
            },
        }
    )
    surface, wire = surface_for(tmp_path, {"read": ("get", op)})
    first = {"results": [{"id": 1, "body": {"storage": {"value": "<p>one</p>"}}}], "cursor": "two"}
    last = {"results": [{"id": 2, "body": {"atlas_doc_format": {"value": json.dumps(DOC)}}}]}
    wire.seed("read", [first, first, last])
    one = surface.call("read", {})
    assert (
        one.body["cursor"] == "two" and one.body["results"][0]["body"]["storage"]["value"] == "one"
    )
    warnings = []
    result = surface.call("read", {}, all_pages=True, warn=warnings.append)
    assert result.body[0]["body"]["storage"]["value"] == "one"
    assert "mention" in result.body[1]["body"]["atlas_doc_format"]["value"]
    assert warnings == ["count=2"]
    assert wire.requests[-1][1] == {"cursor": "two"}
    assert first["results"][0]["body"]["storage"]["value"] == "<p>one</p>"


@pytest.mark.parametrize("payload", [{"id": 1}, {"body": {}}, {"results": [{"id": 1}]}])
def test_absent_optional_values_remain_absent(tmp_path, payload):
    surface, wire = surface_for(tmp_path, {"read": ("get", tags(request=False))})
    wire.seed("read", [payload])
    assert surface.call("read", {}).body == payload


@pytest.mark.parametrize(
    "body, options",
    [
        ({"body": "text"}, {"representation": "wiki"}),
        (
            {"body": {"representation": "storage", "value": "<p>T</p>"}},
            {"representation": "atlas_doc_format"},
        ),
        ({"body": {"value": "encoded"}}, {}),
        ({"body": {"representation": "storage", "value": 4}}, {}),
        ({"body": {"representation": "atlas_doc_format", "value": '"double encoded"'}}, {}),
        ({"body": "text"}, {"raw": True}),
    ],
)
def test_invalid_options_and_envelopes_fail_before_lookup(tmp_path, body, options):
    op = tags(response=False)
    op["x-as-version"] = {
        "operationId": "lookup",
        "parameters": {},
        "responsePath": "/number",
        "target": {"in": "body", "path": "/version/number"},
        "increment": 1,
    }
    surface, wire = surface_for(tmp_path, {"write": ("post", op), "lookup": ("get", {})})
    with pytest.raises(SurfaceError) as caught:
        surface.call("write", {}, body, **options)
    assert caught.value.code == 2 and wire.requests == []


@pytest.mark.parametrize(
    "value",
    [
        None,
        "bad",
        {"view": {"value": "<p>T</p>"}},
        {"storage": {"value": 4}},
        {"storage": {"representation": "atlas_doc_format", "value": "<p>T</p>"}},
        {"atlas_doc_format": {"value": "invalid json"}},
    ],
)
def test_present_malformed_responses_are_errors_but_raw_bypasses_rendering(tmp_path, value):
    surface, wire = surface_for(tmp_path, {"read": ("get", tags(request=False))})
    wire.seed("read", [{"body": value}] * 2)
    with pytest.raises(SurfaceError) as caught:
        surface.call("read", {})
    assert caught.value.code == 2
    assert surface.call("read", {}, raw=True).body == {"body": value}


def test_lookup_outputs_stay_raw_and_outer_options_do_not_leak(tmp_path):
    op = tags()
    op.update(
        {
            "x-as-scope": {"in": "query", "name": "tenant"},
            "parameters": [
                {"name": "tenant", "in": "query", "required": True, "schema": {"type": "string"}}
            ],
            "x-as-prerequisites": [
                {
                    "alias": "key",
                    "operationId": "lookup",
                    "parameter": {"in": "query", "name": "key"},
                    "resultsPath": "/results",
                    "matchPath": "/key",
                    "valuePath": "/id",
                    "target": {"in": "body", "path": "/id"},
                }
            ],
            "x-as-version": {
                "operationId": "version",
                "parameters": {},
                "responsePath": "/number",
                "target": {"in": "body", "path": "/version/number"},
                "increment": 1,
            },
            "x-as-format": [{"target": {"in": "body", "path": "/elapsed"}, "format": "duration"}],
        }
    )
    child = tags(request=False, items=True)
    child["parameters"] = [{"name": "key", "in": "query", "schema": {"type": "string"}}]
    surface, wire = surface_for(
        tmp_path,
        {"write": ("post", op), "lookup": ("get", child), "version": ("get", tags(request=False))},
    )
    # Poison content is irrelevant to the raw metadata consumers; trying to
    # render child bodies would fail before the final request.
    wire.seed("lookup", [{"results": [{"key": "K", "id": "9", "body": {"view": "poison"}}]}])
    wire.seed("version", [{"number": 6, "body": {"view": "poison"}}])
    body = {"body": "Hi", "elapsed": "2h30m"}
    with pytest.raises(SurfaceError) as caught:
        surface.call("write", {"tenant": "NO"}, body, aliases={"key": "K"})
    assert caught.value.code == 4 and wire.requests == []
    surface.call(
        "write",
        {"tenant": "YES"},
        body,
        aliases={"key": "K"},
        scope_allowlist=("YES",),
        representation="atlas_doc_format",
    )
    assert [r[0] for r in wire.requests] == ["lookup", "version", "write"]
    sent = wire.requests[-1][2]
    assert sent["id"] == "9" and sent["version"] == {"number": 7} and sent["elapsed"] == 9000
    assert validate_adf(json.loads(sent["body"]["value"]))
    assert body == {"body": "Hi", "elapsed": "2h30m"}


def test_final_body_validation_and_400_diagnostics(tmp_path):
    op = tags()
    op["requestBody"] = {
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["body"],
                    "properties": {
                        "body": {
                            "type": "object",
                            "required": ["representation", "value"],
                            "properties": {
                                "representation": {"type": "string"},
                                "value": {"type": "string"},
                            },
                        }
                    },
                }
            }
        }
    }
    surface, wire = surface_for(tmp_path, {"write": ("post", op)})
    surface.call("write", {}, {"body": "Hi"}, validate_body=True)
    assert wire.requests[-1][2]["body"]["value"] == "<p>Hi</p>"
    wire.seed("write", [Response(400, {"message": "remote error"})])
    with pytest.raises(SurfaceError) as caught:
        surface.call("write", {}, {"body": "Hi"})
    assert caught.value.messages == ["remote error"]  # Final envelope is valid.


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {},
        [{"request": None}],
        [
            {
                "representations": {"bad": {"converter": "storage", "encoding": "object"}},
                "request": {"path": "/body", "shape": "value"},
            }
        ],
    ],
)
def test_malformed_tags_fail_closed(tmp_path, bad):
    surface, wire = surface_for(tmp_path, {"write": ("post", {"x-as-richtext": bad})})
    with pytest.raises(SurfaceError) as caught:
        surface.call("write", {}, {"body": "T"})
    assert caught.value.code == 2 and wire.requests == []


@pytest.mark.parametrize(
    "document",
    [
        {"type": "doc", "version": True, "content": []},
        {"type": "doc", "version": 1, "content": [{"type": "text", "text": ""}]},
        {"type": "doc", "version": 1, "content": [None]},
        {"type": "doc", "version": 1, "content": None},
    ],
)
def test_encoded_malformed_adf_is_refused_before_transport(tmp_path, document):
    surface, wire = surface_for(tmp_path, {"write": ("post", tags())})
    with pytest.raises(SurfaceError) as caught:
        surface.call(
            "write", {}, {"body": {"representation": "atlas_doc_format", "value": document}}
        )
    assert caught.value.code == 2 and wire.requests == []


def jira_value_tags(*, bulk=False, nullable=False):
    descriptors = []
    for name in ("description", "environment"):
        location = {"path": f"/fields/{name}", "shape": "value", "nullable": nullable}
        request = {**location, **({"itemsPath": "/issueUpdates"} if bulk else {})}
        descriptors.append(
            {
                "representations": {"adf": {"converter": "adf", "encoding": "object"}},
                "request": request,
                "response": location,
                "customFields": "textarea",
            }
        )
    return {"x-as-richtext": descriptors, "x-as-representation": {"default": "adf"}}


def test_bulk_request_converts_each_item_without_guessing_custom_fields(tmp_path):
    surface, wire = surface_for(tmp_path, {"bulk": ("post", jira_value_tags(bulk=True))})
    original = {
        "issueUpdates": [
            {"fields": {"description": "# First", "customfield_10010": "**untouched**"}},
            {"fields": {"environment": "second", "description": DOC}},
            {"fields": {"summary": "third"}},
        ]
    }
    before = deepcopy(original)
    surface.call("bulk", {}, original)
    sent = wire.requests[-1][2]["issueUpdates"]
    assert validate_adf(sent[0]["fields"]["description"])
    assert validate_adf(sent[1]["fields"]["environment"])
    assert sent[0]["fields"]["customfield_10010"] == "**untouched**"
    assert sent[1]["fields"]["description"] == DOC
    assert sent[2] == original["issueUpdates"][2] and original == before


@pytest.mark.parametrize("items", [None, {}, "not an array"])
def test_bad_bulk_shape_fails_before_any_transport(tmp_path, items):
    surface, wire = surface_for(tmp_path, {"bulk": ("post", jira_value_tags(bulk=True))})
    with pytest.raises(SurfaceError, match="itemsPath must identify an array"):
        surface.call("bulk", {}, {"issueUpdates": items})
    assert wire.requests == []


@pytest.mark.parametrize("nullable", [False, True])
def test_direct_adf_null_requires_explicit_nullable_tag(tmp_path, nullable):
    surface, wire = surface_for(tmp_path, {"write": ("post", jira_value_tags(nullable=nullable))})
    body = {"fields": {"description": None, "environment": DOC}}
    wire.seed("write", [body])
    if not nullable:
        with pytest.raises(SurfaceError):
            surface.call("write", {}, body)
        assert wire.requests == []
        return
    result = surface.call("write", {}, body)
    assert wire.requests[-1][2] == body
    assert result.body["fields"]["description"] is None
    assert "mention" in result.body["fields"]["environment"]
    wire.seed("write", [body])
    assert surface.call("write", {}, body, raw=True).body == body


def test_bulk_paths_do_not_reinterpret_top_level_field_input(tmp_path):
    from as_engine.params import build_body

    surface, wire = surface_for(tmp_path, {"bulk": ("post", jira_value_tags(bulk=True))})
    _, _, operation = surface.resolve("bulk")
    payload = build_body(
        None,
        ["fields.description=null", 'issueUpdates=[{"fields":{"description":"Hi"}}]'],
        operation=operation,
    )
    assert payload["fields"]["description"] is None
    surface.call("bulk", {}, payload)
    assert wire.requests[-1][2]["fields"]["description"] is None
    assert validate_adf(wire.requests[-1][2]["issueUpdates"][0]["fields"]["description"])


@pytest.mark.parametrize(
    "change",
    [
        {"customFields": "guess"},
        {"request": {"path": "/fields/description", "shape": "value", "nullable": "yes"}},
        {"request": {"path": "/fields/description", "shape": "envelope", "nullable": True}},
        {"request": {"path": "/fields/description", "shape": "value", "itemsPath": 1}},
    ],
)
def test_jira_additive_descriptor_options_fail_closed(tmp_path, change):
    tags = jira_value_tags()
    tags["x-as-richtext"][0].update(change)
    surface, wire = surface_for(tmp_path, {"write": ("post", tags)})
    with pytest.raises(SurfaceError):
        surface.call("write", {}, {"fields": {"description": "hello"}})
    assert wire.requests == []
