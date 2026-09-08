# as-engine

Spec-driven core for jira-as and confluence-as. Read README.md, then the spec
(JAS-31 on jasonkrue.atlassian.net, file copy in grand-camel-platform at
docs/plans/atlassian-tooling-v2-spec-2026-09.md) and docs/adr/ here.

## Commands

Development prerequisites: see [README.md](README.md#development) for `pip install -e '.[dev]'` (includes hatchling and build), `python -m build --no-isolation`, oasdiff 1.31.0 installation and `OASDIFF=/path/to/oasdiff`.

```bash
pip install -e ".[dev]"
pytest                 # unit tests; coverage floor enforced in CI
ruff check --fix .
mypy src
```

## Rules

- One call path: an Operation record from the index plus a transport call. No
  per-operation methods, no generated client.
- Products own their Base Documents and overlays; this package owns the
  applier, normalizer, compiler, loader, Generic Surface, transport, transform
  registry, guard, help renderer, test doubles and serve mode.
- Atlassian-specific code lives only in the converters module; everything
  else is generic to any OpenAPI-described service.
- Tests sit at the argv seam, the transport seam and the build seam; never on
  internals.
- Tickets live in Jira project JAS (component as-engine).
