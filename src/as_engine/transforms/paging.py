"""Execute only the continuation contract declared by x-as-paging."""

# Invalid tag/data types use the public ValueError usage contract.
# ruff: noqa: TRY004
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..params import _resolve
from ..transport import Response
from . import Context, Transform
from .values import MISSING, parameter, parts, pointer, required, set_target, target_value

STYLES = {"cursor", "nextPageToken", "offset/limit", "start/limit", "none", "ancestor"}


def integer(value: Any, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"paging {name} must be an integer >= {minimum}")
    return value


def link_token(link: Any, name: str, origin: str | None) -> str:
    if not isinstance(link, str) or any(c.isspace() for c in link) or "\\" in link:
        raise ValueError("invalid paging continuation link")
    parsed = urlsplit(link)
    if parsed.scheme or parsed.netloc:
        if not origin:
            raise ValueError("absolute paging link requires a configured service origin")
        base = urlsplit(origin)

        def authority(url: Any) -> tuple[Any, ...]:
            return (
                url.scheme.lower(),
                url.hostname,
                url.port or (443 if url.scheme.lower() == "https" else 80),
            )

        if (
            parsed.username
            or parsed.password
            or parsed.scheme not in {"http", "https"}
            or authority(parsed) != authority(base)
        ):
            raise ValueError("paging continuation link must remain on the service origin")
    if parsed.fragment:
        raise ValueError("paging continuation link must not contain a fragment")
    values = parse_qs(parsed.query, keep_blank_values=True).get(name, [])
    if len(values) != 1 or not values[0]:
        raise ValueError("paging continuation link is missing one unambiguous token")
    return values[0]


def target_schema(context: Context, target: Any) -> dict[str, Any]:
    """Validate a declared parameter or an existing request-body schema field."""
    if not isinstance(target, dict):
        raise ValueError("paging target must be an object")
    if target.get("in") != "body":
        declared = parameter(context, target)
        return _resolve(declared.get("schema", declared), context.index.schemas)
    path = target.get("path")
    if not isinstance(path, str):
        raise ValueError("paging body target must name a field")
    keys = parts(path)
    if not keys:
        raise ValueError("paging body target must name a field")
    request = context.operation.requestBody or {}
    schema = request.get("schema", {})
    if "ref" in request:
        schema = context.index.schemas.get(request["ref"], {})
    for key in keys:
        resolved = _resolve(schema, context.index.schemas)
        properties = dict(resolved.get("properties", {}))
        for child in resolved.get("allOf", []):
            properties.update(_resolve(child, context.index.schemas).get("properties", {}))
        if key not in properties:
            raise ValueError("paging body target is not declared")
        schema = properties[key]
    return _resolve(schema, context.index.schemas)


def offset_value(context: Context, target: Any) -> int:
    value = target_value(context, target)
    schema = target_schema(context, target)
    if schema.get("type") == "string" and isinstance(value, str) and value.isdecimal():
        value = int(value)
    return integer(value, "offset")


def assign(context: Context, target: Any, value: Any, *, offset: bool = False) -> None:
    if offset and target_schema(context, target).get("type") == "string":
        value = str(value)
    set_target(context, target, value)


