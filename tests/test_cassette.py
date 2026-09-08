"""Observe cassette files and replay through the public transport interface."""

import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

import pytest

from as_engine.cassette import Player, Recorder, Scrubber
from as_engine.index import Operation
from as_engine.transport import Response


@pytest.fixture
def operation():
    return Operation("getThings", "GET", "/things", [], None, None, [], None, None, {}, [])


class Wire:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.closed = False

    def call(self, operation, parameters, body):
        return next(self.responses)

    def close(self):
        self.closed = True


class BinaryWire:
    def __init__(self, payload, headers=None):
        self.payload = payload
        self.headers = headers or {"Content-Type": "image/png"}

    def call(self, operation, parameters, body, *, output=None):
        assert output is not None
        Path(output).write_bytes(self.payload)
        return Response(
            200,
            {"path": str(output), "bytes": len(self.payload), "content_type": "image/png"},
            self.headers,
        )


class BinaryThenHeaderWire:
    def __init__(self, payload, secret):
        self.payload = payload
        self.secret = secret
        self.calls = 0

    def call(self, operation, parameters, body, *, output=None):
        self.calls += 1
        if self.calls == 1:
            assert output is not None
            Path(output).write_bytes(self.payload)
            return Response(
                200,
                {"path": str(output), "bytes": len(self.payload), "content_type": "image/png"},
            )
        return Response(200, {"ok": True}, {"Authorization": "Bearer " + self.secret})


def test_scrubs_echoes_encoded_values_keys_and_hashes_before_writing(tmp_path, operation):
    token = "fixture-token+/=SECRET"
    email = "maintainer@fixture.invalid"
    site = "https://private-fixture.invalid"
    cloud = "fixture-cloud-secret"
    basic = base64.b64encode(f"{email}:{token}".encode()).decode()
    body = {"email": email, "token": token, "content": site + "/wiki/" + cloud}
    response = Response(
        200,
        {
            "echo": token + " " + quote(token, safe="") + " " + email,
            "accountId": "private-account-id",
            "other": "private-account-id",
            "_links": {"base": site},
            "cloudId": cloud,
            token: "key must be scrubbed too",
        },
        {"Authorization": "Basic " + basic, "Set-Cookie": "session=secret-cookie"},
    )
    path = tmp_path / "recording.json"
    wire = Wire(response)
    recorder = Recorder(
        wire, path, scrubber=Scrubber(secrets=[token, email], base_url=site, cloud_id=cloud)
    )
    assert recorder.call(operation, {"limit": 3}, body) == response
    recorder.close()
    assert wire.closed
    raw = path.read_text()
    for secret in (
        token,
        quote(token, safe=""),
        email,
        site,
        cloud,
        basic,
        "private-account-id",
        "secret-cookie",
    ):
        assert secret not in raw
    entry = json.loads(raw)["interactions"][0]
    player = Player(path)
    replay = player.call(operation, {"limit": 3}, entry["body"])
    assert replay.status == 200 and "as-secret" in replay.body["echo"]
    assert body["token"] == token and response.body["accountId"] == "private-account-id"
    assert list(tmp_path.iterdir()) == [path]


def test_normalized_matching_preserves_types_array_order_and_copies(tmp_path, operation):
    path = tmp_path / "session.json"
    response = Response(201, {"values": [1]}, {"Content-Type": "application/json"})
    recorder = Recorder(Wire(response, response), path)
    recorder.call(operation, {"b": False, "a": [1, 2]}, {"z": 2, "a": 1})
    recorder.call(operation, {"a": [1, 2], "b": False}, {"a": 1, "z": 2})
    assert len(json.loads(path.read_text())["interactions"]) == 1
    player = Player(path)
    first = player.call(operation, {"a": [1, 2], "b": False}, {"a": 1, "z": 2})
    first.body["values"].append(2)
    first.headers["Content-Type"] = "changed"
    assert player.call(operation, {"a": [1, 2], "b": False}, {"z": 2, "a": 1}) == response
    for params, body in (
        ({"a": [2, 1], "b": False}, {"a": 1, "z": 2}),
        ({"a": [1, 2], "b": 0}, {"a": 1, "z": 2}),
        ({"a": [1, 2], "b": False}, {"a": "1", "z": 2}),
    ):
        with pytest.raises(ValueError, match="cassette miss: getThings"):
            player.call(operation, params, body)
    with pytest.raises(ValueError, match="cassette miss: absent"):
        player.call(replace(operation, operationId="absent"), {}, None)
    player.close()


