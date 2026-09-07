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

## [0.1.0a0] - 2026-09-06

- Package skeleton, CI, downstream-validated publish workflow, ADRs 0001
  and 0002 (JAS-32)
