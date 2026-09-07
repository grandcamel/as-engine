"""Additive Jira identity policies retain the original generic defaults."""

import json

import pytest

from as_engine.compiler import compile_document
from as_engine.errors import SurfaceError
from as_engine.guard import decide
from as_engine.index import ProductIndexes
from as_engine.surface import Surface
from as_engine.transport import Response

KEY = {"in": "key", "name": "issue", "separator": "-"}
JQL = {"in": "query", "name": "jql", "clause": "project", "conjunction": True}
BODY = {"in": "body", "paths": ["/fields/project/key", "/fields/project/id"]}
CHECK = {**KEY, "checks": [{**BODY, "optional": True}]}


@pytest.mark.parametrize(
    "query",
    [
        "project = SBX AND status = Open",
        "status = Open and PROJECT in ('SBX', \"SBX\")",
        "project=SBX AND summary ~ 'OR NOT filter = 7'",
        "project=SBX AND priority IN (High, Low) AND assignee IS EMPTY",
        "project=SBX AND created >= '2026-01-01'",
    ],
)
def test_opt_in_and_literal_jql(query):
    assert decide(JQL, allowlist=("SBX",), params={"jql": query}).allowed
    strict = {key: value for key, value in JQL.items() if key != "conjunction"}
    assert not decide(strict, allowlist=("SBX",), params={"jql": query}).allowed


@pytest.mark.parametrize(
    "query",
    [
        "status=Open",
        "project=SBX OR status=Open",
        "project=SBX AND NOT status=Closed",
        "project=SBX AND assignee=currentUser()",
        "project=SBX AND filter=7",
        "project=SBX AND",
        "project=SBX trailing",
        "project=SBX;status=Open",
        "project in (SBX,GC) AND status=Open",
        "project=GC AND status=Open",
        "project != SBX",
        "project=SBX AND (status=Open)",
        "project=SBX AND status IN ()",
        "project=SBX AND status IN (Open,)",
        "project=SBX AND status='unterminated",
        "project=SBX AND status 'IN' (Open)",
        "project=SBX ORDER BY created",
        "project=SBX AND project IN (GC)",
        "project=SBX AND status=Open OR project=GC",
        "project=SBX AND status=Open /*bad*/",
        "project=SBX AND status IS 'EMPTY'",
    ],
)
def test_conservative_jql_refuses_without_approximating(query):
    assert not decide(JQL, allowlist=("SBX",), params={"jql": query}).allowed


def test_body_jql_requires_visible_matching_project():
    tag = {"in": "body", "path": "/jql", "clause": "project", "conjunction": True}
    body = {"jql": "project=SBX AND status=Open"}
    assert decide(tag, allowlist=("SBX",), argv_identity="SBX", body=body).allowed
    assert not decide(tag, allowlist=("SBX",), body=body).allowed
    assert not decide(tag, allowlist=("SBX", "GC"), argv_identity="GC", body=body).allowed
    assert not decide(tag, allowlist=None, body=body).allowed


@pytest.mark.parametrize("location", ["query", "body"])
def test_every_issue_key_in_array_is_proved(location):
    tag = KEY if location == "query" else {"in": "body", "path": "/issues", "separator": "-"}

    def check(issues):
        kwargs = (
            {"params": {"issue": issues}} if location == "query" else {"body": {"issues": issues}}
        )
        return decide(tag, allowlist=("SBX",), argv_identity="SBX", **kwargs).allowed

    assert check(["SBX-1", "SBX-2"])
    for bad in ([], ["SBX-1", "GC-1"], ["SBX-1", "7"], ["SBX-1", True], ["SBX-bad"]):
        assert not check(bad)


def test_unrestricted_membership_preserves_structure_and_site_gate():
    assert decide(KEY, allowlist=None, params={"issue": "GC-1"}).allowed
    assert not decide(KEY, allowlist=(), params={"issue": "GC-1"}).allowed
    assert not decide(KEY, allowlist=None, params={"issue": "123"}).allowed
    assert not decide({"in": "site"}, allowlist=None).allowed
    assert decide({"in": "site"}, allowlist=None, allow_site=True).allowed
    assert not decide(
        {"in": "site"}, allowlist=("SBX",), allow_site=True, argv_identity="GC"
    ).allowed
    assert not decide({"in": "unknown"}, allowlist=None).allowed