def test_conflicting_response_and_existing_session_refused_without_overwrite(tmp_path, operation):
    path = tmp_path / "session.json"
    recorder = Recorder(Wire(Response(200, [1]), Response(200, [2])), path)
    recorder.call(operation, {}, None)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="conflicting responses"):
        recorder.call(operation, {}, None)
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="already exists"):
        Recorder(Wire(), path)
    assert path.read_bytes() == before


def test_new_secret_discovery_rescrubs_prior_session_entries(tmp_path, operation):
    path = tmp_path / "session.json"
    recorder = Recorder(
        Wire(
            Response(200, {"text": "echo-account"}),
            Response(404, {"accountId": "echo-account", "message": "missing"}),
        ),
        path,
    )
    recorder.call(operation, {"id": 1}, None)
    recorder.call(operation, {"id": 2}, None)
    assert "echo-account" not in path.read_text()
    assert Player(path).call(operation, {"id": 2}, None).status == 404


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {},
        {"format_version": 9},
        {"format_version": 1, "interactions": {}},
        {"format_version": 1, "interactions": [{}]},
    ],
)
def test_invalid_files_fail_closed(tmp_path, bad):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError):
        Player(path)


def test_tampered_hash_status_and_duplicate_keys_fail_closed(tmp_path, operation):
    path = tmp_path / "file.json"
    Recorder(Wire(Response(200, "ok")), path).call(operation, {}, None)
    original = path.read_text()
    for failure in ("hash", "status", "headers", "duplicate"):
        data = json.loads(original)
        entry = data["interactions"][0]
        if failure == "hash":
            entry["body"] = "changed"
        elif failure == "status":
            entry["response"]["status"] = True
        elif failure == "headers":
            entry["response"]["headers"] = {"x": 1}
        else:
            data["interactions"].append(entry)
        path.write_text(json.dumps(data))
        with pytest.raises(ValueError):
            Player(path)


def test_write_failure_preserves_old_scrubbed_file_and_cleans_temporary(
    tmp_path, operation, monkeypatch
):
    path = tmp_path / "session.json"
    recorder = Recorder(Wire(Response(200, "one"), Response(200, "two")), path)
    recorder.call(operation, {"id": 1}, None)
    before = path.read_bytes()

    def denied(*args):
        raise OSError("fixture rename denied")

    monkeypatch.setattr(Path, "replace", denied)
    with pytest.raises(OSError, match="rename denied"):
        recorder.call(operation, {"id": 2}, None)
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_registered_secrets_replay_and_miss_diagnostics_do_not_echo_values(tmp_path, operation):
    path = tmp_path / "session.json"
    Recorder(Wire(Response(200, "ok")), path, scrubber=Scrubber(secrets=["secret-value"])).call(
        operation, {"q": "secret-value"}, None
    )
    player = Player(path, scrubber=Scrubber(secrets=["secret-value"]))
    assert player.call(operation, {"q": "secret-value"}, None).body == "ok"
    with pytest.raises(ValueError) as caught:
        player.call(operation, {"q": "secret-value"}, {"password": "another-secret"})
    assert "secret-value" not in str(caught.value) and "another-secret" not in str(caught.value)


def test_json_body_base64_fields_remain_subject_to_normal_scrubbing(tmp_path, operation):
    path = tmp_path / "session.json"
    Recorder(
        Wire(Response(200, {"body_base64": "registered-secret"})),
        path,
        scrubber=Scrubber(secrets=["registered-secret"]),
    ).call(operation, {}, None)
    raw = path.read_text()
    assert "registered-secret" not in raw
    assert json.loads(raw)["interactions"][0]["response"]["body"]["body_base64"] == "<as-secret-1>"


def test_multipart_recording_hashes_metadata_without_file_bytes_or_paths(tmp_path, operation):
    source = tmp_path / "confidential.txt"
    source.write_bytes(b"confidential multipart payload")
    operation = replace(
        operation, operationId="upload", request_media_types=["multipart/form-data"]
    )
    path = tmp_path / "session.json"

    Recorder(Wire(Response(201, {"id": "new"})), path).call(
        operation, {}, {"file": "@" + str(source), "comment": "safe"}
    )

    raw = path.read_text()
    entry = json.loads(raw)["interactions"][0]
    assert entry["body"] == {"multipart": entry["body"]["multipart"]}
    part = next(part for part in entry["body"]["multipart"] if part["name"] == "file")
    assert part["filename"] == "confidential.txt"
    assert "confidential multipart payload" not in raw
    assert str(source) not in raw
    assert Player(path).call(
        operation, {}, {"file": "@" + str(source), "comment": "safe"}
    ).body == {"id": "new"}


