# Compile the Enriched Spec at build, interpret the compact index at runtime, no generated client

**Status: Proposed 2026-09-06 (wayfinder map JAS-6, ticket JAS-16; spec JAS-31).**

Atlassian publishes about a thousand operations across four OpenAPI
documents, and Atlassian's own guidance is to bring your own code generator.
We decided that the build hook compiles each Enriched Spec (a vendored Base
Document with its Enrichment Entries applied) into a compact JSON operation
index shipped in the wheel, and that the engine interprets that index at
runtime: one call path takes an Operation record, parameters and a body.
There is no generated client. We chose this over generating typed modules
per operation, which produces a large tree that drifts the way the old
hand-written mock did, and over interpreting the raw document at runtime,
which the cold-start prototype (grand-camel-platform branch
prototype/cold-start) showed buys nothing: every index shape loads in about
90 ms against the incumbent CLI's 650 ms startup, 74 ms of it interpreter
start, format differences are noise, and tag subsets save memory rather than
time. Parameters are validated always by the engine's own checker; request
bodies only on request or after a 400, because importing jsonschema alone
costs 382 ms.

## Consequences

- A product's index is a build artifact, never committed; the repository
  holds Base Documents and overlays only.
- Primary indexes load on every invocation; lower-tier documents load on
  demand.
- Help is rendered from the index at runtime, never generated into files.
