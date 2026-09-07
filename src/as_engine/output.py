"""Rendering helpers for generic command surfaces."""

from __future__ import annotations

import json
from typing import Any

from assistant_skills_lib.formatters import format_table  # type: ignore[import-untyped]


def render_output(data: Any, format: str = "json", columns: list[str] | None = None) -> str:
    """Render JSON data in JSON, table, or Markdown form."""

    if format == "json":
        return json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if format not in {"table", "markdown"}:
        raise ValueError(f"unsupported output format: {format}")
    rows = _rows(data)
    if not rows:
        return ""
    if format == "table":
        return format_table(_table_rows(rows), columns=columns or _columns(rows))
    if format == "markdown":
        selected = columns or _columns(rows)
        header = "| " + " | ".join(_escape(column) for column in selected) + " |"
        divider = "| " + " | ".join("---" for _ in selected) + " |"
        body = [
            "| " + " | ".join(_escape(_cell(row.get(column))) for column in selected) + " |"
            for row in rows
        ]
        return "\n".join([header, divider, *body])
    raise AssertionError("validated output format was not rendered")


def _rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item if isinstance(item, dict) else {"value": item} for item in data]
    if isinstance(data, dict):
        return [data]
    return []


def _cell(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if value is None:
        return ""
    return str(value)


def _columns(rows: list[dict[str, Any]]) -> list[str]:
    """Keep first-seen key order while retaining fields from every row."""

    return list(dict.fromkeys(key for row in rows for key in row))


def _table_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: _cell(value) for key, value in row.items()} for row in rows]


def _escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", "<br>")
        .replace("\n", "<br>")
        .replace("\r", "<br>")
    )