def test_binary_recording_snapshots_bytes_and_player_replays_them(tmp_path, operation):
    operation = replace(
        operation, operationId="download", extensions={"x-as-response": {"kind": "binary"}}
    )
    path = tmp_path / "binary.json"
    captured = tmp_path / "capture.bin"
    payload = b"\x00PNG\xffpayload"

    recorder = Recorder(BinaryWire(payload), path)
    response = recorder.call(operation, {}, None, output=captured)
    entry = json.loads(path.read_text())["interactions"][0]["response"]
    assert response.body["path"] == str(captured)
    assert entry["body"] is None
    assert base64.b64decode(entry["body_base64"], validate=True) == payload
    assert str(captured) not in path.read_text()
    replay_path = tmp_path / "replay.bin"
    replay = Player(path).call(operation, {}, None, output=replay_path)
    assert replay_path.read_bytes() == payload
    assert replay.body["path"] == str(replay_path)


def test_invalid_binary_cassette_and_incompatible_replay_fail_closed(tmp_path, operation):
    operation = replace(
        operation, operationId="download", extensions={"x-as-response": {"kind": "binary"}}
    )
    path = tmp_path / "binary.json"
    payload = {
        "format_version": 1,
        "interactions": [
            {
                "operationId": "download",
                "parameters": {},
                "body": None,
                "body_sha256": hashlib.sha256(b"null").hexdigest(),
                "response": {
                    "status": 200,
                    "headers": {},
                    "body": None,
                    "body_base64": "%%%",
                },
            }
        ],
    }
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="invalid cassette binary response"):
        Player(path)

    payload["interactions"][0]["response"]["body_base64"] = base64.b64encode(b"ok").decode()
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="requires a binary operation"):
        Player(path).call(replace(operation, extensions={}), {}, None)


def test_binary_cassette_preserves_base64_and_refuses_discovered_payload_secrets(
    tmp_path, operation
):
    operation = replace(
        operation, operationId="download", extensions={"x-as-response": {"kind": "binary"}}
    )
    coincidence = base64.b64encode(b"abc").decode()
    path = tmp_path / "coincidence.json"
    Recorder(BinaryWire(b"abc"), path, scrubber=Scrubber(secrets=[coincidence])).call(
        operation, {}, None, output=tmp_path / "coincidence.bin"
    )
    assert json.loads(path.read_text())["interactions"][0]["response"]["body_base64"] == coincidence
    assert Player(path).call(operation, {}, None, output=tmp_path / "replay.bin")

    secret = "header-discovered-secret"
    blocked = tmp_path / "blocked.json"
    with pytest.raises(ValueError, match="registered secret"):
        Recorder(
            BinaryWire(b"prefix-" + secret.encode(), {"Authorization": "Bearer " + secret}),
            blocked,
        ).call(operation, {}, None, output=tmp_path / "blocked.bin")
    assert not blocked.exists()

    later = tmp_path / "later.json"
    recorder = Recorder(BinaryThenHeaderWire(b"prefix-" + secret.encode(), secret), later)
    recorder.call(operation, {}, None, output=tmp_path / "first.bin")
    with pytest.raises(ValueError, match="registered secret"):
        recorder.call(replace(operation, operationId="other", extensions={}), {}, None)
    assert len(json.loads(later.read_text())["interactions"]) == 1


def test_player_rejects_non_2xx_or_missing_binary_body(tmp_path, operation):
    operation = replace(
        operation, operationId="download", extensions={"x-as-response": {"kind": "binary"}}
    )
    path = tmp_path / "binary.json"
    body_hash = hashlib.sha256(b"null").hexdigest()
    interaction = {
        "operationId": "download",
        "parameters": {},
        "body": None,
        "body_sha256": body_hash,
        "response": {"status": 302, "headers": {}, "body": None, "body_base64": "b2s="},
    }
    path.write_text(json.dumps({"format_version": 1, "interactions": [interaction]}))
    with pytest.raises(ValueError, match="2xx"):
        Player(path).call(operation, {}, None, output=tmp_path / "ignored.bin")

    interaction["response"] = {"status": 200, "headers": {}, "body": {"not": "binary"}}
    path.write_text(json.dumps({"format_version": 1, "interactions": [interaction]}))
    with pytest.raises(ValueError, match="missing body_base64"):
        Player(path).call(operation, {}, None, output=tmp_path / "ignored.bin")


