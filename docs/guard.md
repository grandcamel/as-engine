# Operation scope guard

`x-as-scope` is an operation-level object. The default registry checks it at
order 5, before prerequisites (10), version (20), paging (100), and the requested
operation's transport send. Products own tag coverage: **untagged operations are
allowed silently**, including Confluence v1 in this pilot. This is not exhaustive
Confluence isolation. A present malformed tag, including null, refuses locally.

The pure `as_engine.guard.decide` function accepts a tag, allowlist, allow_site,
argv_identity, params and body. It returns a frozen Decision (allowed, identity,
reason), imports no product code and performs no I/O. Its optional
`resolved_identity` is a verified resolver result supplied by the hook, never an
unverified caller assertion. `decide(None)` denotes an absent tag; the registry
hook distinguishes an explicitly null operation tag and refuses it.

| `in` | Identity location | Example |
| --- | --- | --- |
| `path` | Required named path parameter | `{"in":"path","name":"project"}` |
| `key` | Named parameter or declared prerequisite alias | `{"in":"key","name":"space-key"}` |
| `body` | JSON Pointer and an explicit argv identity | `{"in":"body","path":"/spaceId"}` |
| `query` | Named query parameter, including arrays | `{"in":"query","name":"space-id"}` |
| `site` | Site-level operation | `{"in":"site"}` |

