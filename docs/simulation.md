# Simulation transport

`SimulationStore(seed=None)` is a small stateful transport fixture for wrapper
tests. Its default seed contains the `DOCS` space (`id` `55`) and pages `1`
(`First`) and `2` (`Second`, child of `1`). A seed is a JSON object whose
`spaces`, `pages`, `blogposts`, `templates`, `users`, `groups`, `restrictions`,
`space_permissions`, and `properties` keys replace the corresponding default
collections. `snapshot()` returns a detached JSON-shaped state copy; `calls` is
available separately on the store. `Simulation(store)`
implements the normal transport seam, records copied `(operationId, parameters,
body)` tuples in the shared `store.calls`, and has a no-op `close()`.

It supports only the page, space, blog, label, restriction, permission,
property, template, and identity operation IDs used by the surviving wrapper
verbs. Missing resources return 404, and updates with an explicit version other
than current plus one return 409. Unknown operations return a descriptive 501;
the simulation never falls back to a responder or HTTP.

`searchByCQL` accepts equality predicates on `space`, `spaceId`, `type`, and
`label`, joined with `AND`, plus `type IN (...)`. It also accepts `ORDER BY
created|lastmodified [ASC|DESC]`. Other CQL is refused with a 400 response.
