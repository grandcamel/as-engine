# Private installed-workflow MCP adapter

`as_engine.workflow_mcp` exposes four read-only tools over stdio, backed by one
operator-fixed installed `jira-as` executable. The pilot admits only
`list-projects`, schema 1, capability `indexed-read-v1`. The product CLI owns
parsing, configuration, Surface guards, indexed GET execution, retries, projection
and paging. The adapter validates and preserves its JSON results.

This source is a candidate for supervisor validation. Source review, synthetic
SDK tests, installed CLI checks and a real ChatGPT result are separate acceptance
stages. No native readiness or successful live request follows from this document.

## Installation and launch

The optional dependency set is `as-engine[mcp]`: `mcp==2.2.0` and
`jsonschema>=4.18`. The engine's ordinary dependency set is unchanged. Normal
engine imports do not import the adapter or require MCP. Explicit module launch
without an optional dependency exits 2 with
`workflow-mcp: missing dependency; install as-engine[mcp]` on stderr.
Python 3.10 is supported by the source; runtime validation on 3.10 and 3.11 is
required. MCP tests require the SDK and do not skip when it is missing.

After the operator prepares and freezes noneditable installs, launch with:

```text
<absolute adapter-venv>/bin/python -I -m as_engine.workflow_mcp --profile <absolute profile.json>
```

The profile is an operator launch argument, never an MCP tool argument. There is
no HTTP listener, credential lookup, tunnel configuration or new Jira command in
this module. stdout belongs to the SDK's JSON-RPC transport. Startup failures and
unresolved cleanup use sanitized stderr categories, never exception text or child
diagnostics. Invalid profiles exit 2; internal failures and unresolved stdio
shutdown exit nonzero.

`create_server(load_profile(path))` is the public async construction seam for SDK
clients. Use the returned server within its SDK lifespan so orderly close invokes
owned cleanup. Construct a new server for a new operator-approved lifetime.

## Operator profile version 1

The profile must be a regular nonsymlink file owned by the launching account and
not group/world writable. Its absolute path is read once, as strict UTF-8 JSON,
at most 64 KiB. Duplicate keys, nonfinite numbers, nesting over 32 levels and
unknown fields fail closed. Every nested configuration object is closed.

| Field | Required value |
| --- | --- |
| `schema_version` | Integer `1` |
| `adapter_id` | `[a-z][a-z0-9-]{0,79}` |
| `product` | `jira-as` |
| `executable` | Canonical absolute path to a regular executable console script; no PATH lookup or tilde expansion |
| `executable_sha256` | SHA256 of that script, 64 lowercase hex characters |
| `expected.product_version`, `expected.engine_version` | Exact installed versions, nonempty strings up to 80 characters |
| `expected.schema_version` | Integer `1` |
| `expected.catalog_revision` | Positive integer |
| `expected.definition_digest` | Exact installed definition digest, 64 lowercase hex characters |
| `expected.workflow_revisions` | Exactly `{"list-projects": <positive integer>}` |
| `context` | The closed object below |
| `limits` | Optional closed object using the limits below |

`context` requires these fields:

| Field | Meaning |
| --- | --- |
| `source` | Exactly `approved-env-v1` |
| `account_email` | Configured account email, at most 254 characters |
| `site_url` | Canonical lowercase HTTPS origin, at most 2048 characters; no userinfo, path, query or fragment |
| `home`, `cwd`, `tmpdir` | Canonical absolute existing directories owned by the launching account and not group/world writable |
| `scope` | Closed object with the three optional fields below |

`scope.allowed_projects` defaults to `[]`. It contains at most 100 unique, sorted
keys matching `[A-Z][A-Z0-9_]{0,79}`. `scope.allow_site_operations` defaults to
`false`; `scope.default_project` defaults to `null`, or must name an allowed
project. There is no unrestricted/null project allowlist. An empty project list
allows no named projects. Site discovery requires its own permission.

