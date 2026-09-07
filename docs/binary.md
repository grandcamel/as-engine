# Multipart requests and binary downloads

`HTTPTransport` selects multipart when `Operation.request_media_types` includes
`multipart/form-data` and has no JSON media type. No request enrichment tag is
needed. JSON retains precedence when both encodings are offered. A nonempty body
object maps each entry to one form part. A string beginning with `@` names a local
file: the filename is its basename, MIME type is guessed from its extension (or
`application/octet-stream`), and the part contains its bytes. Other values use the
normal scalar encoding, including lowercase booleans and compact JSON for nested
objects/arrays. The transport sends `X-Atlassian-Token: nocheck`; `requests` sets
the multipart Content-Type and boundary. File bytes are snapshotted once per call
so status retries resend the same content. Uploads are held in memory.

The existing body parser already retains untagged field values, so
`--field file=@f.png --field comment=hello` and `--body @parts.json` with
`{"file":"@f.png","comment":"hello"}` both work. `--body @file` still reads a
UTF-8 JSON document, and rich-text-tagged `@file` fields still read UTF-8 text.
Ordinary JSON bodies retain literal untagged `@path` values.

An operation tagged `x-as-response: {"kind":"binary"}` downloads successful
response bytes to disk. Python consumers can set an explicit destination with
`Surface.call(operation_id, parameters, output=Path("file.bin"))`; explicit output
also selects binary handling for an untagged operation. The transport's additive
keyword has the same meaning. Existing three-argument transport calls remain valid.
No generic `api call --output` flag is introduced by this change.

Without an explicit destination, a sanitized Content-Disposition filename is used
in the current directory. Path components and control characters are discarded;
an absent, empty or dot filename becomes `attachment.bin`. RFC 2231 encoded
filenames are supported. Explicit output names are honored as supplied and their
parent directory must exist. A temporary file in the destination directory is
replaced atomically after the stream completes; a failed stream removes the
temporary and preserves an existing destination. A successful call replaces an
existing destination. Bytes are streamed in 64 KiB chunks, never JSON/text decoded.

The normal `Response` contains status/headers and a small body
`{"path":"file.bin","bytes":123,"content_type":"application/octet-stream"}`.
The existing JSON/table/Markdown output renderer handles this body unchanged.
Non-success responses retain ordinary HTTP error handling. Connection and stream
failures are transport errors without a URL in the diagnostic; they are not retried.
429/5xx responses retain Retry-After and exponential backoff behavior, with the
normal retry budget independently applied to each permitted request hop. A 409 is
never retried.

Only one same-origin redirect is allowed for a binary GET/HEAD: scheme, hostname
and effective port must match. Userinfo, downgrade, arbitrary origins and a second
redirect are refused without a file write. The refusal names only the destination
hostname (or `<invalid>` when unparseable), never URL credentials, paths or query
strings. Same-origin follow-ups retain session authentication. The follow-up uses the Location query,
not the original parameters, and has no original request body. The pinned product
documents do not identify an exact Atlassian media hostname, so **all cross-origin
redirects are refused**, including Atlassian media URLs. This limitation is the
JAS-61 supervisor ruling; enabling a documented media origin requires a later
change and credential-free cross-origin requests. Ordinary JSON redirects remain
unfollowed.

## Offline doubles and recordings

`Responder` synthesizes stable binary bytes with a filename header. Explicit byte
bodies and seeded byte responses use the same file-output helper. Binary successes
with incompatible seeded bodies are refused; error responses retain their JSON.
Its existing `requests` tuples remain available. Multipart calls additionally
populate `wire_requests` with operationId, parameters, nocheck header and sorted
part descriptors: name, filename (null for scalar fields), size, content_type and
sha256. Descriptors contain no raw uploaded file bytes or directory paths.

Cassette `format_version: 1` and ordinary JSON recordings remain compatible.
Multipart request bodies are stored as `{"multipart":[part descriptors]}`, and
the existing canonical scrubbed-body hash matches the descriptors. Playback checks
file content hashes, filenames, scalar hashes and MIME metadata rather than local
file locations. Successful binary responses store `body: null` and `body_base64`,
plus status and headers; output paths are not recorded. The player validates base64
and the binary mode and writes the decoded bytes through the same atomic helper.
Missing binary data on a successful binary call is refused; no network fallback
exists.

Opaque binary content cannot be scrubbed as structured JSON. Base64 is preserved
exactly; before writing a cassette, Recorder refuses binary payloads containing any
registered literal secret, including those discovered from metadata in this or a
later interaction. Such a refusal leaves the previous cassette intact. Register
unlabelled secrets explicitly as for JSON recordings. Binary fixtures should use
synthetic payloads; a successful download file may already exist when recording is
refused.

`SimulationStore` adds an `attachments` seed collection. Each record has id, pageId,
title, mediaType and data_base64, with optional version/fileSize metadata. A
blogPostId may replace pageId for attachment metadata and downloads. The
default page `1` has `att1` (`first.bin`) and `att2` (`second.txt`). Metadata reads
(`getPageAttachments`, with cursor paging, and `getAttachmentById`) omit data_base64;
`downloadAttatchment` requires the matching page and attachment IDs and uses the
binary tag/output contract. Multipart createAttachment/updateAttachmentData
update attachment data and return JSON metadata; the store records multipart wire
metadata separately from its existing operation call tuples. This bounded state
model is an offline double, not evidence of live attachment compatibility.
