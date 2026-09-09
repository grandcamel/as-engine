"""Split Mode server and bounded wire helpers. See docs/split-mode.md."""

from __future__ import annotations

import hmac
import ipaddress
import json
import math
import os
import re
import signal
import socket
import stat
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .errors import ScopeRefusal, SurfaceError
from .index import Operation, OperationIndex, ProductIndexes
from .params import body_errors, validate_parameters
from .surface import Surface
from .transport import binary_mode, multipart_mode

MAX_BYTES = 8 * 1024 * 1024
TOKEN_BYTES = 4096
_IDENTITY = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}(?:-[0-9]{1,20})?\Z")


class WireError(SurfaceError):
    """A local protocol refusal, distinct from an upstream HTTP failure."""

    def __init__(self, kind: str, message: str, *, code: int = 2) -> None:
        self.kind = kind
        super().__init__(None, [message], code=code)


def tcp_address(tcp: tuple[str, int]) -> tuple[str, int]:
    """Accept literal loopback addresses only; never resolve a hostname."""
    host, port = tcp
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("TCP requires a literal loopback address") from None
    if not address.is_loopback or type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("TCP requires a loopback address and port from 0 to 65535")
    return str(address), port


def token_prelude(token: str | None) -> bytes:
    if not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,4087}", token):
        raise ValueError("TCP requires a nonempty printable ASCII token without whitespace")
    return b"Bearer " + token.encode("ascii") + b"\n"


def read_line(connection: socket.socket, limit: int, timeout: float) -> bytes:
    """Read one bounded line with a total deadline, without pre-reading payloads."""
    end = time.monotonic() + timeout
    result = bytearray()
    while len(result) <= limit:
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise WireError("protocol", "Socket request timed out")
        connection.settimeout(remaining)
        # Peek allows efficient reads without consuming the next frame (TCP auth).
        chunk = connection.recv(min(65536, limit + 1 - len(result)), socket.MSG_PEEK)
        if not chunk:
            raise WireError("protocol", "Socket frame ended before newline")
        newline = chunk.find(b"\n")
        amount = newline + 1 if newline >= 0 else len(chunk)
        result.extend(connection.recv(amount))
        if result.endswith(b"\n"):
            if len(result) > limit:
                break
            return bytes(result)
    raise WireError("protocol", "Socket frame exceeds configured size limit")


def encode_frame(value: Any, limit: int) -> bytes:
    frame = (
        json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n"
    ).encode()
    if len(frame) > limit:
        raise WireError("protocol", "Socket frame exceeds configured size limit")
    return frame


