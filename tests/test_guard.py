import pytest

from as_engine.guard import Decision, decide


def test_untagged_operation_is_silent_allow():
    assert decide(None) == Decision(True, None, "")


@pytest.mark.parametrize("location", ["path", "key", "query"])
def test_named_tags_allow_matching_scalar(location):
    assert decide(
        {"in": location, "name": "project"}, allowlist=("ABC",), params={"project": "ABC"}
    ).allowed


def test_empty_and_malformed_allowlists_deny_tagged_operations():
    tag = {"in": "path", "name": "project"}
    assert not decide(tag, params={"project": "ABC"}).allowed
    assert not decide(tag, allowlist="ABC", params={"project": "ABC"}).allowed
    assert not decide(tag, allowlist=(True,), params={"project": "ABC"}).allowed


def test_numeric_identity_accepts_its_string_spelling_but_not_bool():
    tag = {"in": "path", "name": "id"}
    assert decide(tag, allowlist=(12,), params={"id": "12"}).allowed
    assert not decide(tag, allowlist=(12,), params={"id": True}).allowed


def test_arrays_are_limited_to_named_tags_and_every_entry_must_match():
    tag = {"in": "path", "name": "project"}
    assert decide(tag, allowlist=("A", "B"), params={"project": ["A", "B"]}).allowed
    assert not decide(tag, allowlist=("A",), params={"project": ["A", "B"]}).allowed
    assert not decide(tag, allowlist=("A",), params={"project": []}).allowed


def test_issue_key_derives_project_only_with_well_formed_numeric_suffix():
    tag = {"in": "key", "name": "issue", "separator": "-"}
    assert decide(tag, allowlist=("ABC",), params={"issue": "ABC-123"}) == Decision(True, "ABC", "")
    assert not decide(tag, allowlist=("ABC",), params={"issue": "ABC-nope"}).allowed
    assert not decide(
        {"in": "key", "name": "issue"}, allowlist=("ABC",), params={"issue": "ABC-123"}
    ).allowed


@pytest.mark.parametrize("query", ["project = ABC", "PrOjEcT IN ('ABC', XYZ)"])
def test_query_clause_is_whole_expression_and_restrictive(query):
    tag = {"in": "query", "name": "jql", "clause": "PROJECT"}
    assert decide(tag, allowlist=("ABC", "XYZ"), params={"jql": query}).allowed
    assert not decide(
        tag, allowlist=("ABC",), params={"jql": "project = ABC OR project = XYZ"}
    ).allowed
    assert not decide(tag, allowlist=("ABC",), params={"jql": "other = ABC"}).allowed


def test_site_requires_explicit_true():
    assert not decide({"in": "site"}, allow_site=1).allowed
    assert decide({"in": "site"}, allow_site=True).allowed


def test_body_uses_pointer_and_command_identity():
    tag = {"in": "body", "path": "/project/id"}
    assert decide(tag, allowlist=("7",), argv_identity="7", body={"project": {"id": 7}}).allowed
    assert not decide(tag, allowlist=("8",), argv_identity="8", body={"project": {"id": 7}}).allowed
    assert not decide(tag, allowlist=(7,), argv_identity="7", body={"project": {}}).allowed


def test_body_rejects_bad_json_pointer_escapes_and_honors_resolved_identity():
    assert not decide(
        {"in": "body", "path": "/bad~x"}, allowlist=("A",), argv_identity="A", body={}
    ).allowed
    tag = {"in": "body", "path": "/project", "alias": "project-key", "resolve": [{"from": "key"}]}
    assert not decide(tag, allowlist=("DOCS",), argv_identity="DOCS", body={"project": 55}).allowed
    assert decide(
        tag, allowlist=("DOCS",), argv_identity="DOCS", body={"project": 55}, resolved_identity=55
    ).allowed
    assert not decide(
        tag, allowlist=("DOCS",), argv_identity="ENG", body={"project": 55}, resolved_identity=55
    ).allowed
    assert not decide(
        tag, allowlist=("DOCS",), argv_identity=55, body={"project": 55}, resolved_identity=55
    ).allowed


def test_resolver_metadata_must_be_valid_and_provided():
    tag = {"in": "path", "name": "key", "resolve": [{"lookup": "id"}]}
    assert not decide(tag, allowlist=(7,), params={"key": "ABC"}).allowed
    assert decide(tag, allowlist=(7,), params={"key": "ABC"}, resolved_identity=7) == Decision(
        True, "ABC", ""
    )
    assert not decide(
        {"in": "path", "name": "key", "resolve": []}, allowlist=("ABC",), params={"key": "ABC"}
    ).allowed
    assert not decide(
        {"in": "path", "name": "key", "resolve": ["bad"]}, allowlist=("ABC",), params={"key": "ABC"}
    ).allowed


def test_resolved_arrays_must_all_be_allowed_while_decision_keeps_raw_identity():
    tag = {"in": "query", "name": "projects", "resolve": [{"lookup": "id"}]}
    decision = decide(
        tag, allowlist=(1, 2), params={"projects": ["a", "b"]}, resolved_identity=[1, "2"]
    )
    assert decision == Decision(True, ["a", "b"], "")
    assert not decide(
        tag, allowlist=(1,), params={"projects": ["a", "b"]}, resolved_identity=[1, 2]
    ).allowed


@pytest.mark.parametrize(
    "tag",
    [
        [],
        {},
        {"in": "path", "name": ""},
        {"in": "body", "path": "project"},
        {"in": "body", "path": "/"},
        {"in": "body", "path": "/p", "alias": ""},
        {"in": "query", "name": "p", "clause": "bad clause"},
        {"in": "path", "name": "p", "separator": "-"},
        {"in": [], "name": "p"},
        {"in": "site", "resolve": [{"lookup": "id"}]},
        {"in": "path", "name": "p", "resolve": None},
    ],
)
def test_malformed_tags_deny(tag):
    assert not decide(tag, allowlist=("ABC",), params={"p": "ABC"}, body={}).allowed