Use values derived from the frozen install and approved host context; placeholder
versions and hashes do not establish a binding. The profile contains no token or
arbitrary environment dictionary. Script hashes are checked during bootstrap and
immediately before each child launch. They do not attest every imported package
or prevent hostile concurrent package replacement. Operator-controlled profile,
install and directories are trusted inputs. Native acceptance requires separate
package artifact hashes and a frozen install. Upgrade or context changes require
stopping the instance, renewing the profile and repeating validation; no hot reload.

## Approved host context and native hold

At profile load the adapter snapshots the six admitted Jira environment values:
`JIRA_SITE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_ALLOWED_PROJECTS`,
`JIRA_ALLOW_SITE_OPERATIONS` and `JIRA_DEFAULT_PROJECT`. It also snapshots the
transport and routing settings below for admission checks only.

Before a run, require a complete nonempty credential trio, matching canonical
site/email, and explicit project/site policy matching the profile. Project policy
uses comma-separated keys, case normalization and deduplication; site policy
accepts only true/false, with surrounding whitespace and case normalized. Default
project must exactly match the profile, including absence when it is null.

An explicitly empty `JIRA_ALLOWED_PROJECTS=""` means `[]` and is preserved into
the child. Missing `JIRA_ALLOWED_PROJECTS` is refused, since product absence can
mean unrestricted access. Missing site policy is also refused. The adapter never
infers site permission from SBX, a default project or an empty project list. A
false site flag is forwarded unchanged for the product's existing guard to enforce.

Missing or malformed context returns `runtime-context-unavailable`; a mismatching
identity or scope returns `identity-binding-mismatch`. Both prevent run-child
creation. This binds configured identity, not the authenticated owner behind a
token; supervisor host evidence must establish that identity.

`JIRA_AS_TRANSPORT` must be absent or the literal `http`. Empty values, simulation,
socket, cassette, responder and other selections are refused rather than silently
replaced. Nonempty `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, their lowercase forms,
`REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `SSL_CERT_FILE` or `SSL_CERT_DIR` also refuse
run admission. This pilot supports direct HTTPS only and does not discard an
unsupported selected context while claiming to preserve it. Discovery remains pure.

Each child environment starts empty. Generated settings provide fixed HOME,
TMPDIR, cwd, executable-directory plus system PATH, `LANG=C`, `LC_ALL=C`,
`NO_COLOR=1`, `PYTHONNOUSERSITE=1` and `PYTHONUTF8=1`. Run children receive only
the admitted Jira values, `JIRA_AS_TRANSPORT=http` and
`JIRA_SCOPE_ENFORCEMENT=enforcing`; that pin outranks any product settings file
in the fixed cwd, so a run child never skips the scope guard. Discovery children receive
no Jira credentials or execution-policy values. Bare credential aliases, arbitrary
JIRA settings, Python path overrides, Platform/tunnel secrets and routing overrides
are not forwarded. Credentials never enter argv or result metadata.

The adapter neither sources `.env` nor calls Keychain, searches for replacement
configuration, imports ConfigManager, creates a broker or rewrites retry policy.
The fixed product HOME/cwd remain part of the approved configuration context.
Supervisor acceptance must verify installed configuration precedence and exclude
unsafe fallback or descendant behavior.

**Native activation is held pending an explicitly approved host site scope.** The
current Grand Camel `jira-dev-host` route supplies credentials and an SBX default
project but lacks explicit project/site policy flags. It does not supply this
adapter's required site-read authorization. This charge grants no broker changes,
new flags, account changes or tunnel setup. The supervisor must identify an
existing approved route supplying the exact context or obtain a separately scoped
decision. No successful SDK fixture removes this hold.

## Four tools and fixed dispatch

Bootstrap runs credential-free `workflows list --offset=0 --format=json`, followed
by describe for `list-projects`. Both complete envelopes must match the frozen
versions, digest, revisions, one-entry catalog, declared integer bounds and GET
binding. No bootstrap run, HTTP request or automatic traversal occurs. Discovery
success leaves availability unknown. A valid product compatibility failure is
saved unchanged; initialization and all four generic tool names remain available
for diagnosis, with no advertised callable ID/bounds and empty run inputs. Calls
return the saved failure without launching children. Restart is operator-owned.

