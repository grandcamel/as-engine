# Bounded indexed-read workflows

`as_engine.workflows` supplies a pure catalog and one execution capability,
`indexed-read-v1`. Products own the installed JSON bytes, user-facing names,
selectors, fixed parameters, indexed binding and examples. The shared module
contains no product operation names, service paths, credentials or transport
selection. It does not implement writes, run receipts, status recovery,
procedures, shell commands or an automatic paging loop.

This is the first source implementation, not workflow certification, installed
artifact proof, native-tool proof or live acceptance. Existing Surface behavior
and all other product commands remain unchanged.

## Public Python interface

```python
from as_engine.workflows import Catalog, normalize_inputs, run, validate_binding

# raw is bytes read by the product from its own installed package resource.
# The product/version arguments come from installed product metadata.
catalog = Catalog.load(raw, product=product_name, product_version=product_version)
listing = catalog.list(offset=0)
matches = catalog.search("Which assets are available?", offset=0)
summary = catalog.describe("list-assets")
examples = catalog.describe("list-assets", examples=True)
hint = catalog.hint()

definition = catalog.get("list-assets")
inputs = normalize_inputs(definition, {"limit": 25, "offset": 0})
validate_binding(definition, installed_indexes)
result = run(definition, inputs, product_surface)
```

`Catalog.load(bytes, *, product, product_version)` validates the complete catalog,
including all definitions, before returning a catalog. It reads only the supplied
bytes and installed `as-engine` distribution metadata. Importing the module and
list/search/describe/hint do not construct a Surface, read an index or configuration,
resolve credentials, import an HTTP stack, or call a transport. There is no build
or remote Base Document fallback. The engine version is `unknown` if distribution
metadata is absent; installed-artifact validation must reject such provenance.

`Catalog.get(id)` returns a `Definition`; its `data` and `provenance` properties
return detached dictionaries. Treat definitions as catalog values. The Python
interface is not a sandbox against a caller executing arbitrary Python.
`Catalog.provenance` also returns a detached dictionary.

`WorkflowError.result` is a sanitized result envelope. Load/get/normalize/binding
validation raise this error; catalog discovery and `run` return envelopes for
expected failures. Unexpected programming/import failures are not reclassified
as successful compatibility fallbacks. `failure_result(provenance, *, code=2,
reason='configuration-or-parameters', status='blocked', support=True)` is the
adapter's helper for a known local failure. Never pass raw exception messages
as reason codes or substitute provider text into next actions.

`normalize_inputs` accepts a mapping of integer values, excluding booleans and
strings. Only `limit` and `offset` are supported. Products parse scalar options
and reject duplicate occurrences at their public argv seam before a mapping can
lose that information. Unknown inputs, limit outside 1–100, negative/out-of-bound
offset, or non-integers produce `needs-input`, exit 2. Defaults are limit 25 and
offset 0. Definition provenance carries `input_bounds` on every workflow result.

`validate_binding(definition, indexes)` permits early incompatibility refusal
before product Surface/configuration construction. `run` repeats the check on
`surface.indexes` immediately before calling the Surface. The caller provides
the existing product Surface with its current operator configuration. Execution
calls `surface.call(canonical_id, parameters, None)` without keyword overrides.
It leaves guard enforcement, parameter checks, transport creation/closure and
existing read retries to that Surface. One logical indexed call is not a claim
of one wire attempt. Create a fresh product Surface for each public invocation
so product-local configuration caching cannot outlive the invocation.

## Schema 1

The top-level JSON object has these required fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer 1, excluding boolean. |
| `product`, `product_version` | Exact installed product name/version supplied at load. |
| `revision` | Positive catalog revision. |
| `workflows` | Nonempty array of definitions, with unique stable IDs. |

Only optional `annotations` is accepted at the root and definition level for
additive descriptive metadata. Unknown execution fields are rejected; this
explicit namespace avoids accepting new semantics by accident. Annotations are
non-executable and affect the byte digest. JSON duplicate member names, unsupported
schemas/capabilities, malformed definitions, and catalog/product version mismatch
fail with `blocked`, exit 2, reason `incompatible-definition-or-runtime`.