@pytest.mark.parametrize("status", [200, 201, 302, 303, 307, 400])
def test_volatile_headers_coalesce_with_exact_canonical_allowlist(tmp_path, operation, status):
    path = tmp_path / "stable.json"
    stable = {
        "content-type": "application/json",
        "CONTENT-DISPOSITION": 'attachment; filename="result.json"',
        "lOcAtIoN": "/things/1",
    }
    volatile = {
        "Atl-Request-Id",
        "Atl-Traceid",
        "Date",
        "Server-Timing",
        "X-Amz-Cf-Id",
        "X-Arequestid",
        "Ratelimit",
        "X-Ratelimit-Remaining",
        "Set-Cookie",
        "X-Aaccountid",
        "Via",
        "Connection",
        "Transfer-Encoding",
        "Retry-After",
    }
    first = Response(status, {"id": 1}, {**stable, **dict.fromkeys(volatile, "first")})
    second = Response(
        status,
        {"id": 1},
        {**{k.upper(): v for k, v in stable.items()}, **dict.fromkeys(volatile, "second")},
    )
    recorder = Recorder(Wire(first, second), path)
    assert recorder.call(operation, {}, None) == first
    assert recorder.call(operation, {}, None) == second
    entries = json.loads(path.read_text())["interactions"]
    expected = {
        "Content-Type": "application/json",
        "Content-Disposition": 'attachment; filename="result.json"',
    }
    if status in (201, 303):
        expected["Location"] = "/things/1"
    assert len(entries) == 1
    assert entries[0]["response"] == {"status": status, "body": {"id": 1}, "headers": expected}


@pytest.mark.parametrize("status", [200, 201, 303])
def test_absent_response_headers_are_not_invented(tmp_path, operation, status):
    path = tmp_path / "absent.json"
    Recorder(Wire(Response(status, None)), path).call(operation, {}, None)
    assert json.loads(path.read_text())["interactions"][0]["response"]["headers"] == {}


@pytest.mark.parametrize(
    "second",
    [
        Response(201, {"id": 2}, {"Content-Type": "application/json", "Location": "/things/1"}),
        Response(303, {"id": 1}, {"Content-Type": "application/json", "Location": "/things/1"}),
        Response(201, {"id": 1}, {"Content-Type": "text/plain", "Location": "/things/1"}),
        Response(201, {"id": 1}, {"Content-Type": "application/json", "Location": "/things/2"}),
    ],
    ids=["body", "status", "content-type", "location"],
)
def test_allowlist_keeps_meaningful_response_conflicts(tmp_path, operation, second):
    path = tmp_path / "conflict.json"
    first = Response(201, {"id": 1}, {"Content-Type": "application/json", "Location": "/things/1"})
    recorder = Recorder(Wire(first, second), path)
    recorder.call(operation, {}, None)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="^cassette has conflicting responses for getThings$"):
        recorder.call(operation, {}, None)
    assert path.read_bytes() == before


def test_legacy_generic_surface_headers_replay_unchanged(operation):
    # Two interactions preserved from Confluence's pre-allowlist recording.
    path = Path(__file__).parent / "fixtures/legacy-generic-surface.json"
    before = path.read_bytes()
    data = json.loads(before)
    assert data["format_version"] == 1
    player = Player(path)
    for entry in data["interactions"]:
        response = entry["response"]
        assert response["headers"] == {
            "Authorization": "<as-secret-4>",
            "Content-Type": "application/json",
            "Set-Cookie": "<as-secret-6>",
        }
        assert player.call(
            replace(operation, operationId=entry["operationId"]),
            entry["parameters"],
            entry["body"],
        ) == Response(response["status"], response["body"], response["headers"])
    assert path.read_bytes() == before


def test_binary_allowlist_preserves_filename_and_payload_conflicts(
    tmp_path, operation, monkeypatch
):
    operation = replace(operation, extensions={"x-as-response": {"kind": "binary"}})
    headers = {
        "content-type": "image/png",
        "content-disposition": 'attachment; filename="recorded.png"',
        "Date": "first",
    }
    payload = b"\x00PNG\xffpayload"
    wire = BinaryWire(payload, headers)
    path = tmp_path / "binary-headers.json"
    recorder = Recorder(wire, path)
    recorder.call(operation, {}, None, output=tmp_path / "capture.bin")
    headers["Date"] = "second"
    recorder.call(operation, {}, None, output=tmp_path / "capture-again.bin")
    entries = json.loads(path.read_text())["interactions"]
    assert len(entries) == 1
    assert entries[0]["response"]["headers"] == {
        "Content-Type": "image/png",
        "Content-Disposition": 'attachment; filename="recorded.png"',
    }
    before = path.read_bytes()
    wire.payload = b"different bytes"
    with pytest.raises(ValueError, match="^cassette has conflicting responses for getThings$"):
        recorder.call(operation, {}, None, output=tmp_path / "different.bin")
    assert path.read_bytes() == before
    monkeypatch.chdir(tmp_path)
    replay = Player(path).call(operation, {}, None)
    assert replay.body == {
        "path": "recorded.png",
        "bytes": len(payload),
        "content_type": "image/png",
    }
    assert (tmp_path / "recorded.png").read_bytes() == payload
