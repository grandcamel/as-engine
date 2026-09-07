"""The compatibility backlog may order an already bounded literal query."""

import pytest

from as_engine.guard import decide

BASE = {"in": "query", "name": "jql", "clause": "project", "conjunction": True}
ORDERED = {**BASE, "order_by": ["key", "created", "updated"]}


@pytest.mark.parametrize("suffix", ["key", "key ASC", "created DESC", "updated asc", "KEY desc"])
@pytest.mark.parametrize("words", ["ORDER BY", "order by", "OrDeR bY"])
def test_ordering_is_opt_in_after_proved_filter(suffix, words):
    query = f"project = SBX AND statusCategory != Done {words} {suffix}"
    assert decide(ORDERED, allowlist=("SBX",), params={"jql": query}).allowed
    assert not decide(BASE, allowlist=("SBX",), params={"jql": query}).allowed
    assert not decide(ORDERED, allowlist=("OTHER",), params={"jql": query}).allowed


@pytest.mark.parametrize(
    "query",
    [
        "ORDER BY key",
        "status = Open ORDER BY key",
        "project=SBX OR status=Open ORDER BY key",
        "project=SBX AND NOT status=Done ORDER BY key",
        "project=SBX AND assignee=currentUser() ORDER BY key",
        "project=SBX AND filter=7 ORDER BY key",
        "project=SBX ORDER BY summary",
        "project=SBX ORDER BY key,created",
        "project=SBX ORDER BY key ASC, created DESC",
        "project=SBX ORDER BY key ASC OR project=OTHER",
        "project=SBX ORDER BY key AND status=Open",
        "project=SBX ORDER BY key ORDER BY updated",
        "project=SBX ORDER BY key sideways",
        "project=SBX ORDER BY key()",
        "project=SBX ORDER BY 'key'",
        "project=SBX ORDER 'BY' key",
        "project=SBX ORDER BY key 'ASC'",
        "project=SBX ORDER",
        "project=SBX ORDER BY",
        "project=SBX AND ORDER BY key",
        "project=OTHER ORDER BY key",
        "project=SBX ORDER BY key /* ignored? */",
    ],
)
def test_ordering_refuses_unproved_or_trailing_syntax(query):
    assert not decide(ORDERED, allowlist=("SBX",), params={"jql": query}).allowed


@pytest.mark.parametrize("metadata", [None, [], "key", [1], [""], ["key", "KEY"], ["key()"]])
def test_ordering_metadata_is_validated_even_without_order(metadata):
    assert not decide(
        {**BASE, "order_by": metadata}, allowlist=("SBX",), params={"jql": "project=SBX"}
    ).allowed


def test_quoted_ordering_words_are_literals_and_body_keeps_visible_identity():
    query = "project=SBX AND summary ~ 'ORDER BY key OR NOT filter' order by key"
    assert decide(ORDERED, allowlist=("SBX",), params={"jql": query}).allowed
    body_tag = {
        "in": "body",
        "path": "/jql",
        "clause": "project",
        "conjunction": True,
        "order_by": ["key"],
    }
    assert decide(body_tag, allowlist=("SBX",), argv_identity="SBX", body={"jql": query}).allowed
    assert not decide(body_tag, allowlist=("SBX",), body={"jql": query}).allowed
    assert not decide(
        {**BASE, "conjunction": False, "order_by": ["key"]},
        allowlist=("SBX",),
        params={"jql": query},
    ).allowed
    assert not decide(
        {"in": "key", "name": "key", "order_by": ["key"]}, allowlist=("SBX",), params={"key": "SBX"}
    ).allowed


def test_multiple_body_keys_are_individually_allowed():
    tag = {"in": "body", "key_paths": ["/inwardIssue/key", "/outwardIssue/key"], "separator": "-"}
    body = {"inwardIssue": {"key": "SBX-1"}, "outwardIssue": {"key": "OTHER-2"}}
    assert decide(tag, allowlist=("SBX", "OTHER"), argv_identity="SBX", body=body).allowed
    assert not decide(tag, allowlist=("SBX",), argv_identity="SBX", body=body).allowed
    assert not decide(tag, allowlist=("SBX", "OTHER"), body=body).allowed
    assert not decide(tag, allowlist=None, argv_identity="THIRD", body=body).allowed
    for broken in [None, {}, {"key": "1"}, {"key": ["SBX-1"]}, {"key": "SBX"}]:
        assert not decide(
            tag, allowlist=None, argv_identity="SBX", body={**body, "outwardIssue": broken}
        ).allowed
    for metadata in [None, [], ["/inwardIssue/key", "/inwardIssue/key"], [1], ["not-a-pointer"]]:
        assert not decide(
            {**tag, "key_paths": metadata}, allowlist=None, argv_identity="SBX", body=body
        ).allowed
    assert not decide(
        {**tag, "path": "/inwardIssue/key"}, allowlist=None, argv_identity="SBX", body=body
    ).allowed
