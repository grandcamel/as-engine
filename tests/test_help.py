"""Observable help documents: golden output, budgets, filtering and parity."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from as_engine.help import (
    CAPS,
    document,
    examples_document,
    group_document,
    level0,
    operation_document,
    render_help,
    token_estimate,
    topic_document,
    topics_document,
)
from as_engine.index import Operation, OperationIndex


def fixture_index():
    op = Operation(
        "createPage",
        "POST",
        "/pages",
        ["page"],
        "Create a page.",
        "First paragraph.\n\nSecond paragraph is available with --full.",
        [{"name": "spaceId", "in": "query", "type": "integer", "required": True}],
        {
            "schema": {
                "type": "object",
                "required": ["title"],
                "properties": {"title": {"type": "string"}},
            }
        },
        None,
        {
            "x-as-topic": ["adf"],
            "x-as-note": "ADF bodies carry a representation.",
            "x-as-risk": "destructive",
            "x-as-examples": [
                {"kind": "invocation", "value": "tool api call createPage --space-id 1 --confirm"},
                {"kind": "json", "value": '{"title":"Example"}'},
            ],
        },
        [],
    )
    other = replace(op, operationId="notTagged", extensions={"x-as-note": "adf in text alone"})
    return OperationIndex({op.operationId: op, other.operationId: other}, {})


def pages():
    index = fixture_index()
    op = index.operations["createPage"]
    return {
        "level0": level0("# Tool\n\nDiscover operations with api search and api describe."),
        "group": group_document(index, "api", {"api": [("api call", "Call an operation.")]}),
        "topic": topic_document(index, "adf"),
        "level2": operation_document(op, index),
        "level3": examples_document(op),
        "topics": topics_document(index),
    }


@pytest.mark.parametrize("name", list(CAPS))
def test_golden_and_cap(name):
    value = render_help(pages()[name]) + "\n"
    path = Path(__file__).parent / "golden" / "help" / (name + ".md")
    if os.environ.get("UPDATE_HELP_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    assert value == path.read_text()
    assert token_estimate(value) <= CAPS[name]


@pytest.mark.parametrize("name", list(CAPS))
def test_oversize_page_fails_budget(name):
    oversized = render_help(document(0, "Oversize", [{"text": "x" * (4 * CAPS[name])}]))
    with pytest.raises(AssertionError):
        assert token_estimate(oversized) <= CAPS[name]


def test_json_roundtrip_has_identical_rendered_content_for_every_level():
    for value in pages().values():
        parsed = json.loads(render_help(value, "json"))
        assert render_help(parsed) == render_help(value)


def test_topic_filter_never_uses_descriptions_or_base_tags():
    index = fixture_index()
    text = render_help(topic_document(index, "adf"))
    assert "createPage" in text and "notTagged" not in text and "adf in text alone" not in text
    assert "tool api call" in text and '{"title":"Example"}' in text


def test_long_description_only_full_and_risk_visible():
    index = fixture_index()
    op = index.operations["createPage"]
    default = render_help(operation_document(op, index))
    full = render_help(operation_document(op, index, full=True))
    assert "Second paragraph" not in default and "Second paragraph" in full
    assert "Risk: destructive" in default and "--confirm" in default


def test_topic_continuation_is_explicit_complete_and_bounded():
    op = fixture_index().operations["createPage"]
    operations = {str(n): replace(op, operationId=f"operation{n:03d}") for n in range(70)}
    index = OperationIndex(operations, {})
    offset = 0
    visited = []
    while True:
        value = topic_document(index, "adf", offset=offset)
        text = render_help(value)
        assert token_estimate(text) <= CAPS["topic"]
        entries = [s["title"] for s in value["sections"] if s.get("title")]
        visited.extend(entries)
        offset += len(entries)
        if offset == len(operations):
            break
        assert f"--offset {offset}" in text
    assert visited == sorted(op.operationId for op in operations.values())


def test_unknown_subject_and_invalid_format_are_explicit():
    with pytest.raises(ValueError, match="Unknown"):
        topic_document(fixture_index(), "absent")
    with pytest.raises(ValueError, match="Unknown"):
        group_document(fixture_index(), "absent", {})
    with pytest.raises(ValueError, match="format"):
        render_help(pages()["level0"], "xml")
