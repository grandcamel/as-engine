# One core, two products

**Status: Proposed 2026-09-06 (wayfinder map JAS-6, ticket JAS-24; spec JAS-31).**

jira-as and confluence-as differed only in which OpenAPI documents they
served and which convenience commands they kept, yet each carried its own
client, mock, converters and help. We decided that one core package,
as-engine, holds the applier, normalizer and compiler, the index loader, the
Generic Surface, the transport, the transform registry, the guard, the help
renderer, the test doubles and serve mode, and that jira-as and confluence-as
are thin product packages holding Base Documents, overlays, Wrapper Verbs and
configuration. The core is generic in design, usable by any `*-as` product,
with the Atlassian converters isolated in one module. It is semver; each
product pins a compatible range; a core release runs both products' suites
downstream before publishing; and the core is never promoted on its own, a
product's pinned build carrying the core version it was released with. We
chose this over two independent engines, which would recreate the drift one
level down, and over folding the engine into assistant-skills-lib, which is
generic plumbing shared with an unrelated product and dormant. Confluence
pilots the core because its client was already thin and no organization
wrapper depends on it; Jira follows with the Compatibility Contract.

## Consequences

- Repositories stay separate: as-engine, jira-as, confluence-as and the two
  plugins; JAS is the single Ticket Queue with a component per repository.
- A change to the Atlassian converters is a core change; a change to a
  Base Document or an overlay is a product change.
