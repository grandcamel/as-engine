"""Public paging contracts exercised through compiled indexes and Surface."""

from __future__ import annotations

import json
from typing import Any

import pytest

from as_engine.compiler import compile_document
from as_engine.errors import SurfaceError
from as_engine.index import ProductIndexes
from as_engine.responder import Responder
from as_engine.surface import Surface


class OriginResponder(Responder):
    base_url = "https://offline.invalid/api"


def _parameter(name: str, kind: str = "string", where: str = "query") -> dict[str, Any]:
    return {"name": name, "in": where, "required": False, "schema": {"type": kind}}


def _surface(
    tmp_path: Any,
    paging: dict[str, Any],
    parameters: list[dict[str, Any]],
    *,
    path: str = "/items",
    responder_type: type[Responder] = Responder,
) -> tuple[Surface, Responder]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    document = {
        "openapi": "3.0.3",
        "paths": {
            path: {
                "get": {
                    "operationId": "getItems",
                    "parameters": parameters,
                    "responses": {"200": {"description": "ok"}},
                    "x-as-paging": paging,
                }
            }
        },
    }
    (tmp_path / "primary.json").write_text(json.dumps(compile_document(document)))
    (tmp_path / "catalog.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "documents": [{"id": "primary", "tier": "primary", "file": "primary.json"}],
            }
        )
    )
    indexes = ProductIndexes(tmp_path)
    _, index, _ = indexes.find("getItems")
    responder = responder_type(index)
    return Surface(indexes, lambda _, __: responder), responder


def _raises_paging(
    surface: Surface, message: str, parameters: dict[str, Any] | None = None
) -> None:
    with pytest.raises(SurfaceError) as caught:
        surface.call("getItems", parameters or {}, all_pages=True)
    assert caught.value.code == 2
    assert message in caught.value.messages


def test_cursor_links_preserve_filters_and_page_size(tmp_path):
    surface, responder = _surface(
        tmp_path,
        {
            "style": "cursor",
            "request": {
                "token": {"in": "query", "name": "cursor"},
                "limit": {"in": "query", "name": "pageSize"},
            },
            "itemsPath": "/results",
            "next": {"kind": "link", "path": "/_links/next"},
        },
        [_parameter("filter"), _parameter("cursor"), _parameter("pageSize", "integer")],
    )
    responder.seed(
        "getItems",
        [
            {"results": ["one", "two"], "_links": {"next": "/items?cursor=second"}},
            {"results": ["three"], "_links": {"next": None}},
        ],
    )

    assert surface.call("getItems", {"filter": "open", "pageSize": 2}, all_pages=True).body == [
        "one",
        "two",
        "three",
    ]
    assert responder.requests == [
        ("getItems", {"filter": "open", "pageSize": 2}, None),
        ("getItems", {"filter": "open", "pageSize": 2, "cursor": "second"}, None),
    ]


def test_cursor_direct_token_and_next_page_token_stop_on_null_or_empty(tmp_path):
    cursor, responder = _surface(
        tmp_path / "cursor",
        {
            "style": "cursor",
            "request": {"token": {"in": "query", "name": "cursor"}},
            "itemsPath": "/results",
            "next": {"kind": "token", "path": "/cursor"},
        },
        [_parameter("cursor")],
    )
    responder.seed(
        "getItems", [{"results": [1], "cursor": "again"}, {"results": [2], "cursor": ""}]
    )
    assert cursor.call("getItems", {}, all_pages=True).body == [1, 2]
    assert [request[1] for request in responder.requests] == [{}, {"cursor": "again"}]

    for stop in (None, ""):
        surface, next_responder = _surface(
            tmp_path / str(stop),
            {
                "style": "nextPageToken",
                "request": {"token": {"in": "query", "name": "pageToken"}},
                "itemsPath": "/results",
                "next": {"kind": "token", "path": "/nextPageToken"},
            },
            [_parameter("pageToken")],
        )
        next_responder.seed("getItems", [{"results": [stop], "nextPageToken": stop}])
        assert surface.call("getItems", {}, all_pages=True).body == [stop]
        assert len(next_responder.requests) == 1


