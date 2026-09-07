# Rich text and scalar formats (JAS-38 phase A)

`as_engine.converters` is the only Atlassian-specific package in the engine.
This phase provides conversion APIs and the following tag contract. It does
not install a transform, change either product, or add command-line flags.

```python
from as_engine.converters import convert, render, validate_adf

adf = convert("# Notes\n\nHello **world**", target="adf")
storage = convert("# Notes\n\nHello **world**", target="storage")
markdown = render(adf)                       # also accepts JSON-string ADF
markdown = render(storage, source="storage")
validate_adf(adf)                            # explicit, optional dependency
```

`convert(text, source="markdown", target="adf" | "storage")` accepts literal
text and returns an ADF dictionary or a storage string. It does not open files
or infer JSON from text. `render(document, source="adf" | "storage",
target="markdown", placeholders=True)` defaults to preserving unsupported
content. Unsupported source/target pairs and malformed reserved tokens raise
`ValueError`; wrong argument types raise `TypeError`. File reading, body
wrapping, and raw-output selection belong to the transform/CLI integration.

The block IR is shared by both writers: dictionaries with `type`, text
`content`, heading `level`, optional code `language`, and list `items`.
Lists additionally carry `start` and one `children` block list per item.
An opaque block has `type="placeholder"` and its complete token as `content`.
The dialect covers headings 1–6, paragraphs, fenced code, quotes, rules,
nested bullet/ordered lists, bold, italic, inline code and links. It is a
small deterministic parser, not a complete CommonMark implementation.
Unsupported syntax is consumed as prose, never a non-progressing parser loop.

## Lossless placeholders

A token is self-contained and can be pasted into another process; it needs
no sidecar, cache, account lookup or network request. It has the grammar:

`{{as:1:SOURCE:PLACEMENT:KIND:"LABEL":PAYLOAD}}`

| Part | Meaning |
|---|---|
| `SOURCE` | `adf` or `storage`; restoration into the other format is refused |
| `PLACEMENT` | `inline`, or `block` on its own line |
| `KIND` | Readable node/macro kind, e.g. `mention`, `panel`, `mediaSingle`, `table` |
| `LABEL` | JSON-quoted human-readable display text; informational, editable independently of payload |
| `PAYLOAD` | Canonical unpadded base64url of UTF-8 JSON (complete ADF node) or original XML fragment |

Labels use JSON string escaping for quotes, backslashes and control characters;
Unicode remains readable. The decoder ignores the label, so changing a display
name does not change an account ID or the restored node. Delimiters and even
reserved-token-looking text inside the quoted label are inert. Emitted labels
use the mention display text (ID fallback), panel type and first words, media
alt text/filename (ID fallback), table text, or storage macro name.

Illustrative examples (the `PAYLOAD` marker stands for the complete encoded
subtree, not an abbreviated token to send to the converter):

| Kind | Example |
|---|---|
| Mention | `{{as:1:adf:inline:mention:"@Jason Krueger":PAYLOAD}}` |
| Panel | `{{as:1:adf:block:panel:"info: Read these notes":PAYLOAD}}` |
| Media | `{{as:1:adf:block:mediaSingle:"Architecture diagram":PAYLOAD}}` |
| Table | `{{as:1:adf:block:table:"Name Status Jason Ready":PAYLOAD}}` |
| Macro | `{{as:1:storage:block:jira:"jira":PAYLOAD}}` |

ADF JSON retains all attributes, child nodes, marks and mark ordering. ADF
rendering uses ordinary Markdown only when reparsing reconstructs the exact
node. Mentions use inline tokens, so adjacent prose stays editable. Panels,
media, layouts, task identity, extensions, and tables use complete block
subtrees. Unsupported marks or extra link metadata use inline text-node
tokens. This version preserves all ADF tables as tokens; it does not claim a
lossless pipe-table parser. Unsupported nested structures can use a larger
containing-node token. Empty `doc` versus empty `paragraph` is also preserved.

Storage retains otherwise lossy XML fragments, including macro parameters,
mentions/resource links, attachment identity, layout and table attributes.
Exact XML can require a larger enclosing token when ordinary Markdown would
normalize whitespace, tag spelling, or other lexical details.

Escape a literal reserved opener as `\{{as:...}}`; converter-produced Markdown
escapes literal token-looking prose. Code spans and fences are literal and do
not restore tokens. Malformed reserved tokens, foreign-format tokens and
misplaced block tokens fail rather than silently drop content. Tokens are data,
not commands, and confer no permission to upload media or resolve identities.
Decoded ADF is expected to have come from a valid source document; use explicit
`validate_adf` on untrusted hand-edited tokens. Empty text payloads are refused
even without the optional schema validator.

`placeholders=False` is an explicitly lossy display mode retaining the legacy
helper renderings, such as flattened unknown nodes, pipe tables and macro
callouts. It is different from CLI `--raw`: raw bypasses rendering entirely
and returns the stored representation. Products should use the public default
for editable read/write workflows.

## Schema provenance and validity

`converters/schema/full-57.3.4.json` is the byte-identical first-party
`@atlaskit/adf-schema` 57.3.4 full schema. `MANIFEST.json` records its URL,
SHA-256, fetch timestamp, version and Apache-2.0 license provenance. Stage-0
features are not admitted by this pin. The wheel includes both JSON files.

`validate_adf(document) -> True` uses Draft 4 and raises `ValueError` with
schema diagnostics. It imports `jsonschema` only on explicit validation;
`jsonschema` is a dev extra, never a runtime dependency or import on conversion.
All references in the pin are local. No schema is fetched at runtime.

