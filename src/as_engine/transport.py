"""The single operation transport seam and its pooled HTTP implementation."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import quote

import requests
from assistant_skills_lib.error_handler import (  # type: ignore[import-untyped]
    ServerError,
    handle_api_error,
)
from requests.adapters import HTTPAdapter

from .index import Operation


@dataclass(frozen=True)
class Response:
    status: int
    body: Any
    headers: Mapping[str, str] = field(default_factory=dict)


class Transport(Protocol):
    def call(self, operation: Operation, parameters: Mapping[str, Any], body: Any) -> Response:
        """Execute one operation, returning a response or raising a domain error."""
        ...


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


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
        error_handler: Callable[..., None] = handle_api_error,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.verify_ssl = verify_ssl
        self.error_handler = error_handler
        self.sleep = sleep
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

    def call(self, operation: Operation, parameters: Mapping[str, Any], body: Any) -> Response:
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
        if (
            body is not None
            and media_types
            and not any(
                media == "application/json" or media.endswith("+json") for media in media_types
            )
        ):
            raise ValueError(
                "this surface accepts JSON bodies; operation requires " + ", ".join(media_types)
            )
        if body is not None:
            headers["Content-Type"] = next(
                (m for m in media_types if m == "application/json" or m.endswith("+json")),
                "application/json",
            )
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.request(
                    operation.method,
                    self.base_url + path,
                    params=query,
                    headers=headers,
                    cookies=cookies,
                    json=body,
                    timeout=self.timeout,
                    verify=self.verify_ssl,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                # Do not include request URLs or credential-bearing headers in diagnostics.
                raise ServerError(
                    "HTTP transport failed: " + type(exc).__name__,
                    operation=operation.operationId,
                    status_code=503,
                ) from exc
            if (
                response.status_code == 429 or 500 <= response.status_code <= 599
            ) and attempt < self.max_retries:
                delay = self._delay(response, attempt)
                response.close()
                self.sleep(delay)
                continue
            try:
                if response.status_code >= 400:
                    # Base library's 429 mapper accepts seconds only; retain date handling here.
                    retry_after = response.headers.get("Retry-After")
                    if retry_after and not retry_after.isdigit():
                        response.headers.pop("Retry-After", None)
                    self.error_handler(response, operation.operationId)
                try:
                    result = response.json() if response.content else None
                except ValueError:
                    result = response.text
                return Response(response.status_code, result, dict(response.headers))
            finally:
                response.close()
        raise AssertionError("retry loop exhausted without a result")

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> HTTPTransport:  # noqa: PYI034
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