def test_offset_limit_uses_actual_item_count_and_total_or_is_last(tmp_path):
    paging = {
        "style": "offset/limit",
        "request": {
            "offset": {"in": "query", "name": "offset"},
            "limit": {"in": "query", "name": "limit"},
        },
        "itemsPath": "/results",
        "response": {"totalPath": "/total", "isLastPath": "/isLast"},
    }
    surface, responder = _surface(
        tmp_path, paging, [_parameter("offset", "integer"), _parameter("limit", "integer")]
    )
    responder.seed(
        "getItems",
        [
            {"results": ["a", "b"], "total": 4, "isLast": False},
            {"results": ["c", "d"], "total": 4, "isLast": False},
        ],
    )
    assert surface.call("getItems", {"limit": 9}, all_pages=True).body == ["a", "b", "c", "d"]
    assert [request[1]["offset"] for request in responder.requests] == [0, 2]

    final, final_responder = _surface(
        tmp_path / "last", paging, [_parameter("offset", "integer"), _parameter("limit", "integer")]
    )
    final_responder.seed("getItems", [{"results": ["only"], "total": 10, "isLast": True}])
    assert final.call("getItems", {"limit": 9}, all_pages=True).body == ["only"]
    assert len(final_responder.requests) == 1


def test_start_limit_uses_returned_metadata_and_stops_on_short_page(tmp_path):
    surface, responder = _surface(
        tmp_path,
        {
            "style": "start/limit",
            "request": {
                "offset": {"in": "query", "name": "start"},
                "limit": {"in": "query", "name": "limit"},
            },
            "itemsPath": "/results",
            "response": {"offsetPath": "/start", "limitPath": "/limit", "sizePath": "/size"},
        },
        [_parameter("start", "integer"), _parameter("limit", "integer")],
    )
    responder.seed(
        "getItems",
        [
            {"results": ["a", "b"], "start": 0, "limit": 2, "size": 2},
            {"results": ["c"], "start": 2, "limit": 5, "size": 1},
        ],
    )
    assert surface.call("getItems", {"limit": 20}, all_pages=True).body == ["a", "b", "c"]
    assert [request[1]["start"] for request in responder.requests] == [0, 2]


def test_ancestor_uses_string_path_id_and_prepends_each_page(tmp_path):
    surface, responder = _surface(
        tmp_path,
        {
            "style": "ancestor",
            "request": {"token": {"in": "path", "name": "id"}},
            "itemsPath": "/results",
            "next": {"kind": "token", "path": "/results/0/id"},
            "merge": "prepend",
        },
        [_parameter("id", where="path")],
        path="/items/{id}",
    )
    responder.seed(
        "getItems",
        [
            {"results": [{"id": "parent"}]},
            {"results": [{"id": "root"}]},
            {"results": []},
        ],
    )
    assert surface.call("getItems", {"id": "leaf"}, all_pages=True).body == [
        {"id": "root"},
        {"id": "parent"},
    ]
    assert [request[1]["id"] for request in responder.requests] == ["leaf", "parent", "root"]


def test_none_makes_one_request_and_multi_array_none_keeps_whole_object(tmp_path):
    surface, responder = _surface(
        tmp_path,
        {"style": "none", "request": {}, "itemsPath": "/results"},
        [],
    )
    responder.seed("getItems", [{"results": ["one"]}, {"results": ["must not be called"]}])
    assert surface.call("getItems", {}, all_pages=True).body == ["one"]
    assert len(responder.requests) == 1

    multi, multi_responder = _surface(
        tmp_path / "multi",
        {"style": "none", "request": {}, "itemsPaths": ["/first", "/second"]},
        [],
    )
    response = {"first": [1], "second": [2, 3], "metadata": {"kept": True}}
    multi_responder.seed("getItems", [response])
    assert multi.call("getItems", {}, all_pages=True).body == response
    assert len(multi_responder.requests) == 1


