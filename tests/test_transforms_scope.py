"""Public Surface coverage for generic, bounded scope resolution."""

import json

import pytest

from as_engine.compiler import compile_document
from as_engine.errors import SurfaceError
from as_engine.index import ProductIndexes
from as_engine.responder import Responder
from as_engine.surface import Surface
from as_engine.transport import Response


class RecordingResponder(Responder):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.roles = []

    def call(self, operation, parameters, body):
        self.roles.append(bool(operation.extensions.get("x-as-resolution-read")))
        return super().call(operation, parameters, body)


def _parameter(name, location="query", *, array=False):
    schema = {"type": "array", "items": {"type": "string"}} if array else {"type": "string"}
    return {"name": name, "in": location, "required": True, "schema": schema}


def _operation(operation_id, method, path, *, parameters=(), scope=None, body=False):
    operation = {
        "operationId": operation_id,
        "parameters": list(parameters),
        "responses": {"200": {"description": "generic response"}},
    }
    if scope is not None:
        operation["x-as-scope"] = scope
    if body:
        operation["requestBody"] = {
            "content": {
                "application/json": {
                    "schema": {"type": "object", "properties": {"spaceId": {"type": "integer"}}}
                }
            }
        }
    return {path: {method: operation}}


def scope_product(tmp_path, *, body_scope=None, asset_scope=None, query_scope=None):
    body_scope = body_scope or {
        "in": "body",
        "path": "/spaceId",
        "alias": "space-key",
        "resolve": [
            {
                "operationId": "lookupProject",
                "parameter": "key",
                "array": True,
                "resultsPath": "/results",
                "matchPath": "/key",
                "valuePath": "/id",
            }
        ],
    }
    asset_scope = asset_scope or {
        "in": "path",
        "name": "resource",
        "resolve": [
            {
                "operationId": "readAsset",
                "parameter": "resource",
                "matchPath": "/id",
                "valuePath": "/spaceId",
            },
            {
                "operationId": "readSpace",
                "parameter": "id",
                "matchPath": "/id",
                "valuePath": "/key",
            },
        ],
    }
    query_scope = query_scope or {
        "in": "query",
        "name": "keys",
        "resolve": [
            {
                "operationId": "lookupProject",
                "parameter": "key",
                "array": True,
                "resultsPath": "/results",
                "matchPath": "/key",
                "valuePath": "/id",
            }
        ],
    }
    paths = {}
    for entry in (
        _operation("plainWrite", "post", "/plain"),
        _operation("siteRead", "get", "/site", scope={"in": "site"}),
        _operation(
            "projectWrite",
            "post",
            "/projects/{project}",
            parameters=[_parameter("project", "path")],
            scope={"in": "path", "name": "project"},
        ),
        _operation("bodyWrite", "post", "/body", scope=body_scope, body=True),
        _operation(
            "assetWrite",
            "post",
            "/assets/{resource}",
            parameters=[_parameter("resource", "path")],
            scope=asset_scope,
        ),
        _operation(
            "queryWrite",
            "get",
            "/query",
            parameters=[_parameter("keys", array=True)],
            scope=query_scope,
        ),
        _operation("lookupProject", "get", "/projects", parameters=[_parameter("key", array=True)]),
        _operation(
            "readAsset",
            "get",
            "/asset-metadata/{resource}",
            parameters=[_parameter("resource", "path")],
        ),
        _operation("readSpace", "get", "/spaces/{id}", parameters=[_parameter("id", "path")]),
    ):
        paths.update(entry)
    compiled = compile_document({"openapi": "3.0.3", "paths": paths})
    (tmp_path / "generic.json").write_text(json.dumps(compiled))
    (tmp_path / "catalog.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "documents": [{"id": "generic", "tier": "primary", "file": "generic.json"}],
            }
        )
    )
    return ProductIndexes(tmp_path)


def setup(tmp_path, *, allowlist=(), rules=None, **spec):
    indexes = scope_product(tmp_path, **spec)
    responder = RecordingResponder(indexes.get("generic"))
    factories = []

    def factory(_document, _index):
        factories.append(1)
        return responder

    rules = (
        rules
        if rules is not None
        else {
            "generic:lookupProject": (("key",),),
            "generic:readAsset": (("resource",),),
            "generic:readSpace": (("id",),),
        }
    )
    return (
        Surface(indexes, factory, scope_allowlist=allowlist, scope_resolution_rules=rules),
        responder,
        factories,
    )


def refusal(call):
    with pytest.raises(SurfaceError) as caught:
        call()
    assert caught.value.code == 4


def test_untagged_and_site_scope_are_generic_and_explicit(tmp_path):
    surface, responder, _ = setup(tmp_path)
    surface.call("plainWrite", {})
    refusal(lambda: surface.call("siteRead", {}))
    surface.call("siteRead", {}, scope_allow_site=True)
    assert [request[0] for request in responder.requests] == ["plainWrite", "siteRead"]
    assert responder.roles == [False, False]


def test_path_refusal_precedes_transport_and_per_call_scope_does_not_leak(tmp_path):
    surface, responder, factories = setup(tmp_path)
    refusal(lambda: surface.call("projectWrite", {"project": "DOCS"}))
    assert factories == [] and responder.requests == []
    surface.call("projectWrite", {"project": "DOCS"}, scope_allowlist=("DOCS",))
    refusal(lambda: surface.call("projectWrite", {"project": "DOCS"}))
    assert [request[0] for request in responder.requests] == ["projectWrite"]


