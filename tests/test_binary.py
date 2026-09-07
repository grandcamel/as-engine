"""Multipart wire requests and streamed downloads through the public transport seam."""
from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace
from email import policy
from email.parser import BytesParser

import pytest
import requests
from assistant_skills_lib.error_handler import ConflictError, ServerError
from requests.adapters import BaseAdapter

from as_engine.index import Operation
from as_engine.params import build_body
from as_engine.transport import HTTPTransport, Response, binary_response


def operation(**changes):
    op = Operation("download", "GET", "/attachments/{id}", [], None, None,
                   [{"name": "id", "in": "path", "type": "string", "required": True}],
                   None, None, {"x-as-response": {"kind": "binary"}}, [])
    return replace(op, **changes)


class Wire(BaseAdapter):
    def __init__(self, responses):
        self.responses = iter(responses)
        self.sent = []
        self.streams = []

    def send(self, request, **kwargs):
        self.sent.append((request, kwargs))
        status, data, headers = next(self.responses)
        response = requests.Response()
        response.status_code = status
        response.raw = io.BytesIO(data)
        self.streams.append(response.raw)
        response.headers.update(headers)
        response.request = request
        response.url = request.url
        return response

    def close(self):
        pass


def transport(responses, **kwargs):
    t = HTTPTransport("https://offline.invalid/api", **kwargs)
    wire = Wire(responses)
    t.session.mount("https://", wire)
    return t, wire


def test_multipart_fields_files_and_retry_bytes_use_real_requests(tmp_path):
    content = b"\x89PNG\x00\xff\n"
    path = tmp_path / "f.png"
    path.write_bytes(content)
    op = operation(method="POST", extensions={}, request_media_types=["multipart/form-data"])
    body = build_body(None, [f"file=@{path}", "comment=hello", "minorEdit=true"], operation=op)
    delays = []
    t, wire = transport([(503, b'{}', {}), (200, b'{"ok":true}', {})], sleep=delays.append)
    with t:
        assert t.call(op, {"id": "1"}, body).body == {"ok": True}
    assert delays == [2]
    assert len(wire.sent) == 2
    for request, _settings in wire.sent:
        assert request.headers["X-Atlassian-Token"] == "nocheck"
        assert request.headers["Content-Type"].startswith("multipart/form-data; boundary=")
        parsed = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + request.headers["Content-Type"].encode() + b"\r\n\r\n" + request.body
        )
        parts = {p.get_param("name", header="content-disposition"): p for p in parsed.iter_parts()}
        assert set(parts) == {"file", "comment", "minorEdit"}
        assert parts["file"].get_filename() == "f.png"
        assert parts["file"].get_content_type() == "image/png"
        assert parts["file"].get_payload(decode=True) == content
        assert parts["comment"].get_filename() is None
        assert parts["comment"].get_payload(decode=True) == b"hello"
        assert parts["minorEdit"].get_payload(decode=True) == b"true"
    source = tmp_path / "parts.json"
    source.write_text(json.dumps(body))
    assert build_body(f"@{source}", [], operation=op) == body


def test_multipart_json_priority_and_untagged_json_atpath_unchanged(tmp_path):
    op = operation(method="POST", extensions={}, request_media_types=["multipart/form-data", "application/json"])
    t, wire = transport([(200, b'{"ok":true}', {})])
    with t:
        response = t.call(op, {"id": "1"}, {"file": "@does-not-exist"})
    assert response.body == {"ok": True}
    request, _ = wire.sent[0]
    assert request.body == b'{"file": "@does-not-exist"}'
    assert request.headers["Content-Type"] == "application/json"
    assert "X-Atlassian-Token" not in request.headers


@pytest.mark.parametrize("body", [None, [], {}, {"file": "@"}, {"file": "@missing-binary-file"}])
def test_bad_multipart_body_refuses_before_http(body):
    t, wire = transport([])
    with t, pytest.raises(ValueError, match="multipart"):
        t.call(operation(extensions={}, request_media_types=["multipart/form-data"]), {"id": "1"}, body)
    assert not wire.sent


def test_binary_same_origin_redirect_streams_exact_bytes_and_drops_original_query(tmp_path):
    content = b"\x00\xff\xfe\x00not JSON\n"
    headers = {"Content-Type": "image/png", "Content-Disposition": 'attachment; filename="f.png"'}
    t, wire = transport([(302, b"", {"Location": "/media/file?signature=opaque"}), (200, content, headers)],
                        auth=("fixture", "test-only"))
    target = tmp_path / "chosen.bin"
    with t:
        response = t.call(operation(), {"id": "1"}, None, output=target)
    assert response.body == {"path": str(target), "bytes": len(content), "content_type": "image/png"}
    assert target.read_bytes() == content
    assert hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(content).digest()
    assert len(wire.sent) == 2
    assert wire.sent[1][0].url == "https://offline.invalid/media/file?signature=opaque"
    assert all(req.headers["Authorization"].startswith("Basic ") for req, _ in wire.sent)
    assert all(settings["stream"] for _, settings in wire.sent)


