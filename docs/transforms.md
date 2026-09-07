# Transform hooks

`Surface` owns a registry. Its default is a fresh `default_registry()`; pass
`registry=...` to the constructor to configure one explicitly. To extend the
existing defaults, call `surface.registry.register(tag, transform, order=...)`.
Duplicate tags are refused. Hooks are selected only for tags present on the
operation and run by ascending `(order, tag name)`. Instances are shared across
calls and must store call state in the context, never on themselves.

```python
from as_engine.transforms import Context, Transform
from as_engine.transport import Response

class Example(Transform):
    def request(self, context: Context, tag: object) -> None:
        # Update context.body or context.parameters, or raise ValueError.
        pass

    def response(self, context: Context, tag: object, response: Response) -> Response:
        return response

surface.registry.register("x-as-example", Example(), order=30)
```

Both hooks have default no-op implementations. `request(context, tag) -> None`
prepares input; `response(context, tag, response) -> Response` replaces output.
`Context` exposes `document`, `index`, `operation`, copied mutable `parameters`
and `body`, `aliases`, `all_pages`, `limit`, `warn` (optional message callback),
and a private-to-this-call `state` dictionary (extensions should namespace keys).
`state["count"]` is paging's final count, emitted only after successful hooks.
The caller's input objects remain unchanged.

`context.invoke(name, parameters, body=None, *, all_pages=False) -> Response`
runs the entire pipeline for an exact operationId in the same document. It
shares the transport, checks parameters, and rejects cyclic operation references.
Nested calls do not emit a paging count. Prerequisite and version lookups must
name GET operations. `context.send(parameters, body) -> Response` sends the
current operation through parameter/body validation and HTTP error conversion;
it bypasses hooks and is intended for continuation pages. `context.origin()`
returns the transport's configured `base_url`, or None. Absolute continuation
links require that origin; the engine never follows the link URL, only its
single declared token, on the original operation and parameters.

The defaults run prerequisites (10), version (20), then paging (100). Request
hooks all precede the first main send; response hooks run in the same order.
Use an order before 10 for a scope check that must precede lookups, after 100 for rich-text conversion
that prepares the input and then converts the entire merged output array.
Neither Surface nor Registry needs a new branch.

Parameters supplied by the caller are validated before any transport creation;
only required parameters supplied by an alias are deferred. Parameters are
validated again after hooks and on every page. Optional body validation sees
the transformed body. One transport instance lasts for the whole logical call,
including lookups and continuation pages, and is closed on success or failure.
HTTP 409 maps to exit 7, with no write retry or version refresh.

## Calling and paging

`Surface.call(name, parameters, body=None, *, all_pages=False, limit=None,
aliases=None, version=None, validate_body=False, warn=None)` returns `Response`.
`limit` is a positive aggregate cap requiring `all_pages=True`; request page
size stays in `parameters` under the tag's actual limit name. An undeclared
paging contract is refused with `all_pages=True`. One-page calls preserve the
original response wrapper. Aggregate calls return the declared items array;
`none` sends once. The exceptional `none` tag with `itemsPaths` preserves its
whole object, counts all declared arrays, and refuses an ambiguous total cap.

Cursor/link, direct cursor, nextPageToken, offset/limit, start/limit and ancestor
semantics follow [tags.md](tags.md). Missing array/required metadata, invalid
continuations, repeated tokens/offsets and unknown styles fail. Offset paging
advances by actual returned count. start/limit checks returned offset/size,
uses the returned limit to detect a short page, and stops at empty. Ancestor
pages prepend; a cap selects the first N items of the accumulated order at the
point it is reached and stops fetching higher ancestors. No arbitrary link URL
is requested, and filters and explicit page size survive continuation.

The CLI uses `--all --limit N` for aggregate limits. Without `--all`, existing
`--limit N` remains the OpenAPI page-size parameter and retains its bounds;
`--parameter-limit N` supplies page size explicitly in either mode. Key aliases
come from `x-as-prerequisites` (`--space-key`, for example). `--version N` on
version-tagged operations sets the body version; combining it with an existing
body version is a usage error. `--parameter-version` addresses a colliding spec
parameter. An explicit id alone bypasses lookup; id plus alias is a usage error.