class Paging(Transform):
    def request(self, context: Context, tag: Any) -> None:
        if not isinstance(tag, dict) or tag.get("style") not in STYLES:
            raise ValueError("unknown paging style")
        if not context.all_pages:
            return
        style = tag["style"]
        for target in tag.get("request", {}).values():
            target_schema(context, target)
        roles = tag.get("request", {})
        needed = (
            []
            if style == "none"
            else ["offset", "limit"]
            if style in {"offset/limit", "start/limit"}
            else ["token"]
        )
        if any(role not in roles for role in needed):
            raise ValueError("missing required paging request metadata")
        if "termination" in tag or "advance" in tag:
            if (
                style != "offset/limit"
                or tag.get("termination") != "emptyPage"
                or tag.get("advance") != "requested"
            ):
                raise ValueError(
                    "emptyPage termination requires offset/limit and requested advance"
                )
            limit_target = roles["limit"]
            if target_value(context, limit_target) is MISSING:
                default = target_schema(context, limit_target).get("default", MISSING)
                if default is MISSING:
                    raise ValueError(
                        "requested advance requires an explicit page size or schema default"
                    )
                assign(context, limit_target, default)
            integer(target_value(context, limit_target), "requested page size", 1)
        if tag.get("merge", "append") not in {"append", "prepend"}:
            raise ValueError("unknown paging merge order")
        if "itemsPaths" in tag:
            if style != "none" or context.limit is not None:
                raise ValueError("multiple paging arrays require none style and no aggregate limit")
        elif "itemsPath" not in tag:
            raise ValueError("missing paging itemsPath")
        if style in {"offset/limit", "start/limit"}:
            if target_value(context, roles["offset"]) is MISSING:
                assign(context, roles["offset"], 0, offset=True)
            offset_value(context, roles["offset"])

    def response(self, context: Context, tag: Any, response: Response) -> Response:
        if not context.all_pages:
            return response
        if "itemsPaths" in tag:
            arrays = [required(response.body, path) for path in tag["itemsPaths"]]
            if not all(isinstance(a, list) for a in arrays):
                raise ValueError("paging itemsPaths must identify arrays")
            context.state["count"] = sum(len(a) for a in arrays)
            return response
        merged: list[Any] = []
        page = replace(context, parameters=dict(context.parameters), body=deepcopy(context.body))
        role = "offset" if tag["style"] in {"offset/limit", "start/limit"} else "token"
        target = tag.get("request", {}).get(role)
        initial = target_value(page, target) if target is not None else MISSING
        seen = {str(initial)} if initial is not MISSING else set()
        first = response
        while True:
            items = required(response.body, tag["itemsPath"])
            if not isinstance(items, list):
                raise ValueError("paging itemsPath must identify an array")
            merged = items + merged if tag.get("merge") == "prepend" else merged + items
            if context.limit is not None and len(merged) >= context.limit:
                merged = merged[: context.limit]
                break
            if tag["style"] == "none":
                break
            value = self._next(page, tag, response.body, items, page.parameters)
            if value is MISSING:
                break
            marker = str(value)
            if marker in seen:
                raise ValueError("repeated paging continuation")
            seen.add(marker)
            assign(page, target, value, offset=role == "offset")
            response = context.send(page.parameters, page.body)
        context.state["count"] = len(merged)
        return Response(first.status, merged, first.headers)

    def _next(
        self, context: Context, tag: Any, body: Any, items: list[Any], parameters: dict[str, Any]
    ) -> Any:
        style = tag["style"]
        if style in {"cursor", "nextPageToken", "ancestor"}:
            if style == "ancestor" and not items:
                return MISSING
            descriptor = tag.get("next", {})
            if "path" not in descriptor or descriptor.get("kind") not in {"link", "token"}:
                raise ValueError("missing required paging next metadata")
            last = MISSING
            last_path = tag.get("response", {}).get("isLastPath")
            if last_path is not None:
                last = pointer(body, last_path)
                if last is not MISSING and not isinstance(last, bool):
                    raise ValueError("paging isLast must be a boolean")
                if last is True:
                    return MISSING
            value = pointer(body, descriptor["path"])
            if style == "ancestor" and value is MISSING:
                raise ValueError("missing required ancestor id")
            if value is MISSING or value is None or value == "":
                if last is False:
                    raise ValueError("non-final paging response is missing its continuation")
                return MISSING
            if descriptor["kind"] == "link":
                value = link_token(value, tag["request"]["token"]["name"], context.origin())
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                raise ValueError("paging continuation must be a string or integer")
            return value
        if not items:
            return MISSING
        metadata = tag.get("response", {})
        offset = offset_value(context, tag["request"]["offset"])
        if tag.get("termination") == "emptyPage":
            return offset + integer(
                target_value(context, tag["request"]["limit"]), "requested page size", 1
            )
        if style == "start/limit":
            if not all(k in metadata for k in ("offsetPath", "sizePath", "limitPath")):
                raise ValueError("missing required paging response metadata")
            returned = integer(required(body, metadata["offsetPath"]), "returned offset")
            size = integer(required(body, metadata["sizePath"]), "returned size")
            limit = integer(required(body, metadata["limitPath"]), "returned limit", 1)
            if size != len(items) or returned != offset:
                raise ValueError("inconsistent paging offset or size metadata")
            if size < limit:
                return MISSING
            return returned + size
        if not any(k in metadata for k in ("totalPath", "isLastPath")):
            raise ValueError("missing required paging total/isLast metadata")
        end = offset + len(items)
        stop = False
        if "totalPath" in metadata:
            total = integer(required(body, metadata["totalPath"]), "total")
            stop = end >= total
        if "isLastPath" in metadata:
            last = required(body, metadata["isLastPath"])
            if not isinstance(last, bool):
                raise ValueError("paging isLast must be a boolean")
            stop = stop or last
        return MISSING if stop else end
