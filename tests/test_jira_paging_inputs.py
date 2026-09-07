"""Jira-shaped inputs exercised through compiled records and the public Surface."""

import json
from copy import deepcopy

import pytest

from as_engine.compiler import compile_document
from as_engine.errors import SurfaceError
from as_engine.index import ProductIndexes
from as_engine.responder import Responder
from as_engine.surface import Surface, parse_call_flags


def surface_for(tmp_path, *, body=False, string_offset=False, target=None):
    token = target or {
        "in": "body" if body else "query",
        **({"path": "/nextPageToken"} if body else {"name": "nextPageToken"}),
    }
    tag = {
        "style": "nextPageToken",
        "request": {"token": token},
        "itemsPath": "/issues",
        "next": {"kind": "token", "path": "/nextPageToken"},
        "response": {"isLastPath": "/isLast"},
    }
    parameters = (
        [{"name": "nextPageToken", "in": "query", "schema": {"type": "string"}}] if not body else []
    )
    if string_offset:
        tag = {
            "style": "offset/limit",
            "request": {
                "offset": {"in": "query", "name": "startAt"},
                "limit": {"in": "query", "name": "maxResults"},
            },
            "itemsPath": "/issues",
            "response": {"totalPath": "/total"},
        }
        parameters = [
            {"name": name, "in": "query", "schema": {"type": kind}}
            for name, kind in (("startAt", "string"), ("maxResults", "integer"))
        ]
    operation = {
        "operationId": "searchIssues",
        "parameters": parameters,
        "x-as-paging": tag,
        "responses": {"200": {"description": "ok"}},
    }
    if body:
        operation["requestBody"] = {
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Search"}}}
        }
    doc = {
        "openapi": "3.0.1",
        "paths": {"/search": {"post" if body else "get": operation}},
        "components": {
            "schemas": {
                "Search": {
                    "type": "object",
                    "properties": {
                        "nextPageToken": {"type": "string"},
                        "maxResults": {"type": "integer"},
                        "jql": {"type": "string"},
                    },
                }
            }
        },
    }
    (tmp_path / "index.json").write_text(json.dumps(compile_document(doc)))
    (tmp_path / "catalog.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "documents": [{"id": "jira", "tier": "primary", "file": "index.json"}],
            }
        )
    )
    indexes = ProductIndexes(tmp_path)
    responder = Responder(indexes.get("jira"))
    return Surface(indexes, lambda *_: responder), responder


def test_post_token_updates_only_declared_body_and_keeps_caller_input(tmp_path):
    surface, responder = surface_for(tmp_path, body=True)
    responder.seed(
        "searchIssues",
        [
            {"issues": [1], "nextPageToken": "again", "isLast": False},
            {"issues": [2], "isLast": True},
        ],
    )
    body = {"jql": "project = SBX", "maxResults": 10}
    before = deepcopy(body)
    assert surface.call("searchIssues", {}, body, all_pages=True).body == [1, 2]
    assert body == before
    assert responder.requests == [
        ("searchIssues", {}, before),
        ("searchIssues", {}, {**before, "nextPageToken": "again"}),
    ]


@pytest.mark.parametrize("body", [False, True])
def test_is_last_stops_even_when_response_has_a_token(tmp_path, body):
    surface, responder = surface_for(tmp_path, body=body)
    responder.seed("searchIssues", [{"issues": [1], "nextPageToken": "unused", "isLast": True}])
    assert surface.call("searchIssues", {}, {} if body else None, all_pages=True).body == [1]
    assert len(responder.requests) == 1


@pytest.mark.parametrize(
    "response", [{"issues": [1], "isLast": "false"}, {"issues": [1], "isLast": False}]
)
def test_invalid_or_missing_nonfinal_metadata_refuses(tmp_path, response):
    surface, responder = surface_for(tmp_path)
    responder.seed("searchIssues", [response])
    with pytest.raises(SurfaceError) as caught:
        surface.call("searchIssues", {}, all_pages=True)
    assert caught.value.code == 2
    assert len(responder.requests) == 1


def test_token_without_optional_is_last_and_body_repeated_token(tmp_path):
    surface, responder = surface_for(tmp_path, body=True)
    responder.seed(
        "searchIssues",
        [{"issues": [1], "nextPageToken": "again"}, {"issues": [2], "nextPageToken": "again"}],
    )
    with pytest.raises(SurfaceError, match="repeated paging continuation"):
        surface.call("searchIssues", {}, {}, all_pages=True)
    assert len(responder.requests) == 2


def test_bad_body_target_refuses_before_transport(tmp_path):
    surface, responder = surface_for(tmp_path, body=True, target={"in": "body", "path": "/typo"})
    with pytest.raises(SurfaceError, match="not declared"):
        surface.call("searchIssues", {}, {}, all_pages=True)
    assert responder.requests == []


@pytest.mark.parametrize("initial,expected", [(None, ["0", "2"]), ("3", ["3", "5"])])
def test_string_offset_arithmetic_preserves_wire_schema(tmp_path, initial, expected):
    surface, responder = surface_for(tmp_path, string_offset=True)
    responder.seed(
        "searchIssues",
        [
            {"issues": [1, 2], "total": int(expected[-1]) + 1},
            {"issues": [3], "total": int(expected[-1]) + 1},
        ],
    )
    params = {"maxResults": 20, **({"startAt": initial} if initial is not None else {})}
    assert surface.call("searchIssues", params, all_pages=True).body == [1, 2, 3]
    assert [request[1]["startAt"] for request in responder.requests] == expected
    assert all(request[1]["maxResults"] == 20 for request in responder.requests)


@pytest.mark.parametrize("value", ["-1", "abc", "1.2", ""])
def test_invalid_string_offset_never_sends(tmp_path, value):
    surface, responder = surface_for(tmp_path, string_offset=True)
    with pytest.raises(SurfaceError):
        surface.call("searchIssues", {"startAt": value}, all_pages=True)
    assert responder.requests == []


def test_exact_and_kebab_parameter_flags_share_duplicate_and_type_contract(tmp_path):
    surface, responder = surface_for(tmp_path, string_offset=True)
    operation = surface.resolve("searchIssues")[2]
    for spelling in ("--startAt", "--start-at"):
        params, _ = parse_call_flags(operation, [spelling, "4", "--maxResults", "2"])
        surface.call("searchIssues", params)
        assert responder.requests[-1][1] == {"startAt": "4", "maxResults": 2}
    with pytest.raises(ValueError, match="Duplicate flag"):
        parse_call_flags(operation, ["--startAt", "0", "--start-at", "1"])


def test_exact_alias_does_not_hide_kebab_collision_or_reserved_names(tmp_path):
    from dataclasses import replace

    surface, _ = surface_for(tmp_path)
    operation = surface.resolve("searchIssues")[2]
    ambiguous = replace(
        operation,
        parameters=[
            {"in": "query", "name": name, "schema": {"type": "string"}}
            for name in ("fooBar", "foo-bar")
        ],
    )
    with pytest.raises(ValueError, match="ambiguous parameter flags"):
        parse_call_flags(ambiguous, ["--fooBar", "one"])
    reserved = replace(
        operation,
        parameters=[
            {"in": "query", "name": "validateBody", "type": "string", "schema": {"type": "string"}}
        ],
    )
    params, options = parse_call_flags(reserved, ["--parameter-validateBody", "value"])
    assert params == {"validateBody": "value"}
    assert not options["validate_body"]