Empty input becomes an empty paragraph, not an empty text node. Empty headings,
code blocks and list/table text produce empty content arrays. `create_text("")`
raises because a text node cannot represent that value. Jira's wiki adapter
keeps its own syntax (`*bold*`, `[label|url]`, one paragraph per nonblank line),
but its empty inline result is `[]`. These are the JAS-27 schema corrections.
The pinned schema forbids strong/emphasis/strike on inline code; when Markdown
wraps a code span in those marks, the code mark and text take precedence.
Schema validity is not a guarantee that every Atlassian endpoint supports all
nodes in the shared schema; endpoint representation tags still govern access.

## Operation tags for phase B and JAS-40

Tags are operation-level extensions so the current compiler/index preserves
them. Paths are JSON Pointers, with `~0`/`~1` escaping, as in `tags.md`.
No field-name guessing and no wildcards. Missing optional response paths are
left absent. A present malformed envelope or unsupported representation is an
error, not an implicit format conversion.

A Confluence page operation uses:

```json
{
  "x-as-richtext": [{
    "request": {"path": "/body", "shape": "envelope"},
    "response": {"path": "/body", "shape": "representation-map"},
    "representations": {
      "storage": {"converter": "storage", "encoding": "string"},
      "atlas_doc_format": {"converter": "adf", "encoding": "json-string"}
    }
  }],
  "x-as-representation": {
    "default": "storage",
    "alternatives": ["atlas_doc_format"]
  }
}
```

A descriptor may have only `request` or only `response`. `representations`
is the accepted set, and converter names are restricted to this package's
supported names. `encoding` is `object` for a direct ADF value, `json-string`
for stringified ADF, or `string` for storage. `shape="value"` is a direct
field (e.g. Jira `/fields/description`); `envelope` is
`{"representation": name, "value": encoded_document}`. A response
`representation-map` contains one or more envelopes under representation keys
(e.g. `body.storage.value`). Render each declared present representation;
retain envelope metadata and replace only `value` with Markdown for display.
A response descriptor may add `itemsPath="/results"`: its `path` is then
relative to each array item. For JAS-37's aggregated bare-array response,
iterate that root array instead, applying the same per-item `path`.

For a single-representation Jira value, omit `x-as-representation`; the only
representation is the default. With several accepted representations, require
the default tag. `--representation` overrides only among declared accepted
names. Reject conflicting explicit envelope representation and override.
Already structured inputs are preserved after checking the envelope; never
interpret their nested text as Markdown a second time. Raw string ADF in a
Confluence envelope stays stringified. The transform emits:

```json
{"body":{"representation":"storage","value":"<p>notes</p>"}}
```

or, for `--representation atlas_doc_format`:

```json
{"body":{"representation":"atlas_doc_format","value":"{\"type\":\"doc\",\"version\":1,\"content\":[{\"type\":\"paragraph\",\"content\":[{\"type\":\"text\",\"text\":\"notes\"}]}]}"}}
```

The vendor PageBodyWrite/PageNestedBodyWrite `oneOf` overlap is not repaired
here: retain this fact when phase B tests final request-body validation, and
do not mistake the envelope overlap for invalid ADF.

Scalar tags use a list:

```json
{"x-as-format":[
  {"target":{"in":"body","path":"/dueDate"},"format":"date"},
  {"target":{"in":"query","name":"elapsed"},"format":"duration"}
]}
```

`converters.formats.parse(value, format)` dispatches through `PARSERS`.
Dates require real `YYYY-MM-DD` calendar dates, without locale or timezone
inference. Duration returns integer seconds: an integer/decimal seconds string
or ordered nonrepeated `w d h m s` units, e.g. `2h30m` → 9000. Durations are
nonnegative elapsed time (24-hour days, seven-day weeks), **not Jira worklog
calendar units**. A product needing workday semantics must register a separate
format with explicit calendar configuration. Unknown formats are errors.

## Exact phase-B wiring list

1. Add `x-as-richtext`, `x-as-representation`, and relevant `x-as-format`
   product overlays with JAS-35 provenance/generated entry tests. Describe
   these tags in JAS-40 help, including accepted representations and defaults.
2. Implement a JAS-37 `Transform` registered on `x-as-richtext` at order 110.
   Its `request(context, tag)` converts after prerequisite/version hooks and
   before the main send; its `response(context, tag, response)` runs after
   paging (100), covering both one-page envelopes and merged arrays. Register
   scalar input conversion on `x-as-format`, e.g. order 30. Both dispatch via
   this package; no vendor branches in Surface or Registry.
3. Add per-call `representation` and `raw` options to the context/API and
   product CLI. Keep state per call. `--raw` bypasses the response hook only;
   it does not disable input conversion or request validation.
4. Extend tagged `--field path=value` processing to preserve Markdown strings
   and read `@file` as UTF-8 text at tagged rich-text paths. Ordinary JSON fields
   retain JAS-36 parsing. Report file errors before transport; this package
   never opens a path supplied as Markdown. Reject conflicts before lookups.
5. Build the exact request envelope using the tag, serialize ADF exactly once,
   and let existing post-hook validation see the final body. Preserve caller
   objects and already encoded documents. Response rendering retains metadata.
6. At the public argv/transport seams prove the two updatePage examples,
   default rendered reads, raw reads, mention preservation across separate
   invocations, formats, file errors, unsupported representations and final
   optional body-validation behavior. Test the nested-operation pipeline so
   prerequisite/version reads are not inadvertently converted at wrong paths.

Jira's environment-selected automatic field wrapping remains product policy
until that product migrates to tagged fields; it is not installed globally by
importing the converter package. Existing products continue using their own
helpers until phase B/product migration explicitly switches the call path.
