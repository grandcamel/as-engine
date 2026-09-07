# Enrichment Entry and operation tag contract

JAS-35 freezes the names below for paging/prerequisite transforms (JAS-37),
rich-text work (JAS-38) and help (JAS-40). Tags describe behavior; this change
does not execute lookups, paging, or invocations. Products own their overlays.
`compile_product` validates provenance before emitting any compiled files.
The low-level `apply_overlay` / `compile_document` APIs remain usable for
plain structural overlays. All operation-level `x-as-*` tags survive into
an index Operation's `extensions` unchanged.

## Entry provenance

Every Overlay action, including `remove`, has these action-level fields:

| Field | Required value |
|---|---|
| `description` | Nonempty human-readable description |
| `x-as-reason` | Nonempty reason the Base Document needs enrichment |
| `x-as-evidence` | Object with `url` (HTTP/HTTPS source URL) and `date` (`YYYY-MM-DD`) |
| `x-as-origin` | Nonempty ticket or research-document reference |
| `x-as-test` | Stable, unique per-product test identifier |

These belong alongside `target` and `update`/`remove`, never inside the update
payload. For example:

```json
{
  "target": "$.paths['/pages'].get",
  "update": {
    "x-as-paging": {
      "style": "cursor",
      "request": {
        "token": {"in": "query", "name": "cursor"},
        "limit": {"in": "query", "name": "limit"}
      },
      "itemsPath": "/results",
      "next": {"kind": "link", "path": "/_links/next"}
    }
  },
  "description": "Declare cursor paging for getPages.",
  "x-as-reason": "The paging transform needs the continuation link and item array.",
  "x-as-evidence": {
    "url": "https://developer.atlassian.com/cloud/confluence/rest/v2/intro/#pagination",
    "date": "2026-09-06"
  },
  "x-as-origin": "JAS-35",
  "x-as-test": "paging_v2_getPages"
}
```

Missing fields fail with
`<overlay file>: target <target>: missing required field <name>`.
Invalid values also name the source, target and field. Evidence dates record
the observation date, not a generator run timestamp; regeneration is stable.

`entry_cases(spec_dir)` generates one case per manifest-ordered action, with
`case.id` equal to `x-as-test`. A product parametrizes one test with those ids
and calls `case.check(validate_body=validator)`. The callback receives `(instance, schema,
enriched_document)` and must raise for an invalid body; omitting it for a
schema-bearing example is an error. It proves the target exists in the Base Document,
then checks the action's effect in its ordered overlay context. Duplicate test
ids fail discovery. Add new properties by targeting their existing parent;
entries must not target nodes introduced only by another entry.

Optional action-level `x-as-examples` is a list of:

- `{"kind":"invocation","value":"confluence-as api call getPages --limit 5"}`:
  a nonempty shell-style argv string parsed with `shlex.split`, never executed.
  This proves quoting/tokenization, not that an unlanded CLI accepts its flags.
- `{"kind":"json","value":"{\"spaceId\":\"123\"}","schema":"#/components/requestBodies/PageCreateRequest/content/application~1json/schema"}`:
  serialized JSON (a complete body or a field fragment), with an optional local
  JSON Pointer schema reference.
  Bodies without a schema are parsed. A resolvable schema must be validated by
  the product test's body validator; explicitly missing/external references fail.

Schema validation is test-only: no runtime import or runtime dependency on
`jsonschema`, and no external reference retrieval. Examples may reference
inline request schemas as well as component schemas. Overlay target syntax is
the applier's small JSONPath subset; **tag paths and example schema fragments
are JSON Pointers**, with `~0`/`~1` escaping. Empty `itemsPath` means the entire
response array. Response paths below are relative to the JSON body, without `#`.

## `x-as-paging`

Each list operation carries an object with `style`, `request`, and `itemsPath`.
`request` maps semantic roles (`offset`, `limit`, `token`) to
`{"in":"query"|"path","name":"actual OpenAPI parameter name"}`.
Only parameters the operation actually accepts may be named. Their types and
bounds still come from OpenAPI. `limit` here means server page size; a CLI's
aggregate `--limit` is a separate total-items cap.

| Style | Request roles | Response metadata and continuation |
|---|---|---|
| `cursor` | `token`, optional `limit` | `next: {kind: "link", path: "/_links/next"}` or `{kind: "token", path: "/cursor"}` |
| `nextPageToken` | `token`, optional `limit` | `next: {kind: "token", path: "/nextPageToken"}` |
| `offset/limit` | `offset`, `limit` | `response.totalPath` or `response.isLastPath`; offset starts at zero |
| `start/limit` | `offset` names `start`, `limit` names `limit` | `response: {offsetPath: "/start", limitPath: "/limit", sizePath: "/size"}` |
| `ancestor` | `token` is the current ancestor path id; optional `limit` | `next: {kind: "token", path: "/results/0/id"}`, `merge: "prepend"` |
| `none` | Empty object | One response only; never replay a mutation or bulk lookup |

