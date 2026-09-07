"""Runtime readers for compiled operation indexes."""
# Malformed serialized input is reported consistently as ValueError.
# ruff: noqa: TRY004

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NO_EXAMPLE = object()


@dataclass(frozen=True)
class Operation:
    operationId: str
    method: str
    path: str
    tags: list[str]
    summary: str | None
    description: str | None
    parameters: list[dict[str, Any]]
    requestBody: dict[str, Any] | None
    response_200: str | None
    extensions: dict[str, Any]
    reachable_schemas: list[str]
    response_schema: dict[str, Any] | None = None
    response_example: Any = NO_EXAMPLE
    deprecated: bool = False
    request_body_required: bool = False
    request_media_types: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OperationIndex:
    operations: dict[str, Operation]
    schemas: dict[str, Any]


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"index must be an object: {path}")
    return value


def load_index(path: str | Path) -> OperationIndex:
    value = _read_json(Path(path))
    if value.get("format_version") != 1:
        raise ValueError("unsupported index format_version")
    raw_operations = value.get("operations")
    schemas = value.get("schemas")
    if not isinstance(raw_operations, dict) or not isinstance(schemas, dict):
        raise ValueError("index requires operations and schemas objects")
    operations: dict[str, Operation] = {}
    for operation_id, record in raw_operations.items():
        if not isinstance(record, dict):
            raise ValueError(f"operation record must be an object: {operation_id}")
        try:
            operation = Operation(**record)
        except TypeError as exc:
            raise ValueError(f"invalid operation record: {operation_id}") from exc
        if operation.operationId != operation_id:
            raise ValueError(f"operation id does not match record key: {operation_id}")
        operations[operation_id] = operation
    return OperationIndex(operations=operations, schemas=schemas)


class ProductIndexes:
    """Load primary product indexes eagerly and lower tiers only on request."""

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)
        catalog = _read_json(self._directory / "catalog.json")
        if catalog.get("format_version") != 1 or not isinstance(catalog.get("documents"), list):
            raise ValueError("unsupported catalog")
        self._entries: dict[str, dict[str, str]] = {}
        self._loaded: dict[str, OperationIndex] = {}
        for entry in catalog["documents"]:
            if not isinstance(entry, dict) or not all(
                isinstance(entry.get(key), str) for key in ("id", "tier", "file")
            ):
                raise ValueError("invalid catalog document entry")
            document_id = entry["id"]
            if document_id in self._entries:
                raise ValueError(f"duplicate catalog document id: {document_id}")
            self._entries[document_id] = entry
            if entry["tier"] == "primary":
                self._loaded[document_id] = load_index(self._safe_path(entry["file"]))

    def _safe_path(self, filename: str) -> Path:
        candidate = (self._directory / filename).resolve()
        try:
            candidate.relative_to(self._directory.resolve())
        except ValueError as exc:
            raise ValueError(f"catalog path escapes index directory: {filename!r}") from exc
        return candidate

    def get(self, document_id: str) -> OperationIndex:
        if document_id not in self._entries:
            raise KeyError(document_id)
        if document_id not in self._loaded:
            self._loaded[document_id] = load_index(
                self._safe_path(self._entries[document_id]["file"])
            )
        return self._loaded[document_id]

    def primary(self) -> list[tuple[str, OperationIndex]]:
        """Primary indexes only, independent of previously loaded lower tiers."""
        return [
            (key, self.get(key))
            for key, entry in self._entries.items()
            if entry["tier"] == "primary"
        ]

    def find(self, name: str) -> tuple[str, OperationIndex, Operation]:
        """Resolve exact canonical IDs before aliases, loading lower tiers on demand."""
        from .params import kebab_case

        for primary in (True, False):
            candidates = [
                (key, self.get(key))
                for key, entry in self._entries.items()
                if (entry["tier"] == "primary") == primary
            ]
            exact = [
                (key, index, index.operations[name])
                for key, index in candidates
                if name in index.operations
            ]
            if exact:
                if len(exact) != 1:
                    raise ValueError(f"ambiguous operationId: {name}")
                return exact[0]
            aliases = [
                (key, index, op)
                for key, index in candidates
                for op in index.operations.values()
                if kebab_case(op.operationId) == name
            ]
            if len(aliases) > 1:
                raise ValueError(f"ambiguous operation alias: {name}")
            if aliases:
                return aliases[0]
        raise KeyError(name)