@pytest.mark.parametrize("location", ["path", "key", "query"])
def test_named_identity_must_agree_with_optional_command_flag(location):
    tag = KEY if location == "key" else {"in": location, "name": "issue"}
    value = "SBX-1" if location == "key" else "SBX"
    assert decide(tag, allowlist=("SBX",), argv_identity="SBX", params={"issue": value}).allowed
    assert not decide(tag, allowlist=None, argv_identity="GC", params={"issue": value}).allowed


@pytest.mark.parametrize("project", [{"key": "SBX"}, {"id": "SBX"}, {"key": "SBX", "id": "SBX"}])
def test_body_alternatives_require_all_present_values_to_agree(project):
    assert decide(
        BODY, allowlist=("SBX",), argv_identity="SBX", body={"fields": {"project": project}}
    ).allowed


@pytest.mark.parametrize(
    "project",
    [
        None,
        {},
        [],
        "SBX",
        {"key": None},
        {"key": ""},
        {"key": "SBX", "id": 12},
        {"key": "SBX", "id": None},
    ],
)
def test_present_malformed_body_alternatives_fail_closed(project):
    assert not decide(
        BODY, allowlist=None, argv_identity="SBX", body={"fields": {"project": project}}
    ).allowed
    assert not decide(
        CHECK, allowlist=None, params={"issue": "SBX-1"}, body={"fields": {"project": project}}
    ).allowed


def test_optional_secondary_project_check_uses_primary_identity():
    assert decide(
        CHECK, allowlist=("SBX",), params={"issue": "SBX-1"}, body={"fields": {"summary": "x"}}
    ).allowed
    assert decide(
        CHECK,
        allowlist=("SBX",),
        params={"issue": "SBX-1"},
        body={"fields": {"project": {"key": "SBX"}}},
    ).allowed
    assert not decide(
        CHECK,
        allowlist=None,
        params={"issue": "SBX-1"},
        body={"fields": {"project": {"key": "GC"}}},
    ).allowed


@pytest.mark.parametrize(
    "check",
    [
        None,
        {},
        "bad",
        [{"in": "site", "paths": ["/p"], "optional": True}],
        [{**BODY, "optional": True, "checks": []}],
        [{**BODY, "optional": True, "resolve": []}],
        [{**BODY, "optional": "true"}],
        [{"in": "body", "paths": [], "optional": True}],
        [{"in": "body", "paths": ["/bad~x"], "optional": True}],
    ],
)
def test_malformed_secondary_metadata_never_silently_skips(check):
    assert not decide(
        {**KEY, "checks": check}, allowlist=("SBX",), params={"issue": "SBX-1"}, body={}
    ).allowed


def test_surface_policy_none_and_secondary_refusal_precede_transport(tmp_path):
    compiled = compile_document(
        {
            "openapi": "3.0.3",
            "paths": {
                "/issues/{issue}": {
                    "post": {
                        "operationId": "edit",
                        "x-as-scope": CHECK,
                        "parameters": [
                            {
                                "name": "issue",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        ],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }
    )
    (tmp_path / "jira.json").write_text(json.dumps(compiled))
    (tmp_path / "catalog.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "documents": [{"id": "jira", "tier": "primary", "file": "jira.json"}],
            }
        )
    )
    calls = []

    class Wire:
        def call(self, operation, parameters, body):
            calls.append((parameters, body))
            return Response(200, {})

        def close(self):
            pass

    surface = Surface(ProductIndexes(tmp_path), lambda *_: Wire(), scope_allowlist=None)
    assert surface.call("edit", {"issue": "GC-1"}).status == 200
    with pytest.raises(SurfaceError) as caught:
        surface.call("edit", {"issue": "GC-1"}, {"fields": {"project": {"key": "SBX"}}})
    assert caught.value.code == 4 and "allowlist=null" in str(caught.value)
    assert len(calls) == 1
    with pytest.raises(SurfaceError):
        surface.call("edit", {"issue": "GC-1"}, scope_allowlist=())
    assert surface.call("edit", {"issue": "GC-1"}).status == 200
