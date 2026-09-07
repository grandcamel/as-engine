"""Product-independent progressive help documents and deterministic rendering."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .index import Operation, OperationIndex

TOPICS = (
    "adf",
    "paging",
    "search",
    "fields",
    "project-types",
    "permissions",
    "rate-limits",
    "representations",
    "sandbox",
    "auth",
    "scope",
    "risk",
    "errors",
)
CAPS = {"level0": 400, "group": 800, "topic": 800, "level2": 1200, "level3": 600, "topics": 800}


def token_estimate(text: str) -> int:
    """Conservative, deterministic budget proxy: ceil(characters / 4)."""
    return (len(text) + 3) // 4


def first_paragraph(text: str | None) -> str:
    return re.split(r"\n\s*\n", (text or "").strip(), maxsplit=1)[0]


def document(level: int, title: str, sections: list[dict[str, Any]]) -> dict[str, Any]:
    return {"level": level, "title": title, "sections": sections}


def render_help(value: Mapping[str, Any], format: str = "markdown") -> str:
    """Both formats consume the very same document; JSON retains its structure."""
    if format == "json":
        return json.dumps(value, ensure_ascii=False, indent=2)
    if format != "markdown":
        raise ValueError("help format must be markdown or json")
    lines = ["# " + value["title"]]
    for section in value["sections"]:
        lines.append("")
        if section.get("title"):
            lines.extend(["## " + section["title"], ""])
        if section.get("text"):
            lines.append(section["text"])
        lines.extend("- " + item for item in section.get("items", []))
        for example in section.get("examples", []):
            language = "json" if example["kind"] == "json" else "sh"
            lines.extend(["", "```" + language, example["value"], "```"])
    return "\n".join(lines).strip()


def level0(template: str) -> dict[str, Any]:
    """The product owns the short surface map, including its title."""
    title, _, text = template.strip().partition("\n")
    return document(0, title.removeprefix("# "), [{"text": text.strip()}])


def topics_document(index: OperationIndex) -> dict[str, Any]:
    names = sorted({topic for op in index.operations.values() for topic in operation_topics(op)})
    return document(1, "Topics", [{"items": names, "text": "Use help TOPIC."}])


def operation_topics(operation: Operation) -> list[str]:
    value = operation.extensions.get("x-as-topic", [])
    return [value] if isinstance(value, str) else value if isinstance(value, list) else []


def examples_for(operation: Operation) -> list[dict[str, Any]]:
    value = operation.extensions.get("x-as-examples", [])
    return [
        item
        for item in value
        if isinstance(item, dict)
        and item.get("kind") in {"invocation", "json"}
        and isinstance(item.get("value"), str)
    ]


def examples_document(operation: Operation) -> dict[str, Any]:
    examples = examples_for(operation)
    return document(
        3,
        operation.operationId + " examples",
        [{"examples": examples, "text": "" if examples else "No examples supplied by enrichment."}],
    )


def group_document(
    index: OperationIndex,
    group: str,
    groups: Mapping[str, Sequence[tuple[str, str]]],
    *,
    offset: int = 0,
) -> dict[str, Any]:
    """List product verbs or API-tag operations, with an explicit continuation."""
    if group in groups:
        items = [f"`{name}` — {first_paragraph(summary)}" for name, summary in groups[group]]
    else:
        items = [
            f"`{op.operationId}` — {first_paragraph(op.summary)}"
            for op in sorted(index.operations.values(), key=lambda op: op.operationId)
            if group.casefold() in [tag.casefold() for tag in op.tags]
        ]
        if not items:
            raise ValueError(f"Unknown group or topic: {group}")
    return _page(1, group, [{"items": [item]} for item in items], "group", offset)


def group_examples_document(
    index: OperationIndex, group: str, *, offset: int = 0
) -> dict[str, Any]:
    sections = [
        {"title": op.operationId, "examples": examples_for(op)}
        for op in sorted(index.operations.values(), key=lambda op: op.operationId)
        if examples_for(op)
        and (group == "api" or group.casefold() in [tag.casefold() for tag in op.tags])
    ]
    return _page(3, group, sections, "level3", offset)


def topic_document(
    index: OperationIndex, topic: str, *, examples: bool = False, offset: int = 0
) -> dict[str, Any]:
    operations = [
        op
        for op in sorted(index.operations.values(), key=lambda op: op.operationId)
        if topic in operation_topics(op)
    ]
    if not operations and topic not in TOPICS:
        raise ValueError(f"Unknown topic: {topic}")
    sections = []
    for op in operations:
        section: dict[str, Any] = {"title": op.operationId, "examples": examples_for(op)}
        if not examples:
            section["text"] = str(op.extensions.get("x-as-note", op.summary or ""))
        sections.append(section)
    return _page(3 if examples else 1, topic, sections, "level3" if examples else "topic", offset)


def _page(
    level: int, title: str, sections: list[dict[str, Any]], kind: str, offset: int
) -> dict[str, Any]:
    if offset < 0 or (offset and offset >= len(sections)):
        raise ValueError("help offset is out of range")
    selected: list[dict[str, Any]] = []
    total = len(sections)
    for section in sections[offset:]:
        candidate = document(level, title, [*selected, section])
        # Reserve room for navigation. Never split or silently omit an entry.
        if selected and token_estimate(render_help(candidate)) > CAPS[kind] - 70:
            break
        selected.append(section)
    next_offset = offset + len(selected)
    if next_offset < total:
        selected.append(
            {
                "text": f"Showing entries {offset + 1}–{next_offset} of {total}. "
                f"Continue: help {title} --offset {next_offset}"
                + (" --examples" if level == 3 else "")
                + "."
            }
        )
    if not selected:
        selected.append({"text": "No entries tagged with this topic."})
    return document(level, title, selected)


def operation_document(
    operation: Operation, index: OperationIndex, *, full: bool = False
) -> dict[str, Any]:
    from .surface import describe_operation

    value = describe_operation(operation, index, full=full)
    return describe_document(value)


def describe_document(value: Mapping[str, Any]) -> dict[str, Any]:
    """Build Level 2 from the public describe record (also retained as metadata)."""
    from .params import kebab_case

    sections: list[dict[str, Any]] = [
        {
            "text": "\n\n".join(
                filter(
                    None,
                    [
                        f"{value['method']} `{value['path']}`",
                        value.get("summary"),
                        value.get("description"),
                    ],
                )
            )
        }
    ]
    parameters = []
    for p in value["parameters"]:
        parameters.append(
            f"`--{kebab_case(p['name'])}` ({p['in']}, {p['type']})"
            + (" (required)" if p.get("required") else "")
            + (f"; enum: {json.dumps(p['enum'])}" if "enum" in p else "")
        )
    sections.append(
        {"title": "Parameters", "items": parameters, "text": "" if parameters else "None."}
    )
    body = []
    for item in value["body"]:
        body.append(
            f"`{item['name']}`: {item['type']}"
            + (" (required)" if item["required"] else "")
            + (f"; enum: {json.dumps(item['enum'])}" if "enum" in item else "")
        )
        for child in item.get("properties", []):
            body.append(
                f"`{item['name']}.{child['name']}`: {child['type']}"
                + (" (required)" if child["required"] else "")
            )
    sections.append(
        {
            "title": "Body" + (" (required)" if value["body_required"] else ""),
            "items": body,
            "text": "" if body else "No top-level body properties.",
        }
    )
    tags = value["extensions"]
    details = ["Risk: " + str(value.get("risk", "safe")) + "."]
    if value.get("risk", "safe") in {"destructive", "irreversible"}:
        details.append("Dry-run by default; --confirm sends the request.")
    for key in ("x-as-note", "x-as-scope", "x-as-paging", "x-as-prerequisites", "x-as-version"):
        if key in tags:
            tag = tags[key]
            if key == "x-as-paging" and isinstance(tag, dict):
                tag = tag.get("style")
            elif key == "x-as-prerequisites" and isinstance(tag, list):
                tag = ", ".join("--" + item["alias"] for item in tag)
            elif key == "x-as-version":
                tag = "--version overrides the current-version lookup."
            details.append(key.removeprefix("x-as-") + ": " + str(tag))
    for key, tag in tags.items():
        if "scope" in key and key != "x-as-scope":
            details.append(key + ": " + json.dumps(tag, ensure_ascii=False))
    if value["deprecated"]:
        details.append("Deprecated. Replacement: " + str(value["replacement"] or "not specified"))
    details.append("Use --full for the complete description; --examples for examples.")
    sections.append({"title": "Behavior", "items": details})
    return {**value, **document(2, value["operationId"], sections)}
