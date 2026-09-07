"""Product build seam: validate sources and write deterministic indexes."""
# Malformed serialized input is reported consistently as ValueError.
# ruff: noqa: TRY004

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from .compiler import compile_document


def _inside(root: Path, candidate: str) -> Path:
    path = (root / candidate).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"manifest path escapes spec directory: {candidate!r}") from exc
    return path


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        raise ValueError(f"missing product source: {path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path.name}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    with NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(data)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def compile_product(spec_dir: str | Path, out_dir: str | Path) -> dict[str, Any]:
    """Build indexes described by ``manifest.json`` and return the catalog."""
    source_root = Path(spec_dir).resolve()
    destination = Path(out_dir)
    manifest = _read_json(source_root / "manifest.json")
    documents = manifest.get("documents")
    if manifest.get("format_version") != 1 or not isinstance(documents, list):
        raise ValueError("manifest must have format_version 1 and documents")
    compiled: list[tuple[str, dict[str, Any]]] = []
    catalog_documents: list[dict[str, str]] = []
    ids: set[str] = set()
    for entry in documents:
        if not isinstance(entry, dict):
            raise ValueError("manifest document entry must be an object")
        document_id = entry.get("id")
        filename = entry.get("file")
        declared_version = entry.get("declared_version")
        expected_hash = entry.get("sha256")
        if not all(
            isinstance(value, str) and value
            for value in (document_id, filename, declared_version, expected_hash)
        ):
            raise ValueError("manifest document requires id, file, declared_version, and sha256")
        assert isinstance(document_id, str)
        assert isinstance(filename, str)
        assert isinstance(declared_version, str)
        assert isinstance(expected_hash, str)
        if document_id in ids:
            raise ValueError(f"duplicate manifest document id: {document_id}")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", document_id):
            raise ValueError(f"unsafe manifest document id: {document_id!r}")
        ids.add(document_id)
        document_path = _inside(source_root, filename)
        raw = document_path.read_bytes()
        actual_hash = hashlib.sha256(raw).hexdigest()
        if actual_hash != expected_hash:
            raise ValueError(f"sha256 mismatch for {filename}")
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise ValueError(f"JSON object required: {filename}")
        info = document.get("info")
        actual_version = info.get("version") if isinstance(info, dict) else None
        if actual_version != declared_version:
            raise ValueError(f"declared_version mismatch for {filename}")
        overlay_values: list[dict[str, Any]] = []
        overlays = entry.get("overlays", [])
        if not isinstance(overlays, list) or not all(isinstance(item, str) for item in overlays):
            raise ValueError("manifest overlays must be a list of paths")
        for overlay_name in overlays:
            overlay_values.append(_read_json(_inside(source_root, overlay_name)))
        strip_extensions = entry.get("strip_extensions", ["x-atlassian-narrative"])
        if not isinstance(strip_extensions, list) or not all(
            isinstance(item, str) for item in strip_extensions
        ):
            raise ValueError("manifest strip_extensions must be a list of strings")
        index = compile_document(document, overlay_values, strip_extensions=strip_extensions)
        output_name = f"{document_id}.index.json"
        tier = entry.get("tier", "primary")
        if tier not in ("primary", "lower"):
            raise ValueError("manifest tier must be primary or lower")
        catalog_documents.append({"id": document_id, "tier": tier, "file": output_name})
        compiled.append((output_name, index))
    catalog = {"format_version": 1, "documents": catalog_documents}
    # No output is changed until every source, overlay, hash, and index validates.
    destination.mkdir(parents=True, exist_ok=True)
    for output_name, index in compiled:
        _write_json(destination / output_name, index)
    _write_json(destination / "catalog.json", catalog)
    return catalog
