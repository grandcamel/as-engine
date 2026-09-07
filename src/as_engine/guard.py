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


def _valid_allowlist(allowlist: object) -> TypeGuard[tuple[object, ...] | list[object] | None]:
    return allowlist is None or (
        isinstance(allowlist, (list, tuple)) and all(_valid_identity(item) for item in allowlist)
    )


def _allowed(value: object, allowlist: tuple[object, ...] | list[object] | None) -> bool:
    """Compare valid identities exactly, allowing a numeric id's string spelling."""
    if not _valid_identity(value):
        return False
    return allowlist is None or any(str(value) == str(item) for item in allowlist)


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


def _key_identity(identity: object, separator: object) -> object:
    if not isinstance(separator, str) or not separator:
        raise ValueError("invalid key separator")
    values = identity if isinstance(identity, list) else [identity]
    projects = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("invalid issue key")  # noqa: TRY004 - converted into a scope decision.
        project, marker, number = value.rpartition(separator)
        if not marker or not project or not number.isdecimal():
            raise ValueError("invalid issue key")
        projects.append(project)
    if not projects:
        raise ValueError("empty issue key list")
    return projects if isinstance(identity, list) else projects[0]


def _body_identity(body: object, tag: dict) -> object:
    if "paths" not in tag:
        return _pointer(body, tag.get("path"))
    paths = tag["paths"]
    if "path" in tag or not isinstance(paths, list) or not paths:
        raise ValueError("body alternatives require a nonempty paths list")
    values = []
    parent_present = False
    for path in paths:
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or re.search(r"~(?:[^01]|$)", path)
        ):
            raise ValueError("invalid body alternative JSON Pointer")
        parent_path = path.rsplit("/", 1)[0]
        try:
            parent = _pointer(body, parent_path) if parent_path else body
        except ValueError:
            continue
        if not isinstance(parent, (Mapping, list)):
            raise ValueError("body alternative parent is invalid")  # noqa: TRY004
        parent_present = True
        try:
            value = _pointer(body, path)
        except ValueError as exc:
            if str(exc) != "body identity is missing":
                raise
            continue
        if not _valid_identity(value):
            raise ValueError("body alternative identity is invalid")
        values.append(value)
    if not values:
        raise ValueError(
            "body alternative identity is invalid" if parent_present else "body identity is missing"
        )
    if not all(_same_identity(values[0], value) for value in values[1:]):
        raise ValueError("body alternative identities disagree")
    return values[0]


def _query_identity(value: object, tag: dict) -> object:
    if "conjunction" not in tag:
        return _clause_identity(value, tag["clause"])
    if tag["conjunction"] is not True:
        raise ValueError("conjunction must be true")
    return _conjunction_identity(value, tag["clause"])


def _conjunction_identity(value: object, expected: object) -> object:
    """Small AND-only literal grammar; every character must be consumed.

    Parentheses are IN lists only, not boolean groups or functions.
    The existing complete-clause parser remains the default for other tags.
    """
    if (
        not isinstance(value, str)
        or not isinstance(expected, str)
        or not _IDENTITY.fullmatch(expected)
    ):
        raise ValueError("invalid query clause")
    token = re.compile(
        r"""\s*(?:('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")|([A-Za-z0-9_.-]+)|(!=|!~|<=|>=|=|~|<|>|\(|\)|,))"""
    )
    tokens: list[tuple[str, str]] = []
    cursor = 0
    while cursor < len(value.rstrip()):
        match = token.match(value, cursor)
        if match is None:
            raise ValueError("invalid conjunction syntax")
        tokens.append(
            (
                "quoted" if match[1] else "word" if match[2] else "symbol",
                next(group for group in match.groups() if group is not None),
            )
        )
        cursor = match.end()
    position = 0
    projects: list[str] = []
    reserved = {"AND", "OR", "NOT", "IN", "IS", "WAS", "CHANGED", "ORDER", "BY", "FILTER"}

    def take() -> tuple[str, str]:
        nonlocal position
        if position >= len(tokens):
            raise ValueError("incomplete conjunction predicate")
        current = tokens[position]
        position += 1
        return current

    def literal() -> str:
        kind, text = take()
        if kind not in {"word", "quoted"} or (kind == "word" and text.upper() in reserved):
            raise ValueError("expected a literal query value")
        return text

    while position < len(tokens):
        kind, field = take()
        if (
            kind != "word"
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", field)
            or field.upper() in reserved
        ):
            raise ValueError("invalid conjunction field")
        operator_kind, operator = take()
        operator = operator.upper()
        if operator_kind == "quoted" or operator not in {
            "=",
            "!=",
            "~",
            "!~",
            ">",
            ">=",
            "<",
            "<=",
            "IN",
            "IS",
        }:
            raise ValueError("invalid conjunction operator")
        operands = []
        if operator == "IN":
            if take() != ("symbol", "("):
                raise ValueError("IN requires a literal list")
            operands.append(literal())
            while True:
                delimiter = take()
                if delimiter == ("symbol", ")"):
                    break
                if delimiter != ("symbol", ","):
                    raise ValueError("invalid IN list")
                operands.append(literal())
        else:
            operands.append(literal())
            if operator == "IS" and operands[0].upper() not in {"EMPTY", "NULL"}:
                raise ValueError("IS requires EMPTY or NULL")
        if field.casefold() == expected.casefold():
            if operator not in {"=", "IN"}:
                raise ValueError("project restriction requires equality or IN")
            expression = (
                field
                + " "
                + operator
                + " "
                + ("(" + ",".join(operands) + ")" if operator == "IN" else operands[0])
            )
            project = _clause_identity(expression, expected)
            projects.extend(project if isinstance(project, list) else [str(project)])
        if position < len(tokens):
            kind, joiner = take()
            if kind != "word" or joiner.upper() != "AND":
                raise ValueError("only AND conjunctions are supported")
            if position == len(tokens):
                raise ValueError("trailing AND")
    if not projects:
        raise ValueError("query requires a complete project clause")
    return projects[0] if len(projects) == 1 else projects


