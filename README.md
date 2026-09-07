# as-engine

The spec-driven engine behind the `*-as` command-line tools (jira-as, confluence-as).

It reads Atlassian's published OpenAPI documents as Base Documents, applies
Enrichment Entries (an OpenAPI Overlay in a small target subset) where the
documents are silent or wrong, compiles the Enriched Spec into a compact
operation index at build time, and interprets that index at runtime: a
Generic Surface that can call any operation by its published name, tag-driven
transforms (rich text, paging, prerequisite resolution), a guard that enforces
project scope, and progressive-disclosure help. Products stay thin: they hold
their Base Documents, overlays, Wrapper Verbs and configuration.

Status: bootstrap (JAS-32). The design is the spec on JAS-31 in the
grand-camel-platform tracker; the decisions live on the wayfinder map JAS-6.

## Index API

Products build their vendored OpenAPI documents with
`as_engine.build.compile_product(spec_dir, out_dir)`. The source directory
contains `manifest.json`; each document entry records its source filename,
declared API version (`info.version`), SHA-256, tier, overlay filenames, and explicitly
stripped top-level extensions. The build writes deterministic `<id>.index.json`
files and `catalog.json`. The supported input format is OpenAPI 3; local
component references are resolved without network access.

Overlay targets support dot keys, quoted bracket keys, and nonnegative bracket
indexes (including chained brackets). Updates merge objects and append arrays;
removals delete the selected element. Targets must exist; add properties by
updating their parent object. Filters, wildcards, recursive descent, slices,
and `copy` actions are refused explicitly. Metadata on actions is preserved
without enforcement; enrichment validation belongs to the product.

`compile_document(document, overlays, normalizers=...)` also accepts defect
normalization callables, applied after overlays. No vendor defects are patched
implicitly. JSON uses sorted keys, fixed separators and one trailing newline.
The returned catalog is authoritative; packaging should include its listed
indexes and catalog.json, not arbitrary older files in the output directory.

At runtime, `load_index(path)` returns an `OperationIndex` whose `operations`
map holds `Operation` records. `ProductIndexes(directory)` loads catalog entries
marked `primary` at construction; `get(document_id)` loads lower tiers on demand.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

Releases are tagged `v<version>`; a published GitHub release builds the wheel,
runs the downstream suites of confluence-as and jira-as against it, and only
then publishes to PyPI. This package is never promoted on its own: a product's
pinned build carries the core version it was released with (ADR 0013 in
grand-camel-platform).

## Generic Surface

`Surface(ProductIndexes(directory), transport_factory)` provides `call`, `search`,
`describe` and `topics`. The factory receives `(document_id, OperationIndex)` and
returns a transport implementing
`call(operation: Operation, parameters: Mapping[str, Any], body: Any) -> Response`.
`Response` carries `status`, `body` and `headers`. HTTP domain exceptions reuse
assistant-skills-lib; the product can pass its existing HTTP error mapper.
The surface converts failures to `SurfaceError`, including the operation note.
It closes transports that provide `close()` after each call. A direct Python
consumer can keep an `HTTPTransport` context open for pooled sequential calls.

Parameters are checked before the factory is called: required values, primitive
and array/object types, enums and retained bounds. `kebab_case` maps canonical
operation/parameter names to flags. Body fields are JSON-typed when parseable;
`--field 'spaceId="5"'` therefore supplies a string while `--field spaceId=5`
supplies an integer. Files and stdin must contain JSON. Conflicting dotted paths
are refused rather than overwritten. `parse_call_flags` accepts repeated arrays,
JSON arrays or comma-separated arrays, explicit `true`/`false`, and `--name=value`.
If a spec parameter collides with `body`, `field`, `format`, `validate-body` or
`help`, its flag is prefixed `--parameter-`. Duplicate scalar flags are refused.

Bodies are checked only with `validate_body=True` or after a 400. The small
checker supports local references, required/properties, additionalProperties:false,
nullable, enums, primitive types, bounds, arrays and allOf/oneOf/anyOf. Unsupported
validation keywords yield explicit diagnostics; format annotations are not
assertions. It neither imports jsonschema nor fetches references. Validation
reflects the indexed schema, including upstream defects such as overlapping
oneOf branches. Schema repair belongs in a product overlay.

`HTTPTransport` serializes path/query/header/cookie parameters, pools requests,
applies timeouts, and retries explicit 429 and all 5xx responses with exponential
backoff and numeric/date Retry-After. It never retries a 409, connection exception
or follows a redirect. Retries on mutation responses follow the existing product
policy. Bodies are JSON; operations requiring non-JSON media types fail explicitly.
`Surface.call(..., all_pages=True, limit=120)` follows `x-as-paging`, returns
its merged items array, and reports `count=120` through the warning callback;
one page is the default and the operation's `parameters["limit"]` remains page
size. Prerequisite aliases resolve exact keys through the declared lookup;
version tags inject current+1 or the draft override unless a version is supplied.
Conflicting aliases/ids fail before lookup; 409 exits with conflict code 7 and
never refreshes or retries. `Responder.seed(operation_id, responses)` queues
copied bodies or `Response` objects and `requests` records calls; exhausted
seeded queues fail. See [transform hooks](docs/transforms.md) for the ordered,
per-Surface registry and extension contract.

`Responder(index, status=200, body=...)` implements the identical call interface.
An explicit body wins, including null; otherwise forced error statuses produce a
message, and successes use a media/schema example or bounded deterministic schema
generation. It does not persist state or infer a root from reachable schemas.
Without an indexed 200 schema/example it returns null. Generated values are
representative, not a guarantee that every arbitrary schema constraint is met.

Search and topics visit primary indexes only. Search is case-insensitive over ID,
summary, path, tags and x-as-note, with every supplied word required to match.
Deprecated operations require `include_deprecated=True`. Naming a lower-tier
operation loads that tier on demand. Describe returns a JSON-ready document
(method/path, description, parameters, body outline, response schema and extensions)
that `describe_markdown` renders. All x-as tags and vendor scope tags remain visible.
`x-as-deprecation`/`x-as-deprecated` replacement metadata overrides the standard
OpenAPI deprecated flag; `x-as-topic` accepts a topic string or list.

### Additive index metadata

Old records load unchanged. New projections add fields only when source metadata
exists: `response_schema` for an inline 200 schema (the original `response_200`
remains the named reference or null), `response_example` for a media example,
`deprecated`, `request_body_required`, and `request_media_types`. Parameters retain
explicit `style`/`explode`. The original requestBody representation is unchanged.
These runtime additions require regenerating the product's packaged indexes through
its build hook; there is no runtime Base Document fallback.

See [exit codes](docs/exit-codes.md) for the machine-readable failure contract.
