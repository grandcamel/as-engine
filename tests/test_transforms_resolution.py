"""Public calls expose transformed requests and fail without guessing or retrying."""

import json

import pytest

from as_engine.compiler import compile_document
from as_engine.errors import SurfaceError
from as_engine.index import ProductIndexes
from as_engine.responder import Responder
from as_engine.surface import Surface, parse_call_flags
from as_engine.transforms import Registry, Transform
from as_engine.transport import HTTPTransport, Response


def setup(tmp_path, *, parameter_target=False):
    def param(name, kind="string", location="query", required=False):
        return {"name": name, "in": location, "required": required, "schema": {"type": kind}}

    paging = {
        "style": "cursor",
        "itemsPath": "/results",
        "request": {"token": {"in": "query", "name": "cursor"}},
        "next": {"kind": "token", "path": "/cursor"},
    }
    prereq = [
        {
            "target": {"in": "query", "name": "spaceId"}
            if parameter_target
            else {"in": "body", "path": "/spaceId"},
            "alias": "space-key",
            "operationId": "getSpaces",
            "parameter": {"in": "query", "name": "keys", "array": True},
            "resultsPath": "/results",
            "matchPath": "/key",
            "valuePath": "/id",
        }
    ]
    version = {
        "operationId": "getPage",
        "parameters": {"id": {"in": "path", "name": "id"}},
        "responsePath": "/version/number",
        "target": {"in": "body", "path": "/version/number"},
        "increment": 1,
        "overrides": [{"when": {"path": "/status", "equals": "draft"}, "value": 1}],
    }
    body = {
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "spaceId": {"type": "string"},
                        "version": {
                            "type": "object",
                            "properties": {"number": {"type": "integer"}},
                        },
                    },
                }
            }
        }
    }
    doc = {
        "openapi": "3.0.3",
        "paths": {
            "/spaces": {
                "get": {
                    "operationId": "getSpaces",
                    "parameters": [
                        {
                            "name": "keys",
                            "in": "query",
                            "schema": {"type": "array", "items": {"type": "string"}},
                        },
                        param("cursor"),
                    ],
                    "x-as-paging": paging,
                }
            },
            "/pages": {
                "post": {
                    "operationId": "createPage",
                    "requestBody": body,
                    "parameters": [param("spaceId", "integer", required=True)]
                    if parameter_target
                    else [],
                    "x-as-prerequisites": prereq,
                }
            },
            "/pages/{id}": {
                "get": {
                    "operationId": "getPage",
                    "parameters": [param("id", location="path", required=True)],
                },
                "put": {
                    "operationId": "updatePage",
                    "parameters": [param("id", location="path", required=True)],
                    "requestBody": body,
                    "x-as-version": version,
                },
            },
        },
    }
    (tmp_path / "index.json").write_text(json.dumps(compile_document(doc)))
    (tmp_path / "catalog.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "documents": [{"id": "primary", "tier": "primary", "file": "index.json"}],
            }
        )
    )
    indexes = ProductIndexes(tmp_path)

    class RecordingResponder(Responder):
        closed = 0

        def close(self):
            self.closed += 1

    responder = RecordingResponder(indexes.get("primary"))
    return Surface(indexes, lambda *_: responder), responder


def test_lookup_pages_exactly_and_preserves_input(tmp_path):
    surface, responder = setup(tmp_path)
    responder.seed(
        "getSpaces",
        [
            {"results": [{"key": "OTHER", "id": "1"}], "cursor": "two"},
            {"results": [{"key": "DOCS", "id": "55"}]},
        ],
    )
    body = {"title": "T"}
    warnings = []
    surface.call("create-page", {}, body, aliases={"space-key": "DOCS"}, warn=warnings.append)
    assert responder.requests == [
        ("getSpaces", {"keys": ["DOCS"]}, None),
        ("getSpaces", {"keys": ["DOCS"], "cursor": "two"}, None),
        ("createPage", {}, {"title": "T", "spaceId": "55"}),
    ]
    assert body == {"title": "T"} and warnings == [] and responder.closed == 1


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"key": "OTHER", "id": "5"}],
        [{"key": "DOCS", "id": "1"}, {"key": "DOCS", "id": "2"}],
        [{"key": "DOCS"}],
        [{"key": "DOCS", "id": None}],
    ],
)
def test_lookup_no_guess_or_write(tmp_path, rows):
    surface, responder = setup(tmp_path)
    responder.seed("getSpaces", [{"results": rows}])
    with pytest.raises(SurfaceError):
        surface.call("createPage", {}, {}, aliases={"space-key": "DOCS"})
    assert [r[0] for r in responder.requests] == ["getSpaces"] and responder.closed == 1


