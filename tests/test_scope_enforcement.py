"""A consumer opt-out skips x-as-scope; serve mode enforces regardless of it."""

import json

import pytest
from fake_sidecar import logs, raw_call, request

from as_engine.compiler import compile_document
from as_engine.errors import SurfaceError
from as_engine.index import ProductIndexes
from as_engine.responder import Responder
from as_engine.serve import encode_frame, fake_sidecar, handle_request
from as_engine.surface import Surface
from as_engine.transport import Response
from tests.test_transforms_scope import setup as scope_setup

JQL = {
    "in": "query",
    "name": "jql",
    "clause": "project",
    "conjunction": True,
    "order_by": ["updated"],
}
# The enforcing grammar accepts these ...
PROVABLE = [
    "project = TTRP",
    "project = TTRP ORDER BY updated DESC",
    "project = TTRP AND statusCategory = Done",
]
# ... and refuses these even with no allowlist configured.
UNPROVABLE = ["project = TTRP OR project = CSRE", "assignee = currentUser()"]


class Wire:
    def __init__(self):
        self.calls = []

    def call(self, operation, parameters, body):
        self.calls.append((operation.operationId, dict(parameters)))
        return Response(200, {})

    def close(self):
        pass


def _operation(operation_id, path, scope, name=None, location="query"):
    operation = {
        "operationId": operation_id,
        "responses": {"200": {"description": "ok"}},
        "x-as-scope": scope,
    }
    if name:
        operation["parameters"] = [
            {"name": name, "in": location, "required": True, "schema": {"type": "string"}}
        ]
    return {path: {"get": operation}}


@pytest.fixture
def product(tmp_path):
    paths = {
        **_operation("search", "/search", JQL, "jql"),
        **_operation(
            "issue", "/issue/{key}", {"in": "key", "name": "key", "separator": "-"}, "key", "path"
        ),
        **_operation("site", "/site", {"in": "site"}),
    }
    compiled = compile_document({"openapi": "3.0.3", "paths": paths})
    (tmp_path / "jira.json").write_text(json.dumps(compiled))
    (tmp_path / "catalog.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "documents": [{"id": "jira", "tier": "primary", "file": "jira.json"}],
            }
        )
    )
    return ProductIndexes(tmp_path)


def surface_for(product, **kwargs):
    wire = Wire()
    return Surface(product, lambda *_: wire, **kwargs), wire


def refused(call):
    with pytest.raises(SurfaceError) as caught:
        call()
    assert caught.value.code == 4
    return caught.value


def test_default_enforces_the_conjunction_grammar_without_an_allowlist(product):
    surface, wire = surface_for(product, scope_allowlist=None)
    assert surface.scope_enforcement == "enforcing"
    for jql in UNPROVABLE:
        error = refused(lambda jql=jql: surface.call("search", {"jql": jql}))
        assert "allowlist=null" in str(error)
    assert wire.calls == []
    for jql in PROVABLE:
        assert surface.call("search", {"jql": jql}).status == 200
    assert [params["jql"] for _, params in wire.calls] == PROVABLE


def test_permissive_sends_every_query_and_ignores_allowlist_and_site_gate(product):
    # An empty allowlist denies every scoped call when enforcing; permissive
    # consults neither it nor the site policy.
    surface, wire = surface_for(product, scope_enforcement="permissive")
    for jql in PROVABLE + UNPROVABLE:
        assert surface.call("search", {"jql": jql}).status == 200
    assert surface.call("issue", {"key": "OTHER-1"}).status == 200
    assert surface.call("site", {}).status == 200
    assert wire.calls == [
        *[("search", {"jql": jql}) for jql in PROVABLE + UNPROVABLE],
        ("issue", {"key": "OTHER-1"}),
        ("site", {}),
    ]


def test_per_call_override_wins_in_both_directions_and_does_not_leak(product):
    surface, wire = surface_for(product, scope_allowlist=None, scope_enforcement="permissive")
    refused(lambda: surface.call("search", {"jql": UNPROVABLE[0]}, scope_enforcement="enforcing"))
    assert wire.calls == []
    assert surface.call("search", {"jql": UNPROVABLE[0]}).status == 200
    surface.scope_enforcement = "enforcing"
    assert surface.call("site", {}, scope_enforcement="permissive").status == 200
    refused(lambda: surface.call("site", {}))
    assert [name for name, _ in wire.calls] == ["search", "site"]


@pytest.mark.parametrize("value", ["Permissive", "off", "", None, True])
def test_unknown_mode_is_rejected_at_construction_and_assignment(product, value):
    with pytest.raises(ValueError, match="enforcing or permissive"):
        Surface(product, lambda *_: Wire(), scope_enforcement=value)
    surface, _ = surface_for(product)
    with pytest.raises(ValueError, match="enforcing or permissive"):
        surface.scope_enforcement = value
    assert surface.scope_enforcement == "enforcing"


@pytest.mark.parametrize("value", ["Permissive", "off", "", True])
def test_unknown_per_call_mode_is_a_usage_error_before_send(product, value):
    surface, wire = surface_for(product, scope_enforcement="permissive")
    with pytest.raises(SurfaceError) as caught:
        surface.call("search", {"jql": UNPROVABLE[0]}, scope_enforcement=value)
    assert caught.value.code == 2
    assert caught.value.messages == ["scope_enforcement must be enforcing or permissive"]
    assert wire.calls == []


def test_permissive_performs_no_resolution_reads(tmp_path):
    surface, responder, _ = scope_setup(tmp_path, allowlist=("DOCS",))
    surface.scope_enforcement = "permissive"
    surface.call("assetWrite", {"resource": "R1"}, {"untrusted": "payload"})
    assert responder.requests == [("assetWrite", {"resource": "R1"}, {"untrusted": "payload"})]
    assert responder.roles == [False]


@pytest.mark.parametrize("change", [{}, {"document": "platform"}])
def test_request_handler_enforces_a_permissive_surface(indexes, change):
    surface = Surface(indexes, lambda _, index: Responder(index), scope_enforcement="permissive")
    for name, parameters in [("getIssue", {"issueIdOrKey": "OTHER-1"}), ("site", {})]:
        frame = encode_frame(request(name, parameters=parameters, **change), 8192)
        result, record = handle_request(surface, frame, allowlist=["SBX"])
        assert result["error"]["kind"] == "scope"
        assert record["outcome"] == "refused scope"


def test_serve_forces_enforcement_on_a_permissive_factory(indexes):
    served = []

    def factory():
        surface = Surface(
            indexes, lambda _, index: Responder(index), scope_enforcement="permissive"
        )
        served.append(surface)
        return surface

    try:
        with fake_sidecar(surface_factory=factory, allowlist=["SBX"]) as server:
            assert raw_call(server, request())["status"] == 200
            forged = request(parameters={"issueIdOrKey": "OTHER-1"})
            assert raw_call(server, forged)["error"]["kind"] == "scope"
            assert raw_call(server, request("site", parameters={}))["error"]["kind"] == "scope"
            assert [row["outcome"] for row in logs(server)] == [
                "ok 200",
                "refused scope",
                "refused scope",
            ]
    except PermissionError as exc:
        if exc.errno != 1:
            raise
        pytest.skip("NOT RUN: sandbox socket bind denied: errno=1 Operation not permitted")
    assert [surface.scope_enforcement for surface in served] == ["enforcing"]