Every tool input object is closed; nested run inputs are closed as well. Integers
must be actual JSON integers, excluding booleans and integral floats. Null, string
coercions and tool-supplied executable/env/site/account/policy/transport/argv/file
authority are refused. Defaults are applied once by the handler.

| Tool | Accepted arguments |
| --- | --- |
| `workflows_list` | Optional integer `offset`, default 0, range 0..9223372036854775807 |
| `workflows_search` | Required `query`, 1..512 characters, non-whitespace, no NUL; optional discovery offset |
| `workflows_describe` | Required `workflow: "list-projects"`; optional boolean `examples`, default false |
| `workflows_run` | Required `workflow: "list-projects"`; optional `inputs`, default `{}`; integer `limit` 1..100/default 25 and `offset` 0..9223372036854775807/default 0 |

Unknown tool names return protocol invalid-params `-32602`. Known tools with an
unsupported workflow return adapter `unsupported-workflow`; other malformed
arguments return `invalid-input`. Neither launches a child. All four tools carry
read-only and non-destructive annotations; those hints do not replace guards.

The executable is fixed; argv is a vector with no shell. Every scalar option is
emitted once as `--name=value`; query/workflow follows a positional `--` delimiter.
Describe optionally adds `--examples`. Run emits only `--limit`, `--offset`,
`--format=json` and the admitted workflow. No example execution or automatic next
page occurs. Request another page explicitly using product continuation metadata.

MCP SDK 2.2.0 owns raw JSON-RPC decoding. A raw stdio test pins duplicate-key
handling at that boundary and requires supervisor observation. The adapter cannot
recover duplicate keys already collapsed by the SDK; validated values still
produce one option each. This module does not fork the transport parser.

## Results and errors

`structuredContent` contains the exact validated product JSON object; the single
text content block decodes to the same object. Text escapes HTML/backticks/control
bytes so provider strings remain data. `isError` is true for a nonzero product
exit or an adapter failure. The SDK serializes these fields in camelCase.

Product stdout is accepted only with exit zero and empty stderr. Exits 1..7 must
have one JSON object on stderr and empty stdout, with matching envelope exit code.
Invalid UTF-8, duplicate/nonfinite JSON, multiple objects, mismatched streams,
unknown schema fields, identity drift or echoed token/Basic credential values
produce sanitized adapter failures. No raw diagnostic stream is returned.
Identity/schema incompatibility blocks subsequent dispatch until operator restart.

Closed output schemas distinguish list/search, describe, examples and run.
Describe inputs are metadata bounds; examples inputs are empty. Run inputs are
normalized scalars, or empty on genuine pre-normalization failures. Errors preserve
empty evidence, HTTP-status evidence or indexed-read evidence as delivered. Partial
unknown reads can retain valid items and a range larger than returned_count:
received/omitted counts and sanitized metadata are preserved. The adapter never
recomputes paging, repairs URLs or fabricates a completion signal.

Exit zero can have `complete=false` or `null`; unknown malformed reads use exit 1
with null completeness/continuation. A continuation does not prove a snapshot or
full coverage. Read each result's reason, evidence and next actions.

Adapter failures have `adapter_schema_version: 1`, `status: "adapter-error"` and
one closed `error` object containing `code`, `phase`, `child_exit_code`,
`stdout_bytes`, `stderr_bytes`, `counts_complete`, `cleanup` and `dispatch_blocked`.
Phases are bootstrap/admission/execute/cleanup; cleanup is not-started/reaped/
unresolved. Counts are observed bytes, and are incomplete unless both pipes reach
EOF. No product result fields or raw arguments/exception text enter this envelope.

Exit/count/EOF/cleanup evidence is frozen for the specific invocation before its
owned task returns. Schema rejection, final result-size rejection and other
postexecution failures retain that observation: a reaped child remains reported
as reaped with its actual exit and observed bytes. Predispatch and busy refusals
use not-started/null-exit/zero-byte evidence, never the previous or concurrent
call's observations. A saved blocked failure repeats its original evidence without
launching another child; its counts describe that original attempt, not a fresh
execution. Late cleanup cannot rewrite the saved failure's snapshot.

