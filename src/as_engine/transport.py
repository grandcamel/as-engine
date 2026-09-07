"""The single operation transport seam and its pooled HTTP implementation."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, Protocol
from urllib.parse import quote, urljoin, urlsplit

if TYPE_CHECKING:
    import requests


def __getattr__(name: str) -> Any:
    """Keep historical module-level HTTP names available without eager imports."""
    if name in {"requests", "HTTPAdapter"}:
        import requests
        from requests.adapters import HTTPAdapter

        value = requests if name == "requests" else HTTPAdapter
    elif name in {"ServerError", "handle_api_error"}:
        from assistant_skills_lib.error_handler import (  # type: ignore[import-untyped]
            ServerError,
            handle_api_error,
        )

        value = ServerError if name == "ServerError" else handle_api_error
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def _default_error_handler(*args: Any, **kwargs: Any) -> None:
    """Defer the legacy error mapper until an HTTP transport is used."""
    from assistant_skills_lib.error_handler import handle_api_error  # type: ignore[import-untyped]

    handle_api_error(*args, **kwargs)

from .index import Operation


@dataclass(frozen=True)
class Response:
    status: int
    body: Any
    headers: Mapping[str, str] = field(default_factory=dict)


class Transport(Protocol):
    def call(
        self, operation: Operation, parameters: Mapping[str, Any], body: Any,
        *, output: str | Path | None = None,
    ) -> Response:
        """Execute one operation, returning a response or raising a domain error."""
        ...


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def multipart_mode(operation: Operation) -> bool:
    """JSON takes precedence when an operation offers both request encodings."""
    media_types = operation.request_media_types
    return "multipart/form-data" in media_types and not any(
        media == "application/json" or media.endswith("+json") for media in media_types
    )


def multipart_parts(body: Any) -> list[tuple[str, tuple[str | None, bytes, str]]]:
    """Snapshot file parts once so status retries resend exactly the same bytes."""
    if not isinstance(body, dict) or not body:
        raise ValueError("multipart body must be a nonempty JSON object")
    parts = []
    for name, value in body.items():
        if not isinstance(name, str) or not name:
            raise ValueError("multipart part names must be nonempty strings")
        filename = None
        content_type = "text/plain; charset=utf-8"
        if isinstance(value, str) and value.startswith("@"):
            if len(value) == 1:
                raise ValueError("multipart file reference requires a path")
            path = Path(value[1:])
            filename = path.name
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise ValueError(f"cannot read multipart file: {path}") from exc
        else:
            data = _scalar(value).encode("utf-8")
        parts.append((name, (filename, data, content_type)))
    return parts


def multipart_metadata(body: Any) -> list[dict[str, Any]]:
    """Canonical part descriptors for doubles/cassettes; never persist upload bytes."""
    return sorted(
        [
            {"name": name, "filename": filename, "size": len(data),
             "content_type": content_type, "sha256": hashlib.sha256(data).hexdigest()}
            for name, (filename, data, content_type) in multipart_parts(body)
        ],
        key=lambda part: part["name"],
    )


def binary_mode(operation: Operation, output: str | Path | None = None) -> bool:
    tag = operation.extensions.get("x-as-response")
    if tag is not None and tag != {"kind": "binary"}:
        raise ValueError("x-as-response must be {kind: binary}")
    return output is not None or tag is not None


def _header(headers: Mapping[str, str], name: str) -> str:
    return next((v for k, v in headers.items() if k.lower() == name.lower()), "")


def download_filename(headers: Mapping[str, str]) -> str:
    """Discard server-supplied directories and controls, including Windows paths."""
    message = Message()
    message["Content-Disposition"] = _header(headers, "Content-Disposition")
    filename = message.get_filename() or "attachment.bin"
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    filename = "".join(char for char in filename if ord(char) >= 32 and ord(char) != 127)
    return filename if filename not in ("", ".", "..") else "attachment.bin"


def write_binary(
    chunks: Iterable[bytes], headers: Mapping[str, str], output: str | Path | None = None,
) -> dict[str, Any]:
    """Publish a complete download atomically; a failed stream leaves no partial file."""
    target = Path(output) if output is not None else Path(download_filename(headers))
    temporary: Path | None = None
    size = 0
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".as-download-", delete=False) as f:
            temporary = Path(f.name)
            for chunk in chunks:
                f.write(chunk)
                size += len(chunk)
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"path": str(target), "bytes": size,
            "content_type": _header(headers, "Content-Type") or "application/octet-stream"}


def binary_response(response: Response, output: str | Path | None = None) -> Response:
    """Materialize a double's raw successful bytes using the live file-output contract."""
    if not 200 <= response.status < 300:
        return response
    if not isinstance(response.body, bytes):
        raise ValueError("binary response body must be bytes")  # noqa: TRY004
    return Response(response.status, write_binary([response.body], response.headers, output),
                    response.headers)


