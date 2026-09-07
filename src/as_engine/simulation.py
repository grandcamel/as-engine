"""A deliberately small, stateful Confluence transport double.

The simulation is for wrapper tests.  It models only the indexed operations
used by those wrappers and refuses every other operation instead of guessing
at a Confluence response.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from .index import Operation
from .transport import (
    Response,
    binary_mode,
    binary_response,
    multipart_metadata,
    multipart_mode,
    multipart_parts,
)


def _default_seed() -> dict[str, Any]:
    return {
        "spaces": [{"id": "55", "key": "DOCS", "name": "Docs"}],
        "pages": [
            {"id": "1", "spaceId": "55", "title": "First", "body": {"representation": "storage", "value": "<p>First</p>"}, "version": {"number": 1}},
            {
                "id": "2",
                "spaceId": "55",
                "parentId": "1",
                "title": "Second",
                "body": {"representation": "storage", "value": "<p>Second</p>"},
                "version": {"number": 1},
            },
        ],
        "attachments": [
            {"id": "att1", "pageId": "1", "title": "first.bin",
             "mediaType": "application/octet-stream", "data_base64": "AAEC/w=="},
            {"id": "att2", "pageId": "1", "title": "second.txt",
             "mediaType": "text/plain", "data_base64": "U2Vjb25kXG4="},
        ],
        "blogposts": [],
        "templates": [],
        "users": [{"accountId": "sim-user", "displayName": "Simulation User"}],
        "groups": {"sim-user": []},
        "restrictions": {},
        "space_permissions": {},
        "properties": {},
    }


class SimulationStore:
    """Mutable JSON-shaped state shared by one or more simulation transports."""

    _collections = (
        "spaces",
        "pages",
        "blogposts",
        "attachments",
        "templates",
        "users",
        "groups",
        "restrictions",
        "space_permissions",
        "properties",
    )

    def __init__(self, seed: dict[str, Any] | None = None) -> None:
        state = _default_seed()
        if seed is not None:
            if not isinstance(seed, dict):
                raise ValueError("simulation seed must be a JSON object")
            for name in self._collections:
                if name in seed:
                    state[name] = deepcopy(seed[name])
        self.spaces = state["spaces"]
        self.pages = state["pages"]
        self.blogposts = state["blogposts"]
        self.attachments = state["attachments"]
        self.templates = state["templates"]
        self.users = state["users"]
        self.groups = state["groups"]
        self.restrictions = state["restrictions"]
        self.space_permissions = state["space_permissions"]
        self.properties = state["properties"]
        self.calls: list[tuple[str, dict[str, Any], Any]] = []
        self.wire_requests: list[dict[str, Any]] = []

    def snapshot(self) -> dict[str, Any]:
        """Return detached JSON-compatible state; calls remain available separately."""
        return deepcopy({name: getattr(self, name) for name in self._collections})


class Simulation:
    """Transport implementation backed by a :class:`SimulationStore`."""

    def __init__(self, store: SimulationStore) -> None:
        self.store = store
        self.calls = store.calls

    def close(self) -> None:
        """Match the transport lifecycle; simulation owns no resources."""

    def call(
        self, operation: Operation, parameters: Mapping[str, Any], body: Any,
        *, output: str | Path | None = None,
    ) -> Response:
        name = operation.operationId
        params = deepcopy(dict(parameters))
        payload = deepcopy(body)
        self.calls.append((name, params, payload))
        if multipart_mode(operation):
            self.store.wire_requests.append({
                "operationId": name, "parameters": deepcopy(params),
                "parts": multipart_metadata(body), "headers": {"X-Atlassian-Token": "nocheck"},
            })
        handler = getattr(self, f"_op_{name}", None)
        if handler is None:
            return self._error(501, f"simulation does not support operation: {name}")
        response = handler(params, payload)
        return binary_response(response, output) if binary_mode(operation, output) else response

    @staticmethod
    def _attachment_metadata(item: dict[str, Any]) -> dict[str, Any]:
        return deepcopy({k: v for k, v in item.items() if k != "data_base64"})

    def _attachment(self, attachment_id: Any) -> dict[str, Any] | None:
        return next((item for item in self.store.attachments
                     if self._id(item.get("id")) == self._id(attachment_id)), None)

    def _op_getPageAttachments(self, params: dict[str, Any], _body: Any) -> Response:
        if self._content(params["id"]) is None:
            return self._error(404, "Page not found")
        items = [self._attachment_metadata(item) for item in self.store.attachments
                 if self._id(item.get("pageId")) == self._id(params["id"])]
        offset = int(params.get("cursor", 0))
        limit = int(params.get("limit", 25))
        if offset < 0 or limit < 1:
            return self._error(400, "Invalid attachment page bounds")
        links = {"next": f"?cursor={offset + limit}"} if offset + limit < len(items) else {}
        return Response(200, {"results": items[offset:offset + limit], "_links": links})

    def _op_getAttachmentById(self, params: dict[str, Any], _body: Any) -> Response:
        item = self._attachment(params["id"])
        return (Response(200, self._attachment_metadata(item)) if item is not None
                else self._error(404, "Attachment not found"))

    def _op_downloadAttatchment(self, params: dict[str, Any], _body: Any) -> Response:
        item = self._attachment(params["attachmentId"])
        if item is None or self._id(item.get("pageId", item.get("blogPostId"))) != self._id(params["id"]):
            return self._error(404, "Attachment not found on page")
        try:
            content = base64.b64decode(item.get("data_base64", ""), validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid simulation attachment data_base64") from exc
        return Response(200, content, {
            "Content-Type": item.get("mediaType", "application/octet-stream"),
            "Content-Disposition": 'attachment; filename="' + item.get("title", "attachment.bin") + '"',
        })

    def _attachment_upload(self, params: dict[str, Any], body: Any, *, update: bool) -> Response:
        if self._content(params["id"]) is None:
            return self._error(404, "Page not found")
        parts = dict(multipart_parts(body))
        if "file" not in parts or parts["file"][0] is None:
            return self._error(400, "Attachment upload requires a file part")
        filename, content, content_type = parts["file"]
        if update:
            item = self._attachment(params["attachmentId"])
            if item is None or self._id(item.get("pageId", item.get("blogPostId"))) != self._id(params["id"]):
                return self._error(404, "Attachment not found on page")
        else:
            number = 1
            while self._attachment(f"att{number}") is not None:
                number += 1
            item = {"id": f"att{number}", "pageId": self._id(params["id"])}
            self.store.attachments.append(item)
        item.update({"title": filename, "mediaType": content_type, "fileSize": len(content),
                     "data_base64": base64.b64encode(content).decode("ascii"),
                     "version": {"number": item.get("version", {}).get("number", 0) + 1}})
        metadata = self._attachment_metadata(item)
        return Response(200, metadata if update else {"results": [metadata]})

    def _op_createAttachment(self, params: dict[str, Any], body: Any) -> Response:
        return self._attachment_upload(params, body, update=False)

    def _op_updateAttachmentData(self, params: dict[str, Any], body: Any) -> Response:
        return self._attachment_upload(params, body, update=True)

    @staticmethod
    def _error(status: int, message: str) -> Response:
        return Response(status, {"message": message})

    @staticmethod
    def _results(values: list[Any]) -> Response:
        return Response(200, {"results": deepcopy(values), "_links": {}})

    @staticmethod
    def _id(value: Any) -> str:
        return str(value)

    @staticmethod
    def _labels(item: dict[str, Any]) -> list[dict[str, str]]:
        labels = item.setdefault("labels", [])
        return [{"name": value} if isinstance(value, str) else value for value in labels]

    def _content(self, content_id: Any) -> dict[str, Any] | None:
        ident = self._id(content_id)
        for collection in (self.store.pages, self.store.blogposts):
            for item in collection:
                if self._id(item.get("id")) == ident:
                    return item
        return None

    @staticmethod
    def _content_response(item: dict[str, Any]) -> dict[str, Any]:
        """Render write-shaped bodies as the read-shaped representation map."""
        result = deepcopy(item)
        body = result.get("body")
        if isinstance(body, dict) and isinstance(body.get("representation"), str):
            result["body"] = {body["representation"]: body}
        return result

    def _space(self, identifier: Any, *, key: bool = False) -> dict[str, Any] | None:
        field = "key" if key else "id"
        ident = self._id(identifier)
        return next((s for s in self.store.spaces if self._id(s.get(field)) == ident), None)

    @staticmethod
    def _body_labels(body: Any) -> list[str]:
        values = body if isinstance(body, list) else body.get("labels", body) if isinstance(body, dict) else []
        if not isinstance(values, list):
            values = [values]
        result: list[str] = []
        for value in values:
            name = value.get("name") if isinstance(value, dict) else value
            if isinstance(name, str) and name:
                result.append(name)
        return result

    def _op_getSpaces(self, params: dict[str, Any], _body: Any) -> Response:
        spaces = self.store.spaces
        if params.get("ids") is not None:
            wanted = {self._id(v) for v in params["ids"]}
            spaces = [s for s in spaces if self._id(s.get("id")) in wanted]
        if params.get("keys") is not None:
            wanted = {self._id(v) for v in params["keys"]}
            spaces = [s for s in spaces if self._id(s.get("key")) in wanted]
        return self._results(spaces)

    def _op_getSpaceById(self, params: dict[str, Any], _body: Any) -> Response:
        space = self._space(params["id"])
        return Response(200, deepcopy(space)) if space else self._error(404, "Space not found")

    def _op_getPageById(self, params: dict[str, Any], _body: Any) -> Response:
        page = self._content(params["id"])
        return Response(200, self._content_response(page)) if page else self._error(404, "Content not found")

    def _op_createPage(self, _params: dict[str, Any], body: Any) -> Response:
        if not isinstance(body, dict) or "spaceId" not in body or "title" not in body:
            return self._error(400, "createPage requires spaceId and title")
        if not self._space(body["spaceId"]):
            return self._error(404, "Space not found")
        next_id = str(max([int(p["id"]) for p in self.store.pages if str(p.get("id", "")).isdigit()] or [0]) + 1)
        page = deepcopy(body)
        page["id"] = next_id
        page["spaceId"] = self._id(page["spaceId"])
        page.setdefault("version", {"number": 1})
        self.store.pages.append(page)
        return Response(201, self._content_response(page))

    def _op_updatePage(self, params: dict[str, Any], body: Any) -> Response:
        page = self._content(params["id"])
        if page is None:
            return self._error(404, "Content not found")
        if not isinstance(body, dict):
            return self._error(400, "updatePage requires an object body")
        supplied = body.get("version", {}).get("number") if isinstance(body.get("version"), dict) else None
        current = page.get("version", {}).get("number", 1)
        if supplied is not None and supplied != current + 1:
            return self._error(409, "Version conflict")
        page.update(deepcopy(body))
        page["id"] = self._id(params["id"])
        page["version"] = {"number": current + 1 if supplied is None else supplied}
        return Response(200, self._content_response(page))

    def _op_deletePage(self, params: dict[str, Any], _body: Any) -> Response:
        page = self._content(params["id"])
        if page is None:
            return self._error(404, "Content not found")
        self.store.pages.remove(page)
        return Response(204, None)

    def _op_getChildPages(self, params: dict[str, Any], _body: Any) -> Response:
        return self._results([p for p in self.store.pages if self._id(p.get("parentId", "")) == self._id(params["id"])])

    def _op_getPageAncestors(self, params: dict[str, Any], _body: Any) -> Response:
        page = self._content(params["id"])
        if page is None:
            return self._error(404, "Content not found")
        result: list[dict[str, Any]] = []
        while page.get("parentId") is not None:
            page = self._content(page["parentId"])
            if page is None:
                break
            result.insert(0, page)
        return self._results(result)

    def _op_getPageDescendants(self, params: dict[str, Any], _body: Any) -> Response:
        root = self._content(params["id"])
        if root is None:
            return self._error(404, "Content not found")
        result, pending = [], [self._id(params["id"])]
        while pending:
            parent = pending.pop(0)
            children = [p for p in self.store.pages if self._id(p.get("parentId", "")) == parent]
            result.extend(children)
            pending.extend(self._id(p["id"]) for p in children)
        return self._results(result)

    def _op_movePage(self, params: dict[str, Any], _body: Any) -> Response:
        page = self._content(params["pageId"])
        target = self._content(params["targetId"])
        if page is None or target is None:
            return self._error(404, "Content not found")
        position = params["position"]
        if position not in ("append", "prepend", "before", "after"):
            return self._error(400, "simulation move position is invalid")
        if position in ("append", "prepend"):
            page["parentId"] = self._id(target["id"])
        else:
            page["parentId"] = target.get("parentId")
        return Response(200, deepcopy(page))

    def _op_getPagesInSpace(self, params: dict[str, Any], _body: Any) -> Response:
        return self._results([p for p in self.store.pages if self._id(p.get("spaceId")) == self._id(params["id"])])

    def _op_getBlogPostsInSpace(self, params: dict[str, Any], _body: Any) -> Response:
        return self._results([p for p in self.store.blogposts if self._id(p.get("spaceId")) == self._id(params["id"])])

    def _op_searchByCQL(self, params: dict[str, Any], _body: Any) -> Response:
        try:
            values = self._cql(str(params["cql"]))
        except ValueError as exc:
            return self._error(400, str(exc))
        if "content.metadata.labels" in str(params.get("expand", "")):
            values = [
                {
                    "content": {
                        **deepcopy(value),
                        "metadata": {"labels": {"results": self._labels(value)}},
                    }
                }
                for value in values
            ]
        return self._results(values)

    def _cql(self, cql: str) -> list[dict[str, Any]]:
        import re

        order = None
        ordered = re.search(
            r"\s+ORDER\s+BY\s+(created|lastmodified)(?:\s+(ASC|DESC))?\s*$",
            cql,
            re.IGNORECASE,
        )
        if ordered:
            order = (ordered.group(1).casefold(), ordered.group(2).casefold() != "desc")
            cql = cql[: ordered.start()].strip()
        parts = re.split(r"\s+AND\s+", cql, flags=re.IGNORECASE)
        items: list[dict[str, Any]] = [*self.store.pages, *self.store.blogposts]
        for part in parts:
            term = part.strip()
            match = re.fullmatch(r"(space|spaceId|type|label)\s*=\s*['\"]?([^'\"\s]+)['\"]?", term, re.IGNORECASE)
            in_match = re.fullmatch(r"type\s+IN\s*\(([^)]+)\)", term, re.IGNORECASE)
            if match:
                field, expected = match.group(1).casefold(), match.group(2)
                if field in ("space", "spaceid"):
                    space = self._space(expected, key=field == "space")
                    items = [i for i in items if space and self._id(i.get("spaceId")) == self._id(space["id"])]
                elif field == "type":
                    wanted = expected.casefold().removesuffix("s")
                    items = [i for i in items if i in (self.store.pages if wanted == "page" else self.store.blogposts if wanted == "blogpost" else [])]
                else:
                    items = [i for i in items if expected in [x["name"] for x in self._labels(i)]]
            elif in_match:
                wanted = {x.strip().strip("'\"").casefold().removesuffix("s") for x in in_match.group(1).split(",")}
                items = [i for i in items if ("page" if i in self.store.pages else "blogpost") in wanted]
            else:
                raise ValueError("simulation CQL supports equality on space, spaceId, type, label; type IN; AND; and ORDER BY created or lastmodified")
        if order:
            key, ascending = order
            items.sort(key=lambda item: str(item.get(key, "")), reverse=not ascending)
        return items

    def _op_getPageLabels(self, params: dict[str, Any], _body: Any) -> Response:
        page = self._content(params["id"])
        return self._results(self._labels(page)) if page else self._error(404, "Content not found")

    def _op_addLabelsToContent(self, params: dict[str, Any], body: Any) -> Response:
        item = self._content(params["id"])
        if item is None:
            return self._error(404, "Content not found")
        current = {label["name"] for label in self._labels(item)}
        current.update(self._body_labels(body))
        item["labels"] = sorted(current)
        return Response(200, self._labels(item))

    def _op_removeLabelFromContent(self, params: dict[str, Any], _body: Any) -> Response:
        item = self._content(params["id"])
        if item is None:
            return self._error(404, "Content not found")
        label = self._id(params["label"])
        if label not in {entry["name"] for entry in self._labels(item)}:
            return self._error(404, "Label not found")
        item["labels"] = [entry["name"] for entry in self._labels(item) if entry["name"] != label]
        return Response(204, None)

    def _op_getRestrictions(self, params: dict[str, Any], _body: Any) -> Response:
        if not self._content(params["id"]): return self._error(404, "Content not found")
        return Response(200, deepcopy(self.store.restrictions.get(self._id(params["id"]), {"results": []})))

    def _op_addRestrictions(self, params: dict[str, Any], body: Any) -> Response:
        if not self._content(params["id"]): return self._error(404, "Content not found")
        self.store.restrictions[self._id(params["id"])] = deepcopy(body)
        return Response(200, deepcopy(body))

    _op_updateRestrictions = _op_addRestrictions

    def _op_deleteRestrictions(self, params: dict[str, Any], _body: Any) -> Response:
        if not self._content(params["id"]): return self._error(404, "Content not found")
        self.store.restrictions.pop(self._id(params["id"]), None)
        return Response(204, None)

    def _op_removeUserFromContentRestriction(self, params: dict[str, Any], _body: Any) -> Response:
        return self._remove_restriction(params, "user")

    def _op_removeGroupFromContentRestriction(self, params: dict[str, Any], _body: Any) -> Response:
        return self._remove_restriction(params, "group")

    def _op_addGroupToContentRestrictionByGroupId(
        self, params: dict[str, Any], _body: Any
    ) -> Response:
        return self._add_restriction(params, "group", params["groupId"])

    def _op_addUserToContentRestriction(self, params: dict[str, Any], _body: Any) -> Response:
        value = params.get("accountId") or params.get("key") or params.get("username")
        if value is None:
            return self._error(400, "content restriction user is required")
        return self._add_restriction(params, "user", value)

    def _add_restriction(self, params: dict[str, Any], kind: str, value: Any) -> Response:
        content_id = self._id(params["id"])
        if not self._content(content_id):
            return self._error(404, "Content not found")
        restriction = self.store.restrictions.setdefault(content_id, {"results": []})
        if not isinstance(restriction, dict):
            return self._error(409, "Restriction state is not an object")
        results = restriction.setdefault("results", [])
        if not isinstance(results, list):
            return self._error(409, "Restriction results are not a list")
        operation_key = self._id(params["operationKey"])
        entry = next((x for x in results if isinstance(x, dict) and x.get("operation") == operation_key), None)
        if entry is None:
            entry = {"operation": operation_key, kind + "s": {"results": []}}
            results.append(entry)
        subjects = entry.setdefault(kind + "s", {"results": []}).setdefault("results", [])
        subject_key = "groupId" if kind == "group" else "accountId"
        if not any(self._id(x.get(subject_key)) == self._id(value) for x in subjects):
            subjects.append({subject_key: self._id(value)})
        return Response(200, deepcopy(restriction))

    def _remove_restriction(self, params: dict[str, Any], kind: str) -> Response:
        content_id = self._id(params["id"])
        if not self._content(content_id): return self._error(404, "Content not found")
        entry = self.store.restrictions.get(content_id, {})
        needle = self._id(params.get("groupId") or params.get("accountId") or params.get("key") or params.get("username"))
        def keep(value: Any) -> bool:
            if not isinstance(value, dict): return True
            values = value.get(kind + "s", {}).get("results", [])
            if isinstance(values, list): value[kind + "s"]["results"] = [v for v in values if self._id(v.get("id") or v.get("groupId") or v.get("accountId") or v.get("name")) != needle]
            return True
        if isinstance(entry, dict):
            for value in entry.get("results", []): keep(value)
        return Response(204, None)

    def _op_getSpacePermissionsAssignments(self, params: dict[str, Any], _body: Any) -> Response:
        if not self._space(params["id"]): return self._error(404, "Space not found")
        return self._results(self.store.space_permissions.get(self._id(params["id"]), []))

    def _op_addPermissionToSpace(self, params: dict[str, Any], body: Any) -> Response:
        space = self._space(params["spaceKey"], key=True)
        if not space: return self._error(404, "Space not found")
        grants = self.store.space_permissions.setdefault(self._id(space["id"]), [])
        grant = deepcopy(body) if isinstance(body, dict) else {"value": deepcopy(body)}
        grant.setdefault("id", str(len(grants) + 1))
        grants.append(grant)
        return Response(200, deepcopy(grant))

    def _op_removePermission(self, params: dict[str, Any], _body: Any) -> Response:
        space = self._space(params["spaceKey"], key=True)
        if not space: return self._error(404, "Space not found")
        grants = self.store.space_permissions.get(self._id(space["id"]), [])
        grant = next((g for g in grants if self._id(g.get("id")) == self._id(params["id"])), None)
        if not grant: return self._error(404, "Permission not found")
        grants.remove(grant)
        return Response(204, None)

    def _op_getPageContentProperties(self, params: dict[str, Any], _body: Any) -> Response:
        page_id = self._id(params["page-id"])
        if not self._content(page_id): return self._error(404, "Content not found")
        values = self.store.properties.get(page_id, [])
        if params.get("key") is not None: values = [p for p in values if p.get("key") == params["key"]]
        return self._results(values)

    def _property(self, page_id: Any, property_id: Any) -> dict[str, Any] | None:
        return next((p for p in self.store.properties.get(self._id(page_id), []) if self._id(p.get("id")) == self._id(property_id)), None)

    def _op_getPageContentPropertiesById(self, params: dict[str, Any], _body: Any) -> Response:
        value = self._property(params["page-id"], params["property-id"])
        return Response(200, deepcopy(value)) if value else self._error(404, "Property not found")

    def _op_createPageProperty(self, params: dict[str, Any], body: Any) -> Response:
        page_id = self._id(params["page-id"])
        if not self._content(page_id): return self._error(404, "Content not found")
        values = self.store.properties.setdefault(page_id, [])
        value = deepcopy(body) if isinstance(body, dict) else {"value": deepcopy(body)}
        value.setdefault("id", str(len(values) + 1)); value.setdefault("version", {"number": 1})
        values.append(value)
        return Response(201, deepcopy(value))

    def _op_updatePagePropertyById(self, params: dict[str, Any], body: Any) -> Response:
        value = self._property(params["page-id"], params["property-id"])
        if not value: return self._error(404, "Property not found")
        if not isinstance(body, dict): return self._error(400, "property update requires an object body")
        current = value.get("version", {}).get("number", 1)
        supplied = body.get("version", {}).get("number") if isinstance(body.get("version"), dict) else None
        if supplied is not None and supplied != current + 1: return self._error(409, "Version conflict")
        value.update(deepcopy(body)); value["version"] = {"number": current + 1 if supplied is None else supplied}
        return Response(200, deepcopy(value))

    def _op_deletePagePropertyById(self, params: dict[str, Any], _body: Any) -> Response:
        values = self.store.properties.get(self._id(params["page-id"]), [])
        value = self._property(params["page-id"], params["property-id"])
        if not value: return self._error(404, "Property not found")
        values.remove(value); return Response(204, None)

    def _op_getContentTemplates(self, params: dict[str, Any], _body: Any) -> Response:
        values = [x for x in self.store.templates if x.get("kind", "content") == "content"]
        return self._offset_results(values, params)

    def _op_getBlueprintTemplates(self, params: dict[str, Any], _body: Any) -> Response:
        values = [x for x in self.store.templates if x.get("kind") == "blueprint"]
        return self._offset_results(values, params)

    @staticmethod
    def _offset_results(values: list[dict[str, Any]], params: dict[str, Any]) -> Response:
        start = int(params.get("start", 0))
        limit = int(params.get("limit", max(1, len(values))))
        page = values[start : start + limit]
        return Response(200, {"results": deepcopy(page), "start": start, "limit": limit, "size": len(page)})

    def _op_getContentTemplate(self, params: dict[str, Any], _body: Any) -> Response:
        value = next((x for x in self.store.templates if self._id(x.get("id")) == self._id(params["contentTemplateId"])), None)
        return Response(200, deepcopy(value)) if value else self._error(404, "Template not found")

    def _op_getCurrentUser(self, _params: dict[str, Any], _body: Any) -> Response:
        return Response(200, deepcopy(self.store.users[0])) if self.store.users else self._error(404, "Current user not found")

    def _op_getGroupMembershipsForUser(self, params: dict[str, Any], _body: Any) -> Response:
        account = self._id(params["accountId"])
        if not any(self._id(x.get("accountId")) == account for x in self.store.users): return self._error(404, "User not found")
        return self._results(self.store.groups.get(account, []))