Keys are compared exactly, case-sensitively. There is no wildcard. Empty
allowlists refuse every scoped operation. Missing, empty, malformed or ambiguous
identities refuse. Every element of a nonempty identity array must be allowed.
Site operations require explicit `allow_site=True` independently of the allowlist.
For a body identity, a nonempty string argv identity must itself be allowed and
must equal the body value (or resolve to that body's id). An id's integer and
string spellings compare equally, but booleans do not count as ids. Normal body
schema validation remains separately opt-in and may reject a numeric JSON value.

A `key` tag may add `"separator":"-"` to derive an issue's project from the
last separator and an all-digit suffix. It never uses substring matching.
A `query` tag may add `"clause":"project"`; only a complete `project = KEY`
or `project IN (KEY, KEY)` expression is accepted, with optional single/double
quotes around keys. Clause identifiers and the IN operator are case-insensitive;
keys remain case-sensitive. Boolean expressions, extra predicates, functions,
unknown clauses and trailing syntax refuse. General JQL/CQL parsing is not
implemented. These mechanisms are independently tested with product-free tags.

## Consumer inputs and resolution

`Surface(..., scope_allowlist=(), scope_allow_site=False,
scope_resolution_rules={})` supplies defaults. `Surface.call` accepts per-call
`scope_allowlist`, `scope_allow_site` and `scope_argv_identity`. Defaults and per-call
values reach the Context and nested guarded calls; overrides do not persist into
later calls. The context also owns the dedicated `resolve_scope` method and a
private transport callback. A consumer's resolution policy is trusted code;
it is not loaded from the tag, body, parameters or environment.

A tag may declare one or two `resolve` steps. Each step maps the preceding
identity to a verified value, for example space id to key:

```json
{
  "in": "query", "name": "space-id",
  "resolve": [{
    "operationId": "getSpaces", "parameter": "ids", "array": true,
    "resultsPath": "/results", "matchPath": "/id", "valuePath": "/key"
  }]
}
```

For body tags, the first step receives the explicit argv key and resolves it to
the id which the body must contain. For other kinds it receives the parameter
identity. Omit resultsPath for a single response object; all paths are JSON
Pointers. Each requested identity must match exactly one row; missing/null values,
duplicates, non-success responses and continuation metadata refuse. Array lookups
batch all ids in one read. There is no pagination or arbitrary fallback search.
A route must fit in two steps and the complete route must be authorized before
its first read. Failure to prove membership refuses before the requested operation.

The consumer policy maps `document:operationId` to allowed exact parameter-name
sets, for example `{"v2:getSpaces": (("ids",), ("keys",))}`. Only same-document
GET operations with one of those exact sets are exempt; existing parameter
validation still applies. A context permits at most two resolution reads. The
resolver never recursively invokes the guarded pipeline and never sends a body.
It copies the operation with `x-as-resolution-read: true` for the ordinary
transport's request log. The original indexed operation is unchanged. The
stock Responder records its usual `(operationId, parameters, body)` tuples;
recording consumers/tests additionally retain this operation marker to distinguish
resolution from the requested operation. HTTP URLs and query parameters acquire
no extra marker. Retries remain the transport's existing policy.

The exemption is for resolution only. Calling a site-tagged getSpaces directly
still requires allow_site. Ordinary prerequisite/version calls remain guarded;
version enrichment can therefore perform its own scope resolution before its
normal version read. There is no general internal-call or allow_site bypass.

## Confluence pilot

`CONFLUENCE_ALLOWED_SPACES` is comma-separated space keys; absent or empty means
an empty allowlist. `CONFLUENCE_ALLOW_SITE_OPERATIONS` explicitly allows site
operations for values `1`, `true`, `yes`, `on` (case-insensitive). The corresponding
settings-file entries are `confluence.allowed_spaces` (comma-separated string)
and `confluence.allow_site_operations` (boolean or the truthy strings above).
Environment values override settings, including an empty allowlist or false.
Only the `api` call path uses this policy; legacy verbs are unchanged.

`api call createPage --space DOCS --field 'spaceId="55"' --field title=T`
first checks DOCS against the allowlist, then resolves `getSpaces keys=DOCS`.
Exactly one matching id 55 permits the mutation: **one resolution read followed
by one operation send**. A missing or disallowed --space makes zero requests.
An allowed --space paired with a different body's id reads once and refuses.
`--space-key DOCS` may supply the missing body id through the existing prerequisite
metadata but still requires `--space DOCS`. The hook verifies their equality,
resolves once, fills the target and consumes that alias so the prerequisite does
not repeat the lookup. An explicit id together with the alias remains conflicting.
No hidden key/id map is seeded, persisted or read from configuration.

`getPages` and other tagged space-filtered lists require a nonempty `--space-id`
filter. Every supplied id must resolve to an allowed key. No filter means refusal,
not an implicit site-wide list, even with allow_site enabled.

A page path id does not reveal its space. The explicitly authorized privacy
exception permits two metadata resolution reads before deciding: getPageById(id)
**without body-format**, then getSpaces(ids=spaceId) to read the space key. The
requested operation is sent only after membership is established. On refusal,
there are two resolution reads and zero operation sends. On success the original
body-format and other caller parameters apply only to the requested operation.
`getSpaceById --id N` resolves through getSpaces(ids=N), avoiding self-recursion.
Only v2:getPageById(id), v2:getSpaceById(id) and v2:getSpaces(ids or keys) are in
Confluence's exemption policy. It cannot request page content or any other
resource type. The exception reveals existence/ownership metadata; it grants
no out-of-scope content read or mutation. It does not make membership checking
and the later operation atomic against a concurrent space move.

## Refusals

Local failures use exit **4**, status **null**, and messages naming the operation,
found identity and allowlist form. They do not impersonate a server HTTP 403.
Lookups that cannot establish ownership also refuse locally; no requested
operation is sent. A missing-page resolution response is not exposed as server
404 or as a not-found message: it produces the same local scope-refusal status
as other unresolved membership. Resolution HTTP statuses and response bodies
are withheld from that diagnostic. The authorized metadata requests remain
visible in the transport log; this is not a constant-traffic privacy guarantee. HTTP errors from an operation which passed the guard retain
the existing Surface error contract. Untagged operations emit no guard messages.

## Confluence v2 coverage

The JAS-39 overlay tags 82 of 218 operations (45 path, 7 query, 7 body, 23 site).
The compiled entry checks validate every action's target and JAS-35 provenance.
The list below is a deliberate limitation, not a claim of exhaustive enforcement.

Tagged site: `getAdminKey`, `enableAdminKey`, `disableAdminKey`, `getAttachments`, `getLabels`, `getSpaces`, `createSpace`, `getAvailableSpacePermissions`, `listSpacePermissionCombinations`, `generateSpacePermissionCombinations`, `bulkAssignSpacePermissionRoles`, `bulkRemoveSpacePermissionAccess`, `createSpaceRole`, `getSpaceRoleMode`, `getFooterComments`, `getInlineComments`, `createBulkUserLookup`, `checkAccessByEmail`, `inviteByEmail`, `getDataPolicyMetadata`, `getDataPolicySpaces`, `getClassificationLevels`, `getForgeAppProperties`.

Tagged query: `getBlogPosts`, `getCustomContentByType`, `getLabelBlogPosts`, `getLabelPages`, `getPages`, `getAvailableSpaceRoles`, `getTasks`.

Tagged body: `createBlogPost`, `createCustomContent`, `createPage`, `createWhiteboard`, `createDatabase`, `createSmartLink`, `createFolder`.

Tagged path: `getPageById`, `updatePage`, `deletePage`, `getPageAttachments`, `getCustomContentByTypeInPage`, `getPageLabels`, `getPageLikeCount`, `getPageLikeUsers`, `getPageOperations`, `getPageContentProperties`, `createPageProperty`, `getPageContentPropertiesById`, `updatePagePropertyById`, `deletePagePropertyById`, `postRedactPage`, `updatePageTitle`, `getPageVersions`, `getPageVersionDetails`, `getSpaceById`, `getBlogPostsInSpace`, `getSpaceLabels`, `getSpaceContentLabels`, `getCustomContentByTypeInSpace`, `getSpaceOperations`, `getPagesInSpace`, `getSpaceProperties`, `createSpaceProperty`, `getSpacePropertyById`, `updateSpacePropertyById`, `deleteSpacePropertyById`, `getSpacePermissionsAssignments`, `getSpaceRoleAssignments`, `setSpaceRoleAssignments`, `getPageFooterComments`, `getPageInlineComments`, `getChildPages`, `getPageDirectChildren`, `getPageAncestors`, `getPageDescendants`, `getSpaceDefaultClassificationLevel`, `putSpaceDefaultClassificationLevel`, `deleteSpaceDefaultClassificationLevel`, `getPageClassificationLevel`, `putPageClassificationLevel`, `postPageClassificationLevel`.

Untagged operations and reasons (136):

| Operation | Reason |
| --- | --- |
| `getAttachmentById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `deleteAttachment` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getAttachmentLabels` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getAttachmentOperations` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getAttachmentContentProperties` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `createAttachmentProperty` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getAttachmentContentPropertiesById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `updateAttachmentPropertyById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `deleteAttachmentPropertyById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getAttachmentVersions` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getAttachmentVersionDetails` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getAttachmentComments` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getBlogPostById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `updateBlogPost` | Body destination spaceId does not prove ownership of the existing resource; required lookup is not authorized. |
| `deleteBlogPost` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getBlogpostAttachments` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getCustomContentByTypeInBlogPost` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getBlogPostLabels` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getBlogPostLikeCount` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getBlogPostLikeUsers` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getBlogpostContentProperties` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `createBlogpostProperty` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getBlogpostContentPropertiesById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `updateBlogpostPropertyById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `deleteBlogpostPropertyById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getBlogPostOperations` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getBlogPostVersions` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getBlogPostVersionDetails` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `convertContentIdsToContentTypes` | Bulk heterogeneous content ids have no single supported space-resolution route. |
| `getCustomContentById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `updateCustomContent` | Body destination spaceId does not prove ownership of the existing resource; required lookup is not authorized. |
| `deleteCustomContent` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getCustomContentAttachments` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getCustomContentComments` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getCustomContentLabels` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getCustomContentOperations` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getCustomContentContentProperties` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `createCustomContentProperty` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getCustomContentContentPropertiesById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `updateCustomContentPropertyById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `deleteCustomContentPropertyById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getLabelAttachments` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `postRedactBlog` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getWhiteboardById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `deleteWhiteboard` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getWhiteboardContentProperties` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `createWhiteboardProperty` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getWhiteboardContentPropertiesById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `updateWhiteboardPropertyById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `deleteWhiteboardPropertyById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getWhiteboardOperations` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getWhiteboardDirectChildren` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getWhiteboardDescendants` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getWhiteboardAncestors` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getDatabaseById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `deleteDatabase` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getDatabaseContentProperties` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `createDatabaseProperty` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getDatabaseContentPropertiesById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `updateDatabasePropertyById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `deleteDatabasePropertyById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getDatabaseOperations` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getDatabaseDirectChildren` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getDatabaseDescendants` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getDatabaseAncestors` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getSmartLinkById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `deleteSmartLink` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getSmartLinkContentProperties` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `createSmartLinkProperty` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getSmartLinkContentPropertiesById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `updateSmartLinkPropertyById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `deleteSmartLinkPropertyById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getSmartLinkOperations` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getSmartLinkDirectChildren` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getSmartLinkDescendants` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getSmartLinkAncestors` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getFolderById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `deleteFolder` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getFolderContentProperties` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `createFolderProperty` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getFolderContentPropertiesById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `updateFolderPropertyById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `deleteFolderPropertyById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getFolderOperations` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getFolderDirectChildren` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getFolderDescendants` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getFolderAncestors` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getCustomContentVersions` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getCustomContentVersionDetails` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getSpacePermissionTransitionTaskStatus` | Role or permission-task identity needs a separately reviewed site-level contract; not a space id. |
| `getSpaceRolesById` | Role or permission-task identity needs a separately reviewed site-level contract; not a space id. |
| `updateSpaceRole` | Role or permission-task identity needs a separately reviewed site-level contract; not a space id. |
| `deleteSpaceRole` | Role or permission-task identity needs a separately reviewed site-level contract; not a space id. |
| `getBlogPostFooterComments` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getBlogPostInlineComments` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `createFooterComment` | Multiple possible parent content fields require a reviewed parent-resolution contract. |
| `getFooterCommentById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `updateFooterComment` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `deleteFooterComment` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getFooterCommentChildren` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getFooterLikeCount` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getFooterLikeUsers` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getFooterCommentOperations` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getFooterCommentVersions` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getFooterCommentVersionDetails` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `createInlineComment` | Multiple possible parent content fields require a reviewed parent-resolution contract. |
| `getInlineCommentById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `updateInlineComment` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `deleteInlineComment` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getInlineCommentChildren` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getInlineLikeCount` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getInlineLikeUsers` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getInlineCommentOperations` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getInlineCommentVersions` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getInlineCommentVersionDetails` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getCommentContentProperties` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `createCommentProperty` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getCommentContentPropertiesById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `updateCommentPropertyById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `deleteCommentPropertyById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getTaskById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `updateTask` | Body destination spaceId does not prove ownership of the existing resource; required lookup is not authorized. |
| `getChildCustomContent` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getBlogPostClassificationLevel` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `putBlogPostClassificationLevel` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `postBlogPostClassificationLevel` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getWhiteboardClassificationLevel` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `putWhiteboardClassificationLevel` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `postWhiteboardClassificationLevel` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getDatabaseClassificationLevel` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `putDatabaseClassificationLevel` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `postDatabaseClassificationLevel` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getAttachmentThumbnailById` | Non-page content or parent identity requires a lookup outside the authorized page/space metadata operations. |
| `getForgeAppProperty` | App-property identity needs a separately reviewed site-level contract; not a space id. |
| `putForgeAppProperty` | App-property identity needs a separately reviewed site-level contract; not a space id. |
| `deleteForgeAppProperty` | App-property identity needs a separately reviewed site-level contract; not a space id. |

## Jira identity extensions (JAS-46)

The existing default remains `scope_allowlist=()` (deny scoped operations).
Consumers may configure a Surface with `scope_allowlist=None` for unrestricted
project membership. This does not remove tag validation, identity structure,
body/argv agreement, or the independent site-operation gate. A per-call
`scope_allowlist=None` still means inherit the Surface's policy; an explicit
empty per-call sequence denies scoped calls. Refusal JSON prints this configured
unrestricted policy as `allowlist=null`. Confluence's defaults are unchanged.

Issue-key tags with `separator` also accept nonempty arrays: every element must
have a project prefix and numeric issue suffix, and every derived project must
be allowed. A numeric-only issue ID cannot prove a project. If supplied, the
argv identity must agree with every named/resolved project, even when membership
is unrestricted. A site call's optional argv identity must itself be allowed.

Body tags additionally support a nonempty `paths` list of ordinary JSON Pointers
as an alternative to `path`. Every present key/id alternative must agree; a
present null, empty or malformed value refuses. These are alternatives in
location, not permission to ignore a conflicting value. No wildcard extraction
is supported. A single `path` may select a nonempty identity array; every item
must equal the explicit argv identity. A body `separator` derives project keys
from a scalar or array of issue keys. Body-only scope always requires a real,
matching argv identity, including JQL bodies and unrestricted membership.

A body tag may carry `clause: "project"` for JQL stored at its pointer. Both body
and query clause tags may opt into `conjunction: true`, a conservative literal
AND-only grammar. It requires a complete `project = KEY` or `project IN (...)`
restriction. Other predicates may use literal comparisons, IN lists or IS
EMPTY/NULL, joined only by AND. OR, NOT, functions, saved filters, boolean
grouping, ORDER BY and malformed or trailing syntax refuse. Quoted literal
values do not become boolean syntax. This is not general JQL parsing. The old
whole-clause-only grammar remains the default for tags without this opt-in.

A direct path/key/query tag may include `checks`, a list of body-only descriptors
such as `{"in":"body","paths":["/fields/project/key","/fields/project/id"],
"optional":true}`. The primary identity is proved first; each present secondary
identity must agree with that project. Missing secondary paths are skipped only
when optional; present invalid values refuse. The proved primary identity is
already visible in argv, so a keyed issue update need not repeat `--project`.
Recursive checks, resolvers, non-body descriptors and malformed metadata refuse
before transport. Jira uses this on editIssue and doTransition so a hidden
project change in a file body cannot disagree with the issue key.

Project-filter parameters also use the bounded validator's `uniqueItems`
keyword: metadata must be boolean; true rejects duplicate JSON-equal array
values (1 and 1.0 compare equal, while true and 1 differ). False imposes no
uniqueness requirement. Values are never silently deduplicated, and all other
unsupported validation keywords retain their existing refusal behavior.


## Optional JQL ordering (JAS-48)

A query or body clause tag with `conjunction: true` may declare
`order_by: ["key", "created", "updated"]`. The list must contain nonempty,
unique (case-insensitive) simple field names. After a fully proved AND-only
project filter, this opts into exactly one trailing
`ORDER BY <permitted-field> [ASC|DESC]`; keywords and field matching are
case-insensitive. Direction is optional. Multiple fields, unlisted fields,
functions, quoted grammar tokens, and any trailing predicates or syntax refuse.
Quoted predicate values remain literal text. OR, NOT, saved filters, missing
project restrictions and disallowed project identities still refuse. Body JQL
still requires a matching explicit command identity. Absent `order_by` retains
the previous refusal of all ordering clauses; malformed opt-in metadata refuses
even when the particular query has no ordering. Jira opts in only its replacement
GET/POST issue-search operations so the compatibility backlog can retain its
literal `ORDER BY key ASC` without dropping or rewriting the caller's query.


`{"in":"body","key_paths":["/inwardIssue/key","/outwardIssue/key"],
"separator":"-"}` is an additive multi-key form (JAS-48, linkIssues).
Every distinct JSON Pointer is required and must yield one valid scalar issue
key; every derived project must independently be allowed. A matching explicit
argv project must identify one of those keys. This permits links between two
allowed projects without turning alternative body locations into permission to
ignore one key. Missing/malformed keys, duplicate paths, arrays, conflicting
metadata forms, and any disallowed project refuse. Numeric issue IDs are not
project proof. Existing `path` / `paths` agreement semantics do not change.
