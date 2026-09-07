"""Pure, product-independent scope decisions for tagged operations."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeGuard

_IDENTITY = re.compile(r"^[A-Za-z0-9_-]+$")
_CLAUSE = re.compile(
    r"^\s*([A-Za-z0-9_-]+)\s*(=|IN)\s*(?:"
    r"(['\"])([A-Za-z0-9_-]+)\3|"
    r"([A-Za-z0-9_-]+)|"
    r"\(\s*(?:(['\"])([A-Za-z0-9_-]+)\6|([A-Za-z0-9_-]+))"
    r"(?:\s*,\s*(?:(['\"])([A-Za-z0-9_-]+)\9|([A-Za-z0-9_-]+)))*\s*\)"
    r")\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    identity: object
    reason: str


def _deny(identity: object = None, reason: str = "invalid scope tag") -> Decision:
    return Decision(False, identity, reason)


def _valid_identity(value: object) -> bool:
    return (isinstance(value, str) and bool(value)) or (
        isinstance(value, int) and not isinstance(value, bool)
    )


def _valid_allowlist(allowlist: object) -> TypeGuard[tuple[object, ...] | list[object]]:
    return isinstance(allowlist, (list, tuple)) and all(_valid_identity(item) for item in allowlist)


def _allowed(value: object, allowlist: tuple[object, ...] | list[object]) -> bool:
    """Compare valid identities exactly, allowing a numeric id's string spelling."""
    if not _valid_identity(value):
        return False
    return any(str(value) == str(item) for item in allowlist)


def _same_identity(left: object, right: object) -> bool:
    return _valid_identity(left) and _valid_identity(right) and str(left) == str(right)


def _pointer(body: object, path: object) -> object:
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("body path must be a JSON Pointer")
    value = body
    for token in path[1:].split("/"):
        if re.search(r"~(?:[^01]|$)", token):
            raise ValueError("body path has an invalid JSON Pointer escape")
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, Mapping):
            if token not in value:
                raise ValueError("body identity is missing")
            value = value[token]
        elif isinstance(value, list) and token.isdecimal():
            index = int(token)
            if index >= len(value):
                raise ValueError("body identity is missing")
            value = value[index]
        else:
            raise ValueError("body identity is missing")
    return value


def _clause_identity(value: object, expected: object) -> object:
    if (
        not isinstance(value, str)
        or not isinstance(expected, str)
        or not _IDENTITY.fullmatch(expected)
    ):
        raise ValueError("invalid query clause")
    match = _CLAUSE.fullmatch(value)
    if not match or match.group(1).casefold() != expected.casefold():
        raise ValueError("invalid query clause")
    operator = match.group(2).upper()
    scalar = match.group(4) or match.group(5)
    if operator == "=":
        if scalar is None:
            raise ValueError("invalid query clause")
        return scalar
    if scalar is not None:
        raise ValueError("invalid query clause")
    # Capturing groups 7/8 and each repeated 10/11 retain only the final item;
    # extract values independently after the grammar above has accepted the input.
    contents = value[value.index("(") + 1 : value.rindex(")")]
    items = [item.strip().strip("'\"") for item in contents.split(",")]
    if not items or not all(_IDENTITY.fullmatch(item) for item in items):
        raise ValueError("invalid query clause")
    return items


def _compare(
    identity: object, resolved: object, allowlist: tuple[object, ...] | list[object]
) -> bool:
    candidate = resolved if resolved is not None else identity
    if isinstance(candidate, list):
        return bool(candidate) and all(_allowed(item, allowlist) for item in candidate)
    return _allowed(candidate, allowlist)


def decide(
    tag: object,
    *,
    allowlist: object = (),
    allow_site: bool = False,
    argv_identity: object = None,
    params: object = None,
    body: object = None,
    resolved_identity: object = None,
) -> Decision:
    """Return a fail-closed scope decision without performing I/O or resolution."""
    if tag is None:
        return Decision(True, None, "")
    if not _valid_allowlist(allowlist):
        return _deny(reason="invalid allowlist")
    if not isinstance(tag, dict):
        return _deny()

    location = tag.get("in")
    has_resolve = "resolve" in tag
    resolve = tag.get("resolve")
    if has_resolve and (
        not isinstance(resolve, list)
        or not resolve
        or not all(isinstance(item, dict) for item in resolve)
    ):
        return _deny(reason="invalid resolver metadata")
    if location == "site":
        if has_resolve or set(tag) - {"in"}:
            return _deny()
        return Decision(
            allow_site is True, None, "" if allow_site is True else "site access is disabled"
        )
    if not isinstance(location, str) or location not in {"path", "key", "query", "body"}:
        return _deny()
    if has_resolve and resolved_identity is None:
        return _deny(reason="identity requires resolution")

    if location == "body":
        if set(tag) - {"in", "path", "alias", "resolve"}:
            return _deny()
        alias = tag.get("alias")
        if alias is not None and (not isinstance(alias, str) or not alias):
            return _deny(reason="invalid body alias")
        try:
            identity = _pointer(body, tag.get("path"))
        except ValueError as exc:
            return _deny(reason=str(exc))
        if not _valid_identity(identity) or not isinstance(argv_identity, str) or not argv_identity:
            return _deny(identity, "body or command identity is missing")
        comparison = resolved_identity if resolved_identity is not None else argv_identity
        if not _same_identity(comparison, identity):
            return _deny(identity, "body identity does not match command identity")
        allowed = _allowed(argv_identity, allowlist)
        return Decision(allowed, identity, "" if allowed else "identity is not allowed")

    if not isinstance(params, Mapping):
        return _deny(reason="parameters are missing")
    if set(tag) - {"in", "name", "separator", "clause", "resolve"}:
        return _deny()
    name = tag.get("name")
    if not isinstance(name, str) or not name or name not in params:
        return _deny(reason="tagged identity is missing")
    identity = params[name]
    if location == "query" and "clause" in tag:
        try:
            identity = _clause_identity(identity, tag["clause"])
        except ValueError as exc:
            return _deny(identity, str(exc))
    elif "clause" in tag:
        return _deny(reason="query clause is only valid for query tags")
    if location == "key" and "separator" in tag:
        separator = tag["separator"]
        if not isinstance(separator, str) or not separator or not isinstance(identity, str):
            return _deny(identity, "invalid key separator")
        project, marker, number = identity.rpartition(separator)
        if not marker or not project or not number.isdecimal():
            return _deny(identity, "invalid issue key")
        identity = project
    elif "separator" in tag:
        return _deny(reason="key separator is only valid for key tags")

    if isinstance(identity, list):
        valid = bool(identity) and all(_valid_identity(item) for item in identity)
    else:
        valid = _valid_identity(identity)
    if not valid:
        return _deny(identity, "tagged identity is missing")
    allowed = _compare(identity, resolved_identity, allowlist)
    return Decision(allowed, identity, "" if allowed else "identity is not allowed")
