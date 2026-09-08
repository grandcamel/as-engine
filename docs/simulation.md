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

## Jira workflows

`JiraSimulationStore(seed=None)` and `JiraSimulation(store)` provide independent
Jira state through the same transport interface. The default seed contains
project `SBX`, issues `SBX-1` and `SBX-2`, Open/In Progress/Done transitions,
instance fields (including a textarea custom field), a user, and a board.
Seed keys replace entire collections: `issues`, `projects`, `fields`,
`transitions`, `users`, `sprints`, `boards`, `customers`, `desk_customers`,
`requests`, `approvals`, `slas`, and `articles`. Unknown collections are refused.
Reuse one store across calls to observe mutations; `snapshot()` returns a
detached copy, while `store.calls` records copied operation IDs, parameters,
and bodies. The existing Confluence store and defaults are unchanged.
Within a Jira store, issue keys advance per project and numeric issue IDs advance globally from the seed maxima and are never reused after deletion, like Jira Cloud.

Supported operations cover the Jira wrapper workflows: issue search, creation,
editing, deletion, transitions, links, comments and worklogs; project and field
discovery; autocomplete; boards and sprints; customer creation and desk
assignment; customer requests, transitions, approvals, SLAs, and articles.
Customer transitions preserve an `additionalComment` with a string `body` and
boolean `public`. `partiallyUpdateSprint` merges supplied fields; `updateSprint`
sets omitted writable sprint fields to null. Transition destinations include
status categories, so a Closed status in the Done category is treated as done.

JQL support is intentionally bounded: `AND`-joined `=`, `!=`, `IN`, and `NOT IN`
predicates for project, status, statusCategory, key/issuekey, sprint, issuetype,
and assignee; numeric comparisons for timespent; string comparisons for created
and updated; and `ORDER BY key|created|updated [ASC|DESC]`. Date comparisons do
not implement Jira date functions. Status-category predicates require seeded
statuses to include `statusCategory.key`. Unsupported JQL returns 400, missing
resources return 404, and unsupported operations return 501. This is a bounded
test fixture, with no responder or HTTP fallback, authentication service, or
complete Jira query engine. Use it through `Surface.call` to exercise the
product's scope and transform guards as well as state changes.
