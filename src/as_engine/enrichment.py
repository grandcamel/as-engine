"""Validation and executable examples for product-owned OpenAPI overlays."""
# ruff: noqa: TRY004

from __future__ import annotations

import json
import shlex
from collections.abc import Callable, Iterator
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .overlay import _parent, _target_steps, apply_overlay

BodyValidator = Callable[[Any, dict[str, Any], dict[str, Any]], None]
_REQUIRED = ("description", "x-as-reason", "x-as-origin", "x-as-test")


def _error(source: str, target: Any, message: str) -> ValueError:
    return ValueError(f"{source}: target {target}: {message}")


def _nonempty_string(action: dict[str, Any], name: str, source: str, target: Any) -> str:
    value = action.get(name)
    if not isinstance(value, str) or not value.strip():
        if name not in action:
            raise _error(source, target, f"missing required field {name}")
        raise _error(source, target, f"invalid field {name}: expected nonempty string")
    return value


def _valid_url(value: Any) -> bool:
    if not isinstance(value, str) or any(char.isspace() for char in value):
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def validate_overlay(overlay: dict[str, Any], *, source: str = "<overlay>") -> None:
    """Validate the provenance contract required for every overlay action."""
    actions = overlay.get("actions") if isinstance(overlay, dict) else None
    if not isinstance(actions, list):
        raise ValueError(f"{source}: actions must be a list")
    ids: set[str] = set()
    for action in actions:
        if not isinstance(action, dict):
            raise ValueError(f"{source}: action must be an object")
        target = action.get("target")
        for name in _REQUIRED:
            value = _nonempty_string(action, name, source, target)
            if name == "x-as-test":
                if value in ids:
                    raise _error(source, target, f"duplicate x-as-test {value!r}")
                ids.add(value)
        evidence = action.get("x-as-evidence")
        if not isinstance(evidence, dict):
            if "x-as-evidence" not in action:
                raise _error(source, target, "missing required field x-as-evidence")
            raise _error(source, target, "invalid field x-as-evidence: expected object")
        if not _valid_url(evidence.get("url")):
            raise _error(source, target, "invalid field x-as-evidence.url: expected http(s) URL")
        evidence_date = evidence.get("date")
        if not isinstance(evidence_date, str):
            raise _error(source, target, "invalid field x-as-evidence.date: expected YYYY-MM-DD")
        try:
            if date.fromisoformat(evidence_date).isoformat() != evidence_date:
                raise ValueError
        except ValueError as exc:
            raise _error(
                source, target, "invalid field x-as-evidence.date: expected YYYY-MM-DD"
            ) from exc


def _resolve_pointer(document: dict[str, Any], ref: Any) -> dict[str, Any]:
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise ValueError(f"example schema must be a local reference: {ref!r}")
    value: Any = document
    try:
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            value = value[int(part)] if isinstance(value, list) and part.isdigit() else value[part]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"example schema does not resolve: {ref!r}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"example schema does not resolve to an object: {ref!r}")
    return value


def _check_examples(
    action: dict[str, Any], document: dict[str, Any], validate_body: BodyValidator | None
) -> tuple[Any, ...]:
    examples = action.get("x-as-examples", [])
    if not isinstance(examples, list):
        raise ValueError("x-as-examples must be a list")
    parsed: list[Any] = []
    for example in examples:
        if not isinstance(example, dict) or set(example) - {"kind", "value", "schema"}:
            raise ValueError("x-as-examples entries must be objects with kind and value")
        kind, value = example.get("kind"), example.get("value")
        if kind == "invocation":
            if not isinstance(value, str):
                raise ValueError("invocation example value must be a string")
            try:
                tokens = shlex.split(value)
            except ValueError as exc:
                raise ValueError("invocation example value is invalid shell syntax") from exc
            if not tokens:
                raise ValueError("invocation example value must contain an argv command")
            if "schema" in example:
                raise ValueError("invocation example cannot declare schema")
            parsed.append(tokens)
        elif kind == "json":
            if not isinstance(value, str):
                raise ValueError("json example value must be a JSON string")
            try:
                body = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("json example value is invalid JSON") from exc
            schema_ref = example.get("schema")
            if "schema" in example:
                schema = _resolve_pointer(document, schema_ref)
                if validate_body is None:
                    raise ValueError(
                        "json example schema validation requires validate_body callback"
                    )
                validate_body(body, schema, document)
            parsed.append(body)
        else:
            raise ValueError("x-as-examples kind must be invocation or json")
    return tuple(parsed)


