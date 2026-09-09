# Split Mode protocol

The same installed product supplies the client and server indexes. The host
runs `serve(surface_factory, call_log=..., allowlist=[...], allow_site=False,
socket_path=...)` in the foreground. The factory provides a Surface backed by
its normal HTTP transport; no credential is serialized by the socket client.
Both sides validate parameters, and the server always validates request bodies
and executes its own Surface guard. The server's scope policy is fixed at start.

Each connection carries exactly one request and one response. Frames are UTF-8
JSON objects ending in LF, capped at 8 MiB including LF. Duplicate object members,
non-finite numbers, invalid UTF-8, unknown envelope members and incomplete frames
are refused. No pipelining, retries or streaming is provided. The default socket
I/O/frame deadline is 30 seconds; this is a total deadline per incoming frame,
not a promise to cancel an upstream HTTP call. The engine accepts positive finite
`timeout` and a `max_bytes` between 1024 and 8388608. Server calls are sequential.

Request:

```json
{"operationId":"getIssue","parameters":{"issueIdOrKey":"SBX-1"},"body":null,"document":"platform"}
```

`operationId`, `parameters` (object) and `body` are required. `document` is an
optional catalog ID; it selects only the server's indexed document. Canonical
operation IDs are required. Optional `argv_identity` is a string hint only: it
never changes authorization and is not used as the guard's trusted identity.
For body scope the server derives that cross-check from the indexed body paths;
missing, numeric-only or otherwise unresolved identity remains fail-closed.
Other scope resolution continues through the existing Surface and consumer rules.
The client does not send operation metadata, URLs, scope overrides, output paths
or credentials. Binary/download operations and multipart uploads are refused on
both sides (including raw socket requests).

Success:

```json
{"status":200,"headers":{},"body":{"key":"SBX-1"}}
```

Failure:

```json
{"error":{"kind":"scope","message":"Sidecar scope policy refused this call","status":null,"code":4}}
```

Errors preserve HTTP status and stable Surface exit codes. HTTP statuses map
back to the shared domain exceptions; local policy remains `ScopeRefusal`.
Error kinds include scope, protocol, validation, unsupported, authentication,
permission, not_found, conflict, rate_limit, server, api, connection and internal.
Upstream error text is replaced with a generic message to avoid credential echoes.

Unix sockets are 0600, with an owner-private parent directory recommended. The
server refuses any existing path and removes only its own device/inode on exit.
SIGINT/SIGTERM stop the main-thread loop and restore previous signal handlers.
`stop_event` and `ready(bound_address)` support embedding and test readiness.

TCP accepts only literal loopback IPv4/IPv6 addresses and a per-session token.
Before the JSON request the client sends a separate ASCII line:

```text
Bearer <session-token>
```

The server reads and constant-time compares that line BEFORE parsing any JSON.
The token is 1–4087 printable ASCII non-whitespace characters; the auth line is
bounded at 4096 bytes. Tokens are not logged or included in replies. TCP is for
processes sharing a host network namespace, not a public network listener.

Each accepted connection produces one JSON log line, including malformed,
unauthenticated, refused and error attempts. Unknown operations have null ID,
method and path. Known operations use index templates and only known parameter
names. The identity summary is restricted to bounded project/issue key syntax;
arbitrary values and all body content other than this identity are excluded.

```json
{"ts":"2026-09-09T00:00:00+00:00","operationId":"getIssue","method":"GET","path":"/issue/{issueIdOrKey}","parameters":{"names":["issueIdOrKey"],"identity":["OTHER-1"]},"outcome":"refused scope"}
```

Outcome is `ok <status>`, `refused <kind>` or `error <kind>`. Logs are opened
append-only and created 0600; existing logs must be owned regular 0600 files
with one link. Symlinks are refused. The server writes/fsyncs the single record
before sending the response; a write failure stops serving rather than returning
an unaudited success. A disk failure after an upstream mutation cannot undo that
mutation; do not retry an uncertain response automatically.

## Fake sidecar

```python
from as_engine.serve import fake_sidecar
from as_engine.socket_transport import SocketTransport
from as_engine.surface import Surface

with fake_sidecar(product_indexes, allowlist=["SBX"]) as sidecar:
    client = Surface(
        product_indexes,
        lambda document, index: SocketTransport(sidecar.socket_path, document=document),
        scope_allowlist=["SBX"],
    )
    result = client.call("getIssue", {"issueIdOrKey": "SBX-1"})
```

The default factory uses Responder. A `surface_factory` argument can supply a
seeded Responder or simulation Surface. The context manager waits for readiness,
propagates startup errors, stops and joins its thread, and removes the temporary
socket/log directory. It never changes global environment or loads credentials.
Tests whose sandbox forbids socket binds must report that capability as NOT RUN;
the same tests must pass on a socket-capable host for runtime acceptance.
