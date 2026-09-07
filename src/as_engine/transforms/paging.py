"""Execute only the continuation contract declared by x-as-paging."""

# Invalid tag/data types use the public ValueError usage contract.
# ruff: noqa: TRY004
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..transport import Response
from . import Context, Transform
from .values import MISSING, parameter, pointer, required

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


class Paging(Transform):
    def request(self, context: Context, tag: Any) -> None:
        if not isinstance(tag, dict) or tag.get("style") not in STYLES:
            raise ValueError("unknown paging style")
        if not context.all_pages:
            return
        style = tag["style"]
        for target in tag.get("request", {}).values():
            parameter(context, target)
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
        if tag.get("merge", "append") not in {"append", "prepend"}:
            raise ValueError("unknown paging merge order")
        if "itemsPaths" in tag:
            if style != "none" or context.limit is not None:
                raise ValueError("multiple paging arrays require none style and no aggregate limit")
        elif "itemsPath" not in tag:
            raise ValueError("missing paging itemsPath")
        if style in {"offset/limit", "start/limit"}:
            context.parameters.setdefault(roles["offset"]["name"], 0)

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
        parameters = dict(context.parameters)
        role = "offset" if tag["style"] in {"offset/limit", "start/limit"} else "token"
        name = tag.get("request", {}).get(role, {}).get("name")
        seen = {str(parameters[name])} if name in parameters else set()
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
            value = self._next(context, tag, response.body, items, parameters)
            if value is MISSING:
                break
            marker = str(value)
            if marker in seen:
                raise ValueError("repeated paging continuation")
            seen.add(marker)
            parameters[name] = value
            response = context.send(parameters, context.body)
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
            value = pointer(body, descriptor["path"])
            if style == "ancestor" and value is MISSING:
                raise ValueError("missing required ancestor id")
            if value is MISSING or value is None or value == "":
                return MISSING
            if descriptor["kind"] == "link":
                value = link_token(value, tag["request"]["token"]["name"], context.origin())
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                raise ValueError("paging continuation must be a string or integer")
            return value
        if not items:
            return MISSING
        metadata = tag.get("response", {})
        offset_name = tag["request"]["offset"]["name"]
        offset = integer(parameters[offset_name], "offset")
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