def _target_value(document: dict[str, Any], target: str) -> tuple[Any, Any, Any]:
    steps = _target_steps(target)
    if not steps:
        return document, None, None
    parent, (kind, key) = _parent(document, steps, target)
    if (kind == "key" and (not isinstance(parent, dict) or key not in parent)) or (
        kind == "index"
        and (not isinstance(parent, list) or not isinstance(key, int) or key >= len(parent))
    ):
        raise ValueError(f"overlay target does not exist: {target!r}")
    return parent[key], parent, key


def _expected_merge(existing: Any, update: Any) -> Any:
    """The observable Overlay merge rule, kept independent from the applier."""
    if isinstance(existing, dict) and isinstance(update, dict):
        merged = deepcopy(existing)
        for key, value in update.items():
            merged[key] = (
                _expected_merge(existing[key], value) if key in existing else deepcopy(value)
            )
        return merged
    if isinstance(existing, list) and isinstance(update, list):
        return [*deepcopy(existing), *deepcopy(update)]
    return deepcopy(update)


@dataclass(frozen=True)
class EntryCase:
    """One overlay action with its base document and ordered predecessors."""

    id: str
    document: dict[str, Any]
    before_document: dict[str, Any]
    preceding_actions: tuple[dict[str, Any], ...]
    action: dict[str, Any]
    source: str

    def check(self, validate_body: BodyValidator | None = None) -> tuple[Any, ...]:
        """Check target and merge semantics independently, returning parsed examples."""
        target = self.action.get("target")
        if not isinstance(target, str):
            raise ValueError(f"{self.source}: target must be a string")
        _target_value(deepcopy(self.document), target)
        context = deepcopy(self.before_document)
        before, _parent_context, _key_context = _target_value(context, target)
        expected = deepcopy(context)
        _expected_before, parent, key = _target_value(expected, target)
        if self.action.get("remove") is True:
            if parent is None:
                raise ValueError("cannot remove the overlay document root")
            del parent[key]
        else:
            if "update" not in self.action:
                raise ValueError("overlay action must contain exactly one of update or remove")
            if parent is None:
                if not isinstance(self.action["update"], dict):
                    raise ValueError("root overlay update must be an object")
                expected = _expected_merge(expected, self.action["update"])
            else:
                parent[key] = _expected_merge(before, self.action["update"])
        actual = apply_overlay(context, {"actions": [self.action]})
        if actual != expected:
            raise AssertionError(f"{self.source}: action {self.id} did not produce expected merge")
        return _check_examples(self.action, actual, validate_body)


def entry_cases(spec_dir: str | Path) -> Iterator[EntryCase]:
    """Yield one independently-checkable case per manifest overlay action."""
    root = Path(spec_dir).resolve()

    def inside(candidate: str) -> Path:
        path = (root / candidate).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"manifest path escapes spec directory: {candidate!r}") from exc
        return path

    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    documents = manifest.get("documents") if isinstance(manifest, dict) else None
    if not isinstance(documents, list):
        raise ValueError("manifest must have documents")
    ids: set[str] = set()
    for entry in documents:
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
            raise ValueError("manifest document requires file")
        document = json.loads(inside(entry["file"]).read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError(f"JSON object required: {entry['file']}")
        prior: list[dict[str, Any]] = []
        before_document = deepcopy(document)
        overlays = entry.get("overlays", [])
        if not isinstance(overlays, list) or not all(isinstance(item, str) for item in overlays):
            raise ValueError("manifest overlays must be a list of paths")
        for name in overlays:
            overlay = json.loads(inside(name).read_text(encoding="utf-8"))
            if not isinstance(overlay, dict):
                raise ValueError(f"JSON object required: {name}")
            validate_overlay(overlay, source=name)
            for action in overlay["actions"]:
                test_id = action["x-as-test"]
                if test_id in ids:
                    raise ValueError(
                        f"{name}: target {action.get('target')}: duplicate x-as-test {test_id!r}"
                    )
                ids.add(test_id)
                yield EntryCase(
                    test_id,
                    deepcopy(document),
                    deepcopy(before_document),
                    tuple(prior),
                    deepcopy(action),
                    name,
                )
                prior.append(deepcopy(action))
                before_document = apply_overlay(before_document, {"actions": [action]})