def test_paging_cap_stops_requests_and_default_call_returns_one_page(tmp_path):
    paging = {
        "style": "cursor",
        "request": {"token": {"in": "query", "name": "cursor"}},
        "itemsPath": "/results",
        "next": {"kind": "token", "path": "/cursor"},
    }
    surface, responder = _surface(tmp_path, paging, [_parameter("cursor")])
    responder.seed("getItems", [{"results": [1, 2], "cursor": "more"}])
    assert surface.call("getItems", {}).body == {"results": [1, 2], "cursor": "more"}
    assert len(responder.requests) == 1

    capped, capped_responder = _surface(tmp_path / "cap", paging, [_parameter("cursor")])
    capped_responder.seed(
        "getItems",
        [
            {"results": [1, 2], "cursor": "more"},
            {"results": [3, 4], "cursor": "unused"},
            {"results": [5], "cursor": None},
        ],
    )
    assert capped.call("getItems", {}, all_pages=True, limit=3).body == [1, 2, 3]
    assert len(capped_responder.requests) == 2


@pytest.mark.parametrize(
    ("paging", "response", "message"),
    [
        (
            {"style": "mystery", "request": {}, "itemsPath": "/results"},
            None,
            "unknown paging style",
        ),
        (
            {"style": "cursor", "request": {}, "itemsPath": "/results"},
            None,
            "missing required paging request metadata",
        ),
        (
            {
                "style": "cursor",
                "request": {"token": {"in": "query", "name": "cursor"}},
                "itemsPath": "/results",
                "next": {"kind": "token", "path": "/cursor"},
            },
            {"cursor": None},
            "missing required response metadata: /results",
        ),
        (
            {
                "style": "offset/limit",
                "request": {
                    "offset": {"in": "query", "name": "offset"},
                    "limit": {"in": "query", "name": "limit"},
                },
                "itemsPath": "/results",
                "response": {},
            },
            {"results": [1]},
            "missing required paging total/isLast metadata",
        ),
    ],
)
def test_paging_rejects_unknown_style_and_missing_contract_data(
    tmp_path, paging, response, message
):
    names = {rule["name"] for rule in paging.get("request", {}).values()}
    parameters = [
        _parameter(name, "integer" if name in {"offset", "limit"} else "string") for name in names
    ]
    surface, responder = _surface(tmp_path, paging, parameters)
    if response is not None:
        responder.seed("getItems", [response])
    _raises_paging(surface, message)


def test_paging_rejects_repeated_tokens_and_bad_links(tmp_path):
    token = {
        "style": "nextPageToken",
        "request": {"token": {"in": "query", "name": "pageToken"}},
        "itemsPath": "/results",
        "next": {"kind": "token", "path": "/nextPageToken"},
    }
    surface, responder = _surface(tmp_path, token, [_parameter("pageToken")])
    responder.seed(
        "getItems",
        [
            {"results": [1], "nextPageToken": "again"},
            {"results": [2], "nextPageToken": "again"},
        ],
    )
    _raises_paging(surface, "repeated paging continuation")

    link = {
        "style": "cursor",
        "request": {"token": {"in": "query", "name": "cursor"}},
        "itemsPath": "/results",
        "next": {"kind": "link", "path": "/next"},
    }
    for value, message in (
        ("not a link", "invalid paging continuation link"),
        (
            "https://outside.invalid/items?cursor=next",
            "paging continuation link must remain on the service origin",
        ),
    ):
        linked, linked_responder = _surface(
            tmp_path / message.split()[0],
            link,
            [_parameter("cursor")],
            responder_type=OriginResponder,
        )
        linked_responder.seed("getItems", [{"results": [1], "next": value}])
        _raises_paging(linked, message)


def test_same_origin_absolute_cursor_link_is_allowed(tmp_path):
    surface, responder = _surface(
        tmp_path,
        {
            "style": "cursor",
            "request": {"token": {"in": "query", "name": "cursor"}},
            "itemsPath": "/results",
            "next": {"kind": "link", "path": "/next"},
        },
        [_parameter("cursor")],
        responder_type=OriginResponder,
    )
    responder.seed(
        "getItems",
        [
            {"results": [1], "next": "https://offline.invalid/api/items?cursor=next"},
            {"results": [2], "next": None},
        ],
    )
    assert surface.call("getItems", {}, all_pages=True).body == [1, 2]
