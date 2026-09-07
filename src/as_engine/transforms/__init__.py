"""Ordered, per-Surface transform registry and public hook context."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..index import Operation, OperationIndex
from ..transport import Response


class Invoke(Protocol):
    def __call__(
        self,
        name: str,
        parameters: Mapping[str, Any],
        body: Any = None,
        /,
        *,
        all_pages: bool = False,
    ) -> Response: ...


@dataclass
class Context:
    document: str
    index: OperationIndex
    operation: Operation
    parameters: dict[str, Any]
    body: Any
    aliases: Mapping[str, str]
    all_pages: bool
    limit: int | None
    invoke: Invoke
    send: Callable[[Mapping[str, Any], Any], Response]
    origin: Callable[[], str | None]
    warn: Callable[[str], None] | None = None
    state: dict[str, Any] = field(default_factory=dict)
    scope_allowlist: tuple[str, ...] = ()
    scope_allow_site: bool = False
    scope_argv_identity: str | None = None
    scope_resolution_rules: Mapping[str, tuple[tuple[str, ...], ...]] = field(default_factory=dict)
    scope_send: Callable[[Operation, Mapping[str, Any], Any], Response] | None = None

    def resolve_scope(self, operation_id: str, parameters: Mapping[str, Any]) -> Response:
        """Issue a bounded metadata read under consumer-supplied resolution policy."""
        from .scope import resolution_read

        return resolution_read(self, operation_id, parameters)


class Transform:
    """Subclass either hook; instances must keep call state in context.state."""

    def request(self, context: Context, tag: Any) -> None:
        pass

    def response(self, context: Context, tag: Any, response: Response) -> Response:
        return response


class Registry:
    def __init__(self) -> None:
        self._entries: dict[str, tuple[int, Transform]] = {}

    def register(self, tag: str, transform: Transform, *, order: int) -> None:
        if tag in self._entries:
            raise ValueError(f"transform already registered: {tag}")
        self._entries[tag] = (order, transform)

    def selected(self, operation: Operation) -> list[tuple[str, Transform]]:
        return [
            (tag, entry[1])
            for tag, entry in sorted(self._entries.items(), key=lambda item: (item[1][0], item[0]))
            if tag in operation.extensions
        ]


def default_registry() -> Registry:
    from .paging import Paging
    from .prerequisites import Prerequisites
    from .scope import Scope
    from .version import Version

    registry = Registry()
    registry.register("x-as-scope", Scope(), order=5)
    registry.register("x-as-prerequisites", Prerequisites(), order=10)
    registry.register("x-as-version", Version(), order=20)
    registry.register("x-as-paging", Paging(), order=100)
    return registry