Each definition requires:

| Field | Contract |
| --- | --- |
| `id`, `revision` | Lowercase hyphenated stable ID; positive definition revision. |
| `requires` | Exactly `["indexed-read-v1"]`, supported by `CAPABILITIES`. |
| `title`, `purpose`, `search_terms` | Bounded descriptions and task-language search terms. |
| `inputs` | Exactly `limit` and `offset` integer descriptors. |
| `prerequisites` | Bounded descriptive strings; never executable steps. |
| `binding` | Exact indexed GET binding described below. |
| `projection` | JSON Pointers for `id`, `key`, `name`, plus canonical URL descriptor. |
| `paging` | Pinned offset/limit tag and explicit optional response evidence. |
| `examples` | Existing `{kind,value}` entries, kind `invocation` or `json`; optional `schema`. |

Input descriptors require `type`, `default`, `minimum`, `maximum`; optional
`description` is descriptive only. Limit uses integer/default25/min1/max100.
Offset uses integer/default0/min0 and an explicit maximum within the indexed
parameter bounds, including its int32/int64 format when declared. The checker
validates both ends of each input range and fixed values through the existing
parameter checker, so incompatible bounds cannot wait until a remote call.

The `binding` requires exactly `kind`, `document`, `operation_id`, `method`, `path`,
`fixed_parameters`, `input_parameters`, `parameter_schemas`, and `scope`.
`kind` is `indexed-read`, method is `GET`, and the path is an absolute service
path without query/fragment/templates. Inputs map to distinct query parameters;
fixed scalar query values cannot collide with them. For example:

```json
{
  "kind": "indexed-read",
  "document": "inventory",
  "operation_id": "enumerateAssets",
  "method": "GET",
  "path": "/v1/assets",
  "fixed_parameters": {"sort": "key"},
  "input_parameters": {"limit": "count", "offset": "start"},
  "parameter_schemas": {
    "count": {"in": "query", "schema": {"type": "integer", "format": "int32"}},
    "start": {"in": "query", "schema": {"type": "integer", "format": "int64"}},
    "sort": {"in": "query", "schema": {"type": "string", "enum": ["key"]}}
  },
  "scope": {"in": "site"}
}
```

These descriptors pin the **effective compiled parameter schemas** used by
Surface, not the full Base Document parameter prose. Retain the full indexed
`schema` object where present; for flattened records retain the same
`type/enum/minimum/maximum/items/properties` fields the Surface checker uses.
The compiler can omit Base Document defaults on flattened string parameters;
do not claim omitted defaults as indexed evidence. Fixed values are explicit.

Canonical resolution must return the declared document, exact operation ID,
GET method and path; aliases and missing/replaced operations cannot substitute.
Scope is either direct `{"in":"site"}` or a direct query identity whose name
is among fixed parameters. It must equal the indexed `x-as-scope` tag. This first
capability does not accept resolver scope or transform procedures.
It rejects request bodies, required bodies, and `x-as-prerequisites`,
`x-as-version`, `x-as-format`, `x-as-richtext`, or `x-as-response` metadata.
Those would need an explicitly supported later capability. Definitions cannot
pass allow-site, allowlist, body, executable, site, credential or transport overrides.

Projection and paging example (service-specific paths remain product data):

```json
{
  "projection": {
    "id": "/uuid", "key": "/code", "name": "/label",
    "canonical_url": {"pointer": "/self", "path": "/v1/assets/{identity}"}
  },
  "paging": {
    "tag": {
      "style": "offset/limit", "itemsPath": "/entries",
      "request": {
        "offset": {"in": "query", "name": "start"},
        "limit": {"in": "query", "name": "count"}
      },
      "response": {"totalPath": "/total"}
    },
    "evidence": {"offset": "/start", "limit": "/count", "total": "/total", "is_last": "/last"}
  }
}
```

