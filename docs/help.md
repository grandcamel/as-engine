# Progressive help

`as_engine.help` contains pure functions over an `OperationIndex`, a product
Level 0 template and a product group/verb listing. Documents contain `level`,
`title` and ordered `sections` (text, items, examples). `render_help(document,
format='markdown')` renders Markdown or JSON from that same document. Level 2
also retains the public describe record as metadata for existing consumers;
rendering a JSON document back to Markdown reproduces the original output.
No help function constructs a transport or reads credentials.

| Level | Entry point | Content | Budget |
| --- | --- | --- | --- |
| 0 | bare invocation, help | Product-authored surface map | 400 |
| 1 | help GROUP | Product verbs or indexed API-tag operations | 800 |
| 1 | help TOPIC | Only operations carrying that topic tag, notes, examples | 800 |
| 2 | api describe OPERATION; wrapper --help | First paragraph, parameters, body, risk, paging, replacement | 1200 |
| 3 | --examples | Enrichment invocations and JSON bodies | 600 |
| 1 | help topics | Available enrichment topics | 800 |

Budget estimates use `ceil(len(rendered_markdown)/4)` including the CLI's final
newline. This is a deterministic character proxy, not a model tokenizer.
JSON carries identical display content plus structural keys/describe metadata;
serialization overhead is not charged a second time. `--full` is intentionally
uncapped. Long group/topic lists use an explicit `--offset N` continuation;
entries are never silently dropped. The renderer does not silently truncate an
oversized individual entry: the golden/cap test fails, requiring author review.

The core does not assume an Atlassian product. `level0(template)`,
`group_document(index, group, groups)`, `topic_document(index, topic)`,
`operation_document(operation, index, full=False)`, `examples_document(operation)`
and `topics_document(index)` are public document builders. Examples are data,
never executed by help. Empty topics explicitly report no tagged entries.

## Tags

Operation-level `x-as-topic` is a list of topic names. Neither base OpenAPI
`tags` nor the word "adf" appearing in a description enrolls an operation in
`help adf`. The initial vocabulary is adf, paging, search, fields, project-types,
permissions, rate-limits, representations, sandbox, auth, scope, risk, errors.
Products may add topics. For older indexes a single string topic is accepted.
`x-as-note` is prose displayed in help and reused by the existing error object.

`x-as-risk` is `safe`, `destructive`, or `irreversible`; absence means safe.
Confluence's CLI enforces destructive/irreversible as a JSON preview by default,
returning exit 0 without calling Surface or performing prerequisite/version
reads. `--confirm` sends through the existing Surface and its guard/transforms.
The preview includes method, available resolved path, validated parameters and
body. Pending aliases or automatic version requirements are identified, never
fabricated. A preview is not proof that a later confirmed request will pass its
guard or remote validation. Direct Python Surface consumers retain their existing
call behavior: this CLI confirmation policy is enforced by the product adapter.

Level 3 consumes operation-level `x-as-examples` using the existing `kind`,
`value`, optional `schema` shape. Action-level examples remain executable
build-test evidence; products project desired runtime examples into the operation
via the overlay's `update` as well. Do not invent another example format.
Every action retains the provenance and unique test ID required by [tags.md](tags.md).

## Golden maintenance

Run `UPDATE_HELP_GOLDEN=1 .venv/bin/python -m pytest -q -p no:cacheprovider
 tests/test_help.py` (on one line) from either product to regenerate that
product's `tests/golden/help/` snapshots. Inspect the resulting diff. Run without
the switch to compare exactly. Cap checks remain enabled during regeneration;
no update switch accepts an oversized page. Tests also verify that deliberately
oversized pages exceed each cap, topics exclude untagged operations, continuation
covers every entry, and JSON round-trips to the exact Markdown.

The compiler preserves later source paragraphs in optional `full_description`
only when it differs from the existing trimmed `description`. Index
`format_version` remains 1: this is an additive optional field, defaulting to
None in older indexes, with the existing description bytes unchanged.