def _compare(
    identity: object, resolved: object, allowlist: tuple[object, ...] | list[object] | None
) -> bool:
    candidate = resolved if resolved is not None else identity
    if isinstance(candidate, list):
        return bool(candidate) and all(_allowed(item, allowlist) for item in candidate)
    return _allowed(candidate, allowlist)


def _decide_primary(
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
        if argv_identity is not None and not _allowed(argv_identity, allowlist):
            return _deny(argv_identity, "command identity is not allowed")
        return Decision(
            allow_site is True, None, "" if allow_site is True else "site access is disabled"
        )
    if not isinstance(location, str) or location not in {"path", "key", "query", "body"}:
        return _deny()
    if has_resolve and resolved_identity is None:
        return _deny(reason="identity requires resolution")

    if location == "body":
        if set(tag) - {
            "in",
            "path",
            "paths",
            "alias",
            "resolve",
            "clause",
            "conjunction",
            "separator",
        }:
            return _deny()
        alias = tag.get("alias")
        if alias is not None and (not isinstance(alias, str) or not alias):
            return _deny(reason="invalid body alias")
        try:
            identity = _body_identity(body, tag)
            if "clause" in tag:
                identity = _query_identity(identity, tag)
            elif "conjunction" in tag:
                raise ValueError("conjunction requires a query clause")
            if "separator" in tag:
                if "clause" in tag:
                    raise ValueError("body clause and separator cannot be combined")
                identity = _key_identity(identity, tag["separator"])
        except ValueError as exc:
            return _deny(reason=str(exc))
        values = identity if isinstance(identity, list) else [identity]
        if not values or not all(_valid_identity(value) for value in values):
            return _deny(identity, "body identity is missing or invalid")
        if not isinstance(argv_identity, str) or not argv_identity:
            return _deny(identity, "body scope requires an explicit command identity")
        comparison = resolved_identity if resolved_identity is not None else argv_identity
        if not all(_same_identity(comparison, value) for value in values):
            return _deny(identity, "body identity does not match command identity")
        allowed = _allowed(argv_identity, allowlist)
        return Decision(allowed, identity, "" if allowed else "identity is not allowed")

    if not isinstance(params, Mapping):
        return _deny(reason="parameters are missing")
    if set(tag) - {"in", "name", "separator", "clause", "conjunction", "resolve"}:
        return _deny()
    name = tag.get("name")
    if not isinstance(name, str) or not name or name not in params:
        return _deny(reason="tagged identity is missing")
    identity = params[name]
    if location == "query" and "clause" in tag:
        try:
            identity = _query_identity(identity, tag)
        except ValueError as exc:
            return _deny(identity, str(exc))
    elif "clause" in tag:
        return _deny(reason="query clause is only valid for query tags")
    if "conjunction" in tag and "clause" not in tag:
        return _deny(reason="conjunction requires a query clause")
    if location == "key" and "separator" in tag:
        try:
            identity = _key_identity(identity, tag["separator"])
        except ValueError as exc:
            return _deny(identity, str(exc))
    elif "separator" in tag:
        return _deny(reason="key separator is only valid for key tags")

    if isinstance(identity, list):
        valid = bool(identity) and all(_valid_identity(item) for item in identity)
    else:
        valid = _valid_identity(identity)
    if not valid:
        return _deny(identity, "tagged identity is missing")
    if argv_identity is not None:
        candidate = resolved_identity if resolved_identity is not None else identity
        values = candidate if isinstance(candidate, list) else [candidate]
        if not isinstance(argv_identity, str) or not all(
            _same_identity(argv_identity, value) for value in values
        ):
            return _deny(identity, "tagged identity does not match command identity")
    allowed = _compare(identity, resolved_identity, allowlist)
    return Decision(allowed, identity, "" if allowed else "identity is not allowed")


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
    """Decide primary and optional secondary identities without I/O."""
    primary = tag
    checks = None
    if isinstance(tag, dict) and "checks" in tag:
        if tag.get("in") not in {"key", "path", "query"} or "resolve" in tag:
            return _deny(reason="secondary checks require a direct named primary identity")
        checks = tag["checks"]
        primary = {key: value for key, value in tag.items() if key != "checks"}
    decision = _decide_primary(
        primary,
        allowlist=allowlist,
        allow_site=allow_site,
        argv_identity=argv_identity,
        params=params,
        body=body,
        resolved_identity=resolved_identity,
    )
    if not decision.allowed or checks is None:
        if isinstance(tag, dict) and "checks" in tag and checks is None:
            return _deny(reason="checks must be a list")
        return decision
    if not isinstance(checks, list):
        return _deny(reason="checks must be a list")
    identities = decision.identity if isinstance(decision.identity, list) else [decision.identity]
    if not identities or not all(_same_identity(identities[0], value) for value in identities):
        return _deny(reason="secondary checks need one primary project identity")
    for check in checks:
        if (
            not isinstance(check, dict)
            or set(check) != {"in", "paths", "optional"}
            or check["in"] != "body"
            or type(check["optional"]) is not bool
        ):
            return _deny(reason="invalid secondary body check")
        try:
            identity = _body_identity(body, check)
        except ValueError as exc:
            if check["optional"] and str(exc) == "body identity is missing":
                continue
            return _deny(reason=str(exc))
        if not _same_identity(identities[0], identity):
            return _deny(identity, "secondary body identity does not match primary identity")
    return decision
