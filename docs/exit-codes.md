# Generic Surface exit codes

The `api` group emits raw JSON on stdout by default. Failures write one JSON
object on stderr with keys `status` (HTTP integer, or null for local failure),
`messages` (array), `operation` (canonical operationId when resolved, or null),
and `note` (the operation's x-as-note, or null). A deprecated call also emits a
warning naming its replacement, or saying none is specified. Legacy command
exit codes are unchanged.

| Exit | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Other failure, including a refused redirect |
| 2 | Usage, parameter/body validation, or HTTP 400 |
| 3 | Authentication failure (401) |
| 4 | Local scope refusal (status null), or server permission refusal (403) |
| 5 | Operation/resource not found (404) |
| 6 | Server/transport failure or exhausted 429/5xx retries |
| 7 | Conflict (409); never retried or automatically refreshed |

A local usage failure has status null. A 400 retains the server's messages and
adds any body-validation diagnostics. A 409 is surfaced immediately. A network
exception becomes a generic 503 diagnostic without including a credential-bearing
request URL. Server body messages pass through the shared library sanitizer.

Example stderr, exit 2:

```json
{"status":400,"messages":["Responder forced HTTP 400"],"operation":"getPages","note":null}
```
