"""Observe sidecar policy, audit records, validation and cleanup over real sockets."""

import json
import os
import signal
import stat
import subprocess
import sys

import pytest
from fake_sidecar import logs, raw_call, request

from as_engine.responder import Responder
from as_engine.serve import fake_sidecar, serve
from as_engine.surface import Surface
from as_engine.transport import Response


def test_raw_scope_forgery_logged(sidecar):
    reply = raw_call(sidecar, request(parameters={"issueIdOrKey": "OTHER-1"}, argv_identity="SBX"))
    assert reply["error"]["kind"] == "scope"
    assert reply["error"]["code"] == 4
    assert logs(sidecar)[0]["outcome"] == "refused scope"
    assert logs(sidecar)[0]["parameters"] == {"names": ["issueIdOrKey"], "identity": ["OTHER-1"]}


@pytest.mark.parametrize(
    "parameters", [{}, {"issueIdOrKey": 42}, {"issueIdOrKey": "SBX-1", "token-secret": "hidden"}]
)
def test_server_checks_parameters(sidecar, parameters):
    assert raw_call(sidecar, request(parameters=parameters))["error"]["kind"] == "validation"
    assert len(logs(sidecar)) == 1
    assert "hidden" not in sidecar.call_log.read_text()
    assert "token-secret" not in sidecar.call_log.read_text()


@pytest.mark.parametrize(
    "body", [None, {}, {"project": "SBX", "kind": "bug"}, {"project": "SBX", "kind": 1}]
)
def test_server_checks_required_body_type_enum(sidecar, body):
    result = raw_call(sidecar, request("createIssue", parameters={}, body=body))
    assert result["error"]["kind"] == "validation"


def test_body_identity_derived_and_hint_ignored(sidecar):
    result = raw_call(
        sidecar,
        request(
            "createIssue",
            parameters={},
            body={"project": "SBX", "kind": "task"},
            argv_identity="OTHER",
        ),
    )
    assert result["status"] == 200
    result = raw_call(
        sidecar,
        request(
            "createIssue",
            parameters={},
            body={"project": "OTHER", "kind": "task"},
            argv_identity="SBX",
        ),
    )
    assert result["error"]["kind"] == "scope"


@pytest.mark.parametrize(
    "change",
    [
        {"scope_allowlist": None},
        {"document": "../../secrets"},
        {"document": 1},
        {"argv_identity": []},
        {"operationId": "noSuchOperation"},
    ],
)
def test_forged_envelope_or_document_refused(sidecar, change):
    assert "error" in raw_call(sidecar, request(**change))
    assert len(logs(sidecar)) == 1
    assert "secrets" not in sidecar.call_log.read_text()


@pytest.mark.parametrize(
    "frame", [b"not-json\n", b"[]\n", b'{"body":null,"body":null}\n', b'{"body":NaN}\n', b"\xff\n"]
)
def test_bad_json_is_one_refused_call(sidecar, frame):
    assert raw_call(sidecar, frame, malformed=True)["error"]["kind"] == "protocol"
    assert logs(sidecar)[0]["outcome"] == "refused protocol"


@pytest.mark.parametrize("name", ["binary", "upload"])
def test_raw_file_io_refused(sidecar, name):
    result = raw_call(sidecar, request(name, body={"file": "@/private/secret"}))
    assert result["error"]["kind"] == "unsupported"
    assert "secret" not in sidecar.call_log.read_text()


def test_site_denied_by_default(sidecar):
    assert raw_call(sidecar, request("site", parameters={}))["error"]["kind"] == "scope"


def test_permissions_and_one_line_per_outcome(indexes):
    index = indexes.get("platform")
    responder = Responder(index)
    responder.seed(
        "getIssue",
        [Response(200, {"ok": True}), Response(503, {"message": "credential-must-not-escape"})],
    )
    try:
        with fake_sidecar(
            surface_factory=lambda: Surface(indexes, lambda *_: responder), allowlist=["SBX"]
        ) as server:
            assert stat.S_IMODE(server.socket_path.stat().st_mode) == 0o600
            assert stat.S_IMODE(server.call_log.stat().st_mode) == 0o600
            assert raw_call(server, request())["status"] == 200
            assert (
                raw_call(server, request(parameters={"issueIdOrKey": "OTHER-1"}))["error"]["kind"]
                == "scope"
            )
            error = raw_call(server, request())
            assert error["error"]["status"] == 503
            assert "credential-must-not-escape" not in json.dumps(error)
            assert [row["outcome"] for row in logs(server)] == [
                "ok 200",
                "refused scope",
                "error server",
            ]
            assert "credential-must-not-escape" not in server.call_log.read_text()
            path = server.socket_path
        assert not path.exists()
    except PermissionError as exc:
        pytest.skip(f"NOT RUN: sandbox socket bind denied: errno={exc.errno}")


