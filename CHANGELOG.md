# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
as-engine is never promoted on its own: a product's pinned build carries the
core version it was released with (grand-camel-platform ADR 0013).

## [Unreleased]

### Added

- The OpenAPI Overlay applier (update/remove subset: simple paths, bracket
  keys, bracket integer indexes; unsupported selectors and `copy` refused),
  normalization hooks (declared extensions stripped; defect normalization),
  the deterministic operation-index compiler (format_version 1 with a
  reachable-schemas table), the product build entry
  `as_engine.build.compile_product(spec_dir, out_dir)` with manifest pin
  verification, and the runtime index loader (`load_index`,
  `ProductIndexes`: primary tiers eager, lower tiers on demand) (JAS-34)
- Required provenance on every Enrichment Entry (`description`,
  `x-as-reason`, `x-as-evidence` {url, date}, `x-as-origin`, `x-as-test`),
  validated by `compile_product` before any output with the overlay, target
  and field named; product-wide unique test ids; `entry_cases` /
  `EntryCase.check` for generated per-entry tests (target exists, action
  applies, examples parse and validate through a caller-supplied callback);
  `docs/tags.md` freezes the `x-as-paging`, `x-as-prerequisites` and
  `x-as-version` contract (JAS-35)
- The Generic Surface: `surface` (call, search, describe, topics over
  `ProductIndexes`; kebab-case addressing; deprecated operations filtered
  and warned), `params` (required/type/enum/bounds checks before any
  request; bodies from files, stdin or dotted fields; optional body
  diagnostics), `transport` (typed Transport protocol and Response; pooled
  HTTP with timeouts and 429/5xx retry honouring Retry-After; no 409 retry),
  `responder` (stateless double from examples, inline or named schemas),
  `errors` (JSON error object; exit codes in `docs/exit-codes.md`),
  `output` (json, table, markdown); the compiler additionally records inline
  200 schemas, response examples, the `deprecated` flag, request-body
  required/media types and parameter style/explode when present (JAS-36)
- `cassette`: Recorder and Player transports at the transport seam (JSON
  format_version 1 keyed by operationId, canonical parameters and the
  scrubbed body hash; exact match or a clear miss, never a network fallback;
  recursive scrubbing of credentials, registered secrets and site
  identifiers before anything is persisted); `docs/cassettes.md` (JAS-42)
- Ordered tag-driven transform hooks, paging across six declared styles with
  aggregate limits and counts, exact prerequisite key resolution, current-
  version injection with draft override, and additive seeded responder queues.
  HTTP 409 has conflict exit code 7 and is never retried (JAS-37)
- Shared Markdown/ADF/Confluence-storage converters, labeled lossless
  placeholders, date/duration parsers, the pinned first-party ADF schema
  (57.3.4) with opt-in validation, and rich-text/representation transform
  contracts. Empty ADF text nodes and the bare-heading parser loop are fixed;
  product CLI/transform wiring follows in phase B (JAS-38 phase A)
- Product-independent four-level help documents with Markdown/JSON rendering,
  topic filtering, examples, token-cap snapshots and compatible full-
  description retention in the index (JAS-40)
- An operation-tag scope guard with default-deny allowlists, explicit site
  permission, body/argv identity binding and bounded consumer-authorized
  metadata resolution before prerequisite or operation sends; local scope
  refusal exits 4 with status null; per-call policy context on the Surface
  (JAS-39)
- Tag-driven rich-text (order 110) and scalar-format (order 30) transforms on
  the Generic Surface, per-call representation selection and raw response
  output, Markdown and @file input at tagged fields, lossless placeholder
  round trips; the converters package gains a structural check_document helper
  (JAS-38 phase B)
- JAS-41: `as_engine.simulation` — `SimulationStore` and `Simulation`, the
  opt-in stateful transport double for Wrapper Verb tests (spaces, pages and
  blog posts with versions, labels, restriction and space grants, content
  properties, templates; a bounded CQL subset; explicit 501/400/404/409); the
  `x-as-legacy-verbs` operation tag documented in `docs/tags.md`;
  `transport.py`, `surface.py` and `errors.py` defer the HTTP and shared
  error-library imports to first use so `api describe` no longer loads
  `requests`. (JAS-41)
- JAS-61: multipart form request bodies on the transport seam (`@path` values
  become file parts, the `X-Atlassian-Token: nocheck` header is sent, bytes
  snapshotted once for retry stability) and binary streamed downloads selected
  by the `x-as-response` tag (atomic output, `{path, bytes, content_type}`
  responses, one same-origin redirect; cross-origin refused naming the
  destination host); the responder, cassette (`body_base64`, multipart
  metadata without bytes) and simulation support both modes; additive
  `Surface.call(output=...)`; `docs/binary.md`. (JAS-61)
- JAS-63: operation discovery imports no HTTP or converter code — the Surface
  loads the rich-text validator at call time and builds its default transform
  registry on first access, and `output.py` keeps table formatting local
  (byte-equal to the shared library's tabulate output and its fallback)
  instead of importing the shared package; the import-isolation test now
  covers surface, output and help. (JAS-63)
- Exact OpenAPI parameter names accepted as call flags alongside the kebab
  form; paging transform gains declared body targets for POST paging, optional
  isLast termination on token paging, string-typed numeric offsets, and the
  paired emptyPage/requested offset contract; the responder decodes serialized
  JSON media examples when the declared schema is an object or array (JAS-45)

## [0.1.0a0] - 2026-09-06

- Package skeleton, CI, downstream-validated publish workflow, ADRs 0001
  and 0002 (JAS-32)