The complete tag must match indexed `x-as-paging`. Its request roles must match
the input mapping; any supplied tag response pointer must match the corresponding
evidence role. Evidence requires offset/limit descriptors and at least one of
total/is_last. Descriptors are required in the definition; values are optional
in actual responses. Additional evidence such as `is_last` need not be in the
paging tag, but its property and type must exist in the indexed response schema.

Schema checks follow local component references and object properties, requiring
an object envelope, an array at itemsPath, string identity/name/link properties,
integer offset/limit/total and boolean is_last. This capability does not infer
union/allOf schema semantics or array-index selectors. Unknown/missing/changed
schema evidence fails before dispatch. A complete synthetic definition and
compiled fixture live in `tests/test_workflows.py`.

## Discovery, provenance and rendering

The generic hint retains product/version, catalog revision and capability, and
starts discovery with `workflows search "task" --format json` without naming an
operation or workflow ID. Default descriptions include the first authored
`invocation` example, when present, as a detached `{kind,value}` entry under
`examples`. `examples=True` still returns all authored examples; JSON-only
definitions do not gain an invented invocation. Description/example caps remain
1,200/600 token proxies, including the added example.

List/search/describe state `support=true`, `availability=unknown`. This declares
support, not account access or certification. An unknown workflow has
`support=false`, `needs-input`, exit 2 and a `search-catalog` next action.
An unrelated search has an explicit empty entries array. Search uses casefolded
word overlap over title, purpose and search terms, ignores common grammatical
words, and sorts equal scores by stable ID. Queries are nonempty and at most
512 characters. Search/list offsets refer to the ordered matching catalog.
The complete escaped search envelope must also fit the discovery output budget,
including an empty match set or the unchanged query repeated in continuation.
Unicode escaping can exhaust that budget even when the input is within 512
characters. If the query prevents a result page from fitting, search returns a
bounded `needs-input`, exit 2, with reason `query-output-too-large` and a
`shorten-query` next action. This refusal does not echo or truncate the query,
return a partial match set, or supply continuation. Retry with a shorter query.
It is an input/output constraint, not a definition or installed-package mismatch;
an oversized definition is still rejected as incompatibility at catalog load.

Discovery pagination fits each complete entry within 3,200 Markdown characters
including the final newline (800-token proxy). Continuation explicitly includes
the next offset and, for search, the same query. Descriptions fit 4,800 characters
and examples fit 2,400. A definition that cannot fit individually is rejected at
load; no fields/entries are silently trimmed. The product must separately keep
its entire level0, including the derived hint, within 1,600 characters. Existing
help caps remain 400/800/1200/600; this module does not edit them.

Every result identifies installed product/version, engine version, schema and
required schema, supported/required capabilities, catalog and definition revision,
workflow ID where applicable, and SHA-256 of the exact loaded JSON bytes.
`definition_digest` hashes the whole installed catalog resource; it is separate
from any build stamp that hashes only Python/build configuration. Whitespace
changes alter this digest. The hint derives from the same catalog and gives
product/version, catalog revision, capability and starting search/describe
syntax; detailed hashes are retrieved on demand.

`render_result(result, format='json'|'markdown')` returns one string without a
final newline. JSON is a compact complete envelope. Markdown renders the same
values as a JSON data block with one top-level field per line.
`result_document(result)` exposes the established `{level,title,sections}` help
shape, using a `{kind:'json',value:...}` data example. `render_help` JSON/Markdown
round-tripping preserves that document. Provider strings are JSON-escaped,
including backticks, HTML delimiters and control bytes; they never become
headings, executable examples or next actions. Decoding the data block recovers
the exact result values, including duplicate names.

The product adapter writes the single result to stdout only for exit 0. For
nonzero exit it writes the corresponding result to stderr with empty success
stdout. Format selection and duplicate scalar option detection belong to the
product CLI. The shared renderer does not print or exit.

## Read-result and paging semantics