def test_tcp_auth_before_json(indexes):
    try:
        with fake_sidecar(
            indexes, allowlist=["SBX"], tcp=("127.0.0.1", 0), token="session-test"
        ) as server:
            result = raw_call(server, b"not json\n", malformed=True, prelude=b"Bearer wrong\n")
            assert result["error"]["kind"] == "authentication"
            assert raw_call(server, request(), prelude=b"Bearer session-test\n")["status"] == 200
            assert len(logs(server)) == 2
            assert "session-test" not in server.call_log.read_text()
    except PermissionError as exc:
        pytest.skip(f"NOT RUN: sandbox TCP bind denied: errno={exc.errno}")


def test_existing_path_never_replaced(indexes, tmp_path):
    path = tmp_path / "existing"
    path.write_text("owned by another process")
    with pytest.raises(ValueError, match="already exists"):
        serve(
            lambda: Surface(indexes, lambda _, i: Responder(i)),
            socket_path=path,
            call_log=tmp_path / "calls",
            allowlist=["SBX"],
        )
    assert path.read_text() == "owned by another process"


@pytest.mark.parametrize("kind", ["symlink", "public", "hardlink"])
def test_unsafe_log_refuses_before_bind(indexes, tmp_path, kind):
    log = tmp_path / "log"
    target = tmp_path / "target"
    target.write_text("retained")
    if kind == "symlink":
        log.symlink_to(target)
    elif kind == "hardlink":
        target.chmod(0o600)
        os.link(target, log)
    else:
        log.write_text("retained")
        log.chmod(0o644)
    with pytest.raises((ValueError, OSError)):
        serve(
            lambda: Surface(indexes, lambda _, i: Responder(i)),
            socket_path=tmp_path / "s",
            call_log=log,
            allowlist=[],
        )
    assert target.read_text() == "retained"


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_foreground_signal_cleanup(indexes, sidecar, tmp_path, signum):
    # sidecar fixture is also the explicit host-capability gate.
    from tempfile import TemporaryDirectory

    with TemporaryDirectory(prefix="as-") as root:
        script = "from as_engine.index import ProductIndexes; from as_engine.serve import serve; from as_engine.surface import Surface; from as_engine.responder import Responder; import sys; i=ProductIndexes(sys.argv[1]); serve(lambda: Surface(i, lambda _, ix: Responder(ix)), socket_path=sys.argv[2], call_log=sys.argv[3], allowlist=['SBX'], ready=lambda _: print('ready', flush=True))"
        path = root + "/s"
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path), path, root + "/log"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout.readline().strip() == "ready"
            process.send_signal(signum)
            assert process.wait(timeout=5) == 0
            assert not os.path.exists(path)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


@pytest.mark.parametrize(
    "change,kind",
    [
        ({}, None),
        ({"parameters": {"issueIdOrKey": "OTHER-1"}, "argv_identity": "SBX"}, "scope"),
        ({"parameters": {}}, "validation"),
        ({"parameters": {"issueIdOrKey": 1}}, "validation"),
        ({"document": "platform"}, None),
    ],
)
def test_exact_wire_request_handler_without_socket(indexes, change, kind):
    from as_engine.serve import encode_frame, handle_request

    surface = Surface(indexes, lambda _, index: Responder(index))
    result, record = handle_request(
        surface, encode_frame(request(**change), 8192), allowlist=["SBX"]
    )
    if kind is None:
        assert result == {"status": 200, "headers": {}, "body": {"key": "SBX-1"}}
        assert record["outcome"] == "ok 200"
    else:
        assert result["error"]["kind"] == kind
        assert record["outcome"] == f"refused {kind}"
        if change["parameters"] == {}:
            assert result["error"]["message"] == "missing required parameter: issueIdOrKey"


def test_body_handler_without_socket(indexes):
    from as_engine.serve import encode_frame, handle_request

    surface = Surface(indexes, lambda _, index: Responder(index))
    for project, kind in [("SBX", None), ("OTHER", "scope")]:
        frame = encode_frame(
            request(
                "createIssue",
                parameters={},
                body={"project": project, "kind": "task"},
                argv_identity="SBX",
            ),
            8192,
        )
        result, _ = handle_request(surface, frame, allowlist=["SBX"])
        if kind:
            assert result["error"]["kind"] == kind
        else:
            assert result["status"] == 200


def test_validation_reason_without_value_echo(indexes):
    from as_engine.serve import encode_frame, handle_request

    frame = encode_frame(
        request("createIssue", parameters={}, body={"project": "SBX", "kind": "synthetic-secret"}),
        8192,
    )
    result, record = handle_request(
        Surface(indexes, lambda _, index: Responder(index)), frame, allowlist=["SBX"]
    )
    assert result["error"]["message"] == "body.kind: is not an allowed value"
    assert "synthetic-secret" not in json.dumps((result, record))
