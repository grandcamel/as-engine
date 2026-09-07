"""Observe cassette files and replay through the public transport interface."""

import base64
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