def _redirect_target(url: str, location: str) -> str | None:
    """One same-origin hop only; no media host is authorized by the pinned documents."""
    if not location or any(ord(char) <= 32 or ord(char) == 127 for char in location):
        return None
    if "\\" in location:
        return None
    try:
        target = urljoin(url, location)
        source, destination = urlsplit(url), urlsplit(target)
        def origin(parts: Any) -> tuple[str, str | None, int | None]:
            return parts.scheme, parts.hostname, parts.port or {"http": 80, "https": 443}.get(parts.scheme)
        if (destination.scheme not in ("http", "https") or not destination.hostname
            or destination.username is not None or destination.password is not None
            or origin(source) != origin(destination)):
            return None
    except ValueError:
        return None
    return target


class HTTPTransport:
    """Product supplies its base URL and error mapper; no operation-specific methods.

    Retry only explicit 429/5xx responses, never connection failures or 409.
    Retrying mutations matches the product's existing status-retry policy.
    """

    def __init__(
        self,
        base_url: str,
        *,
        auth: tuple[str, str] | None = None,
        timeout: float = 30,
        max_retries: int = 3,
        retry_backoff: float = 2,
        verify_ssl: bool = True,
        error_handler: Callable[..., None] = _default_error_handler,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.verify_ssl = verify_ssl
        self.sleep = sleep
        import requests
        from requests.adapters import HTTPAdapter

        self._requests = requests
        if error_handler is _default_error_handler:
            from assistant_skills_lib.error_handler import (
                handle_api_error,  # type: ignore[import-untyped]
            )

            error_handler = handle_api_error
        self.error_handler = error_handler
        self.session = requests.Session()
        self.session.auth = auth
        self.session.headers.update({"Accept": "application/json"})
        # The explicit loop owns retries, including all 5xx and Retry-After dates.
        self.session.mount("https://", HTTPAdapter(max_retries=0))
        self.session.mount("http://", HTTPAdapter(max_retries=0))

    def _delay(self, response: requests.Response, attempt: int) -> float:
        value = response.headers.get("Retry-After", "")
        try:
            return max(0, float(int(value)))
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                return max(0, (when - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                return self.retry_backoff * 2**attempt

    def call(
        self, operation: Operation, parameters: Mapping[str, Any], body: Any,
        *, output: str | Path | None = None,
    ) -> Response:
        path = operation.path
        query: list[tuple[str, str]] = []
        headers: dict[str, str] = {}
        cookies: dict[str, str] = {}
        for parameter in operation.parameters:
            name = parameter["name"]
            if name not in parameters:
                continue
            value = parameters[name]
            location = parameter["in"]
            style = parameter.get("style", "form" if location in ("query", "cookie") else "simple")
            explode = parameter.get("explode", style == "form")
            if location == "query":
                if isinstance(value, list):
                    separator = {"spaceDelimited": " ", "pipeDelimited": "|"}.get(style, ",")
                    values = [_scalar(v) for v in value]
                    query.extend((name, v) for v in values) if explode else query.append(
                        (name, separator.join(values))
                    )
                elif isinstance(value, dict):
                    if style == "deepObject":
                        query.extend((f"{name}[{k}]", _scalar(v)) for k, v in value.items())
                    elif explode:
                        query.extend((k, _scalar(v)) for k, v in value.items())
                    else:
                        query.append(
                            (name, ",".join(x for k, v in value.items() for x in (k, _scalar(v))))
                        )
                else:
                    query.append((name, _scalar(value)))
            else:
                if isinstance(value, list):
                    encoded = ",".join(_scalar(v) for v in value)
                elif isinstance(value, dict):
                    encoded = ",".join(
                        f"{k}={_scalar(v)}" if explode else f"{k},{_scalar(v)}"
                        for k, v in value.items()
                    )
                else:
                    encoded = _scalar(value)
                if location == "path":
                    path = path.replace("{" + name + "}", quote(encoded, safe=""))
                elif location == "header":
                    headers[name] = encoded
                elif location == "cookie":
                    cookies[name] = encoded
        if "{" in path or "}" in path:
            raise ValueError("unresolved path parameter")
        if not path.startswith("/") or path.startswith("//") or "://" in path:
            raise ValueError("operation path must be relative to the configured API base")
        media_types = operation.request_media_types
        multipart = multipart_mode(operation)
        binary = binary_mode(operation, output)
        if (
            body is not None and media_types and not multipart
            and not any(media == "application/json" or media.endswith("+json") for media in media_types)
        ):
            raise ValueError(
                "this surface accepts JSON bodies; operation requires " + ", ".join(media_types)
            )
        request_body: dict[str, Any] = {"json": body}
        if multipart:
            request_body = {"files": multipart_parts(body)}
            headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
            headers["X-Atlassian-Token"] = "nocheck"
        elif body is not None:
            headers["Content-Type"] = next(
                (m for m in media_types if m == "application/json" or m.endswith("+json")),
                "application/json",
            )
        if binary:
            headers["Accept"] = "*/*"
            request_body["stream"] = True
        url = self.base_url + path
        for hop in range(2 if binary else 1):
            for attempt in range(self.max_retries + 1):
                try:
                    response = self.session.request(
                        operation.method, url, params=query, headers=headers, cookies=cookies,
                        **request_body, timeout=self.timeout, verify=self.verify_ssl,
                        allow_redirects=False,
                    )
                except self._requests.RequestException as exc:
                    self._network_error(operation, exc)
                if (
                    response.status_code == 429 or 500 <= response.status_code <= 599
                ) and attempt < self.max_retries:
                    delay = self._delay(response, attempt)
                    response.close()
                    self.sleep(delay)
                    continue
                break
            try:
                if binary and 300 <= response.status_code < 400:
                    location = response.headers.get("Location", "")
                    target = _redirect_target(url, location)
                    if (hop or target is None or operation.method.upper() not in ("GET", "HEAD")
                        or response.status_code not in (301, 302, 303, 307, 308)):
                        try:
                            host = urlsplit(urljoin(url, location)).hostname if location else None
                        except ValueError:
                            host = None
                        return Response(response.status_code, {
                            "message": "binary redirect refused: destination host " + (host or "<invalid>"),
                        })
                    url, query = target, []
                    # A download redirect carries neither the original query nor a request body.
                    request_body = {"stream": True}
                    continue
                if response.status_code >= 400:
                    retry_after = response.headers.get("Retry-After")
                    if retry_after and not retry_after.isdigit():
                        response.headers.pop("Retry-After", None)
                    self.error_handler(response, operation.operationId)
                result: Any
                if binary and 200 <= response.status_code < 300:
                    result = write_binary(response.iter_content(chunk_size=65536), response.headers, output)
                else:
                    try:
                        result = response.json() if response.content else None
                    except ValueError:
                        result = response.text
                return Response(response.status_code, result, dict(response.headers))
            except self._requests.RequestException as exc:
                self._network_error(operation, exc)
            finally:
                response.close()
        raise AssertionError("retry loop exhausted without a result")

    @staticmethod
    def _network_error(operation: Operation, exc: Exception) -> NoReturn:
        from assistant_skills_lib.error_handler import ServerError  # type: ignore[import-untyped]

        # Do not include request URLs or credential-bearing headers in diagnostics.
        raise ServerError(
            "HTTP transport failed: " + type(exc).__name__,
            operation=operation.operationId, status_code=503,
        ) from exc

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> HTTPTransport:  # noqa: PYI034
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
