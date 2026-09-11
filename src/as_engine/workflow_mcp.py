"""Optional, private stdio adapter to one operator-fixed installed Jira CLI.

No product execution imports: the console script owns all business behavior.
Install as-engine[mcp] explicitly. Ordinary engine imports do not load this module.
See docs/workflow-mcp.md for configuration, limits and the ownership boundary.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import stat
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    import mcp_types as types
    from jsonschema import Draft202012Validator, validators
    from mcp.server import Server, ServerRequestContext
    from mcp.server.stdio import stdio_server
except ModuleNotFoundError as exc:
    if __name__ == "__main__" and exc.name in {"mcp", "mcp_types", "jsonschema"}:
        print("workflow-mcp: missing dependency; install as-engine[mcp]", file=sys.stderr)
        raise SystemExit(2) from None
    raise

MAX_OFFSET = 9223372036854775807
TOOLS = ("workflows_list", "workflows_search", "workflows_describe", "workflows_run")
CAPABILITY = "indexed-read-v1"
WORKFLOW = "list-projects"
ROUTING_OVERRIDES = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR",
)
CONTEXT_NAMES = (
    "JIRA_SITE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_ALLOWED_PROJECTS",
    "JIRA_ALLOW_SITE_OPERATIONS", "JIRA_DEFAULT_PROJECT", "JIRA_AS_TRANSPORT",
    *ROUTING_OVERRIDES,
)
LIMITS = {
    "call_timeout_seconds": (180, 1, 600),
    "discovery_timeout_seconds": (10, 1, 30),
    "stdout_max_bytes": (1048576, 4096, 1048576),
    "stderr_max_bytes": (1048576, 4096, 1048576),
    "terminate_grace_seconds": (2, 1, 5),
    "kill_grace_seconds": (2, 1, 5),
    "drain_grace_seconds": (1, 1, 5),
}
ERROR_CODES = (
    "invalid-input", "unsupported-workflow", "incompatible-catalog", "incompatible-output",
    "runtime-context-unavailable", "identity-binding-mismatch", "executable-unavailable",
    "executable-changed", "busy", "launch-failed", "timeout", "cancelled", "output-limit",
    "invalid-json", "unexpected-stream", "child-exit", "unsafe-output", "cleanup-unresolved",
    "internal-error",
)


def _object(properties: dict[str, Any], required: tuple[str, ...] | None = None) -> dict:
    return {"type": "object", "properties": properties, "additionalProperties": False,
            "required": list(properties) if required is None else list(required)}


def _enum(*values: Any) -> dict:
    schema = {"enum": list(values)}
    if all(type(value) is bool for value in values):
        schema["type"] = "boolean"
    elif all(isinstance(value, str) for value in values):
        schema["type"] = "string"
    return schema


def _integer(minimum: int = 0, maximum: int | None = None) -> dict:
    result = {"type": "integer", "minimum": minimum}
    if maximum is not None:
        result["maximum"] = maximum
    return result


def _text(maximum: int | None = None) -> dict:
    result: dict[str, Any] = {"type": "string", "minLength": 1}
    if maximum is not None:
        result["maxLength"] = maximum
    return result


def _array(items: dict, maximum: int = 100) -> dict:
    return {"type": "array", "items": items, "maxItems": maximum}


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


StrictValidator = validators.extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
        "integer", lambda _checker, value: type(value) is int
    ),
)


def _valid(schema: dict, value: Any) -> bool:
    return StrictValidator(schema).is_valid(value)


def _json(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)
    for raw, escaped in (("`", "\\u0060"), ("<", "\\u003c"),
                         (">", "\\u003e"), ("&", "\\u0026")):
        text = text.replace(raw, escaped)
    return text


def _pairs(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(_value: str) -> Any:
    raise ValueError("nonfinite JSON")


def _decode(raw: bytes, cap: int) -> dict:
    if len(raw) > cap:
        raise ValueError("JSON limit")
    # Check nesting before json.loads, including strings with escaped quotes.
    depth = 0
    quoted = escaped = False
    text = raw.decode("utf-8")
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > 32:
                raise ValueError("JSON depth")
        elif char in "]}":
            depth -= 1
    value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    if not isinstance(value, dict):
        # All invalid child JSON uses ValueError for the caller's invalid-json mapping.
        raise ValueError("JSON object required")  # noqa: TRY004
    return value


_HASH = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_PROJECT = {"type": "string", "pattern": "^[A-Z][A-Z0-9_]{0,79}$"}
_BOOL = {"type": "boolean"}
_EMPTY = _object({})
_BOUNDS = _object({
    name: _object({"type": _enum("integer"), "default": _integer(),
                   "minimum": _integer(), "maximum": _integer(), "description": _text(256)},
                  ("type", "default", "minimum", "maximum"))
    for name in ("limit", "offset")
})
_INPUTS = _object({"limit": _integer(1, 100), "offset": _integer(0, MAX_OFFSET)})
_PROFILE_SCHEMA = _object({
    "schema_version": {"type": "integer", "const": 1},
    "adapter_id": {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,79}$"},
    "product": _enum("jira-as"), "executable": _text(), "executable_sha256": _HASH,
    "expected": _object({
        "product_version": _text(80), "engine_version": _text(80),
        "schema_version": {"type": "integer", "const": 1},
        "catalog_revision": _integer(1), "definition_digest": _HASH,
        "workflow_revisions": _object({WORKFLOW: _integer(1)}),
    }),
    "context": _object({
        "source": _enum("approved-env-v1"), "account_email": _text(254),
        "site_url": _text(2048), "home": _text(), "cwd": _text(), "tmpdir": _text(),
        "scope": _object({"allowed_projects": {**_array(_PROJECT), "uniqueItems": True},
                          "allow_site_operations": _BOOL,
                          "default_project": _nullable(_PROJECT)}, ()),
    }),
    "limits": _object({key: _integer(low, high) for key, (_, low, high) in LIMITS.items()}, ()),
}, ("schema_version", "adapter_id", "product", "executable", "executable_sha256",
    "expected", "context"))


def _origin(value: str) -> str:
    parts = urlsplit(value)
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None
        or parts.password is not None or parts.path not in {"", "/"}
        or parts.query or parts.fragment or "\\" in value
        or any(c.isspace() or ord(c) < 32 for c in value)):
        raise ValueError("site origin")
    port = parts.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("site port")
    return "https://" + parts.netloc.lower()


@dataclass(frozen=True)
class Profile:
    """An immutable, validated launch snapshot; obtain it with load_profile()."""

    _document: str = field(repr=False)
    _context: tuple[tuple[str, str], ...] = field(repr=False)

    @property
    def document(self) -> dict:
        return json.loads(self._document)

    def environment(self, *, run: bool) -> dict[str, str]:
        data = self.document
        context = data["context"]
        env = {
            "HOME": context["home"], "TMPDIR": context["tmpdir"],
            "PATH": str(Path(data["executable"]).parent) + ":/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "C", "LC_ALL": "C", "NO_COLOR": "1",
            "PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1",
        }
        if run:
            source = dict(self._context)
            for name in CONTEXT_NAMES[:6]:
                if name in source:
                    env[name] = source[name]
            env["JIRA_AS_TRANSPORT"] = "http"
        return env

    def runtime_error(self) -> str | None:
        source = dict(self._context)
        if source.get("JIRA_AS_TRANSPORT", "http") != "http":
            return "runtime-context-unavailable"
        if any(source.get(name) for name in ROUTING_OVERRIDES):
            return "runtime-context-unavailable"
        if any(not source.get(name) for name in CONTEXT_NAMES[:3]):
            return "runtime-context-unavailable"
        # Presence, not truthiness: the empty string is the explicit empty scope.
        if "JIRA_ALLOWED_PROJECTS" not in source or "JIRA_ALLOW_SITE_OPERATIONS" not in source:
            return "runtime-context-unavailable"
        context = self.document["context"]
        scope = context["scope"]
        try:
            raw = source["JIRA_ALLOWED_PROJECTS"]
            projects = sorted({v.strip().upper() for v in raw.split(",")}) if raw.strip() else []
            flag = source["JIRA_ALLOW_SITE_OPERATIONS"].strip().lower()
            if (any(not _valid(_PROJECT, key) for key in projects)
                or flag not in {"true", "false"}):
                return "runtime-context-unavailable"
            if (_origin(source["JIRA_SITE_URL"]) != context["site_url"]
                or source["JIRA_EMAIL"] != context["account_email"]
                or projects != scope["allowed_projects"]
                or (flag == "true") != scope["allow_site_operations"]
                or source.get("JIRA_DEFAULT_PROJECT") != scope["default_project"]):
                return "identity-binding-mismatch"
        except ValueError:
            return "runtime-context-unavailable"
        if any("\0" in value for value in source.values()):
            return "runtime-context-unavailable"
        return None

    def executable_error(self) -> str | None:
        data = self.document
        path = Path(data["executable"])
        try:
            if (str(path.resolve(strict=True)) != str(path) or not path.is_file()
                or not os.access(path, os.X_OK)):
                return "executable-unavailable"
            if hashlib.sha256(path.read_bytes()).hexdigest() != data["executable_sha256"]:
                return "executable-changed"
        except OSError:
            return "executable-unavailable"
        return None

    def contains_secret(self, value: dict) -> bool:
        source = dict(self._context)
        token = source.get("JIRA_API_TOKEN")
        if not token:
            return False
        basic = base64.b64encode((source.get("JIRA_EMAIL", "") + ":" + token).encode()).decode()
        # Check decoded leaves: JSON escaping must not hide a token match.
        def contains(node: Any) -> bool:
            if isinstance(node, str):
                return token in node or basic in node
            if isinstance(node, dict):
                return any(contains(k) or contains(v) for k, v in node.items())
            if isinstance(node, list):
                return any(contains(v) for v in node)
            return False
        return contains(value)


def load_profile(path: str | Path) -> Profile:
    """Read a nonsecret operator profile, snapshot only explicitly admitted env names.

    Raises ValueError with a fixed category; no profile/environment values escape.
    Executable/runtime availability is checked separately so discovery can diagnose it.
    """
    try:
        path = Path(path)
        info = path.lstat()
        if (not path.is_absolute() or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o022):
            raise ValueError("profile ownership")
        with path.open("rb") as stream:
            data = _decode(stream.read(65537), 65536)
        if not _valid(_PROFILE_SCHEMA, data):
            raise ValueError("profile schema")
        context = data["context"]
        if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", context["account_email"]) is None:
            raise ValueError("account email")
        if _origin(context["site_url"]) != context["site_url"]:
            raise ValueError("canonical origin required")
        for key in ("home", "cwd", "tmpdir"):
            directory = Path(context[key])
            if (not directory.is_absolute() or str(directory.resolve(strict=True)) != str(directory)
                or not directory.is_dir() or directory.stat().st_uid != os.getuid()
                or directory.stat().st_mode & 0o022):
                raise ValueError("fixed directory required")
        executable = Path(data["executable"])
        if not executable.is_absolute() or str(executable.resolve()) != str(executable):
            raise ValueError("fixed executable required")
        scope = context["scope"]
        scope.setdefault("allowed_projects", [])
        scope.setdefault("allow_site_operations", False)
        scope.setdefault("default_project", None)
        if (scope["allowed_projects"] != sorted(scope["allowed_projects"])
            or scope["default_project"] is not None
            and scope["default_project"] not in scope["allowed_projects"]):
            raise ValueError("scope ordering/default")
        data["limits"] = {key: data.get("limits", {}).get(key, default)
                          for key, (default, _, _) in LIMITS.items()}
        return Profile(_json(data), tuple((n, os.environ[n]) for n in CONTEXT_NAMES if n in os.environ))
    except (OSError, ValueError, TypeError, RecursionError):
        raise ValueError("invalid-profile") from None


ERROR_SCHEMA = _object({
    "adapter_schema_version": {"type": "integer", "const": 1},
    "status": _enum("adapter-error"),
    "error": _object({
        "code": _enum(*ERROR_CODES), "phase": _enum("bootstrap", "admission", "execute", "cleanup"),
        "child_exit_code": {"type": ["integer", "null"]},
        "stdout_bytes": _integer(), "stderr_bytes": _integer(), "counts_complete": _BOOL,
        "cleanup": _enum("not-started", "reaped", "unresolved"), "dispatch_blocked": _BOOL,
    }),
})


def _failure(code: str, *, phase: str = "admission", child_exit_code: int | None = None,
             stdout_bytes: int = 0, stderr_bytes: int = 0, counts_complete: bool = True,
             cleanup: str = "not-started", dispatch_blocked: bool = False) -> dict:
    return {"adapter_schema_version": 1, "status": "adapter-error", "error": {
        "code": code, "phase": phase, "child_exit_code": child_exit_code,
        "stdout_bytes": stdout_bytes, "stderr_bytes": stderr_bytes,
        "counts_complete": counts_complete, "cleanup": cleanup,
        "dispatch_blocked": dispatch_blocked,
    }}


# This is the schema-1 wire vocabulary, not an execution or pagination implementation.
_REASONS = {
    "incompatible-definition-or-runtime": "The definition and installed runtime/index are incompatible; check package alignment.",
    "unsupported-workflow": "This catalog does not support that workflow; search the catalog.",
    "invalid-input": "Supply only the declared integer inputs within their bounds.",
    "invalid-discovery-input": "Use a nonempty query of at most 512 characters and a nonnegative offset.",
    "query-output-too-large": "The escaped query and search result exceed the discovery output budget; retry with a shorter query.",
    "catalog-read": "Support is declared; account, configuration and scope have not been checked.",
    "scope-refused": "Current scope or account permission blocks this read.",
    "authentication-failed": "Authentication failed; inspect operator credentials.",
    "configuration-or-parameters": "Configuration, credentials or indexed parameters prevent this read.",
    "resource-not-found": "The resource was not found; this is not evidence of an empty result.",
    "transport-failed": "The transport failed or exhausted its existing read retry policy.",
    "conflict": "The service reported a conflict; inspect before retrying.",
    "read-failed": "The read failed; inspect the configuration and service before retrying.",
    "malformed-page": "The page is malformed, contradictory or oversized; inspect before retrying.",
    "no-progress": "The service reports more results but returned no items; no continuation is safe.",
    "completion-unknown": "No completion signal establishes whether another page follows this range.",
    "offset-unestablished": "More results exist, but response offset evidence is absent; inspect or retry.",
    "continuation-out-of-bounds": "More results exist, but the next offset exceeds the declared bounds.",
    "more-results": "Another page follows this range; continuation advances by the returned count.",
    "final-page": "Consistent evidence establishes that no page follows this returned range.",
}
_ABSENT_MESSAGE = (
    "Workflow metadata or the optional engine capability is unavailable; "
    "check installed package alignment."
)
_REASON_SCHEMA = {"oneOf": [
    _object({"code": _enum(code), "message": _enum(message)})
    for code, message in _REASONS.items()
] + [_object({"code": _enum("incompatible-definition-or-runtime"),
              "message": _enum(_ABSENT_MESSAGE)})]}
_EXAMPLES = _array(_object({"kind": _enum("invocation", "json"), "value": _text(1200),
                           "schema": {"type": "object"}}, ("kind", "value")), 10)
_CONTINUE = _object({"workflow": _enum(WORKFLOW), "inputs": _INPUTS})
_ACTION = {"oneOf": [
    _object({"action": _enum("inspect-configuration-or-definition", "search-catalog", "shorten-query")}),
    _object({"action": _enum("continue", "inspect-or-retry"),
             "workflow": _enum(WORKFLOW), "inputs": _INPUTS}),
]}
_EVIDENCE = {"oneOf": [
    _EMPTY, _object({"http_status": _nullable(_integer(100, 599))}),
    _object({
        "binding": _enum("indexed-read"), "document": _enum("platform"),
        "operation_id": _enum("searchProjects"), "method": _enum("GET"),
        "received_count": _nullable(_integer()), "omitted_count": _nullable(_integer()),
        "coverage": _enum("Only an offset0-to-final traversal covers the full set; reads are not a transactional snapshot."),
        "metadata_present": {**_array(_enum("offset", "limit", "total", "is_last"), 4),
                             "uniqueItems": True},
        "metadata": _object({"offset": _integer(), "limit": _integer(1),
                             "total": _integer(), "is_last": _BOOL}, ()),
    }, ("binding", "document", "operation_id", "method", "received_count", "omitted_count", "coverage")),
]}
_BASE = {
    "product": _enum("jira-as"), "product_version": _text(80), "engine_version": _text(80),
    "schema_version": _nullable(_integer(1)), "required_schema_version": _nullable(_integer(1)),
    "runtime_capabilities": {**_array(_text(80), 10), "uniqueItems": True},
    "required_capabilities": {**_array(_text(80), 10), "uniqueItems": True},
    "definition_digest": _nullable(_HASH), "catalog_revision": _nullable(_integer(1)),
    "workflow": _nullable(_enum(WORKFLOW)), "revision": _nullable(_integer(1)),
    "support": _nullable(_BOOL), "availability": _enum("unknown", "blocked", "available"),
    "status": _enum("completed-read", "needs-input", "blocked", "failed", "unknown"),
    "exit_code": _integer(0, 7), "inputs": {"oneOf": [_EMPTY, _INPUTS, _BOUNDS]},
    "items": _array(_object({"id": _text(), "key": _text(), "name": _text(),
                            "url": _nullable(_text(4096)), "url_source": _nullable(_enum("provider-self"))})),
    "returned_count": _integer(0, 100), "limit": _nullable(_integer(1, 100)),
    "offset": _nullable(_integer()), "range": _nullable(_object({"start": _integer(), "end": _integer()})),
    "complete": _nullable(_BOOL), "continuation": _nullable(_CONTINUE),
    "evidence": _EVIDENCE, "reason": _REASON_SCHEMA, "next_actions": _array(_ACTION, 1),
}
_PARAM_SCHEMA = _object({"in": _enum("query"), "schema": _object({
    "type": _enum("integer", "string"), "format": _enum("int32", "int64"),
    "default": {"type": ["integer", "string"]}, "maximum": _integer(),
    "enum": _array(_text(80)),
}, ("type",))})
_DESCRIBE = {
    "title": _text(120), "purpose": _text(512), "search_terms": _array(_text(256), 20),
    "prerequisites": _array(_text(256), 20), "examples": _EXAMPLES,
    "binding": _object({
        "kind": _enum("indexed-read"), "document": _enum("platform"),
        "operation_id": _enum("searchProjects"), "method": _enum("GET"),
        "path": _enum("/rest/api/3/project/search"),
        "fixed_parameters": {"const": {"orderBy": "key", "action": "view"}},
        "input_parameters": {"const": {"limit": "maxResults", "offset": "startAt"}},
        "parameter_schemas": _object({name: _PARAM_SCHEMA for name in
                                      ("startAt", "maxResults", "orderBy", "action")}),
        "scope": {"const": {"in": "site"}},
    }),
    "projection": {"const": {"id": "/id", "key": "/key", "name": "/name",
                              "canonical_url": {"pointer": "/self", "path": "/rest/api/3/project/{identity}"}}},
    "paging": {"const": {
        "tag": {"style": "offset/limit", "itemsPath": "/values",
                "request": {"offset": {"in": "query", "name": "startAt"},
                            "limit": {"in": "query", "name": "maxResults"}},
                "response": {"totalPath": "/total"}},
        "evidence": {"offset": "/startAt", "limit": "/maxResults",
                     "total": "/total", "is_last": "/isLast"},
    }},
}


def _product_schema(action: str) -> dict:
    failure = _object({**_BASE, "exit_code": _integer(1, 7),
                       "view": _enum("run"), "input_bounds": _BOUNDS}, tuple(_BASE))
    success = {**_BASE, "exit_code": {"type": "integer", "const": 0},
               "status": _enum("completed-read"), "support": _enum(True)}
    if action in {"list", "search"}:
        success["inputs"] = _EMPTY
        success.update(view=_enum(action), entries=_array(_object({
            "id": _enum(WORKFLOW), "revision": _integer(1), "title": _text(120),
            "purpose": _text(512), "support": _enum(True), "availability": _enum("unknown"),
        }), 1), continuation=_nullable(_object({
            "action": _enum(action), "offset": _integer(),
            **({"query": _text(512)} if action == "search" else {}),
        })))
        if action == "search":
            success["query"] = _text(512)
    else:
        success.update(view=_enum(action), input_bounds=_BOUNDS)
        if action == "describe":
            success.update(_DESCRIBE)
            success["inputs"] = _BOUNDS
        if action == "examples":
            success["inputs"] = _EMPTY
            success["examples"] = _EXAMPLES
        if action == "run":
            success["inputs"] = _INPUTS
    return {"oneOf": [failure, _object(success)]}


def _output_schema(action: str) -> dict:
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
            "oneOf": [ERROR_SCHEMA, _product_schema(action)]}


def _inputs(action: str, bounds: dict | None) -> dict:
    offset = {**_integer(0, MAX_OFFSET), "default": 0}
    workflow = _enum(WORKFLOW) if bounds is not None else {
        "type": "string", "pattern": "^[a-z][a-z0-9-]{0,79}$"}
    if action == "list":
        return _object({"offset": offset}, ())
    if action == "search":
        return _object({"offset": offset, "query": _text(512)}, ("query",))
    if action == "describe":
        return _object({"workflow": workflow, "examples": {**_BOOL, "default": False}}, ("workflow",))
    return _object({"workflow": workflow,
                    "inputs": {**_object(bounds or {}, ()), "default": {}}}, ("workflow",))


@dataclass
class _Attempt:
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    overflow: asyncio.Event = field(default_factory=asyncio.Event)
    process: Any = None
    streams: list[bytearray] = field(default_factory=lambda: [bytearray(), bytearray()])
    counts: list[int] = field(default_factory=lambda: [0, 0])
    eof: list[bool] = field(default_factory=lambda: [False, False])
    readers: list[asyncio.Task] = field(default_factory=list)
    waiter: asyncio.Task | None = None
    reader_failed: bool = False
    cleanup: str = "not-started"
    observation: _Observation | None = None


@dataclass(frozen=True)
class _Observation:
    """Sanitized immutable evidence for one invocation, never a latest-call lookup."""

    phase: str = "admission"
    child_exit_code: int | None = None
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    counts_complete: bool = True
    cleanup: str = "not-started"
    dispatch_blocked: bool = False

    @classmethod
    def capture(cls, attempt: _Attempt, phase: str) -> _Observation:
        return cls(phase, getattr(attempt.process, "returncode", None),
                   attempt.counts[0], attempt.counts[1], all(attempt.eof),
                   attempt.cleanup, attempt.cleanup == "unresolved")

    @classmethod
    def saved_error(cls, value: dict) -> _Observation:
        error = value["error"]
        return cls(**{key: error[key] for key in cls.__dataclass_fields__})

    def failure(self, code: str) -> dict:
        return _failure(
            code, phase=self.phase, child_exit_code=self.child_exit_code,
            stdout_bytes=self.stdout_bytes, stderr_bytes=self.stderr_bytes,
            counts_complete=self.counts_complete, cleanup=self.cleanup,
            dispatch_blocked=self.dispatch_blocked,
        )


async def _settled(tasks: list[asyncio.Task], seconds: float) -> bool:
    if not tasks:
        return True
    done, pending = await asyncio.wait(tasks, timeout=seconds)
    for task in done:
        _consume(task)
    return not pending


def _consume(task: asyncio.Task) -> None:
    # Retrieving an exception does not change task.result(); it prevents asyncio
    # from emitting an unobserved exception (possibly containing child data).
    if not task.cancelled():
        task.exception()


class _Children:
    """Per-server ownership, never global process discovery or group signaling."""

    def __init__(self, profile: Profile):
        self.profile = profile
        self.limits = profile.document["limits"]
        self.active: asyncio.Task | None = None
        self.attempt: _Attempt | None = None
        self.blocked: dict | None = None
        self.last_error: dict | None = None
        self.retained: list[asyncio.Task] = []
        self.closer: asyncio.Task | None = None

    def _task(self, coroutine: Any) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        task.add_done_callback(_consume)
        self.retained.append(task)
        return task

    def _error(self, attempt: _Attempt, code: str, phase: str, cleanup: str) -> dict:
        attempt.cleanup = cleanup
        return _failure(code, phase=phase,
                        child_exit_code=getattr(attempt.process, "returncode", None),
                        stdout_bytes=attempt.counts[0], stderr_bytes=attempt.counts[1],
                        counts_complete=all(attempt.eof), cleanup=cleanup,
                        dispatch_blocked=self.blocked is not None or cleanup == "unresolved")

    def _refuse(self, attempt: _Attempt) -> dict:
        self.blocked = self._error(attempt, "cleanup-unresolved", "cleanup", "unresolved")
        self.last_error = self.blocked
        print(_json(self.blocked), file=sys.stderr)
        return self.blocked

    async def _read(self, attempt: _Attempt, index: int, stream: Any) -> None:
        maximum = self.limits["stdout_max_bytes" if index == 0 else "stderr_max_bytes"]
        try:
            while True:
                # The boundary probe consumes at most one byte past retained capacity.
                remaining = maximum - len(attempt.streams[index])
                chunk = await stream.read(min(16384, remaining + 1))
                if not chunk:
                    attempt.eof[index] = True
                    return
                attempt.counts[index] += len(chunk)
                attempt.streams[index].extend(chunk[:remaining])
                if len(chunk) > remaining:
                    attempt.overflow.set()
                    # Stop retaining, but keep draining until owned cleanup settles.
                    while True:
                        chunk = await stream.read(16384)
                        if not chunk:
                            attempt.eof[index] = True
                            return
                        attempt.counts[index] += len(chunk)
        except Exception:  # noqa: BLE001
            # Reader boundaries sanitize arbitrary failures and mark cleanup unresolved.
            attempt.reader_failed = True
            attempt.overflow.set()

    def _attach(self, attempt: _Attempt, process: Any) -> None:
        attempt.process = process
        attempt.cleanup = "unresolved"
        attempt.readers = [self._task(self._read(attempt, index, stream))
                           for index, stream in enumerate((process.stdout, process.stderr))]
        attempt.waiter = self._task(process.wait())

    async def _cleanup(self, attempt: _Attempt) -> bool:
        process = attempt.process
        if process is None:
            return True
        attempt.cleanup = "unresolved"
        try:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            if attempt.waiter is not None:
                done = await _settled([attempt.waiter], self.limits["terminate_grace_seconds"])
                if not done and process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                if not done:
                    done = await _settled([attempt.waiter], self.limits["kill_grace_seconds"])
            else:
                done = False
            drained = await _settled(attempt.readers, self.limits["drain_grace_seconds"])
            if not drained:
                for reader in attempt.readers:
                    if not reader.done():
                        reader.cancel()
                # Yield once for cancellation without adding another grace period.
                # EOF remains independently required; unsettled tasks stay owned.
                drained = await _settled(attempt.readers, 0)
            if not done or not drained or not all(attempt.eof) or attempt.reader_failed:
                return False
            reaped = (attempt.waiter is not None and not attempt.waiter.cancelled()
                      and attempt.waiter.exception() is None and process.returncode is not None)
            if reaped:
                attempt.cleanup = "reaped"
            return reaped
        except Exception:  # noqa: BLE001
            # Any owned-handle failure means cleanup is unresolved, never a raw diagnostic.
            return False

    async def _late_spawn(self, attempt: _Attempt, spawn: asyncio.Task) -> None:
        try:
            process = await asyncio.shield(spawn)
        except Exception:  # noqa: BLE001
            # Consume late creation failures without leaking details; admission stays blocked.
            return
        self._attach(attempt, process)
        await self._cleanup(attempt)
        # Never clear the permanent refusal, even if a late child is now reaped.

    async def _own(self, attempt: _Attempt, argv: list[str], run: bool, phase: str) -> dict:
        watch = [self._task(attempt.stop.wait()), self._task(attempt.overflow.wait())]
        spawn: asyncio.Task | None = None
        cause: str | None = None
        try:
            timeout = self.limits["call_timeout_seconds" if run else "discovery_timeout_seconds"]
            deadline = asyncio.get_running_loop().time() + timeout
            spawn = self._task(asyncio.create_subprocess_exec(
                *argv, cwd=self.profile.document["context"]["cwd"],
                env=self.profile.environment(run=run), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                close_fds=True, limit=16384,
            ))
            done, _ = await asyncio.wait([spawn, watch[0]], timeout=timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
            if spawn not in done:
                cause = "cancelled" if attempt.stop.is_set() else "timeout"
                grace = sum(self.limits[key] for key in
                            ("terminate_grace_seconds", "kill_grace_seconds", "drain_grace_seconds"))
                if not await _settled([spawn], grace):
                    self._task(self._late_spawn(attempt, spawn))
                    return self._refuse(attempt)
            try:
                self._attach(attempt, spawn.result())
            except (OSError, ValueError):
                return self._error(attempt, "launch-failed", phase, "not-started")
            if cause is None:
                # Include readers: direct child exit alone does not prove pipe EOF.
                async def complete() -> None:
                    # One cancelled reader must not orphan the gather's outcome.
                    await asyncio.gather(attempt.waiter, *attempt.readers, return_exceptions=True)

                completed = self._task(complete())
                remaining = max(0, deadline - asyncio.get_running_loop().time())
                done, _ = await asyncio.wait([completed, *watch], timeout=remaining,
                                             return_when=asyncio.FIRST_COMPLETED)
                if attempt.stop.is_set():
                    cause = "cancelled"
                elif attempt.overflow.is_set():
                    cause = "unexpected-stream" if attempt.reader_failed else "output-limit"
                elif completed not in done:
                    cause = "timeout"
                else:
                    completed.result()
            if not await self._cleanup(attempt):
                return self._refuse(attempt)
            if cause:
                return self._error(attempt, cause, phase, "reaped")
            code = attempt.process.returncode
            if type(code) is not int or not 0 <= code <= 7:
                return self._error(attempt, "child-exit", phase, "reaped")
            index = 0 if code == 0 else 1
            if attempt.counts[1 - index]:
                return self._error(attempt, "unexpected-stream", phase, "reaped")
            try:
                value = _decode(bytes(attempt.streams[index]), self.limits[
                    "stdout_max_bytes" if index == 0 else "stderr_max_bytes"])
            except (ValueError, RecursionError):
                return self._error(attempt, "invalid-json", phase, "reaped")
            if type(value.get("exit_code")) is not int or value["exit_code"] != code:
                return self._error(attempt, "incompatible-output", phase, "reaped")
            if self.profile.contains_secret(value):
                return self._error(attempt, "unsafe-output", phase, "reaped")
            return value
        except (Exception, asyncio.CancelledError):  # noqa: BLE001
            # This ownership boundary must reap or retain handles after every failure.
            # A creation task can settle at the same instant another await fails.
            # Transfer even an already-completed handle before deciding cleanup.
            if attempt.process is None and spawn is not None:
                if not spawn.done():
                    self._task(self._late_spawn(attempt, spawn))
                    return self._refuse(attempt)
                if not spawn.cancelled() and spawn.exception() is None:
                    self._attach(attempt, spawn.result())
                elif spawn.cancelled():
                    return self._refuse(attempt)
            if attempt.process is not None and not await self._cleanup(attempt):
                return self._refuse(attempt)
            return self._error(attempt, "internal-error", phase,
                               "reaped" if attempt.process is not None else "not-started")
        finally:
            # Freeze before yielding ownership: late cleanup may continue to
            # update the retained attempt after an unresolved response is saved.
            attempt.observation = _Observation.capture(attempt, phase)
            for task in watch:
                task.cancel()
            self.active = None

    async def call(self, argv: list[str], *, run: bool, phase: str) -> tuple[dict, _Observation]:
        self.retained = [task for task in self.retained if not task.done()]
        if self.blocked is not None:
            return self.blocked, _Observation.saved_error(self.blocked)
        if self.active is not None:
            return _failure("busy"), _Observation()
        error = self.profile.executable_error()
        if error:
            return _failure(error, phase=phase), _Observation(phase=phase)
        if run and (error := self.profile.runtime_error()):
            return _failure(error), _Observation()
        attempt = self.attempt = _Attempt()
        owner = self.active = self._task(self._own(attempt, argv, run, phase))
        # This task, not the MCP request task, owns creation/read/reap through cancellation.
        def remember(task: asyncio.Task) -> None:
            if not task.cancelled() and task.exception() is None:
                result = task.result()
                if result.get("status") == "adapter-error":
                    self.last_error = result
                    if attempt.stop.is_set() and result != self.blocked:
                        print(_json(result), file=sys.stderr)
        owner.add_done_callback(remember)
        try:
            value = await asyncio.shield(owner)
            observed = attempt.observation or _Observation.capture(attempt, phase)
            return value, observed
        except asyncio.CancelledError:
            attempt.stop.set()
            raise
        except Exception:  # noqa: BLE001
            # If ownership itself fails unexpectedly, preserve its local snapshot;
            # unresolved ownership still blocks dispatch instead of losing evidence.
            observed = attempt.observation or _Observation.capture(attempt, phase)
            if observed.cleanup == "unresolved":
                value = self._refuse(attempt)
                return value, _Observation.saved_error(value)
            return observed.failure("internal-error"), observed

    async def close(self) -> None:
        # A cancelling SDK scope must not cancel the task owning shutdown.
        # Completion is consumed even if that scope cannot await a response.
        if self.closer is None:
            self.closer = self._task(self._close())
        await asyncio.shield(self.closer)

    async def _close(self) -> None:
        grace = sum(self.limits[key] for key in
                    ("terminate_grace_seconds", "kill_grace_seconds", "drain_grace_seconds"))
        # One total shutdown deadline covers late creation and attached cleanup.
        deadline = asyncio.get_running_loop().time() + 2 * grace + 1
        if self.active is not None and self.attempt is not None:
            self.attempt.stop.set()
        while True:
            pending = [task for task in self.retained
                       if task is not asyncio.current_task() and not task.done()]
            if not pending:
                break
            remaining = max(0, deadline - asyncio.get_running_loop().time())
            if not await _settled(pending, remaining):
                if self.attempt is not None:
                    self._refuse(self.attempt)
                break
        # Retain unresolved ownership; never cancel a spawn task that can acquire a child.
        self.retained = [task for task in self.retained if not task.done()]


class _Adapter:
    def __init__(self, profile: Profile):
        self.profile = profile
        self.children = _Children(profile)
        self.description: dict | None = None
        self.bounds: dict | None = None
        self.bootstrap_error: tuple[dict, _Observation] | None = None
        self.protocol_block: tuple[dict, _Observation] | None = None

    def _argv(self, action: str, args: dict) -> list[str]:
        argv = [self.profile.document["executable"], "workflows", action]
        if action in {"list", "search"}:
            argv.append("--offset=" + str(args.get("offset", 0)))
        elif action == "run":
            values = self._normalized(args)
            argv.extend("--" + name + "=" + str(values[name]) for name in ("limit", "offset"))
        if action == "describe" and args.get("examples", False):
            argv.append("--examples")
        argv.append("--format=json")
        if action == "search":
            argv.extend(["--", args["query"]])
        elif action in {"describe", "run"}:
            argv.extend(["--", args["workflow"]])
        return argv

    def _normalized(self, args: dict) -> dict:
        return {name: args.get("inputs", {}).get(name, rule["default"])
                for name, rule in (self.bounds or {}).items()}

    def _identity(self, value: dict) -> bool:
        expected = self.profile.document["expected"]
        if any(value.get(key) != expected[key] for key in (
            "product_version", "engine_version", "schema_version", "catalog_revision",
            "definition_digest",
        )):
            return False
        if (value["runtime_capabilities"] != [CAPABILITY]
            or value["required_capabilities"] != [CAPABILITY]
            or value["required_schema_version"] != 1):
            return False
        if value["workflow"] is None:
            return value["revision"] is None and "input_bounds" not in value
        return (value["workflow"] == WORKFLOW
                and value["revision"] == expected["workflow_revisions"][WORKFLOW])

    def _check(self, value: dict, action: str, args: dict) -> bool:
        if not _valid(_product_schema(action), value):
            return False
        incompatible = value["reason"]["code"] == "incompatible-definition-or-runtime"
        if incompatible:
            return (value["status"] == "blocked" and value["exit_code"] == 2
                    and value["availability"] == "blocked" and value["inputs"] in ({}, self._normalized(args))
                    and value["items"] == [] and value["returned_count"] == 0
                    and value["complete"] is None and value["continuation"] is None
                    and value["range"] is None and value["evidence"] == {} and "entries" not in value
                    and value["limit"] == value["inputs"].get("limit")
                    and value["offset"] == value["inputs"].get("offset")
                    and value["next_actions"] == [{"action": "inspect-configuration-or-definition"}])
        if not self._identity(value):
            return False
        code = value["exit_code"]
        if code and (value["status"] == "completed-read" or value["complete"] is not None
                     or value["continuation"] is not None):
            return False
        if code and value["inputs"] not in ({}, self._normalized(args) if action == "run" else {}):
            return False
        if value["status"] == "unknown" and code != 1:
            return False
        if value["status"] == "blocked" and value["availability"] != "blocked":
            return False
        if value["complete"] is True and value["continuation"] is not None:
            return False
        if "input_bounds" in value and self.bounds is not None and value["input_bounds"] != self.bounds:
            return False
        if action in {"list", "search"} and code == 0:
            entries = value["entries"]
            if (value["returned_count"] != len(entries) or value["items"] or value["inputs"]
                or value["evidence"] or value["range"] is not None or value["limit"] is not None
                or value["availability"] != "unknown" or value["workflow"] is not None
                or value["reason"]["code"] != "catalog-read"
                or value["offset"] != args.get("offset", 0)
                or action == "search" and value["query"] != args["query"]):
                return False
            expected = self.profile.document["expected"]["workflow_revisions"][WORKFLOW]
            if any(row["revision"] != expected for row in entries):
                return False
            if self.description and any(
                row[k] != self.description[k] for row in entries for k in ("title", "purpose")
            ):
                return False
        elif action in {"describe", "examples"} and code == 0:
            if (value["items"] or value["returned_count"] or value["evidence"]
                or value["availability"] != "unknown" or value["complete"] is not None
                or value["continuation"] is not None or value["workflow"] != WORKFLOW
                or value["reason"]["code"] != "catalog-read"):
                return False
            if action == "describe":
                if value["inputs"] != value["input_bounds"]:
                    return False
                if self.description and any(value[key] != self.description[key] for key in
                                             (*_DESCRIBE, "inputs", "input_bounds")):
                    return False
            elif value["inputs"]:
                return False
        elif action == "run":
            if (value["workflow"] != WORKFLOW or value["returned_count"] != len(value["items"])
                or value["inputs"] not in ({}, self._normalized(args))):
                return False
            if not code and (value["inputs"] != self._normalized(args)
                             or value["availability"] != "available"
                             or value["evidence"].get("binding") != "indexed-read"):
                return False
            if value["inputs"] and any(value[k] != value["inputs"][k] for k in ("limit", "offset")):
                return False
            evidence = value["evidence"]
            if ("metadata" in evidence) != ("metadata_present" in evidence):
                return False
            if "metadata" in evidence and not set(evidence["metadata"]) <= set(evidence["metadata_present"]):
                return False
            if value["range"] and value["range"]["end"] < value["range"]["start"]:
                return False
            if any(not item[k].strip() or any(ord(c) < 32 or ord(c) == 127 or c in "/\\"
                                              for c in item[k])
                   for item in value["items"] for k in ("id", "key")):
                return False
            if any(not item["name"].strip() for item in value["items"]):
                return False
            if any((item["url"] is None) != (item["url_source"] is None) for item in value["items"]):
                return False
            continuation = value["continuation"]
            if continuation and (continuation["inputs"]["offset"] <= value["inputs"]["offset"]
                                 or continuation["inputs"]["limit"] != value["inputs"]["limit"]):
                return False
        return True

    async def _invoke(self, action: str, args: dict, *, phase: str = "execute") -> tuple[dict, _Observation]:
        value, observed = await self.children.call(self._argv(action, args), run=action == "run", phase=phase)
        try:
            if value.get("status") == "adapter-error":
                if value["error"]["code"] == "incompatible-output":
                    observed = replace(observed, dispatch_blocked=True)
                    value = {**value, "error": {**value["error"], "dispatch_blocked": True}}
                    self.protocol_block = value, observed
                return value, observed
            view = "examples" if action == "describe" and args.get("examples") else action
            if not self._check(value, view, args):
                observed = replace(observed, dispatch_blocked=True)
                self.protocol_block = observed.failure("incompatible-output"), observed
                return self.protocol_block
            if value["reason"]["code"] == "incompatible-definition-or-runtime":
                observed = replace(observed, dispatch_blocked=True)
                self.protocol_block = value, observed
            return value, observed
        except Exception:  # noqa: BLE001
            # Postexecution validation failures retain this call's evidence, never raw details.
            return observed.failure("internal-error"), observed

    async def bootstrap(self) -> None:
        listing, listing_observed = await self._invoke("list", {}, phase="bootstrap")
        if listing.get("status") == "adapter-error" or listing.get("exit_code") != 0:
            self.bootstrap_error = listing, listing_observed
            return
        if (listing["complete"] is not True or listing["continuation"] is not None
            or [row["id"] for row in listing["entries"]] != [WORKFLOW]):
            listing_observed = replace(listing_observed, dispatch_blocked=True)
            self.bootstrap_error = listing_observed.failure("incompatible-catalog"), listing_observed
            return
        description, description_observed = await self._invoke("describe", {"workflow": WORKFLOW}, phase="bootstrap")
        if description.get("status") == "adapter-error" or description.get("exit_code") != 0:
            self.bootstrap_error = description, description_observed
            return
        bounds = description["inputs"]
        if (any(bounds["limit"][key] != expected for key, expected in
                (("minimum", 1), ("maximum", 100), ("default", 25)))
            or any(bounds["offset"][key] != expected for key, expected in
                   (("minimum", 0), ("maximum", MAX_OFFSET), ("default", 0)))
            or any(listing["entries"][0][key] != description[key] for key in ("title", "purpose"))):
            description_observed = replace(description_observed, dispatch_blocked=True)
            self.bootstrap_error = description_observed.failure("incompatible-catalog"), description_observed
            return
        self.description = description
        self.bounds = bounds

    def _result(self, value: dict, action: str, observed: _Observation | None = None) -> types.CallToolResult:
        observed = observed if observed is not None else _Observation()
        schema = _output_schema(action)
        if not _valid(schema, value):
            value = observed.failure("incompatible-output")
        result = types.CallToolResult(
            content=[types.TextContent(type="text", text=_json(value))],
            structured_content=value,
            is_error=value.get("status") == "adapter-error" or value.get("exit_code", 0) != 0,
        )
        if len(result.model_dump_json(by_alias=True).encode()) > 4 * 1048576:
            return self._result(observed.failure("output-limit"), action, observed)
        return result

    async def list_tools(self, _ctx: ServerRequestContext, params: Any) -> Any:
        if params is not None and params.cursor is not None:
            return types.ErrorData(code=-32602, message="Tool list has no cursor")
        descriptions = (
            "List installed supported tasks. This does not check account availability.",
            "Search installed task descriptions using ordinary words; no task execution.",
            "Describe an installed task's inputs, prerequisites and examples; no execution.",
            (
                "Run one bounded read through the fixed installed product and approved context. "
                "Inspect completeness and continuation; a successful call need not be exhaustive."
            ),
        )
        tools = []
        for name, description in zip(TOOLS, descriptions):
            action = name.removeprefix("workflows_")
            output = _output_schema(action)
            if action == "describe":
                output = {"type": "object", "anyOf": [output, _output_schema("examples")]}
            schema = _inputs(action, self.bounds)
            StrictValidator.check_schema(schema)
            StrictValidator.check_schema(output)
            tools.append(types.Tool(name=name, description=description, input_schema=schema,
                                    output_schema=output,
                                    annotations=types.ToolAnnotations(read_only_hint=True,
                                                                      destructive_hint=False)))
        return types.ListToolsResult(tools=tools)

    async def call_tool(self, _ctx: ServerRequestContext, params: Any) -> Any:
        if params.name not in TOOLS:
            return types.ErrorData(code=-32602, message="Unknown workflow tool")
        action = params.name.removeprefix("workflows_")
        args = {} if params.arguments is None else params.arguments
        output_action = "examples" if action == "describe" and args.get("examples") is True else action
        observed = _Observation()
        try:
            if len(_json(args).encode()) > 16384:
                return self._result(_failure("invalid-input"), output_action)
            if "workflow" in args and isinstance(args["workflow"], str) and args["workflow"] != WORKFLOW:
                return self._result(_failure("unsupported-workflow"), output_action)
            if (not _valid(_inputs(action, self.bounds), args)
                or action == "search" and (not args["query"].strip() or "\0" in args["query"])):
                return self._result(_failure("invalid-input"), output_action)
            if self.children.blocked is not None:
                observed = _Observation.saved_error(self.children.blocked)
                return self._result(self.children.blocked, output_action, observed)
            refusal = self.protocol_block or self.bootstrap_error
            if refusal is not None:
                value, observed = refusal
                return self._result(value, output_action, observed)
            value, observed = await self._invoke(action, args)
            return self._result(value, output_action, observed)
        except (ValueError, TypeError, RecursionError):
            code = "invalid-input" if observed.cleanup == "not-started" else "internal-error"
            return self._result(observed.failure(code), output_action, observed)
        except Exception:  # noqa: BLE001
            # The public SDK boundary must not expose exception text from internal failures.
            return self._result(observed.failure("internal-error"), output_action, observed)


async def create_server(profile: Profile) -> Server:
    """Construct four public MCP tools after bounded, credential-free CLI bootstrap.

    Run this Server within its SDK lifespan (Client or Server.run) for owned cleanup.
    """
    adapter = _Adapter(profile)
    try:
        await adapter.bootstrap()
    except BaseException:
        await adapter.children.close()
        raise

    @asynccontextmanager
    async def lifespan(_server: Server):
        try:
            yield None
        finally:
            await adapter.children.close()

    return Server(profile.document["adapter_id"], on_list_tools=adapter.list_tools,
                  on_call_tool=adapter.call_tool, lifespan=lifespan)


async def _stdio(profile: Profile) -> int:
    # Keep the adapter handle at this boundary so unresolved shutdown is a nonzero exit.
    adapter = _Adapter(profile)
    try:
        await adapter.bootstrap()
        server = Server(profile.document["adapter_id"], on_list_tools=adapter.list_tools,
                        on_call_tool=adapter.call_tool)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        await adapter.children.close()
    return 1 if adapter.children.blocked is not None else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Private installed-workflow stdio MCP adapter")
    parser.add_argument("--profile", required=True)
    args = parser.parse_args()
    try:
        profile = load_profile(args.profile)
    except ValueError:
        print("workflow-mcp: invalid-profile", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_stdio(profile))
    except KeyboardInterrupt:
        return 1
    except Exception:  # noqa: BLE001
        # The process boundary emits only a fixed category, never an SDK/runtime traceback.
        print("workflow-mcp: internal-error", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