def decode_frame(frame: bytes) -> Any:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate member")
            result[key] = value
        return result

    def constant(value: str) -> Any:
        raise ValueError("nonfinite number")

    try:
        return json.loads(frame.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise WireError(
            "protocol", "Socket frame must be valid UTF-8 JSON with unique members"
        ) from None


def check_limits(timeout: float, max_bytes: int) -> None:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Socket timeout must be finite and positive")
    if type(max_bytes) is not int or not 1024 <= max_bytes <= MAX_BYTES:
        raise ValueError(f"Socket frame limit must be between 1024 and {MAX_BYTES} bytes")


def check_operation(operation: Operation, output: str | Path | None = None) -> None:
    if binary_mode(operation, output):
        raise WireError(
            "unsupported", "Socket transport does not support output= or binary downloads"
        )
    if multipart_mode(operation):
        raise WireError("unsupported", "Socket transport does not support multipart uploads")


class _DocumentIndexes(ProductIndexes):
    """An indexed document selection, never a client-supplied path or operation."""

    def __init__(self, document: str, index: OperationIndex) -> None:
        self.document = document
        self.index = index

    def find(self, name: str) -> tuple[str, OperationIndex, Operation]:
        return self.document, self.index, self.index.operations[name]

    def get(self, document_id: str) -> OperationIndex:
        if document_id != self.document:
            raise KeyError(document_id)
        return self.index

    def primary(self) -> list[tuple[str, OperationIndex]]:
        return [(self.document, self.index)]


def _body_identity(operation: Operation, body: Any) -> str | None:
    """Supply the guard's body-identity cross-check from the call, never its hint."""
    from .transforms.values import pointer

    tag = operation.extensions.get("x-as-scope", {})
    if not isinstance(tag, dict) or tag.get("in") != "body":
        return None
    paths = tag.get("paths", [tag.get("path")])
    if not isinstance(paths, list):
        return None
    candidates = [pointer(body, path, None) for path in paths if isinstance(path, str)]
    # The guard still validates all paths, types, checks and membership itself.
    return next((v for v in candidates if isinstance(v, str) and _IDENTITY.fullmatch(v)), None)


def _summary(operation: Operation, parameters: Mapping[str, Any], body: Any) -> dict[str, Any]:
    names = sorted({p["name"] for p in operation.parameters}.intersection(parameters))
    tag = operation.extensions.get("x-as-scope", {})
    identities: list[str] = []
    if isinstance(tag, dict):
        candidate = (
            _body_identity(operation, body)
            if tag.get("in") == "body"
            else parameters.get(tag.get("name", ""))
            if tag.get("in") in {"key", "path"}
            else None
        )
        if isinstance(candidate, str) and _IDENTITY.fullmatch(candidate):
            identities.append(candidate)
    return {"names": names, "identity": identities}


def _error(error: Exception) -> tuple[dict[str, Any], str]:
    if (
        isinstance(error, ScopeRefusal)
        or isinstance(error, SurfaceError)
        and error.status is None
        and error.code == 4
    ):
        kind, message, status, code, outcome = (
            "scope",
            "Sidecar scope policy refused this call",
            None,
            4,
            "refused",
        )
    elif isinstance(error, WireError):
        kind, message, status, code, outcome = error.kind, str(error), None, error.code, "refused"
    elif isinstance(error, SurfaceError):
        status, code = error.status, error.code
        kind = {
            400: "validation",
            401: "authentication",
            403: "permission",
            404: "not_found",
            409: "conflict",
            429: "rate_limit",
        }.get(
            status if status is not None else 0,
            "server"
            if status is not None and status >= 500
            else "validation"
            if code == 2
            else "api",
        )
        message = f"Sidecar call failed: {kind}"  # No upstream response or credential echo.
        outcome = "refused" if status is None else "error"
    elif isinstance(error, (ValueError, TypeError, KeyError)):
        kind, message, status, code, outcome = (
            "validation",
            "Sidecar input validation failed",
            None,
            2,
            "refused",
        )
    elif isinstance(error, (OSError, TimeoutError)):
        kind, message, status, code, outcome = (
            "connection",
            "Sidecar connection failed",
            None,
            1,
            "error",
        )
    else:
        kind, message, status, code, outcome = (
            "internal",
            "Sidecar internal error",
            None,
            1,
            "error",
        )
    return {
        "error": {"kind": kind, "message": message, "status": status, "code": code}
    }, f"{outcome} {kind}"


def handle_request(
    surface: Surface,
    frame: bytes,
    *,
    allowlist: Sequence[str],
    allow_site: bool = False,
    max_bytes: int = MAX_BYTES,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Process an authenticated frame through the exact server checker/guard seam.

    No socket or log is opened here. The serving loop authenticates first, then
    appends the returned audit record exactly once before sending the reply.
    """
    policy = tuple(allowlist)
    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "operationId": None,
        "method": None,
        "path": None,
        "parameters": {"names": [], "identity": []},
    }
    try:
        if len(frame) > max_bytes or not frame.endswith(b"\n"):
            raise WireError("protocol", "Incomplete or oversized socket frame")
        request = decode_frame(frame)
        if (
            not isinstance(request, dict)
            or set(request) - {"operationId", "parameters", "body", "document", "argv_identity"}
            or not {"operationId", "parameters", "body"} <= request.keys()
        ):
            raise WireError("protocol", "Invalid socket request envelope")
        if (
            not isinstance(request["operationId"], str)
            or not request["operationId"]
            or not isinstance(request["parameters"], dict)
        ):
            raise WireError("protocol", "Invalid operationId or parameters")
        if "argv_identity" in request and not isinstance(request["argv_identity"], str):
            raise WireError("protocol", "argv_identity must be a string")
        selected = surface
        if "document" in request:
            document = request["document"]
            if not isinstance(document, str):
                raise WireError("protocol", "document must be an indexed document ID")
            selected = Surface(
                _DocumentIndexes(document, surface.indexes.get(document)),
                surface.transport_factory,
                registry=surface.registry,
                scope_allowlist=policy,
                scope_allow_site=allow_site,
                scope_resolution_rules=surface.scope_resolution_rules,
            )
        _, index, operation = selected.resolve(request["operationId"])
        if operation.operationId != request["operationId"]:
            raise WireError("protocol", "operationId must be canonical")
        parameters, body = request["parameters"], request["body"]
        record.update(
            operationId=operation.operationId,
            method=operation.method,
            path=operation.path,
            parameters=_summary(operation, parameters, body),
        )
        check_operation(operation)
        try:
            checked = validate_parameters(operation, parameters, index.schemas)
        except ValueError as exc:
            # Known parameter names and checker reasons are safe; unknown names may
            # themselves contain secrets, so do not reflect those arbitrary names.
            message = str(exc)
            if message.startswith("unknown parameter:"):
                message = "unknown parameter"
            raise WireError("validation", message) from None
        problems = body_errors(operation, body, index.schemas)
        if problems:
            messages = [
                "body: contains an unknown field" if value.endswith(": is not allowed") else value
                for value in problems
            ]
            raise WireError("validation", "; ".join(messages[:8])[:1024])
        response = selected.call(
            operation.operationId,
            checked,
            body,
            validate_body=True,
            scope_allowlist=policy,
            scope_allow_site=allow_site,
            scope_argv_identity=_body_identity(operation, body),
            raw=bool(operation.extensions.get("x-as-richtext")),
        )
        reply = {
            "status": response.status,
            "headers": dict(response.headers),
            "body": response.body,
        }
        encode_frame(reply, max_bytes)
        outcome = f"ok {response.status}"
    except Exception as exc:  # noqa: BLE001 — return a safe protocol failure
        reply, outcome = _error(exc)
    record["outcome"] = outcome
    return reply, record


def _open_log(path: str | Path) -> int:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
    )
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        os.close(descriptor)
        raise ValueError("Call log must be an owned regular file with mode 0600 and one link")
    return descriptor


def serve(
    surface_factory: Callable[[], Surface],
    *,
    call_log: str | Path,
    allowlist: Sequence[str],
    allow_site: bool = False,
    socket_path: str | Path | None = None,
    tcp: tuple[str, int] | None = None,
    token: str | None = None,
    timeout: float = 30,
    max_bytes: int = MAX_BYTES,
    stop_event: threading.Event | None = None,
    ready: Callable[[str | tuple[str, int]], None] | None = None,
) -> None:
    """Serve until signalled/stopped; callers own the Surface factory and policy.

    ``ready`` runs only after bind/listen; ``stop_event`` supports an in-process
    fake sidecar. SIGTERM/SIGINT are installed/restored only in the main thread.
    """
    check_limits(timeout, max_bytes)
    if (
        allowlist is None
        or isinstance(allowlist, str)
        or any(
            not isinstance(key, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key) is None
            for key in allowlist
        )
        or type(allow_site) is not bool
    ):
        raise ValueError("Serve requires an explicit project allowlist and boolean site policy")
    policy = tuple(allowlist)
    if tcp is not None and socket_path is not None:
        raise ValueError("Choose a Unix socket or TCP, not both")
    if tcp is None and token is not None:
        raise ValueError("Tokens apply only to TCP")
    prelude = token_prelude(token) if tcp is not None else None
    address = tcp_address(tcp) if tcp is not None else None
    path = Path(socket_path if socket_path is not None else f"/tmp/as-engine-{os.getuid()}.sock")
    stopped = stop_event if stop_event is not None else threading.Event()
    surface = surface_factory()
    surface.scope_allowlist, surface.scope_allow_site = policy, allow_site
    log_fd = _open_log(call_log)
    listener: socket.socket | None = None
    identity: tuple[int, int] | None = None
    active: socket.socket | None = None
    previous: dict[Any, Any] = {}

    def stop(signum: int, frame: Any) -> None:
        stopped.set()
        if active is not None:
            try:
                active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    try:
        if address is None:
            if os.path.lexists(path):
                raise ValueError("Socket path already exists; refusing to replace it")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path))
            info = path.lstat()
            identity = (info.st_dev, info.st_ino)
            path.chmod(0o600)
            bound: str | tuple[str, int] = str(path)
        else:
            listener = socket.socket(
                socket.AF_INET6 if ":" in address[0] else socket.AF_INET, socket.SOCK_STREAM
            )
            listener.bind(address)
            actual = listener.getsockname()
            bound = (actual[0], actual[1])
        listener.listen(16)
        listener.settimeout(0.1)
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, stop)
        if ready:
            ready(bound)
        while not stopped.is_set():
            try:
                active, _ = listener.accept()
            except TimeoutError:
                continue
            with active:
                record: dict[str, Any] = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "operationId": None,
                    "method": None,
                    "path": None,
                    "parameters": {"names": [], "identity": []},
                }
                try:
                    if prelude is not None:
                        supplied = read_line(active, TOKEN_BYTES, timeout)
                        if not hmac.compare_digest(supplied, prelude):
                            raise WireError("authentication", "Sidecar token refused", code=3)
                    reply, record = handle_request(
                        surface,
                        read_line(active, max_bytes, timeout),
                        allowlist=policy,
                        allow_site=allow_site,
                        max_bytes=max_bytes,
                    )
                    outcome = record["outcome"]
                    frame = encode_frame(reply, max_bytes)
                except Exception as exc:  # noqa: BLE001 — isolate untrusted calls; never expose details
                    reply, outcome = _error(exc)
                    frame = encode_frame(reply, max_bytes)
                record["outcome"] = outcome
                # Commit the sole audit record before replying; disk failure stops serving.
                line = encode_frame(record, MAX_BYTES)
                if os.write(log_fd, line) != len(line):
                    raise OSError("Incomplete sidecar call-log write")
                os.fsync(log_fd)
                try:
                    active.settimeout(timeout)
                    active.sendall(frame)
                except OSError:
                    pass  # The call's outcome is already recorded exactly once.
            active = None
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if listener is not None:
            listener.close()
        os.close(log_fd)
        if identity is not None:
            try:
                info = path.lstat()
                if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == identity:
                    path.unlink()
            except FileNotFoundError:
                pass


@dataclass(frozen=True)
class Sidecar:
    socket_path: Path | None
    tcp: tuple[str, int] | None
    call_log: Path


@contextmanager
def fake_sidecar(
    indexes: ProductIndexes | None = None,
    *,
    surface_factory: Callable[[], Surface] | None = None,
    allowlist: Sequence[str] = (),
    allow_site: bool = False,
    tcp: tuple[str, int] | None = None,
    token: str | None = None,
    timeout: float = 1,
    max_bytes: int = MAX_BYTES,
) -> Iterator[Sidecar]:
    """Serve Responder through the real socket seam in a joined in-process thread.

    Supply indexes for the stateless double, or a trusted Surface factory for
    seeded Responder/simulation tests. Startup failures are re-raised unchanged.
    No HTTP transport, credentials, or global environment changes are required.
    """
    from .responder import Responder

    check_limits(timeout, max_bytes)
    if surface_factory is None:
        if indexes is None:
            raise ValueError("fake_sidecar requires indexes or a Surface factory")
        product = indexes

        def surface_factory() -> Surface:
            return Surface(product, lambda document, index: Responder(index))

    factory = surface_factory
    with TemporaryDirectory(prefix="as-") as temporary:
        root = Path(temporary)
        stopped, started = threading.Event(), threading.Event()
        failures: list[BaseException] = []
        addresses: list[str | tuple[str, int]] = []

        def ready(address: str | tuple[str, int]) -> None:
            addresses.append(address)
            started.set()

        def run() -> None:
            try:
                serve(
                    factory,
                    socket_path=root / "s" if tcp is None else None,
                    tcp=tcp,
                    token=token,
                    call_log=root / "calls.jsonl",
                    allowlist=allowlist,
                    allow_site=allow_site,
                    timeout=timeout,
                    max_bytes=max_bytes,
                    stop_event=stopped,
                    ready=ready,
                )
            except BaseException as exc:  # noqa: BLE001 — propagate failures to the test owner
                failures.append(exc)
            finally:
                started.set()

        worker = threading.Thread(target=run, name="as-engine-fake-sidecar")
        worker.start()
        try:
            if not started.wait(10):
                raise RuntimeError("Fake sidecar did not report startup")
            if failures:
                raise failures[0]
            address = addresses[0]
            yield Sidecar(
                Path(address) if isinstance(address, str) else None,
                address if isinstance(address, tuple) else None,
                root / "calls.jsonl",
            )
        finally:
            stopped.set()
            worker.join(timeout + 2)
            if worker.is_alive():
                raise RuntimeError("Fake sidecar did not stop; thread remains active")
        if failures:
            raise failures[0]
