"""Small public-seam fixtures shared by the Split Mode tests."""

import json
import socket
from dataclasses import asdict
from pathlib import Path

import pytest

from as_engine.index import Operation, ProductIndexes
from as_engine.serve import fake_sidecar


def operation(name="getIssue", **changes):
    values = {
        "operationId": name,
        "method": "GET",
        "path": "/issue/{issueIdOrKey}",
        "tags": [],
        "summary": None,
        "description": None,
        "parameters": [{"name": "issueIdOrKey", "in": "path", "type": "string", "required": True}],
        "requestBody": None,
        "response_200": None,
        "extensions": {"x-as-scope": {"in": "key", "name": "issueIdOrKey", "separator": "-"}},
        "reachable_schemas": [],
        "response_example": {"key": "SBX-1"},
    }
    values.update(changes)
    return Operation(**values)


@pytest.fixture
def indexes(tmp_path):
    entries = [
        operation(),
        operation(
            "createIssue",
            method="POST",
            path="/issue",
            parameters=[],
            requestBody={
                "schema": {
                    "type": "object",
                    "required": ["project", "kind"],
                    "properties": {
                        "project": {"type": "string"},
                        "kind": {"type": "string", "enum": ["task"]},
                    },
                }
            },
            request_body_required=True,
            extensions={"x-as-scope": {"in": "body", "paths": ["/project"]}},
        ),
        operation("binary", extensions={"x-as-response": {"kind": "binary"}}),
        operation("upload", request_media_types=["multipart/form-data"]),
        operation("site", path="/site", parameters=[], extensions={"x-as-scope": {"in": "site"}}),
    ]
    (tmp_path / "catalog.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "documents": [{"id": "platform", "tier": "primary", "file": "index.json"}],
            }
        )
    )
    (tmp_path / "index.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "operations": {op.operationId: asdict(op) for op in entries},
                "schemas": {},
            }
        )
    )
    return ProductIndexes(tmp_path)


@pytest.fixture
def sidecar(indexes):
    try:
        with fake_sidecar(indexes, allowlist=["SBX"]) as server:
            yield server
    except PermissionError as exc:
        if exc.errno != 1:
            raise
        pytest.skip("NOT RUN: sandbox socket bind denied: errno=1 Operation not permitted")


def raw_call(server, request, *, prelude=b"", malformed=False):
    family = socket.AF_UNIX if server.socket_path is not None else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.connect(str(server.socket_path) if family == socket.AF_UNIX else server.tcp)
        connection.sendall(
            prelude + (request if malformed else (json.dumps(request) + "\n").encode())
        )
        with connection.makefile("rb") as stream:
            return json.loads(stream.readline())


def request(name="getIssue", **changes):
    value = {"operationId": name, "parameters": {"issueIdOrKey": "SBX-1"}, "body": None}
    value.update(changes)
    return value


def logs(server):
    return [json.loads(line) for line in Path(server.call_log).read_text().splitlines()]