Absent `merge` means append. The `ancestor` style is required by Confluence's
`getPageAncestors` operation description: fetch higher ancestors with the first
returned id, prepending them to preserve top-to-bottom order. It is not cursor
paging. Consumers must reject unknown styles rather than assume offset paging.

Link/token styles stop when continuation is missing/null/empty (ancestor stops
on an empty array). Repeated continuations must fail rather than loop. Links
must remain on the configured service origin; a tag grants no arbitrary URL
access. Offset styles advance by the number of returned items, not requested
size; honor declared total/is-last signals and stop on an empty page. For
`start/limit`, use the returned offset plus size; the returned limit can differ
from the requested limit. A short page relative to the returned limit ends
that sequence. Missing required paging metadata is an error, not license to
invent a parameter or spin indefinitely.

The v2 `listSpacePermissionCombinations` endpoint returns the cursor directly
at `/cursor`; most cursor endpoints return a link. The next descriptor is
authoritative, not an assumption that every cursor response has `_links`.

A `none` response with several independent arrays uses `itemsPaths` (list of
JSON Pointers) instead of `itemsPath`. The whole response is retained; no
pagination or flattening between those arrays occurs. Confluence v1's
`getAvailableContentStates` has `/spaceContentStates` and `/customContentStates`.

Confluence's generator adapts JAS-9's local refs/allOf + array-sibling
classifier. Generated overlays precede hand overlays in the manifest; the
applier recursively merges updates and appends arrays. The generator never
writes the hand files. A response-wrapper signal without a corresponding
request input is explicitly handed to review. In particular, v1 mutation
responses reuse start/limit wrappers but are `none`, not repeatable reads.
An untagged operation has no declared paging contract, not implicit `none`.

## `x-as-prerequisites`

This is a list of key-to-id lookup specifications. Page create uses:

```json
{
  "x-as-prerequisites": [{
    "target": {"in": "body", "path": "/spaceId"},
    "alias": "space-key",
    "operationId": "getSpaces",
    "parameter": {"in": "query", "name": "keys", "array": true},
    "resultsPath": "/results",
    "matchPath": "/key",
    "valuePath": "/id"
  }]
}
```

`alias` is the key-form flag without leading `--`. `target` is either a body
JSON Pointer or a parameter `{in: "path"|"query", name: "..."}`. `operationId`
resolves in the same document's operation index. `parameter` receives the alias
value; `array: true` wraps one alias as a one-element query array. Read
`resultsPath`, compare each element's `matchPath` exactly with the supplied key,
require exactly one match, and extract its `valuePath`. Zero/multiple matches
are errors; do not take the first unrelated space. Follow the lookup's paging
tag as necessary. A supplied id avoids lookup; id and alias together must be
rejected as conflicting input. Preserve the destination schema type (the
Confluence create body expects a string `spaceId`).

## `x-as-version`

Page update uses:

```json
{
  "x-as-version": {
    "operationId": "getPageById",
    "parameters": {"id": {"in": "path", "name": "id"}},
    "responsePath": "/version/number",
    "target": {"in": "body", "path": "/version/number"},
    "increment": 1,
    "overrides": [{"when": {"path": "/status", "equals": "draft"}, "value": 1}]
  }
}
```

`parameters` maps read-operation parameter names to source-operation inputs.
If the target already exists, preserve it without a read. Otherwise apply a
matching literal override first, or read the current version and add
`increment`. The draft override comes from the pinned `PageUpdateRequest`
schema's version description; it needs no read. The normal read must fetch the
current version (no historical `version` argument). Require an integer version;
never convert a failed read into a guessed value. A write conflict (409) is
surfaced without retrying the write or refreshing version automatically.

## Scope of enforcement

See [operation scope guard](guard.md) for the `x-as-scope` tag and runtime refusal contract.

The engine enforces Entry provenance and generated entry checks. Tag execution
and exhaustive generic tag-schema enforcement belong to their consumers.
Confluence's build-seam tests separately verify these actual tag destinations,
request parameters, lookup operations and response-schema paths resolve in the
vendored documents, and that the resulting index carries the tags.

## Help topics: x-as-topic

An operation-level list of topic strings enrolls the operation in topic help,
for example `"x-as-topic": ["adf", "representations"]`. Topic membership comes
only from this enrichment tag, never base tags or text matching. The seed list
and four-level rendering contract are in [help.md](help.md). Each new overlay
action still carries full JAS-35 provenance and a unique `x-as-test` identifier.

## Advisory prose: x-as-note

An operation-level string holds the gotcha/help prose shown by topic help and
Level 2. The existing error object's `note` uses this same extension. Notes may
point to another operation or a lower tier without changing an operation's API
behavior. Keep notes short enough for the help page's token budget.

## Risk: x-as-risk

An operation-level string is `safe`, `destructive`, or `irreversible` (absent
means safe). Level 2 shows it. Product CLI adapters default destructive and
irreversible operations to a zero-request JSON preview and require `--confirm`
to invoke the normal guarded call path. Direct Surface calls do not implement
this product CLI policy. See [help.md](help.md) for preview fields and limits.