Codes are invalid-input, unsupported-workflow, incompatible-catalog,
incompatible-output, runtime-context-unavailable, identity-binding-mismatch,
executable-unavailable, executable-changed, busy, launch-failed, timeout, cancelled,
output-limit, invalid-json, unexpected-stream, child-exit, unsafe-output,
cleanup-unresolved and internal-error. A timeout is an adapter interruption, not
evidence that the product exhausted its retry policy.

## Bounds and owned cleanup

| Profile limit | Default | Accepted integer range |
| --- | --- | --- |
| `call_timeout_seconds` | 180 | 1..600 |
| `discovery_timeout_seconds` | 10 per child | 1..30 |
| `stdout_max_bytes` | 1048576 | 4096..1048576 |
| `stderr_max_bytes` | 1048576 | 4096..1048576 |
| `terminate_grace_seconds` | 2 | 1..5 |
| `kill_grace_seconds` | 2 | 1..5 |
| `drain_grace_seconds` | 1 | 1..5 |

One child is admitted across all tools and bootstrap. Concurrent requests receive
`busy`; there is no child wait queue. A run's deadline includes creation and
stream collection; two bootstrap discovery calls each have their own deadline.
Product retries and Retry-After can exceed 180 seconds, so it is not a product
worst-case guarantee. The adapter does not retry the CLI.

Both pipes are read concurrently in chunks at most 16 KiB. Each retains at most
its configured cap plus a one-byte overflow probe that is counted but not retained.
Excess bytes are drained during cleanup, never accumulated. JSON nesting is
bounded to 32; decoded tool arguments to 16 KiB; the serialized CallToolResult to
4 MiB. Oversize results fail without truncating product data. These are decoded
handler and child-output bounds, not a raw incoming SDK-frame memory limit.

An owned task shields spawn/handle transfer from public request cancellation. It
retains the child, pipe readers and waiter through cleanup. Terminate and then
kill operate only through that child handle, with their bounded grace periods;
pipe EOF is independently required. Reader cancellation outcomes, gathers and
late tasks are consumed to avoid leaking asyncio diagnostics. Normal attached
cleanup adds at most the configured terminate/kill/drain sum (5 seconds by
default, apart from scheduling overhead).

If spawn itself remains unsettled, its settlement wait is bounded by the same
grace sum before permanent refusal. A late handle is still transferred and cleaned
up, potentially after the request has ended. Clean late reaping never silently
clears the refusal. Orderly close also waits for retained tasks under one deadline
of twice the grace sum plus one second (11 seconds by default); it never treats a
request's completion as proof that late ownership has settled.
An unreaped child, unsettled spawn or pipe, reader failure or
inherited pipe without EOF yields `cleanup-unresolved`, blocks all subsequent
child dispatch and retains owned state. Tools/list remains inspectable. A client
that cancelled may receive no response; safe failure state is retained and only
its sanitized category/count envelope is emitted to stderr.

There are no new process groups/sessions, group signals, PID searches, borrowed
PIDs or hard-death containment promise. An inherited-pipe fixture establishes
detection/refusal only. Native acceptance must prove the fixed HTTP CLI does not
spawn unmanaged descendants in its approved configuration. A server/tunnel hard
death or requirement for stronger containment needs a separate scope decision.
Do not automatically restart around unresolved ownership.

## Acceptance still required

The supervisor owns static checks, full suites before any commit, package builds,
Python 3.10/3.11 SDK wire checks, frozen-wheel CLI smoke and both-root landing.
Engine synthetic-child tests establish adapter contracts only. Shared Jira
CLI/Surface scenario tests follow engine acceptance in a separate source phase.

Before native use, separately prove account/site/scope binding, installed config
precedence, child ownership behavior, the approved host route, actual personal
ChatGPT/tunnel access and the client's schema/deadline/cancellation behavior.
Only then compare one real native `list-projects` page (limit 25, offset 0) with
the fixed installed CLI in the same account, scope and time window, preserving
semantic items/order, reasons, continuation and provenance. SDK green alone does
not close this native gate. No live writes, broader workflows, publication or
tunnel/account provisioning are authorized by this pilot.
