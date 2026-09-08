# Transport cassettes

`as_engine.cassette.Recorder` and `Player` implement the existing `Transport`
protocol: `call(operation, parameters, body) -> Response`. There is no second
operation API. The caller still goes through parameter validation, transforms
when enabled, and the normal surface error/output handling.

```python
from as_engine.cassette import Recorder, Player, Scrubber

scrubber = Scrubber(
    base_url="https://example.invalid",
    cloud_id="example-cloud-id",
    secrets=["example-token", "user@example.invalid"],
)
recorder = Recorder(transport, "new-session.json", scrubber=scrubber)
response = recorder.call(operation, parameters, body)
recorder.close()

player = Player("new-session.json")
response = player.call(operation, parameters, body)
```

Create a **new path** for every recording session. Existing files are refused,
so re-record to another filename, review it, then replace the old fixture. A
session has one writer; concurrent recording is unsupported. Each successful
call atomically replaces the file, with only scrubbed bytes in the temporary
file. The destination directory must exist. `close()` closes the wrapped
transport; it does not delete or truncate the recording. A wrapped transport
exception propagates and creates no interaction. To capture HTTP errors, use
`HTTPTransport(error_handler=lambda *_: None)` while recording so final error
responses reach the recorder; `Surface` still maps them to `SurfaceError`.
Transport retries still run, and only their final response is recorded.

## Format and matching

JSON has `format_version: 1` and an `interactions` array. Each interaction is:

```json
{
  "operationId": "getPages",
  "parameters": {"limit": 5},
  "body": null,
  "body_sha256": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b",
  "response": {
    "status": 200,
    "headers": {"Content-Type": "application/json"},
    "body": {"results": []}
  }
}
```

The key is the canonical operationId, normalized parameters and SHA-256 of the
**scrubbed** body. Canonical JSON sorts object keys, uses compact separators
and UTF-8, and refuses nonfinite numbers. Object order does not matter; array
order and JSON types do. The Generic Surface converts CLI parameter strings to
their schema types before calling the transport. Missing and explicit null
body both mean JSON null. No raw-body fingerprint is stored.

Playback is reusable, not a stateful sequence: identical keys and responses
coalesce; differing responses for an identical scrubbed key are refused.
Player rejects duplicate keys, invalid status/header shapes, unsupported format
versions and body-hash mismatches. A miss names the operation, parameter names
and scrubbed body hash, without printing parameter/body values. It raises
`ValueError` (usage exit 2 through the surface); there is no HTTP fallback.
Responses are copied so one caller cannot change future playback.

### Recorded headers

`as_engine.cassette.RECORDED_RESPONSE_HEADERS` defines the recording allowlist:
`Content-Type` and `Content-Disposition` at any status, and `Location` only
on 201/303 responses, when present. Names are matched case-insensitively and
written with that canonical spelling. All other response headers are omitted
after secret discovery and scrubbing. Identical request keys coalesce when
status, scrubbed body (including binary `body_base64`) and allowlisted headers
match; differences in those fields still cause a conflicting-response error.
Volatile headers alone never cause a conflict. Format version 1 is unchanged;
Player accepts older fixtures and returns their headers as recorded.

## Scrubbing and review

Register all unlabelled tokens and site identifiers **before the first call**.
`Scrubber` replaces registered values in keys, values, free text and headers,
including URL-encoded and JSON-escaped forms. Register a precomputed Basic
credential as well if it can appear outside an Authorization header.

Before each file write the scrubber discovers Authorization/Proxy-Authorization,
Cookie/Set-Cookie, email/emailAddress, accountId, cloudId, token/apiToken/
accessToken/refreshToken, password/secret, and `_links.base` string values
recursively (credential field spelling is case-insensitive and ignores `_`/`-`).
It registers the full header plus a bare Bearer/Basic credential; valid Basic
values also register their decoded email and token. Those discovered values
are replaced everywhere in the session, including earlier response echoes.
Unregistered opaque prose cannot be identified reliably; later discovery cannot
undo an earlier file already observed by someone else. Register such values
up front and review the complete recording before sharing it.

Placeholders such as `<as-site-1>` and `<as-secret-1>` are stable within the
session; the secret-to-placeholder map lives only in memory. Multiple values
remain distinct. Use the recorded placeholders in replay requests containing
sensitive fields. A Python caller may instead pass a `Scrubber` initialized
with the same registrations and order for the same session; there is no secret
map in the cassette and raw identities are not portable playback inputs.
Scrubbing that would merge object keys or contradict a request key fails.

Re-recording against a live site is a host-only maintainer step after the
JAS-43 ruling and its authorized fixture scope. It is never an install, runtime
playback or CI action. Supply synthetic content, register credentials/site/cloud
identifiers, use a new path, inspect bytes and diff, run the offline contracts,
and only then replace a fixture. Today's Confluence fixture was recorded from
an intercepted local fake service; it makes no live-service compatibility claim.