def test_duplicate_match_on_later_page(tmp_path):
    surface, responder = setup(tmp_path)
    responder.seed(
        "getSpaces",
        [
            {"results": [{"key": "DOCS", "id": "1"}], "cursor": "next"},
            {"results": [{"key": "DOCS", "id": "2"}]},
        ],
    )
    with pytest.raises(SurfaceError, match="exactly one"):
        surface.call("createPage", {}, aliases={"space-key": "DOCS"})
    assert len(responder.requests) == 2


def test_parameter_target_coercion_and_conflict(tmp_path):
    surface, responder = setup(tmp_path, parameter_target=True)
    responder.seed("getSpaces", [{"results": [{"key": "DOCS", "id": "55"}]}])
    surface.call("createPage", {}, aliases={"space-key": "DOCS"})
    assert responder.requests[-1] == ("createPage", {"spaceId": 55}, None)
    responder.requests.clear()
    with pytest.raises(SurfaceError, match="conflicting"):
        surface.call("createPage", {"spaceId": "55"}, aliases={"space-key": "DOCS"})
    assert responder.requests == []


@pytest.mark.parametrize("aliases", [{"unknown": "x"}, {"space-key": ""}])
def test_bad_alias_never_sends(tmp_path, aliases):
    surface, responder = setup(tmp_path)
    with pytest.raises(SurfaceError):
        surface.call("createPage", {}, aliases=aliases)
    assert responder.requests == []


@pytest.mark.parametrize(
    "body,version", [({"version": {"number": 9}}, 9), ({"status": "draft"}, 1)]
)
def test_explicit_and_draft_version_no_read(tmp_path, body, version):
    surface, responder = setup(tmp_path)
    surface.call("updatePage", {"id": "1"}, body)
    assert len(responder.requests) == 1 and responder.requests[0][2]["version"]["number"] == version


@pytest.mark.parametrize("current", ["7", True, None, {}, 7.5])
def test_bad_version_never_writes(tmp_path, current):
    surface, responder = setup(tmp_path)
    responder.seed("getPage", [{"version": {"number": current}}])
    with pytest.raises(SurfaceError, match="integers"):
        surface.call("updatePage", {"id": "1"}, {"title": "New"})
    assert [r[0] for r in responder.requests] == ["getPage"]


def test_read_error_and_write_conflict(tmp_path):
    surface, responder = setup(tmp_path)
    responder.seed("getPage", [Response(404, {"message": "missing"})])
    with pytest.raises(SurfaceError) as error:
        surface.call("updatePage", {"id": "1"}, {})
    assert error.value.code == 5 and len(responder.requests) == 1
    responder.requests.clear()
    responder.seed("getPage", [{"version": {"number": 7}}])
    responder.seed("updatePage", [Response(409, {"message": "conflict"})])
    with pytest.raises(SurfaceError) as error:
        surface.call("updatePage", {"id": "1"}, {})
    assert error.value.code == 7 and error.value.status == 409
    assert responder.requests == [
        ("getPage", {"id": "1"}, None),
        ("updatePage", {"id": "1"}, {"version": {"number": 8}}),
    ]


def test_http_conflict_mapping_and_no_retry(tmp_path, monkeypatch):
    import requests

    surface, _ = setup(tmp_path)
    sent = []

    def request(*args, **kwargs):
        sent.append((args, kwargs))
        response = requests.Response()
        response.status_code = 409
        response._content = b'{"message":"changed"}'
        return response

    transport = HTTPTransport("https://offline.invalid", max_retries=5)
    monkeypatch.setattr(transport.session, "request", request)
    surface.transport_factory = lambda *_: transport
    with pytest.raises(SurfaceError) as error:
        surface.call("updatePage", {"id": "1"}, {"version": {"number": 2}})
    assert error.value.code == 7 and error.value.messages == ["changed"] and len(sent) == 1


def test_registry_extension_without_surface_changes(tmp_path):
    surface, responder = setup(tmp_path)
    registry = Registry()

    class Plugin(Transform):
        def request(self, context, tag):
            context.body = {"plugin": True}

        def response(self, context, tag, response):
            return Response(response.status, {"converted": response.body})

    registry.register("x-as-prerequisites", Plugin(), order=0)
    surface.registry = registry
    responder.seed("createPage", [{"id": "9"}])
    assert surface.call("createPage", {}).body == {"converted": {"id": "9"}}
    assert responder.requests == [("createPage", {}, {"plugin": True})]


def test_flags_follow_tags(tmp_path):
    surface, _ = setup(tmp_path)
    create = surface.resolve("createPage")[2]
    update = surface.resolve("updatePage")[2]
    params, options = parse_call_flags(create, ["--space-key=DOCS"])
    assert params == {} and options["aliases"] == {"space-key": "DOCS"}
    assert parse_call_flags(update, ["--id", "1", "--version", "8"])[1]["version"] == 8
    for op, argv in [
        (create, ["--version", "8"]),
        (update, ["--version", "x"]),
        (create, ["--space-key", "A", "--space-key", "B"]),
    ]:
        with pytest.raises(ValueError):
            parse_call_flags(op, argv)
