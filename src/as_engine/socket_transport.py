"""Credential-free operation transport for a Split Mode sidecar."""

from __future__ import annotations

import socket
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .errors import ScopeRefusal, SurfaceError, exit_code
from .index import Operation
from .serve import (
    MAX_BYTES,
    WireError,
    check_limits,
    check_operation,
    decode_frame,
    encode_frame,
    read_line,
    tcp_address,
    token_prelude,
)
from .transport import Response


def _raise_error(error: Any) -> None:
    if (
        not isinstance(error, dict)
        or set(error) - {"kind", "message", "status", "code"}
        or not isinstance(error.get("kind"), str)
        or not isinstance(error.get("message"), str)
    ):
        raise WireError("protocol", "Invalid socket error response")
    status, kind, message = error.get("status"), error["kind"], error["message"]
    code = error.get("code", exit_code(status) if type(status) is int else 1)
    if (
        (status is not None and (type(status) is not int or not 100 <= status <= 599))
        or type(code) is not int
        or not 1 <= code <= 7
    ):
        raise WireError("protocol", "Invalid socket error status or exit code")
    if kind == "scope" and status is None:
        raise ScopeRefusal(message)
    if status is None:
        raise SurfaceError(None, [message], code=code)
    from assistant_skills_lib import error_handler  # type: ignore[import-untyped]

    classes = {
        400: error_handler.ValidationError,
        401: error_handler.AuthenticationError,
        403: error_handler.PermissionError,
        404: error_handler.NotFoundError,
        409: error_handler.ConflictError,
        429: error_handler.RateLimitError,
    }
    cls = classes.get(
        status, error_handler.ServerError if status >= 500 else error_handler.BaseAPIError
    )
    raise cls(message=message, status_code=status)


class SocketTransport:
    """One connection per call; no Jira credential or implicit retry is held.

    ``endpoint`` is a Unix path or a (literal loopback host, port, session token)
    tuple. ``document`` is an optional authoritative catalog ID, not a pathname.
    """

    def __init__(
        self,
        endpoint: str | Path | tuple[str, int, str],
        *,
        document: str | None = None,
        timeout: float = 30,
        max_bytes: int = MAX_BYTES,
    ) -> None:
        check_limits(timeout, max_bytes)
        self.timeout, self.max_bytes, self.document = timeout, max_bytes, document
        self._prelude: bytes | None = None
        if isinstance(endpoint, tuple):
            host, port, token = endpoint
            self.endpoint: str | tuple[str, int] = tcp_address((host, port))
            if port == 0:
                raise ValueError("Socket client TCP port must be positive")
            self.family = socket.AF_INET6 if ":" in host else socket.AF_INET
            self._prelude = token_prelude(token)
        else:
            self.endpoint = str(endpoint)
            if not self.endpoint:
                raise ValueError("Socket client requires a Unix socket path")
            self.family = socket.AF_UNIX

    def call(
        self,
        operation: Operation,
        parameters: Mapping[str, Any],
        body: Any,
        *,
        output: str | Path | None = None,
    ) -> Response:
        check_operation(operation, output)
        request = {
            "operationId": operation.operationId,
            "parameters": dict(parameters),
            "body": body,
        }
        if self.document is not None:
            request["document"] = self.document
        frame = encode_frame(request, self.max_bytes)
        try:
            with socket.socket(self.family, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(self.endpoint)
                if self._prelude is not None:
                    connection.sendall(self._prelude)
                connection.sendall(frame)
                reply = decode_frame(read_line(connection, self.max_bytes, self.timeout))
        except OSError as exc:
            # Never include the session token or raw OS/peer payload in errors.
            raise SurfaceError(None, ["Socket sidecar connection failed"], code=1) from exc
        if not isinstance(reply, dict):
            raise WireError("protocol", "Invalid socket response envelope")
        if set(reply) == {"error"}:
            _raise_error(reply["error"])
        if (
            set(reply) != {"status", "headers", "body"}
            or type(reply["status"]) is not int
            or not 200 <= reply["status"] < 300
            or not isinstance(reply["headers"], dict)
            or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in reply["headers"].items()
            )
        ):
            raise WireError("protocol", "Invalid socket response status or headers")
        return Response(reply["status"], reply["body"], reply["headers"])

    def close(self) -> None:
        """Connections are already closed on every success and failure path."""