def test_body_scope_requires_allowed_argv_before_lookup_and_binds_resolved_id(tmp_path):
    surface, responder, factories = setup(tmp_path, allowlist=("DOCS",))
    refusal(lambda: surface.call("bodyWrite", {}, {}, scope_argv_identity="DOCS"))
    refusal(lambda: surface.call("bodyWrite", {}, {"spaceId": 55}, scope_argv_identity="ENG"))
    assert factories == [] and responder.requests == []
    responder.seed("lookupProject", [{"results": [{"key": "DOCS", "id": 55}]}])
    surface.call("bodyWrite", {}, {"spaceId": 55}, scope_argv_identity="DOCS")
    assert responder.requests == [
        ("lookupProject", {"key": ["DOCS"]}, None),
        ("bodyWrite", {}, {"spaceId": 55}),
    ]
    assert responder.roles == [True, False]


def test_body_mismatch_reads_once_but_never_mutates(tmp_path):
    surface, responder, _ = setup(tmp_path, allowlist=("DOCS",))
    responder.seed("lookupProject", [{"results": [{"key": "DOCS", "id": 55}]}])
    refusal(lambda: surface.call("bodyWrite", {}, {"spaceId": 77}, scope_argv_identity="DOCS"))
    assert [request[0] for request in responder.requests] == ["lookupProject"]
    assert responder.roles == [True]


def test_two_step_resource_scope_uses_metadata_only_reads_then_write(tmp_path):
    surface, responder, _ = setup(tmp_path, allowlist=("DOCS",))
    responder.seed("readAsset", [{"id": "R1", "spaceId": "55"}])
    responder.seed("readSpace", [{"id": "55", "key": "DOCS"}])
    surface.call("assetWrite", {"resource": "R1"}, {"untrusted": "payload"})
    assert responder.requests == [
        ("readAsset", {"resource": "R1"}, None),
        ("readSpace", {"id": "55"}, None),
        ("assetWrite", {"resource": "R1"}, {"untrusted": "payload"}),
    ]
    assert responder.roles == [True, True, False]


def test_two_step_resource_refusal_after_reads_never_writes(tmp_path):
    surface, responder, _ = setup(tmp_path, allowlist=("DOCS",))
    responder.seed("readAsset", [{"id": "R1", "spaceId": "55"}])
    responder.seed("readSpace", [{"id": "55", "key": "ENG"}])
    refusal(lambda: surface.call("assetWrite", {"resource": "R1"}))
    assert [request[0] for request in responder.requests] == ["readAsset", "readSpace"]


@pytest.mark.parametrize(
    "response",
    [
        {"results": []},
        {"results": [{"key": "DOCS", "id": 55}, {"key": "DOCS", "id": 55}]},
        {"results": [{"key": "DOCS", "id": 55}], "cursor": "more"},
        Response(status=500, body={"message": "nope"}),
    ],
)
def test_unprovable_lookup_fails_scope_without_operation(tmp_path, response):
    surface, responder, _ = setup(tmp_path, allowlist=("DOCS",))
    responder.seed("lookupProject", [response])
    refusal(lambda: surface.call("bodyWrite", {}, {"spaceId": 55}, scope_argv_identity="DOCS"))
    assert [request[0] for request in responder.requests] == ["lookupProject"]


def test_resolution_route_is_policy_bound_and_never_self_authorizes(tmp_path):
    surface, responder, factories = setup(tmp_path, allowlist=("DOCS",), rules={})
    refusal(lambda: surface.call("bodyWrite", {}, {"spaceId": 55}, scope_argv_identity="DOCS"))
    assert factories == [] and responder.requests == []
    three_steps = [
        {
            "operationId": "lookupProject",
            "parameter": "key",
            "array": True,
            "resultsPath": "/results",
            "matchPath": "/key",
            "valuePath": "/id",
        }
    ] * 3
    surface, responder, factories = setup(
        tmp_path,
        allowlist=("DOCS",),
        body_scope={"in": "body", "path": "/spaceId", "resolve": three_steps},
    )
    refusal(lambda: surface.call("bodyWrite", {}, {"spaceId": 55}, scope_argv_identity="DOCS"))
    assert factories == [] and responder.requests == []


def test_array_query_uses_one_batched_lookup_and_requires_every_resolved_identity(tmp_path):
    surface, responder, _ = setup(tmp_path, allowlist=("1", "2"))
    responder.seed("lookupProject", [{"results": [{"key": "A", "id": 1}, {"key": "B", "id": 2}]}])
    surface.call("queryWrite", {"keys": ["A", "B"]})
    assert responder.requests == [
        ("lookupProject", {"key": ["A", "B"]}, None),
        ("queryWrite", {"keys": ["A", "B"]}, None),
    ]
    assert responder.roles == [True, False]


def test_resolution_error_does_not_disclose_resource_existence(tmp_path):
    surface, responder, _ = setup(tmp_path, allowlist=("DOCS",))
    responder.seed("readAsset", [Response(404, {"message": "Page not found"})])
    with pytest.raises(SurfaceError) as caught:
        surface.call("assetWrite", {"resource": "R1"})
    diagnostic = json.dumps(caught.value.as_dict())
    assert caught.value.code == 4 and caught.value.status is None
    assert "404" not in diagnostic and "Page not found" not in diagnostic
    assert responder.requests == [("readAsset", {"resource": "R1"}, None)]
    assert responder.roles == [True]