Run results carry normalized inputs, input bounds, items, returned count,
limit/offset, range start/end, completeness, continuation, evidence/reason and
safe next actions. Successful reads have `status=completed-read`, exit 0.
There is no write verification claim or run receipt. HTTP 404 is a failure,
never an empty successful result.

Items retain exact nonempty string id/key/name, with duplicate names as separate
identities. Duplicate IDs or keys and malformed identities mark the page unknown.
A canonical URL must be a valid HTTPS provider-self URL with no userinfo,
query/fragment, whitespace or controls, matching the declared detail path and
returned ID or key. Otherwise `url` and `url_source` are null; valid links have
`url_source=provider-self`. Other provider URLs are not substituted. Links and
nextPage fields are never followed or interpreted as execution authority.

A page must have the declared object/array shape and no more than the requested
limit. Integer metadata excludes booleans. Supplied offset must equal the request;
supplied page size must be positive, no larger than the requested limit and no
smaller than the returned count. Let `end = offset + received_count`:

- Total greater than end or is_last=false proves more results; total equal to
  end or is_last=true proves a final page only if every supplied signal agrees.
- Negative/invalid totals, total less than end, offset mismatch, invalid page
  size, contradictory signals, invalid items, duplicates and overflow produce
  `unknown`, exit 1, complete=null and no automatic continuation.
- A valid nonempty nonfinal page with matching offset evidence has
  complete=false and continuation `{workflow,inputs:{limit,offset:end}}`.
  A server-reduced page size advances by the actual count. If the next offset
  exceeds declared bounds, report more results but provide no continuation.
- Missing total/is_last gives complete=null and a reason. Missing offset
  evidence prevents automatic continuation. A consistent final signal can still
  establish that no page follows the requested range.
- An empty nonfinal page is unknown/no-progress, never a loop. An empty page
  with no final signal has unknown completeness. An empty page is complete only
  with positive consistent final evidence, such as offset0/total0/is_last=true.

For malformed or oversized pages, bounded usable items may be retained with
explicit `received_count` and `omitted_count`; no truncation is hidden. The range
refers to the provider's received range, not the count of retained valid items.
Malformed envelope/array counts are null because no range can be established.
Completeness concerns whether another page follows this range. Only a traversal
from offset 0 to a final page covers the full fixture result set; multiple real
reads are not a transactional snapshot of a changing service.

Surface error mapping preserves existing exit meanings without echoing server
dumps, exception text, secrets or request URLs:

| Exit | Workflow status | Meaning |
| --- | --- | --- |
| 2 | blocked | Surface configuration, credentials, parameter validation or HTTP 400. Workflow input errors instead use needs-input. |
| 3 | blocked | HTTP 401. |
| 4 | blocked | Current local scope refusal or HTTP 403. |
| 5 | failed | HTTP 404. |
| 6 | failed | Existing transport/network failure or exhausted 429/5xx retries. |
| 7 | failed | HTTP 409, retaining existing conflict behavior. |
| 1 | failed / unknown | Other Surface failure / malformed read evidence respectively. |

## Product adapter and acceptance boundary

Import this optional module lazily inside the new product group/help path.
Catch `ModuleNotFoundError` only when its `name` is exactly
`as_engine.workflows`; unrelated nested import failures remain real errors.
A missing module or declared capability must produce a structured
incompatible-definition-or-runtime result before task transport. Existing
api/help and lazy legacy groups must remain usable with a supported older
engine, and level0 must retain its current bytes when the new capability is
absent. Never upgrade/downgrade the operator environment as a fallback.

The adapter loads the one product resource, uses these shared catalog/result
semantics, and retains real configuration/guard/transport seams. If product
Surface creation rejects an invalid mode/configuration before `run`, the adapter
uses `failure_result(definition.provenance)` without raw exception text. Duplicate
argv checks, actual missing-module/older-wheel pairing, installed resource/hash
alignment, product example execution, help golden changes, and the fresh-agent
smoke remain product/supervisor acceptance work. Tests in this source slice
exercise a synthetic indexed GET through the real Surface; they do not establish
those product or installed/runtime outcomes.
