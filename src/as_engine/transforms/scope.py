"""Scope decisions before prerequisites, with bounded metadata-only resolution."""

# Malformed metadata follows the public local-refusal contract.
# ruff: noqa: TRY004
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from assistant_skills_lib.error_handler import BaseAPIError  # type: ignore[import-untyped]

from ..errors import ScopeRefusal
from ..guard import decide
from ..params import validate_parameters
from ..transport import Response
from . import Context, Transform
from .values import MISSING, parts, pointer, set_target


def _identity(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value):
        raise ValueError("missing or invalid scope identity")
    return str(value)


def _refuse(context: Context, identity: Any, reason: str) -> ScopeRefusal:
    return ScopeRefusal(
        f"{context.operation.operationId}: scope identity {identity!r}; "
        f"allowlist={json.dumps(None if context.scope_allowlist is None else list(context.scope_allowlist))}: {reason}",
        context.operation.operationId,
    )


def resolution_read(context: Context, operation_id: str, parameters: Mapping[str, Any]) -> Response:
    """The only guard exemption: exact consumer-approved metadata parameters.

    The marked Operation reaches the ordinary transport seam, so recorders may
    distinguish resolution from the requested operation without changing argv.
    No hooks, pagination, content parameter, or caller request body are replayed.
    """
    key = f"{context.document}:{operation_id}"
    allowed = context.scope_resolution_rules.get(key, ())
    operation = context.index.operations.get(operation_id)
    reads = context.state.setdefault("scope.reads", [])
    if (
        operation is None
        or operation.method.upper() != "GET"
        or tuple(sorted(parameters)) not in allowed
        or not parameters
        or context.scope_send is None
        or len(reads) >= 2
    ):
        raise ValueError("scope resolution is not authorized or exceeds two reads")
    checked = validate_parameters(operation, parameters, context.index.schemas)
    marked = replace(operation, extensions={**operation.extensions, "x-as-resolution-read": True})
    reads.append((operation_id, dict(checked)))
    response = context.scope_send(marked, checked, None)
    if not 200 <= response.status < 300:
        raise ValueError("scope resolution could not establish membership")
    return response


def _resolve(context: Context, steps: Any, identity: Any) -> Any:
    if not isinstance(steps, list) or not 1 <= len(steps) <= 2:
        raise ValueError("scope resolution requires one or two steps")
    # Validate the complete route before any read. A tag cannot grant its own
    # exemption: operation and exact parameter shape must be in consumer policy.
    for step in steps:
        if not isinstance(step, dict) or set(step) - {
            "operationId",
            "parameter",
            "array",
            "resultsPath",
            "matchPath",
            "valuePath",
        }:
            raise ValueError("invalid scope resolution step")
        if not all(
            isinstance(step.get(k), str) and step[k]
            for k in ("operationId", "parameter", "matchPath", "valuePath")
        ):
            raise ValueError("scope resolution step has missing metadata")
        if "array" in step and type(step["array"]) is not bool:
            raise ValueError("scope resolution array must be boolean")
        for key in ("matchPath", "valuePath", "resultsPath"):
            if key in step:
                parts(step[key])
        op = context.index.operations.get(step["operationId"])
        allowed = context.scope_resolution_rules.get(
            f"{context.document}:{step['operationId']}", ()
        )
        if op is None or op.method.upper() != "GET" or (step["parameter"],) not in allowed:
            raise ValueError("scope resolution route is not authorized")
    multiple = isinstance(identity, list)
    values = identity if multiple else [identity]
    if not values:
        raise ValueError("empty scope identity list")
    for step in steps:
        requested = list(dict.fromkeys(_identity(value) for value in values))
        if len(requested) > 1 and not step.get("array", False):
            raise ValueError("scope resolution cannot resolve multiple scalar identities")
        response = context.resolve_scope(
            step["operationId"],
            {step["parameter"]: requested if step.get("array", False) else requested[0]},
        )
        rows = (
            pointer(response.body, step["resultsPath"])
            if "resultsPath" in step
            else [response.body]
        )
        if not isinstance(rows, list):
            raise ValueError("scope resolution result is not an array")
        # A truncated result cannot prove uniqueness. Never follow a continuation
        # or promote a partial lookup into an ownership decision.
        if pointer(response.body, "/_links/next", None) or pointer(response.body, "/cursor", None):
            raise ValueError("scope resolution result is incomplete")
        values = []
        for value in requested:
            matches = [
                row
                for row in rows
                if isinstance(row, dict)
                and pointer(row, step["matchPath"]) is not MISSING
                and _identity(pointer(row, step["matchPath"])) == value
            ]
            if len(matches) != 1:
                raise ValueError("scope resolution requires exactly one matching identity")
            values.append(_identity(pointer(matches[0], step["valuePath"])))
    return values if multiple else values[0]


class Scope(Transform):
    def request(self, context: Context, tag: Any) -> None:
        if tag is None:
            raise _refuse(context, None, "scope tag must be an object")
        params = {**context.parameters, **context.aliases}
        raw = None
        try:
            if isinstance(tag, dict):
                raw = (
                    pointer(context.body, tag.get("path", ""), None)
                    if tag.get("in") == "body"
                    else params.get(tag.get("name", ""))
                )
            kwargs = {
                "allowlist": context.scope_allowlist,
                "allow_site": context.scope_allow_site,
                "argv_identity": context.scope_argv_identity,
                "params": params,
                "body": context.body,
            }
            decision = decide(tag, **kwargs)
            if isinstance(tag, dict) and "resolve" in tag:
                if "checks" in tag:
                    raise ValueError("secondary checks cannot use resolvers")
                if not context.scope_allowlist:
                    raise ValueError("empty allowlist")
                if tag.get("in") == "body":
                    argv = context.scope_argv_identity
                    if not argv or argv not in context.scope_allowlist:
                        raise ValueError("body scope requires an allowed argv identity")
                    alias = tag.get("alias")
                    supplied_alias = context.aliases.get(alias) if isinstance(alias, str) else None
                    if supplied_alias is not None:
                        if supplied_alias != argv:
                            raise ValueError("scope argv identity and prerequisite alias disagree")
                        if raw is not None:
                            raise ValueError("conflicting body identity and prerequisite alias")
                    elif raw is None:
                        raise ValueError("missing body scope identity")
                    resolved = _resolve(context, tag["resolve"], argv)
                    if supplied_alias is not None:
                        set_target(context, {"in": "body", "path": tag["path"]}, resolved)
                        context.aliases = {k: v for k, v in context.aliases.items() if k != alias}
                        kwargs["body"] = context.body
                else:
                    # Missing/invalid inputs must fail before a metadata read.
                    values = raw if isinstance(raw, list) else [raw]
                    if not values:
                        raise ValueError("missing scope identity")
                    for value in values:
                        _identity(value)
                    resolved = _resolve(context, tag["resolve"], raw)
                decision = decide(tag, **kwargs, resolved_identity=resolved)
            if not decision.allowed:
                raise _refuse(context, decision.identity, decision.reason)
        except ScopeRefusal:
            raise
        except (ValueError, TypeError, KeyError, BaseAPIError) as exc:
            # Resolution failure is local inability to prove scope. Do not expose
            # untrusted response bodies or network exception text in the refusal.
            reason = str(exc) if isinstance(exc, ValueError) else "scope could not be established"
            raise _refuse(context, raw, reason) from exc
