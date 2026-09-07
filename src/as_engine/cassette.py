"""Scrubbed JSON recordings and strict offline playback at the transport seam."""
# Malformed cassette input follows the surface ValueError contract.
# ruff: noqa: TRY004

from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
from collections.abc import Iterable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

from .index import Operation
from .transport import Response, Transport


def _json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _hash(body: Any) -> str:
    return hashlib.sha256(_json(body).encode()).hexdigest()


class Scrubber:
    """Register literal sensitive values; never serialize the replacement map.

    Structured sensitive fields are discovered before replacing free text. Register
    unlabelled secrets yourself; arbitrary prose cannot reliably identify a token.
    """

    def __init__(
        self,
        *,
        secrets: Iterable[str] = (),
        base_url: str | None = None,
        cloud_id: str | None = None,
    ) -> None:
        self._replacements: dict[str, str] = {}
        self._counts: dict[str, int] = {}
        if base_url:
            self.register(base_url.rstrip("/"), "site")
        if cloud_id:
            self.register(cloud_id, "cloud")
        for secret in secrets:
            self.register(secret)

    def register(self, value: str, kind: str = "secret") -> None:
        if not value or value in self._replacements or re.fullmatch(r"<as-[a-z]+-\d+>", value):
            return
        self._counts[kind] = self._counts.get(kind, 0) + 1
        placeholder = f"<as-{kind}-{self._counts[kind]}>"
        for variant in (value, quote(value, safe=""), quote_plus(value), json.dumps(value)[1:-1]):
            self._replacements.setdefault(variant, placeholder)

    def discover(self, value: Any, parent: str = "") -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                name = re.sub(r"[-_]", "", str(key)).casefold()
                kind = None
                if name in ("authorization", "proxyauthorization", "cookie", "setcookie"):
                    kind = "secret"
                elif name in ("email", "emailaddress"):
                    kind = "email"
                elif name == "accountid":
                    kind = "account"
                elif name in ("cloudid",):
                    kind = "cloud"
                elif name in (
                    "token",
                    "apitoken",
                    "accesstoken",
                    "refreshtoken",
                    "password",
                    "secret",
                ):
                    kind = "secret"
                elif parent == "_links" and key == "base":
                    kind = "site"
                if kind and isinstance(child, str):
                    self.register(child, kind)
                    # A server can echo the bare credential separately from its header.
                    if name in ("authorization", "proxyauthorization"):
                        scheme, _, credential = child.partition(" ")
                        self.register(credential)
                        if scheme.casefold() == "basic":
                            try:
                                decoded = base64.b64decode(credential, validate=True).decode()
                            except (ValueError, UnicodeError):
                                pass
                            else:
                                self.register(decoded)
                                for part in decoded.split(":", 1):
                                    self.register(part)
                self.discover(child, str(key))
        elif isinstance(value, (list, tuple)):
            for child in value:
                self.discover(child, parent)

    def scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            # One substitution pass prevents substitutions inside placeholders.
            if not self._replacements:
                return value
            pattern = "|".join(
                re.escape(key) for key in sorted(self._replacements, key=len, reverse=True)
            )
            return re.sub(pattern, lambda match: self._replacements[match.group()], value)
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, child in value.items():
                if not isinstance(key, str):
                    raise ValueError("cassette JSON object keys must be strings")
                clean_key = self.scrub(key)
                if clean_key in result:
                    raise ValueError("scrubbing would merge cassette object keys")
                result[clean_key] = self.scrub(child)
            return result
        if isinstance(value, (list, tuple)):
            return [self.scrub(child) for child in value]
        return value


def _key(entry: Mapping[str, Any]) -> str:
    return _json([entry["operationId"], entry["parameters"], entry["body_sha256"]])


def _load(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("format_version") != 1:
            raise ValueError("unsupported cassette format_version")
        entries = data["interactions"]
        if not isinstance(entries, list):
            raise ValueError("cassette interactions must be an array")
        keys: set[str] = set()
        for entry in entries:
            if not isinstance(entry["operationId"], str) or not isinstance(
                entry["parameters"], dict
            ):
                raise ValueError("invalid cassette request")
            if entry["body_sha256"] != _hash(entry["body"]):
                raise ValueError("cassette request body hash mismatch")
            response = entry["response"]
            status = response["status"]
            if type(status) is not int or not 100 <= status <= 599:
                raise ValueError("invalid cassette response status")
            if not isinstance(response["headers"], dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in response["headers"].items()
            ):
                raise ValueError("invalid cassette response headers")
            _json(response["body"])
            key = _key(entry)
            if key in keys:
                raise ValueError("duplicate cassette request")
            keys.add(key)
        return entries
    except (KeyError, TypeError, IndexError) as exc:
        raise ValueError("invalid cassette structure") from exc


class Recorder:
    """Wrap a transport and atomically write a named, single-writer session.

    A recording starts at a new path; contradictory responses for the same
    scrubbed request are refused. Only scrubbed bytes reach temporary files.
    """

    def __init__(
        self, transport: Transport, path: str | Path, *, scrubber: Scrubber | None = None
    ) -> None:
        self.transport = transport
        self.path = Path(path)
        self.scrubber = scrubber or Scrubber()
        if self.path.exists():
            raise ValueError("cassette recording path already exists; choose a new session path")
        self._entries: list[dict[str, Any]] = []

    def call(self, operation: Operation, parameters: Mapping[str, Any], body: Any) -> Response:
        # Validate JSON before sending, and snapshot before a wrapped transport mutates inputs.
        request = deepcopy(
            {"operationId": operation.operationId, "parameters": dict(parameters), "body": body}
        )
        _json(request)
        response = self.transport.call(operation, parameters, body)
        entry = {
            **request,
            "response": {
                "status": response.status,
                "headers": dict(response.headers),
                "body": deepcopy(response.body),
            },
        }
        pending = [*self._entries, entry]
        self.scrubber.discover(pending)
        clean = self.scrubber.scrub(pending)
        unique: dict[str, dict[str, Any]] = {}
        for item in clean:
            item["body_sha256"] = _hash(item["body"])
            key = _key(item)
            if key in unique and unique[key]["response"] != item["response"]:
                raise ValueError("cassette has conflicting responses for " + operation.operationId)
            unique[key] = item
        payload = _json({"format_version": 1, "interactions": list(unique.values())}) + "\n"
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        # Retain raw values in memory to scrub prior echoes discovered by later calls.
        self._entries = pending
        return response

    def close(self) -> None:
        close = getattr(self.transport, "close", None)
        if close:
            close()


class Player:
    """Exact, reusable offline lookup. No network transport or fallback exists."""

    def __init__(self, path: str | Path, *, scrubber: Scrubber | None = None) -> None:
        self.scrubber = scrubber or Scrubber()
        self._entries = {_key(entry): entry for entry in _load(Path(path))}

    def call(self, operation: Operation, parameters: Mapping[str, Any], body: Any) -> Response:
        request = {
            "operationId": operation.operationId,
            "parameters": dict(parameters),
            "body": body,
        }
        self.scrubber.discover(request)
        request = self.scrubber.scrub(request)
        request["body_sha256"] = _hash(request["body"])
        entry = self._entries.get(_key(request))
        if entry is None:
            # Name the operation, parameter names and scrubbed body hash, never raw values.
            raise ValueError(
                f"cassette miss: {operation.operationId}; parameters={sorted(parameters)}; "
                f"body_sha256={request['body_sha256']}"
            )
        response = deepcopy(entry["response"])
        return Response(response["status"], response["body"], response["headers"])

    def close(self) -> None:
        """No resources to release."""
