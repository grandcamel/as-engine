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
