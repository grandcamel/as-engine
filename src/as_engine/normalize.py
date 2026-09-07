"""Normalization hooks run between enrichment and compilation."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from copy import deepcopy
from typing import Any

Normalizer = Callable[[dict[str, Any]], dict[str, Any] | None]


def normalize_document(
    document: dict[str, Any],
    *,
    normalizers: Iterable[Normalizer] = (),
    strip_extensions: Iterable[str] = ("x-atlassian-narrative",),
) -> dict[str, Any]:
    """Copy, strip explicitly named top-level extensions, then run hooks."""
    result = deepcopy(document)
    for extension in strip_extensions:
        if not isinstance(extension, str) or not extension.startswith("x-"):
            raise ValueError("only top-level x- extensions may be stripped")
        result.pop(extension, None)
    for normalizer in normalizers:
        normalized = normalizer(result)
        if normalized is not None:
            if not isinstance(normalized, dict):
                raise ValueError("normalizer must return a document or None")
            result = normalized
    return result
