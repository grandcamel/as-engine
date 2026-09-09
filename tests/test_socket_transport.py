"""Exercise the SocketTransport contract through the actual fake sidecar seam."""

import pytest
from fake_sidecar import operation

from as_engine.errors import ScopeRefusal, SurfaceError
from as_engine.responder import Responder
from as_engine.serve import WireError, decode_frame, encode_frame, fake_sidecar
from as_engine.socket_transport import SocketTransport
from as_engine.surface import Surface
from as_engine.transport import Response


def test_socket_response(sidecar):
    transport = SocketTransport(sidecar.socket_path, document="platform")
    assert transport.call(operation(), {"issueIdOrKey": "SBX-1"}, None) == Response(
        200, {"key": "SBX-1"}
    )
    transport.close()


def test_scope_exception(sidecar):
    with pytest.raises(ScopeRefusal) as exc:
        SocketTransport(sidecar.socket_path).call(operation(), {"issueIdOrKey": "OTHER-1"}, None)
    assert exc.value.code == 4


@pytest.mark.parametrize(
    "status,cls,code",
    [
        (400, "ValidationError", 2),
        (401, "AuthenticationError", 3),
        (403, "PermissionError", 4),
        (404, "NotFoundError", 5),
        (409, "ConflictError", 7),
        (429, "RateLimitError", 6),
        (503, "ServerError", 6),
    ],
)
def test_http_domain_error_and_surface_exit(indexes, status, cls, code):
    from assistant_skills_lib import error_handler

    try:
        with fake_sidecar(
            surface_factory=lambda: Surface(indexes, lambda _, i: Responder(i, status=status)),
            allowlist=["SBX"],
        ) as server:
            transport = SocketTransport(server.socket_path)
            with pytest.raises(getattr(error_handler, cls)) as exc:
                transport.call(operation(), {"issueIdOrKey": "SBX-1"}, None)
            assert exc.value.status_code == status
            client = Surface(indexes, lambda *_: transport, scope_allowlist=["SBX"])
            with pytest.raises(SurfaceError) as failure:
                client.call("getIssue", {"issueIdOrKey": "SBX-1"})
            assert (failure.value.status, failure.value.code) == (status, code)
    except PermissionError as exc:
        pytest.skip(f"NOT RUN: sandbox socket bind denied: errno={exc.errno}")


@pytest.mark.parametrize(
    "endpoint",
    [
        ("example.test", 80, "token"),
        ("0.0.0.0", 80, "token"),
        ("127.0.0.1", 0, "token"),
        ("127.0.0.1", 80, ""),
        ("127.0.0.1", 80, "bad\ntoken"),
    ],
)
def test_invalid_tcp_configuration(endpoint):
    with pytest.raises(ValueError):
        SocketTransport(endpoint)


@pytest.mark.parametrize("kwargs", [{"timeout": 0}, {"timeout": float("inf")}, {"max_bytes": 1}])
def test_invalid_limits(kwargs):
    with pytest.raises(ValueError):
        SocketTransport("/not-connected", **kwargs)


@pytest.mark.parametrize(
    "op,output",
    [
        (operation(), "out"),
        (operation(extensions={"x-as-response": {"kind": "binary"}}), None),
        (operation(request_media_types=["multipart/form-data"]), None),
    ],
)
def test_file_io_refused_before_connect(op, output):
    with pytest.raises(WireError, match="does not support"):
        SocketTransport("/not-connected").call(op, {}, None, output=output)


def test_wire_size_and_unique_json():
    with pytest.raises(WireError, match="size limit"):
        encode_frame({"large": "x" * 1024}, 1024)
    with pytest.raises(WireError, match="unique members"):
        decode_frame(b'{"operationId":"first","operationId":"second"}\n')
