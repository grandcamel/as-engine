"""Transport-seam tests: real requests serialization through an offline adapter."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
import requests
from assistant_skills_lib.error_handler import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionError,
    RateLimitError,
    ServerError,
    ValidationError,
)
from requests.adapters import BaseAdapter

from as_engine.index import Operation
from as_engine.transport import HTTPTransport


class Wire(BaseAdapter):
    def __init__(self, responses):
        self.responses = iter(responses)
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append((request, kwargs))
        status, body, headers = next(self.responses)
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(body).encode() if body is not None else b""
        response.headers.update(headers)
        response.request = request
        return response

    def close(self):
        pass


def operation(**kwargs):
    base = Operation(
        "getThings",
        "GET",
        "/things/{id}",
        [],
        None,
        None,
        [
            {"name": "id", "in": "path", "type": "string", "required": True},
            {"name": "enabled", "in": "query", "type": "boolean"},
            {"name": "ids", "in": "query", "type": "array"},
            {"name": "x-version", "in": "header", "type": "integer"},
        ],
        None,
        None,
        {},
        [],
    )
    return replace(base, **kwargs)


def transport(responses, **kwargs):
    result = HTTPTransport("https://offline.invalid/api", **kwargs)
    wire = Wire(responses)
    result.session.mount("https://", wire)
    return result, wire


def test_request_serialization_pool_lifecycle_timeout_and_json_body():
    t, wire = transport(
        [(200, {"ok": True}, {}), (204, None, {})],
        auth=("test@example.invalid", "test-only"),
        timeout=12,
    )
    with t:
        op = operation(method="POST", request_media_types=["application/json"])
        assert t.call(
            op, {"id": "a/b ?", "enabled": False, "ids": [1, 2], "x-version": 7}, {"x": 3}
        ).body == {"ok": True}
        assert t.call(op, {"id": "x"}, None).status == 204
    req, settings = wire.sent[0]
    assert req.url == "https://offline.invalid/api/things/a%2Fb%20%3F?enabled=false&ids=1&ids=2"
    assert req.method == "POST" and json.loads(req.body) == {"x": 3}
    assert req.headers["x-version"] == "7"
    assert req.headers["Authorization"].startswith("Basic ")
    assert settings["timeout"] == 12 and settings["verify"] is True


@pytest.mark.parametrize(
    "status,exception",
    [
        (400, ValidationError),
        (401, AuthenticationError),
        (403, PermissionError),
        (404, NotFoundError),
        (409, ConflictError),
        (429, RateLimitError),
        (500, ServerError),
        (599, ServerError),
    ],
)
def test_domain_mapping_after_final_status(status, exception):
    t, wire = transport([(status, {"message": "test failure"}, {})], max_retries=0)
    with t, pytest.raises(exception) as caught:
        t.call(operation(), {"id": "one"}, None)
    assert caught.value.status_code == status
    assert len(wire.sent) == 1


def test_all_5xx_and_429_retry_after_and_backoff():
    delays = []
    t, wire = transport(
        [(501, {}, {}), (429, {}, {"Retry-After": "7"}), (503, {}, {}), (200, [1], {})],
        sleep=delays.append,
    )
    with t:
        assert t.call(operation(), {"id": "x"}, None).body == [1]
    assert delays == [2, 7, 8] and len(wire.sent) == 4


def test_retry_after_http_date_and_final_date_rate_limit_mapping():
    date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=15))
    delays = []
    t, wire = transport(
        [(429, {}, {"Retry-After": date}), (429, {}, {"Retry-After": date})],
        sleep=delays.append,
        max_retries=1,
    )
    with t, pytest.raises(RateLimitError):
        t.call(operation(), {"id": "x"}, None)
    assert 12 <= delays[0] <= 15 and len(wire.sent) == 2


def test_conflict_and_redirect_never_retried_or_followed():
    t, wire = transport([(302, None, {"Location": "https://other.invalid"})])
    with t:
        assert t.call(operation(), {"id": "x"}, None).status == 302
    assert len(wire.sent) == 1
    t, wire = transport([(409, {"message": "changed"}, {})])
    with t, pytest.raises(ConflictError):
        t.call(operation(), {"id": "x"}, None)
    assert len(wire.sent) == 1


def test_style_serialization_and_non_json_refusal():
    t, wire = transport([(200, {}, {})])
    op = operation(
        path="/things",
        parameters=[
            {"name": "ids", "in": "query", "style": "pipeDelimited", "explode": False},
            {"name": "filter", "in": "query", "style": "deepObject"},
            {"name": "visit", "in": "cookie"},
        ],
    )
    with t:
        t.call(op, {"ids": [1, 2], "filter": {"active": True}, "visit": "x"}, None)
        assert "ids=1%7C2" in wire.sent[0][0].url
        assert "filter%5Bactive%5D=true" in wire.sent[0][0].url
        assert wire.sent[0][0].headers["Cookie"] == "visit=x"
        with pytest.raises(ValueError, match="JSON"):
            t.call(replace(op, request_media_types=["text/plain"]), {}, {})
    assert len(wire.sent) == 1


def test_connection_error_is_domain_error_without_secret_url(monkeypatch):
    t = HTTPTransport("https://offline.invalid")

    def fail(*args, **kwargs):
        raise requests.Timeout("credential-bearing-url")

    monkeypatch.setattr(t.session, "request", fail)
    with t, pytest.raises(ServerError) as caught:
        t.call(operation(), {"id": "x"}, None)
    assert "credential-bearing" not in str(caught.value)
