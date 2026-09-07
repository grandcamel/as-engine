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
