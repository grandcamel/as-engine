"""Stable Generic Surface errors, independent of a product's exception hints."""

from __future__ import annotations

import json
from typing import Any

from assistant_skills_lib.error_handler import (  # type: ignore[import-untyped]
    BaseAPIError,
    sanitize_error_message,
)


def messages_from(data: Any) -> list[str]:
    """Collect v1/v2 and generic API message fields without dropping later errors."""
    if isinstance(data, str):
        try:
            return messages_from(json.loads(data))
        except (ValueError, TypeError):
            return [sanitize_error_message(data)] if data else []
    if isinstance(data, list):
        return [message for value in data for message in messages_from(value)]
    if isinstance(data, dict):
        found = []
        for key in (
            "errorMessages",
            "errors",
            "message",
            "errorMessage",
            "error",
            "title",
            "detail",
            "translation",
            "data",
        ):
            if key in data:
                found.extend(messages_from(data[key]))
        if not found:
            for value in data.values():
                found.extend(messages_from(value))
        return list(dict.fromkeys(found))
    return []


class SurfaceError(Exception):
    """One JSON error object and a stable process exit code."""

    def __init__(
        self,
        status: int | None,
        messages: list[str],
        operation: str | None = None,
        note: Any = None,
        *,
        code: int | None = None,
    ):
        self.status = status
        self.messages = messages
        self.operation = operation
        self.note = note
        self.code = code if code is not None else exit_code(status)
        super().__init__("; ".join(messages))

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "messages": self.messages,
            "operation": self.operation,
            "note": self.note,
        }

    @classmethod
    def from_domain(cls, error: BaseAPIError) -> SurfaceError:
        return cls(
            error.status_code,
            messages_from(error.response_data) or [sanitize_error_message(error.message)],
            error.operation,
        )


def exit_code(status: int | None) -> int:
    if status == 400:
        return 2
    if status == 401:
        return 3
    if status == 403:
        return 4
    if status == 404:
        return 5
    if status == 409:
        return 7
    if status == 429 or status is not None and status >= 500:
        return 6
    return 1