@pytest.mark.parametrize("location", [
    "https://evil.invalid/bytes", "https://api.media.atlassian.com/bytes", "http://offline.invalid/bytes",
    "https://offline.invalid:444/bytes", "https://user@offline.invalid/bytes",
    "https://offline.invalid.evil.invalid/bytes", "//evil.invalid/bytes", "file:///tmp/bytes",
    "https://offline.invalid\\@evil.invalid/bytes", "https://offline.invalid/\nbytes", "",
])
def test_binary_refuses_cross_origin_downgrade_userinfo_and_malformed_redirect(tmp_path, location):
    t, wire = transport([(302, b"", {"Location": location})])
    target = tmp_path / "out.bin"
    with t:
        response = t.call(operation(), {"id": "1"}, None, output=target)
    assert response.status == 302 and "refused" in response.body["message"]
    assert len(wire.sent) == 1 and not target.exists()


def test_binary_second_redirect_refused_and_no_partial_output(tmp_path):
    t, wire = transport([(302, b"", {"Location": "/first"}), (307, b"", {"Location": "/second"})])
    with t:
        response = t.call(operation(), {"id": "1"}, None, output=tmp_path / "out")
    assert response.status == 307 and len(wire.sent) == 2
    assert list(tmp_path.iterdir()) == []


def test_binary_redirect_refusal_names_host_without_url_secrets(tmp_path):
    location = "https://fixture-user:fixture-password@media.invalid/private-file?signature=fixture-token"
    t, wire = transport([(302, b"", {"Location": location})])
    with t:
        response = t.call(operation(), {"id": "1"}, None, output=tmp_path / "out")
    assert response.status == 302
    assert response.body == {"message": "binary redirect refused: destination host media.invalid"}
    assert len(wire.sent) == 1 and list(tmp_path.iterdir()) == []


def test_binary_retries_initial_and_redirected_statuses_and_preserves_conflict(tmp_path):
    delays = []
    t, wire = transport([(429, b'{}', {"Retry-After": "3"}), (302, b"", {"Location": "/media"}),
                         (503, b'{}', {}), (200, b"download", {})], sleep=delays.append)
    with t:
        assert t.call(operation(), {"id": "1"}, None, output=tmp_path / "out").body["bytes"] == 8
    assert delays == [3, 2] and len(wire.sent) == 4
    t, wire = transport([(409, b'{"message":"conflict"}', {})])
    with t, pytest.raises(ConflictError):
        t.call(operation(), {"id": "1"}, None, output=tmp_path / "conflict")
    assert len(wire.sent) == 1 and not (tmp_path / "conflict").exists()


@pytest.mark.parametrize("disposition,filename", [
    ('attachment; filename="../../outside.bin"', "outside.bin"),
    ("attachment; filename*=UTF-8''caf%C3%A9.bin", "café.bin"),
    ('attachment; filename=".."', "attachment.bin"),
    ("", "attachment.bin"),
])
def test_binary_default_filename_is_safe_basename(tmp_path, monkeypatch, disposition, filename):
    monkeypatch.chdir(tmp_path)
    response = binary_response(Response(200, b"raw\x00", {"Content-Disposition": disposition}))
    assert response.body == {"path": filename, "bytes": 4, "content_type": "application/octet-stream"}
    assert (tmp_path / filename).read_bytes() == b"raw\x00"


def test_explicit_output_selects_binary_without_tag_and_stream_failure_is_atomic(tmp_path, monkeypatch):
    target = tmp_path / "out"
    t, _wire = transport([(200, b"\x00\xff", {})])
    with t:
        assert t.call(operation(extensions={}), {"id": "1"}, None, output=target).body["bytes"] == 2
    target.write_bytes(b"keep previous output")
    response = requests.Response()
    response.status_code = 200
    response.close = lambda: None
    def chunks(**kwargs):
        yield b"partial"
        raise requests.ConnectionError("secret-url-must-not-leak")
    response.iter_content = chunks
    t, _wire = transport([])
    monkeypatch.setattr(t.session, "request", lambda *args, **kwargs: response)
    with t, pytest.raises(ServerError) as error:
        t.call(operation(), {"id": "1"}, None, output=target)
    assert "secret-url" not in str(error.value)
    assert target.read_bytes() == b"keep previous output"
    assert list(tmp_path.iterdir()) == [target]
