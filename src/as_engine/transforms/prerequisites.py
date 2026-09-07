"""Resolve declared key aliases through same-document, paged lookups."""

# Invalid tag/data types use the public ValueError usage contract.
# ruff: noqa: TRY004
from __future__ import annotations

from typing import Any

from . import Context, Transform
from .values import MISSING, pointer, required, set_target, target_value


class Prerequisites(Transform):
    def request(self, context: Context, tag: Any) -> None:
        # Validate all conflicts before the first lookup.
        for rule in tag:
            alias = rule["alias"]
            if alias in context.aliases and target_value(context, rule["target"]) is not MISSING:
                raise ValueError(f"conflicting id and --{alias}")
        for rule in tag:
            alias = rule["alias"]
            if alias not in context.aliases:
                continue
            key = context.aliases[alias]
            lookup = context.index.operations.get(rule["operationId"])
            if lookup is None or lookup.method.upper() != "GET":
                raise ValueError("prerequisite lookup must name a same-document GET operation")
            p = rule["parameter"]
            if not any(x["name"] == p["name"] and x["in"] == p["in"] for x in lookup.parameters):
                raise ValueError("prerequisite lookup parameter is not declared")
            paged = "x-as-paging" in lookup.extensions
            response = context.invoke(
                lookup.operationId, {p["name"]: [key] if p.get("array") else key}, all_pages=paged
            )
            rows = (
                response.body
                if paged and isinstance(response.body, list)
                else required(response.body, rule["resultsPath"])
            )
            if not isinstance(rows, list):
                raise ValueError("prerequisite results must be an array")
            matches = [row for row in rows if pointer(row, rule["matchPath"]) == key]
            if len(matches) != 1:
                raise ValueError(f"--{alias}: expected exactly one match, found {len(matches)}")
            value = required(matches[0], rule["valuePath"])
            if value is None:
                raise ValueError("prerequisite lookup returned a null id")
            set_target(context, rule["target"], value)
